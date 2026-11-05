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
# Where the tracker keeps the data of shots in projects with episodes.
#
# Prism keeps a project's shot info in shots -> sequence -> shot, with no
# episode in it, unless the project was made with Prism 2.1.4 or later (the
# project's own Prism version decides, not the version running). So in an
# older episode project ep203/008/050 and ep208/008/050 share ONE metadata
# block: a new shot showed another episode's status, assignee and
# description, and editing one changed the other.
#
# The tracker now keeps every episode shot's data apart:
#
# - Projects made before Prism 2.1.4: the tracker keeps each episode shot's
#   block in its own file, in the tracker's config folder, laid out the way
#   Prism 2.1.4 lays out shot info: shots -> episode -> sequence -> shot. It
#   never writes an episode shot's data to Prism's shot info file there.
# - Projects made with Prism 2.1.4 or later: Prism keeps shots per episode
#   itself, and the tracker writes there as before. The tracker's file is
#   still read, after Prism's entry, so data saved before a project upgrade
#   isn't lost.
#
# A shot that has no entry of its own yet may still have one in the old,
# shared place (shots -> sequence -> shot). That one is only used when no
# other episode has a shot of the same sequence and name, so it can't be
# another episode's; otherwise the shot starts blank. Prism's own lookup
# (getShotInfoEntityData, getMetaData) falls back to the shared entry
# whatever the other episodes have, so the tracker doesn't use it for
# episode shots.
#
# Which episodes "have" a name is decided by the shot folders and by the
# per-episode frame ranges in the shot info file (which also remember a
# shot that was deleted or omitted, and so may have written the shared
# entry too). Before anything changes which episodes have a name - a shot
# created, renamed or deleted - the shots that have that name get an entry
# of their own holding what they show at that moment (see pinShots), so
# nothing they show changes. A shot Prism creates gets an empty entry of its
# own straight away (see markCreated), so it never starts with someone
# else's data. A value Prism or a script sets on such a shot afterwards
# (setMetaData) is added to that entry (see beforeSetMetaData).
#
# Nothing is written by just looking: building rows and syncing only read.
#
# Only the tracker writes its file, and its writers take turns (see
# StoreLock), so two Prism sessions saving at once can't lose each other's
# change.
#
####################################################


import contextlib
import copy
import errno
import inspect
import logging
import os
import socket
import threading
import time

import TrackerPrismGuards


logger = logging.getLogger(__name__)

# The tracker's file for episode shots, in its project config folder (next
# to ProductionTracker.json), with the project's config extension.
FILE_BASE_NAME = "ProductionTracker_EpisodeShots"

# Shots Prism is creating right now: {(episode, sequence, shot): {"metaData":
# the metadata it was given, "new": its folder didn't exist yet, "marked":
# markCreated has recorded it}}. Set by beforeCreate (the createShot guard)
# before Prism creates the shot, read by markCreated from Prism's
# onShotCreated.
_creating = {}

# While above zero the tracker itself is renaming a shot and sees to its
# entries (see handledByCaller), so the guard around Prism's rename doesn't.
_callerHandles = [0]

# The writers of the tracker's file take turns through a lock file of their
# own beside it - not Prism's "<file>.lock", which Prism waits for before
# every read of the file. A lock left behind (a crash) is taken over once it
# has stayed the same for LOCK_STALE seconds, timed on this machine's clock,
# so clocks that differ between machines don't matter.
LOCK_SUFFIX = ".trackerlock"
LOCK_TIMEOUT = 30.0
LOCK_STALE = 20.0
_heldLocks = {}


# ------------------------------------------------------------ helpers ------
def usesEpisodes(core):
    try:
        return bool(core.projects.getUseEpisodes())
    except Exception:
        return False


def applies(core, entity):
    """True for a shot of a project with episodes, with its episode known:
    the shots whose data this module looks after."""
    entity = entity or {}
    return (
        entity.get("type", "shot") == "shot"
        and bool(entity.get("episode"))
        and bool(entity.get("sequence"))
        and bool(entity.get("shot"))
        and usesEpisodes(core)
    )


def keepsOwnFile(core):
    """True when the tracker keeps episode shots in its own file: a project
    with episodes made before Prism 2.1.4, where Prism has no per-episode
    place for them."""
    return usesEpisodes(core) and not TrackerPrismGuards.useEpisodicShotInfo(core)


def shotKey(entity):
    return (entity.get("episode"), entity.get("sequence"), entity.get("shot"))


