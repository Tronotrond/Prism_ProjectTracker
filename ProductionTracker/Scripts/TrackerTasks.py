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
# Production Tracker - Prism departments and tasks
#
# Backs the "Use Prism departments and tasks" tracker mode: instead of one
# row per shot, the tracker shows each shot's real departments and the tasks
# inside them, matching what the Project Browser's Scenefiles tab shows.
#
# Both the structure and the storage are Prism's own:
#
#   departments  core.entities.getSteps()      -> abbreviations on disk ("lgt")
#   tasks        core.entities.getCategories() -> task folders ("Lighting")
#   task data    core.entities.getTaskData()   -> <task folder>/info.json
#
# So a task's tracker fields live next to the task itself and travel with the
# project, exactly like the per-entity metadata used in the default mode.
#
# Departments and tasks only exist on disk once something has been created in
# them, so nothing is invented here: the tracker shows what is really there,
# and createTask() makes a real department/task folder through Prism when the
# user asks for one.
#
####################################################


import os

from PrismUtils.Decorators import err_catcher_plugin as err_catcher


class TrackerTasks(object):
    """Read and write tracker fields on Prism departments/tasks."""

    def __init__(self, core):
        self.core = core

    # ===================================================================
    #  Structure
    # ===================================================================
    @err_catcher(name=__name__)
    def longName(self, entity, abbreviation):
        """'lgt' -> 'Lighting'. Falls back to the abbreviation itself."""
        try:
            name = self.core.entities.getLongDepartmentName(
                entity.get("type") or "shot", abbreviation
            )
        except Exception:
            name = None
        return name or abbreviation

    @err_catcher(name=__name__)
    def departments(self, entity):
        """[(abbreviation, longName)] present on disk, in Prism's own order."""
        try:
            abbreviations = self.core.entities.getSteps(entity) or []
        except Exception:
            return []
        return [(a, self.longName(entity, a)) for a in abbreviations]

    @err_catcher(name=__name__)
    def tasks(self, entity, department):
        """Task names inside a department, in Prism's own order."""
        try:
            return self.core.entities.getCategories(entity, department) or []
        except Exception:
            return []

    @err_catcher(name=__name__)
    def configuredDepartments(self, entityType):
        """[(abbreviation, longName)] a project allows, from Project Settings.

        Used to offer departments when adding a task, including ones nobody
        has created a folder for yet.
        """
        try:
            if entityType == "asset":
                departments = self.core.projects.getAssetDepartments() or []
            else:
                departments = self.core.projects.getShotDepartments() or []
        except Exception:
            return []

        out = []
        for dept in departments:
            if not isinstance(dept, dict):
                continue
            abbreviation = (dept.get("abbreviation") or "").strip()
            name = (dept.get("name") or "").strip()
            if not abbreviation and not name:
                continue
            out.append((abbreviation or name, name or abbreviation))
        return out

    @err_catcher(name=__name__)
    def defaultTasks(self, entityType, department):
        """Task names Prism suggests for a department (may be empty)."""
        try:
            return self.core.entities.getDefaultTasksForDepartment(
                entityType, department
            ) or []
        except Exception:
            return []

    @err_catcher(name=__name__)
    def departmentsFolder(self, entity):
        """Folder holding an entity's department folders (or '')."""
        try:
            return self.core.getEntityPath(entity=entity, reqEntity="step") or ""
        except Exception:
            return ""

    @err_catcher(name=__name__)
    def departmentFolder(self, entity, department):
        """Folder holding a department's task folders (or '')."""
        try:
            return self.core.getEntityPath(entity=entity, step=department) or ""
        except Exception:
            return ""

    @err_catcher(name=__name__)
    def taskFolder(self, entity, department, task):
        """Folder on disk holding a task (or '')."""
        try:
            return self.core.getEntityPath(
                entity=entity, step=department, category=task
            ) or ""
        except Exception:
            return ""

    @err_catcher(name=__name__)
    def createTask(self, entity, department, task):
        """Create a real department/task folder. Returns an error string or ''."""
        department = (department or "").strip()
        task = (task or "").strip()
        if not department:
            return "No department was given."
        if not task:
            return "No task name was given."

        try:
            existing = [d[0] for d in self.departments(entity)]
            if department not in existing:
                # createCat=False: we create the task ourselves just below, so
                # Prism should not also drop its own default task in there.
                self.core.entities.createDepartment(
                    department, entity, createCat=False
                )
            if task in self.tasks(entity, department):
                return "'%s' already exists in that department." % task
            self.core.entities.createCategory(entity, department, task)
        except Exception as e:
            return str(e)

        if not os.path.exists(self.taskFolder(entity, department, task)):
            return "Prism did not create the task folder."
        return ""

    # ===================================================================
    #  Task data (<task folder>/info.json)
    # ===================================================================
    @err_catcher(name=__name__)
    def dataPath(self, entity, department, task):
        try:
            return self.core.entities.getTaskDataPath(entity, department, task)
        except Exception:
            return ""

    @err_catcher(name=__name__)
    def read(self, entity, department, task):
        """Everything stored for a task ({} when the file does not exist)."""
        try:
            data = self.core.entities.getTaskData(entity, department, task)
        except Exception:
            data = None
        return data if isinstance(data, dict) else {}

    @err_catcher(name=__name__)
    def write(self, entity, department, task, values):
        """Merge `values` into the task's info file.

        Read-modify-write against the file as it is right now, so keys written
        by Prism (or by another artist since this window opened) survive, and
        only the fields handed in here are touched.
        """
        path = self.dataPath(entity, department, task)
        if not path:
            raise RuntimeError("Could not resolve the task data path.")

        folder = os.path.dirname(path)
        if folder and not os.path.exists(folder):
            os.makedirs(folder)

        try:
            self.core.configs.clearCache(path)
        except Exception:
            pass

        current = {}
        if os.path.exists(path):
            try:
                loaded = self.core.getConfig(configPath=path)
                if isinstance(loaded, dict):
                    current = loaded
            except Exception:
                current = {}

        current.update(values or {})
        self.core.setConfig(data=current, configPath=path, updateNestedData=False)
        return current
