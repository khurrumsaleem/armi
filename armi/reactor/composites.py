# Copyright 2019 TerraPower, LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
This module contains the basic composite pattern underlying the reactor package.

This follows the principles of the `Composite Design Pattern
<https://en.wikipedia.org/wiki/Composite_pattern>`_ to allow the construction of a part/whole
hierarchy representing a physical nuclear reactor. The composite objects act somewhat like lists:
they can be indexed, iterated over, appended, extended, inserted, etc. Each member of the hierarchy
knows its children and its parent, so full access to the hierarchy is available from everywhere.
This design was chosen because of the close analogy of the model to the physical nature of nuclear
reactors.

Warning
-------
Because each member of the hierarchy is linked to the entire tree, it is often unsafe to save
references to individual members; it can cause large and unexpected memory inefficiencies.

See Also
--------
:doc:`/developer/index`.
"""

import collections
import itertools
import operator
import timeit
from typing import Callable, Dict, Iterator, List, Optional, Tuple, Type, Union

import numpy as np

from armi import context, runLog, utils
from armi.nucDirectory import elements, nucDir, nuclideBases
from armi.physics.neutronics.fissionProductModel import fissionProductModel
from armi.reactor import grids, parameters
from armi.reactor.flags import Flags, TypeSpec
from armi.reactor.parameters import resolveCollections
from armi.utils import densityTools, tabulate, units
from armi.utils.densityTools import calculateNumberDensity
from armi.utils.flags import auto


class FlagSerializer(parameters.Serializer):
    """
    Serializer implementation for Flags.

    This operates by converting each set of Flags (too large to fit in a uint64) into a
    sequence of enough uint8 elements to represent all flags. These constitute a
    dimension of a 2-D numpy array containing all Flags for all objects provided to the
    ``pack()`` function.
    """

    version = "1"

    @staticmethod
    def pack(data):
        """
        Flags are represented as a 2D numpy array of uint8 (single-byte, unsigned
        integers), where each row contains the bytes representing a single Flags
        instance. We also store the list of field names so that we can verify that the
        reader and the writer can agree on the meaning of each bit.

        Under the hood, this calls the private implementation providing the
        :py:class:`armi.reactor.flags.Flags` class as the target output class.
        """
        return FlagSerializer._packImpl(data, Flags)

    @staticmethod
    def _packImpl(data, flagCls: Type[utils.Flag]):
        """
        Implement the pack operation given a target output Flag class.

        This is kept separate from the public interface to permit testing of the
        functionality without having to do unholy things to ARMI's actual set of
        ``reactor.flags.Flags``.
        """
        npa = np.array([b for f in data for b in f.to_bytes()], dtype=np.uint8).reshape((len(data), flagCls.width()))

        return npa, {"flag_order": flagCls.sortedFields()}

    @staticmethod
    def _remapBits(inp: int, mapping: Dict[int, int]):
        """
        Given an input bitfield, map each bit to the appropriate new bit position based
        on the passed mapping.

        Parameters
        ----------
        inp : int
            input bitfield
        mapping : dict
            dictionary mapping from old bit position -> new bit position
        """
        f = 0
        for bit in itertools.count():
            if (1 << bit) > inp:
                break
            if (1 << bit) & inp:
                f = f | (1 << mapping[bit])

        return f

    @classmethod
    def unpack(cls, data, version, attrs):
        """
        Reverse the pack operation.

        This will allow for some degree of conversion from old flags to a new set of
        flags, as long as all of the source flags still exist in the current set of
        flags.

        Under the hood, this calls the private implementation providing the
        :py:class:`armi.reactor.flags.Flags` class as the target output class.
        """
        return cls._unpackImpl(data, version, attrs, Flags)

    @classmethod
    def _unpackImpl(cls, data, version, attrs, flagCls: Type[utils.Flag]):
        """
        Implement the unpack operation given a target output Flag class.

        This is kept separate from the public interface to permit testing of the
        functionality without having to do unholy things to ARMI's actual set of
        ``reactor.flags.Flags``.

        If the set of flags for the currently-configured App match the input set of
        flags, they are read in directly, which is good and cheap. However, if the set
        of flags differ from the input and the current App, we will try to convert them
        (as long as all of the input flags exist in the current App). Conversion is done
        by forming a map from all input bit positions to the current-App bit positions
        of the same meaning. E.g., if FUEL flag used to be the 3rd bit position, but now
        it is the 6th bit position, the map will contain ``map[3] = 6``. Then for each
        bitfield that is read in, each bit position is queried and if present, mapped to
        the proper corresponding new bit position. The result of this mapping is used to
        construct the Flags object.
        """
        flagOrderPassed = attrs["flag_order"]
        flagOrderNow = flagCls.sortedFields()

        if version != cls.version:
            raise ValueError(
                f"The FlagSerializer version used to pack the data ({version}) does not match "
                f"the current version ({cls.version})! This database either needs to be migrated, "
                "or on-the-fly inter-version conversion needs to be implemented."
            )

        flagSetIn = set(flagOrderPassed)
        flagSetNow = set(flagOrderNow)

        # Make sure that all of the old flags still exist
        if not flagSetIn.issubset(flagSetNow):
            missingFlags = flagSetIn - flagSetNow
            runLog.warning(
                "The set of flags in the database includes unknown flags. For convenience, we will "
                f"add these to the system: {missingFlags}"
            )
            flagCls.extend({k: auto() for k in missingFlags})

        flagOrderNow = flagCls.sortedFields()

        if all(i == j for i, j in zip(flagOrderPassed, flagOrderNow)):
            out = [flagCls.from_bytes(row.tobytes()) for row in data]
        else:
            newFlags = {i: flagOrderNow.index(oldFlag) for (i, oldFlag) in enumerate(flagOrderPassed)}
            out = [flagCls(cls._remapBits(int.from_bytes(row.tobytes(), byteorder="little"), newFlags)) for row in data]

        return out


def _defineBaseParameters():
    """
    Return parameter definitions that all ArmiObjects must have to function properly.

    For now, this pretty much just includes ``flags``, since these are used throughout
    the composite model to filter which objects are considered when traversing the
    reactor model.

    Note also that the base ParameterCollection class also has a ``serialNum``
    parameter. These are defined in different locations, since serialNum is a guaranteed
    feature of a ParameterCollection (for serialization to the database and history
    tracking), while the ``flags`` parameter is more a feature of the composite model.

    .. important::
        Notice that the ``flags`` parameter is not written to the database. This is for
        a couple of reasons:
        * Flags are derived from an ArmiObject's name. Since the name is stored on
        the DB, it is possible to recover the flags from that.
        * Storing flags to the DB may be complicated, since it is easier to imagine a
        number of flags that is greater than the width of natively-supported integer
        types, requiring some extra tricks to store the flags in an HDF5 file.
        * Allowing flags to be modified by plugins further complicates things, in that
        it is important to ensure that the meaning of all bits in the flag value are
        consistent between a database state and the current ARMI environment. This may
        require encoding these meanings in to the database as some form of metadata.
    """
    pDefs = parameters.ParameterDefinitionCollection()

    pDefs.add(
        parameters.Parameter(
            "flags",
            units=units.UNITLESS,
            description="The type specification of this object",
            location=parameters.ParamLocation.AVERAGE,
            saveToDB=True,
            default=Flags(0),
            setter=parameters.NoDefault,
            categories=set(),
            serializer=FlagSerializer,
        )
    )

    return pDefs


class CompositeModelType(resolveCollections.ResolveParametersMeta):
    """
    Metaclass for tracking subclasses of ArmiObject subclasses.

    It is often useful to have an easily-accessible collection of all classes that participate in
    the ARMI composite reactor model. This metaclass maintains a collection of all defined
    subclasses, called TYPES.
    """

    TYPES: Dict[str, Type] = dict()
    """
    Dictionary mapping class name to class object for all subclasses.

    :meta hide-value:
    """

    def __new__(cls, name, bases, attrs):
        newType = resolveCollections.ResolveParametersMeta.__new__(cls, name, bases, attrs)

        CompositeModelType.TYPES[name] = newType

        return newType


class ArmiObject(metaclass=CompositeModelType):
    """
    The abstract base class for all composites and leaves.

    This:

    * declares the interface for objects in the composition
    * implements default behavior for the interface common to all classes
    * Declares an interface for accessing and managing child objects
    * Defines an interface for accessing parents.

    Called "component" in gang of four, this is an ArmiObject here because the word component was
    already taken in ARMI.

    The :py:class:`armi.reactor.parameters.ResolveParametersMeta` metaclass is used to automatically
    create ``ParameterCollection`` subclasses for storing parameters associated with any particular
    subclass of ArmiObject. Defining a ``pDefs`` class attribute in the definition of a subclass of
    ArmiObject will lead to the creation of a new subclass of
    py:class:`armi.reactor.parameters.ParameterCollection`, which will contain the definitions from
    that class's ``pDefs`` as well as the definitions for all of its parents. A new
    ``paramCollectionType`` class attribute will be added to the ArmiObject subclass to reflect
    which type of parameter collection should be used.

    Warning
    -------
    This class has far too many public methods. We are in the midst of a composite tree cleanup that
    will likely break these out onto a number of separate functional classes grouping things like
    composition, location, shape/dimensions, and various physics queries. Methods are being
    collected here from the various specialized subclasses (Block, Assembly) in preparation for this
    next step. As a result, the public API on this method should be considered unstable.

    .. impl:: Parameters are accessible throughout the armi tree.
        :id: I_ARMI_PARAM1
        :implements: R_ARMI_PARAM

        An ARMI reactor model is composed of collections of ARMIObject objects. These
        objects are combined in a hierarchical manner. Each level of the composite tree
        is able to be assigned parameters which define it, such as temperature, flux,
        or keff values. This class defines an attribute of type ``ParameterCollection``,
        which contains all the functionality of an ARMI ``Parameter`` object. Because
        the entire model is composed of ARMIObjects at the most basic level, each level
        of the Composite tree contains this parameter attribute and can thus be queried.

    Attributes
    ----------
    name : str
        Object name
    parent : ArmiObject
        The object's parent in a hierarchical tree
    cached : dict
        Some cached values for performance
    p : ParameterCollection
        The state variables
    spatialGrid : grids.Grid
        The spatial grid that this object contains
    spatialLocator : grids.LocationBase
        The location of this object in its parent grid, or global space

    See Also
    --------
    armi.reactor.parameters
    """

    paramCollectionType: Optional[Type[parameters.ParameterCollection]] = None
    pDefs = _defineBaseParameters()

    def __init__(self, name):
        self.name = name
        self.parent = None
        self.cached = {}
        self._backupCache = None
        self.p = self.paramCollectionType()
        # NOTE: LFPs are not serialized to the database, which could matter when loading an old DB.
        self._lumpedFissionProducts = None
        self.spatialGrid = None
        self.spatialLocator = grids.CoordinateLocation(0.0, 0.0, 0.0, None)

    def __lt__(self, other):
        """
        Implement the less-than operator.

        Implementing this on the ArmiObject allows most objects, under most circumstances to be
        sorted. This is useful from the context of the Database classes, so that they can produce a
        stable layout of the serialized composite structure.

        By default, this sorts using the spatial locator in K, J, I order, which should give a
        relatively intuitive order. It also makes sure that the objects being sorted live in the
        same grid.
        """
        if self.spatialLocator is None or other.spatialLocator is None:
            runLog.error(f"could not compare {self} and {other}")
            raise ValueError("One or more of the compared objects have no spatialLocator")

        if self.spatialLocator.grid is not other.spatialLocator.grid:
            runLog.error(f"could not compare {self} and {other}")
            raise ValueError(
                "Composite grids must be the same to compare:\n"
                f"This grid: {self.spatialGrid}\n"
                f"Other grid: {other.spatialGrid}"
            )
        try:
            t1 = tuple(reversed(self.spatialLocator.getCompleteIndices()))
            t2 = tuple(reversed(other.spatialLocator.getCompleteIndices()))
            return t1 < t2
        except ValueError:
            runLog.error(f"failed to compare {self.spatialLocator} and {other.spatialLocator}")
            raise

    def __getstate__(self):
        """
        Python method for reducing data before pickling.

        This removes links to parent objects, which allows one to, for example, pickle
        an assembly without pickling the entire reactor. Likewise, one could
        MPI_COMM.bcast an assembly without broadcasting the entire reactor.

        Notes
        -----
        Special treatment of ``parent`` is not enough, since the spatialGrid also
        contains a reference back to the armiObject. Consequently, the ``spatialGrid``
        needs to be reassigned in ``__setstate__``.
        """
        state = self.__dict__.copy()
        state["parent"] = None

        if "r" in state:
            raise RuntimeError("An ArmiObject should never contain the entire Reactor.")

        return state

    def __setstate__(self, state):
        """
        Sets the state of this ArmiObject.

        Notes
        -----
        This ArmiObject may have lost a reference to its parent. If the parent was also
        pickled (serialized), then the parent should update the ``.parent`` attribute
        during its own ``__setstate__``. That means within the context of
        ``__setstate__`` one should not rely upon ``self.parent``.
        """
        self.__dict__.update(state)

        if self.spatialGrid is not None:
            self.spatialGrid.armiObject = self
            # Spatial locators also get disassociated with their grids when detached;
            # make sure they get hooked back up
            for c in self:
                c.spatialLocator.associate(self.spatialGrid)

        # now "reattach" children
        for c in self:
            c.parent = self

    def __repr__(self):
        return f"<{self.__class__.__name__}: {self.name}>"

    def __format__(self, spec):
        return format(str(self), spec)

    def __bool__(self):
        """
        Flag that says this is non-zero in a boolean context.

        Notes
        -----
        The default behavior for ``not [obj]`` that has a  ``__len__`` defined is to see
        if the length is zero. However, for these composites, we'd like Assemblies, etc.
        to be considered non-zero even if they don't have any blocks. This is important
        for parent resolution, etc. If one of these objects exists, it is non-zero,
        regardless of its contents.
        """
        return True

    def __add__(self, other):
        """Return a list of all children in this and another object."""
        return self.getChildren() + other.getChildren()

    def duplicate(self):
        """
        Make a clean copy of this object.

        Warning
        -------
        Be careful with inter-object dependencies. If one object contains a reference to another
        object which contains links to the entire hierarchical tree, memory can fill up rather
        rapidly. Weak references are designed to help with this problem.
        """
        raise NotImplementedError

    def clearCache(self):
        """Clear the cache so all new values are recomputed."""
        self.cached = {}
        for child in self:
            child.clearCache()

    def _getCached(self, name):
        """
        Obtain a value from the cache.

        Cached values can be used to temporarily store frequently read but long-to-compute values.
        The practice is generally discouraged because it's challenging to make sure to properly
        invalidate the cache when the state changes.
        """
        return self.cached.get(name, None)

    def _setCache(self, name, val):
        """
        Set a value in the cache.

        See Also
        --------
        _getCached : returns a previously-cached value
        """
        self.cached[name] = val

    def copyParamsFrom(self, other):
        """
        Overwrite this object's params with other object's.

        Parameters
        ----------
        other : ArmiObject
            The object to copy params from
        """
        self.p = other.p.__class__()
        for p, val in other.p.items():
            self.p[p] = val

    def updateParamsFrom(self, new):
        """
        Update this object's params with a new object's.

        Parameters
        ----------
        new : ArmiObject
            The object to copy params from
        """
        for paramName, val in new.p.items():
            self.p[paramName] = val

    def iterChildren(
        self,
        deep=False,
        generationNum=1,
        predicate: Optional[Callable[["ArmiObject"], bool]] = None,
    ) -> Iterator["ArmiObject"]:
        """Iterate over children of this object."""
        raise NotImplementedError()

    def getChildren(self, deep=False, generationNum=1, includeMaterials=False) -> list["ArmiObject"]:
        """Return the children of this object."""
        raise NotImplementedError()

    def iterChildrenWithFlags(self, typeSpec: TypeSpec, exactMatch=False) -> Iterator["ArmiObject"]:
        """Produce an iterator of children that have given flags."""
        return self.iterChildren(predicate=lambda o: o.hasFlags(typeSpec, exactMatch))

    def getChildrenWithFlags(self, typeSpec: TypeSpec, exactMatch=False) -> list["ArmiObject"]:
        """Get all children that have given flags."""
        return list(self.iterChildrenWithFlags(typeSpec, exactMatch))

    def iterChildrenOfType(self, typeName: str) -> Iterator["ArmiObject"]:
        """Iterate over children that have a specific input type name."""
        return self.iterChildren(predicate=lambda o: o.getType() == typeName)

    def getChildrenOfType(self, typeName: str) -> list["ArmiObject"]:
        """Produce a list of children that have a specific input type name."""
        return list(self.iterChildrenOfType(typeName))

    def getComponents(self, typeSpec: TypeSpec = None, exact=False):
        """
        Return all armi.reactor.component.Component within this Composite.

        Parameters
        ----------
        typeSpec : TypeSpec
            Component flags. Will restrict Components to specific ones matching the
            flags specified.

        exact : bool, optional
            Only match exact component labels (names). If True, 'coolant' will not match
            'interCoolant'.  This has no impact if compLabel is None.

        Returns
        -------
        list of Component
            items matching compLabel and exact criteria
        """
        raise NotImplementedError()

    def iterComponents(self, typeSpec: TypeSpec = None, exact=False):
        """Yield components one by one in a generator."""
        raise NotImplementedError()

    def doChildrenHaveFlags(self, typeSpec: TypeSpec, deep=False):
        """
        Generator that yields True if the next child has given flags.

        Parameters
        ----------
        typeSpec : TypeSpec
            Requested type of the child
        """
        for c in self.getChildren(deep):
            if c.hasFlags(typeSpec, exact=False):
                yield True
            else:
                yield False

    def containsAtLeastOneChildWithFlags(self, typeSpec: TypeSpec):
        """
        Return True if any of the children are of a given type.

        Parameters
        ----------
        typeSpec : TypeSpec
            Requested type of the children

        See Also
        --------
        self.doChildrenHaveFlags
        self.containsOnlyChildrenWithFlags
        """
        return any(self.doChildrenHaveFlags(typeSpec))

    def containsOnlyChildrenWithFlags(self, typeSpec: TypeSpec):
        """
        Return True if all of the children are of a given type.

        Parameters
        ----------
        typeSpec : TypeSpec
            Requested type of the children

        See Also
        --------
        self.doChildrenHaveFlags
        self.containsAtLeastOneChildWithFlags
        """
        return all(self.doChildrenHaveFlags(typeSpec))

    def copyParamsToChildren(self, paramNames):
        """
        Copy param values in paramNames to all children.

        Parameters
        ----------
        paramNames : list
            List of param names to copy to children

        """
        for paramName in paramNames:
            myVal = self.p[paramName]
            for c in self:
                c.p[paramName] = myVal

    @classmethod
    def getParameterCollection(cls):
        """
        Return a new instance of the specific ParameterCollection type associated with this object.

        This has the same effect as ``obj.paramCollectionType()``. Getting a new
        instance through a class method like this is useful in situations where the
        ``paramCollectionType`` is not a top-level object and therefore cannot be
        trivially pickled. Since we know that by the time we want to make any instances
        of/unpickle a given ``ArmiObject``, such a class attribute will have been
        created and associated. So, we use this top-level method to dig
        dynamically down to the underlying parameter collection type.

        .. impl:: Composites (and all ARMI objects) have parameter collections.
            :id: I_ARMI_CMP_PARAMS
            :implements: R_ARMI_CMP_PARAMS

            This class method allows a user to obtain the
            ``paramCollection`` object, which is the object containing the interface for
            all parameters of an ARMI object.

        See Also
        --------
        :py:meth:`armi.reactor.parameters.parameterCollections.ParameterCollection.__reduce__`
        """
        return cls.paramCollectionType()

    def getParamNames(self):
        """
        Get a list of parameters keys that are available on this object.

        Will not have any corner, edge, or timenode dependence.
        """
        return sorted(k for k in self.p.keys() if not isinstance(k, tuple))

    def nameContains(self, s):
        """
        True if s is in this object's name (eg. nameContains('fuel')==True for 'testfuel'.

        Notes
        -----
        Case insensitive (all gets converted to lower)
        """
        name = self.name.lower()
        if isinstance(s, list):
            return any(n.lower() in name for n in s)
        else:
            return s.lower() in name

    def getName(self):
        """Get composite name."""
        return self.name

    def setName(self, name):
        self.name = name

    def hasFlags(self, typeID: TypeSpec, exact=False):
        """
        Determine if this object is of a certain type.

        .. impl:: Composites have queryable flags.
            :id: I_ARMI_CMP_FLAG0
            :implements: R_ARMI_CMP_FLAG

            This method queries the flags (i.e. the ``typeID``) of the Composite for a
            given type, returning a boolean representing whether or not the candidate
            flag is present in this ArmiObject. Candidate flags cannot be passed as a
            ``string`` type and must be of a type ``Flag``. If no flags exist in the
            object then ``False`` is returned.

            If a list of flags is provided, then all input flags will be
            checked against the flags of the object. If exact is ``False``, then the
            object must have at least one of candidates exactly. If it is ``True`` then
            the object flags and candidates must match exactly.

        Parameters
        ----------
        typeID : TypeSpec
            Flags to test the object against, to see if it contains them. If a list is
            provided, each element is treated as a "candidate" set of flags. Return True
            if any of candidates match. When exact is True, the object must match one of
            the candidates exactly. If exact is False, the object must have at least the
            flags contained in a candidate for that candidate to be a match; extra flags
            on the object are permitted. None matches all objects if exact is False, or
            no objects if exact is True.

        exact : bool, optional
            Require the type of the object to fully match the provided typeID(s)

        Returns
        -------
        hasFlags : bool
            True if this object is in the typeID list.

        Notes
        -----
        Type comparisons use bitwise comparisons using valid flags.

        If you have an 'inner control' assembly, then this will evaluate True for the
        INNER | CONTROL flag combination. If you just want all FUEL, simply use FUEL
        with no additional qualifiers. For more complex comparisons, use bitwise
        operations.

        Always returns true if typeID is none and exact is False, allowing for default
        parameters to be passed in when the method does not care about the object type.
        If the typeID is none and exact is True, this will always return False.

        Examples
        --------
        If you have an object with the ``INNER``, ``DRIVER``, and ``FUEL`` flags, then

        >>> obj.getType()
        [some integer]

        >>> obj.hasFlags(Flags.FUEL)
        True

        >>> obj.hasFlags(Flags.INNER | Flags.DRIVER | Flags.FUEL)
        True

        >>> obj.hasFlags(Flags.OUTER | Flags.DRIVER | Flags.FUEL)
        False

        >>> obj.hasFlags(Flags.INNER | Flags.FUEL)
        True

        >>> obj.hasFlags(Flags.INNER | Flags.FUEL, exact=True)
        False

        >>> obj.hasFlags([Flags.INNER | Flags.DRIVER | Flags.FUEL,
        ... Flags.OUTER | Flags.DRIVER | Flags.FUEL], exact=True)
        False

        """
        if not typeID:
            return not exact
        if isinstance(typeID, str):
            raise TypeError("Must pass Flags, or an iterable of Flags; Strings are no longer supported")

        elif not isinstance(typeID, Flags):
            # list behavior gives a spec1 OR spec2 OR ... behavior.
            return any(self.hasFlags(typeIDi, exact=exact) for typeIDi in typeID)

        if not self.p.flags:
            # default still set, or null flag. Do down here so we get proper error
            # handling of invalid typeSpecs
            return False

        if exact:
            # all bits must be identical for exact match
            return self.p.flags == typeID

        # all bits that are 1s in the typeID must be present
        return self.p.flags & typeID == typeID

    def getType(self):
        """Return the object type."""
        return self.p.type

    def setType(self, typ, flags: Optional[Flags] = None):
        """
        Set the object type.

        .. impl:: Composites have modifiable flags.
            :id: I_ARMI_CMP_FLAG1
            :implements: R_ARMI_CMP_FLAG

            This method allows for the setting of flags parameter of the Composite.

        Parameters
        ----------
        typ : str
            The desired "type" for the object. Type describes the general class of the
            object, and typically corresponds to the name of the blueprint that created
            it.

        flags : Flags, optional
            The set of Flags to apply to the object. If these are omitted, then Flags
            will be derived from the ``typ``.

        Warning
        -------
        We are in the process of developing more robust definitions for things like
        "name" and "type". "type" will generally refer to the name of the blueprint that
        created a particular object. When present, a "name" will refer to a specific
        instance of an object of a particular "type". Think unique names for each
        assembly in a core, even if they are all created from the same blueprint and
        therefore have the same "type". When this work is complete, it will be strongly
        discouraged, or even disallowed to change the type of an object after it has
        been created, and ``setType()`` may be removed entirely.
        """
        self.p.flags = flags or Flags.fromStringIgnoreErrors(typ)
        self.p.type = typ

    def getVolume(self):
        return sum(child.getVolume() for child in self)

    def getArea(self, cold=False):
        return sum(child.getArea(cold) for child in self)

    def _updateVolume(self):
        """Recompute and store volume."""
        children = self.getChildren()
        # Derived shapes must come last so we temporarily change the order if we
        # have one.
        from armi.reactor.components import DerivedShape

        for child in children[:]:
            if isinstance(child, DerivedShape):
                children.remove(child)
                children.append(child)
        for child in children:
            child._updateVolume()

    def getVolumeFractions(self):
        """
        Return volume fractions of each child.

        Sets volume or area of missing piece (like coolant) if it exists.  Caching would
        be nice here.

        Returns
        -------
        fracs : list
            list of (component, volFrac) tuples

        See Also
        --------
        test_block.Block_TestCase.test_consistentAreaWithOverlappingComponents

        Notes
        -----
        void areas can be negative in gaps between fuel/clad/liner(s), but these
        negative areas are intended to account for overlapping positive areas to insure
        the total area of components inside the clad is accurate. See
        test_block.Block_TestCase.test_consistentAreaWithOverlappingComponents
        """
        children = self.getChildren()
        numerator = [c.getVolume() for c in children]
        denom = sum(numerator)
        if denom == 0.0:
            numerator = [c.getArea() for c in children]
            denom = sum(numerator)

        fracs = [(ci, nu / denom) for ci, nu in zip(children, numerator)]
        return fracs

    def getVolumeFraction(self):
        """Return the volume fraction that this object takes up in its parent."""
        if self.parent is not None:
            for child, frac in self.parent.getVolumeFractions():
                if child is self:
                    return frac

        raise ValueError(f"No parent is defined for {self}. Cannot compute its volume fraction.")

    def getMaxArea(self):
        """
        The maximum area of this object if it were totally full.

        See Also
        --------
        armi.reactor.blocks.HexBlock.getMaxArea
        """
        raise NotImplementedError()

    def getMass(self, nuclideNames=None) -> float:
        """
        Determine the mass in grams of nuclide(s) and/or elements in this object.

        .. impl:: Return mass of composite.
            :id: I_ARMI_CMP_GET_MASS
            :implements: R_ARMI_CMP_GET_MASS

            This method allows for the querying of the mass of a Composite.
            If the ``nuclideNames`` argument is included, it will filter for the mass
            of those nuclide names and provide the sum of the mass of those nuclides.

        Parameters
        ----------
        nuclideNames : str, optional
            The nuclide/element specifier to get the mass of in the object.
            If omitted, total mass is returned.

        Returns
        -------
        mass : float
            The mass in grams.
        """
        return sum(c.getMass(nuclideNames=nuclideNames) for c in self)

    def getMassFrac(self, nucName):
        """
        Get the mass fraction of a nuclide.

        Notes
        -----
        If you need multiple mass fractions, use ``getMassFracs``.

        """
        nuclideNames = self._getNuclidesFromSpecifier(nucName)
        massFracs = self.getMassFracs()
        return sum(massFracs.get(nucName, 0.0) for nucName in nuclideNames)

    def getMicroSuffix(self):
        raise NotImplementedError(
            f"Cannot get the suffix on {type(self)} objects. Only certain subclasses"
            " of composite such as Blocks or Components have the concept of micro suffixes."
        )

    def _getNuclidesFromSpecifier(self, nucSpec):
        """
        Convert a nuclide specification to a list of valid nuclide/element keys.

        nucSpec : nuclide specifier
            Can be a string name of a nuclide or element, or a list of such strings.

        This might get Zr isotopes when ZR is passed in if they exist, or it will get
        elemental ZR if that exists. When expanding elements, all known nuclides are
        returned, not just the natural ones.

        """
        allNuclidesHere = self.getNuclides()
        if nucSpec is None:
            return allNuclidesHere
        elif isinstance(nucSpec, (str)):
            nuclideNames = [nucSpec]
        elif isinstance(nucSpec, list):
            nuclideNames = nucSpec
        else:
            raise TypeError(f"nucSpec={nucSpec} is an invalid specifier. It is a {type(nucSpec)}")

        # expand elementals if appropriate.
        convertedNucNames = []
        for nucName in nuclideNames:
            if nucName in allNuclidesHere:
                convertedNucNames.append(nucName)
                continue
            try:
                # Need all nuclide bases, not just natural isotopics because, e.g. PU
                # has no natural isotopics!
                nucs = [
                    nb.name
                    for nb in elements.bySymbol[nucName].nuclides
                    if not isinstance(nb, nuclideBases.NaturalNuclideBase)
                ]
                convertedNucNames.extend(nucs)
            except KeyError:
                convertedNucNames.append(nucName)

        return sorted(set(convertedNucNames))

    def getMassFracs(self):
        """
        Get mass fractions of all nuclides in object.

        Ni [1/cm3] * Ai [g/mole]  ~ mass
        """
        numDensities = self.getNumberDensities()
        return densityTools.getMassFractions(numDensities)

    def setMassFrac(self, nucName, val):
        """
        Adjust the composition of this object so the mass fraction of nucName is val.

        See Also
        --------
        setMassFracs : efficiently set multiple mass fractions at the same time.
        """
        self.setMassFracs({nucName: val})

    def setMassFracs(self, massFracs):
        r"""
        Apply one or more adjusted mass fractions.

        This will adjust the total mass of the object, as the mass of everything
        designated will change, while anything else will not.

        .. math::

            m_i = \frac{M_i}{\sum_j(M_j)}

            (M_{j \ne i} + M_i) m_i = M_i

            \frac{m_i M_{j \ne i}}{1-m_i} = M_i

            \frac{m_i M_{j \ne i}}{V(1-m_i)} = M_i/V = m_i \rho

            N_i = \frac{m_i \rho N_A}{A_i}

            N_i = \frac{m_i M_{j \ne i} N_A}{V (1-m_i) {A_i}}

            \frac{M_{j \ne i}}{V} = m_{j \ne i} \rho

            m_{j \ne i} = 1 - m_i

        Notes
        -----
        You can't just change one mass fraction though, you have scale all others to
        fill the remaining frac.

        Parameters
        ----------
        massFracs: dict
            nucName : new mass fraction pairs.

        """
        rho = self.density()
        if not rho:
            raise ValueError(f"Cannot set mass fractions on {self} because the mass density is zero.")
        oldMassFracs = self.getMassFracs()
        totalFracSet = 0.0
        for nucName, massFrac in massFracs.items():
            self.setNumberDensity(
                nucName,
                (massFrac * rho * units.MOLES_PER_CC_TO_ATOMS_PER_BARN_CM / nucDir.getAtomicWeight(nucName)),
            )
            if nucName in oldMassFracs:
                del oldMassFracs[nucName]
            totalFracSet += massFrac
        totalOther = sum(oldMassFracs.values())
        if totalOther:
            # we normalize the remaining mass fractions so their concentrations relative
            # to each other stay constant.
            normalizedOtherMassFracs = {nucNameOther: val / totalOther for nucNameOther, val in oldMassFracs.items()}
            for nucNameOther, massFracOther in normalizedOtherMassFracs.items():
                self.setNumberDensity(
                    nucNameOther,
                    (
                        (1.0 - totalFracSet)
                        * massFracOther
                        * rho
                        * units.MOLES_PER_CC_TO_ATOMS_PER_BARN_CM
                        / nucDir.getAtomicWeight(nucNameOther)
                    ),
                )

    def adjustMassFrac(
        self,
        nuclideToAdjust=None,
        elementToAdjust=None,
        nuclideToHoldConstant=None,
        elementToHoldConstant=None,
        val=0.0,
    ):
        r"""
        Set the initial Zr mass fraction while maintaining Uranium enrichment, but general purpose.

        Parameters
        ----------
        nuclideToAdjust : str, optional
            The nuclide name to adjust
        elementToAdjust : str, optional
            The element to adjust. All isotopes in this element will adjust
        nuclideToHoldconstant : str, optional
            A nuclide to hold constant
        elementToHoldConstant : str
            Same
        val : float
            The value to set the adjust mass fraction to be.

        Notes
        -----
        If you use this for two elements one after the other, you will probably get
        something wrong. For instance, if you have U-10Zr and add Pu at 10% mass
        fraction, the Zr fraction will drop below 10% of the total. The U-Zr fractions
        will remain constant though. So this is mostly useful if you have U-10Zr and
        want to change it to U-5Zr.

        Theory:

        Mass fraction of each nuclide to be adjusted = Ai where A1+A2+A...+AI = A
        Mass fraction of nuclides to be held constant = Ci where sum = C
        Mass fraction of other nuclides is Oi, sum = O
        new value for A is v

        A+C+O = 1.0
        A'=v. If A>0, then A'=A*f1=v where f1 = v/A
        If A=0, then Ai' = v/len(A), distributing the value evenly among isotopes

        Now, to adjust the other nuclides, we know
        A'+C+O' = 1.0 , or v+C+O' = 1.0
        So, O'= 1.0-v-C
        We can scale each Oi evenly by multiplying by the factor f2
        Oi' = Oi * (1-C-v)/O = Oi * f2  where f2= (1-C-v)

        See Also
        --------
        setMassFrac
        getMassFrac
        """
        self.clearCache()  # don't keep densities around or anything.
        if val > 1.0 or val < 0:
            raise ValueError(f"Invalid mass fraction {val} for {nuclideToAdjust}/{elementToAdjust} in {self.getName()}")
        if not nuclideToAdjust and not elementToAdjust:
            raise TypeError("Must provide a nuclide or element to adjust to adjustMassFrac")

        # sum of other nuclide mass fractions before change is Y
        # need Yx+newZr = 1.0 where x is a scaling factor
        # so x=(1-newZr)/Y

        # determine nuclides to hold constant
        nuclides = set(self.getNuclides())
        if nuclideToHoldConstant or elementToHoldConstant:
            # note that if these arguments are false, you'll get ALL nuclides in the
            # material use material.getNuclides to get only non-zero ones.  use
            # nucDir.getNuclides to get all. Intersect with current nuclides to
            # eliminate double counting of element/isotopes
            constantNuclides = set(
                nucDir.getNuclideNames(nucName=nuclideToHoldConstant, elementSymbol=elementToHoldConstant)
            ).intersection(nuclides)
            constantSum = sum(self.getMassFrac(nucName) for nucName in constantNuclides)
        else:
            constantNuclides = []
            constantSum = 0.0

        # determine which nuclides we're adjusting.
        # Rather than calling this material's getNuclides method, we call the
        # nucDirectory to do this. this way, even zeroed-out nuclides will get in the
        # mix
        adjustNuclides = set(
            nucDir.getNuclideNames(nucName=nuclideToAdjust, elementSymbol=elementToAdjust)
        ).intersection(nuclides)
        # get original mass frac A of those to be adjusted.
        A = sum(self.getMassFrac(ni) for ni in adjustNuclides)

        factor1 = val / A if A else None

        # set the ones we're adjusting to their given value.
        numNucs = len(adjustNuclides)
        newA = 0.0
        newMassFracs = {}
        for nuc in adjustNuclides:
            if factor1 is None:
                # this is for when adjust nuclides have zero mass fractions. Like Zr.
                # In this case, if there are multiple nuclides, we will distribute them
                # evenly because we have no other indication of how to adjust them.
                newMassFrac = val / numNucs
            else:
                # this is for when the nuclides we're adjusting already exist
                # with non-zero mass fractions could be Pu vector.
                newMassFrac = self.getMassFrac(nuc) * factor1
            newA += newMassFrac
            newMassFracs[nuc] = newMassFrac
            if nuc == "ZR":
                # custom parameter only set here to determine how to behave for UZr
                # density, linear expansion. Can't let it roam with each mass frac
                # 'cause then the density roams too and there are "oscillations"
                self.zrFrac = newMassFrac

        # error checking.
        if abs(newA - val) > 1e-10:
            runLog.error(f"Adjust Mass fraction did not adjust {adjustNuclides} from {A} to {val}. It got to {newA}")
            raise RuntimeError("Failed to adjust mass fraction.")

        # determine the mass fraction of the nuclides that will be adjusted to
        # accommodate the requested change
        othersSum = 1.0 - A - constantSum
        if not othersSum:
            # no others to be modified.
            factor2 = 1.0
        else:
            # use newA rather than val
            factor2 = (1.0 - newA - constantSum) / othersSum

        # change all the other nuclides using f2 factor
        for nuc in self.getNuclides():
            if nuc not in adjustNuclides and nuc not in constantNuclides:
                newMassFracs[nuc] = self.getMassFrac(nuc) * factor2

        self.setMassFracs(newMassFracs)

    def adjustMassEnrichment(self, massFraction):
        """
        Adjust the enrichment of this object.

        If it's Uranium, enrichment means U-235 fraction.
        If it's Boron, enrichment means B-10 fraction, etc.

        Parameters
        ----------
        newEnrich : float
            The new enrichment as a fraction.
        """
        raise NotImplementedError

    def getNumberDensity(self, nucName):
        """
        Return the number density of a nuclide in atoms/barn-cm.

        .. impl:: Get number density for a specific nuclide
            :id: I_ARMI_CMP_NUC0
            :implements: R_ARMI_CMP_NUC

            This method queries the number density
            of a specific nuclide within the Composite. It invokes the
            ``getNuclideNumberDensities`` method for just the requested nuclide.

        Notes
        -----
        This can get called very frequently and has to do volume computations so should
        use some kind of caching that is invalidated by any temperature, composition,
        etc. changes. Even with caching the volume calls are still somewhat expensive so
        prefer the methods in see also.

        See Also
        --------
        ArmiObject.getNuclideNumberDensities: More efficient for >1 specific nuc density is needed.
        ArmiObject.getNumberDensities: More efficient for when all nucs in object is needed.
        """
        return self.getNuclideNumberDensities([nucName])[0]

    def getNuclideNumberDensities(self, nucNames):
        """Return a list of number densities in atoms/barn-cm for the nuc names requested.

        .. impl:: Get number densities for specific nuclides.
            :id: I_ARMI_CMP_NUC1
            :implements: R_ARMI_CMP_NUC

            This method provides the capability to query the volume weighted number
            densities for a list of nuclides within a given Composite. It provides the
            result in units of atoms/barn-cm. The volume weighting is accomplished by
            multiplying the number densities within each child Composite by the volume
            of the child Composite and dividing by the total volume of the Composite.
        """
        volumes = np.array([c.getVolume() / (c.parent.getSymmetryFactor() if c.parent else 1.0) for c in self])  # c x 1
        totalVol = volumes.sum()
        if totalVol == 0.0:
            # there are no children so no volume or number density
            return [0.0] * len(nucNames)

        nucDensForEachComp = np.array([c.getNuclideNumberDensities(nucNames) for c in self])  # c x n
        return volumes.dot(nucDensForEachComp) / totalVol

    def _getNdensHelper(self):
        """
        Return a number densities dict with unexpanded lfps.

        Notes
        -----
        This is implemented more simply on the component level.
        """
        nucNames = self.getNuclides()
        return dict(zip(nucNames, self.getNuclideNumberDensities(nucNames)))

    def getNumberDensities(self, expandFissionProducts=False):
        """
        Retrieve the number densities in atoms/barn-cm of all nuclides (or those requested) in the object.

        .. impl:: Number density of composite is retrievable.
            :id: I_ARMI_CMP_GET_NDENS
            :implements: R_ARMI_CMP_GET_NDENS

            This method provides a way for retrieving the number densities
            of all nuclides within the Composite. It does this by leveraging the
            ``_getNdensHelper`` method, which invokes the ``getNuclideNumberDensities``
            method. This method considers the nuclides within each child Composite of
            this composite (if they exist). If the ``expandFissionProducts`` flag is
            ``True``, then the lumped fission products are expanded to include their
            constituent elements via the ``_expandLFPs`` method.

        Parameters
        ----------
        expandFissionProducts : bool (optional)
            expand the fission product number densities

        Returns
        -------
        numberDensities : dict
            nucName keys, number density values (atoms/bn-cm)
        """
        numberDensities = self._getNdensHelper()
        if expandFissionProducts:
            return self._expandLFPs(numberDensities)
        return numberDensities

    def _expandLFPs(self, numberDensities):
        """
        Expand the LFPs on the numberDensities dictionary using this composite's
        lumpedFissionProductCollection.
        """
        lfpCollection = self.getLumpedFissionProductCollection()
        if lfpCollection:  # may not have lfps in non-fuel
            lfpDensities = lfpCollection.getNumberDensities(self)
            numberDensities = {
                nucName: numberDensities.get(nucName, 0.0) + lfpDensities.get(nucName, 0.0)
                for nucName in set(numberDensities) | set(lfpDensities)
            }
            # remove LFPs from the result
            for lfpName in lfpCollection:
                numberDensities.pop(lfpName, None)
        else:
            lfpMass = sum(
                dens
                for name, dens in numberDensities.items()
                if isinstance(nuclideBases.byName[name], nuclideBases.LumpNuclideBase)
            )
            if lfpMass:
                raise RuntimeError(
                    f"Composite {self} is attempting to expand lumped fission products, but does not have "
                    "an lfpCollection."
                )
        return numberDensities

    def getChildrenWithNuclides(self, nucNames):
        """Return children that contain any nuclides in nucNames."""
        nucNames = set(nucNames)  # only convert to set once
        return [child for child in self if nucNames.intersection(child.getNuclides())]

    def getAncestor(self, fn):
        """
        Return the first ancestor that satisfies the supplied predicate.

        Parameters
        ----------
        fn : Function-like object
            The predicate used to test the validity of an ancestor. Should return true
            if the ancestor satisfies the caller's requirements
        """
        if fn(self):
            return self
        if self.parent is None:
            return None
        else:
            return self.parent.getAncestor(fn)

    def getAncestorAndDistance(self, fn, _distance=0) -> Optional[Tuple["ArmiObject", int]]:
        """
        Return the first ancestor that satisfies the supplied predicate, along with how
        many levels above self the ancestor lies.

        Parameters
        ----------
        fn : Function-like object
            The predicate used to test the validity of an ancestor. Should return true
            if the ancestor satisfies the caller's requirements
        """
        if fn(self):
            return self, _distance
        if self.parent is None:
            return None
        else:
            return self.parent.getAncestorAndDistance(fn, _distance + 1)

    def getAncestorWithFlags(self, typeSpec: TypeSpec, exactMatch=False):
        """
        Return the first ancestor that matches the passed flags.

        Parameters
        ----------
        typeSpec : TypeSpec
            A collection of flags to match on candidate parents

        exactMatch : bool
            Whether the flags match should be exact

        Returns
        -------
        armi.composites.ArmiObject
            the first ancestor up the chain of parents that matches the passed flags

        See Also
        --------
        ArmiObject.hasFlags()
        """
        if self.hasFlags(typeSpec, exact=exactMatch):
            return self

        if self.parent is None:
            return None
        else:
            return self.parent.getAncestorWithFlags(typeSpec, exactMatch=exactMatch)

    def getTotalNDens(self):
        """
        Return the total number density of all atoms in this object.

        Returns
        -------
        nTot : float
            Total ndens of all nuclides in atoms/bn-cm. Not homogenized.
        """
        nFPsPerLFP = fissionProductModel.NUM_FISSION_PRODUCTS_PER_LFP  # LFPs count as two! Big deal in non BOL cases.
        return sum(dens * (nFPsPerLFP if "LFP" in name else 1.0) for name, dens in self.getNumberDensities().items())

    def setNumberDensity(self, nucName, val):
        """
        Set the number density of this nuclide to this value.

        This distributes atom density evenly across all children that contain nucName.
        If the nuclide doesn't exist in any of the children, then that's actually an
        error. This would only happen if some unnatural nuclide like Pu239 built up in
        fresh UZr. That should be anticipated and dealt with elsewhere.

        """
        activeChildren = self.getChildrenWithNuclides({nucName})
        if not activeChildren:
            activeVolumeFrac = 1.0
            if val:
                raise ValueError(
                    f"The nuclide {nucName} does not exist in any children of {self}; "
                    f"cannot set its number density to {val}. The nuclides here are: {self.getNuclides()}"
                )
        else:
            activeVolumeFrac = sum(vf for ci, vf in self.getVolumeFractions() if ci in activeChildren)
        dehomogenizedNdens = val / activeVolumeFrac  # scale up to dehomogenize on children.
        for child in activeChildren:
            child.setNumberDensity(nucName, dehomogenizedNdens)

    def setNumberDensities(self, numberDensities):
        """
        Set one or more multiple number densities. Reset any non-listed nuclides to 0.0.

        Parameters
        ----------
        numberDensities : dict
            nucName: ndens pairs.

        Notes
        -----
        We'd like to not have to call setNumberDensity for each nuclide because we don't
        want to call ``getVolumeFractions`` for each nuclide (it's inefficient).

        """
        numberDensities.update({nuc: 0.0 for nuc in self.getNuclides() if nuc not in numberDensities})
        self.updateNumberDensities(numberDensities)

    def updateNumberDensities(self, numberDensities):
        """
        Set one or more multiple number densities. Leaves unlisted number densities alone.

        This changes a nuclide number density only on children that already have that
        nuclide, thereby allowing, for example, actinides to stay in the fuel component
        when setting block-level values.

        The complication is that various number densities are distributed among various
        components. This sets the number density for each nuclide evenly across all
        components that contain it.

        Parameters
        ----------
        numberDensities : dict
            nucName: ndens pairs.

        """
        children, volFracs = zip(*self.getVolumeFractions())
        childNucs = tuple(set(child.getNuclides()) for child in children)

        allDehomogenizedNDens = collections.defaultdict(dict)

        # compute potentially-different homogenization factors for each child.  evenly
        # distribute entire number density over the subset of active children.
        for nuc, dens in numberDensities.items():
            # get "active" indices, i.e., indices of children containing nuc
            # NOTE: this is one of the rare instances in which (imo), using explicit
            # indexing clarifies subsequent code since it's not necessary to zip +
            # filter + extract individual components (just extract by filtered index).
            indiciesToSet = tuple(i for i, nucsInChild in enumerate(childNucs) if nuc in nucsInChild)

            if not indiciesToSet:
                if dens == 0:
                    # density is zero, skip
                    continue

                # This nuc doesn't exist in any children but is to be set.
                # Evenly distribute it everywhere.
                childrenToSet = children
                dehomogenizedNDens = dens / sum(volFracs)

            else:
                childrenToSet = tuple(children[i] for i in indiciesToSet)
                dehomogenizedNDens = dens / sum(volFracs[i] for i in indiciesToSet)

            for child in childrenToSet:
                allDehomogenizedNDens[child][nuc] = dehomogenizedNDens

        # apply the child-dependent ndens vectors to the children
        for child, ndens in allDehomogenizedNDens.items():
            child.updateNumberDensities(ndens)

    def changeNDensByFactor(self, factor):
        """Change the number density of all nuclides within the object by a multiplicative factor."""
        densitiesScaled = {nuc: val * factor for nuc, val in self.getNumberDensities().items()}
        self.setNumberDensities(densitiesScaled)
        # Update detailedNDens
        if self.p.detailedNDens is not None:
            self.p.detailedNDens *= factor
        # Update pinNDens
        if self.p.pinNDens is not None:
            self.p.pinNDens *= factor

    def clearNumberDensities(self):
        """
        Reset all the number densities to nearly zero.

        Set to almost zero, so components remember which nuclides are where.
        """
        ndens = {nuc: units.TRACE_NUMBER_DENSITY for nuc in self.getNuclides()}
        self.setNumberDensities(ndens)

    def density(self):
        """Returns the mass density of the object in g/cc."""
        density = 0.0
        for nuc in self.getNuclides():
            density += (
                self.getNumberDensity(nuc) * nucDir.getAtomicWeight(nuc) / units.MOLES_PER_CC_TO_ATOMS_PER_BARN_CM
            )

        return density

    def getNumberOfAtoms(self, nucName):
        """Return the number of atoms of nucName in this object."""
        numDens = self.getNumberDensity(nucName)  # atoms/bn-cm
        return numDens * self.getVolume() / units.CM2_PER_BARN

    def getLumpedFissionProductCollection(self):
        """
        Get collection of LFP objects. Will work for global or block-level LFP models.

        Returns
        -------
        lfps : LumpedFissionProduct
            lfpName keys , lfp object values

        See Also
        --------
        armi.physics.neutronics.fissionProductModel.lumpedFissionProduct.LumpedFissionProduct : LFP object
        """
        return self._lumpedFissionProducts

    def setLumpedFissionProducts(self, lfpCollection):
        self._lumpedFissionProducts = lfpCollection

    def setChildrenLumpedFissionProducts(self, lfpCollection):
        for c in self:
            c.setLumpedFissionProducts(lfpCollection)

    def getFissileMassEnrich(self):
        """Returns the fissile mass enrichment."""
        hm = self.getHMMass()
        if hm > 0:
            return self.getFissileMass() / hm
        else:
            return 0.0

    def getUraniumNumEnrich(self):
        """Returns U-235 number fraction."""
        u8 = self.getNumberDensity("U238")
        if u8 < 1e-10:
            return 0.0
        u5 = self.getNumberDensity("U235")
        return u5 / (u8 + u5)

    def calcTotalParam(
        self,
        param,
        objs=None,
        volumeIntegrated=False,
        addSymmetricPositions=False,
        typeSpec: TypeSpec = None,
        generationNum=1,
        calcBasedOnFullObj=False,
    ):
        """
        Sums up a parameter throughout the object's children or list of objects.

        Parameters
        ----------
        param : str
            Name of the block parameter to sum

        objs : iterable, optional
            A list of objects to sum over. If none, all children in object will be used

        volumeIntegrated : bool, optional
            Integrate over volume

        addSymmetricPositions : bool, optional
            If True, will multiply by the symmetry factor of the core (3 for 1/3 models,
            1 for full core models)

        typeSpec : TypeSpec
            object types to restrict to

        generationNum : int, optional
            Which generation to consider. 1 means direct children, 2 means children of
            children. Default: Just return direct children.

        calcBasedOnFullObj : bool, optional
            Some assemblies or blocks, such as the center assembly in a third core
            model, are not modeled as full assemblies or blocks. In the third core model
            objects at these positions are modeled as having 1/3 the volume and thus 1/3
            the power. Setting this argument to True will apply the full value of the
            parameter as if it was a full block or assembly.
        """
        tot = 0.0
        if objs is None:
            objs = self.getChildren(generationNum=generationNum)

        if addSymmetricPositions:
            if calcBasedOnFullObj:
                raise ValueError(
                    "AddSymmetricPositions is Incompatible with calcBasedOnFullObj. Will result in double counting."
                )
            try:
                coreMult = self.powerMultiplier
            except AttributeError:
                coreMult = self.parent.powerMultiplier
            if not coreMult:
                raise ValueError(f"powerMultiplier is equal to {coreMult}")
        else:
            coreMult = 1.0

        for a in objs:
            if not a.hasFlags(typeSpec):
                continue

            mult = a.getVolume() if volumeIntegrated else 1.0
            if calcBasedOnFullObj:
                mult *= a.getSymmetryFactor()

            tot += a.p[param] * mult

        return tot * coreMult

    def calcAvgParam(
        self,
        param,
        typeSpec: TypeSpec = None,
        weightingParam=None,
        volumeAveraged=True,
        absolute=True,
        generationNum=1,
    ):
        r"""
        Calculate the child-wide average of a parameter.

        Parameters
        ----------
        param : str
            The ARMI block parameter that you want the average from

        typeSpec : TypeSpec
            The child types that should be included in the calculation. Restrict average
            to a certain child type with this parameter.

        weightingParam : None or str, optional
             An optional block param that the average will be weighted against

        volumeAveraged : bool, optional
            volume (or height, or area) average this param

        absolute : bool, optional
            Returns the average of the absolute value of param

        generationNum : int, optional
            Which generation to average over (1 for children, 2 for grandchildren)


        The weighted sum is:

        .. math::

            \left<\text{x}\right> = \frac{\sum_{i} x_i w_i}{\sum_i w_i}

        where :math:`i` is each child, :math:`x_i` is the param value of the i-th child,
        and :math:`w_i` is the weighting param value of the i-th child.

        Warning
        -------
        If a param is unset/zero on any of the children, this will be included in the
        average and may significantly perturb results.

        Returns
        -------
        float
            The average parameter value.
        """
        total = 0.0
        weightSum = 0.0
        for child in self.getChildren(generationNum=generationNum):
            if child.hasFlags(typeSpec):
                if weightingParam:
                    weight = child.p[weightingParam]
                    if weight < 0:
                        # Just for conservatism, do not allow negative weights.
                        raise ValueError(f"Weighting value ({weightingParam},{weight}) cannot be negative.")
                else:
                    weight = 1.0

                if volumeAveraged:
                    weight *= child.getVolume()

                weightSum += weight
                if absolute:
                    total += abs(child.p[param]) * weight
                else:
                    total += child.p[param] * weight
        if not weightSum:
            raise ValueError(
                f"Cannot calculate {weightingParam}-weighted average of {param} in {self}. "
                f"Weights sum to zero. typeSpec is {typeSpec}"
            )
        return total / weightSum

    def getMaxParam(
        self,
        param,
        typeSpec: TypeSpec = None,
        absolute=True,
        generationNum=1,
        returnObj=False,
    ):
        """
        Find the maximum value for the parameter in this container.

        Parameters
        ----------
        param : str
            block parameter that will be sought.

        typeSpec : TypeSpec
            restricts the search to cover a variety of block types.

        absolute : bool
            looks for the largest magnitude value, regardless of sign, default: true

        returnObj : bool, optional
            If true, returns the child object as well as the value.

        Returns
        -------
        maxVal : float
            The maximum value of the parameter asked for
        obj : child object
            The object that has the max (only returned if ``returnObj==True``)
        """
        compartor = lambda x, y: x > y
        return self._minMaxHelper(
            param,
            typeSpec,
            absolute,
            generationNum,
            returnObj,
            -float("inf"),
            compartor,
        )

    def getMinParam(
        self,
        param,
        typeSpec: TypeSpec = None,
        absolute=True,
        generationNum=1,
        returnObj=False,
    ):
        """
        Find the minimum value for the parameter in this container.

        See Also
        --------
        getMaxParam : details
        """
        compartor = lambda x, y: x < y
        return self._minMaxHelper(param, typeSpec, absolute, generationNum, returnObj, float("inf"), compartor)

    def _minMaxHelper(
        self,
        param,
        typeSpec: TypeSpec,
        absolute,
        generationNum,
        returnObj,
        startingNum,
        compartor,
    ):
        """Helper for getMinParam and getMaxParam."""
        maxP = (startingNum, None)
        realVal = 0.0
        objs = self.getChildren(generationNum=generationNum)
        for b in objs:
            if b.hasFlags(typeSpec):
                try:
                    val = b.p[param]
                except parameters.UnknownParameterError:
                    # No worries; not all Composite types are guaranteed to have the
                    # relevant parameter. It might be a good idea to more strongly
                    # type-check this, perhaps by passing the paramDef,
                    # rather than its name?
                    continue
                if val is None:
                    # Neither bigger or smaller than anything (also illegal in Python3)
                    continue
                if absolute:
                    absVal = abs(val)
                else:
                    absVal = val
                if compartor(absVal, maxP[0]):
                    maxP = (absVal, b)
                    realVal = val
        if returnObj:
            return realVal, maxP[1]
        else:
            return realVal

    def getChildParamValues(self, param):
        """Get the child parameter values in a numpy array."""
        return np.array([child.p[param] for child in self])

    def isFuel(self):
        """True if this is a fuel block."""
        return self.hasFlags(Flags.FUEL)

    def containsHeavyMetal(self):
        """True if this has HM."""
        return any(nucDir.isHeavyMetal(nucName) for nucName in self.getNuclides())

    def getNuclides(self):
        """
        Determine which nuclides are present in this armi object.

        Returns
        -------
        list
            List of nuclide names that exist in this
        """
        nucs = set()
        for child in self:
            nucs.update(child.getNuclides())
        return nucs

    def getFissileMass(self):
        """Returns fissile mass in grams."""
        return self.getMass(nuclideBases.NuclideBase.fissile)

    def getHMMass(self):
        """Returns heavy metal mass in grams."""
        nucs = []
        for nucName in self.getNuclides():
            if nucDir.isHeavyMetal(nucName):
                nucs.append(nucName)
        mass = self.getMass(nucs)
        return mass

    def getHMMoles(self):
        """
        Get the number of moles of heavy metal in this object.

        Notes
        -----
        If an object is on a symmetry line, the volume reported by getVolume
        is reduced to reflect that the block is not wholly within the reactor. This
        reduction in volume reduces the reported HM moles.
        """
        return self.getHMDens() / units.MOLES_PER_CC_TO_ATOMS_PER_BARN_CM * self.getVolume()

    def getHMDens(self):
        """
        Compute the total heavy metal density of this object.

        Returns
        -------
        hmDens : float
            The total heavy metal number (atom) density in atoms/bn-cm.
        """
        hmNuclides = [nuclide for nuclide in self.getNuclides() if nucDir.isHeavyMetal(nuclide)]
        hmDens = np.sum(self.getNuclideNumberDensities(hmNuclides))
        return hmDens

    def getFPMass(self):
        """Returns mass of fission products in this block in grams."""
        nucs = []
        for nucName in self.getNuclides():
            if "LFP" in nucName:
                nucs.append(nucName)
        mass = self.getMass(nucs)
        return mass

    def getFuelMass(self):
        """Returns mass of fuel in grams."""
        return sum((c.getFuelMass() for c in self))

    def constituentReport(self):
        """A print out of some pertinent constituent information."""
        from armi.utils import iterables

        rows = [["Constituent", "HMFrac", "FuelFrac"]]
        columns = [-1, self.getHMMass(), self.getFuelMass()]

        for base_ele in ["U", "PU"]:
            total = sum([self.getMass(nuclide.name) for nuclide in elements.bySymbol[base_ele]])
            rows.append([base_ele, total, total])

        fp_total = self.getFPMass()
        rows.append(["FP", fp_total, fp_total])

        ma_nuclides = iterables.flatten(
            [ele.nuclides for ele in [elements.byZ[key] for key in elements.byZ.keys() if key > 94]]
        )
        ma_total = sum([self.getMass(nuclide.name) for nuclide in ma_nuclides])
        rows.append(["MA", ma_total, ma_total])

        for i, row in enumerate(rows):
            for j, entry in enumerate(row):
                try:
                    percent = entry / columns[j] * 100.0
                    rows[i][j] = percent or "-"
                except ZeroDivisionError:
                    rows[i][j] = "NaN"
                except TypeError:
                    pass  # trying to divide the string name

        return "\n".join(["{:<14}{:<10}{:<10}".format(*row) for row in rows])

    def getAtomicWeight(self):
        r"""
        Calculate the atomic weight of this object in g/mole of atoms.

        .. warning:: This is not the molecular weight, which is grams per mole of
            molecules (grams/gram-molecule). That requires knowledge of the chemical
            formula. Don't be surprised when you run this on UO2 and find it to be 90;
            there are a lot of Oxygen atoms in UO2.

        .. math::

            A =  \frac{\sum_i N_i A_i }{\sum_i N_i}
        """
        numerator = 0.0
        denominator = 0.0

        numDensities = self.getNumberDensities()

        for nucName, nDen in numDensities.items():
            atomicWeight = nuclideBases.byName[nucName].weight
            numerator += atomicWeight * nDen
            denominator += nDen
        return numerator / denominator

    def getMasses(self):
        """
        Return a dictionary of masses indexed by their nuclide names.

        Notes
        -----
        Implemented to get number densities and then convert to mass
        because getMass is too slow on a large tree.
        """
        numDensities = self.getNumberDensities()
        vol = self.getVolume()
        return {nucName: densityTools.getMassInGrams(nucName, vol, ndens) for nucName, ndens in numDensities.items()}

    def getIntegratedMgFlux(self, adjoint=False, gamma=False):
        raise NotImplementedError

    def getMgFlux(self, adjoint=False, average=False, volume=None, gamma=False):
        """
        Return the multigroup neutron flux in [n/cm^2/s].

        The first entry is the first energy group (fastest neutrons). Each additional
        group is the next energy group, as set in the ISOTXS library.

        On blocks, it is stored integrated over volume on <block>.p.mgFlux

        Parameters
        ----------
        adjoint : bool, optional
            Return adjoint flux instead of real

        average : bool, optional
            If true, will return average flux between latest and previous. Doesn't work
            for pin detailed yet

        volume: float, optional
            The volume-integrated flux is divided by volume before being
            returned. The user may specify a volume here, or the function will
            obtain the block volume directly.

        gamma : bool, optional
            Whether to return the neutron flux or the gamma flux.

        Returns
        -------
        flux : np.ndarray
            multigroup neutron flux in [n/cm^2/s]
        """
        if average:
            raise NotImplementedError(
                f"{self.__class__} class has no method for producing average MG flux -- tryusing blocks"
            )

        volume = volume or self.getVolume()
        return self.getIntegratedMgFlux(adjoint=adjoint, gamma=gamma) / volume

    def removeMass(self, nucName, mass):
        self.addMass(nucName, -mass)

    def addMass(self, nucName, mass):
        """Add mass to a particular nuclide.

        Parameters
        ----------
        nucName : str
            nuclide name e.g. 'U235'

        mass : float
            mass in grams of nuclide to be added to this armi Object
        """
        volume = self.getVolume()
        addedNumberDensity = densityTools.calculateNumberDensity(nucName, mass, volume)
        self.setNumberDensity(nucName, self.getNumberDensity(nucName) + addedNumberDensity)

    def addMasses(self, masses):
        """
        Adds a vector of masses.

        Parameters
        ----------
        masses : dict
            a dictionary of masses (g) indexed by nucNames (string)
        """
        for nucName, mass in masses.items():
            if mass:
                self.addMass(nucName, mass)

    def setMass(self, nucName, mass):
        """
        Set the mass in an object by adjusting the ndens of the nuclides.

        Parameters
        ----------
        nucName : str
            Nuclide name to set mass of
        mass : float
            Mass in grams to set.

        """
        d = calculateNumberDensity(nucName, mass, self.getVolume())
        self.setNumberDensity(nucName, d)

    def setMasses(self, masses):
        """
        Set a vector of masses.

        Parameters
        ----------
        masses : dict
            a dictionary of masses (g) indexed by nucNames (string)
        """
        self.clearNumberDensities()
        for nucName, mass in masses.items():
            self.setMass(nucName, mass)

    def getSymmetryFactor(self):
        """
        Return a scaling factor due to symmetry on the area of the object or its children.

        See Also
        --------
        armi.reactor.blocks.HexBlock.getSymmetryFactor : concrete implementation
        """
        return 1.0

    def getBoundingIndices(self):
        """
        Find the 3-D index bounds (min, max) of all children in the spatial grid of this object.

        Returns
        -------
        bounds : tuple
            ((minI, maxI), (minJ, maxJ), (minK, maxK))
        """
        minI = minJ = minK = float("inf")
        maxI = maxJ = maxK = -float("inf")
        for obj in self:
            i, j, k = obj.spatialLocator.getCompleteIndices()
            if i >= maxI:
                maxI = i
            if i <= minI:
                minI = i

            if j >= maxJ:
                maxJ = j
            if j <= minJ:
                minJ = j

            if k >= maxK:
                maxK = k
            if k <= minK:
                minK = k

        return ((minI, maxI), (minJ, maxJ), (minK, maxK))

    def getComponentNames(self):
        r"""
        Get all unique component names of this Composite.

        Returns
        -------
        set or str
            A set of all unique component names found in this Composite.
        """
        return set(c.getName() for c in self.iterComponents())

    def getComponentsOfShape(self, shapeClass):
        """
        Return list of components in this block of a particular shape.

        Parameters
        ----------
        shapeClass : Component
            The class of component, e.g. Circle, Helix, Hexagon, etc.

        Returns
        -------
        param : list
            List of components in this block that are of the given shape.
        """
        return [c for c in self.iterComponents() if isinstance(c, shapeClass)]

    def getComponentsOfMaterial(self, material=None, materialName=None):
        """
        Return list of components in this block that are made of a particular material.

        Only one of the selectors may be used

        Parameters
        ----------
        material : armi.materials.material.Material, optional
            The material to match
        materialName : str, optional
            The material name to match.

        Returns
        -------
        componentsWithThisMat : list

        """
        if materialName is None:
            materialName = material.getName()
        else:
            assert material is None, "Cannot call with more than one selector. Choose one or the other."

        componentsWithThisMat = []
        for c in self.iterComponents():
            if c.getProperties().getName() == materialName:
                componentsWithThisMat.append(c)
        return componentsWithThisMat

    def hasComponents(self, typeSpec: Union[TypeSpec, List[TypeSpec]], exact=False):
        """
        Return true if components matching all TypeSpec exist in this object.

        Parameters
        ----------
        typeSpec : Flags or iterable of Flags
            Component flags to check for
        """
        # Wrap the typeSpec in a tuple if we got a scalar
        try:
            typeSpec = iter(typeSpec)
        except TypeError:
            typeSpec = (typeSpec,)

        return all(self.getComponents(t, exact) for t in typeSpec)

    def getComponentByName(self, name):
        """
        Gets a particular component from this object, based on its name.

        Parameters
        ----------
        name : str
            The blueprint name of the component to return
        """
        components = [c for c in self.iterComponents() if c.name == name]
        nComp = len(components)
        if nComp == 0:
            return None
        elif nComp > 1:
            raise ValueError(f"More than one component named '{name}' in {self}")
        else:
            return components[0]

    def getComponent(self, typeSpec: TypeSpec, exact=False, quiet=False):
        """
        Get a particular component from this object.

        Parameters
        ----------
        typeSpec : flags.Flags or list of Flags
            The type specification of the component to return

        exact : boolean, optional
            Demand that the component flags be exactly equal to the typespec. Default: False

        quiet : boolean, optional
            Warn if the component is not found. Default: False

        Careful with multiple similar names in one object

        Returns
        -------
        Component : The component that matches the criteria or None
        """
        results = self.getComponents(typeSpec, exact=exact)
        if len(results) == 1:
            return results[0]
        elif not results:
            if not quiet:
                runLog.warning(
                    "No component matched {0} in {1}. Returning None".format(typeSpec, self),
                    single=True,
                    label="None component returned instead of {0}".format(typeSpec),
                )
            return None
        else:
            raise ValueError("Multiple components match in {} match typeSpec {}: {}".format(self, typeSpec, results))

    def getNumComponents(self, typeSpec: TypeSpec, exact=False):
        """
        Get the number of components that have these flags, taking into account multiplicity. Useful
        for getting nPins even when there are pin detailed cases.

        Parameters
        ----------
        typeSpec : Flags
            Expected flags of the component to get. e.g. Flags.FUEL

        Returns
        -------
        total : int
            the number of components of this type in this object, including multiplicity.
        """
        total = 0
        for c in self.iterComponents(typeSpec, exact):
            total += int(c.getDimension("mult"))
        return total

    def setComponentDimensionsReport(self):
        """Makes a summary of the dimensions of the components in this object."""
        reportGroups = []
        for c in self.iterComponents():
            reportGroups.append(c.setDimensionReport())

        return reportGroups

    def expandAllElementalsToIsotopics(self):
        reactorNucs = self.getNuclides()
        for elemental in nuclideBases.where(
            lambda nb: isinstance(nb, nuclideBases.NaturalNuclideBase) and nb.name in reactorNucs
        ):
            self.expandElementalToIsotopics(elemental)

    def expandElementalToIsotopics(self, elementalNuclide):
        """
        Expands the density of a specific elemental nuclides to its natural isotopics.

        Parameters
        ----------
        elementalNuclide : :class:`armi.nucDirectory.nuclideBases.NaturalNuclide` natural nuclide to
            replace.
        """
        natName = elementalNuclide.name
        for component in self.iterComponents():
            elementalDensity = component.getNumberDensity(natName)
            if elementalDensity == 0.0:
                continue

            keepIndex = np.where(component.p.nuclides != natName.encode())[0]
            newNuclides = [nuc.decode() for nuc in component.p.nuclides[keepIndex]]
            newNDens = component.p.numberDensities[keepIndex]
            component.updateNumberDensities(dict(zip(newNuclides, newNDens)), wipe=True)

            # add in isotopics
            for natNuc in elementalNuclide.getNaturalIsotopics():
                component.setNumberDensity(natNuc.name, elementalDensity * natNuc.abundance)

    def getAverageTempInC(self, typeSpec: TypeSpec = None, exact=False):
        """Return the average temperature of the ArmiObject in C by averaging all components."""
        tempNumerator = 0.0
        totalVol = 0.0
        for component in self.iterComponents(typeSpec, exact):
            vol = component.getVolume()
            tempNumerator += component.temperatureInC * vol
            totalVol += vol

        return tempNumerator / totalVol

    def resolveLinkedDims(self, components):
        """Resolve link strings to links on all child components."""
        for component in self.iterComponents():
            component.resolveLinkedDims(components)

    def getDominantMaterial(self, typeSpec: TypeSpec = None, exact=False):
        """
        Return the first sample of the most dominant material (by volume) in this object.

        Parameters
        ----------
        typeSpec : Flags or iterable of Flags, optional
            The types of components to consider (e.g. ``[Flags.FUEL, Flags.CONTROL]``)
        exact : bool, optional
            Whether or not the TypeSpec is exact

        Returns
        -------
        mat : armi.materials.material.Material
             the first instance of the most dominant material (by volume) in this object.

        See Also
        --------
        getComponentsOfMaterial
            Gets components that are made of a particular material
        gatherMaterialsByVolume
            Classifies all materials by volume
        """
        return getDominantMaterial([self], typeSpec, exact)


class Composite(ArmiObject):
    """
    An ArmiObject that has children.

    This is a fundamental ARMI state object that generally represents some piece of the
    nuclear reactor that is made up of other smaller pieces. This object can cache
    information about its children to help performance.

    **Details about spatial representation**

    Spatial representation of a ``Composite`` is handled through a combination of the
    ``spatialLocator`` and ``spatialGrid`` parameters. The ``spatialLocator`` is a numpy
    triple representing either:

    1. Indices in the parent's ``spatialGrid`` (for lattices, etc.), used when the dtype is int.

    2. Coordinates in the parent's universe in cm, used when the dtype is float.

    The top parent of any composite must have a coordinate-based ``spatialLocator``. For
    example, a Reactor an a Pump should both have coordinates based on how far apart
    they are.

    The traversal of indices and grids is recursive. The Reactor/Core/Assembly/Block
    model is handled by putting a 2-D grid (either Theta-R, Hex, or Cartesian) on the
    Core and individual 1-D Z-meshes on the assemblies. Then, Assemblies have 2-D
    spatialLocators (i,j,0) and Blocks have 1-D spatiaLocators (0,0,k). These get added
    to form the global indices. This way, if an assembly is moved, all the blocks
    immediately and naturally move with it. Individual children may have
    coordinate-based spatialLocators mixed with siblings in a grid. This allows mixing
    grid-representation with explicit representation, often useful in advanced
    assemblies and thermal reactors.

    The traversal of indices and grids is recursive. The
    Reactor/Core/Assembly/Block model is handled by putting a 2-D grid (either
    Theta-R, Hex, or Cartesian) on the Core and individual 1-D Z-meshes on the
    assemblies. Then, Assemblies have 2-D spatialLocators (i,j,0) and Blocks
    have 1-D spatiaLocators (0,0,k). These get added to form the global indices.
    This way, if an assembly is moved, all the blocks immediately and naturally
    move with it. Individual children may have coordinate-based spatialLocators
    mixed with siblings in a grid. This allows mixing grid-representation with
    explicit representation, often useful in advanced assemblies and thermal
    reactors.

    .. impl:: Composites are a physical part of the reactor in a hierarchical data model.
        :id: I_ARMI_CMP0
        :implements: R_ARMI_CMP

        An ARMI reactor model is composed of collections of ARMIObject objects. This
        class is a child-class of the ARMIObject class and provides a structure
        allowing a reactor model to be composed of Composites.

        This class provides various methods to query and modify the hierarchical ARMI
        reactor model, including but not limited to, iterating, sorting, and adding or
        removing child Composites.

    """

    _children: list["Composite"]

    def __init__(self, name):
        ArmiObject.__init__(self, name)
        self.childrenByLocator = {}
        self._children = []

    def __getitem__(self, index):
        return self._children[index]

    def __setitem__(self, index, obj):
        raise NotImplementedError("Unsafe to insert elements directly")

    def __iter__(self):
        return iter(self._children)

    def __len__(self):
        return len(self._children)

    def __contains__(self, item):
        """
        Membership check.

        This does not use quality checks for membership checking because equality
        operations can be fairly heavy. Rather, this only checks direct identity
        matches.
        """
        return id(item) in set(id(c) for c in self._children)

    def sort(self):
        """Sort the children of this object."""
        # sort the top-level children of this Composite
        self._children.sort()

        # recursively sort the children below it.
        for c in self._children:
            if issubclass(c.__class__, Composite):
                c.sort()

    def index(self, obj):
        """Obtain the list index of a particular child."""
        return self._children.index(obj)

    def append(self, obj):
        """Append a child to this object."""
        self._children.append(obj)

    def extend(self, seq):
        """Add a list of children to this object."""
        self._children.extend(seq)

    def add(self, obj):
        """Add one new child."""
        if obj in self:
            raise RuntimeError(f"Cannot add {obj} because it has already been added to {self}.")
        obj.parent = self
        self._children.append(obj)

    def remove(self, obj):
        """Remove a particular child."""
        obj.parent = None
        obj.spatialLocator = obj.spatialLocator.detachedCopy()
        self._children.remove(obj)

    def moveTo(self, locator):
        """Move to specific location in parent. Often in a grid."""
        if locator.grid.armiObject is not self.parent:
            raise ValueError(
                f"Cannot move {self} to a location in  {locator.grid.armiObject}"
                ", which is not its parent ({self.parent})."
            )
        self.spatialLocator = locator

    def insert(self, index, obj):
        """Insert an object into the list of children at a particular index."""
        if obj in self._children:
            raise RuntimeError(f"Cannot insert {obj} because it has already been added to {self}.")
        obj.parent = self
        self._children.insert(index, obj)

    def removeAll(self):
        """Remove all children."""
        for c in self.getChildren()[:]:
            self.remove(c)

    def setChildren(self, items):
        """Clear this container and fills it with new children."""
        self.removeAll()
        for c in items:
            self.add(c)

    def iterChildren(
        self,
        deep=False,
        generationNum=1,
        predicate: Optional[Callable[["Composite"], bool]] = None,
    ) -> Iterator["Composite"]:
        """Iterate over children objects of this composite.

        Parameters
        ----------
        deep : bool, optional
            If true, traverse the entire composite tree. Otherwise, go as far as ``generationNum``.
        generationNum: int, optional
            Produce composites at this depth. A depth of ``1`` includes children of ``self``, ``2``
            is children of children, and so on.
        predicate: f(Composite) -> bool, optional
            Function to check on a composite before producing it. All items in the iteration
            will pass this check.

        Returns
        -------
        iterator of Composite

        See Also
        --------
        :meth:`getChildren` produces a list for situations where you need to perform
        multiple iterations or do list operations (append, indexing, sorting, containment, etc.)

        Composites are naturally iterable. The following are identical::

            >>> for child in c.getChildren():
            ...     pass
            >>> for child in c.iterChildren():
            ...     pass
            >>> for child in c:
            ...     pass

        If you do not need any depth-traversal, natural iteration should be sufficient.

        The :func:`filter` command may be sufficient if you do not wish to pass a predicate. The following
        are identical::
            >>> checker = lambda c: len(c.name) % 3
            >>> for child in c.getChildren(predicate=checker):
            ...     pass
            >>> for child in c.iterChildren(predicate=checker):
            ...     pass
            >>> for child in filter(checker, c):
            ...     pass

        If you're going to be doing traversal beyond the first generation, this method will help you.

        """
        if deep and generationNum > 1:
            raise RuntimeError("Cannot get children with a generation number set and the deep flag set")
        if predicate is None:
            checker = lambda _: True
        else:
            checker = predicate
        yield from self._iterChildren(deep, generationNum, checker)

    def _iterChildren(
        self, deep: bool, generationNum: int, checker: Callable[["Composite"], bool]
    ) -> Iterator["Composite"]:
        if deep or generationNum == 1:
            yield from filter(checker, self)
        if deep or generationNum > 1:
            for c in self:
                yield from c._iterChildren(deep, generationNum - 1, checker)

    def iterChildrenWithMaterials(self, *args, **kwargs) -> Iterator:
        """Produce an iterator that also includes any materials found on descendants.

        Arguments are forwarded to :meth:`iterChildren` and control the depth of traversal
        and filtering of objects.

        This is useful for sending state across MPI tasks where you need a more full
        representation of the composite tree. Which includes the materials attached
        to components.
        """
        children = self.iterChildren(*args, **kwargs)
        # Each entry is either (c, ) or (c, c.material) if the child has a material attribute
        stitched = map(
            lambda c: ((c,) if getattr(c, "material", None) is None else (c, c.material)),
            children,
        )
        # Iterator that iterates over each "sub" iterator. If we have ((c0, ), (c1, m1)), this produces a single
        # iterator of (c0, c1, m1)
        return itertools.chain.from_iterable(stitched)

    def getChildren(
        self,
        deep=False,
        generationNum=1,
        includeMaterials=False,
        predicate: Optional[Callable[["Composite"], bool]] = None,
    ) -> list["Composite"]:
        """
        Return the children objects of this composite.

        .. impl:: Composites have children in the hierarchical data model.
            :id: I_ARMI_CMP1
            :implements: R_ARMI_CMP

            This method retrieves all children within a given Composite object. Children of any
            generation can be retrieved. This is achieved by visiting all children and calling this
            method recursively for each generation requested.

            If the method is called with ``includeMaterials``, it will additionally include
            information about the material for each child. If a function is supplied as the
            ``predicate`` argument, then this method will be used to evaluate all children as a
            filter to include or not. For example, if the caller of this method only desires
            children with a certain flag, or children which only contain a certain material, then
            the ``predicate`` function can be used to perform this filtering.

        Parameters
        ----------
        deep : boolean, optional
            Return all children of all levels.

        generationNum : int, optional
            Which generation to return. 1 means direct children, 2 means children of children.
            Setting this parameter will only return children of this generation, not their parents.
            Default: Just return direct children.

        includeMaterials : bool, optional
            Include the material properties

        predicate : callable, optional
            An optional unary predicate to use for filtering results. This can be used to request
            children of specific types, or with desired attributes. Not all ArmiObjects have the
            same methods and members, so care should be taken to make sure that the predicate
            executes gracefully in all cases (e.g., use ``getattr(obj, "attribute", None)`` to
            access instance attributes). Failure to meet the predicate only affects the object in
            question; children will still be considered.

        See Also
        --------
        :meth:`iterChildren` if you do not need to produce a full list, e.g., just iterating
        over objects.

        Examples
        --------
        >>> obj.getChildren()
        [child1, child2, child3]

        >>> obj.getChildren(generationNum=2)
        [grandchild1, grandchild2, grandchild3]

        >>> obj.getChildren(deep=True)
        [child1, child2, child3, grandchild1, grandchild2, grandchild3]

        # Assuming that grandchild1 and grandchild3 are Component objects
        >>> obj.getChildren(deep=True, predicate=lambda o: isinstance(o, Component))
        [grandchild1, grandchild3]

        """
        if not includeMaterials:
            items = self.iterChildren(deep=deep, generationNum=generationNum, predicate=predicate)
        else:
            items = self.iterChildrenWithMaterials(deep=deep, generationNum=generationNum, predicate=predicate)
        return list(items)

    def getComponents(self, typeSpec: TypeSpec = None, exact=False):
        return list(self.iterComponents(typeSpec, exact))

    def iterComponents(self, typeSpec: TypeSpec = None, exact=False):
        """
        Return an iterator of armi.reactor.component.Component objects within this Composite.

        Parameters
        ----------
        typeSpec : TypeSpec
            Component flags. Will restrict Components to specific ones matching the flags specified.

        exact : bool, optional
            Only match exact component labels (names). If True, 'coolant' will not match
            'interCoolant'. This has no impact if typeSpec is None.

        Returns
        -------
        iterator of Component
            items matching typeSpec and exact criteria
        """
        return (c for child in self for c in child.iterComponents(typeSpec, exact))

    def syncMpiState(self):
        """
        Synchronize all parameters of this object and all children to all worker nodes over the
        network using MPI.

        In parallelized runs, if each process has its own copy of the entire reactor hierarchy, this
        method synchronizes the state of all parameters on all objects.

        .. impl:: Composites can be synchronized across MPI threads.
            :id: I_ARMI_CMP_MPI
            :implements: R_ARMI_CMP_MPI

            Parameters need to be handled properly during parallel code execution. This method
            synchronizes all parameters of the composite object across all processes by cycling
            through all the children of the Composite and ensuring that their parameters are
            properly synchronized. If it fails to synchronize, an error message is displayed which
            alerts the user to which Composite has inconsistent data across the processes.

        Returns
        -------
        int
            number of parameters synchronized over all components
        """
        if context.MPI_SIZE == 1:
            return 0

        startTime = timeit.default_timer()
        # sync parameters...
        genItems = itertools.chain(
            [self],
            self.iterChildrenWithMaterials(deep=True),
        )
        allComps = [c for c in genItems if hasattr(c, "p")]
        sendBuf = [c.p.getSyncData() for c in allComps]
        runLog.debug(f"syncMpiState has {len(allComps)} comps")

        try:
            context.MPI_COMM.barrier()  # sync up
            allGatherTime = -timeit.default_timer()
            allSyncData = context.MPI_COMM.allgather(sendBuf)
            allGatherTime += timeit.default_timer()
        except:
            msg = ["Failure while trying to allgather."]
            for ci, compData in enumerate(sendBuf):
                if compData is not None:
                    msg += [f"sendBuf[{ci}]: {compData}"]
            runLog.error("\n".join(msg))
            raise

        # key is (comp, paramName) value is conflicting nodes
        errors = collections.defaultdict(list)
        syncCount = 0
        compsPerNode = {len(nodeSyncData) for nodeSyncData in allSyncData}

        if len(compsPerNode) != 1:
            raise ValueError(f"The workers have different reactor sizes! comp lengths: {compsPerNode}")

        for ci, comp in enumerate(allComps):
            if not hasattr(comp, "_syncParameters"):
                # materials don't have Parameters to sync
                continue
            data = (nodeSyncData[ci] for nodeSyncData in allSyncData)
            syncCount += comp._syncParameters(data, errors)

        if errors:
            errorData = sorted(
                (str(comp), comp.__class__.__name__, str(comp.parent), paramName, nodes)
                for (comp, paramName), nodes in errors.items()
            )
            message = "Synchronization failed due to overlapping data. Only the first duplicates are listed\n{}".format(
                tabulate.tabulate(
                    errorData,
                    headers=[
                        "Composite",
                        "Composite Type",
                        "Composite Parent",
                        "ParameterName",
                        "NodeRanks",
                    ],
                )
            )
            raise ValueError(message)

        self._markSynchronized()
        runLog.extra(
            f"Synchronized reactor over MPI in {timeit.default_timer() - startTime:.4f} seconds"
            f", {allGatherTime:.4f} seconds in MPI allgather. count:{syncCount}"
        )

        return syncCount

    def _syncParameters(self, allSyncData, errors):
        """Ensure no overlap with syncedKeys, use errors to report overlapping data."""
        syncedKeys = set()
        for nodeRank, nodeSyncData in enumerate(allSyncData):
            if nodeSyncData is None:
                continue

            for key, val in nodeSyncData.items():
                if key in syncedKeys:
                    # Edge Case: a Composite object is flagged as out of sync, and this parameter
                    # was also globally modified and readjusted to the original value.
                    curVal = self.p[key]
                    if isinstance(val, np.ndarray) or isinstance(curVal, np.ndarray):
                        if (val != curVal).any():
                            errors[self, key].append(nodeRank)
                    elif curVal != val:
                        errors[self, key].append(nodeRank)
                        runLog.error(f"in {self}, {key} differ ({curVal} != {val})")
                    continue
                syncedKeys.add(key)
                self.p[key] = val

        self.clearCache()
        return len(syncedKeys)

    def _markSynchronized(self):
        """
        Mark the composite and child parameters as synchronized across MPI.

        We clear SINCE_LAST_DISTRIBUTE_STATE so that anything after this point will set the
        SINCE_LAST_DISTRIBUTE_STATE flag, indicating it has been modified
        SINCE_LAST_DISTRIBUTE_STATE.
        """
        paramDefs = set()
        items = itertools.chain(
            [self],
            self.iterChildrenWithMaterials(deep=True),
        )
        for child in items:
            # Materials don't have a "p" / Parameter attribute to sync
            if hasattr(child, "p"):
                # below reads as: assigned & everything_but(SINCE_LAST_DISTRIBUTE_STATE)
                child.p.assigned &= ~parameters.SINCE_LAST_DISTRIBUTE_STATE
                paramDefs.add(child.p.paramDefs)

        for paramDef in paramDefs:
            paramDef.resetAssignmentFlag(parameters.SINCE_LAST_DISTRIBUTE_STATE)

    def retainState(self, paramsToApply=None):
        """
        Restores a state before and after some operation.

        Parameters
        ----------
        paramsToApply : iterable
            Parameters that should be applied to the state after existing the state retainer. All
            others will be reverted to their values upon entering.

        Notes
        -----
        This should be used in a `with` statement.
        """
        return StateRetainer(self, paramsToApply)

    def backUp(self):
        """
        Create and store a backup of the state.

        This needed to be overridden due to linked components which actually have a parameter value
        of another ARMI component.
        """
        self._backupCache = (self.cached, self._backupCache)
        self.cached = {}  # don't .clear(), using reference above!
        self.p.backUp()
        if self.spatialGrid:
            self.spatialGrid.backUp()

    def restoreBackup(self, paramsToApply):
        """
        Restore the parameters from previously created backup.

        Parameters
        ----------
        paramsToApply : list of ParmeterDefinitions
            restores the state of all parameters not in `paramsToApply`
        """
        self.p.restoreBackup(paramsToApply)
        self.cached, self._backupCache = self._backupCache
        if self.spatialGrid:
            self.spatialGrid.restoreBackup()

    def getLumpedFissionProductsIfNecessary(self, nuclides=None):
        """Return Lumped Fission Product objects that belong to this object or any of its children."""
        if self.requiresLumpedFissionProducts(nuclides=nuclides):
            lfps = self.getLumpedFissionProductCollection()
            if lfps is None:
                for c in self:
                    return c.getLumpedFissionProductsIfNecessary(nuclides=nuclides)
            else:
                return lfps
        # There are no lumped fission products in the batch so if you use a
        # dictionary no one will know the difference
        return {}

    def getLumpedFissionProductCollection(self):
        """
        Get collection of LFP objects. Will work for global or block-level LFP models.

        Returns
        -------
        lfps : object
            lfpName keys, lfp object values

        See Also
        --------
        armi.physics.neutronics.fissionProductModel.lumpedFissionProduct.LumpedFissionProduct
        """
        lfps = ArmiObject.getLumpedFissionProductCollection(self)
        if lfps is None:
            for c in self:
                lfps = c.getLumpedFissionProductCollection()
                if lfps is not None:
                    break

        return lfps

    def requiresLumpedFissionProducts(self, nuclides=None):
        """True if any of the nuclides in this object are Lumped nuclides."""
        if nuclides is None:
            nuclides = self.getNuclides()

        # ruff: noqa: SIM110
        for nucName in nuclides:
            if isinstance(nuclideBases.byName[nucName], nuclideBases.LumpNuclideBase):
                return True

        return False

    def getIntegratedMgFlux(self, adjoint=False, gamma=False):
        """
        Returns the multigroup neutron tracklength in [n-cm/s].

        The first entry is the first energy group (fastest neutrons). Each additional group is the
        next energy group, as set in the ISOTXS library.

        Parameters
        ----------
        adjoint : bool, optional
            Return adjoint flux instead of real
        gamma : bool, optional
            Whether to return the neutron flux or the gamma flux.

        Returns
        -------
        integratedFlux : np.ndarray
            multigroup neutron tracklength in [n-cm/s]
        """
        integratedMgFlux = np.zeros(1)
        for c in self:
            mgFlux = c.getIntegratedMgFlux(adjoint=adjoint, gamma=gamma)
            if mgFlux is not None:
                integratedMgFlux = integratedMgFlux + mgFlux

        return integratedMgFlux

    def _getReactionRates(self, nucName, nDensity=None):
        """
        Helper to get the reaction rates of a certain nuclide on one ArmiObject.

        Parameters
        ----------
        nucName : str
            nuclide name -- e.g. 'U235'
        nDensity : float
            number density

        Returns
        -------
        rxnRates : dict
            dictionary of reaction rates (rxn/s) for nG, nF, n2n, nA and nP

        Notes
        -----
        If you set nDensity to 1/CM2_PER_BARN this makes 1 group cross section generation easier.

        This method is not designed to work on ``Assembly``, ``Core``, or anything higher on the
        hierarchy than ``Block``.
        """
        from armi.reactor.blocks import Block
        from armi.reactor.reactors import Core

        if nDensity is None:
            nDensity = self.getNumberDensity(nucName)

        try:
            return getReactionRateDict(
                nucName,
                self.getAncestor(lambda c: isinstance(c, Core)).lib,
                self.getAncestor(lambda x: isinstance(x, Block)).getMicroSuffix(),
                self.getIntegratedMgFlux(),
                nDensity,
            )
        except AttributeError:
            runLog.warning(
                f"Object {self} does not belong to a core and so has no reaction rates.",
                single=True,
            )
            return {"nG": 0, "nF": 0, "n2n": 0, "nA": 0, "nP": 0}
        except KeyError:
            runLog.warning(
                f"Attempting to get a reaction rate on an isotope not in the lib {nucName}.",
                single=True,
            )
            return {"nG": 0, "nF": 0, "n2n": 0, "nA": 0, "nP": 0}

    def getReactionRates(self, nucName, nDensity=None):
        """
        Get the reaction rates of a certain nuclide on this ArmiObject.

        Parameters
        ----------
        nucName : str
            nuclide name -- e.g. 'U235'
        nDensity : float
            number Density

        Returns
        -------
        rxnRates : dict
            reaction rates (1/s) for nG, nF, n2n, nA and nP

        Notes
        -----
        This is volume integrated NOT (1/cm3-s).

        If you set nDensity to 1 this makes 1-group cross section generation easier.
        """
        from armi.reactor.components import Component

        # find child objects
        objects = self.getChildren(deep=True, predicate=lambda x: isinstance(x, Component))
        if not len(objects):
            objects = [self]

        # The reaction rates for this object is the sum of its children
        rxnRates = {"nG": 0, "nF": 0, "n2n": 0, "nA": 0, "nP": 0, "n3n": 0}
        for armiObject in objects:
            for rxName, val in armiObject._getReactionRates(nucName, nDensity).items():
                rxnRates[rxName] += val

        return rxnRates

    def printContents(self, includeNuclides=True):
        """Display information about all the comprising children in this object."""
        runLog.important(self)
        for c in self:
            c.printContents(includeNuclides=includeNuclides)

    def _genChildByLocationLookupTable(self):
        """Update the childByLocation lookup table."""
        runLog.extra("Generating location-to-child lookup table.")
        self.childrenByLocator = {}
        for child in self:
            self.childrenByLocator[child.spatialLocator] = child

    def getBoundingCircleOuterDiameter(self, Tc=None, cold=False):
        """
        Get sum circle bound.

        Used to roughly approximate relative size vs. other objects
        """
        getter = operator.methodcaller("getBoundingCircleOuterDiameter", Tc, cold)
        return sum(map(getter, self))

    def getPuMoles(self):
        """Returns total number of moles of Pu isotopes."""
        nucNames = [nuc.name for nuc in elements.byZ[94].nuclides]
        puN = np.sum(self.getNuclideNumberDensities(nucNames))

        return puN / units.MOLES_PER_CC_TO_ATOMS_PER_BARN_CM * self.getVolume()


class StateRetainer:
    """
    Retains state during some operations.

    This can be used to temporarily cache state, perform an operation, extract some info, and then
    revert back to the original state.

    * A state retainer is faster than restoring state from a database as it reduces the number of IO
      reads; however, it does use more memory.

    * This can be used on any object within the composite pattern via with
      ``[rabc].retainState([list], [of], [parameters], [to], [retain]):``.
      Use on an object up in the hierarchy applies to all objects below as well.

    * This is intended to work across MPI, so that if you were to broadcast the reactor the state
      would be correct; however the exact implication on ``parameters`` may be unclear.

    """

    def __init__(self, composite: Composite, paramsToApply=None):
        """
        Create an instance of a StateRetainer.

        Parameters
        ----------
        composite: Composite
            composite object to retain state (recursively)

        paramsToApply: iterable of parameters.Parameter
            Iterable of parameters.Parameter to retain updated values after `__exit__`. All other
            parameters are reverted to the original state, i.e. retained at the original value.
        """
        self.composite = composite
        self.paramsToApply = set(paramsToApply or [])

    def __enter__(self):
        self._enterExitHelper(lambda obj: obj.backUp())
        return self

    def __exit__(self, *args):
        self._enterExitHelper(lambda obj: obj.restoreBackup(self.paramsToApply))

    def _enterExitHelper(self, func):
        """Helper method for ``__enter__`` and ``__exit__``. ``func`` is a lambda to either
        ``backUp()`` or ``restoreBackup()``.
        """
        paramDefs = set()
        items = itertools.chain(
            (self.composite,),
            self.composite.iterChildrenWithMaterials(deep=True),
        )
        for child in items:
            if hasattr(child, "p"):
                # materials don't have Parameters
                paramDefs.update(child.p.paramDefs)
            func(child)
        for paramDef in paramDefs:
            func(paramDef)


def gatherMaterialsByVolume(objects: List[ArmiObject], typeSpec: TypeSpec = None, exact=False):
    """
    Compute the total volume of each material in a set of objects and give samples.

    Parameters
    ----------
    objects : list of ArmiObject
        Objects to look within. This argument allows clients to search though some subset of the
        three (e.g. when you're looking for all CLADDING components within FUEL blocks)

    typeSpec : TypeSpec
        Flags for the components to look at

    exact : bool
        Whether or not the TypeSpec is exact

    Notes
    -----
    This helper method is outside the main ArmiObject tree for the special clients that need to
    filter both by container type (e.g. Block type) with one set of flags, and Components with
    another set of flags.

    .. warning:: This is a **composition** related helper method that will likely be filed into
        classes/modules that deal specifically with the composition of things in the data model.
        Thus clients that use it from here should expect to need updates soon.
    """
    volumes = {}
    samples = {}
    for obj in objects:
        for c in obj.iterComponents(typeSpec, exact):
            vol = c.getVolume()
            matName = c.material.getName()
            volumes[matName] = volumes.get(matName, 0.0) + vol
            if matName not in samples:
                samples[matName] = c.material

    return volumes, samples


def getDominantMaterial(objects: List[ArmiObject], typeSpec: TypeSpec = None, exact=False):
    """
    Return the first sample of the most dominant material (by volume) in a set of objects.

    Warning
    -------
    This is a **composition** related helper method that will likely be filed into classes/modules
    that deal specifically with the composition of things in the data model. Thus clients that use
    it from here should expect to need updates soon.
    """
    volumes, samples = gatherMaterialsByVolume(objects, typeSpec, exact)

    if volumes:
        # find matName with max volume
        maxMatName = list(sorted(volumes.items(), key=lambda item: item[1])).pop()[0]
        # return this material. Note that if this material has properties like Zr-frac, enrichment,
        # etc. then this will just return one in the batch, not an average.
        return samples[maxMatName]

    return None


def getReactionRateDict(nucName, lib, xsSuffix, mgFlux, nDens):
    """
    Parameters
    ----------
    nucName : str
        nuclide name -- e.g. 'U235', 'PU239', etc. Not to be confused with the nuclide _label_, see
        the nucDirectory module for a description of the difference.
    lib : isotxs
        cross section library
    xsSuffix : str
        cross section suffix, consisting of the type followed by the burnup group, e.g. 'AB' for the
        second burnup group of type A
    mgFlux : np.ndarray
        integrated mgFlux (n-cm/s)
    nDens : float
        number density (atom/bn-cm)

    Returns
    -------
    rxnRates - dict
        dictionary of reaction rates (rxn/s) for nG, nF, n2n, nA and nP

    Notes
    -----
    Assume there is no n3n cross section in ISOTXS
    """
    nucLabel = nuclideBases.byName[nucName].label
    key = f"{nucLabel}{xsSuffix}"
    libNuc = lib[key]
    rxnRates = {"n3n": 0}
    for rxName, mgXSs in [
        ("nG", libNuc.micros.nGamma),
        ("nF", libNuc.micros.fission),
        ("n2n", libNuc.micros.n2n),
        ("nA", libNuc.micros.nalph),
        ("nP", libNuc.micros.np),
    ]:
        rxnRates[rxName] = nDens * sum(mgXSs * mgFlux)

    return rxnRates