def shotEntity(episode, sequence, shot):
    return {"type": "shot", "episode": episode, "sequence": sequence, "shot": shot}


def filePath(core):
    """The tracker's episode shot file, or "" without a project."""
    if not getattr(core, "projectPath", None):
        return ""
    try:
        folder = core.projects.getConfigFolder()
    except Exception:
        folder = ""
    if not folder:
        return ""
    try:
        extension = core.configs.getProjectExtension() or ".json"
    except Exception:
        extension = ".json"
    return os.path.join(folder, FILE_BASE_NAME + extension)


def shotInfoPath(core):
    return core.configs.getConfigPath(config="shotinfo")


def readFile(core, path):
    """A whole file read from disk rather than from Prism's caches ({} if it
    doesn't exist). Raises if it exists but can't be read, so a failed read
    is never taken for "nothing stored"."""
    if not path:
        raise RuntimeError("the tracker's data file could not be located")
    if not os.path.exists(path):
        return {}
    data = core.getConfig(configPath=path, allowCache=False)
    if not isinstance(data, dict):
        raise RuntimeError("could not read %s" % os.path.basename(path))
    return data


def readStore(core):
    return readFile(core, filePath(core))


def readShotInfo(core):
    return readFile(core, shotInfoPath(core))


def metaOf(entry):
    """A deep copy of an entry's metadata block ({} if none)."""
    block = entry.get("metadata") if isinstance(entry, dict) else None
    return copy.deepcopy(block) if isinstance(block, dict) else {}


def nested(data, keys):
    return TrackerPrismGuards.nestedEntry(data, keys) if isinstance(data, dict) else None


# --------------------------------------------------------- who has a name --
def rangeHolders(shotInfo):
    """{(sequence, shot): {episodes}} from the per-episode frame ranges in
    the shot info file (shotRanges -> episode -> sequence -> shot). They are
    kept for a shot that was deleted or omitted as well."""
    out = {}
    ranges = (shotInfo or {}).get("shotRanges")
    if not isinstance(ranges, dict):
        return out
    for episode, sequences in ranges.items():
        if not isinstance(sequences, dict):
            continue
        for sequence, shots in sequences.items():
            # A range stored without an episode is sequence -> {shot: [start,
            # end]}: its values are lists, so it is skipped here.
            if not isinstance(shots, dict):
                continue
            for shot in shots:
                out.setdefault((sequence, shot), set()).add(episode)
    return out


def takesKeyword(function, name):
    """True when function has a parameter called name (Prism's err_catcher
    keeps the signature). Asked rather than tried: a wrong keyword would
    raise inside Prism's err_catcher, which shows an error."""
    try:
        return name in inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False


def listIncludingOmitted(function, **kwargs):
    """function(**kwargs), with omitted entities included where it can be
    asked for: an omitted shot, or a shot in an omitted episode, still has
    its name (and may have written the shared entry)."""
    if takesKeyword(function, "includeOmitted"):
        kwargs["includeOmitted"] = True
    return function(**kwargs) or []


def episodeNames(core):
    """Every episode, omitted ones included."""
    try:
        episodes = listIncludingOmitted(core.entities.getEpisodes)
    except Exception:
        episodes = []
    return [e.get("episode") for e in episodes if isinstance(e, dict) and e.get("episode")]


def episodeShots(core, episodes=None):
    """{episode: [(sequence, shot)]} for the shots that exist now, omitted
    ones (and those of omitted episodes) included."""
    out = {}
    for episode in episodeNames(core) if episodes is None else episodes:
        try:
            shots = listIncludingOmitted(core.entities.getShots, episode=episode)
        except Exception:
            shots = []
        out[episode] = [
            (s.get("sequence"), s.get("shot")) for s in shots if isinstance(s, dict)
        ]
    return out


def shotNames(core):
    """{(sequence, shot): {episodes}} for every shot that exists now,
    omitted ones included."""
    out = {}
    for episode, shots in episodeShots(core).items():
        for sequence, shot in shots:
            if sequence and shot:
                out.setdefault((sequence, shot), set()).add(episode)
    return out


def folderHolders(core, sequence, shot, episodes=None):
    """The episodes that have a shot folder sequence/shot right now (looking
    in every episode, omitted ones included, unless `episodes` is given)."""
    out = set()
    for episode in episodeNames(core) if episodes is None else episodes:
        try:
            path = core.getEntityPath(entity=shotEntity(episode, sequence, shot))
        except Exception:
            path = ""
        if path and os.path.isdir(path):
            out.add(episode)
    return out


