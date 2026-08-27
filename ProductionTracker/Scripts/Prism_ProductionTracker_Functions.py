# -*- coding: utf-8 -*-
#
# Production Tracker - a Prism Pipeline plugin
# Copyright (C) 2026 Trond Hille
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License in the
# LICENSE file, or <https://www.gnu.org/licenses/>, for more details.
#
# Commercial licences, which lift the GPL obligations for proprietary use,
# are available separately from the copyright holder.
#
####################################################
#
# Production Tracker - a custom Prism plugin
#
####################################################


import os

from qtpy.QtCore import *
from qtpy.QtGui import *
from qtpy.QtWidgets import *

from PrismUtils.Decorators import err_catcher_plugin as err_catcher


class Prism_ProductionTracker_Functions(object):
    def __init__(self, core, plugin):
        self.core = core
        self.plugin = plugin
        self.trackerDlg = None
        self.userManagerDlg = None

        # Inject our menu into the Project Browser once its UI is built.
        self.core.registerCallback(
            "projectBrowser_loadUI", self.projectBrowser_loadUI, plugin=self.plugin
        )

    # if returns true, the plugin will be loaded by Prism
    @err_catcher(name=__name__)
    def isActive(self):
        return True

    @err_catcher(name=__name__)
    def projectBrowser_loadUI(self, origin):
        """Add the 'Production Tracker' menu to the Project Browser menu bar."""
        menu = QMenu("Production Tracker", origin)

        actOpen = QAction("Open Tracker", origin)
        iconPath = os.path.join(
            self.pluginDirectory, "Resources", "productiontracker.png"
        )
        if os.path.exists(iconPath):
            actOpen.setIcon(self.core.media.getColoredIcon(iconPath))
        actOpen.triggered.connect(lambda: self.openTracker(origin))
        menu.addAction(actOpen)

        menu.addSeparator()

        actUsers = QAction("User Management", origin)
        actUsers.triggered.connect(lambda: self.openUserManager(origin))
        menu.addAction(actUsers)

        # Insert before the Help menu so it sits with the other app menus.
        origin.menubar.insertMenu(origin.helpMenu.menuAction(), menu)

    @err_catcher(name=__name__)
    def openTracker(self, origin=None):
        """Create (or raise) the Production Tracker window."""
        import ProductionTrackerDlg

        parent = origin or getattr(self.core, "pb", None) or self.core.messageParent

        if self.trackerDlg and self.core.isObjectValid(self.trackerDlg):
            self.trackerDlg.refresh()
            self.trackerDlg.show()
            self.trackerDlg.raise_()
            self.trackerDlg.activateWindow()
            return self.trackerDlg

        self.trackerDlg = ProductionTrackerDlg.ProductionTrackerDlg(
            core=self.core, plugin=self.plugin, parent=parent
        )
        self.trackerDlg.show()
        return self.trackerDlg

    @err_catcher(name=__name__)
    def openUserManager(self, origin=None):
        """Create (or raise) the User Management window for this project."""
        import UserManagerDlg

        parent = origin or getattr(self.core, "pb", None) or self.core.messageParent

        if self.userManagerDlg and self.core.isObjectValid(self.userManagerDlg):
            # Re-read the project's settings and roster before re-showing.
            self.userManagerDlg.loadAll()
            self.userManagerDlg.show()
            self.userManagerDlg.raise_()
            self.userManagerDlg.activateWindow()
            return self.userManagerDlg

        self.userManagerDlg = UserManagerDlg.UserManagerDlg(
            core=self.core, plugin=self.plugin, parent=parent
        )
        # Refresh the tracker's assignee lists after the roster is edited.
        self.userManagerDlg.finished.connect(self._onUserManagerClosed)
        self.userManagerDlg.show()
        return self.userManagerDlg

    @err_catcher(name=__name__)
    def _onUserManagerClosed(self, *args):
        if self.trackerDlg and self.core.isObjectValid(self.trackerDlg):
            try:
                self.trackerDlg.reloadTeam()
            except Exception:
                pass
