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
# Production Tracker - per-project settings & team roster storage
#
# Everything the tracker needs to know about people lives in ONE config file
# inside the project itself: whether user management is switched on, the
# email-notification settings, and the team roster. Nothing is stored on a
# shared network location and nothing is global, so a project is fully
# self-contained and can simply be copied or handed over.
#
# The file location is resolved by ASKING PRISM, never by hardcoding folder
# names: users are free to rename "00_Pipeline", "Configs" and everything
# else in the project structure, and this module follows whatever they chose
# - for the current project and for any other project we import from.
#
####################################################


import os

from PrismUtils.Decorators import err_catcher_plugin as err_catcher


# Base name of our config file; the extension is whatever Prism uses for the
# project (.yml / .json), so we stay consistent with the rest of the project.
CONFIG_BASE_NAME = "ProductionTracker"

# The notification send-log is a high-churn diagnostic file, kept next to the
# config as plain JSON.
LOG_FILE_NAME = "ProductionTracker_NotifyLog.json"

# Config sections.
CAT_SETTINGS = "settings"
CAT_USERS = "users"
CAT_DEPARTMENTS = "departments"

# The blank department, always offered as the first choice and never listed
# in the settings dialog (it means "no department set").
DEPT_NONE = ""
NEUTRAL_COLOR = "#6b7280"

# The end of every pipeline. Always appended as the LAST department of both
# lists and never editable, so "finished" is one fixed, recognisable value:
# a Completed shot sitting in this department is what earns the green row
# tint. Because it is added automatically it is not stored in the config.
APPROVED_DEPT = "Approved"
APPROVED_COLOR = "#22c55e"

# ---------------------------------------------------------------------------
#  Tracker mode
#
#  MODE_SHOT  - one row per shot/asset, with a free-text Department column.
#               Tracker fields live in that entity's Prism metadata.
#  MODE_TASKS - the shot's real Prism departments and tasks, as the Project
#               Browser shows them. Each TASK is the tracked row and its
#               fields live in that task's own info file; the shot row
#               becomes a rollup of the tasks under it.
#
#  The two modes read and write different places, so switching is lossless
#  but each mode only shows what was entered in that mode.
# ---------------------------------------------------------------------------
MODE_SHOT = "shot"
MODE_TASKS = "tasks"
TRACKER_MODES = (MODE_SHOT, MODE_TASKS)

# ---------------------------------------------------------------------------
#  Department options
#
#  Editable per project (Production Tracker -> Settings). Shots and assets
#  move through different pipelines, so each has its own list, and FX layer
#  rows are locked to a single department of their own.
#
#  The values below are what a project starts with. "Approved" is not listed:
#  it is appended to both lists automatically.
# ---------------------------------------------------------------------------
DEFAULT_DEPARTMENTS = {
    "shot": [
        {"name": "Layout", "color": "#0ea5e9"},
        {"name": "Animation", "color": "#8b5cf6"},
        {"name": "Look-dev", "color": "#ec4899"},
        {"name": "Rendering", "color": "#f59e0b"},
        {"name": "Comp", "color": "#14b8a6"},
    ],
    "asset": [
        {"name": "Modelling", "color": "#0ea5e9"},
        {"name": "Rigging", "color": "#8b5cf6"},
        {"name": "Look-Dev", "color": "#ec4899"},
    ],
    # FX layer rows are locked to a single department.
    "fxlayer": {"name": "VFX", "color": "#f97316"},
}

DEPARTMENT_KINDS = ("shot", "asset")

DEFAULT_SETTINGS = {
    # Master switch. Off => no roster, no mentions, no notifications; comments
    # are authored under the plain Prism username.
    "enabled": False,
    # Email notifications (only meaningful while "enabled" is True).
    "notify_enabled": False,
    "smtp_host": "",
    "smtp_port": 25,
    "use_tls": False,
    "from_addr": "",
    # Which tracker layout to show (see MODE_SHOT / MODE_TASKS).
    "tracker_mode": MODE_SHOT,
}


