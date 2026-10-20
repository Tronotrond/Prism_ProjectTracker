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
# Production Tracker - task statuses from Prism's Project Management
#
# Prism's Project Management plugin gives every task a status of its own
# once a project's "Manager" (Project Settings -> Project Management) is set.
# In "Prism departments and tasks" mode the tracker then shows and edits
# that status instead of keeping a second one, so the tracker and the
# Project Browser always agree.
#
# Which manager a project uses is read from the project's own settings, not
# from whether the plugin is loaded on this machine, so every artist works
# on the same status even where the plugin isn't installed:
#
#   "Prism"        The status is the "status" key of the task's info file
#                  and the list of statuses is in the project settings, so
#                  both can be read and written without the plugin. When it
#                  is loaded, writes go through it. The info file holds the
#                  status's ABBREVIATION ("fin", "wtg"), which is what Prism
#                  writes and looks its icon up by: a full name there shows
#                  as "?" in Prism. The tracker shows and edits names, so
#                  read() maps either spelling to the name and write()
#                  stores the abbreviation (see storedValue / nameOf).
#   Kitsu, ...     The status lives on that server and only the plugin can
#                  reach it. Without the plugin the status is shown as
#                  unavailable and can't be changed.
#   None / unset   The tracker keeps its own statuses (tracker_status).
#
####################################################


import logging
import os

from PrismUtils.Decorators import err_catcher_plugin as err_catcher


logger = logging.getLogger(__name__)

PLUGIN_NAME = "ProjectManagement"
PRISM_MANAGER = "Prism"
# Where the Prism manager keeps a task's status in its info file.
STATUS_FIELD = "status"

# What the Prism manager uses when a project hasn't edited its list (as its
# getDefaultStatus() returns it): task statuses, then product/media ones.
DEFAULT_STATUSES = [
    {"name": "Waiting to Start", "abbreviation": "wtg", "icon": "", "color": [200, 200, 200],
     "tasks": True, "products": False, "media": False},
    {"name": "In Progress", "abbreviation": "ip", "icon": "", "color": [200, 150, 100],
     "tasks": True, "products": False, "media": False},
    {"name": "Finished", "abbreviation": "fin", "icon": "", "color": [50, 200, 50],
     "tasks": True, "products": False, "media": False},
    {"name": "To Review", "abbreviation": "rev", "icon": "wave", "color": [200, 200, 100],
     "tasks": False, "products": True, "media": True},
    {"name": "Approved", "abbreviation": "apr", "icon": "", "color": [50, 200, 50],
     "tasks": False, "products": True, "media": True},
]

# Status names (or abbreviations) that mean "done" or "no longer needed"
# unless the project picks its own in the tracker's settings. Compared by
# statusKey(), so case, spaces and hyphens don't matter.
DONE_KEYS = ("finished", "fin", "completed", "complete", "done", "approved",
             "apr", "final", "delivered")
SKIP_KEYS = ("notneeded", "omit", "omitted", "cancelled", "canceled",
             "skip", "skipped", "na")

# The tracker's own statuses (by statusKey) and the Prism statuses they
# correspond to, best match first. Used when copying statuses between the
# two (see trackerToPrism / prismToTracker).
EQUIVALENTS = [
    ("notstarted", ("notstarted", "waitingtostart", "wtg", "todo", "ready", "pending")),
    ("inprogress", ("inprogress", "ip", "wip")),
    ("inreview", ("inreview", "review", "toreview", "rev", "ir", "pendingreview")),
    ("completed", ("completed", "complete", "finished", "fin", "done", "approved", "final")),
    ("notneeded", ("notneeded", "nn", "omit", "omitted", "cancelled", "canceled", "skip", "na")),
]


def statusKey(text):
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def hexColor(value, fallback="#6b7280"):
    """Prism's [r, g, b] (or a "#rrggbb" string) as "#rrggbb"."""
    if isinstance(value, str) and value.startswith("#"):
        return value
    try:
        r, g, b = [max(0, min(255, int(c))) for c in list(value)[:3]]
        return "#%02x%02x%02x" % (r, g, b)
    except Exception:
        return fallback


