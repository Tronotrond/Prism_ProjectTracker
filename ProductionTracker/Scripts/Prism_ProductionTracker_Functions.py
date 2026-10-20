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

import TrackerPrismGuards


# Prism events (in this Prism session) after which an open tracker may show
# something out of date. It checks for changes straight away instead of at
# its next poll; changes made on other machines still come in by polling.
PRISM_CHANGE_EVENTS = (
    "onShotCreated",
    "onAssetCreated",
    "onAssetFolderCreated",
    "onDepartmentCreated",
    "onTaskCreated",
    "onEditShotDlgSaved",
    "postProjectSettingsSave",
    "onProjectBrowserRefreshUI",
)


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
        # Save and close the tracker while the old project is still current,
        # so nothing it holds can be written into the next project.
        self.core.registerCallback(
            "onPreProjectChange", self.onPreProjectChange, plugin=self.plugin
        )

        # Stop Prism's own metadata dialogs and asset-description setter from
        # writing stale copies over tracker data (see TrackerPrismGuards).
        # Installing is idempotent; it is retried after initialisation in
        # case part of Prism wasn't ready when this plugin loaded.
        TrackerPrismGuards.install(self.core, self.plugin)
        self.core.registerCallback(
            "postInitialize", self.installGuards, plugin=self.plugin
        )

        for name in PRISM_CHANGE_EVENTS:
            self.core.registerCallback(name, self.onPrismChanged, plugin=self.plugin)

    # if returns true, the plugin will be loaded by Prism
    @err_catcher(name=__name__)
    def isActive(self):
        return True

    @err_catcher(name=__name__)
    def installGuards(self, *args):
        TrackerPrismGuards.install(self.core, self.plugin)
        self.watchStatusChanges()

    @err_catcher(name=__name__)
    def onPrismChanged(self, *args, **kwargs):
        """Something changed in this Prism session: have an open tracker
        check for it now. Statuses set by Prism's Project Management plugin
        come here too (see watchStatusChanges)."""
        dlg = self.trackerDlg
        if dlg and self.core.isObjectValid(dlg) and dlg.isVisible():
            dlg.requestSync()
        # Its manager may have just been switched (Project Settings saved).
        self.watchStatusChanges()

    @err_catcher(name=__name__)
    def watchStatusChanges(self):
        """Call onPrismChanged after Prism's Project Management plugin sets a
        task status (the Project Browser's status menu), so an open tracker
        shows it straight away. Wraps the plugin's public setTaskStatus and
        its current manager's; does nothing when the plugin isn't loaded."""
        pm = self.core.getPlugin("ProjectManagement")
        if not pm:
            return
        for owner in (pm, getattr(pm, "curManager", None)):
            orig = getattr(owner, "setTaskStatus", None) if owner is not None else None
            if orig is None or getattr(orig, TrackerPrismGuards.GUARD_ATTR, False):
                continue

            def setTaskStatus(*args, _orig=orig, **kwargs):
                result = _orig(*args, **kwargs)
                try:
                    self.onPrismChanged()
                except Exception:
                    pass
                return result

            setattr(setTaskStatus, TrackerPrismGuards.GUARD_ATTR, True)
            try:
                setattr(owner, "setTaskStatus", setTaskStatus)
            except Exception:
                pass

    @err_catcher(name=__name__)
    def onPreProjectChange(self, *args):
        """Close the tracker (saving pending edits) before the project
        changes; it is bound to the project it was opened in."""
        dlg = self.trackerDlg
        if dlg and self.core.isObjectValid(dlg):
            unsaved = dlg.saveBeforeClose() or []
            if unsaved:
                self.core.popup(
                    "Production Tracker could not save these changes before the "
                    "project changed, so they are lost:  %s" % ", ".join(unsaved[:20])
                )
        for attr in ("trackerDlg", "userManagerDlg"):
            dlg = getattr(self, attr)
            if dlg and self.core.isObjectValid(dlg):
                dlg.close()
                dlg.deleteLater()
            setattr(self, attr, None)

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
        # The Project Management plugin may have loaded after this one.
        self.watchStatusChanges()

        if self.trackerDlg and self.core.isObjectValid(self.trackerDlg):
            # An open window is up to date and may hold edits that are still
            # waiting to autosave, so it is only brought to the front. A
            # closed one is re-read, keeping any edit it couldn't save.
            if not self.trackerDlg.isVisible():
                self.trackerDlg.reloadForShow()
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
