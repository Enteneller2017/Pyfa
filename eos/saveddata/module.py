# ===============================================================================
# Copyright (C) 2010 Diego Duclos
#
# This file is part of eos.
#
# eos is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.
#
# eos is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with eos.  If not, see <http://www.gnu.org/licenses/>.
# ===============================================================================

from math import floor

from logbook import Logger
from sqlalchemy.orm import reconstructor, validates

import eos.db
from eos.const import FittingModuleState, FittingHardpoint, FittingSlot
from eos.effectHandlerHelpers import HandledCharge, HandledItem
from eos.modifiedAttributeDict import ChargeAttrShortcut, ItemAttrShortcut, ModifiedAttributeDict
from eos.saveddata.citadel import Citadel
from eos.saveddata.mutator import Mutator
from eos.utils.float import floatUnerr
from eos.utils.spoolSupport import calculateSpoolup, resolveSpoolOptions
from eos.utils.stats import DmgTypes

pyfalog = Logger(__name__)

ProjectedMap = {
    FittingModuleState.OVERHEATED: FittingModuleState.ACTIVE,
    FittingModuleState.ACTIVE: FittingModuleState.OFFLINE,
    FittingModuleState.OFFLINE: FittingModuleState.ACTIVE,
    FittingModuleState.ONLINE: FittingModuleState.ACTIVE  # Just in case
}


# Old state : New State
LocalMap = {
    FittingModuleState.OVERHEATED: FittingModuleState.ACTIVE,
    FittingModuleState.ACTIVE: FittingModuleState.ONLINE,
    FittingModuleState.OFFLINE: FittingModuleState.ONLINE,
    FittingModuleState.ONLINE: FittingModuleState.ACTIVE
}


# For system effects. They should only ever be online or offline
ProjectedSystem = {
    FittingModuleState.OFFLINE: FittingModuleState.ONLINE,
    FittingModuleState.ONLINE: FittingModuleState.OFFLINE
}


