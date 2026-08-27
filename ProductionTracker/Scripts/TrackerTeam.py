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
# Production Tracker - team roster
#
# The roster is stored PER PROJECT (see TrackerSettings) so a project carries
# its own list of people and nothing depends on a shared network location.
# This module is the roster's read side plus the avatar/colour helpers used
# throughout the UI.
#
# While user management is switched off, loadUsers() returns an empty list:
# the tracker then falls back to the plain Prism username everywhere and
# mentions/notifications are inactive.
#
####################################################


import getpass
import hashlib
import os

from qtpy.QtCore import Qt
from qtpy.QtGui import QPixmap, QPainter, QColor, QBrush, QFont, QPen

from PrismUtils.Decorators import err_catcher_plugin as err_catcher

import TrackerSettings


# A palette of pleasant, distinct avatar colors.
AVATAR_PALETTE = [
    "#ef4444", "#f97316", "#f59e0b", "#eab308", "#84cc16", "#22c55e",
    "#10b981", "#14b8a6", "#06b6d4", "#0ea5e9", "#3b82f6", "#6366f1",
    "#8b5cf6", "#a855f7", "#d946ef", "#ec4899", "#f43f5e", "#64748b",
]


class TrackerTeam(object):
    """Load the project team roster and build avatar images."""

    def __init__(self, core):
        self.core = core
        self.settings = TrackerSettings.TrackerSettings(core)

    # ------------------------------------------------------------- storage --
    @err_catcher(name=__name__)
    def isEnabled(self):
        return self.settings.isEnabled()

    @err_catcher(name=__name__)
    def loadUsers(self, ignoreEnabled=False):
        """Roster for this project ([] while user management is off)."""
        users = self.settings.getUsers(ignoreEnabled=ignoreEnabled)
        for u in users:
            if not u.get("color"):
                u["color"] = self.colorForName(
                    u.get("full_name") or u.get("username") or ""
                )
        return users

    @err_catcher(name=__name__)
    def saveUsers(self, users):
        self.settings.saveUsers(users)

    # ----------------------------------------------------------- lookups ----
    @err_catcher(name=__name__)
    def findByUsername(self, username):
        if not username:
            return None
        username = username.strip().lower()
        for u in self.loadUsers():
            if u["username"].lower() == username:
                return u
        return None

    @err_catcher(name=__name__)
    def systemUsernames(self):
        """Candidate identifiers for the person at this machine."""
        names = []
        for val in (
            os.getenv("USERNAME"),
            os.getenv("USER"),
            getattr(self.core, "username", None),
        ):
            if val and val.strip() and val.strip() not in names:
                names.append(val.strip())
        try:
            gu = getpass.getuser()
            if gu and gu not in names:
                names.append(gu)
        except Exception:
            pass
        return names

    @err_catcher(name=__name__)
    def resolveCurrentUser(self, users=None):
        """Match the current system/Prism user against the roster."""
        if users is None:
            users = self.loadUsers()
        for cand in self.systemUsernames():
            c = cand.lower()
            for u in users:
                if u["username"] and u["username"].lower() == c:
                    return u
        return None

    # ------------------------------------------------------------ avatars ---
    @err_catcher(name=__name__)
    def colorForName(self, name):
        """Deterministic palette color from a name (stable across sessions)."""
        if not name:
            return AVATAR_PALETTE[0]
        h = hashlib.md5(name.encode("utf-8")).hexdigest()
        return AVATAR_PALETTE[int(h, 16) % len(AVATAR_PALETTE)]

    @err_catcher(name=__name__)
    def initials(self, fullName, username=""):
        source = (fullName or username or "").strip()
        if not source:
            return "?"
        parts = [p for p in source.replace("_", " ").replace(".", " ").split() if p]
        if not parts:
            return "?"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()

    @err_catcher(name=__name__)
    def makeAvatar(self, fullName, color=None, username="", size=24):
        color = color or self.colorForName(fullName or username)
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(color)))
        p.drawEllipse(0, 0, size - 1, size - 1)

        p.setPen(QPen(QColor("#ffffff")))
        f = QFont()
        f.setBold(True)
        f.setPixelSize(int(size * 0.42))
        p.setFont(f)
        p.drawText(pm.rect(), Qt.AlignCenter, self.initials(fullName, username))
        p.end()
        return pm
