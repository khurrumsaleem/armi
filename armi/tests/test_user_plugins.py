# Copyright 2022 TerraPower, LLC
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

"""Tests for the UserPlugin class."""
# pylint: disable=missing-function-docstring,missing-class-docstring,protected-access,invalid-name,no-self-use,no-method-argument,import-outside-toplevel
import copy
import os
import unittest

import pluggy

from armi import context
from armi import getApp
from armi import getPluginManagerOrFail
from armi import interfaces
from armi import plugins
from armi import utils
from armi.bookkeeping.db.database3 import DatabaseInterface
from armi.reactor import zones
from armi.reactor.assemblies import Assembly
from armi.reactor.flags import Flags
from armi.reactor.tests import test_reactors
from armi.settings import caseSettings
from armi.tests import TEST_ROOT
from armi.utils import directoryChangers


class UserPluginFlags(plugins.UserPlugin):
    """Simple UserPlugin that defines a single, new flag."""

    @staticmethod
    @plugins.HOOKIMPL
    def defineFlags():
        return {"SPECIAL": utils.flags.auto()}


class UserPluginFlags2(plugins.UserPlugin):
    """Simple UserPlugin that defines a single, new flag."""

    @staticmethod
    @plugins.HOOKIMPL
    def defineFlags():
        return {"FLAG2": utils.flags.auto()}


class UserPluginFlags3(plugins.UserPlugin):
    """Simple UserPlugin that defines a single, new flag."""

    @staticmethod
    @plugins.HOOKIMPL
    def defineFlags():
        return {"FLAG3": utils.flags.auto()}


# text-file version of a stand-alone Python file for a simple User Plugin
upFlags4 = """
from armi import plugins
from armi import utils

class UserPluginFlags4(plugins.UserPlugin):
    @staticmethod
    @plugins.HOOKIMPL
    def defineFlags():
        return {"FLAG4": utils.flags.auto()}
"""


class UserPluginBadDefinesSettings(plugins.UserPlugin):
    """This is invalid/bad because it implements defineSettings()"""

    @staticmethod
    @plugins.HOOKIMPL
    def defineSettings():
        return [1, 2, 3]


class UserPluginBadDefineParameterRenames(plugins.UserPlugin):
    """This is invalid/bad because it implements defineParameterRenames()"""

    @staticmethod
    @plugins.HOOKIMPL
    def defineParameterRenames():
        return {"oldType": "type"}


class UserPluginOnProcessCoreLoading(plugins.UserPlugin):
    """
    This plugin flex-tests the onProcessCoreLoading() hook,
    and arbitrarily adds "1" to the height of every block,
    after the DB is loaded.
    """

    @staticmethod
    @plugins.HOOKIMPL
    def onProcessCoreLoading(core, cs):
        blocks = core.getBlocks(Flags.FUEL)
        for b in blocks:
            b.p.height += 1.0


class UserPluginDefineZoningStrategy(plugins.UserPlugin):
    """
    This plugin flex-tests the applyZoningStrategy() hook,
    and puts every Assembly into its own Zone.
    """

    @staticmethod
    @plugins.HOOKIMPL
    def applyZoningStrategy(core, cs):
        core.zones = zones.Zones()
        assems = core.getAssemblies()
        for a in assems:
            loc = a.getLocation()
            z = zones.Zone(name=loc, locations=[loc], zoneType=Assembly)
            core.zones.addZone(z)

        return len(core.zones)


class UpInterface(interfaces.Interface):
    """
    A mostly meaningless little test interface, just to prove that we can affect
    the reactor state from an interface inside a UserPlugin
    """

    name = "UpInterface"

    def interactEveryNode(self, cycle, node):
        self.r.core.p.power += 100


class UserPluginWithInterface(plugins.UserPlugin):
    """A little test UserPlugin, just to show how to add an Inteface through a UserPlugin"""

    @staticmethod
    @plugins.HOOKIMPL
    def exposeInterfaces(cs):
        return [
            interfaces.InterfaceInfo(
                interfaces.STACK_ORDER.PREPROCESSING, UpInterface, {"enabled": True}
            )
        ]