class Module(HandledItem, HandledCharge, ItemAttrShortcut, ChargeAttrShortcut):
    """An instance of this class represents a module together with its charge and modified attributes"""
    MINING_ATTRIBUTES = ("miningAmount",)
    SYSTEM_GROUPS = ("Effect Beacon", "MassiveEnvironments", "Abyssal Hazards", "Non-Interactable Object")

    def __init__(self, item, baseItem=None, mutaplasmid=None):
        """Initialize a module from the program"""

        self.itemID = item.ID if item is not None else None
        self.baseItemID = baseItem.ID if baseItem is not None else None
        self.mutaplasmidID = mutaplasmid.ID if mutaplasmid is not None else None

        if baseItem is not None:
            # we're working with a mutated module, need to get abyssal module loaded with the base attributes
            # Note: there may be a better way of doing this, such as a metho on this classe to convert(mutaplamid). This
            # will require a bit more research though, considering there has never been a need to "swap" out the item of a Module
            # before, and there may be assumptions taken with regards to the item never changing (pre-calculated / cached results, for example)
            self.__item = eos.db.getItemWithBaseItemAttribute(self.itemID, self.baseItemID)
            self.__baseItem = baseItem
            self.__mutaplasmid = mutaplasmid
        else:
            self.__item = item
            self.__baseItem = baseItem
            self.__mutaplasmid = mutaplasmid

        if item is not None and self.isInvalid:
            raise ValueError("Passed item is not a Module")

        self.__charge = None

        self.projected = False
        self.state = FittingModuleState.ONLINE
        self.build()

    @reconstructor
    def init(self):
        """Initialize a module from the database and validate"""
        self.__item = None
        self.__baseItem = None
        self.__charge = None
        self.__mutaplasmid = None

        # we need this early if module is invalid and returns early
        self.__slot = self.dummySlot

        if self.itemID:
            self.__item = eos.db.getItem(self.itemID)
            if self.__item is None:
                pyfalog.error("Item (id: {0}) does not exist", self.itemID)
                return

        if self.baseItemID:
            self.__item = eos.db.getItemWithBaseItemAttribute(self.itemID, self.baseItemID)
            self.__baseItem = eos.db.getItem(self.baseItemID)
            self.__mutaplasmid = eos.db.getMutaplasmid(self.mutaplasmidID)
            if self.__baseItem is None:
                pyfalog.error("Base Item (id: {0}) does not exist", self.itemID)
                return

        if self.isInvalid:
            pyfalog.error("Item (id: {0}) is not a Module", self.itemID)
            return

        if self.chargeID:
            self.__charge = eos.db.getItem(self.chargeID)

        self.build()

    def build(self):
        """ Builds internal module variables from both init's """

        if self.__charge and self.__charge.category.name != "Charge":
            self.__charge = None

        self.__baseVolley = None
        self.__baseRemoteReps = None
        self.__miningyield = None
        self.__reloadTime = None
        self.__reloadForce = None
        self.__chargeCycles = None
        self.__hardpoint = FittingHardpoint.NONE
        self.__itemModifiedAttributes = ModifiedAttributeDict(parent=self)
        self.__chargeModifiedAttributes = ModifiedAttributeDict(parent=self)
        self.__slot = self.dummySlot  # defaults to None

        if self.__item:
            self.__itemModifiedAttributes.original = self.__item.attributes
            self.__itemModifiedAttributes.overrides = self.__item.overrides
            self.__hardpoint = self.__calculateHardpoint(self.__item)
            self.__slot = self.calculateSlot(self.__item)

            # Instantiate / remove mutators if this is a mutated module
            if self.__baseItem:
                for x in self.mutaplasmid.attributes:
                    attr = self.item.attributes[x.name]
                    id = attr.ID
                    if id not in self.mutators:  # create the mutator
                        Mutator(self, attr, attr.value)
                # @todo: remove attributes that are no longer part of the mutaplasmid.

            self.__itemModifiedAttributes.mutators = self.mutators

        if self.__charge:
            self.__chargeModifiedAttributes.original = self.__charge.attributes
            self.__chargeModifiedAttributes.overrides = self.__charge.overrides

    @classmethod
    def buildEmpty(cls, slot):
        empty = Module(None)
        empty.__slot = slot
        empty.dummySlot = slot
        return empty

    @classmethod
    def buildRack(cls, slot, num=None):
        empty = Rack(None)
        empty.__slot = slot
        empty.dummySlot = slot
        empty.num = num
        return empty

    @property
    def isEmpty(self):
        return self.dummySlot is not None

    @property
    def hardpoint(self):
        return self.__hardpoint

    @property
    def isInvalid(self):
        # todo: validate baseItem as well if it's set.
        if self.isEmpty:
            return False
        return (
            self.__item is None or (
                self.__item.category.name not in ("Module", "Subsystem", "Structure Module") and
                self.__item.group.name not in self.SYSTEM_GROUPS) or
            (self.item.isAbyssal and not self.isMutated))

    @property
    def isMutated(self):
        return self.baseItemID and self.mutaplasmidID

    @property
    def numCharges(self):
        return self.getNumCharges(self.charge)

    def getNumCharges(self, charge):
        if charge is None:
            charges = 0
        else:
            chargeVolume = charge.volume
            containerCapacity = self.item.capacity
            if chargeVolume is None or containerCapacity is None:
                charges = 0
            else:
                charges = int(floatUnerr(containerCapacity / chargeVolume))
        return charges

    @property
    def numShots(self):
        if self.charge is None:
            return 0
        if self.__chargeCycles is None and self.charge:
            numCharges = self.numCharges
            # Usual ammo like projectiles and missiles
            if numCharges > 0 and "chargeRate" in self.itemModifiedAttributes:
                self.__chargeCycles = self.__calculateAmmoShots()
            # Frequency crystals (combat and mining lasers)
            elif numCharges > 0 and "crystalsGetDamaged" in self.chargeModifiedAttributes:
                self.__chargeCycles = self.__calculateCrystalShots()
            # Scripts and stuff
            else:
                self.__chargeCycles = 0
            return self.__chargeCycles
        else:
            return self.__chargeCycles

    @property
    def modPosition(self):
        if self.owner:
            return self.owner.modules.index(self) if not self.isProjected else self.owner.projectedModules.index(self)

    @property
    def isProjected(self):
        if self.owner:
            return self in self.owner.projectedModules
        return None

    @property
    def isExclusiveSystemEffect(self):
        return self.item.group.name in ("Effect Beacon", "Non-Interactable Object", "MassiveEnvironments")

    @property
    def isCapitalSize(self):
        return self.getModifiedItemAttr("volume", 0) >= 4000

    @property
    def hpBeforeReload(self):
        """
        If item is some kind of repairer with charges, calculate
        HP it reps before going into reload.
        """
        cycles = self.numShots
        armorRep = self.getModifiedItemAttr("armorDamageAmount") or 0
        shieldRep = self.getModifiedItemAttr("shieldBonus") or 0
        if not cycles or (not armorRep and not shieldRep):
            return 0
        hp = round((armorRep + shieldRep) * cycles)
        return hp

    def __calculateAmmoShots(self):
        if self.charge is not None:
            # Set number of cycles before reload is needed
            # numcycles = math.floor(module_capacity / (module_volume * module_chargerate))
            chargeRate = self.getModifiedItemAttr("chargeRate")
            numCharges = self.numCharges
            numShots = floor(numCharges / chargeRate)
        else:
            numShots = None
        return numShots

    def __calculateCrystalShots(self):
        if self.charge is not None:
            if self.getModifiedChargeAttr("crystalsGetDamaged") == 1:
                # For depletable crystals, calculate average amount of shots before it's destroyed
                hp = self.getModifiedChargeAttr("hp")
                chance = self.getModifiedChargeAttr("crystalVolatilityChance")
                damage = self.getModifiedChargeAttr("crystalVolatilityDamage")
                crystals = self.numCharges
                numShots = floor((crystals * hp) / (damage * chance))
            else:
                # Set 0 (infinite) for permanent crystals like t1 laser crystals
                numShots = 0
        else:
            numShots = None
        return numShots

    @property
    def maxRange(self):
        attrs = ("maxRange", "shieldTransferRange", "powerTransferRange",
                 "energyDestabilizationRange", "empFieldRange",
                 "ecmBurstRange", "warpScrambleRange", "cargoScanRange",
                 "shipScanRange", "surveyScanRange")
        for attr in attrs:
            maxRange = self.getModifiedItemAttr(attr, None)
            if maxRange is not None:
                return maxRange
        if self.charge is not None:
            try:
                chargeName = self.charge.group.name
            except AttributeError:
                pass
            else:
                if chargeName in ("Scanner Probe", "Survey Probe"):
                    return None
            # Source: http://www.eveonline.com/ingameboard.asp?a=topic&threadID=1307419&page=1#15
            # D_m = V_m * (T_m + T_0*[exp(- T_m/T_0)-1])
            maxVelocity = self.getModifiedChargeAttr("maxVelocity")
            flightTime = self.getModifiedChargeAttr("explosionDelay") / 1000.0
            mass = self.getModifiedChargeAttr("mass")
            agility = self.getModifiedChargeAttr("agility")
            if maxVelocity and (flightTime or mass or agility):
                accelTime = min(flightTime, mass * agility / 1000000)
                # Average distance done during acceleration
                duringAcceleration = maxVelocity / 2 * accelTime
                # Distance done after being at full speed
                fullSpeed = maxVelocity * (flightTime - accelTime)
                return duringAcceleration + fullSpeed

    @property
    def falloff(self):
        attrs = ("falloffEffectiveness", "falloff", "shipScanFalloff")
        for attr in attrs:
            falloff = self.getModifiedItemAttr(attr, None)
            if falloff is not None:
                return falloff

    @property
    def slot(self):
        return self.__slot

    @property
    def itemModifiedAttributes(self):
        return self.__itemModifiedAttributes

    @property
    def chargeModifiedAttributes(self):
        return self.__chargeModifiedAttributes

    @property
    def item(self):
        return self.__item if self.__item != 0 else None

    @property
    def baseItem(self):
        return self.__baseItem

    @property
    def mutaplasmid(self):
        return self.__mutaplasmid

    @property
    def charge(self):
        return self.__charge if self.__charge != 0 else None

    @charge.setter
    def charge(self, charge):
        self.__charge = charge
        if charge is not None:
            self.chargeID = charge.ID
            self.__chargeModifiedAttributes.original = charge.attributes
            self.__chargeModifiedAttributes.overrides = charge.overrides
        else:
            self.chargeID = None
            self.__chargeModifiedAttributes.original = None
            self.__chargeModifiedAttributes.overrides = {}

        self.__itemModifiedAttributes.clear()

    @property
    def miningStats(self):
        if self.__miningyield is None:
            if self.isEmpty:
                self.__miningyield = 0
            else:
                if self.state >= FittingModuleState.ACTIVE:
                    volley = self.getModifiedItemAttr("specialtyMiningAmount") or self.getModifiedItemAttr(
                            "miningAmount") or 0
                    if volley:
                        cycleTime = self.cycleTime
                        self.__miningyield = volley / (cycleTime / 1000.0)
                    else:
                        self.__miningyield = 0
                else:
                    self.__miningyield = 0

        return self.__miningyield

    def getVolley(self, spoolOptions=None, targetResists=None, ignoreState=False):
        if self.isEmpty or (self.state < FittingModuleState.ACTIVE and not ignoreState):
            return DmgTypes(0, 0, 0, 0)
        if self.__baseVolley is None:
            dmgGetter = self.getModifiedChargeAttr if self.charge else self.getModifiedItemAttr
            dmgMult = self.getModifiedItemAttr("damageMultiplier", 1)
            self.__baseVolley = DmgTypes(
                em=(dmgGetter("emDamage", 0)) * dmgMult,
                thermal=(dmgGetter("thermalDamage", 0)) * dmgMult,
                kinetic=(dmgGetter("kineticDamage", 0)) * dmgMult,
                explosive=(dmgGetter("explosiveDamage", 0)) * dmgMult)
        spoolType, spoolAmount = resolveSpoolOptions(spoolOptions, self)
        spoolBoost = calculateSpoolup(
            self.getModifiedItemAttr("damageMultiplierBonusMax", 0),
            self.getModifiedItemAttr("damageMultiplierBonusPerCycle", 0),
            self.rawCycleTime / 1000, spoolType, spoolAmount)[0]
        spoolMultiplier = 1 + spoolBoost
        volley = DmgTypes(
            em=self.__baseVolley.em * spoolMultiplier * (1 - getattr(targetResists, "emAmount", 0)),
            thermal=self.__baseVolley.thermal * spoolMultiplier * (1 - getattr(targetResists, "thermalAmount", 0)),
            kinetic=self.__baseVolley.kinetic * spoolMultiplier * (1 - getattr(targetResists, "kineticAmount", 0)),
            explosive=self.__baseVolley.explosive * spoolMultiplier * (1 - getattr(targetResists, "explosiveAmount", 0)))
        return volley

    def getDps(self, spoolOptions=None, targetResists=None, ignoreState=False):
        volley = self.getVolley(spoolOptions=spoolOptions, targetResists=targetResists, ignoreState=ignoreState)
        if not volley:
            return DmgTypes(0, 0, 0, 0)
        # Some weapons repeat multiple times in one cycle (bosonic doomsdays). Get the number of times it fires off
        volleysPerCycle = max(self.getModifiedItemAttr("doomsdayDamageDuration", 1) / self.getModifiedItemAttr("doomsdayDamageCycleTime", 1), 1)
        dpsFactor = volleysPerCycle / (self.cycleTime / 1000)
        dps = DmgTypes(
            em=volley.em * dpsFactor,
            thermal=volley.thermal * dpsFactor,
            kinetic=volley.kinetic * dpsFactor,
            explosive=volley.explosive * dpsFactor)
        return dps

    def getRemoteReps(self, spoolOptions=None, ignoreState=False):
        if self.isEmpty or (self.state < FittingModuleState.ACTIVE and not ignoreState):
            return None, 0

        def getBaseRemoteReps(module):
            remoteModuleGroups = {
                "Remote Armor Repairer": "Armor",
                "Ancillary Remote Armor Repairer": "Armor",
                "Mutadaptive Remote Armor Repairer": "Armor",
                "Remote Hull Repairer": "Hull",
                "Remote Shield Booster": "Shield",
                "Ancillary Remote Shield Booster": "Shield",
                "Remote Capacitor Transmitter": "Capacitor"}
            rrType = remoteModuleGroups.get(module.item.group.name, None)
            if not rrType:
                return None, 0
            if rrType == "Hull":
                rrAmount = module.getModifiedItemAttr("structureDamageAmount", 0)
            elif rrType == "Armor":
                rrAmount = module.getModifiedItemAttr("armorDamageAmount", 0)
            elif rrType == "Shield":
                rrAmount = module.getModifiedItemAttr("shieldBonus", 0)
            elif rrType == "Capacitor":
                rrAmount = module.getModifiedItemAttr("powerTransferAmount", 0)
            else:
                return None, 0
            if rrAmount:
                rrAmount *= 1 / (self.cycleTime / 1000)
                if module.item.group.name == "Ancillary Remote Armor Repairer" and module.charge:
                    rrAmount *= module.getModifiedItemAttr("chargedArmorDamageMultiplier", 1)

            return rrType, rrAmount

        if self.__baseRemoteReps is None:
            self.__baseRemoteReps = getBaseRemoteReps(self)

        rrType, rrAmount = self.__baseRemoteReps

        if rrType and rrAmount and self.item.group.name == "Mutadaptive Remote Armor Repairer":
            spoolType, spoolAmount = resolveSpoolOptions(spoolOptions, self)
            spoolBoost = calculateSpoolup(
                self.getModifiedItemAttr("repairMultiplierBonusMax", 0),
                self.getModifiedItemAttr("repairMultiplierBonusPerCycle", 0),
                self.rawCycleTime / 1000, spoolType, spoolAmount)[0]
            rrAmount *= (1 + spoolBoost)

        return rrType, rrAmount

    def getSpoolData(self, spoolOptions=None):
        weaponMultMax = self.getModifiedItemAttr("damageMultiplierBonusMax", 0)
        weaponMultPerCycle = self.getModifiedItemAttr("damageMultiplierBonusPerCycle", 0)
        if weaponMultMax and weaponMultPerCycle:
            spoolType, spoolAmount = resolveSpoolOptions(spoolOptions, self)
            _, spoolCycles, spoolTime = calculateSpoolup(
                weaponMultMax, weaponMultPerCycle,
                self.rawCycleTime / 1000, spoolType, spoolAmount)
            return spoolCycles, spoolTime
        rrMultMax = self.getModifiedItemAttr("repairMultiplierBonusMax", 0)
        rrMultPerCycle = self.getModifiedItemAttr("repairMultiplierBonusPerCycle", 0)
        if rrMultMax and rrMultPerCycle:
            spoolType, spoolAmount = resolveSpoolOptions(spoolOptions, self)
            _, spoolCycles, spoolTime = calculateSpoolup(
                rrMultMax, rrMultPerCycle,
                self.rawCycleTime / 1000, spoolType, spoolAmount)
            return spoolCycles, spoolTime
        return 0, 0

    @property
    def reloadTime(self):
        # Get reload time from attrs first, then use
        # custom value specified otherwise (e.g. in effects)
        moduleReloadTime = self.getModifiedItemAttr("reloadTime")
        if moduleReloadTime is None:
            moduleReloadTime = self.__reloadTime
        return moduleReloadTime or 0.0

    @reloadTime.setter
    def reloadTime(self, milliseconds):
        self.__reloadTime = milliseconds

    @property
    def forceReload(self):
        return self.__reloadForce

    @forceReload.setter
    def forceReload(self, type):
        self.__reloadForce = type

    def fits(self, fit, hardpointLimit=True):
        """
        Function that determines if a module can be fit to the ship. We always apply slot restrictions no matter what
        (too many assumptions made on this), however all other fitting restrictions are optional
        """

        slot = self.slot
        if fit.getSlotsFree(slot) <= (0 if self.owner != fit else -1):
            return False

        fits = self.__fitRestrictions(fit, hardpointLimit)

        if not fits and fit.ignoreRestrictions:
            self.restrictionOverridden = True
            fits = True

        return fits

    def __fitRestrictions(self, fit, hardpointLimit=True):

        if not fit.canFit(self.item):
            return False

        # EVE doesn't let capital modules be fit onto subcapital hulls. Confirmed by CCP Larrikin that this is dictated
        # by the modules volume. See GH issue #1096
        if not isinstance(fit.ship, Citadel) and fit.ship.getModifiedItemAttr("isCapitalSize", 0) != 1 and self.isCapitalSize:
            return False

        # If the mod is a subsystem, don't let two subs in the same slot fit
        if self.slot == FittingSlot.SUBSYSTEM:
            subSlot = self.getModifiedItemAttr("subSystemSlot")
            for mod in fit.modules:
                if mod.getModifiedItemAttr("subSystemSlot") == subSlot:
                    return False

        # Check rig sizes
        if self.slot == FittingSlot.RIG:
            if self.getModifiedItemAttr("rigSize") != fit.ship.getModifiedItemAttr("rigSize"):
                return False

        # Check max group fitted
        max = self.getModifiedItemAttr("maxGroupFitted", None)
        if max is not None:
            current = 0  # if self.owner != fit else -1  # Disabled, see #1278
            for mod in fit.modules:
                if (mod.item and mod.item.groupID == self.item.groupID and
                        self.modPosition != mod.modPosition):
                    current += 1

            if current >= max:
                return False

        # Check this only if we're told to do so
        if hardpointLimit:
            if fit.getHardpointsFree(self.hardpoint) < 1:
                return False

        return True

    def isValidState(self, state):
        """
        Check if the state is valid for this module, without considering other modules at all
        """
        # Check if we're within bounds
        if state < -1 or state > 2:
            return False
        elif state >= FittingModuleState.ACTIVE and not self.item.isType("active"):
            return False
        elif state == FittingModuleState.OVERHEATED and not self.item.isType("overheat"):
            return False
        else:
            return True

    def getMaxState(self, proposedState=None):
        states = sorted((s for s in FittingModuleState if proposedState is None or s <= proposedState), reverse=True)
        for state in states:
            if self.isValidState(state):
                return state

    def canHaveState(self, state=None, projectedOnto=None):
        """
        Check with other modules if there are restrictions that might not allow this module to be activated
        """
        # If we're going to set module to offline or online for local modules or offline for projected,
        # it should be fine for all cases
        item = self.item
        if (state <= FittingModuleState.ONLINE and projectedOnto is None) or (state <= FittingModuleState.OFFLINE):
            return True

        # Check if the local module is over it's max limit; if it's not, we're fine
        maxGroupActive = self.getModifiedItemAttr("maxGroupActive", None)
        if maxGroupActive is None and projectedOnto is None:
            return True

        # Following is applicable only to local modules, we do not want to limit projected
        if projectedOnto is None:
            currActive = 0
            group = item.group.name
            for mod in self.owner.modules:
                currItem = getattr(mod, "item", None)
                if mod.state >= FittingModuleState.ACTIVE and currItem is not None and currItem.group.name == group:
                    currActive += 1
                if currActive > maxGroupActive:
                    break
            return currActive <= maxGroupActive
        # For projected, we're checking if ship is vulnerable to given item
        else:
            # Do not allow to apply offensive modules on ship with offensive module immunite, with few exceptions
            # (all effects which apply instant modification are exception, generally speaking)
            if item.offensive and projectedOnto.ship.getModifiedItemAttr("disallowOffensiveModifiers") == 1:
                offensiveNonModifiers = {"energyDestabilizationNew",
                                         "leech",
                                         "energyNosferatuFalloff",
                                         "energyNeutralizerFalloff"}
                if not offensiveNonModifiers.intersection(set(item.effects)):
                    return False
            # If assistive modules are not allowed, do not let to apply these altogether
            if item.assistive and projectedOnto.ship.getModifiedItemAttr("disallowAssistance") == 1:
                return False
            return True

    def isValidCharge(self, charge):
        # Check sizes, if 'charge size > module volume' it won't fit
        if charge is None:
            return True
        chargeVolume = charge.volume
        moduleCapacity = self.item.capacity
        if chargeVolume is not None and moduleCapacity is not None and chargeVolume > moduleCapacity:
            return False

        itemChargeSize = self.getModifiedItemAttr("chargeSize")
        if itemChargeSize > 0:
            chargeSize = charge.getAttribute('chargeSize')
            if itemChargeSize != chargeSize:
                return False

        chargeGroup = charge.groupID
        for i in range(5):
            itemChargeGroup = self.getModifiedItemAttr('chargeGroup' + str(i), None)
            if itemChargeGroup is None:
                continue
            if itemChargeGroup == chargeGroup:
                return True

        return False

    def getValidCharges(self):
        validCharges = set()
        for i in range(5):
            itemChargeGroup = self.getModifiedItemAttr('chargeGroup' + str(i), None)
            if itemChargeGroup is not None:
                g = eos.db.getGroup(int(itemChargeGroup), eager="items.attributes")
                if g is None:
                    continue
                for singleItem in g.items:
                    if singleItem.published and self.isValidCharge(singleItem):
                        validCharges.add(singleItem)

        return validCharges

    @staticmethod
    def __calculateHardpoint(item):
        effectHardpointMap = {
            "turretFitted"  : FittingHardpoint.TURRET,
            "launcherFitted": FittingHardpoint.MISSILE
        }

        if item is None:
            return FittingHardpoint.NONE

        for effectName, slot in effectHardpointMap.items():
            if effectName in item.effects:
                return slot

        return FittingHardpoint.NONE

    @staticmethod
    def calculateSlot(item):
        effectSlotMap = {
            "rigSlot"    : FittingSlot.RIG.value,
            "loPower"    : FittingSlot.LOW.value,
            "medPower"   : FittingSlot.MED.value,
            "hiPower"    : FittingSlot.HIGH.value,
            "subSystem"  : FittingSlot.SUBSYSTEM.value,
            "serviceSlot": FittingSlot.SERVICE.value
        }
        if item is None:
            return None
        for effectName, slot in effectSlotMap.items():
            if effectName in item.effects:
                return slot
        if item.group.name in Module.SYSTEM_GROUPS:
            return FittingSlot.SYSTEM

        return None

    @validates("ID", "itemID", "ammoID")
    def validator(self, key, val):
        map = {
            "ID"    : lambda _val: isinstance(_val, int),
            "itemID": lambda _val: _val is None or isinstance(_val, int),
            "ammoID": lambda _val: isinstance(_val, int)
        }

        if not map[key](val):
            raise ValueError(str(val) + " is not a valid value for " + key)
        else:
            return val

    def clear(self):
        self.__baseVolley = None
        self.__baseRemoteReps = None
        self.__miningyield = None
        self.__reloadTime = None
        self.__reloadForce = None
        self.__chargeCycles = None
        self.itemModifiedAttributes.clear()
        self.chargeModifiedAttributes.clear()

    def calculateModifiedAttributes(self, fit, runTime, forceProjected=False, gang=False):
        # We will run the effect when two conditions are met:
        # 1: It makes sense to run the effect
        #    The effect is either offline
        #    or the effect is passive and the module is in the online state (or higher)

        #    or the effect is active and the module is in the active state (or higher)
        #    or the effect is overheat and the module is in the overheated state (or higher)
        # 2: the runtimes match

        if self.projected or forceProjected:
            context = "projected", "module"
            projected = True
        else:
            context = ("module",)
            projected = False

        # if gang:
        #     context += ("commandRun",)

        if self.charge is not None:
            # fix for #82 and it's regression #106
            if not projected or (self.projected and not forceProjected) or gang:
                for effect in self.charge.effects.values():
                    if effect.runTime == runTime and \
                            effect.activeByDefault and \
                            (effect.isType("offline") or
                             (effect.isType("passive") and self.state >= FittingModuleState.ONLINE) or
                             (effect.isType("active") and self.state >= FittingModuleState.ACTIVE)) and \
                            (not gang or (gang and effect.isType("gang"))):

                        chargeContext = ("moduleCharge",)
                        # For gang effects, we pass in the effect itself as an argument. However, to avoid going through
                        # all the effect files and defining this argument, do a simple try/catch here and be done with it.
                        # @todo: possibly fix this
                        try:
                            effect.handler(fit, self, chargeContext, effect=effect)
                        except:
                            effect.handler(fit, self, chargeContext)

        if self.item:
            if self.state >= FittingModuleState.OVERHEATED:
                for effect in self.item.effects.values():
                    if effect.runTime == runTime and \
                            effect.isType("overheat") \
                            and not forceProjected \
                            and effect.activeByDefault \
                            and ((gang and effect.isType("gang")) or not gang):
                        effect.handler(fit, self, context)

            for effect in self.item.effects.values():
                if effect.runTime == runTime and \
                        effect.activeByDefault and \
                        (effect.isType("offline") or
                         (effect.isType("passive") and self.state >= FittingModuleState.ONLINE) or
                         (effect.isType("active") and self.state >= FittingModuleState.ACTIVE)) \
                        and ((projected and effect.isType("projected")) or not projected) \
                        and ((gang and effect.isType("gang")) or not gang):
                    try:
                        effect.handler(fit, self, context, effect=effect)
                    except:
                        effect.handler(fit, self, context)

    @property
    def cycleTime(self):
        # Determine if we'll take into account reload time or not
        factorReload = self.owner.factorReload if self.forceReload is None else self.forceReload

        numShots = self.numShots
        speed = self.rawCycleTime

        if factorReload and self.charge:
            raw_reload_time = self.reloadTime
        else:
            raw_reload_time = 0.0

        # Module can only fire one shot at a time, think bomb launchers or defender launchers
        if self.disallowRepeatingAction:
            if numShots > 0:
                """
                The actual mechanics behind this is complex.  Behavior will be (for 3 ammo):
                    fire, reactivation delay, fire, reactivation delay, fire, max(reactivation delay, reload)
                so your effective reload time depends on where you are at in the cycle.

                We can't do that, so instead we'll average it out.

                Currently would apply to bomb launchers and defender missiles
                """
                effective_reload_time = ((self.reactivationDelay * (numShots - 1)) + max(raw_reload_time, self.reactivationDelay, 0))
            else:
                """
                Applies to MJD/MJFG
                """
                effective_reload_time = max(raw_reload_time, self.reactivationDelay, 0)
                speed = speed + effective_reload_time
        else:
            """
            Currently no other modules would have a reactivation delay, so for sanities sake don't try and account for it.
            Okay, technically cloaks do, but they also have 0 cycle time and cap usage so why do you care?
            """
            effective_reload_time = raw_reload_time

        if numShots > 0 and self.charge:
            speed = (speed * numShots + effective_reload_time) / numShots

        return speed

    @property
    def rawCycleTime(self):
        speed = max(
                self.getModifiedItemAttr("speed", 0),  # Most weapons
                self.getModifiedItemAttr("duration", 0),  # Most average modules
                self.getModifiedItemAttr("durationSensorDampeningBurstProjector", 0),
                self.getModifiedItemAttr("durationTargetIlluminationBurstProjector", 0),
                self.getModifiedItemAttr("durationECMJammerBurstProjector", 0),
                self.getModifiedItemAttr("durationWeaponDisruptionBurstProjector", 0)
        )
        return speed

    @property
    def disallowRepeatingAction(self):
        return self.getModifiedItemAttr("disallowRepeatingActivation", 0)

    @property
    def reactivationDelay(self):
        return self.getModifiedItemAttr("moduleReactivationDelay", 0)

    @property
    def capUse(self):
        capNeed = self.getModifiedItemAttr("capacitorNeed")
        if capNeed and self.state >= FittingModuleState.ACTIVE:
            cycleTime = self.cycleTime
            if cycleTime > 0:
                capUsed = capNeed / (cycleTime / 1000.0)
                return capUsed
        else:
            return 0

    @staticmethod
    def getProposedState(mod, click, proposedState=None):
        # todo: instead of passing in module, make this a instanced function.
        pyfalog.debug("Get proposed state for module.")
        if mod.slot == FittingSlot.SUBSYSTEM or mod.isEmpty:
            return FittingModuleState.ONLINE

        if mod.slot == FittingSlot.SYSTEM:
            transitionMap = ProjectedSystem
        else:
            transitionMap = ProjectedMap if mod.projected else LocalMap

        currState = mod.state

        if proposedState is not None:
            state = proposedState
        elif click == "right":
            state = FittingModuleState.OVERHEATED
        elif click == "ctrl":
            state = FittingModuleState.OFFLINE
        else:
            state = transitionMap[currState]
            if not mod.isValidState(state):
                state = -1

        if mod.isValidState(state):
            return state
        else:
            return currState

    def __deepcopy__(self, memo):
        item = self.item
        if item is None:
            copy = Module.buildEmpty(self.slot)
        else:
            copy = Module(self.item, self.baseItem, self.mutaplasmid)
        copy.charge = self.charge
        copy.state = self.state

        for x in self.mutators.values():
            Mutator(copy, x.attribute, x.value)

        return copy

    def rebase(self, item):
        state = self.state
        charge = self.charge
        Module.__init__(self, item, self.baseItem, self.mutaplasmid)
        self.state = state
        if self.isValidCharge(charge):
            self.charge = charge
        for x in self.mutators.values():
            Mutator(self, x.attribute, x.value)

    def __repr__(self):
        if self.item:
            return "Module(ID={}, name={}) at {}".format(
                    self.item.ID, self.item.name, hex(id(self))
            )
        else:
            return "EmptyModule() at {}".format(hex(id(self)))


class Rack(Module):
    """
    This is simply the Module class named something else to differentiate
    it for app logic. The only thing interesting about it is the num property,
    which is the number of slots for this rack
    """
    num = None