# ------------------------------------------------------------- reading ----
def prismOwnEntry(core, entity, shotInfo):
    """A shot's per-episode entry in Prism's shot info file, where the
    project has them (2.1.4 layout) and it holds a metadata block; else
    None. Prism 2.1.4 also makes such an entry just for a shot's connected
    entities (setConnectedEntities): that one isn't the shot's data, and
    taking it for it would hide what the tracker's file holds.
    """
    if keepsOwnFile(core):
        return None
    found = nested(shotInfo, ("shots",) + shotKey(entity))
    if isinstance(found, dict) and isinstance(found.get("metadata"), dict):
        return found
    return None


def ownEntry(core, entity, shotInfo, store):
    """A shot's own entry, or None when it has none yet.

    Prism's per-episode entry where the project has them (2.1.4 layout)
    and it holds metadata, else the tracker's file - which a 2.1.4-layout
    project still has from before an upgrade, or for a shot marked new by
    markCreated.
    """
    found = prismOwnEntry(core, entity, shotInfo)
    if found is not None:
        return found
    return nested(store, ("shots",) + shotKey(entity))


def sharedEntry(shotInfo, entity):
    """The shot's entry at shots -> sequence -> shot, where Prism before
    2.1.4 keeps the shots of every episode."""
    return nested(shotInfo, ("shots", entity.get("sequence"), entity.get("shot")))


def resolve(core, entity, shotInfo, store, holders):
    """(metadata, source) of an episode shot. metadata is a deep copy.

    holders: the episodes that have a shot of this sequence and name (see
    rangeHolders, folderHolders). source is "own" (its own entry), "shared"
    (the entry without an episode, used as no other episode has the name)
    or "" (nothing that is surely this shot's: it starts blank).
    """
    own = ownEntry(core, entity, shotInfo, store)
    if own is not None:
        return metaOf(own), "own"
    others = set(holders or ()) - set([entity.get("episode")])
    if not others:
        shared = sharedEntry(shotInfo, entity)
        if shared is not None:
            return metaOf(shared), "shared"
    return {}, ""


def holdersOf(entity, shotInfo, known=None):
    """Every episode known to have this shot's sequence and name: `known`
    (from the shot folders) and the per-episode frame ranges."""
    holders = set(known or ())
    holders |= rangeHolders(shotInfo).get((entity.get("sequence"), entity.get("shot")), set())
    return holders


def readMetaData(core, entity, known=None):
    """(metadata, source) of an episode shot, read fresh from disk.
    Raises if a file can't be read.

    known: episodes already known to have this name (the tracker's last
    rebuild). The shot folders of every episode are looked at too, so this
    decides the way the Reader and pinShots do.
    """
    shotInfo = readShotInfo(core)
    store = readStore(core)
    known = set(known or ()) | folderHolders(core, entity.get("sequence"), entity.get("shot"))
    return resolve(core, entity, shotInfo, store, holdersOf(entity, shotInfo, known))


class Reader(object):
    """Episode shots' data as read at one moment - each file read once - for
    building every row. Unreadable files read as empty; the tracker checks
    them before it rebuilds (see ProductionTrackerDlg.refresh)."""

    def __init__(self, core, shots=()):
        self.core = core
        try:
            self.shotInfo = readShotInfo(core)
        except Exception as e:
            logger.warning("Production Tracker: could not read the shot info file: %s", e)
            self.shotInfo = {}
        try:
            self.store = readStore(core)
        except Exception as e:
            logger.warning("Production Tracker: could not read the episode shot file: %s", e)
            self.store = {}
        # Which episodes have each name: every shot that exists (omitted
        # ones too, as readMetaData and pinShots see them) and the shots
        # given - `names` - and also the per-episode frame ranges.
        try:
            self.names = shotNames(core) if usesEpisodes(core) else {}
        except Exception as e:
            logger.warning("Production Tracker: could not list the shots: %s", e)
            self.names = {}
        for shot in shots or ():
            if shot.get("episode") and shot.get("sequence") and shot.get("shot"):
                self.names.setdefault(
                    (shot["sequence"], shot["shot"]), set()
                ).add(shot["episode"])
        self.holders = rangeHolders(self.shotInfo)
        for name, episodes in self.names.items():
            self.holders.setdefault(name, set()).update(episodes)

    def metaData(self, entity):
        holders = self.holders.get((entity.get("sequence"), entity.get("shot")), set())
        return resolve(self.core, entity, self.shotInfo, self.store, holders)[0]


