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
# Guards around Prism's own writers of the shot/asset info files, installed
# once per Prism process that loads the plugin. Each of them wrote an older
# copy of the data back over newer tracker values.
#
# 1. The "Meta Data" table in Prism's Edit Shot and asset dialogs. It reads
#    an entity's metadata once, when the dialog opens, turns every value
#    into text, and on save replaces the entity's whole metadata block with
#    what it shows. The tracker's fields live in that block, so a dialog left
#    open while the tracker saved put the old status/description back, and
#    its save turned the structured comment and FX-layer values into
#    strings. Guarded, the save re-reads the metadata and only applies the
#    rows the user actually changed in the dialog; every other key keeps its
#    current stored value and type.
#
# 2. entities.setAssetDescription. Prism reads the asset info file from its
#    in-memory cache (which another Prism process may have read long ago)
#    and merges that whole copy over the file, reverting every other
#    asset's tracker fields. Guarded, only this asset's description is
#    written, against the file as it is on disk.
#
# 3. The asset Edit dialog's description box. It is filled when the dialog
#    opens and written back on OK whether or not it was edited. Guarded, it
#    opens with the description on disk and is only written if changed.
#
# 4. entities.renameShot / renameSequence / renameEpisode. They rewrite
#    whole sections of the shot info file from Prism's cache. Guarded, the
#    cache is cleared first so they work from the file as it is now.
#
####################################################


import copy
import logging
import os


logger = logging.getLogger(__name__)

GUARD_ATTR = "_productionTrackerGuarded"

# Set while Prism's asset Edit dialog is open (see guardAssetDialog):
# {"asset": name, "shown": description shown when it opened}.
_assetDialog = {"active": False}


def install(core, plugin=None):
    """Install every guard, skipping any that can't be (never raises)."""
    for guard in (
        guardMetaDataWidget,
        guardSetAssetDescription,
        guardAssetDialog,
        guardShotRenames,
    ):
        try:
            guard(core, plugin)
        except Exception as e:
            logger.warning("Production Tracker: could not install %s: %s", guard.__name__, e)


# ------------------------------------------------------------ helpers ------
def infoPath(core, entity):
    """Path of the shot or asset info file an entity's metadata lives in."""
    config = "assetinfo" if entity.get("type") == "asset" else "shotinfo"
    return core.configs.getConfigPath(config=config)


def assetInfoPath(core):
    """The asset info file, located the way Prism's description functions
    locate it."""
    return os.path.join(
        core.projects.getPipelineFolder(),
        "Assetinfo",
        "assetInfo" + core.configs.getProjectExtension(),
    )


def readFreshFile(core, path):
    """A whole info file read from disk ({} if it doesn't exist), or None
    if it exists but can't be read."""
    if not path:
        return None
    if not os.path.exists(path):
        return {}
    data = core.getConfig(configPath=path, allowCache=False)
    return data if isinstance(data, dict) else None


def readFreshMetaData(core, entity):
    """An entity's metadata block read straight from disk, or None if the
    file can't be read (a deep copy - it is safe to modify)."""
    data = readFreshFile(core, infoPath(core, entity))
    if data is None:
        return None

    if entity.get("type") == "asset":
        name = core.entities.getAssetNameFromPath(entity.get("asset_path", ""))
        assets = data.get("assets") if isinstance(data.get("assets"), dict) else {}
        entry = assets.get(name) if isinstance(assets.get(name), dict) else {}
        block = entry.get("metadata")
    else:
        block = (core.entities.getShotInfoEntityData(data, entity) or {}).get("metadata")
    return copy.deepcopy(block) if isinstance(block, dict) else {}


def mergeDialogMetaData(shownAtLoad, inDialog, stored):
    """What a metadata dialog's save should write.

    shownAtLoad: {key: (text, show)} as the dialog's rows read when it
    opened. inDialog: {key: {"value": text, "show": bool}} as they read now.
    stored: the metadata on disk at this moment.

    Rows the user left alone keep the stored entry (with its real type and
    any change made since the dialog opened). Rows added or edited in the
    dialog are written as typed, and rows removed in it are removed.
    """
    out = copy.deepcopy(stored)
    for key, entry in inDialog.items():
        if shownAtLoad.get(key) == (entry.get("value"), bool(entry.get("show"))):
            continue
        out[key] = entry
    for key in shownAtLoad:
        if key not in inDialog:
            out.pop(key, None)
    return out


def plainText(text):
    """Text as a QTextEdit hands it back (it normalises line breaks and
    non-breaking spaces), so text shown in one can be compared with what
    it returns."""
    try:
        from qtpy.QtGui import QTextDocument

        doc = QTextDocument()
        doc.setPlainText(text or "")
        return doc.toPlainText()
    except Exception:
        return text or ""


def popup(core, text):
    logger.warning("Production Tracker: %s", text)
    try:
        core.popup(text)
    except Exception:
        pass


