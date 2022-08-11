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
A Zone object is a collection of locations in the Core.
A Zones object is a collection of Zone objects.
Together, they are used to conceptually divide the Core for analysis.
"""
from typing import Iterator, List, Optional, Set, Union

from armi import getPluginManagerOrFail
from armi import runLog
from armi.reactor.assemblies import Assembly
from armi.reactor.blocks import Block
from armi.reactor.flags import Flags
from armi.settings.fwSettings import globalSettings


class Zone:
    """
    A group of locations in the Core, used to divide it up for analysis.
    Each location represents an Assembly or a Block.
    """

    VALID_TYPES = (Assembly, Block)

    def __init__(
        self, name: str, locations: Optional[List] = None, zoneType: type = Assembly
    ):
        self.name = name

        # A single Zone must contain items of the same type
        if zoneType not in Zone.VALID_TYPES:
            raise TypeError(
                "Invalid Type {0}; A Zone can only be of type {1}".format(
                    zoneType, Zone.VALID_TYPES
                )
            )
        self.zoneType = zoneType

        # a Zone is mostly just a collection of locations in the Reactor
        if locations is None:
            self.locs = set()
        else:
            # NOTE: We are not validating the locations.
            self.locs = set(locations)

    def __contains__(self, loc: str) -> bool:
        return loc in self.locs

    def __iter__(self) -> Iterator[str]:
        """Loop through the locations, in alphabetical order."""
        for loc in sorted(self.locs):
            yield loc

    def __len__(self) -> int:
        """Return the number of locations"""
        return len(self.locs)

    def __repr__(self) -> str:
        zType = "Assemblies"
        if self.zoneType == Block:
            zType = "Blocks"

        return "<Zone {0} with {1} {2}>".format(self.name, len(self), zType)

    def addLoc(self, loc: str) -> None:
        """
        Adds the location to this Zone.

        Parameters
        ----------
        items : list
            List of str objects

        Notes
        -----
        This method does not validate that the location given is somehow "valid".
        We are not doing any reverse lookups in the Reactor to prove that the type
        or location is valid. Because this would require heavier computation, and
        would add some chicken-and-the-egg problems into instantiating a new Reactor.

        Returns
        -------
        None
        """
        assert isinstance(loc, str), "The location must be a str: {0}".format(loc)
        self.locs.add(loc)

    def addLocs(self, locs: List) -> None:
        """
        Adds the locations to this Zone

        Parameters
        ----------
        items : list
            List of str objects
        """
        for loc in locs:
            self.addLoc(loc)

    def addItem(self, item: Union[Assembly, Block]) -> None:
        """
        Adds the location of an Assembly or Block to a zone

        Parameters
        ----------
        item : Assembly or Block
            A single item with Core location (Assembly or Block)
        """
        assert issubclass(
            type(item), self.zoneType
        ), "The item ({0}) but be have a type in: {1}".format(item, Zone.VALID_TYPES)
        self.addLoc(item.getLocation())

    def addItems(self, items: List) -> None:
        """
        Adds the locations of a list of Assemblies or Blocks to a zone

        Parameters
        ----------
        items : list
            List of Assembly/Block objects
        """
        for item in items:
            self.addItem(item)


class Zones:
    """Collection of Zone objects."""

    def __init__(self):
        """Build a Zones object."""
        self._zones = {}

    @property
    def names(self) -> List:
        """Ordered names of contained zones.

        Returns
        -------
        list
            Alphabetical collection of Zone names
        """
        return sorted(self._zones.keys())

    def __contains__(self, name: str) -> bool:
        return name in self._zones

    def __delitem__(self, name: str) -> None:
        del self._zones[name]

    def __getitem__(self, name: str) -> Zone:
        """Access a zone by name."""
        return self._zones[name]

    def __iter__(self) -> Iterator[Zone]:
        """Loop through the zones in order."""
        for nm in sorted(self._zones.keys()):
            yield self._zones[nm]

    def __len__(self) -> int:
        """Return the number of Zone objects"""
        return len(self._zones)

    def addZone(self, zone: Zone) -> None:
        """Add a zone to the collection.

        Parameters
        ----------
        zone: Zone
            A new Zone to add to this collection.

        Returns
        -------
        None
        """
        if zone.name in self._zones:
            raise ValueError(
                "Cannot add {} because a zone of that name already exists.".format(
                    zone.name
                )
            )
        self._zones[zone.name] = zone

    def addZones(self, zones: List) -> None:
        """
        Add multiple zones to the collection,
        then validate that this Zones collection still make sense.

        Parameters
        ----------
        zones: List (or Zones)
            A multiple new Zone objects to add to this collection.

        Returns
        -------
        None
        """
        for zone in zones:
            self.addZone(zone)

        self.checkDuplicates()

    def removeZone(self, name: str) -> None:
        """delete a zone by name

        Parameters
        ----------
        name: str
            Name of zone to remove

        Returns
        -------
        None
        """
        del self[name]

    def checkDuplicates(self) -> None:
        """
        Validate that the the zones are mutually exclusive.

        That is, make sure that no item appears in more than one Zone.

        Returns
        -------
        None
        """
        allLocs = []
        for zone in self:
            allLocs.extend(list(zone.locs))

        # use set lotic to test for duplicates
        if len(allLocs) == len(set(allLocs)):
            # no duplicates
            return

        # find duplicates by removing unique locs from the full list
        for uniqueLoc in set(allLocs):
            allLocs.remove(uniqueLoc)

        # there are duplicates, so raise an error
        locs = sorted(set(allLocs))
        raise RuntimeError("Duplicate items found in Zones: {0}".format(locs))

    def getZoneLocations(self, zoneNames: List) -> Set:
        """
        Get the location labels of a particular (or a few) zone(s).

        Parameters
        ----------
        zoneNames : str, or list
            the zone name or list of names

        Returns
        -------
        zoneLocs : set
            List of location labels of this/these zone(s)
        """
        if not isinstance(zoneNames, list):
            zoneNames = [zoneNames]

        zoneLocs = set()
        for zn in zoneNames:
            try:
                thisZoneLocs = set(self[zn])
            except KeyError:
                runLog.error(
                    "The zone {0} does not exist. Please define it.".format(zn)
                )
                raise
            zoneLocs.update(thisZoneLocs)

        return zoneLocs

    def getAllLocations(self) -> Set:
        """Return all locations across every Zone in this Zones object

        Returns
        -------
        set
            A combination set of all locations, from every Zone
        """
        locs = set()
        for zoneName in self:
            locs.update(self[zoneName])

        return locs

    def findZoneItIsIn(self, a: Union[Assembly, Block]) -> Optional[Zone]:
        """
        Return the zone object that this Assembly/Block is in.

        Parameters
        ----------
        a : Assembly or Block
           The item to locate

        Returns
        -------
        zone : Zone object that the input item resides in.
        """
        aLoc = a.getLocation()
        zoneFound = False
        for zone in self:
            if aLoc in zone.locs:
                zoneFound = True
                return zone

        if not zoneFound:
            runLog.warning("Was not able to find which zone {} is in".format(a))

        return None


# TODO: This only works for Assemblies!
def zoneSummary(core, zoneNames=None):
    """
    Print out power distribution of fuel assemblies this/these zone.

    Parameters
    ----------
    core : Core
        A fully-initialized Core object
    zoneNames : list, optional
        The names of the zones you want to inspect. Leave blank to summarize all zones.

    Returns
    -------
    None
    """
    if zoneNames is None:
        zoneNames = core.zones.names

    msg = "Zone Summary"
    if core.r is not None:
        msg += " at Cycle {0}, timenode {1}".format(core.r.p.cycle, core.r.p.timeNode)

    runLog.info(msg)
    totalPower = 0.0

    for zoneName in sorted(zoneNames):
        runLog.info("zone {0}".format(zoneName))
        massFlow = 0.0

        # find the maximum power to flow in each zone
        maxPower = -1.0
        fuelAssemsInZone = core.getAssemblies(Flags.FUEL, zones=zoneName)
        a = []
        for a in fuelAssemsInZone:
            flow = a.p.THmassFlowRate * a.getSymmetryFactor()
            aPow = a.calcTotalParam("power", calcBasedOnFullObj=True)
            if aPow > maxPower:
                maxPower = aPow

            if not flow:
                runLog.important(
                    "No TH data. Run with thermal hydraulics activated. "
                    "Zone report will have flow rate of zero",
                    single=True,
                    label="Cannot summarize zone T/H",
                )
                # no TH for some reason
                flow = 0.0

            massFlow += flow

        # Get power from the extracted power method.
        slabPowList = _getZoneAxialPowerDistribution(core, zoneName)
        if not slabPowList or not fuelAssemsInZone:
            runLog.important("No fuel assemblies exist in zone {0}".format(zoneName))
            return

        # loop over the last assembly to produce the final output.
        z = 0.0
        totalZonePower = 0.0
        for zi, b in enumerate(a):
            slabHeight = b.getHeight()
            thisSlabPow = slabPowList[zi]
            runLog.info(
                "  Power of {0:8.3f} cm slab at z={1:8.3f} (W): {2:12.5E}"
                "".format(slabHeight, z, thisSlabPow)
            )
            z += slabHeight
            totalZonePower += thisSlabPow

        runLog.info("  Total Zone Power (Watts): {0:.3E}".format(totalZonePower))
        runLog.info(
            "  Zone Average Flow rate (kg/s): {0:.3f}"
            "".format(massFlow / len(fuelAssemsInZone))
        )
        runLog.info(
            "  There are {0} assemblies in this zone"
            "".format(len(fuelAssemsInZone) * core.powerMultiplier)
        )

        totalPower += totalZonePower

    runLog.info("Total power of fuel in all zones is {0:.6E} Watts".format(totalPower))


def _getZoneAxialPowerDistribution(core, zoneName):
    """
    Return a list of powers in watts of the axial levels of zone.
    (Helper method for Zones summary.)

    Parameters
    ----------
    core : Core
        A fully-initialized Core object
    zoneName : str
        The name of the zone you want to inspect.

    See Also
    --------
    zoneSummary

    Returns
    -------
    list
        Block powers, ordered by axial position.
    """
    slabPower = {}
    zi = 0
    for a in core.getAssemblies(Flags.FUEL, zones=[zoneName]):
        # Add up slab power and flow rates
        for zi, b in enumerate(a):
            slabPower[zi] = (
                slabPower.get(zi, 0.0)
                + b.p.power * b.getSymmetryFactor() * core.powerMultiplier
            )

    # reorder the dictionary into a list, knowing that zi is stopped at the highest block
    slabPowList = []
    for i in range(zi + 1):
        try:
            slabPowList.append(slabPower[i])
        except:
            runLog.warning("slabPower {} zone {}".format(slabPower, zoneName))

    return slabPowList


def buildZones(core, cs) -> None:
    """
    Build/update the Zones.

    The zoning option is determined by the ``zoningStrategy`` setting.

    Parameters
    ----------
    core : Core
        A fully-initialized Core object
    cs : CaseSettings
        The standard ARMI settings object

    Notes
    -----
    This method is being reconsidered, so it currently only supports manual zoning.

    Returns
    -------
    None
    """
    zoneCounts = getPluginManagerOrFail().hook.applyZoningStrategy(core=core, cs=cs)

    if len(zoneCounts) > 1:
        raise RuntimeError("Only one plugin can register a Zoning Strategy.")

    if len(zoneCounts) == 0:
        zones = Zones()
        zones.addZones(buildManualZones(cs))
        core.zones = zones


def buildManualZones(cs):
    """
    Build the Zones that are defined manually in the given CaseSettings file,
    in the `zoneDefinitions` setting.

    Parameters
    ----------
    cs : CaseSettings
        The standard ARMI settings object

    Examples
    --------
    Manual zones will be defined in a special string format, e.g.:

    zoneDefinitions:
        - ring-1: 001-001
        - ring-2: 002-001, 002-002
        - ring-3: 003-001, 003-002, 003-003

    Notes
    -----
    This function will just define the Zones it sees in the settings, it does
    not do any validation against a Core object to ensure those manual zones
    make sense.

    Returns
    -------
    Zones
        One or more zones, as defined in the `zoneDefinitions` setting.
    """
    runLog.debug("Building Zones by manual definitions in `zoneDefinitions` setting")
    stripper = lambda s: s.strip()
    zones = Zones()

    # parse the special input string for zone definitions
    for zoneString in cs["zoneDefinitions"]:
        zoneName, zoneLocs = zoneString.split(":")
        zoneLocs = zoneLocs.split(",")
        zone = Zone(zoneName.strip())
        zone.addLocs(map(stripper, zoneLocs))
        zones.addZone(zone)

    if not len(zones):
        runLog.debug("No manual zones defined in `zoneDefinitions` setting")

    return zones