class TrackerSettings(object):
    """Read/write the tracker's per-project config file."""

    def __init__(self, core):
        self.core = core

    # =====================================================================
    #  Paths - always resolved through Prism, never hardcoded
    # =====================================================================
    @err_catcher(name=__name__)
    def getPipelineFolder(self, projectPath=None):
        """Pipeline folder of the current project, or of `projectPath`.

        For a foreign project we must resolve that project's OWN folder
        structure, otherwise Prism would answer using the currently open
        project's naming.
        """
        if not projectPath:
            return self.core.projects.getPipelineFolder()

        structure = self.core.projects.getProjectStructure(projectPath=projectPath)
        return self.core.projects.getPipelineFolder(
            projectPath=projectPath, structure=structure
        )

    @err_catcher(name=__name__)
    def getConfigFolderName(self):
        """Leaf name Prism uses for the per-project config folder.

        Asked of Prism rather than written as a literal, so a renamed config
        folder keeps working. Falls back to Prism's own default name.
        """
        try:
            name = os.path.basename(
                os.path.normpath(self.core.projects.getConfigFolder())
            )
        except Exception:
            name = ""
        return name or "Configs"

    @err_catcher(name=__name__)
    def getConfigFolder(self, projectPath=None):
        if not projectPath:
            # Without an open project there is no pipeline folder to hang off,
            # and Prism would hand back a relative path. Say "nowhere" instead.
            if not self.hasProject():
                return ""
            return self.core.projects.getConfigFolder()

        pipeline = self.getPipelineFolder(projectPath)
        if not pipeline:
            return ""
        return os.path.join(pipeline, self.getConfigFolderName())

    @err_catcher(name=__name__)
    def getExtension(self):
        try:
            return self.core.configs.getProjectExtension() or ".json"
        except Exception:
            return ".json"

    @err_catcher(name=__name__)
    def getConfigPath(self, projectPath=None):
        """Where our config file lives (current project unless given)."""
        folder = self.getConfigFolder(projectPath)
        if not folder:
            return ""
        return os.path.join(folder, CONFIG_BASE_NAME + self.getExtension())

    @err_catcher(name=__name__)
    def getLogPath(self):
        folder = self.getConfigFolder()
        if not folder:
            return ""
        return os.path.join(folder, LOG_FILE_NAME)

    @err_catcher(name=__name__)
    def hasProject(self):
        return bool(getattr(self.core, "projectPath", None))

    # =====================================================================
    #  Reading
    # =====================================================================
    @err_catcher(name=__name__)
    def readAll(self, projectPath=None):
        """Whole config file as a dict (empty when absent/unreadable)."""
        path = self.getConfigPath(projectPath)
        if not path or not os.path.exists(path):
            return {}
        try:
            self.core.configs.clearCache(path)
        except Exception:
            pass
        try:
            # No `dft` here on purpose: Prism writes defaults back to disk when
            # one is supplied, and reading must never create a file.
            data = self.core.configs.getConfig(configPath=path)
        except Exception:
            data = None
        return data if isinstance(data, dict) else {}

    @err_catcher(name=__name__)
    def getSettings(self, projectPath=None):
        """Settings dict with every default filled in."""
        settings = dict(DEFAULT_SETTINGS)
        stored = self.readAll(projectPath).get(CAT_SETTINGS)
        if isinstance(stored, dict):
            for key in DEFAULT_SETTINGS:
                if key in stored and stored[key] is not None:
                    settings[key] = stored[key]
        try:
            settings["smtp_port"] = int(settings["smtp_port"])
        except (TypeError, ValueError):
            settings["smtp_port"] = DEFAULT_SETTINGS["smtp_port"]
        settings["enabled"] = bool(settings["enabled"])
        settings["notify_enabled"] = bool(settings["notify_enabled"])
        settings["use_tls"] = bool(settings["use_tls"])
        settings["smtp_host"] = (settings["smtp_host"] or "").strip()
        settings["from_addr"] = (settings["from_addr"] or "").strip()
        if settings["tracker_mode"] not in TRACKER_MODES:
            settings["tracker_mode"] = MODE_SHOT
        return settings

    @err_catcher(name=__name__)
    def isEnabled(self):
        """Is user management switched on for this project?"""
        return self.getSettings()["enabled"]

    @err_catcher(name=__name__)
    def getTrackerMode(self):
        """MODE_SHOT (one row per shot) or MODE_TASKS (departments/tasks)."""
        return self.getSettings()["tracker_mode"]

    @err_catcher(name=__name__)
    def saveTrackerMode(self, mode):
        settings = self.getSettings()
        settings["tracker_mode"] = mode if mode in TRACKER_MODES else MODE_SHOT
        self.saveSettings(settings)

    @err_catcher(name=__name__)
    def notificationsActive(self):
        """Notifications only run when the master switch is on too."""
        s = self.getSettings()
        return bool(s["enabled"] and s["notify_enabled"] and s["smtp_host"])

    @err_catcher(name=__name__)
    def getUsers(self, projectPath=None, ignoreEnabled=False):
        """Roster as a list of clean dicts.

        Returns [] while user management is off, so every caller degrades to
        the "no roster" behaviour without needing its own check. The User
        Manager passes ignoreEnabled=True because it must still show and edit
        the roster while the feature is switched off.
        """
        if not ignoreEnabled and not projectPath and not self.isEnabled():
            return []

        users = self.readAll(projectPath).get(CAT_USERS)
        if not isinstance(users, list):
            return []

        clean = []
        for u in users:
            if not isinstance(u, dict):
                continue
            clean.append(
                {
                    "full_name": (u.get("full_name") or "").strip(),
                    "username": (u.get("username") or "").strip(),
                    "email": (u.get("email") or "").strip(),
                    "color": (u.get("color") or "").strip(),
                }
            )
        clean.sort(key=lambda x: x["full_name"].lower())
        return clean

    # =====================================================================
    #  Writing
    # =====================================================================
    @err_catcher(name=__name__)
    def ensureConfigFolder(self):
        folder = self.getConfigFolder()
        if folder and not os.path.exists(folder):
            os.makedirs(folder)
        return folder

    @err_catcher(name=__name__)
    def saveSettings(self, settings):
        merged = dict(DEFAULT_SETTINGS)
        merged.update(settings or {})
        self.ensureConfigFolder()
        self.core.configs.setConfig(
            cat=CAT_SETTINGS,
            val=merged,
            configPath=self.getConfigPath(),
            updateNestedData=False,
        )

    @err_catcher(name=__name__)
    def saveUsers(self, users):
        self.ensureConfigFolder()
        # Replace the whole section rather than merging, so removed people
        # don't come back from the stored copy.
        self.core.configs.setConfig(
            cat=CAT_USERS,
            val=users or [],
            configPath=self.getConfigPath(),
            updateNestedData=False,
        )

    # =====================================================================
    #  Department options
    # =====================================================================
    @err_catcher(name=__name__)
    def _cleanColor(self, value, fallback=NEUTRAL_COLOR):
        value = (value or "").strip()
        return value if value.startswith("#") and len(value) in (4, 7) else fallback

    @err_catcher(name=__name__)
    def getDepartments(self, projectPath=None):
        """The EDITABLE departments: {"shot": [...], "asset": [...], "fxlayer": {...}}.

        APPROVED_DEPT is deliberately not included - callers append it as the
        final entry themselves (see departmentNames). Any copy of it found in
        a stored config is dropped here so it cannot appear twice.
        """
        stored = self.readAll(projectPath).get(CAT_DEPARTMENTS)
        result = {}

        for kind in DEPARTMENT_KINDS:
            entries = (stored or {}).get(kind) if isinstance(stored, dict) else None
            if not isinstance(entries, list):
                result[kind] = [dict(d) for d in DEFAULT_DEPARTMENTS[kind]]
                continue

            clean = []
            seen = set()
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                name = (entry.get("name") or "").strip()
                # The blank "no department" choice and "Approved" are both
                # implicit - never stored, never editable.
                if not name or name.lower() in seen:
                    continue
                if name.lower() == APPROVED_DEPT.lower():
                    continue
                seen.add(name.lower())
                clean.append(
                    {"name": name, "color": self._cleanColor(entry.get("color"))}
                )
            result[kind] = clean

        fx = (stored or {}).get("fxlayer") if isinstance(stored, dict) else None
        if isinstance(fx, dict) and (fx.get("name") or "").strip():
            result["fxlayer"] = {
                "name": fx["name"].strip(),
                "color": self._cleanColor(
                    fx.get("color"), DEFAULT_DEPARTMENTS["fxlayer"]["color"]
                ),
            }
        else:
            result["fxlayer"] = dict(DEFAULT_DEPARTMENTS["fxlayer"])
        return result

    @err_catcher(name=__name__)
    def departmentNames(self, kind, departments=None):
        """The full ordered dropdown for "shot" or "asset".

        Blank first, the project's own departments in their configured order,
        then APPROVED_DEPT last.
        """
        departments = departments if departments is not None else self.getDepartments()
        names = [d["name"] for d in departments.get(kind, [])]
        return [DEPT_NONE] + names + [APPROVED_DEPT]

    @err_catcher(name=__name__)
    def departmentColors(self, departments=None):
        """name -> colour for every department, including the implicit ones."""
        departments = departments if departments is not None else self.getDepartments()
        colors = {DEPT_NONE: NEUTRAL_COLOR, APPROVED_DEPT: APPROVED_COLOR}
        for kind in DEPARTMENT_KINDS:
            for d in departments.get(kind, []):
                colors[d["name"]] = d["color"]
        fx = departments.get("fxlayer") or {}
        if fx.get("name"):
            colors[fx["name"]] = fx.get("color") or NEUTRAL_COLOR
        return colors

    @err_catcher(name=__name__)
    def saveDepartments(self, departments):
        self.ensureConfigFolder()
        self.core.configs.setConfig(
            cat=CAT_DEPARTMENTS,
            val=departments or {},
            configPath=self.getConfigPath(),
            updateNestedData=False,
        )

    # =====================================================================
    #  Importing from another project
    # =====================================================================
    @err_catcher(name=__name__)
    def isPrismProject(self, projectPath):
        """True when `projectPath` looks like a real Prism project folder."""
        if not projectPath or not os.path.isdir(projectPath):
            return False
        try:
            configPath = self.core.configs.getProjectConfigPath(
                projectPath=projectPath
            )
        except Exception:
            return False
        return bool(configPath and os.path.exists(configPath))

    @err_catcher(name=__name__)
    def findConfigInProject(self, projectPath):
        """Locate our config file inside another Prism project.

        Tries the location that project's own structure implies first, then
        falls back to scanning its pipeline folder - so a config that was
        moved by hand, or written before a structure change, is still found.
        Returns "" when the project has no tracker data.
        """
        direct = self.getConfigPath(projectPath)
        if direct and os.path.exists(direct):
            return direct

        pipeline = self.getPipelineFolder(projectPath)
        if not pipeline or not os.path.isdir(pipeline):
            return ""

        for root, dirs, files in os.walk(pipeline):
            for f in files:
                base, ext = os.path.splitext(f)
                if base == CONFIG_BASE_NAME and ext.lower() in (".json", ".yml", ".yaml"):
                    return os.path.join(root, f)
        return ""

    @err_catcher(name=__name__)
    def importUsersFrom(self, projectPath):
        """Roster stored in another project (may be empty).

        Reads the file we located rather than re-deriving the path, so a
        config found by the fallback scan is read from where it actually is.
        """
        path = self.findConfigInProject(projectPath)
        if not path:
            return []
        try:
            self.core.configs.clearCache(path)
        except Exception:
            pass
        try:
            data = self.core.configs.getConfig(configPath=path)
        except Exception:
            data = None
        if not isinstance(data, dict):
            return []

        users = data.get(CAT_USERS)
        if not isinstance(users, list):
            return []

        clean = []
        for u in users:
            if not isinstance(u, dict):
                continue
            name = (u.get("full_name") or "").strip()
            username = (u.get("username") or "").strip()
            if not name and not username:
                continue
            clean.append(
                {
                    "full_name": name,
                    "username": username,
                    "email": (u.get("email") or "").strip(),
                    "color": (u.get("color") or "").strip(),
                }
            )
        clean.sort(key=lambda x: x["full_name"].lower())
        return clean
