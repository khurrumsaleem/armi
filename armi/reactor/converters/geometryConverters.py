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
Change a reactor from one geometry to another.

Examples may include going from Hex to R-Z or from Third-core to full core.  This module contains
**converters** (which create new reactor objects with different geometry), and **changers** (which
modify a given reactor in place) in this module.

Generally, mass is conserved in geometry conversions.

Warning
-------
These are mostly designed for hex geometry.
"""

import collections
import copy
import math
import operator
import os

import matplotlib
import matplotlib.pyplot as plt
import numpy as np

from armi import materials, runLog
from armi.physics.neutronics.fissionProductModel import lumpedFissionProduct
from armi.reactor import (
    assemblies,
    blocks,
    components,
    geometry,
    grids,
    parameters,
    reactors,
)
from armi.reactor.converters import blockConverters, meshConverters
from armi.reactor.flags import Flags
from armi.reactor.parameters import (
    NEVER,
    SINCE_LAST_GEOMETRY_TRANSFORMATION,
    Category,
    ParamLocation,
)
from armi.utils import hexagon, plotting, units

BLOCK_AXIAL_MESH_SPACING = 20  # Block axial mesh spacing set for nodal diffusion calculation (cm)
STR_SPACE = " "


class GeometryChanger:
    """Geometry changer class that updates the geometry (number of assems or blocks per assem) of a given reactor."""

    def __init__(self, cs=None):
        self._newAssembliesAdded = []
        self._sourceReactor = None
        self._cs = cs

    def __repr__(self):
        return "<{}>".format(self.__class__.__name__)

    def convert(self, r):
        """
        Run the conversion.

        Parameters
        ----------
        r : Reactor object
            The reactor to convert.
        """
        raise NotImplementedError

    def reset(self):
        """
        When called, the reactor core model is reset to it's original configuration, or
        parameter data from the converted reactor core model is transformed back to the origin
        reactor state, thus cleaning up the converted reactor core model.

        Notes
        -----
        This should be implemented on each of the geometry converters.
        """
        runLog.info(f"Resetting the state of the converted reactor core model in {self}")
        self._newAssembliesAdded = []


class GeometryConverter(GeometryChanger):
    """
    Base class for GeometryConverter which makes a new converted reactor.

    Examples
    --------
    To convert a hex case to a R-Z case, do this:

    >>> from armi.reactorConverters import HexToRZConverter
    >>> HexToRZConverter(useMostCommonXsId=False, expandReactor=False)
    >>> geomConv.convert(r)
    >>> newR = geomConv.convReactor
    >>> dif3d = dif3dInterface.Dif3dInterface('dif3dRZ', newR)
    >>> dif3d.o = self.o
    >>> dif3d.writeInput('rzGeom_actual.inp')
    """

    def __init__(self, cs=None):
        GeometryChanger.__init__(self, cs=cs)
        self.convReactor = None


class FuelAssemNumModifier(GeometryChanger):
    """
    Modify the number of fuel assemblies in the reactor.

    Notes
    -----
    - The number of fuel assemblies should ALWAYS be set for the third-core regardless of the
      reactor geometry model.
    - The modification is only valid for third-core and full-core geometry models.
    """

    def __init__(self, cs):
        GeometryChanger.__init__(self, cs)
        self.numFuelAssems = None  # in full core.
        self.fuelType = "feed fuel"
        self.overwriteList = [Flags.REFLECTOR, Flags.SHIELD]
        self.ringsToAdd = []
        self.modifyReactorPower = False

    def convert(self, r):
        """
        Set the number of fuel assemblies in the reactor.

        Notes
        -----
        - While adding fuel, does not modify existing fuel/control positions, but does overwrite
          assemblies in the overwriteList (e.g. reflectors, shields)
        - Once specified amount of fuel is in place, removes all assemblies past the outer fuel boundary
        - To re-add reflector/shield assemblies around the new core, use the ringsToAdd attribute
        """
        self._sourceReactor = r

        if self._sourceReactor.core.powerMultiplier != 1 and self._sourceReactor.core.powerMultiplier != 3:
            raise ValueError(
                "Invalid reactor geometry {} in {}. Reactor must be full or third core to modify the "
                "number of assemblies.".format(r.core.powerMultiplier, self)
            )

        # Set the number of fueled and non-fueled positions within the core (Full core or third-core)
        coreGeom = "full-core" if self._sourceReactor.core.powerMultiplier == 1 else "third-core"
        runLog.info("Modifying {} geometry to have {} fuel assemblies.".format(coreGeom, self.numFuelAssems))
        nonFuelAssems = (
            sum(not assem.hasFlags(Flags.FUEL) for assem in self._sourceReactor.core)
            * self._sourceReactor.core.powerMultiplier
        )
        self.numFuelAssems *= self._sourceReactor.core.powerMultiplier
        totalCoreAssems = nonFuelAssems + self.numFuelAssems

        # Adjust the total power of the reactor by keeping power per assembly constant
        if self.modifyReactorPower:
            self._sourceReactor.core.p.power *= float(self.numFuelAssems) / (
                len(self._sourceReactor.core.getAssemblies(Flags.FUEL)) * self._sourceReactor.core.powerMultiplier
            )

        # Get the sorted assembly locations in the core (Full core or third core)
        assemOrderList = r.core.spatialGrid.generateSortedHexLocationList(totalCoreAssems)
        if self._sourceReactor.core.powerMultiplier == 3:
            assemOrderList = [loc for loc in assemOrderList if r.core.spatialGrid.isInFirstThird(loc)]

        # Add fuel assemblies to the core
        addingFuelIsComplete = False
        numFuelAssemsAdded = 0
        for loc in assemOrderList:
            assem = self._sourceReactor.core.childrenByLocator.get(loc)
            if numFuelAssemsAdded < self.numFuelAssems:
                if assem is None:
                    raise KeyError("Cannot find expected fuel assem in {}".format(loc))
                # Add new fuel assembly to the core
                if assem.hasFlags(self.overwriteList):
                    fuelAssem = self._sourceReactor.core.createAssemblyOfType(assemType=self.fuelType, cs=self._cs)
                    # Remove existing assembly in the core location before adding new assembly
                    if assem.hasFlags(self.overwriteList):
                        self._sourceReactor.core.removeAssembly(assem, discharge=False)
                    self._sourceReactor.core.add(fuelAssem, loc)
                    numFuelAssemsAdded += self._sourceReactor.core.powerMultiplier
                else:
                    # Keep the existing assembly in the core
                    if assem.hasFlags(Flags.FUEL):
                        # Count the assembly in the location if it is fuel
                        numFuelAssemsAdded += self._sourceReactor.core.powerMultiplier
                    else:
                        pass
            # Flag the completion of adding fuel assemblies (see note 1)
            elif numFuelAssemsAdded == self.numFuelAssems:
                addingFuelIsComplete = True

            # Remove the remaining assemblies in the the assembly list once all the fuel has been added
            if addingFuelIsComplete and assem is not None:
                self._sourceReactor.core.removeAssembly(assem, discharge=False)

        # Remove all other assemblies from the core
        for assem in self._sourceReactor.core.getAssemblies():
            if assem.spatialLocator not in assemOrderList:  # check if assembly is on the list
                r.core.removeAssembly(assem, discharge=False)  # get rid of the old assembly

        # Add the remaining rings of assemblies to the core
        for assemType in self.ringsToAdd:
            self.addRing(assemType=assemType)

        # Complete the reactor loading
        self._sourceReactor.core.processLoading(self._cs)
        self._sourceReactor.core.numRings = self._sourceReactor.core.getNumRings()
        self._sourceReactor.core.regenAssemblyLists()
        self._sourceReactor.core.circularRingList = None  # need to reset this (possibly other stuff too)

    def addRing(self, assemType="big shield"):
        """
        Add a ring of fuel assemblies around the outside of an existing core.

        Works by first finding the assembly furthest from the center, then filling in
        all assemblies that are within one pitch further with the specified assembly type

        Parameters
        ----------
        assemType : str
            Assembly type that will be added to the outside of the core
        """
        r = self._sourceReactor
        # first look through the core and finds the one farthest from the center
        maxDist = 0.0
        for assem in r.core.getAssemblies():
            dist = np.linalg.norm(assem.spatialLocator.getGlobalCoordinates())  # get distance from origin
            dist = round(dist, 6)  # round dist to 6 places to avoid differences due to floating point math
            maxDist = max(maxDist, dist)

        # add one hex pitch to the maximum distance to get the bounding distance for the new ring
        hexPitch = r.core.spatialGrid.pitch
        newRingDist = maxDist + hexPitch

        maxArea = math.pi * (newRingDist + hexPitch) ** 2.0  # area that is guaranteed to bound the new core
        maxAssemsFull = maxArea / hexagon.area(hexPitch)  # divide by hex area to get number of hexes in a full core

        # generate ordered list of assembly locations
        assemOrderList = r.core.spatialGrid.generateSortedHexLocationList(maxAssemsFull)
        if r.core.powerMultiplier == 3:
            assemOrderList = [loc for loc in assemOrderList if self._sourceReactor.core.spatialGrid.isInFirstThird(loc)]
        elif r.core.powerMultiplier != 1:
            raise RuntimeError("{} only works on full or 1/3 symmetry.".format(self))
        # add new assemblies to core within one ring
        for locator in assemOrderList:
            assem = r.core.childrenByLocator.get(locator)  # check on assemblies, moving radially outward
            dist = np.linalg.norm(locator.getGlobalCoordinates())
            dist = round(dist, 6)
            if dist <= newRingDist:  # check distance
                if assem is None:  # no assembly in that position, add assembly
                    newAssem = r.core.createAssemblyOfType(assemType=assemType, cs=self._cs)
                    r.core.add(newAssem, locator)  # put new assembly in reactor!
                else:  # all other types of assemblies (fuel, control, etc) leave as is
                    pass
            else:
                pass

    def reset(self):
        """Resetting the reactor core model state after adding fuel assemblies is not currently supported."""
        raise NotImplementedError


class HexToRZThetaConverter(GeometryConverter):
    """
    Convert hex-based cases to an equivalent R-Z-Theta full core geometry.

    Parameters
    ----------
    converterSettings: dict
        Settings that specify how the mesh of the RZTheta reactor should be generated. Controls the
        number of theta regions, how to group regions, etc.

        uniformThetaMesh
            bool flag that determines if the theta mesh should be uniform or not

        thetaBins
            Number of theta bins to create

        radialConversionType
           * ``Ring Compositions`` -- to convert by composition

        axialConversionType
            * ``Axial Coordinates`` --  use
              :py:class:`armi.reactor.converters.meshConverters._RZThetaReactorMeshConverterByAxialCoordinates`
            * ``Axial Bins`` -- use
              :py:class:`armi.reactor.converters.meshConverters._RZThetaReactorMeshConverterByAxialBins`

        homogenizeAxiallyByFlags
            Boolean that if set to True will ignore the `axialConversionType` input and determine a
            mesh based on the material boundaries for each RZ region axially.

    expandReactor : bool
        If True, the HEX-Z reactor will be expanded to full core geometry prior to converting to the
        RZT reactor. Either way the converted RZTheta core will be full core.
    strictHomogenization : bool
        If True, the converter will restrict HEX-Z blocks with dissimilar XS types from being
        homogenized into an RZT block.
    """

    _GEOMETRY_TYPE = geometry.GeomType.RZT
    _SYMMETRY_TYPE = geometry.SymmetryType(
        domainType=geometry.DomainType.FULL_CORE,
        boundaryType=geometry.BoundaryType.NO_SYMMETRY,
    )
    _BLOCK_MIXTURE_TYPE_MAP = {
        "mixture control": ["control"],
        "mixture fuel": ["fuel"],
        "mixture radial shield": ["radial shield"],
        "mixture axial shield": ["shield"],
        "mixture structure": [
            "grid plate",
            "reflector",
            "inlet nozzle",
            "handling socket",
        ],
        "mixture duct": ["duct"],
        "mixture plenum": ["plenum"],
    }

    _BLOCK_MIXTURE_TYPE_EXCLUSIONS = ["control", "fuel", "radial shield"]
    _MESH_BY_RING_COMP = "Ring Compositions"
    _MESH_BY_AXIAL_COORDS = "Axial Coordinates"
    _MESH_BY_AXIAL_BINS = "Axial Bins"

    def __init__(self, cs, converterSettings, expandReactor=False, strictHomogenization=False):
        GeometryConverter.__init__(self, cs)
        self.converterSettings = converterSettings
        self.meshConverter = None
        self._expandSourceReactor = expandReactor
        self._strictHomogenization = strictHomogenization
        self._radialMeshConversionType = None
        self._axialMeshConversionType = None
        self._previousRadialZoneAssemTypes = None
        self._currentRadialZoneType = None
        self._assemsInRadialZone = collections.defaultdict(list)
        self._newBlockNum = 0
        self.blockMap = collections.defaultdict(list)
        self.blockVolFracs = collections.defaultdict(dict)
        self._homogenizeAxiallyByFlags = False

    def _generateConvertedReactorMesh(self):
        """Convert the source reactor using the converterSettings."""
        runLog.info("Generating mesh coordinates for the reactor conversion")
        self._radialMeshConversionType = self.converterSettings["radialConversionType"]
        self._axialMeshConversionType = self.converterSettings["axialConversionType"]
        self._homogenizeAxiallyByFlags = self.converterSettings.get("homogenizeAxiallyByFlags", False)
        converter = None
        if self._radialMeshConversionType == self._MESH_BY_RING_COMP:
            if self._homogenizeAxiallyByFlags:
                converter = meshConverters.RZThetaReactorMeshConverterByRingCompositionAxialFlags(
                    self.converterSettings
                )
            elif self._axialMeshConversionType == self._MESH_BY_AXIAL_COORDS:
                converter = meshConverters.RZThetaReactorMeshConverterByRingCompositionAxialCoordinates(
                    self.converterSettings
                )
            elif self._axialMeshConversionType == self._MESH_BY_AXIAL_BINS:
                converter = meshConverters.RZThetaReactorMeshConverterByRingCompositionAxialBins(self.converterSettings)
        if converter is None:
            raise ValueError(
                "No mesh converter exists for `radialConversionType` and `axialConversionType` settings "
                "of {} and {}".format(self._radialMeshConversionType, self._axialMeshConversionType)
            )
        self.meshConverter = converter
        return self.meshConverter.generateMesh(self._sourceReactor)

    def convert(self, r):
        """
        Run the conversion to 3 dimensional R-Z-Theta.

        .. impl:: Tool to convert a hex core to an RZTheta core.
            :id: I_ARMI_CONV_3DHEX_TO_2DRZ
            :implements: R_ARMI_CONV_3DHEX_TO_2DRZ

            This method converts the hex-z mesh to r-theta-z mesh.
            It first verifies that the geometry type of the input reactor ``r``
            has the expected HEX geometry. Upon conversion, it determines the inner
            and outer diameters of each ring in the r-theta-z mesh and calls
            ``_createRadialThetaZone`` to create a radial theta zone with a homogenized mixture.
            The axial dimension of the r-theta-z mesh is then updated by ``updateAxialMesh``.

        Attributes
        ----------
        r : Reactor object
            The reactor to convert.

        Notes
        -----
        The linked requirement technically points to a child class of this class, HexToRZConverter.
        However, this is the method where the conversion actually happens and thus the
        implementation tag is noted here.

        As a part of the RZT mesh converters it is possible to obtain a radial mesh that has
        repeated ring numbers.  For instance, if there are fuel assemblies and control assemblies
        within the same radial hex ring then it's possible that a radial mesh output from the
        byRingComposition mesh converter method will look something like:

        self.meshConverter.radialMesh = [2, 3, 4, 4, 5, 5, 6, 6, 6, 7, 8, 8, 9, 10]

        In this instance the hex ring will remain the same for multiple iterations over radial
        direction when homogenizing the hex core into the RZT geometry. In this case, the converter
        needs to keep track of the compositions within this ring so that it can separate this
        repeated ring into multiple RZT rings. Each of the RZT rings should have a single
        composition (fuel1, fuel2, control, etc.)

        See Also
        --------
        armi.reactor.converters.meshConverters
        """
        runLog.info(f"Converting {r.core} using {self}")

        if r.core.geomType != geometry.GeomType.HEX:
            raise ValueError("Cannot use {} to convert {} reactor".format(self, str(r.core.geomType).upper()))

        self._sourceReactor = r
        self._setupSourceReactorForConversion()
        rztSpatialGrid = self._generateConvertedReactorMesh()
        runLog.info(rztSpatialGrid)
        self._setupConvertedReactor(rztSpatialGrid)
        self.convReactor.core.lib = self._sourceReactor.core.lib

        innerDiameter = 0.0
        lowerRing = 1
        radialMeshCm = [0.0]
        for radialIndex, upperRing in enumerate(self.meshConverter.radialMesh):
            lowerTheta = 0.0
            # see notes
            self._previousRadialZoneAssemTypes = self._previousRadialZoneAssemTypes if lowerRing == upperRing else []
            if lowerRing == upperRing:
                lowerRing = upperRing - 1

            self._setNextAssemblyTypeInRadialZone(lowerRing, upperRing)
            self._setAssemsInRadialZone(radialIndex, lowerRing, upperRing)
            for thetaIndex, upperTheta in enumerate(self.meshConverter.thetaMesh):
                zoneAssems = self._getAssemsInRadialThetaZone(lowerRing, upperRing, lowerTheta, upperTheta)
                self._writeRadialThetaZoneHeader(
                    radialIndex,
                    lowerRing,
                    upperRing,
                    thetaIndex,
                    lowerTheta,
                    upperTheta,
                )
                outerDiameter = self._createRadialThetaZone(
                    innerDiameter,
                    thetaIndex,
                    radialIndex,
                    lowerTheta,
                    upperTheta,
                    zoneAssems,
                )
                lowerTheta = upperTheta
            innerDiameter = outerDiameter
            lowerRing = upperRing
            radialMeshCm.append(outerDiameter / 2.0)

        # replace temporary index-based ring indices with actual radial distances
        self.convReactor.core.spatialGrid._bounds = (
            self.convReactor.core.spatialGrid._bounds[0],
            np.array(radialMeshCm),
            self.convReactor.core.spatialGrid._bounds[2],
        )

        self.convReactor.core.updateAxialMesh()
        self.convReactor.core.summarizeReactorStats()

        # Track the new assemblies that were created when the converted reactor was
        # initialized so that the global assembly counter can be reset later.
        self._newAssembliesAdded = self.convReactor.core.getAssemblies()

    def _setNextAssemblyTypeInRadialZone(self, lowerRing, upperRing):
        """
        Change the currently-active assembly type to the next active one based on a specific order.

        If this is called with the same (lowerRing, upperRing) twice, the next assembly type
        will be applied. This is useful, for instance, in putting control zones amidst fuel.
        """
        sortedAssemTypes = self._getSortedAssemblyTypesInRadialZone(lowerRing, upperRing)
        for aType in sortedAssemTypes:
            if aType not in self._previousRadialZoneAssemTypes:
                self._previousRadialZoneAssemTypes.append(aType)
                self._currentRadialZoneType = aType
                break

    def _getSortedAssemblyTypesInRadialZone(self, lowerRing, upperRing):
        """
        Retrieve assembly types in a radial zone between (lowerRing, upperRing), sort from highest
        occurrence to lowest.

        Notes
        -----
        - Assembly types are based on the assembly names and not the direct composition within each
          assembly. For instance, if two assemblies are named `fuel 1` and `fuel 2` but they have
          the same composition at some reactor state then they will still be separated as two
          different assembly types.
        """
        aCountByTypes = collections.Counter()
        for a in self._getAssembliesInCurrentRadialZone(lowerRing, upperRing):
            aCountByTypes[a.getType().lower()] += 1

        # sort on tuple (int, str) to force consistent ordering of result when counts are tied
        sortedAssemTypes = sorted(aCountByTypes, key=lambda aType: (aCountByTypes[aType], aType), reverse=True)
        return sortedAssemTypes

    def _getAssembliesInCurrentRadialZone(self, lowerRing, upperRing):
        ringAssems = []
        for ring in range(lowerRing, upperRing):
            ringAssems.extend(self._sourceReactor.core.getAssembliesInSquareOrHexRing(ring))
        return ringAssems

    def _setupSourceReactorForConversion(self):
        self._sourceReactor.core.summarizeReactorStats()
        if self._expandSourceReactor:
            self._expandSourceReactorGeometry()

    def _setupConvertedReactor(self, grid):
        self.convReactor = reactors.Reactor("ConvertedReactor", self._sourceReactor.blueprints)
        core = reactors.Core("Core")
        if self._cs is not None:
            core.setOptionsFromCs(self._cs)
        self.convReactor.add(core)

        grid.symmetry = self._SYMMETRY_TYPE
        grid.geomType = self._GEOMETRY_TYPE
        grid.armiObject = self.convReactor.core
        self.convReactor.core.spatialGrid = grid
        self.convReactor.core.p.power = self._sourceReactor.core.p.power
        self.convReactor.core.name += " - {0}".format(self._GEOMETRY_TYPE)

    def _setAssemsInRadialZone(self, radialIndex, lowerRing, upperRing):
        """
        Retrieve a list of assemblies in the reactor between (lowerRing, upperRing).

        Notes
        -----
        self._assemsInRadialZone keeps track of the unique assemblies that are in each radial ring.
        This ensures that no assemblies are duplicated when using self._getAssemsInRadialThetaZone()
        """
        lowerTheta = 0.0
        for _thetaIndex, upperTheta in enumerate(self.meshConverter.thetaMesh):
            assemsInRadialThetaZone = self._getAssemsInRadialThetaZone(lowerRing, upperRing, lowerTheta, upperTheta)
            newAssemsInRadialZone = set(assemsInRadialThetaZone)
            oldAssemsInRadialZone = set(self._assemsInRadialZone[radialIndex])
            self._assemsInRadialZone[radialIndex].extend(
                sorted(list(newAssemsInRadialZone.union(oldAssemsInRadialZone)))
            )
            lowerTheta = upperTheta

        if not self._assemsInRadialZone[radialIndex]:
            raise ValueError(
                "No assemblies in radial zone {} between rings {} and {}".format(
                    self._assemsInRadialZone[radialIndex], lowerRing, upperRing
                )
            )

    @staticmethod
    def _getAssembliesInSector(core, theta1, theta2):
        """
        Locate assemblies in an angular sector.

        Parameters
        ----------
        theta1, theta2 : float
            The angles (in degrees) in which assemblies shall be drawn.

        Returns
        -------
        aList : list
            List of assemblies in this sector
        """
        aList = []

        converter = EdgeAssemblyChanger()
        converter.addEdgeAssemblies(core)
        for a in core:
            x, y, _ = a.spatialLocator.getLocalCoordinates()
            theta = math.atan2(y, x)
            if theta < 0.0:
                theta = math.tau + theta

            theta = math.degrees(theta)

            phi = theta
            if theta1 <= phi <= theta2 or abs(theta1 - phi) < 0.001 or abs(theta2 - phi) < 0.001:
                aList.append(a)
        converter.removeEdgeAssemblies(core.r.core)

        if not aList:
            raise ValueError("There are no assemblies in {} between angles of {} and {}".format(core, theta1, theta2))

        return aList

    def _getAssemsInRadialThetaZone(self, lowerRing, upperRing, lowerTheta, upperTheta):
        """Retrieve list of assemblies in the reactor between (lowerRing, upperRing) and
        (lowerTheta, upperTheta).
        """
        thetaAssems = self._getAssembliesInSector(
            self._sourceReactor.core, math.degrees(lowerTheta), math.degrees(upperTheta)
        )
        ringAssems = self._getAssembliesInCurrentRadialZone(lowerRing, upperRing)
        if self._radialMeshConversionType == self._MESH_BY_RING_COMP:
            ringAssems = self._selectAssemsBasedOnType(ringAssems)

        ringAssems = set(ringAssems)
        thetaAssems = set(thetaAssems)
        assemsInRadialThetaZone = sorted(ringAssems.intersection(thetaAssems))

        if not assemsInRadialThetaZone:
            raise ValueError(
                "No assemblies in radial-theta zone between rings {} and {} and theta bounds of {} and {}".format(
                    lowerRing, upperRing, lowerTheta, upperTheta
                )
            )

        return assemsInRadialThetaZone

    def _selectAssemsBasedOnType(self, assems):
        """Retrieve a list of assemblies of a given type within a subset of an assembly list.

        Parameters
        ----------
        assems: list
            Subset of assemblies in the reactor.
        """
        selectedAssems = []
        for a in assems:
            if a.getType().lower() == self._currentRadialZoneType:
                selectedAssems.append(a)

        return selectedAssems

    def _createRadialThetaZone(self, innerDiameter, thetaIndex, radialIndex, lowerTheta, upperTheta, zoneAssems):
        """
        Add a new stack of circles to the TRZ reactor by homogenizing assems.

        Parameters
        ----------
        innerDiameter : float
            The current innerDiameter of the radial-theta zone

        thetaIndex : float
            The theta index of the radial-theta zone

        radialIndex : float
            The radial index of the radial-theta zone

        lowerTheta : float
            The lower theta bound for the radial-theta zone

        upperTheta : float
            The upper theta bound for the radial-theta zone

        Returns
        -------
        outerDiameter : float
            The outer diameter (in cm) of the radial zone just added
        """
        newAssembly = assemblies.ThRZAssembly("mixtureAssem")
        newAssembly.spatialLocator = self.convReactor.core.spatialGrid[thetaIndex, radialIndex, 0]
        newAssembly.p.AziMesh = 2
        newAssembly.spatialGrid = grids.AxialGrid.fromNCells(len(self.meshConverter.axialMesh), armiObject=newAssembly)

        lfp = lumpedFissionProduct.lumpedFissionProductFactory(self._cs)

        lowerAxialZ = 0.0
        for axialIndex, upperAxialZ in enumerate(self.meshConverter.axialMesh):
            # Setup the new block data
            newBlockName = "B{:04d}{}".format(int(newAssembly.getNum()), chr(axialIndex + 65))
            newBlock = blocks.ThRZBlock(newBlockName)

            # Compute the homogenized block data
            (
                newBlockAtoms,
                newBlockType,
                newBlockTemp,
                newBlockVol,
            ) = self.createHomogenizedRZTBlock(newBlock, lowerAxialZ, upperAxialZ, zoneAssems)
            # Compute radial zone outer diameter
            axialSegmentHeight = upperAxialZ - lowerAxialZ
            radialZoneVolume = self._calcRadialRingVolume(lowerAxialZ, upperAxialZ, radialIndex)
            radialRingArea = radialZoneVolume / axialSegmentHeight * self._sourceReactor.core.powerMultiplier
            outerDiameter = blockConverters.getOuterDiamFromIDAndArea(innerDiameter, radialRingArea)

            # Set new homogenized block parameters
            material = materials.material.Material()
            material.name = "mixture"
            material.refDens = 1.0  # generic density. Will cancel out.
            dims = {
                "inner_radius": innerDiameter / 2.0,
                "radius_differential": (outerDiameter - innerDiameter) / 2.0,
                "inner_axial": lowerAxialZ,
                "height": axialSegmentHeight,
                "inner_theta": lowerTheta,
                "azimuthal_differential": (upperTheta - lowerTheta),
                "mult": 1.0,
                "Tinput": newBlockTemp,
                "Thot": newBlockTemp,
            }
            for nuc in self._sourceReactor.blueprints.allNuclidesInProblem:
                material.setMassFrac(nuc, 0.0)

            newComponent = components.DifferentialRadialSegment("mixture", material, **dims)
            newBlock.p.axMesh = int(axialSegmentHeight / BLOCK_AXIAL_MESH_SPACING) + 1
            newBlock.p.zbottom = lowerAxialZ
            newBlock.p.ztop = upperAxialZ

            newBlock.setLumpedFissionProducts(lfp)

            # Assign the new block cross section type and burn up group
            newBlock.setType(newBlockType)
            newXsType, newEnvGroup = self._createBlendedXSID(newBlock)
            newBlock.p.xsType = newXsType
            newBlock.p.envGroup = newEnvGroup

            # Update the block dimensions and set the block densities
            newComponent.updateDims()  # ugh.
            newBlock.p.height = axialSegmentHeight
            newBlock.clearCache()
            newBlock.add(newComponent)
            for nuc, atoms in newBlockAtoms.items():
                newBlock.setNumberDensity(nuc, atoms / newBlockVol)

            self._writeRadialThetaZoneInfo(axialIndex + 1, axialSegmentHeight, newBlock)
            self._checkVolumeConservation(newBlock)

            newAssembly.add(newBlock)
            lowerAxialZ = upperAxialZ

        newAssembly.calculateZCoords()  # builds mesh
        self.convReactor.core.add(newAssembly)

        return outerDiameter

    def _calcRadialRingVolume(self, lowerZ, upperZ, radialIndex):
        """Compute the total volume of a list of assemblies within a ring between two axial heights."""
        ringVolume = 0.0
        for assem in self._assemsInRadialZone[radialIndex]:
            for b, heightHere in assem.getBlocksBetweenElevations(lowerZ, upperZ):
                ringVolume += b.getVolume() * heightHere / b.getHeight()

        if not ringVolume:
            raise ValueError("Ring volume of ring {} is 0.0".format(radialIndex + 1))

        return ringVolume

    def _checkVolumeConservation(self, newBlock):
        """Write the volume fractions of each hex block within the homogenized RZT block."""
        newBlockVolumeFraction = 0.0
        for hexBlock in self.blockMap[newBlock]:
            newBlockVolumeFraction += self.blockVolFracs[newBlock][hexBlock]

        if abs(newBlockVolumeFraction - 1.0) > 0.00001:
            raise ValueError(
                "The volume fraction of block {} is {} and not 1.0. An error occurred when "
                "converting the reactor geometry.".format(newBlock, newBlockVolumeFraction)
            )

    def createHomogenizedRZTBlock(self, homBlock, lowerAxialZ, upperAxialZ, radialThetaZoneAssems):
        """
        Create the homogenized RZT block by computing the average atoms in the zone.

        Additional calculations are performed to determine the homogenized block type, the block
        average temperature, and the volume fraction of each hex block that is in the new
        homogenized block.
        """
        homBlockXsTypes = set()
        numHexBlockByType = collections.Counter()
        homBlockAtoms = collections.defaultdict(int)
        homBlockVolume = 0.0
        homBlockTemperature = 0.0
        for assem in radialThetaZoneAssems:
            blocksHere = assem.getBlocksBetweenElevations(lowerAxialZ, upperAxialZ)
            for b, heightHere in blocksHere:
                homBlockXsTypes.add(b.p.xsType)
                numHexBlockByType[b.getType().lower()] += 1
                blockVolumeHere = b.getVolume() * heightHere / b.getHeight()
                if blockVolumeHere == 0.0:
                    raise ValueError("Geometry conversion failed. Block {} has zero volume".format(b))
                homBlockVolume += blockVolumeHere
                homBlockTemperature += b.getAverageTempInC() * blockVolumeHere

                numDensities = b.getNumberDensities()

                for nucName, nDen in numDensities.items():
                    homBlockAtoms[nucName] += nDen * blockVolumeHere
                self.blockMap[homBlock].append(b)
                self.blockVolFracs[homBlock][b] = blockVolumeHere
        # Notify if blocks with different xs types are being homogenized. May be undesired behavior.
        if len(homBlockXsTypes) > 1:
            msg = (
                "Blocks {} with dissimilar XS IDs are being homogenized in {} between axial heights"
                " {} cm and {} cm. ".format(
                    self.blockMap[homBlock],
                    self.convReactor.core,
                    lowerAxialZ,
                    upperAxialZ,
                )
            )
            if self._strictHomogenization:
                raise ValueError(msg + "Modify mesh converter settings before proceeding.")
            else:
                runLog.extra(msg)

        homBlockType = self._getHomogenizedBlockType(numHexBlockByType)
        homBlockTemperature = homBlockTemperature / homBlockVolume
        for b in self.blockMap[homBlock]:
            self.blockVolFracs[homBlock][b] = self.blockVolFracs[homBlock][b] / homBlockVolume

        return homBlockAtoms, homBlockType, homBlockTemperature, homBlockVolume

    def _getHomogenizedBlockType(self, numHexBlockByType):
        """
        Generate the homogenized block mixture type based on the frequency of hex block types that
        were merged together.

        Notes
        -----
        self._BLOCK_MIXTURE_TYPE_EXCLUSIONS:
            The normal function of this method is to assign the mixture name based on the number of
            occurrences of the block type. This list stops that and assigns the mixture based on the
            first occurrence. (i.e. if the mixture has a set of blocks but it comes across one with
            the name of 'control' the process will stop and the new mixture type will be set to
            'mixture control'.

        self._BLOCK_MIXTURE_TYPE_MAP:
            A dictionary that provides the name of blocks that are condensed together
        """
        assignedMixtureBlockType = None

        # Find the most common block type out of the types in the block mixture type exclusions list
        excludedBlockTypesInBlock = set(
            [x for x in self._BLOCK_MIXTURE_TYPE_EXCLUSIONS for y in numHexBlockByType if x in y]
        )
        if excludedBlockTypesInBlock:
            for blockType in self._BLOCK_MIXTURE_TYPE_EXCLUSIONS:
                if blockType in excludedBlockTypesInBlock:
                    assignedMixtureBlockType = "mixture " + blockType
                    return assignedMixtureBlockType

        # Assign block type by most common hex block type
        mostCommonHexBlockType = sorted(numHexBlockByType.most_common(1))[0][0]  # sort needed for tie break

        for mixtureType in sorted(self._BLOCK_MIXTURE_TYPE_MAP):
            validBlockTypesInMixture = self._BLOCK_MIXTURE_TYPE_MAP[mixtureType]
            for validBlockType in validBlockTypesInMixture:
                if validBlockType in mostCommonHexBlockType:
                    assignedMixtureBlockType = mixtureType
                    return assignedMixtureBlockType

        assignedMixtureBlockType = "mixture structure"
        runLog.debug(
            f"The mixture type for this homogenized block {mostCommonHexBlockType} "
            f"was not determined and is defaulting to {assignedMixtureBlockType}"
        )

        return assignedMixtureBlockType

    def _createBlendedXSID(self, newBlock):
        """Generate the blended XS id using the most common XS id in the hexIdList."""
        ids = [hexBlock.getMicroSuffix() for hexBlock in self.blockMap[newBlock]]
        xsTypeList, envGroupList = zip(*ids)

        xsType, _count = collections.Counter(xsTypeList).most_common(1)[0]
        envGroup, _count = collections.Counter(envGroupList).most_common(1)[0]

        return xsType, envGroup

    def _writeRadialThetaZoneHeader(self, radIdx, lowerRing, upperRing, thIdx, lowerTheta, upperTheta):
        radialAssemType = "({})".format(self._currentRadialZoneType) if self._currentRadialZoneType is not None else ""
        runLog.info("Creating: Radial Zone {}, Theta Zone {} {}".format(radIdx + 1, thIdx + 1, radialAssemType))
        runLog.extra(
            "{} Hex Rings: [{}, {}), Theta Revolutions: [{:.2f}, {:.2f})".format(
                9 * STR_SPACE,
                lowerRing,
                upperRing,
                lowerTheta * units.RAD_TO_REV,
                upperTheta * units.RAD_TO_REV,
            )
        )
        runLog.debug(
            "{} Axial Zone - Axial Height (cm) Block Number Block Type             XS ID : "
            "Original Hex Block XS ID(s)".format(9 * STR_SPACE)
        )
        runLog.debug(
            "{} ---------- - ----------------- ------------ ---------------------- ----- : "
            "---------------------------".format(9 * STR_SPACE)
        )

    def _writeRadialThetaZoneInfo(self, axIdx, axialSegmentHeight, blockObj):
        """
        Create a summary of the mapping between the converted reactor block ids to the hex
        reactor block ids.
        """
        self._newBlockNum += 1
        hexBlockXsIds = []
        for hexBlock in self.blockMap[blockObj]:
            hexBlockXsIds.append(hexBlock.getMicroSuffix())

        runLog.debug(
            "{} {:<10} - {:<17.3f} {:<12} {:<22} {:<5} : {}".format(
                9 * STR_SPACE,
                axIdx,
                axialSegmentHeight,
                self._newBlockNum,
                blockObj.getType(),
                blockObj.getMicroSuffix(),
                hexBlockXsIds,
            )
        )

    def _expandSourceReactorGeometry(self):
        """Expansion of the reactor geometry to build the R-Z-Theta core model."""
        runLog.info("Expanding source reactor core to a full core model")
        reactorExpander = ThirdCoreHexToFullCoreChanger(self._cs)
        reactorExpander.convert(self._sourceReactor)
        self._sourceReactor.core.summarizeReactorStats()

    def plotConvertedReactor(self, fNameBase=None):
        """
        Generate plots for the converted RZT reactor.

        Parameters
        ----------
        fNameBase : str, optional
            A name that will form the basis of the N plots that are generated by this method. Will
            get split on extension and have numbers added. Should be like ``coreMap.png``.

        Notes
        -----
        XTView can be used to view the RZT reactor but this is useful to examine the conversion of
        the hex-z reactor to the rzt reactor.

        This makes plots of each individual theta mesh
        """
        runLog.info(
            "Generating plot(s) of the converted {} reactor".format(str(self.convReactor.core.geomType).upper())
        )
        figs = []
        colConv = matplotlib.colors.ColorConverter()
        colGen = plotting.colorGenerator(5)
        blockColors = {}
        thetaMesh, radialMesh, axialMesh = self._getReactorMeshCoordinates()
        innerTheta = 0.0
        for i, outerTheta in enumerate(thetaMesh):
            fig, ax = plt.subplots(figsize=(12, 12))
            innerRadius = 0.0
            for outerRadius in radialMesh:
                innerAxial = 0.0
                for outerAxial in axialMesh:
                    b = self._getBlockAtMeshPoint(
                        innerTheta,
                        outerTheta,
                        innerRadius,
                        outerRadius,
                        innerAxial,
                        outerAxial,
                    )
                    blockType = b.getType()
                    blockColor = self._getBlockColor(colConv, colGen, blockColors, blockType)
                    if blockColor is not None:
                        blockColors[blockType] = blockColor
                    blockPatch = matplotlib.patches.Rectangle(
                        (innerRadius, innerAxial),
                        (outerRadius - innerRadius),
                        (outerAxial - innerAxial),
                        facecolor=blockColors[blockType],
                        linewidth=0,
                        alpha=0.7,
                    )
                    ax.add_patch(blockPatch)
                    innerAxial = outerAxial
                innerRadius = outerRadius
            ax.set_title(
                "{} Core Map from {} to {:.4f} revolutions".format(
                    str(self.convReactor.core.geomType).upper(),
                    innerTheta * units.RAD_TO_REV,
                    outerTheta * units.RAD_TO_REV,
                ),
                y=1.03,
            )
            ax.set_xticks([0.0] + radialMesh)
            ax.set_yticks([0.0] + axialMesh)
            ax.tick_params(axis="both", which="major", labelsize=11, length=0, width=0)
            ax.grid()
            labels = ax.get_xticklabels()
            for label in labels:
                label.set_rotation(270)
            handles = []
            labels = []
            for blockType, blockColor in blockColors.items():
                line = matplotlib.lines.Line2D([], [], color=blockColor, markersize=15, label=blockType)
                handles.append(line)
                labels.append(line.get_label())
            ax.set_xlabel("Radial Mesh (cm)".upper(), labelpad=20)
            ax.set_ylabel("Axial Mesh (cm)".upper(), labelpad=20)
            if fNameBase:
                root, ext = os.path.splitext(fNameBase)
                fName = root + f"{i}" + ext
                plt.savefig(fName)
                plt.close()
            else:
                figs.append(fig)
            innerTheta = outerTheta

        return figs

    def _getReactorMeshCoordinates(self):
        thetaMesh, radialMesh, axialMesh = self.convReactor.core.findAllMeshPoints(applySubMesh=False)
        thetaMesh.remove(0.0)
        radialMesh.remove(0.0)
        axialMesh.remove(0.0)
        return thetaMesh, radialMesh, axialMesh

    def _getBlockAtMeshPoint(self, innerTheta, outerTheta, innerRadius, outerRadius, innerAxial, outerAxial):
        for b in self.convReactor.core.iterBlocks():
            blockMidTh, blockMidR, blockMidZ = b.spatialLocator.getGlobalCoordinates(nativeCoords=True)
            if (blockMidTh >= innerTheta) and (blockMidTh <= outerTheta):
                if (blockMidR >= innerRadius) and (blockMidR <= outerRadius):
                    if (blockMidZ >= innerAxial) and (blockMidZ <= outerAxial):
                        return b
        raise ValueError(
            "No block found between ({}, {}), ({}, {}), ({}, {})\nLast block had TRZ= {} {} {}".format(
                innerTheta,
                outerTheta,
                innerRadius,
                outerRadius,
                innerAxial,
                outerAxial,
                blockMidTh,
                blockMidR,
                blockMidZ,
            )
        )

    @staticmethod
    def _getBlockColor(colConverter, colGenerator, blockColors, blockType):
        nextColor = None
        if blockType not in blockColors:
            if "fuel" in blockType:
                nextColor = "tomato"
            elif "structure" in blockType:
                nextColor = "lightgrey"
            elif "radial shield" in blockType:
                nextColor = "lightgrey"
            elif "duct" in blockType:
                nextColor = "grey"
            else:
                while True:
                    try:
                        nextColor = next(colGenerator)
                        colConverter.to_rgba(nextColor)
                        break
                    except ValueError:
                        continue
        return nextColor

    def reset(self):
        """Clear out attribute data, including holding the state of the converted reactor core model."""
        self.meshConverter = None
        self._radialMeshConversionType = None
        self._axialMeshConversionType = None
        self._previousRadialZoneAssemTypes = None
        self._currentRadialZoneType = None
        self._assemsInRadialZone = collections.defaultdict(list)
        self._newBlockNum = 0
        self.blockMap = collections.defaultdict(list)
        self.blockVolFracs = collections.defaultdict(dict)
        self.convReactor = None
        super().reset()


class HexToRZConverter(HexToRZThetaConverter):
    """
    Create a new reactor with R-Z coordinates from the Hexagonal-Z reactor.

    This is a subclass of the HexToRZThetaConverter. See the HexToRZThetaConverter for
    explanation and setup of the converterSettings.
    """

    _GEOMETRY_TYPE = geometry.GeomType.RZ


class ThirdCoreHexToFullCoreChanger(GeometryChanger):
    """
    Change third-core models to full core in place.

    Does not generate a new reactor object.

    Examples
    --------
    >>> converter = ThirdCoreHexToFullCoreChanger()
    >>> converter.convert(myReactor)
    """

    EXPECTED_INPUT_SYMMETRY = geometry.SymmetryType(geometry.DomainType.THIRD_CORE, geometry.BoundaryType.PERIODIC)

    def __init__(self, cs=None):
        GeometryChanger.__init__(self, cs)
        self.listOfVolIntegratedParamsToScale = []

    def _scaleBlockVolIntegratedParams(self, b, direction):
        if direction == "up":
            op = operator.mul
        elif direction == "down":
            op = operator.truediv

        for param in self.listOfVolIntegratedParamsToScale:
            if b.p[param] is None:
                continue
            if type(b.p[param]) is list:
                # some params like volume-integrated mg flux are lists
                b.p[param] = [op(val, 3) for val in b.p[param]]
            else:
                b.p[param] = op(b.p[param], 3)

    def convert(self, r):
        """
        Run the conversion.

        .. impl:: Convert a one-third-core geometry to a full-core geometry.
            :id: I_ARMI_THIRD_TO_FULL_CORE0
            :implements: R_ARMI_THIRD_TO_FULL_CORE

            This method first checks if the input reactor is already full core. If full-core
            symmetry is detected, the input reactor is returned. If not, it then verifies that the
            input reactor has the expected one-third core symmetry and HEX geometry.

            Upon conversion, it loops over the assembly vector of the source one-third core model,
            copies and rotates each source assembly to create new assemblies, and adds them on the
            full-core grid. For the center assembly, it modifies its parameters.

            Finally, it sets the domain type to full core.

        Parameters
        ----------
        sourceReactor : Reactor object
            The reactor to convert.
        """
        self._sourceReactor = r

        if self._sourceReactor.core.isFullCore:
            # already full core from geometry file. No need to copy symmetry over.
            runLog.important("Detected that full core reactor already exists. Cannot expand.")
            return self._sourceReactor
        elif not (
            self._sourceReactor.core.symmetry == self.EXPECTED_INPUT_SYMMETRY
            and self._sourceReactor.core.geomType == geometry.GeomType.HEX
        ):
            raise ValueError(
                "ThirdCoreHexToFullCoreChanger requires the input to have third core hex geometry. "
                "Geometry received was {} {} {}".format(
                    self._sourceReactor.core.symmetry.domain,
                    self._sourceReactor.core.symmetry.boundary,
                    self._sourceReactor.core.geomType,
                )
            )

        edgeChanger = EdgeAssemblyChanger()
        edgeChanger.removeEdgeAssemblies(self._sourceReactor.core)
        runLog.info("Expanding to full core geometry")

        # store a copy of the 1/3 geometry grid, so that we can use it to find symmetric
        # locations, while the core has a full-core grid so that it does not yell at us
        # for adding stuff outside of the first 1/3
        grid = copy.deepcopy(self._sourceReactor.core.spatialGrid)

        # Set the core grid's symmetry early, since the core uses it for error checks
        self._sourceReactor.core.symmetry = geometry.SymmetryType(
            geometry.DomainType.FULL_CORE, geometry.BoundaryType.NO_SYMMETRY
        )

        for a in self._sourceReactor.core.getAssemblies():
            # make extras and add them too. since the input is assumed to be 1/3 core.
            otherLocs = grid.getSymmetricEquivalents(a.spatialLocator.indices)
            thisZone = (
                self._sourceReactor.core.zones.findZoneItIsIn(a) if len(self._sourceReactor.core.zones) > 0 else None
            )
            angle = 2 * math.pi / (len(otherLocs) + 1)
            count = 1
            for i, j in otherLocs:
                newAssem = copy.deepcopy(a)
                newAssem.makeUnique()
                newAssem.rotate(count * angle)
                count += 1
                self._sourceReactor.core.add(newAssem, self._sourceReactor.core.spatialGrid[i, j, 0])
                if thisZone:
                    thisZone.addLoc(newAssem.getLocation())
                self._newAssembliesAdded.append(newAssem)

            if a.getLocation() == "001-001":
                runLog.extra(f"Modifying parameters in central assembly {a} to convert from 1/3 to full core")

                if not self.listOfVolIntegratedParamsToScale:
                    # populate the list with all parameters that are VOLUME_INTEGRATED
                    (
                        self.listOfVolIntegratedParamsToScale,
                        _,
                    ) = _generateListOfParamsToScale(self._sourceReactor.core, paramsToScaleSubset=[])

                for b in a:
                    self._scaleBlockVolIntegratedParams(b, "up")

        # set domain after expanding, because it isn't actually full core until it's
        # full core; setting the domain causes the core to clear its caches.
        self._sourceReactor.core.symmetry = geometry.SymmetryType(
            geometry.DomainType.FULL_CORE, geometry.BoundaryType.NO_SYMMETRY
        )

    def restorePreviousGeometry(self, r=None):
        """Undo the changes made by convert by going back to 1/3 core.

        .. impl:: Restore a one-third-core geometry to a full-core geometry.
            :id: I_ARMI_THIRD_TO_FULL_CORE1
            :implements: R_ARMI_THIRD_TO_FULL_CORE

            This method is a reverse process of the method ``convert``. It converts the full-core
            reactor model back to the original one-third core reactor model by removing the added
            assemblies and changing the parameters of the center assembly from full core to one
            third core.
        """
        r = r or self._sourceReactor

        # remove the assemblies that were added when the conversion happened.
        if bool(self._newAssembliesAdded):
            for a in self._newAssembliesAdded:
                r.core.removeAssembly(a, discharge=False)

            r.core.symmetry = geometry.SymmetryType.fromAny(self.EXPECTED_INPUT_SYMMETRY)

            # change the central assembly params back to 1/3
            a = r.core.getAssemblyWithStringLocation("001-001")
            runLog.extra(f"Modifying parameters in central assembly {a} to revert from full to 1/3 core")
            for b in a:
                self._scaleBlockVolIntegratedParams(b, "down")
        self.reset()


class EdgeAssemblyChanger(GeometryChanger):
    """
    Add/remove "edge assemblies" for Finite difference or MCNP cases.

    Examples
    --------
        edgeChanger = EdgeAssemblyChanger()
        edgeChanger.removeEdgeAssemblies(reactor.core)
    """

    def addEdgeAssemblies(self, core):
        """
        Add the assemblies on the 120 degree symmetric line to 1/3 symmetric cases.

        Needs to be called before a finite difference (DIF3D, DIFNT) or MCNP calculation.

        .. impl:: Add assemblies along the 120-degree line to a reactor.
            :id: I_ARMI_ADD_EDGE_ASSEMS0
            :implements: R_ARMI_ADD_EDGE_ASSEMS

            Edge assemblies on the 120-degree symmetric line of a one-third core reactor model are
            added because they are needed for DIF3D-finite difference or MCNP models. This is done
            by copying the assemblies from the lower boundary and placing them in their reflective
            positions on the upper boundary of the symmetry line.

        Parameters
        ----------
        reactor : Reactor
            Reactor to modify

        See Also
        --------
        removeEdgeAssemblies : removes the edge assemblies
        """
        if core.isFullCore:
            return

        if self._newAssembliesAdded:
            runLog.important("Skipping addition of edge assemblies because they are already there")
            return

        assembliesOnLowerBoundary = core.getAssembliesOnSymmetryLine(grids.BOUNDARY_0_DEGREES)
        assembliesOnUpperBoundary = []
        for a in assembliesOnLowerBoundary:
            a.clearCache()  # symmetry factors of these assemblies will change since they are now half assems.
            a2 = copy.deepcopy(a)
            a2.makeUnique()
            assembliesOnUpperBoundary.append(a2)

        if not assembliesOnUpperBoundary:
            runLog.extra("No edge assemblies to add")

        # Move the assemblies into their reflective position on symmetry line 3
        for a in assembliesOnUpperBoundary:
            # loc will now be either an empty set [], or two different locations
            # in our case, we only want the first of the two locations
            locs = core.spatialGrid.getSymmetricEquivalents(a.spatialLocator)
            if locs:
                i, j = locs[0]
                spatialLocator = core.spatialGrid[i, j, 0]
                if core.childrenByLocator.get(spatialLocator):
                    runLog.warning("Edge assembly already exists in {0}. Not adding.".format(locs[0]))
                    continue
                # add the copied assembly to the reactor list
                runLog.debug("Adding edge assembly {0} to {1} to the reactor".format(a, spatialLocator))
                core.add(a, spatialLocator)
                self._newAssembliesAdded.append(a)

        parameters.ALL_DEFINITIONS.resetAssignmentFlag(SINCE_LAST_GEOMETRY_TRANSFORMATION)

    def removeEdgeAssemblies(self, core):
        """
        Remove the edge assemblies in preparation for the nodal diffusion approximation.

        This makes use of the assemblies knowledge of if it is in a region that it needs to be
        removed.

        .. impl:: Remove assemblies along the 120-degree line from a reactor.
            :id: I_ARMI_ADD_EDGE_ASSEMS1
            :implements: R_ARMI_ADD_EDGE_ASSEMS

            This method is the reverse process of the method ``addEdgeAssemblies``. It is needed for
            the DIF3D-Nodal calculation. It removes the assemblies on the 120-degree symmetry line.

        See Also
        --------
        addEdgeAssemblies : adds the edge assemblies
        """
        if core.isFullCore:
            return

        assembliesOnLowerBoundary = core.getAssembliesOnSymmetryLine(grids.BOUNDARY_0_DEGREES)
        # Don't use newAssembliesAdded b/c this may be BOL cleaning of a fresh case that has edge
        # assems.
        edgeAssemblies = core.getAssembliesOnSymmetryLine(grids.BOUNDARY_120_DEGREES)

        for a in edgeAssemblies:
            runLog.debug(
                "Removing edge assembly {} from {} from the reactor without discharging".format(
                    a, a.spatialLocator.getRingPos()
                )
            )
            core.removeAssembly(a, discharge=False)

        if edgeAssemblies:
            for a in assembliesOnLowerBoundary:
                a.clearCache()  # clear cached area since symmetry factor will change
            # Reset the SINCE_LAST_GEOMETRY_TRANSFORMATION flag, so that subsequent geometry
            # conversions don't erroneously think they've been changed inside this geometry
            # conversion
            pDefs = parameters.ALL_DEFINITIONS.unchanged_since(NEVER)
            pDefs.setAssignmentFlag(SINCE_LAST_GEOMETRY_TRANSFORMATION)
        else:
            runLog.debug("No edge assemblies to remove.")

        self.reset()

    @staticmethod
    def scaleParamsRelatedToSymmetry(core, paramsToScaleSubset=None):
        """
        Scale volume-dependent params like power to account for cut-off edges.

        These params are at half their full hex value. Scale them right before deleting their
        symmetric identicals. The two operations (scaling them and then removing others) is
        identical to combining two half-assemblies into a full one.

        See Also
        --------
        armi.reactor.converters.geometryConverter.EdgeAssemblyChanger.removeEdgeAssemblies
        armi.reactor.blocks.HexBlock.getSymmetryFactor
        """
        runLog.extra("Scaling edge-assembly parameters to account for full hexes instead of two halves")
        completeListOfParamsToScale = _generateListOfParamsToScale(core, paramsToScaleSubset)
        symmetricAssems = (
            core.getAssembliesOnSymmetryLine(grids.BOUNDARY_0_DEGREES),
            core.getAssembliesOnSymmetryLine(grids.BOUNDARY_120_DEGREES),
        )
        if not all(symmetricAssems):
            runLog.extra("No edge-assemblies found to scale parameters for.")

        for a, aSymmetric in zip(*symmetricAssems):
            for b, bSymmetric in zip(a, aSymmetric):
                _scaleParamsInBlock(b, bSymmetric, completeListOfParamsToScale)


def _generateListOfParamsToScale(core, paramsToScaleSubset):
    fluxParamsToScale = (
        core.getFirstBlock()
        .p.paramDefs.inCategory(Category.fluxQuantities)
        .inCategory(Category.multiGroupQuantities)
        .names
    )
    listOfVolumeIntegratedParamsToScale = (
        core.getFirstBlock()
        .p.paramDefs.atLocation(ParamLocation.VOLUME_INTEGRATED)
        .since(SINCE_LAST_GEOMETRY_TRANSFORMATION)
    )
    listOfVolumeIntegratedParamsToScale = listOfVolumeIntegratedParamsToScale.names
    if paramsToScaleSubset:
        listOfVolumeIntegratedParamsToScale = [
            pn for pn in paramsToScaleSubset if pn in listOfVolumeIntegratedParamsToScale
        ]
    return (listOfVolumeIntegratedParamsToScale, fluxParamsToScale)


def _scaleParamsInBlock(b, bSymmetric, completeListOfParamsToScale):
    """Scale volume-integrated params to include their identical symmetric assemblies."""
    listOfVolumeIntegratedParamsToScale, fluxParamsToScale = completeListOfParamsToScale
    for paramName in [pn for pn in listOfVolumeIntegratedParamsToScale if np.any(b.p[pn])]:
        runLog.debug(
            "Scaling {} in symmetric identical assemblies".format(paramName),
            single=True,
        )
        if paramName in fluxParamsToScale:
            _scaleFluxValues(b, bSymmetric, paramName)  # updated volume weighted fluxes
        else:
            b.p[paramName] = b.p[paramName] + bSymmetric.p[paramName]


def _scaleFluxValues(b, bSymmetric, paramName):
    totalVol = b.getVolume() + bSymmetric.getVolume()

    b.p[paramName] = [f + fSymmetric for f, fSymmetric in zip(b.p[paramName], bSymmetric.p[paramName])]

    newTotalFlux = sum(b.p[paramName]) / totalVol

    if paramName == "mgFlux":
        b.p.flux = newTotalFlux
    elif paramName == "adjMgFlux":
        b.p.fluxAdj = newTotalFlux
    elif paramName == "mgFluxGamma":
        b.p.fluxGamma = newTotalFlux