class PrismStatus(object):
    """The task statuses of the project's Project Management manager."""

    def __init__(self, core, tasks):
        self.core = core
        self.tasks = tasks
        self.manager = ""
        self.entries = []
        # The project's Prism status list, kept even while it uses no
        # manager: a task's status left by the Prism manager is stored by
        # abbreviation and is named through this list (see leftByPrism).
        self.prismEntries = []
        self.doneNames = None
        self.skipNames = None
        # True when the last reload couldn't read the project settings; the
        # manager and statuses read before are kept meanwhile.
        self.readFailed = False

    # ===================================================================
    #  State
    # ===================================================================
    @err_catcher(name=__name__)
    def reload(self, doneNames=None, skipNames=None):
        """Re-read the project's manager and task statuses.

        doneNames / skipNames: the statuses the project chose (in the
        tracker's settings) to count as done / not needed, or None to go by
        name (see DONE_KEYS / SKIP_KEYS).
        """
        self.doneNames = doneNames if isinstance(doneNames, list) else None
        self.skipNames = skipNames if isinstance(skipNames, list) else None
        settings = self.projectSettings()
        if settings is None:
            # Unreadable for a moment (being saved on a network share, say):
            # not the same as "no manager", which would switch every task
            # row to the tracker's own statuses until the next read.
            self.readFailed = True
            logger.warning("Production Tracker: could not read the project settings")
            return
        self.readFailed = False
        manager = settings.get("manager")
        self.manager = manager if isinstance(manager, str) and manager not in ("", "None") else ""
        self.entries = self.loadEntries(settings) if self.manager else []
        self.prismEntries = self.entriesFrom(self.prismList(settings))

    def projectSettings(self):
        """The project's Project Management settings, read from disk ({}
        when it has none), or None when the project settings can't be
        read."""
        try:
            data = self.core.getConfig(config="project", allowCache=False)
        except Exception:
            data = None
        if not isinstance(data, dict):
            return None
        settings = data.get("prjManagement")
        return settings if isinstance(settings, dict) else {}

    def loadEntries(self, settings):
        """[{"name", "abbreviation", "color"}] for tasks, in Prism's order."""
        raw = None
        manager = self.usableManager()
        if manager is not None and not self.isPrism():
            try:
                raw = self.plugin().getTaskStatusList()
            except Exception as e:
                logger.warning("Production Tracker: could not get the %s statuses: %s", self.manager, e)
        if raw is None and self.isPrism():
            raw = self.prismList(settings)
        return self.entriesFrom(raw)

    @staticmethod
    def prismList(settings):
        """The Prism manager's task statuses, as the project settings hold
        them (Prism's defaults when the project hasn't edited them)."""
        raw = settings.get("prism_status")
        if not isinstance(raw, list):
            raw = DEFAULT_STATUSES
        # The list also holds product and media statuses.
        return [s for s in raw if isinstance(s, dict) and s.get("tasks")]

    @staticmethod
    def entriesFrom(raw):
        entries = []
        seen = set()
        for status in raw or []:
            if not isinstance(status, dict):
                continue
            name = status.get("name")
            if not isinstance(name, str) or not name.strip() or name in seen:
                continue
            seen.add(name)
            entries.append(
                {
                    "name": name,
                    "abbreviation": status.get("abbreviation") or "",
                    "color": hexColor(status.get("color")),
                }
            )
        return entries

    def active(self):
        """True when task statuses come from Project Management."""
        return bool(self.manager)

    def isPrism(self):
        return self.manager == PRISM_MANAGER

    def plugin(self):
        try:
            return self.core.getPlugin(PLUGIN_NAME)
        except Exception:
            return None

    def usableManager(self):
        """The plugin's manager object for this project, or None when the
        plugin isn't loaded here or is set to a different manager."""
        plugin = self.plugin()
        manager = getattr(plugin, "curManager", None) if plugin else None
        if manager is None or getattr(manager, "name", None) != self.manager:
            return None
        return manager

    def readOnly(self):
        """True when the statuses are on a server this machine can't reach
        (a manager other than Prism, without the plugin)."""
        return self.active() and not self.isPrism() and self.usableManager() is None

    def description(self):
        """Where task statuses come from, for tooltips and messages."""
        if not self.active():
            return "the tracker's own task statuses"
        if self.readOnly():
            return (
                "%s, through Prism's Project Management plugin, which isn't "
                "loaded on this machine" % self.manager
            )
        return "Prism's Project Management (%s)" % self.manager

    # ===================================================================
    #  Status names
    # ===================================================================
    def names(self):
        return [e["name"] for e in self.entries]

    def entry(self, name):
        for e in self.entries:
            if e["name"] == name:
                return e
        return None

    def nameOf(self, value, entries=None):
        """The status name for a stored value: a name, or an abbreviation as
        Prism writes it ("fin" -> "Finished"). Matched exactly first, then
        ignoring case, spaces and punctuation. A value no status has is
        returned unchanged (the row shows it as it is).

        entries: the list to look in (the current statuses by default)."""
        if not isinstance(value, str) or not value:
            return value
        entries = self.entries if entries is None else entries
        for e in entries:
            if e["name"] == value:
                return e["name"]
        for e in entries:
            if e["abbreviation"] and e["abbreviation"] == value:
                return e["name"]
        wanted = statusKey(value)
        if wanted:
            for e in entries:
                if statusKey(e["name"]) == wanted:
                    return e["name"]
            for e in entries:
                if e["abbreviation"] and statusKey(e["abbreviation"]) == wanted:
                    return e["name"]
        return value

    def leftByPrism(self, value):
        """(name, abbreviation) of a status the Prism manager left in a
        task's info file, for a project that no longer uses a manager."""
        name = self.nameOf(value, self.prismEntries)
        for e in self.prismEntries:
            if e["name"] == name:
                return name, e["abbreviation"]
        return name, ""

    def storedValue(self, name):
        """What the Prism manager keeps in a task's info file for a status:
        its abbreviation (the name when the status has none, or for a
        value no status has)."""
        e = self.entry(self.nameOf(name))
        if e and e["abbreviation"]:
            return e["abbreviation"]
        return name

    def color(self, name, fallback="#6b7280"):
        e = self.entry(name)
        return e["color"] if e else fallback

    def keysOf(self, name):
        e = self.entry(name)
        keys = set([statusKey(name)])
        if e and e["abbreviation"]:
            keys.add(statusKey(e["abbreviation"]))
        return keys

    def isDone(self, name):
        if not name:
            return False
        if self.doneNames is not None:
            return name in self.doneNames
        return bool(self.keysOf(name) & set(DONE_KEYS))

    def isSkipped(self, name):
        if not name:
            return False
        if self.skipNames is not None:
            return name in self.skipNames
        return bool(self.keysOf(name) & set(SKIP_KEYS))

    # ===================================================================
    #  Mapping to and from the tracker's own statuses
    # ===================================================================
    def trackerToPrism(self, trackerStatus):
        """The project's status matching one of the tracker's, or None."""
        wanted = statusKey(trackerStatus)
        for key, candidates in EQUIVALENTS:
            if key != wanted:
                continue
            for candidate in candidates:
                for e in self.entries:
                    if candidate in self.keysOf(e["name"]):
                        return e["name"]
            return None
        # Not one of the tracker's five: the same name, if the project has it.
        for e in self.entries:
            if wanted in self.keysOf(e["name"]):
                return e["name"]
        return None

    @staticmethod
    def prismToTracker(prismStatus, trackerStatuses, abbreviation=""):
        """The tracker status matching a Prism one (the name itself when
        none does; the tracker keeps unknown statuses as they are)."""
        keys = set([statusKey(prismStatus), statusKey(abbreviation)]) - set([""])
        byKey = dict((statusKey(s), s) for s in trackerStatuses)
        for key, candidates in EQUIVALENTS:
            if keys & set(candidates) and key in byKey:
                return byKey[key]
        return prismStatus

    @err_catcher(name=__name__)
    def addStatuses(self, statuses):
        """Add task statuses to the project's Prism status list.

        statuses: [(name, hexColor)]. Only for the Prism manager; the list
        lives in the project settings, where Project Settings -> Project
        Management edits it too.
        """
        settings = self.projectSettings()
        if settings is None:
            raise RuntimeError("the project settings could not be read")
        current = settings.get("prism_status")
        if not isinstance(current, list):
            # Written out in full, product and media statuses included, or
            # the project would lose Prism's defaults for those.
            current = [dict(s) for s in DEFAULT_STATUSES]
        taken = set(statusKey(s.get("abbreviation")) for s in current if isinstance(s, dict))
        names = set(s.get("name") for s in current if isinstance(s, dict))
        for name, color in statuses:
            if name in names:
                continue
            abbreviation = "".join(w[0] for w in name.split() if w).lower() or name[:3].lower()
            base, n = abbreviation, 2
            while statusKey(abbreviation) in taken:
                abbreviation = "%s%d" % (base, n)
                n += 1
            taken.add(statusKey(abbreviation))
            c = color.lstrip("#")
            current.append(
                {
                    "name": name,
                    "abbreviation": abbreviation,
                    "icon": "",
                    "color": [int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)],
                    "tasks": True,
                    "products": False,
                    "media": False,
                }
            )
        self.core.setConfig("prjManagement", "prism_status", current, config="project")
        self.reload(self.doneNames, self.skipNames)
        self.clearIconCache()

    def clearIconCache(self):
        """Drop the plugin's per-status icons. It keeps the icon it drew for
        each status name, and a status it saw before the name was in the
        project's list keeps its "?" icon for the rest of the session."""
        cache = getattr(self.plugin(), "statusIconCache", None)
        if isinstance(cache, dict):
            cache.clear()

    # ===================================================================
    #  Reading and writing a task's status
    # ===================================================================
    def read(self, entity, department, task, data, strict=False):
        """A task's status ("" when it has none, or it can't be read here).

        data: the task's info file, already read fresh by the caller. The
        Prism manager's status is in there; Prism's own getTaskStatus reads
        it from its cache, which can be older than the file.

        strict: raise when a manager's status can't be read, instead of
        returning "" (for saving, where "" would read as a cleared status).
        """
        if self.isPrism():
            value = (data or {}).get(STATUS_FIELD)
            # Prism stores the abbreviation; older tracker saves the name.
            return self.nameOf(value) if isinstance(value, str) else ""
        manager = self.usableManager()
        if manager is None:
            if strict and self.active():
                raise RuntimeError("%s statuses can't be reached from this machine" % self.manager)
            return ""
        try:
            value = manager.getTaskStatus(entity, department, task)
        except Exception as e:
            if strict:
                raise RuntimeError("could not read the %s status: %s" % (self.manager, e))
            logger.warning("Production Tracker: could not read a %s status: %s", self.manager, e)
            return ""
        return value if isinstance(value, str) else ""

    def write(self, entity, department, task, status):
        """Set a task's status. Raises if it didn't take.

        Deliberately not wrapped in err_catcher, which would swallow the
        error and make a failed write look like a saved one.
        """
        manager = self.usableManager()
        if self.isPrism():
            # The info file holds the abbreviation, as Prism's own status
            # menu writes it; a name there shows as "?" in Prism.
            value = self.storedValue(status) if status else status
            if manager is None or not status:
                # Without the plugin (or to clear it), the status is written
                # to the info file the way the plugin writes it; this write
                # is checked by TrackerTasks itself.
                self.tasks.write(entity, department, task, {STATUS_FIELD: value})
                return
            path = self.tasks.dataPath(entity, department, task) or ""
            if not os.path.isdir(os.path.dirname(path)):
                raise RuntimeError("the task no longer exists in Prism")
            # Raises if the file is there but unreadable: Prism's own save
            # would then replace it with just the status.
            self.tasks.readFresh(entity, department, task)
            manager.setTaskStatus(entity, department, task, value)
            stored = self.tasks.readFresh(entity, department, task).get(STATUS_FIELD)
            if stored != value:
                try:
                    self.core.configs.clearCache(self.tasks.dataPath(entity, department, task))
                except Exception:
                    pass
                path = self.tasks.dataPath(entity, department, task) or ""
                raise RuntimeError(
                    "the status did not reach %s (is the file in use?); it will "
                    "be retried" % (os.path.basename(path) or "the task's info file")
                )
            return

        if manager is None:
            raise RuntimeError(
                "statuses are kept in %s, which needs Prism's Project "
                "Management plugin on this machine" % self.manager
            )
        if not status:
            raise RuntimeError("a %s status can't be cleared" % self.manager)
        if manager.setTaskStatus(entity, department, task, status) is False:
            raise RuntimeError("%s did not accept the status '%s'" % (self.manager, status))