# ------------------------------------------------------------- writing ----
def prune(data, keys):
    """Remove data[k1]...[kn] and any dict along the way left empty."""
    trail = [data]
    for key in keys[:-1]:
        level = trail[-1].get(key)
        if not isinstance(level, dict):
            return
        trail.append(level)
    trail[-1].pop(keys[-1], None)
    for level, key in zip(reversed(trail[:-1]), reversed(keys[:-1])):
        if level.get(key) == {}:
            level.pop(key)
        else:
            break


def lockOwner():
    try:
        return "%s %s" % (socket.gethostname(), os.getpid())
    except Exception:
        return str(os.getpid())


def lockState(path):
    """What a lock file looks like now, to tell whether it changes, or None
    when there is none."""
    try:
        info = os.stat(path)
        with open(path, "r") as f:
            owner = f.read(200)
    except (IOError, OSError):
        return None
    return (info.st_mtime, info.st_size, owner)


class StoreLock(object):
    """The tracker's lock on a file, held while its whole contents are read,
    changed and written back, so another session's write in between can't
    be lost. Raises RuntimeError when it can't be had within LOCK_TIMEOUT.
    Taking it again in the same thread while held is allowed."""

    def __init__(self, path):
        self.lockPath = path + LOCK_SUFFIX
        self.fileName = os.path.basename(path)
        self.key = (self.lockPath, threading.current_thread().ident)

    def __enter__(self):
        if _heldLocks.get(self.key):
            _heldLocks[self.key] += 1
            return self
        start = time.time()
        seen = None  # (how the lock file looked, when first seen like that)
        while True:
            try:
                handle = os.open(self.lockPath, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except OSError as e:
                # Windows answers EACCES for a lock file being removed.
                if e.errno not in (errno.EEXIST, errno.EACCES):
                    raise RuntimeError("could not lock %s: %s" % (self.fileName, e))
                now = time.time()
                state = lockState(self.lockPath)
                if state is not None:
                    if seen is None or seen[0] != state:
                        seen = (state, now)
                    elif now - seen[1] >= LOCK_STALE:
                        logger.warning(
                            "Production Tracker: taking over the lock on %s left by %s",
                            self.fileName, state[2] or "an unknown session",
                        )
                        try:
                            os.remove(self.lockPath)
                        except OSError:
                            pass
                        seen = None
                        continue
                if now - start >= LOCK_TIMEOUT:
                    who = (state[2] if state else "") or "another session"
                    raise RuntimeError(
                        "%s is being saved by someone else (%s)"
                        % (self.fileName, who)
                    )
                time.sleep(0.05)
                continue
            try:
                os.write(handle, lockOwner().encode("utf-8"))
            except OSError:
                pass
            finally:
                os.close(handle)
            _heldLocks[self.key] = 1
            return self

    def __exit__(self, *exc):
        count = _heldLocks.get(self.key, 0) - 1
        if count > 0:
            _heldLocks[self.key] = count
            return False
        _heldLocks.pop(self.key, None)
        try:
            os.remove(self.lockPath)
        except OSError as e:
            logger.warning(
                "Production Tracker: could not remove the lock on %s: %s", self.fileName, e
            )
        return False


def updateStore(core, changesFor):
    """Change entries of the tracker's episode shot file: one fresh read,
    one write, then a read back, all while holding the tracker's lock on
    the file (see StoreLock). Raises if the lock can't be had, the file
    can't be read or the change didn't reach it.

    changesFor: {(episode, sequence, shot): entry, or None to remove it},
    or a function given the file's data that returns one (called under the
    lock, so it decides on the newest data). Returns whether anything was
    written.
    """
    path = filePath(core)
    if not path:
        raise RuntimeError("the tracker's data file could not be located")
    folder = os.path.dirname(path)
    if folder and not os.path.exists(folder):
        os.makedirs(folder)

    with StoreLock(path):
        data = readFile(core, path)
        changes = changesFor(data) if callable(changesFor) else changesFor
        if not changes:
            return False
        for key, entry in changes.items():
            keys = ("shots",) + tuple(key)
            if entry is None:
                prune(data, keys)
            else:
                TrackerPrismGuards.nestedEntry(data, keys[:-1], create=True)[keys[-1]] = entry

        core.setConfig(data=data, configPath=path, updateNestedData=False)

        written = readFile(core, path)
        for key, entry in changes.items():
            if nested(written, ("shots",) + tuple(key)) != entry:
                try:
                    core.configs.clearCache(path)
                except Exception:
                    pass
                raise RuntimeError(
                    "the change did not reach %s (is the file in use?)" % os.path.basename(path)
                )
    return True


def fileStamp(path):
    """(modified time, size) of a file, or None, to tell whether it was
    written in between."""
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (getattr(info, "st_mtime_ns", info.st_mtime), info.st_size)


def readUnchanged(core, path, change):
    """Read a file Prism writes as well (so the tracker's lock can't keep
    others out), apply change(data) to it, and return the data once the
    file is seen unchanged from before the read to after the change - read
    again if someone wrote it meanwhile. That leaves only the moment of
    writing in which another write could be lost. Raises if it keeps
    changing."""
    for _attempt in range(3):
        stamp = fileStamp(path)
        data = readFile(core, path)
        change(data)
        if fileStamp(path) == stamp:
            return data
    raise RuntimeError(
        "%s kept changing while saving (someone else is saving)"
        % os.path.basename(path)
    )


def writeMetaData(core, entity, meta):
    """Store an episode shot's metadata block in its own entry and check it
    reached the disk (raises if not). The tracker's file for a project made
    before Prism 2.1.4, Prism's per-episode entry otherwise."""
    key = shotKey(entity)
    if keepsOwnFile(core):

        def changesFor(store):
            entry = nested(store, ("shots",) + key)
            entry = copy.deepcopy(entry) if isinstance(entry, dict) else {}
            entry["metadata"] = meta
            return {key: entry}

        updateStore(core, changesFor)
        return

    path = shotInfoPath(core)

    def change(data):
        TrackerPrismGuards.nestedEntry(data, ("shots",) + key, create=True)["metadata"] = meta

    data = readUnchanged(core, path, change)
    core.setConfig(data=data, configPath=path, updateNestedData=False)
    if nested(readFile(core, path), ("shots",) + key + ("metadata",)) != meta:
        try:
            core.configs.clearCache(path)
        except Exception:
            pass
        raise RuntimeError(
            "the change did not reach %s (is the file in use?)" % os.path.basename(path)
        )


def removeOwn(core, entity):
    """Remove a deleted shot's entry from the tracker's file (raises if it
    couldn't be)."""
    key = shotKey(entity)

    def changesFor(store):
        return {key: None} if nested(store, ("shots",) + key) is not None else {}

    updateStore(core, changesFor)


def pinShots(core, names, shotInfo=None):
    """Before shots named `names` ({(sequence, shot)}) are created, renamed
    or deleted: give every existing episode shot of those names that has no
    entry of its own one holding what it shows now (its shared entry, or
    blank), so the change can't make it show something else - another
    episode's data, or a deleted shot's.

    Only the tracker's file is written. Raises if it can't be.
    """
    if not usesEpisodes(core):
        return False
    names = set((seq, shot) for seq, shot in names or () if seq and shot)
    if not names:
        return False
    if shotInfo is None:
        shotInfo = readShotInfo(core)
    ranges = rangeHolders(shotInfo)
    episodes = episodeNames(core)
    existing = {}
    for seq, shot in names:
        existing[(seq, shot)] = folderHolders(core, seq, shot, episodes)

    def changesFor(store):
        changes = {}
        for (seq, shot), present in existing.items():
            holders = present | ranges.get((seq, shot), set())
            # Only shots that exist get an entry; a name kept only by a
            # deleted shot's frame range just counts as taken.
            for episode in present:
                entity = shotEntity(episode, seq, shot)
                if ownEntry(core, entity, shotInfo, store) is not None:
                    continue
                meta, _source = resolve(core, entity, shotInfo, store, holders)
                changes[shotKey(entity)] = {"metadata": meta}
        return changes

    return updateStore(core, changesFor)


def markCreated(core, entity, metaData=None):
    """Give a shot Prism has just created an entry of its own in the
    tracker's file, so it never shows a shared entry another episode wrote.
    It holds the metadata the shot was created with. Never raises.

    Does nothing when the shot has an entry of its own already - unless
    beforeCreate saw its folder didn't exist yet: then an entry the
    tracker's file still holds for that name is a leftover of an earlier
    shot (one deleted in Prism, or whose sequence was renamed), and the new
    shot doesn't take it over.
    """
    try:
        if not applies(core, entity):
            return False
        entity = shotEntity(*shotKey(entity))
        key = shotKey(entity)
        info = _creating.get(key) or {}
        if metaData is None:
            metaData = info.get("metaData")
        fresh = bool(info.get("new")) and not info.get("marked")
        shotInfo = {} if keepsOwnFile(core) else readShotInfo(core)

        def changesFor(store):
            if prismOwnEntry(core, entity, shotInfo) is not None:
                return {}
            if nested(store, ("shots",) + key) is not None and not fresh:
                return {}
            meta = copy.deepcopy(metaData) if isinstance(metaData, dict) else {}
            return {key: {"metadata": meta}}

        written = updateStore(core, changesFor)
        if info:
            info["marked"] = True
        return written
    except Exception as e:
        logger.warning(
            "Production Tracker: could not record the new shot %s: %s",
            "/".join(str(k) for k in shotKey(entity or {})), e,
        )
        return False


# ------------------------------------------------ Prism's own changes -----
def entityFrom(args):
    """The shot dict in a Prism callback's arguments, or None."""
    for arg in args:
        if isinstance(arg, dict) and arg.get("shot"):
            return arg
    return None


def beforeCreate(core, entity, metaData=None):
    """Called before Prism creates a shot (by the createShot guard, or by
    the tracker when that guard isn't installed). Never raises."""
    try:
        if not applies(core, entity):
            return
        try:
            existed = os.path.exists(core.getEntityPath(entity=entity) or "")
        except Exception:
            existed = True  # unknown: an entry it has is kept
        _creating[shotKey(entity)] = {"metaData": metaData, "new": not existed}
        pinShots(core, [(entity.get("sequence"), entity.get("shot"))])
    except Exception as e:
        logger.warning("Production Tracker: could not prepare for the new shot: %s", e)


def afterCreate(core, entity, created=None):
    """Called by the createShot guard after Prism's createShot of a shot
    that didn't exist. created: whether it was created, or None to look for
    its folder. Never raises."""
    try:
        if not applies(core, entity):
            return
        if created is None:
            try:
                created = os.path.isdir(core.getEntityPath(entity=entity) or "")
            except Exception:
                created = False
        if created:
            markCreated(core, entity)
    except Exception as e:
        logger.warning("Production Tracker: could not record the new shot: %s", e)
    finally:
        _creating.pop(shotKey(entity or {}), None)


def metaDataCalls(args, kwargs):
    """[(entity, metaData)] of a call to Prism's setMetaData(entity,
    metaData) or setMetaData(entities=[...], metaDatas=[...])."""
    names = ("entity", "metaData", "entities", "metaDatas")
    values = dict(zip(names, args))
    values.update((k, v) for k, v in kwargs.items() if k in names)
    if values.get("entity") and not values.get("entities"):
        return [(values.get("entity"), values.get("metaData"))]
    return list(zip(values.get("entities") or [], values.get("metaDatas") or []))


def beforeSetMetaData(core, args, kwargs):
    """Called by the guard around Prism's setMetaData before it runs, and
    returns a function to call afterwards (or None). Never raises.

    In a project with episodes made before Prism 2.1.4, Prism writes an
    episode shot's metadata to the entry shared by every episode's shot of
    that name, which the tracker doesn't read for a shot of its own. So
    what the call changes is added to the shot's entry in the tracker's
    file: the keys whose value differs from what the shared entry held
    before the call. Not the whole block: Prism's Edit Shot dialog, and a
    script that reads with getMetaData, start from the shared entry, which
    may be another episode's; a value they pass on unchanged isn't taken as
    this shot's. A value equal to the shared entry's, and a key removed, are
    not carried over for the same reason.

    A shot Prism is creating is left to markCreated, which records the
    metadata it is created with whole.
    """
    try:
        if not keepsOwnFile(core):
            return None
        calls = []
        for entity, meta in metaDataCalls(args, kwargs):
            if not isinstance(entity, dict) or entity.get("type") != "shot":
                continue
            if not isinstance(meta, dict) or not applies(core, entity):
                continue
            entity = shotEntity(*shotKey(entity))
            if shotKey(entity) in _creating:
                continue
            calls.append((entity, copy.deepcopy(meta)))
        if not calls:
            return None
        shotInfo = readShotInfo(core)
        store = readStore(core)
        before = []
        for entity, meta in calls:
            known = folderHolders(core, entity["sequence"], entity["shot"])
            shown, source = resolve(
                core, entity, shotInfo, store, holdersOf(entity, shotInfo, known)
            )
            before.append((entity, meta, metaOf(sharedEntry(shotInfo, entity)), shown, source))
    except Exception as e:
        logger.warning("Production Tracker: setMetaData: episode shot data not prepared: %s", e)
        return None

    def after():
        def changesFor(store):
            changes = {}
            for entity, meta, shared, shown, source in before:
                diff = dict(
                    (k, copy.deepcopy(v)) for k, v in meta.items() if shared.get(k) != v
                )
                if not diff:
                    continue
                own = nested(store, ("shots",) + shotKey(entity))
                if own is None:
                    if source == "shared":
                        # Only this episode has the name: the tracker shows
                        # the shared entry, which now holds these values.
                        continue
                    entry = {"metadata": shown}
                else:
                    entry = copy.deepcopy(own)
                    if not isinstance(entry.get("metadata"), dict):
                        entry["metadata"] = {}
                entry["metadata"].update(diff)
                changes[shotKey(entity)] = entry
            return changes

        updateStore(core, changesFor)

    return guarded("setMetaData", after)


def entityFolder(core, entity):
    try:
        return os.path.normpath(core.getEntityPath(entity=entity) or "")
    except Exception:
        return ""


@contextlib.contextmanager
def handledByCaller():
    """While the tracker renames a shot itself: it pins and moves the
    tracker's entries itself (see prepareShotRename), so the guard around
    Prism's rename leaves them alone."""
    _callerHandles[0] += 1
    try:
        yield
    finally:
        _callerHandles[0] -= 1


def beforePrismChange(core, name, args, kwargs):
    """Called by the guard around Prism's renameShot, renameSequence,
    renameEpisode and deleteShot, before it runs. Pins the shots whose names
    it touches (see pinShots) and returns a function to call afterwards,
    which moves the tracker's entries along with a rename, or None. Best
    effort: never raises, and never stops Prism's own change."""
    try:
        if not usesEpisodes(core) or _callerHandles[0]:
            return None
        locations = kwargs.get("locations", args[2] if len(args) > 2 else None)
        if locations and "global" not in locations:
            # Only renaming a user's local copies; the project already was.
            return None
        if name == "renameShot" and len(args) >= 2:
            after = prepareShotRename(core, args[0], args[1], strict=False)
            return guarded("renameShot", after) if after is not None else None
        if name == "renameSequence" and len(args) >= 2:
            return beforeSequenceRename(core, args[0], args[1])
        if name == "renameEpisode" and len(args) >= 2:
            return afterEpisodeRename(core, args[0], args[1])
        if name == "deleteShot" and args and isinstance(args[0], dict):
            if applies(core, args[0]):
                pinShots(core, [(args[0].get("sequence"), args[0].get("shot"))])
    except Exception as e:
        logger.warning("Production Tracker: %s: episode shot data not prepared: %s", name, e)
    return None


def moveEntries(core, moves):
    """Move entries of the tracker's file: {oldKey: newKey}, keys being
    (episode, sequence, shot), or a function given the file's data that
    returns that. One that isn't there is skipped. Raises if the file can't
    be written."""

    def changesFor(store):
        changes = {}
        for old, new in (moves(store) if callable(moves) else moves).items():
            entry = nested(store, ("shots",) + tuple(old))
            if entry is None or old == new:
                continue
            changes.setdefault(tuple(old), None)
            changes[tuple(new)] = copy.deepcopy(entry)
        return changes

    return updateStore(core, changesFor)


def guarded(label, function):
    """function, made to log rather than raise (for after Prism's change)."""

    def run():
        try:
            function()
        except Exception as e:
            logger.warning(
                "Production Tracker: %s: the tracker's episode shot data was not "
                "updated: %s", label, e,
            )

    return run


def renamedOnDisk(oldFolder, newFolder):
    """Whether a rename from oldFolder to newFolder happened. Prism reports
    a cancelled rename (a file in use) with a popup, and the shot keeps its
    old folder. A rename that only changes case (sh050 -> SH050) leaves the
    old path "existing" on Windows, so there the new name's spelling in the
    folder is what tells."""
    if not newFolder:
        return False
    if oldFolder and os.path.normcase(oldFolder) == os.path.normcase(newFolder):
        try:
            return os.path.basename(newFolder) in os.listdir(os.path.dirname(newFolder))
        except OSError:
            return False
    if oldFolder and os.path.exists(oldFolder):
        return False
    return os.path.isdir(newFolder)


def prepareShotRename(core, cur, new, strict=True):
    """Before a shot cur is renamed to new: pin the shots of both names (see
    pinShots) and return a function to call after the rename, which moves
    the shot's entry in the tracker's file to its new name (it does nothing
    when the rename didn't happen). None when it isn't an episode shot.

    strict: a failed pin raises (the tracker's own rename then doesn't go
    ahead), and so does a failed move. Otherwise a failed pin is logged.
    """
    if not isinstance(cur, dict) or not isinstance(new, dict) or not applies(core, cur):
        return None
    old = shotEntity(*shotKey(cur))
    renamed = shotEntity(
        new.get("episode") or old["episode"],
        new.get("sequence") or old["sequence"],
        new.get("shot") or old["shot"],
    )
    names = [(old["sequence"], old["shot"]), (renamed["sequence"], renamed["shot"])]
    if strict:
        pinShots(core, names)
    else:
        try:
            pinShots(core, names)
        except Exception as e:
            logger.warning("Production Tracker: renameShot: shots not pinned: %s", e)
    oldFolder = entityFolder(core, old)

    def after():
        if renamedOnDisk(oldFolder, entityFolder(core, renamed)):
            moveEntries(core, {shotKey(old): shotKey(renamed)})

    return after


def beforeSequenceRename(core, curName, newName):
    if not curName or not newName or curName == newName:
        return None
    # Prism's renameSequence isn't told an episode, so every episode's
    # shots of the two sequence names are pinned.
    before = episodeShots(core)
    names = set()
    for shots in before.values():
        names |= set(s for s in shots if s[0] in (curName, newName))
    try:
        pinShots(core, names)
    except Exception as e:
        logger.warning("Production Tracker: renameSequence: shots not pinned: %s", e)

    def after():
        now = episodeShots(core, list(before))
        renamedIn = []
        for episode, shots in before.items():
            if not any(seq == curName for seq, _shot in shots):
                continue
            stillThere = any(seq == curName for seq, _shot in now.get(episode, []))
            moved = any(seq == newName for seq, _shot in now.get(episode, []))
            if moved and not stillThere:
                renamedIn.append(episode)
        if not renamedIn:
            return  # not renamed in any episode (cancelled)

        # Every entry the tracker's file has under the old sequence name in
        # those episodes moves, not just the shots listed: an entry left
        # behind would be taken by a shot created later under the old name.
        def moves(store):
            out = {}
            for episode in renamedIn:
                shots = nested(store, ("shots", episode, curName)) or {}
                for shot in shots:
                    out[(episode, curName, shot)] = (episode, newName, shot)
            return out

        moveEntries(core, moves)

    return guarded("renameSequence", after)


def afterEpisodeRename(core, curName, newName):
    # The sequence and shot names stay the same, so which episodes have a
    # name doesn't change and nothing needs pinning; only the tracker's
    # entries move to the new episode name.
    if not curName or not newName or curName == newName:
        return None

    def after():
        names = episodeNames(core)
        if curName in names or newName not in names:
            return  # cancelled

        def moves(store):
            out = {}
            sequences = nested(store, ("shots", curName)) or {}
            for seq, shots in sequences.items():
                if isinstance(shots, dict):
                    for shot in shots:
                        out[(curName, seq, shot)] = (newName, seq, shot)
            return out

        moveEntries(core, moves)

    return guarded("renameEpisode", after)


# ------------------------------------------------------ show-flag repair ---
def repairShownShowFlags(core, entity, badKeys):
    """Unset "show" on the keys badKeys(meta) names in the metadata block
    Prism shows for an episode shot - which, in a project made before Prism
    2.1.4, is the entry shared by every episode's shot of that name. Such a
    flag makes Prism's own Scene Browser raise when the shot is selected.

    This is the one write the tracker still makes to the shared entry, and
    it only changes those flags: no value is copied into or out of it.
    Read fresh, written once and read back; raises if it didn't land.
    Returns whether anything was written.
    """
    path = shotInfoPath(core)
    found = {}

    def change(data):
        entry = TrackerPrismGuards.shotInfoEntityData(core, data, entity)
        meta = entry.get("metadata") if isinstance(entry, dict) else None
        keys = badKeys(meta) if isinstance(meta, dict) else []
        for key in keys:
            meta[key]["show"] = False
        found["keys"] = keys

    data = readUnchanged(core, path, change)
    keys = found.get("keys") or []
    if not keys:
        return False
    core.setConfig(data=data, configPath=path, updateNestedData=False)

    entry = TrackerPrismGuards.shotInfoEntityData(core, readFile(core, path), entity)
    meta = entry.get("metadata") if isinstance(entry, dict) else None
    if not isinstance(meta, dict) or badKeys(meta):
        try:
            core.configs.clearCache(path)
        except Exception:
            pass
        raise RuntimeError(
            "the change did not reach %s (is the file in use?)" % os.path.basename(path)
        )
    return True