class TestUserPlugins(unittest.TestCase):
    def setUp(self):
        """
        Manipulate the standard App. We can't just configure our own, since the
        pytest environment bleeds between tests.
        """
        self._backupApp = copy.deepcopy(getApp())

    def tearDown(self):
        """Restore the App to its original state"""
        import armi

        armi._app = self._backupApp
        context.APP_NAME = "armi"

    def test_userPluginsFlags(self):
        # a basic test that a UserPlugin is loaded
        app = getApp()

        pluginNames = [p[0] for p in app.pluginManager.list_name_plugin()]
        self.assertNotIn("UserPluginFlags", pluginNames)

        app.pluginManager.register(UserPluginFlags)

        pluginNames = [p[0] for p in app.pluginManager.list_name_plugin()]
        self.assertIn("UserPluginFlags", pluginNames)

        # we shouldn't be able to register the same plugin twice
        with self.assertRaises(ValueError):
            app.pluginManager.register(UserPluginFlags)

    def test_validateUserPluginLimitations(self):
        # this should NOT raise any errors
        up = UserPluginFlags()

        # this should raise an error because it has a defineSettings() method
        with self.assertRaises(AssertionError):
            bad0 = UserPluginBadDefinesSettings()

    def test_registerUserPlugins(self):
        app = getApp()

        pluginNames = [p[0] for p in app.pluginManager.list_name_plugin()]
        self.assertNotIn("UserPluginFlags2", pluginNames)

        plugins = ["armi.tests.test_user_plugins.UserPluginFlags2"]
        app.registerUserPlugins(plugins)

        pluginNames = [p[0] for p in app.pluginManager.list_name_plugin()]
        self.assertIn("UserPluginFlags2", pluginNames)

    def test_registerUserPluginsAbsPath(self):
        app = getApp()

        with directoryChangers.TemporaryDirectoryChanger():
            # write a simple UserPlugin to a simple Python file
            with open("plugin4.py", "w") as f:
                f.write(upFlags4)

            # register that plugin using an absolute path
            cwd = os.getcwd()
            plugins = [os.path.join(cwd, "plugin4.py") + ":UserPluginFlags4"]
            app.registerUserPlugins(plugins)

        pluginNames = [p[0] for p in app.pluginManager.list_name_plugin()]
        self.assertIn("UserPluginFlags4", pluginNames)

    def test_registerUserPluginsFromSettings(self):
        app = getApp()
        cs = caseSettings.Settings().modified(
            caseTitle="test_registerUserPluginsFromSettings",
            newSettings={
                "userPlugins": ["armi.tests.test_user_plugins.UserPluginFlags3"],
            },
        )

        pluginNames = [p[0] for p in app.pluginManager.list_name_plugin()]
        self.assertNotIn("UserPluginFlags3", pluginNames)

        cs.registerUserPlugins()

        pluginNames = [p[0] for p in app.pluginManager.list_name_plugin()]
        self.assertIn("UserPluginFlags3", pluginNames)

    def test_userPluginOnProcessCoreLoading(self):
        """
        Test that a UserPlugin can affect the Reactor state,
        by implementing onProcessCoreLoading() to arbitrarily increase the
        height of all the blocks by 1.0
        """
        # register the plugin
        app = getApp()
        name = "UserPluginOnProcessCoreLoading"

        pluginNames = [p[0] for p in app.pluginManager.list_name_plugin()]
        self.assertNotIn(name, pluginNames)
        app.pluginManager.register(UserPluginOnProcessCoreLoading)

        # validate the plugins was registered
        pluginz = app.pluginManager.list_name_plugin()
        pluginNames = [p[0] for p in pluginz]
        self.assertIn(name, pluginNames)

        # grab the loaded plugin
        plug0 = [p[1] for p in pluginz if p[0] == name][0]

        # load a reactor and grab the fuel assemblies
        o, r = test_reactors.loadTestReactor(TEST_ROOT)
        fuels = r.core.getBlocks(Flags.FUEL)

        # prove that our plugin affects the core in the desired way
        heights = [float(f.p.height) for f in fuels]
        plug0.onProcessCoreLoading(core=r.core, cs=o.cs)
        for i, height in enumerate(heights):
            self.assertEqual(fuels[i].p.height, height + 1.0)

    def test_userPluginDefineZoningStrategy(self):
        """
        Test that a UserPlugin can affect the Reactor state,
        by implementing applyZoningStrategy() to arbitrarily put each
        Assembly in the test reactor into its own Zone.
        """
        # register the plugin
        app = getApp()
        name = "UserPluginDefineZoningStrategy"

        # register the zoning plugin
        pluginNames = [p[0] for p in app.pluginManager.list_name_plugin()]
        self.assertNotIn(name, pluginNames)
        app.pluginManager.register(UserPluginDefineZoningStrategy)

        # also register another plugin, to test a more complicated situation
        app.pluginManager.register(UserPluginOnProcessCoreLoading)

        # validate the plugins was registered
        pluginz = app.pluginManager.list_name_plugin()
        pluginNames = [p[0] for p in pluginz]
        self.assertIn(name, pluginNames)

        # load a reactor and grab the fuel assemblies
        o, r = test_reactors.loadTestReactor(TEST_ROOT)

        # prove that our plugin affects the core in the desired way
        self.assertEqual(len(r.core.zones), len(r.core.getAssemblies()))
        name0 = r.core.zones.names[0]
        self.assertIn(name0, r.core.zones[name0])

    def test_userPluginDefineZoningStrategyMultipleFail(self):
        """Ensure that multiple plugins registering Zoning stragies raises an Error"""

        class DuplicateZoner(UserPluginDefineZoningStrategy):
            pass

        # register the plugin
        app = getApp()
        name0 = "UserPluginDefineZoningStrategy"
        name1 = "DuplicateZoner"

        # register the zoning plugin
        pluginNames = [p[0] for p in app.pluginManager.list_name_plugin()]
        self.assertNotIn(name0, pluginNames)
        self.assertNotIn(name1, pluginNames)
        app.pluginManager.register(UserPluginDefineZoningStrategy)
        app.pluginManager.register(DuplicateZoner)

        # validate the plugins was registered
        pluginz = app.pluginManager.list_name_plugin()
        pluginNames = [p[0] for p in pluginz]
        self.assertIn(name0, pluginNames)
        self.assertIn(name1, pluginNames)

        # trying to load a Reactor should raise an error
        with self.assertRaises(RuntimeError):
            o, r = test_reactors.loadTestReactor(TEST_ROOT)

    def test_userPluginWithInterfaces(self):
        """Test that UserPlugins can correctly inject an interface into the stack"""
        # register the plugin
        app = getApp()

        pluginNames = [p[0] for p in app.pluginManager.list_name_plugin()]
        self.assertNotIn("UserPluginWithInterface", pluginNames)

        # register custom UserPlugin, that has an
        plugins = ["armi.tests.test_user_plugins.UserPluginWithInterface"]
        app.registerUserPlugins(plugins)

        pluginNames = [p[0] for p in app.pluginManager.list_name_plugin()]
        self.assertIn("UserPluginWithInterface", pluginNames)

        # load a reactor and grab the fuel assemblieapps
        o, r = test_reactors.loadTestReactor(TEST_ROOT)
        fuels = r.core.getAssemblies(Flags.FUEL)

        # This is here because we have multiple tests altering the App()
        o.interfaces = []
        o.initializeInterfaces(r)

        app.pluginManager.hook.exposeInterfaces(cs=o.cs)

        # This test is not set up for a full run through all the interfaces, for
        # instance, there is not database prepped. So let's skip some interfaces.
        for skipIt in ["fuelhandler", "history"]:
            for i, interf in enumerate(o.interfaces):
                if skipIt in str(interf).lower():
                    o.interfaces = o.interfaces[:i] + o.interfaces[i + 1 :]
                    break

        # test that the core power goes up
        power0 = float(r.core.p.power)
        o.cs["nCycles"] = 2
        o.operate()
        self.assertGreater(r.core.p.power, power0)