# ---------------------------------------------------- metadata dialog -----
def guardMetaDataWidget(core, plugin=None):
    import MetaDataWidget

    cls = MetaDataWidget.MetaDataWidget
    if getattr(cls, GUARD_ATTR, False):
        return

    origLoad = cls.loadMetaData
    origSave = cls.save

    def shownRows(widget):
        """{key: (text, show)} for the rows the widget shows right now."""
        rows = {}
        for key, entry in widget.getMetaData().items():
            rows[key] = (entry.get("value"), bool(entry.get("show")))
        return rows

    def loadMetaData(self, entityData):
        result = origLoad(self, entityData)
        try:
            self._trackerShownAtLoad = shownRows(self)
        except Exception:
            self._trackerShownAtLoad = None
        return result

    def save(self, entityData=None):
        if not entityData:
            entityData = self.entityData
        if not entityData or entityData.get("type") not in ("shot", "asset"):
            return origSave(self, entityData)

        try:
            stored = readFreshMetaData(self.core, entityData)
        except Exception:
            stored = None
        if stored is None:
            # Prism's own save would write the dialog's text-only copy over
            # the whole block (or, if its read fails too, the whole file with
            # just this entity), so nothing is written.
            popup(
                self.core,
                "The metadata was not saved: the shot/asset info file could "
                "not be read. Please try again.",
            )
            return

        shownAtLoad = getattr(self, "_trackerShownAtLoad", None) or {}
        data = mergeDialogMetaData(shownAtLoad, self.getMetaData(), stored)
        self.core.entities.setMetaData(entityData, data)

    cls.loadMetaData = loadMetaData
    cls.save = save
    setattr(cls, GUARD_ATTR, True)


# ---------------------------------------------- asset description --------
def guardSetAssetDescription(core, plugin=None):
    entities = core.entities
    orig = entities.setAssetDescription
    if getattr(orig, GUARD_ATTR, False):
        return

    def setAssetDescription(assetName, description):
        if (
            _assetDialog["active"]
            and _assetDialog.get("asset") == assetName
            and plainText(_assetDialog.get("shown")) == plainText(description)
        ):
            # Prism's asset dialog writes its description box back on OK
            # even when it wasn't touched; skip that so it can't put back the
            # text it showed when it opened.
            return

        assetFile = assetInfoPath(core)
        data = readFreshFile(core, assetFile)
        if data is None:
            # Writing now would replace the file with just this entry.
            popup(
                core,
                "The description of '%s' was not saved: the asset info file "
                "could not be read. Please try again." % assetName,
            )
            return
        entry = data.get(assetName)
        if not isinstance(entry, dict):
            entry = data[assetName] = {}
        entry["description"] = description
        core.setConfig(data=data, configPath=assetFile, updateNestedData=False)

    setattr(setAssetDescription, GUARD_ATTR, True)
    core.plugins.monkeyPatch(orig, setAssetDescription, plugin, quiet=True)


def guardAssetDialog(core, plugin=None):
    """Prism's asset Edit dialog (SceneBrowser.editAsset): open it with the
    description on disk, and remember it so an untouched description box
    isn't written back (see guardSetAssetDescription)."""
    import SceneBrowser

    cls = SceneBrowser.SceneBrowser
    if getattr(cls, GUARD_ATTR, False):
        return
    origEdit = cls.editAsset

    def editAsset(self, *args, **kwargs):
        entities = self.core.entities
        origGet = entities.getAssetDescription
        # Whether getAssetDescription is already replaced on the instance
        # (by another plugin), so that is what gets put back afterwards.
        hadOwn = "getAssetDescription" in vars(entities)

        def getAssetDescription(assetName, projectPath=None):
            # The dialog asks first: answer from disk and remember the text.
            if projectPath is None and not _assetDialog.get("asset"):
                data = readFreshFile(self.core, assetInfoPath(self.core))
                if data is not None:
                    entry = data.get(assetName)
                    text = entry.get("description", "") if isinstance(entry, dict) else ""
                    _assetDialog.update({"asset": assetName, "shown": text or ""})
                    return text
            if projectPath is None:
                return origGet(assetName)
            return origGet(assetName, projectPath)

        _assetDialog.clear()
        _assetDialog["active"] = True
        entities.getAssetDescription = getAssetDescription
        try:
            return origEdit(self, *args, **kwargs)
        finally:
            if entities.getAssetDescription is getAssetDescription:
                if hadOwn:
                    entities.getAssetDescription = origGet
                else:
                    del entities.getAssetDescription
            _assetDialog.clear()
            _assetDialog["active"] = False

    cls.editAsset = editAsset
    setattr(cls, GUARD_ATTR, True)


# ------------------------------------------------------- shot renames ----
def guardShotRenames(core, plugin=None):
    entities = core.entities
    for name in ("renameShot", "renameSequence", "renameEpisode"):
        try:
            orig = getattr(entities, name)
            if getattr(orig, GUARD_ATTR, False):
                continue

            def wrapper(*args, _orig=orig, **kwargs):
                # These rewrite whole sections of the shot info file from
                # Prism's cache; make that cache the file as it is now.
                try:
                    core.configs.clearCache(path=core.configs.getConfigPath(config="shotinfo"))
                except Exception:
                    pass
                return _orig(*args, **kwargs)

            wrapper.__name__ = name
            setattr(wrapper, GUARD_ATTR, True)
            core.plugins.monkeyPatch(orig, wrapper, plugin, quiet=True)
        except Exception as e:
            logger.warning("Production Tracker: could not guard %s: %s", name, e)
