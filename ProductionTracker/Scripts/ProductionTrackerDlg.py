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
# The main tracker window: lists all shots and assets of the current
# project (organized like Prism: sequences as folders containing shots,
# nested folders containing assets) and lets artists assign users, set
# status, frame ranges and due dates. New shots/assets can be added and
# are created in Prism on save.
#
# Tracker data (status / assignee / due date) is stored in Prism's native
# per-entity metadata (00_Pipeline/Shotinfo & Assetinfo), so it lives in
# the project and is shared by all users. Saves only write rows that
# actually changed and re-read the latest data before merging, so
# simultaneous editing by multiple users doesn't clobber each other.
#
####################################################


import os
import copy
import datetime
import shutil
import threading
import time
import uuid

from qtpy.QtCore import *
from qtpy.QtGui import *
from qtpy.QtWidgets import *

from PrismUtils.Decorators import err_catcher_plugin as err_catcher

import Prism_ProductionTracker_Variables as PluginVars
import TrackerSettings
import TrackerTasks
import TrackerTeam
import TrackerComments
import TrackerCommentsWidget
import TrackerNotify
import TrackerNotifyLogDlg


# --- Metadata keys used to persist tracker data on each entity -------------
STATUS_KEY = "tracker_status"
ASSIGNEE_KEY = "tracker_assignee"
DUE_KEY = "tracker_due"
NOTES_KEY = "tracker_notes"
DEPT_KEY = "tracker_department"
# Extra VFX/FX layers tracked in parallel with a shot are stored as a list on
# the parent shot's metadata (they are not real Prism entities).
FXLAYERS_KEY = "tracker_fxlayers"

# Prism's Scene Browser renders every metadata entry whose "show" flag is set
# with QLabel(value), which only accepts a string. These two keys hold
# structured payloads (a comment thread, a list of FX layers), so they must
# always be written unshown or Prism's own entity-info panel raises a
# TypeError. See repairShowFlags() for data saved before this was fixed.
UNSHOWN_KEYS = (NOTES_KEY, FXLAYERS_KEY)
# Prism stores shot descriptions in metadata under this key.
DESC_KEY = "Description"

# In "Prism departments and tasks" mode the same fields are stored in each
# task's own info file instead of on the entity. Plain keys there - a task
# info file is an ordinary config, not Prism's {"value":..., "show":...}
# metadata format.
TASK_KEYS = {
    "status": STATUS_KEY,
    "assignee": ASSIGNEE_KEY,
    "due": DUE_KEY,
    "desc": DESC_KEY,
    "notes": NOTES_KEY,
}

# The single-value fields of a row. On save each one is merged against what
# is on disk at that moment (see mergeRowValues): a field this user did not
# touch takes the stored value, so an edit to one field never writes a stale
# copy of the others over someone else's newer change. Comments are merged
# separately, by comment id (TrackerComments.merge_comments).
ENTITY_FIELDS = ("assignee", "status", "department", "due", "range", "desc")
TASK_FIELDS = ("assignee", "status", "due", "desc")
FX_FIELDS = ("name", "assignee", "status", "due", "desc")

# How often (seconds) task mode checks the task folders and info files for
# changes made by other users. Only file timestamps are read, on a background
# thread, so it is light even on a network share. Shot mode doesn't need this:
# all of its data lives in the two files checked every few seconds.
TASK_POLL_INTERVAL = 30

# --- Status options and their accent colors --------------------------------
# Fixed: the same five states everywhere, so a status always means the same
# thing when a project is handed over.
STATUS_NOT_STARTED = "Not started"
STATUS_IN_PROGRESS = "In-progress"
STATUS_IN_REVIEW = "In-review"
STATUS_COMPLETED = "Completed"
STATUS_NOT_NEEDED = "Not needed"

STATUSES = [
    STATUS_NOT_STARTED,
    STATUS_IN_PROGRESS,
    STATUS_IN_REVIEW,
    STATUS_COMPLETED,
    STATUS_NOT_NEEDED,
]

STATUS_COLORS = {
    STATUS_NOT_STARTED: "#6b7280",  # grey
    STATUS_IN_PROGRESS: "#3b82f6",  # blue
    STATUS_IN_REVIEW: "#f59e0b",    # amber
    STATUS_COMPLETED: "#22c55e",    # green
    STATUS_NOT_NEEDED: "#52525b",   # muted grey (abandoned)
}

# --- Department options ----------------------------------------------------
# Purely informational (not tied to any Prism pipeline step); gives leads and
# artists another level of visibility on where a shot/asset is in the pipe.
# Unlike statuses these ARE editable per project (Configuration -> Settings);
# loadOptions() reads them into self.deptShot / self.deptAsset / self.deptFx.
# APPROVED_DEPT is always appended last and marks the end of the pipeline.
DEPT_NONE = TrackerSettings.DEPT_NONE
APPROVED_DEPT = TrackerSettings.APPROVED_DEPT
NEUTRAL_COLOR = TrackerSettings.NEUTRAL_COLOR

# Departments that were renamed since first release; old values saved in
# entity metadata are migrated to the new label on load (and re-saved on the
# next autosave).
DEPARTMENT_RENAMES = {
    "Complete": "Approved",
}

# UI colors
SEPARATOR_COLOR = "#3d3d3d"
ROW_BG = "#242424"
ROW_BG_ALT = "#2b2b2b"

# A shot that is both Completed and its department set to Approved gets a
# distinct dark-green row tint (stronger than the normal assignee tint) so
# fully wrapped-up shots stand out in the list.
COMPLETE_APPROVED_COLOR = "#15803d"
COMPLETE_APPROVED_ALPHA = 0.45

# A row whose status is "Not needed" is faded out (including its thumbnail)
# to make it visually clear the shot/asset is no longer relevant. The status
# dropdown itself is left at full opacity so it's still easy to change back.
FADE_OPACITY = 0.4

# Column layout
COL_NAME = 0
COL_TYPE = 1
COL_DESC = 2
COL_ASSIGNEE = 3
COL_STATUS = 4
COL_DEPARTMENT = 5
COL_RANGE = 6
COL_DUE = 7
COL_COUNT = 8

# Roles stored on leaf tree items
ROLE_ENTITY = Qt.UserRole
ROLE_ISNEW = Qt.UserRole + 1
ROLE_SNAPSHOT = Qt.UserRole + 2
ROLE_ISLEAF = Qt.UserRole + 3
ROLE_NOTES = Qt.UserRole + 4
ROLE_ROWCOLOR = Qt.UserRole + 5
ROLE_FXSNAPSHOT = Qt.UserRole + 6
# The unbadged thumbnail pixmap, kept so the comment indicator can be
# re-composited onto it when the read/unread state changes.
ROLE_THUMB = Qt.UserRole + 7
# True when the row's status is "Not needed" - tells the delegate to fade the
# natively-drawn columns (name/icon/type/range) too.
ROLE_FADEROW = Qt.UserRole + 8
# True on a shot/asset row in task mode: it is a summary of the task rows
# beneath it rather than an editable row of its own.
ROLE_ROLLUP = Qt.UserRole + 9
# On a department folder in task mode: (abbreviation, longName).
ROLE_DEPT = Qt.UserRole + 10

THUMB_W = 96
THUMB_H = 54


def statMtime(path):
    """A file or folder's modification time, 0 when it doesn't exist."""
    try:
        return os.path.getmtime(path)
    except (OSError, ValueError):
        return 0


class RowTintDelegate(QStyledItemDelegate):
    """Paints a per-row background tint stored in ROLE_ROWCOLOR.

    A plain QTreeWidgetItem.setBackground() is ignored once the tree has a
    stylesheet that targets '::item', so the tint is drawn here instead. The
    cell widgets (and their holders) are transparent, so this fill shows
    through the whole row, not just the text-only columns.
    """

    def paint(self, painter, option, index):
        color = index.data(ROLE_ROWCOLOR)
        if color:
            painter.save()
            painter.fillRect(option.rect, color)
            painter.restore()
        if index.data(ROLE_FADEROW) and index.column() != COL_STATUS:
            painter.save()
            painter.setOpacity(FADE_OPACITY)
            super(RowTintDelegate, self).paint(painter, option, index)
            painter.restore()
        else:
            super(RowTintDelegate, self).paint(painter, option, index)


class NoScrollComboBox(QComboBox):
    """A combo box that ignores the mouse wheel.

    The tracker list has many drop downs, so scrolling the view while the
    cursor happens to sit over one would otherwise change its value. Ignoring
    the wheel event lets it propagate to the parent so the list scrolls
    instead. The drop-down popup still scrolls normally when it is open.
    """

    def __init__(self, *args, **kwargs):
        super(NoScrollComboBox, self).__init__(*args, **kwargs)
        # Don't grab focus just by hovering/wheeling over the widget.
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event):
        event.ignore()


class NoScrollDateEdit(QDateEdit):
    """A date edit that ignores the mouse wheel (see NoScrollComboBox).

    Only a left click (or tab) activates the field, so scrolling the tracker
    list can't accidentally change a due date.

    Also fixes up the pop-up calendar the first time it can be opened: weeks
    start on Monday, and the month/year buttons lose the menu arrow that
    Prism's stylesheet draws on top of their text.
    """

    def __init__(self, *args, **kwargs):
        super(NoScrollDateEdit, self).__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.StrongFocus)
        self._calendarReady = False

    def wheelEvent(self, event):
        event.ignore()

    def prepareCalendar(self):
        """Configure the pop-up calendar, once.

        Deliberately lazy: the tracker builds one date editor per row, and
        asking for calendarWidget() constructs a whole QCalendarWidget, so
        doing it up front would build one for every shot and asset in the
        project just to show a handful of them.
        """
        if self._calendarReady or not self.calendarPopup():
            return
        self._calendarReady = True

        cal = self.calendarWidget()
        if cal is None:
            return
        cal.setFirstDayOfWeek(Qt.Monday)
        # An empty field parks the value on the minimum date (see the "No
        # date" state in DueDateWidget), which would otherwise open the
        # calendar in January 2000. Watch for the popup opening so it can be
        # sent to the current month instead.
        cal.installEventFilter(self)

        # The month and year buttons are QToolButtons with a popup menu, so
        # the style paints a drop-down arrow over the label. There is no API
        # to turn that off - only the stylesheet reaches it.
        noArrow = (
            "QToolButton{padding-right:10px;}"
            "QToolButton::menu-indicator{image:none; width:0px;}"
            "QToolButton::down-arrow{image:none; width:0px;}"
        )
        for name in ("qt_calendar_monthbutton", "qt_calendar_yearbutton"):
            btn = cal.findChild(QToolButton, name)
            if btn is not None:
                btn.setStyleSheet(noArrow)

    def hasDate(self):
        """False while the field is showing its empty 'No date' state."""
        return self.date() != self.minimumDate()

    def eventFilter(self, obj, event):
        """Open an unset field on the current month rather than on the
        sentinel date that represents 'no date'.

        Only the shown page is moved, never the selection, so the field keeps
        reading 'No date' until the user actually picks a day.
        """
        if event.type() == QEvent.Show and not self.hasDate():
            cal = self.calendarWidget()
            if cal is not None and obj is cal:
                today = QDate.currentDate()
                cal.setCurrentPage(today.year(), today.month())
        return super(NoScrollDateEdit, self).eventFilter(obj, event)

    # The popup can only be opened by interacting with this widget, so
    # preparing it on first click / keypress / focus is enough.
    def mousePressEvent(self, event):
        self.prepareCalendar()
        super(NoScrollDateEdit, self).mousePressEvent(event)

    def keyPressEvent(self, event):
        self.prepareCalendar()
        super(NoScrollDateEdit, self).keyPressEvent(event)

    def focusInEvent(self, event):
        self.prepareCalendar()
        super(NoScrollDateEdit, self).focusInEvent(event)


class NoScrollSpinBox(QSpinBox):
    """A spin box that ignores the mouse wheel (see NoScrollComboBox)."""

    def __init__(self, *args, **kwargs):
        super(NoScrollSpinBox, self).__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event):
        event.ignore()


class DueDateWidget(QWidget):
    """A date picker that supports an empty ('No date') state."""

    def __init__(self, isoDate=None):
        super(DueDateWidget, self).__init__()
        lo = QHBoxLayout()
        lo.setContentsMargins(2, 0, 2, 0)
        lo.setSpacing(2)
        self.setLayout(lo)

        self.dateEdit = NoScrollDateEdit()
        self.dateEdit.setCalendarPopup(True)
        self.dateEdit.setDisplayFormat("yyyy-MM-dd")
        self.dateEdit.setStyleSheet("QDateEdit{background: transparent;}")
        self.dateEdit.setMinimumDate(QDate(2000, 1, 1))
        self.dateEdit.setSpecialValueText("  No date")
        self.dateEdit.setDate(self.dateEdit.minimumDate())

        self.b_clear = QToolButton()
        self.b_clear.setText("x")
        self.b_clear.setToolTip("Clear due date")
        self.b_clear.clicked.connect(self.clear)

        lo.addWidget(self.dateEdit)
        lo.addWidget(self.b_clear)

        if isoDate:
            d = QDate.fromString(isoDate, "yyyy-MM-dd")
            if d.isValid():
                self.dateEdit.setDate(d)

    def clear(self):
        self.dateEdit.setDate(self.dateEdit.minimumDate())

    def isoDate(self):
        if self.dateEdit.date() == self.dateEdit.minimumDate():
            return ""
        return self.dateEdit.date().toString("yyyy-MM-dd")


# Width reserved for a row's +/trash buttons, so the column headings above
# the rows line up with the fields rather than drifting across them.
BULK_BUTTON_W = 30


class BulkList(QWidget):
    """A stack of identical input rows for creating several things at once.

    The first row carries a "+" that appends another; every later row carries
    a trash button that removes it, so a single row can never be deleted out
    from under the dialog. A new row starts as a copy of the one above it with
    its name cleared, which is the point of bulk entry: the shared parts (the
    sequence, the folder, the department, the frame range) carry over and only
    the name has to be typed.

    `rowFactory(previous)` returns a fields widget exposing:
        values()  -> dict for that row
        isBlank() -> True when nothing has been entered
        focusName() to put the cursor in the obvious field
    """

    def __init__(self, rowFactory, parent=None):
        super(BulkList, self).__init__(parent)
        self.rowFactory = rowFactory
        self.rows = []

        self._lo = QVBoxLayout()
        self._lo.setContentsMargins(0, 0, 0, 0)
        self._lo.setSpacing(4)
        self.setLayout(self._lo)

        self.addRow(focus=False)

    # ------------------------------------------------------------ rows ----
    @err_catcher(name=__name__)
    def addRow(self, focus=True):
        previous = self.rows[-1][1] if self.rows else None
        fields = self.rowFactory(previous)

        holder = QWidget()
        hlo = QHBoxLayout()
        hlo.setContentsMargins(0, 0, 0, 0)
        hlo.setSpacing(4)
        holder.setLayout(hlo)
        hlo.addWidget(fields, 1)

        b_add = QToolButton()
        b_add.setText("+")
        b_add.setFixedWidth(BULK_BUTTON_W)
        b_add.setToolTip("Add another")
        b_add.clicked.connect(lambda: self.addRow())
        hlo.addWidget(b_add)

        b_remove = QToolButton()
        b_remove.setIcon(self.style().standardIcon(QStyle.SP_TrashIcon))
        b_remove.setFixedWidth(BULK_BUTTON_W)
        b_remove.setToolTip("Remove this row")
        b_remove.clicked.connect(lambda: self.removeRow(holder))
        hlo.addWidget(b_remove)

        self._lo.addWidget(holder)
        self.rows.append((holder, fields, b_add, b_remove))
        self.refreshButtons()
        if focus:
            fields.focusName()
        return fields

    @err_catcher(name=__name__)
    def removeRow(self, holder):
        for i, row in enumerate(self.rows):
            if row[0] is holder:
                self.rows.pop(i)
                holder.setParent(None)
                holder.deleteLater()
                break
        self.refreshButtons()

    @err_catcher(name=__name__)
    def refreshButtons(self):
        """Only the first row adds; only later rows can be removed."""
        for i, (_holder, _fields, b_add, b_remove) in enumerate(self.rows):
            b_add.setVisible(i == 0)
            b_remove.setVisible(i > 0)

    @err_catcher(name=__name__)
    def values(self):
        """Every non-blank row, in order."""
        return [f.values() for (_h, f, _a, _r) in self.rows if not f.isBlank()]


class ShotFields(QWidget):
    """Episode / sequence / shot / frame range for one row of AddShotDialog.

    The episode field only exists in projects that use episodes; elsewhere it
    is absent entirely rather than shown empty.
    """

    def __init__(self, sequences=None, previous=None, sequence="",
                 episodes=None, episode=""):
        super(ShotFields, self).__init__()
        lo = QHBoxLayout()
        lo.setContentsMargins(0, 0, 0, 0)
        lo.setSpacing(4)
        self.setLayout(lo)

        self.cb_episode = None
        if episodes is not None:
            self.cb_episode = QComboBox()
            self.cb_episode.setEditable(True)
            self.cb_episode.addItems(episodes)
            self.cb_episode.setFixedWidth(140)
            lo.addWidget(self.cb_episode)

        self.cb_sequence = QComboBox()
        self.cb_sequence.setEditable(True)
        self.cb_sequence.addItems(sequences or [])
        self.cb_sequence.setFixedWidth(150)
        self.e_shot = QLineEdit()
        self.e_shot.setPlaceholderText("Shot name")
        self.e_shot.setMinimumWidth(140)
        self.sp_start = NoScrollSpinBox()
        self.sp_start.setRange(0, 9999999)
        self.sp_start.setValue(1001)
        self.sp_start.setFixedWidth(90)
        self.sp_start.setToolTip("Start frame")
        self.sp_end = NoScrollSpinBox()
        self.sp_end.setRange(0, 9999999)
        self.sp_end.setValue(1100)
        self.sp_end.setFixedWidth(90)
        self.sp_end.setToolTip("End frame")

        if previous is not None:
            # Carry over everything except the name.
            self.cb_sequence.setCurrentText(previous.cb_sequence.currentText())
            self.sp_start.setValue(previous.sp_start.value())
            self.sp_end.setValue(previous.sp_end.value())
            if self.cb_episode is not None and previous.cb_episode is not None:
                self.cb_episode.setCurrentText(previous.cb_episode.currentText())
        else:
            self.cb_sequence.setCurrentText(sequence or "")
            if self.cb_episode is not None:
                self.cb_episode.setCurrentText(episode or "")

        lo.addWidget(self.cb_sequence)
        lo.addWidget(self.e_shot, 1)
        lo.addWidget(self.sp_start)
        lo.addWidget(self.sp_end)

    def focusName(self):
        self.e_shot.setFocus()

    def isBlank(self):
        # The NAME is what decides. A new row inherits the sequence from the
        # one above it, so an untouched extra row must still count as blank
        # rather than failing validation for a missing shot name.
        return not self.e_shot.text().strip()

    def values(self):
        return {
            "episode": (
                self.cb_episode.currentText().strip()
                if self.cb_episode is not None
                else ""
            ),
            "sequence": self.cb_sequence.currentText().strip(),
            "shot": self.e_shot.text().strip(),
            "start": self.sp_start.value(),
            "end": self.sp_end.value(),
        }


class AssetFields(QWidget):
    """One asset path for AddAssetDialog."""

    def __init__(self, previous=None, folder=""):
        super(AssetFields, self).__init__()
        lo = QHBoxLayout()
        lo.setContentsMargins(0, 0, 0, 0)
        lo.setSpacing(4)
        self.setLayout(lo)

        self.e_path = QLineEdit()
        self.e_path.setPlaceholderText("e.g. Characters/Dog")
        self.e_path.setMinimumWidth(320)
        self.e_path.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        if previous is not None:
            # Keep the folder the previous row was in, drop its name.
            prev = previous.e_path.text().strip().replace("\\", "/")
            self.e_path.setText(prev.rsplit("/", 1)[0] + "/" if "/" in prev else "")
        elif folder:
            self.e_path.setText(folder.rstrip("/") + "/")
        lo.addWidget(self.e_path, 1)

    def focusName(self):
        self.e_path.setFocus()

    def isBlank(self):
        # As with shots: a new row inherits the folder of the one above it and
        # that prefix always ends in "/", so a row that names nothing yet is
        # blank. "Characters" with no slash IS an asset at the root.
        text = self.e_path.text().strip().replace("\\", "/")
        return not text.strip("/") or text.endswith("/")

    def values(self):
        return {
            "asset_path": self.e_path.text().strip().replace("\\", "/").strip("/")
        }


class TaskFields(QWidget):
    """Department + task name for one row of AddTaskDialog."""

    def __init__(self, departments, previous=None, department=""):
        super(TaskFields, self).__init__()
        lo = QHBoxLayout()
        lo.setContentsMargins(0, 0, 0, 0)
        lo.setSpacing(4)
        self.setLayout(lo)

        self.cb_department = QComboBox()
        self.cb_department.setFixedWidth(180)
        for abbreviation, longName in departments:
            label = (
                longName
                if longName == abbreviation
                else "%s (%s)" % (longName, abbreviation)
            )
            self.cb_department.addItem(label, abbreviation)

        self.cb_task = QComboBox()
        self.cb_task.setEditable(True)
        self.cb_task.setMinimumWidth(200)

        wanted = previous.cb_department.currentData() if previous is not None else department
        if wanted:
            idx = self.cb_department.findData(wanted)
            if idx >= 0:
                self.cb_department.setCurrentIndex(idx)

        lo.addWidget(self.cb_department)
        lo.addWidget(self.cb_task, 1)

    def focusName(self):
        self.cb_task.setFocus()

    def isBlank(self):
        return not self.cb_task.currentText().strip()

    def values(self):
        return {
            "department": self.cb_department.currentData() or "",
            "task": self.cb_task.currentText().strip(),
        }


class BulkDialog(QDialog):
    """Shared frame for the three add dialogs: a BulkList plus OK/Cancel.

    Subclasses supply makeFields(), the column headings, and validate(rows).
    """

    def __init__(self, core, title, parent=None):
        super(BulkDialog, self).__init__(parent)
        self.core = core
        self.setWindowTitle(title)
        self.core.parentWindow(self, parent=parent)

        self._lo = QVBoxLayout()
        self.setLayout(self._lo)

    @err_catcher(name=__name__)
    def buildBody(self, headings, hint=""):
        """headings: [(text, fixedWidth or 0, stretch)] mirroring the fields."""
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(4)
        for text, width, stretch in headings:
            label = QLabel(text)
            label.setStyleSheet("color:#9ca3af;")
            if width:
                label.setFixedWidth(width)
            head.addWidget(label, stretch)
        # Leave room for the +/trash column so nothing sits over it.
        head.addSpacing(BULK_BUTTON_W * 2 + 8)
        self._lo.addLayout(head)

        self.list = BulkList(self.makeFields)
        self._lo.addWidget(self.list)

        if hint:
            h = QLabel(hint)
            h.setWordWrap(True)
            h.setStyleSheet("color:#9ca3af;")
            self._lo.addWidget(h)

        self._lo.addStretch()

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.onAccept)
        bb.rejected.connect(self.reject)
        self._lo.addWidget(bb)

    @err_catcher(name=__name__)
    def duplicatesWithin(self, rows, keyFn):
        """Names entered more than once in this dialog."""
        seen = set()
        dupes = []
        for row in rows:
            key = keyFn(row)
            if key in seen and key not in dupes:
                dupes.append(key)
            seen.add(key)
        return dupes

    @err_catcher(name=__name__)
    def onAccept(self):
        rows = self.list.values()
        if not rows:
            self.core.popup(self.emptyMessage(), parent=self)
            return
        error = self.validate(rows)
        if error:
            self.core.popup(error, parent=self)
            return
        self._rows = rows
        self.accept()

    @err_catcher(name=__name__)
    def getRows(self):
        return getattr(self, "_rows", [])


class AddShotDialog(BulkDialog):
    """Create one or more shots."""

    def __init__(self, core, parent=None, sequences=None, sequence="",
                 existing=None, episodes=None, episode=""):
        self.sequences = sequences or []
        self.sequence = sequence
        self.existing = existing or set()
        # None means "this project does not use episodes"; a list (even an
        # empty one) means it does and the column is shown.
        self.episodes = episodes
        self.episode = episode
        super(AddShotDialog, self).__init__(core, "Add Shot", parent=parent)

        headings = [("Sequence", 150, 0), ("Shot", 0, 1),
                    ("Start", 90, 0), ("End", 90, 0)]
        if self.episodes is not None:
            headings.insert(0, ("Episode", 140, 0))
        self.buildBody(
            headings,
            "Press + to queue up more shots; each new row keeps the sequence "
            "and frame range of the one above it.",
        )

    def makeFields(self, previous):
        return ShotFields(
            self.sequences, previous, self.sequence,
            episodes=self.episodes, episode=self.episode,
        )

    def emptyMessage(self):
        return "Enter a sequence and a shot name."

    @err_catcher(name=__name__)
    def validate(self, rows):
        for row in rows:
            if not row["sequence"] or not row["shot"]:
                return "Every row needs both a sequence and a shot name."
            if self.episodes is not None and not row.get("episode"):
                return "This project uses episodes, so every row needs one."
            if row["end"] < row["start"]:
                return "%s / %s: the end frame is before the start frame." % (
                    row["sequence"],
                    row["shot"],
                )

        def key(row):
            if row.get("episode"):
                return "%s / %s / %s" % (row["episode"], row["sequence"], row["shot"])
            return "%s / %s" % (row["sequence"], row["shot"])

        dupes = self.duplicatesWithin(rows, lambda r: key(r).lower())
        if dupes:
            return "Listed more than once: %s" % ", ".join(dupes)

        clashes = [key(r) for r in rows if key(r).lower() in self.existing]
        if clashes:
            return "Already in this project: %s" % ", ".join(clashes)
        return ""


class AddAssetDialog(BulkDialog):
    """Create one or more assets."""

    def __init__(self, core, parent=None, folder="", existing=None):
        self.folder = folder
        self.existing = existing or set()
        super(AddAssetDialog, self).__init__(core, "Add Asset", parent=parent)
        self.buildBody(
            [("Asset path", 0, 1)],
            "Use '/' to place an asset inside (nested) folders. Press + to "
            "queue up more; each new row keeps the folder of the one above it.",
        )

    def makeFields(self, previous):
        return AssetFields(previous, self.folder)

    def emptyMessage(self):
        return "Enter an asset path."

    @err_catcher(name=__name__)
    def validate(self, rows):
        for row in rows:
            if not row["asset_path"]:
                return "Every row needs an asset path."

        dupes = self.duplicatesWithin(rows, lambda r: r["asset_path"].lower())
        if dupes:
            return "Listed more than once: %s" % ", ".join(dupes)

        clashes = [
            r["asset_path"] for r in rows if r["asset_path"].lower() in self.existing
        ]
        if clashes:
            return "Already in this project: %s" % ", ".join(clashes)
        return ""


class AddTaskDialog(BulkDialog):
    """Create one or more Prism departments/tasks under a shot or asset.

    Departments come from Project Settings rather than from disk, so work can
    be set up in a department nobody has saved a scene into yet.
    """

    def __init__(self, core, tasks, entity, parent=None, department=""):
        self.tasks = tasks
        self.entity = entity
        self.department = department
        self.entityType = entity.get("type") or "shot"
        self.departments = self.tasks.configuredDepartments(self.entityType)
        super(AddTaskDialog, self).__init__(core, "Add Task", parent=parent)
        self.buildBody(
            [("Department", 180, 0), ("Task", 0, 1)],
            "Press + to queue up more tasks; each new row keeps the "
            "department of the one above it.",
        )

    def makeFields(self, previous):
        fields = TaskFields(self.departments, previous, self.department)
        fields.cb_department.currentIndexChanged.connect(
            lambda _i, f=fields: self.suggestTasks(f)
        )
        self.suggestTasks(fields)
        return fields

    @err_catcher(name=__name__)
    def suggestTasks(self, fields):
        """Offer Prism's default task names for the chosen department."""
        department = fields.cb_department.currentData() or ""
        suggestions = self.tasks.defaultTasks(self.entityType, department)
        existing = self.tasks.tasks(self.entity, department)
        current = fields.cb_task.currentText()
        fields.cb_task.blockSignals(True)
        fields.cb_task.clear()
        fields.cb_task.addItems([t for t in suggestions if t not in existing])
        fields.cb_task.setCurrentText(current)
        fields.cb_task.blockSignals(False)

    def emptyMessage(self):
        return "Enter a task name."

    @err_catcher(name=__name__)
    def validate(self, rows):
        for row in rows:
            if not row["department"]:
                return "Every row needs a department."
            if not row["task"]:
                return "Every row needs a task name."

        def key(row):
            return "%s / %s" % (row["department"], row["task"])

        dupes = self.duplicatesWithin(rows, lambda r: key(r).lower())
        if dupes:
            return "Listed more than once: %s" % ", ".join(dupes)

        clashes = []
        for row in rows:
            existing = [t.lower() for t in self.tasks.tasks(self.entity, row["department"])]
            if row["task"].lower() in existing:
                clashes.append(key(row))
        if clashes:
            return "Already exists: %s" % ", ".join(clashes)
        return ""


class AboutDialog(QDialog):
    """Help -> About: what this is, which version, and where it lives."""

    def __init__(self, core, parent=None):
        super(AboutDialog, self).__init__(parent)
        self.core = core
        self.setWindowTitle("About %s" % PluginVars.PLUGIN_NAME)
        self.core.parentWindow(self, parent=parent)

        lo = QVBoxLayout()
        lo.setContentsMargins(20, 16, 20, 12)
        lo.setSpacing(8)
        self.setLayout(lo)

        title = QLabel(PluginVars.PLUGIN_NAME)
        f = title.font()
        f.setPointSize(f.pointSize() + 4)
        f.setBold(True)
        title.setFont(f)
        lo.addWidget(title)

        version = QLabel(PluginVars.PLUGIN_VERSION)
        version.setStyleSheet("color:#9ca3af;")
        lo.addWidget(version)

        blurb = QLabel(
            "A production tracking plugin for Prism Pipeline.\n\n"
            "Tracks status, assignees, departments and due dates for shots "
            "and assets, with threaded comments. All data is stored in the "
            "project itself."
        )
        blurb.setWordWrap(True)
        lo.addWidget(blurb)

        # Rendered as a real link and opened in the system browser.
        link = QLabel(
            '<a href="%s" style="color:#60a5fa;">%s</a>'
            % (PluginVars.PLUGIN_URL, PluginVars.PLUGIN_URL)
        )
        link.setOpenExternalLinks(True)
        link.setTextInteractionFlags(Qt.TextBrowserInteraction)
        link.setWordWrap(True)
        lo.addWidget(link)

        # The formal warranty disclaimer lives in LICENSE and in every file
        # header, where it belongs. An About box is a first impression, so it
        # points at the licence rather than shouting the disclaimer.
        licence = QLabel(
            "Free and open source under the GNU General Public License v3.0 "
            "or later. See the LICENSE file, and the project page for setup "
            "notes and known limitations."
        )
        licence.setWordWrap(True)
        licence.setStyleSheet("color:#9ca3af;")
        lo.addWidget(licence)

        lo.addStretch()

        row = QHBoxLayout()
        row.addStretch()
        self.b_close = QPushButton("Close")
        self.b_close.clicked.connect(self.accept)
        row.addWidget(self.b_close)
        lo.addLayout(row)

        self.resize(460, self.sizeHint().height())


class ProductionTrackerDlg(QDialog):
    def __init__(self, core, plugin, parent=None):
        super(ProductionTrackerDlg, self).__init__()
        self.core = core
        self.plugin = plugin
        self.newRows = []  # entity dicts pending creation
        self._syncing = False
        self._lastSyncMtimes = None
        self._building = False

        # Per-project settings, team roster and notifications.
        self.settings = TrackerSettings.TrackerSettings(core)
        self.tasks = TrackerTasks.TrackerTasks(core)
        self.trackerMode = TrackerSettings.MODE_SHOT
        self.loadOptions()
        self.team = TrackerTeam.TrackerTeam(core)
        self.notify = TrackerNotify.TrackerNotify(core)
        self.userMgmtEnabled = False
        self._repairedShowFlags = False
        self._warnedAssetNames = False
        # Set while a save pushes merged values back into the row widgets,
        # so those programmatic changes don't queue another autosave.
        self._applyingRemote = False
        # "label (field)" entries where this user and someone else changed
        # the same field; reported in the status bar after a save.
        self._conflicts = []
        # Task mode change detection (see TASK_POLL_INTERVAL): path -> mtime
        # as of the last rebuild, and the state of the background check.
        self._taskWatch = {}
        self._taskWatchGen = 0
        self._taskPollThread = None
        self._taskPollResult = None
        self._taskChangesPending = False
        self._lastTaskPoll = time.time()
        # Assignees found on tasks while building (see addTaskAssignees).
        self._taskAssignees = set()
        self.teamUsers = []
        self.currentUser = None
        self.avatarCache = {}

        # Sticky per-user preferences (stored in Prism's user config).
        self.colorRowsByUser = self.loadViewPref("colorRowsByUser")
        self.showThumbnails = self.loadViewPref("showThumbnails")

        # Per-user "which comments have I seen" state (drives the comment
        # bubble indicator on each row). Stored in Prism's user config.
        self._readState = self.loadReadState()

        self.core.parentWindow(self, parent=parent)
        self.setWindowTitle("Production Tracker")
        self.resize(1800, 900)

        self.setupUi()
        self.connectEvents()
        self.refresh()

        # Live sync: poll the shared config files for changes by other users.
        self.syncTimer = QTimer(self)
        self.syncTimer.setInterval(3000)
        self.syncTimer.timeout.connect(self.onSyncTick)
        self.syncTimer.start()

        # Debounced auto-save: field edits are written a short moment after the
        # user stops changing things (new shots/assets still need explicit Save).
        self.saveTimer = QTimer(self)
        self.saveTimer.setSingleShot(True)
        self.saveTimer.setInterval(1500)
        self.saveTimer.timeout.connect(self.flushAutoSave)

        # Debounced persistence of the comment read-state so navigating rows
        # with the keyboard doesn't hammer the user config on disk.
        self.readSaveTimer = QTimer(self)
        self.readSaveTimer.setSingleShot(True)
        self.readSaveTimer.setInterval(2000)
        self.readSaveTimer.timeout.connect(self.flushReadState)

    # --------------------------------------------------------- options ------
    @err_catcher(name=__name__)
    def loadOptions(self):
        """Read this project's department options into lookup tables.

        Called before the UI is built and again on every refresh, so editing
        them in Settings takes effect without reopening the tracker. Statuses
        are fixed (see STATUSES) and need no loading.
        """
        self.trackerMode = self.settings.getTrackerMode()
        depts = self.settings.getDepartments()
        self.deptShot = self.settings.departmentNames("shot", depts)
        self.deptAsset = self.settings.departmentNames("asset", depts)
        self.deptFx = depts["fxlayer"]["name"]
        self.deptColors = self.settings.departmentColors(depts)

    @err_catcher(name=__name__)
    def taskMode(self):
        """True while the tracker is grouping by Prism departments/tasks."""
        return self.trackerMode == TrackerSettings.MODE_TASKS

    @err_catcher(name=__name__)
    def applyModeVisibility(self):
        """Hide what the current mode does not use.

        In task mode the tree groups by department, so the Department column
        would only duplicate the row's own parent folder.
        """
        self.tree.setColumnHidden(COL_DEPARTMENT, self.taskMode())

    @err_catcher(name=__name__)
    def applyUserManagementVisibility(self):
        """Show or hide everything that only makes sense with a team roster.

        With user management off there is nobody to assign work to, so the
        Assignee column, the assignee filter and the per-user row tinting are
        hidden rather than left as dead controls.

        Hiding the column does NOT discard anything: the combo widgets stay
        alive behind it, so an assignee stored while the feature was on is
        still read back and re-saved untouched, and reappears the moment user
        management is switched on again.
        """
        show = self.userMgmtEnabled

        self.tree.setColumnHidden(COL_ASSIGNEE, not show)
        self.cb_filterAssignee.setVisible(show)
        if not show:
            # Drop any assignee filter so hidden rows can't stay filtered out.
            self.cb_filterAssignee.blockSignals(True)
            self.cb_filterAssignee.setCurrentIndex(0)
            self.cb_filterAssignee.blockSignals(False)

        self.actColorRows.setVisible(show)
        self.actColorRows.setEnabled(show)

    # ------------------------------------------------------------------ UI --
    @err_catcher(name=__name__)
    def setupUi(self):
        lo = QVBoxLayout()
        lo.setContentsMargins(10, 10, 10, 10)
        lo.setSpacing(8)
        self.setLayout(lo)

        # --- Menu bar ---
        self.menubar = QMenuBar(self)
        fileMenu = self.menubar.addMenu("File")
        self.actClose = fileMenu.addAction("Close")

        configMenu = self.menubar.addMenu("Configuration")
        self.actSettings = configMenu.addAction("Settings...")
        self.actSettings.setToolTip(
            "Edit the status and department options for this project."
        )
        configMenu.addSeparator()
        self.actColorRows = configMenu.addAction("Color rows by user color")
        self.actColorRows.setCheckable(True)
        self.actColorRows.setChecked(self.colorRowsByUser)
        self.actColorRows.setToolTip(
            "Tint each row with the assigned user's color (subtle, kept readable)."
        )
        self.actShowThumbs = configMenu.addAction("Show thumbnails")
        self.actShowThumbs.setCheckable(True)
        self.actShowThumbs.setChecked(self.showThumbnails)
        self.actShowThumbs.setToolTip(
            "Show each shot's and asset's preview image in the Name column."
        )
        configMenu.addSeparator()
        self.actViewNotifyLog = configMenu.addAction("View Notification Log")
        self.actViewNotifyLog.setToolTip(
            "Show every mention/reply/remind-me email this plugin has attempted to send."
        )
        helpMenu = self.menubar.addMenu("Help")
        self.actAbout = helpMenu.addAction("About")

        lo.setMenuBar(self.menubar)

        # --- Header / toolbar ---
        headerLo = QHBoxLayout()
        title = QLabel("Production Tracker")
        f = title.font()
        f.setPointSize(f.pointSize() + 4)
        f.setBold(True)
        title.setFont(f)
        headerLo.addWidget(title)
        headerLo.addStretch()

        self.e_search = QLineEdit()
        self.e_search.setPlaceholderText("Filter by name...")
        self.e_search.setClearButtonEnabled(True)
        self.e_search.setFixedWidth(220)
        headerLo.addWidget(self.e_search)

        self.cb_filterAssignee = NoScrollComboBox()
        self.cb_filterAssignee.setToolTip("Show only tasks assigned to...")
        self.cb_filterAssignee.setMinimumWidth(170)
        headerLo.addWidget(self.cb_filterAssignee)

        self.b_refresh = QPushButton("Refresh")
        self.b_addShot = QPushButton("Add Shot")
        self.b_addAsset = QPushButton("Add Asset")
        self.b_save = QPushButton("Save")
        self.b_save.setStyleSheet(
            "QPushButton{background-color: #2563eb; color: white; font-weight: bold; padding: 4px 14px;}"
            "QPushButton:hover{background-color: #1d4ed8;}"
        )
        for b in (self.b_refresh, self.b_addShot, self.b_addAsset, self.b_save):
            # Stop Enter (in a description/name field) from triggering a button
            # such as Refresh, which would rebuild the tree and drop the edit.
            b.setAutoDefault(False)
            b.setDefault(False)
            headerLo.addWidget(b)
        lo.addLayout(headerLo)

        # --- Tree ---
        self.tree = QTreeWidget()
        self.tree.setColumnCount(COL_COUNT)
        self.tree.setHeaderLabels(
            ["Name", "Type", "Description", "Assignee", "Status", "Department", "Frame Range", "Due Date"]
        )
        self.tree.setIconSize(QSize(THUMB_W, THUMB_H))
        self.tree.setUniformRowHeights(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tree.setExpandsOnDoubleClick(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        # Row background variation + solid vertical separators between fields.
        self.tree.setStyleSheet(
            "QTreeWidget{background-color:%s; alternate-background-color:%s; outline:0;"
            " border:1px solid %s;}"
            "QTreeWidget::item{border-right:1px solid %s; padding-top:4px; padding-bottom:4px;}"
            "QTreeWidget::item:selected{background-color:rgba(59,130,246,0.25); color:white;}"
            "QHeaderView::section{background-color:#2f2f2f; color:#cbd5e1; border:0;"
            " border-right:1px solid %s; border-bottom:1px solid %s; padding:5px;}"
            % (ROW_BG, ROW_BG_ALT, SEPARATOR_COLOR, SEPARATOR_COLOR, SEPARATOR_COLOR, SEPARATOR_COLOR)
        )
        # Draws the per-row user-color tint (see RowTintDelegate).
        self.tree.setItemDelegate(RowTintDelegate(self.tree))

        header = self.tree.header()
        header.setSectionResizeMode(COL_NAME, QHeaderView.Interactive)
        self.tree.setColumnWidth(COL_NAME, 300)
        header.setSectionResizeMode(COL_TYPE, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_DESC, QHeaderView.Interactive)
        self.tree.setColumnWidth(COL_DESC, 220)
        header.setSectionResizeMode(COL_ASSIGNEE, QHeaderView.Interactive)
        self.tree.setColumnWidth(COL_ASSIGNEE, 170)
        header.setSectionResizeMode(COL_STATUS, QHeaderView.Interactive)
        self.tree.setColumnWidth(COL_STATUS, 150)
        header.setSectionResizeMode(COL_DEPARTMENT, QHeaderView.Interactive)
        self.tree.setColumnWidth(COL_DEPARTMENT, 140)
        header.setSectionResizeMode(COL_RANGE, QHeaderView.Interactive)
        self.tree.setColumnWidth(COL_RANGE, 130)
        header.setSectionResizeMode(COL_DUE, QHeaderView.Interactive)
        self.tree.setColumnWidth(COL_DUE, 180)

        # --- Comments side panel (threaded comments; replaces plain notes) ---
        self.commentsPanel = TrackerCommentsWidget.CommentsPanel(self.core, self.team)
        self.commentsPanel.setStatuses(STATUSES)

        # --- Split view: tree on the left, comments on the right ---
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(self.tree)
        self.splitter.addWidget(self.commentsPanel)
        self.splitter.setStretchFactor(0, 4)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setCollapsible(1, False)
        self.splitter.setSizes([1320, 340])
        # Stretch factor 1 lets the splitter (and the tree/comments inside it)
        # consume all remaining vertical space instead of hugging the top.
        lo.addWidget(self.splitter, 1)

        # --- Status bar ---
        self.l_status = QLabel("")
        self.l_status.setStyleSheet("color: #9ca3af;")
        lo.addWidget(self.l_status)

        self._notesItem = None

    @err_catcher(name=__name__)
    def connectEvents(self):
        self.b_refresh.clicked.connect(self.refresh)
        self.b_addShot.clicked.connect(self.onAddShot)
        self.b_addAsset.clicked.connect(self.onAddAsset)
        self.b_save.clicked.connect(self.onSave)
        self.e_search.textChanged.connect(self.applyFilter)
        self.cb_filterAssignee.currentIndexChanged.connect(self.applyFilter)
        self.tree.itemSelectionChanged.connect(self.updateNotesPanel)
        self.tree.customContextMenuRequested.connect(self.onTreeContextMenu)
        self.commentsPanel.changed.connect(self.onCommentsChanged)
        self.actClose.triggered.connect(self.close)
        self.actSettings.triggered.connect(self.openSettings)
        self.actAbout.triggered.connect(self.openAbout)
        self.actColorRows.toggled.connect(self.onToggleColorRows)
        self.actShowThumbs.toggled.connect(self.onToggleThumbnails)
        self.actViewNotifyLog.triggered.connect(self.openNotifyLog)

    @err_catcher(name=__name__)
    def wrapCell(self, widget):
        """Wrap a cell widget so the row background shows through and a solid
        vertical separator is drawn on the right edge."""
        holder = QWidget()
        holder.setObjectName("cellWrap")
        hlo = QHBoxLayout()
        hlo.setContentsMargins(5, 1, 5, 1)
        hlo.setSpacing(0)
        hlo.addWidget(widget)
        holder.setLayout(hlo)
        holder.setStyleSheet(
            "QWidget#cellWrap{background: transparent; border-right:1px solid %s;}"
            % SEPARATOR_COLOR
        )
        return holder

    # ---------------------------------------------------------------- data --
    @err_catcher(name=__name__)
    def warnDuplicateAssetNames(self, assets):
        """Warn when two assets share a name across different folders.

        Prism keys asset metadata by the asset's NAME, not by its path
        (getAssetNameFromPath returns the basename), so two assets called the
        same thing in different folders share one metadata record - and with
        it one status, assignee, due date and comment thread. That is Prism's
        own behaviour rather than something this plugin can fix, but it looks
        exactly like a tracker bug, so say it plainly. Once per window.
        """
        if self._warnedAssetNames:
            return

        byName = {}
        for asset in assets:
            path = (asset.get("asset_path") or "").replace("\\", "/").strip("/")
            if not path:
                continue
            byName.setdefault(path.rsplit("/", 1)[-1].lower(), set()).add(path)

        clashes = [sorted(paths) for paths in byName.values() if len(paths) > 1]
        if not clashes:
            return

        self._warnedAssetNames = True
        shown = clashes[:5]
        lines = ["    " + "  =  ".join(group) for group in shown]
        if len(clashes) > len(shown):
            lines.append("    ...and %d more" % (len(clashes) - len(shown)))

        self.core.popup(
            "These assets share a name across different folders:\n\n%s\n\n"
            "Prism stores asset data under the asset NAME rather than its "
            "folder path, so each group above shares one status, assignee, "
            "due date and comment thread - in Prism as well as here.\n\n"
            "Renaming them apart is the only way to track them separately."
            % "\n".join(lines),
            parent=self,
        )

    @err_catcher(name=__name__)
    def loadEpisodes(self):
        """Episode names, or [] when the project does not use episodes.

        Guarded: Prism 2.1.3's getEpisodes() raises KeyError on a project
        whose structure has no episode template, which is every project with
        the feature switched off.
        """
        if not self._episodes:
            return []
        try:
            episodes = self.core.entities.getEpisodes() or []
        except Exception:
            return []
        return [e.get("episode", "") for e in episodes if e.get("episode")]

    @err_catcher(name=__name__)
    def loadShots(self):
        """All shots, each carrying an "episode" key (blank when unused).

        Prism only fills `episode` in when it is asked for one, so an episode
        project is walked episode by episode; otherwise every shot would come
        back unattached and land in the wrong branch of the tree.
        """
        shots = []
        try:
            if self._episodes:
                for episode in self.loadEpisodes():
                    for shot in self.core.entities.getShots(episode=episode) or []:
                        shot["episode"] = episode
                        shots.append(shot)
            else:
                shots = self.core.entities.getShots() or []
        except Exception:
            shots = []

        # Deliberately NOT filling in a blank "episode" key. Prism resolves an
        # entity's paths from this dict, and an empty episode resolves to
        # nothing at all - it silently costs you the thumbnail and the folder
        # shortcuts. Every reader here uses .get("episode", "") instead.
        for s in shots:
            s["type"] = "shot"
        return shots

    @err_catcher(name=__name__)
    def loadSequences(self):
        """All sequences, each carrying an "episode" key (see loadShots).

        Without this an episode project grows a second, empty copy of every
        sequence: the sequence dicts would say episode "" while the shots
        under them say something else, so the two never match up.
        """
        sequences = []
        try:
            if self._episodes:
                for episode in self.loadEpisodes():
                    for seq in self.core.entities.getSequences(episode=episode) or []:
                        seq["episode"] = episode
                        sequences.append(seq)
            else:
                sequences = self.core.entities.getSequences() or []
        except Exception:
            sequences = []

        # As in loadShots: never inject a blank episode key.
        return sequences

    @err_catcher(name=__name__)
    def loadAssets(self):
        try:
            assets = self.core.entities.getAssets()
        except Exception:
            assets = []
        for a in assets:
            a["type"] = "asset"
        return assets

    @err_catcher(name=__name__)
    def getAssigneeList(self):
        """Names offered in the assignee dropdowns.

        Order: the current (matched) user first, then the rest of the global
        team roster, then anyone already assigned in this project who isn't in
        the roster. Raw login names that match a roster user are hidden.
        """
        names = []
        if self.currentUser and self.currentUser.get("full_name"):
            names.append(self.currentUser["full_name"])
        for u in self.teamUsers:
            if u["full_name"] and u["full_name"] not in names:
                names.append(u["full_name"])

        # Set of everything the roster already covers (full names + usernames).
        known = set(n.lower() for n in names)
        for u in self.teamUsers:
            if u.get("username"):
                known.add(u["username"].lower())

        extras = set()
        for entity in self._existingEntities:
            meta = self.core.entities.getMetaData(entity)
            val = (meta.get(ASSIGNEE_KEY, {}).get("value") or "").strip()
            if val and val.lower() not in known:
                extras.add(val)
        return names + sorted(extras)

    @err_catcher(name=__name__)
    def addTaskAssignees(self):
        """Offer assignees that are only found on tasks.

        getAssigneeList() runs before the tree is built and only sees the
        shot/asset metadata. Task files are read while building, so anyone
        assigned to a task who is not on the roster is added to the assignee
        filter and every row's dropdown afterwards, the same as a stray name
        on a shot is in the other mode.
        """
        known = set(n.lower() for n in self.assigneeList)
        for u in self.teamUsers:
            if u.get("username"):
                known.add(u["username"].lower())
        extras = sorted(n for n in self._taskAssignees if n.lower() not in known)
        if not extras:
            return

        self.assigneeList.extend(extras)
        for item in self.iterLeafItems():
            cb = self.cellWidget(item, COL_ASSIGNEE)
            if cb is None:
                continue
            for name in extras:
                if cb.findText(name) == -1:
                    icon = self.avatarFor(name)
                    if icon is not None:
                        cb.addItem(icon, name)
                    else:
                        cb.addItem(name)
        self.populateAssigneeFilter()

    @err_catcher(name=__name__)
    def resolveAssignee(self, value):
        """Map a stored assignee to a roster full name.

        Older data may store a raw login name (e.g. 'jdoe'); show it as the
        matching team member's full name instead.
        """
        value = (value or "").strip()
        if not value:
            return ""
        for u in self.teamUsers:
            if u.get("full_name") and u["full_name"].lower() == value.lower():
                return u["full_name"]
        for u in self.teamUsers:
            if u.get("username") and u["username"].lower() == value.lower():
                return u["full_name"] or value
        return value

    @err_catcher(name=__name__)
    def reloadTeam(self):
        """Reload the roster (e.g. after the User Manager was used) and rebuild."""
        self.refresh(preserve=self.captureTreeState())

    @err_catcher(name=__name__)
    def avatarFor(self, name):
        """Cached avatar QIcon for an assignee name."""
        if not name:
            return None
        if name in self.avatarCache:
            return self.avatarCache[name]
        user = None
        for u in self.teamUsers:
            if u["full_name"] == name:
                user = u
                break
        color = user["color"] if user else self.team.colorForName(name)
        username = user["username"] if user else ""
        icon = QIcon(self.team.makeAvatar(name, color, username=username, size=22))
        self.avatarCache[name] = icon
        return icon

    # ------------------------------------------------------- row coloring ---
    @err_catcher(name=__name__)
    def loadViewPref(self, key, default=True):
        """Read one of the sticky view preferences from the Prism user config.

        These are per user rather than per project - how someone likes the
        list to look is not something to impose on the rest of the team.
        """
        try:
            val = self.core.getConfig("productiontracker", key, config="user")
        except Exception:
            val = None
        return default if val is None else bool(val)

    @err_catcher(name=__name__)
    def saveViewPref(self, key, val):
        try:
            self.core.setConfig(
                "productiontracker", key, val=bool(val), config="user"
            )
        except Exception:
            pass

    @err_catcher(name=__name__)
    def onToggleColorRows(self, checked):
        self.colorRowsByUser = bool(checked)
        self.saveViewPref("colorRowsByUser", self.colorRowsByUser)
        self.applyAllRowColors()

    @err_catcher(name=__name__)
    def onToggleThumbnails(self, checked):
        self.showThumbnails = bool(checked)
        self.saveViewPref("showThumbnails", self.showThumbnails)
        self.applyThumbnails()

    @err_catcher(name=__name__)
    def thumbFor(self, item):
        """A row's thumbnail, or None while thumbnails are switched off.

        The pixmap itself stays on the item either way, so toggling back on
        costs nothing - no entity has to be read again.
        """
        if not self.showThumbnails:
            return None
        base = item.data(0, ROLE_THUMB)
        return base if base and not base.isNull() else None

    @err_catcher(name=__name__)
    def refreshRollupIcon(self, item):
        """Rollup rows are not leaves, so they carry no comment badge."""
        base = self.thumbFor(item)
        item.setIcon(COL_NAME, QIcon(base) if base else QIcon())

    @err_catcher(name=__name__)
    def applyThumbnails(self):
        """Re-draw every row's name cell after the toggle."""
        for item in self.iterLeafItems():
            self.updateCommentIndicator(item)
        for item in self.rollupItems():
            self.refreshRollupIcon(item)

    # ---------------------------------------------------- comment indicator -
    @err_catcher(name=__name__)
    def loadReadState(self):
        """Read the per-user 'comments I've already seen' map (entity->epoch)."""
        try:
            val = self.core.getConfig(
                "productiontracker", "commentsRead", config="user"
            )
        except Exception:
            val = None
        return dict(val) if isinstance(val, dict) else {}

    @err_catcher(name=__name__)
    def flushReadState(self):
        try:
            self.core.setConfig(
                "productiontracker", "commentsRead", val=self._readState, config="user"
            )
        except Exception:
            pass

    @err_catcher(name=__name__)
    def entityReadKey(self, entity):
        """Stable string key for the read-state map from an entity."""
        return "|".join(str(p) for p in self.entityKey(entity))

    @err_catcher(name=__name__)
    def commentIndicatorState(self, item):
        """Return None (no comments), 'read', or 'unread' for a leaf item."""
        entity = item.data(0, ROLE_ENTITY) or {}
        if entity.get("type") == "fxlayer":
            return None
        notes = item.data(0, ROLE_NOTES) or {}
        if TrackerComments.active_count(notes) <= 0:
            return None
        latest = TrackerComments.latest_activity(
            notes, exclude_author=self.core.username
        )
        seen = self._readState.get(self.entityReadKey(entity), 0)
        return "unread" if latest > seen else "read"

    @err_catcher(name=__name__)
    def makeCommentBadge(self, size, unread):
        """Draw a small speech-bubble badge (yellow=unread, grey=all read)."""
        color = QColor("#facc15") if unread else QColor("#9ca3af")
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        radius = size * 0.22
        body = QRectF(size * 0.10, size * 0.12, size * 0.80, size * 0.58)
        bubble = QPainterPath()
        bubble.addRoundedRect(body, radius, radius)
        tail = QPainterPath()
        tail.moveTo(size * 0.30, body.bottom() - size * 0.02)
        tail.lineTo(size * 0.24, body.bottom() + size * 0.24)
        tail.lineTo(size * 0.50, body.bottom() - size * 0.02)
        tail.closeSubpath()
        shape = bubble.united(tail)
        p.setPen(QPen(QColor(15, 15, 15, 200), max(1.0, size * 0.05)))
        p.setBrush(QBrush(color))
        p.drawPath(shape)
        # Three little "text" dots inside the bubble.
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(15, 15, 15, 170)))
        dot = size * 0.06
        cy = body.center().y()
        for frac in (0.34, 0.50, 0.66):
            p.drawEllipse(QPointF(size * frac, cy), dot, dot)
        p.end()
        return pm

    @err_catcher(name=__name__)
    def updateCommentIndicator(self, item):
        """Refresh the comment bubble badge on a leaf row's name cell."""
        if item is None or not item.data(0, ROLE_ISLEAF):
            return
        entity = item.data(0, ROLE_ENTITY) or {}
        if entity.get("type") == "fxlayer":
            return
        base = self.thumbFor(item)
        state = self.commentIndicatorState(item)
        if state is None:
            # No comments: show the plain thumbnail (or nothing) and leave any
            # existing tooltip (e.g. the "new" hint) untouched.
            item.setIcon(COL_NAME, QIcon(base) if base and not base.isNull() else QIcon())
            return
        unread = state == "unread"
        if base and not base.isNull():
            pm = QPixmap(base)
            badgeSize = max(16, int(pm.height() * 0.46))
            badge = self.makeCommentBadge(badgeSize, unread)
            p = QPainter(pm)
            p.drawPixmap(pm.width() - badgeSize - 2, pm.height() - badgeSize - 2, badge)
            p.end()
            item.setIcon(COL_NAME, QIcon(pm))
        else:
            item.setIcon(COL_NAME, QIcon(self.makeCommentBadge(22, unread)))
        item.setToolTip(
            COL_NAME, "Unread comments" if unread else "Has comments (all read)"
        )

    @err_catcher(name=__name__)
    def markItemRead(self, item):
        """Record that the current user has now seen this row's comments."""
        if item is None or not item.data(0, ROLE_ISLEAF):
            return
        entity = item.data(0, ROLE_ENTITY) or {}
        if entity.get("type") == "fxlayer":
            return
        notes = item.data(0, ROLE_NOTES) or {}
        if TrackerComments.active_count(notes) > 0:
            key = self.entityReadKey(entity)
            newSeen = max(TrackerComments.latest_activity(notes), time.time())
            if self._readState.get(key, 0) < newSeen:
                self._readState[key] = newSeen
                self.readSaveTimer.start()
        self.updateCommentIndicator(item)

    @err_catcher(name=__name__)
    def colorForAssignee(self, name):
        """Return the roster color for an assignee full name, or None."""
        name = (name or "").strip()
        if not name:
            return None
        for u in self.teamUsers:
            if u.get("full_name") and u["full_name"] == name:
                return u.get("color")
        return None

    @err_catcher(name=__name__)
    def blendColor(self, hexColor, base=ROW_BG, alpha=0.2):
        """Blend a color over the row background at low opacity so text stays
        readable (a solid, pre-composited tint rather than a translucent fill)."""
        c = QColor(hexColor)
        b = QColor(base)
        r = int(round(b.red() * (1 - alpha) + c.red() * alpha))
        g = int(round(b.green() * (1 - alpha) + c.green() * alpha))
        bl = int(round(b.blue() * (1 - alpha) + c.blue() * alpha))
        return QColor(r, g, bl)

    @err_catcher(name=__name__)
    def applyRowColor(self, item):
        """Tint/fade a single leaf row based on its status/department/assignee.

        Priority: "Not needed" status fades the whole row (except the status
        dropdown itself); Completed + Approved gets a distinct dark-green
        tint; otherwise the normal per-assignee tint (if enabled) applies.
        """
        if item is None or not item.data(0, ROLE_ISLEAF):
            return
        statusCb = self.cellWidget(item, COL_STATUS)
        status = statusCb.currentText() if statusCb else ""
        deptCb = self.cellWidget(item, COL_DEPARTMENT)
        department = deptCb.currentText() if deptCb else ""

        entity = item.data(0, ROLE_ENTITY) or {}
        # A task row has no department of its own - the tree groups by it -
        # so being Completed is enough to count as wrapped up.
        endOfLine = (
            status == STATUS_COMPLETED
            and (entity.get("type") == "task" or department == APPROVED_DEPT)
        )

        faded = status == STATUS_NOT_NEEDED
        color = None
        if not faded:
            if endOfLine:
                color = self.blendColor(COMPLETE_APPROVED_COLOR, alpha=COMPLETE_APPROVED_ALPHA)
            elif self.colorRowsByUser and self.userMgmtEnabled:
                assigneeCb = self.cellWidget(item, COL_ASSIGNEE)
                name = assigneeCb.currentText().strip() if assigneeCb else ""
                hexColor = self.colorForAssignee(name)
                if hexColor:
                    color = self.blendColor(hexColor)

        for col in range(COL_COUNT):
            item.setData(col, ROLE_ROWCOLOR, color)
            item.setData(col, ROLE_FADEROW, faded)
            if col == COL_STATUS:
                continue
            holder = self.tree.itemWidget(item, col)
            if holder is None:
                continue
            if faded:
                effect = holder.graphicsEffect()
                if not isinstance(effect, QGraphicsOpacityEffect):
                    effect = QGraphicsOpacityEffect(holder)
                    holder.setGraphicsEffect(effect)
                effect.setOpacity(FADE_OPACITY)
            elif holder.graphicsEffect() is not None:
                holder.setGraphicsEffect(None)
        if not self._building and self.core.isObjectValid(self.tree):
            self.tree.viewport().update()

    @err_catcher(name=__name__)
    def applyAllRowColors(self):
        for item in self.iterLeafItems():
            self.applyRowColor(item)

    @err_catcher(name=__name__)
    def getEntityDescription(self, entity, meta=None):
        """Read the Prism description for an entity.

        Assets store it in Assetinfo; shots store it in metadata['Description'].
        """
        if entity.get("type") == "asset":
            try:
                assetName = self.core.entities.getAssetNameFromPath(entity["asset_path"])
                return self.core.entities.getAssetDescription(assetName) or ""
            except Exception:
                return ""
        if meta is None:
            meta = self.core.entities.getMetaData(entity)
        return meta.get(DESC_KEY, {}).get("value", "") if meta else ""

    @err_catcher(name=__name__)
    def usesEpisodes(self):
        try:
            return bool(self.core.projects.getUseEpisodes())
        except Exception:
            return False

    # ------------------------------------------------------------- refresh --
    @err_catcher(name=__name__)
    def refresh(self, preserve=None):
        if not getattr(self.core, "projectPath", None):
            self.core.popup("No project is currently active in Prism.", parent=self)
            return

        # Read the latest data from disk so we show other users' changes.
        try:
            self.core.configs.clearCache()
        except Exception:
            pass

        self._building = True

        # Project team roster + who "I" am on this machine. With user
        # management off the roster is empty, currentUser is None and comments
        # fall back to the plain Prism username (see currentTrackerUsername).
        self.loadOptions()
        self.userMgmtEnabled = self.settings.isEnabled()
        self.teamUsers = self.team.loadUsers()
        self.currentUser = self.team.resolveCurrentUser(self.teamUsers)
        self.avatarCache = {}
        self.commentsPanel.setUsers(self.teamUsers)
        self.commentsPanel.setMentionsEnabled(self.userMgmtEnabled)
        self.applyUserManagementVisibility()
        self.applyModeVisibility()
        self.commentsPanel.setCurrentUser(
            self.currentTrackerUsername(), self.displayNameFor(self.currentTrackerUsername())
        )

        self._episodes = self.usesEpisodes()
        shots = self.loadShots()
        sequences = self.loadSequences()
        assets = self.loadAssets()

        self._existingEntities = shots + assets

        # One-time migration per window: data saved before the "show" flag was
        # fixed makes Prism's own Scene Browser raise the moment such an
        # entity is selected, so repair it as soon as we have the entity list
        # rather than waiting for the user to hit Save.
        if not self._repairedShowFlags:
            self._repairedShowFlags = True
            self.repairShowFlags()

        self.warnDuplicateAssetNames(assets)
        self.assigneeList = self.getAssigneeList()

        self.populateAssigneeFilter()

        # The tree is rebuilt below, so any current notes binding is stale.
        self._notesItem = None
        self.tree.clear()

        # Rebuilt below as task mode reads each task (see watchPath).
        self._taskWatch = {}
        self._taskWatchGen += 1
        self._taskPollResult = None
        self._taskChangesPending = False
        self._taskAssignees = set()

        self.shotsRoot = self.makeSectionItem("SHOTS")
        self.buildShotTree(sequences, shots)

        self.assetsRoot = self.makeSectionItem("ASSETS")
        self.buildAssetTree(assets)

        # Re-attach still-unsaved new rows.
        for entity in self.newRows:
            if entity.get("type") == "shot":
                parent = self.ensureShotParent(entity)
            else:
                parent, entity["_leafName"] = self.ensureAssetParent(entity["asset_path"])
            self.addEntityItem(parent, entity, isNew=True)

        self.addTaskAssignees()
        self.updateNodeCounts()
        if preserve:
            self.restoreTreeState(preserve)
        else:
            self.tree.expandToDepth(3 if self.taskMode() else 2)
        self.refreshAllRollups()
        self.applyFilter()
        self.updateStatusLabel()
        self.updateNotesPanel()
        self.applyAllRowColors()
        self._lastSyncMtimes = self.currentMtimes()
        self._building = False

    @err_catcher(name=__name__)
    def populateAssigneeFilter(self):
        """Fill the assignee filter dropdown, preserving the current choice."""
        cb = self.cb_filterAssignee
        prev = cb.currentData() if cb.count() else "__all__"
        cb.blockSignals(True)
        cb.clear()
        cb.addItem("All assignees", "__all__")
        if self.currentUser and self.currentUser.get("full_name"):
            icon = self.avatarFor(self.currentUser["full_name"])
            label = "Me (%s)" % self.currentUser["full_name"]
            if icon is not None:
                cb.addItem(icon, label, "__me__")
            else:
                cb.addItem(label, "__me__")
        for name in self.assigneeList:
            icon = self.avatarFor(name)
            if icon is not None:
                cb.addItem(icon, name, name)
            else:
                cb.addItem(name, name)
        cb.addItem("Unassigned", "__unassigned__")
        idx = cb.findData(prev)
        cb.setCurrentIndex(idx if idx >= 0 else 0)
        cb.blockSignals(False)

    # ---------------------------------------------------------- live sync ---
    @err_catcher(name=__name__)
    def cfgPaths(self):
        """Paths of the shared config files that hold tracker data."""
        paths = []
        for cfg in ("shotinfo", "assetinfo"):
            try:
                p = self.core.configs.getConfigPath(config=cfg)
            except Exception:
                p = None
            if p:
                paths.append(p)
        return paths

    @err_catcher(name=__name__)
    def currentMtimes(self):
        mtimes = {}
        for p in self.cfgPaths():
            try:
                mtimes[p] = os.path.getmtime(p) if os.path.exists(p) else 0
            except Exception:
                mtimes[p] = 0
        return mtimes

    @err_catcher(name=__name__)
    def onSyncTick(self):
        # Runs in the background; only flags _taskChangesPending.
        self.pollTaskFiles()
        # Don't disturb the user while a dropdown/calendar popup is open.
        if self._syncing or QApplication.activePopupWidget() is not None:
            return
        # Don't rebuild while a local edit is still waiting to be auto-saved,
        # otherwise the rebuild would discard the in-progress change.
        if self.saveTimer.isActive():
            return
        # Don't rebuild while the user is actively editing a cell.
        fw = QApplication.focusWidget()
        if fw is not None and self.tree.isAncestorOf(fw):
            return
        if not getattr(self.core, "projectPath", None):
            return
        if self._taskChangesPending or self.currentMtimes() != self._lastSyncMtimes:
            self.liveSync()

    # ---------------------------------------------------- task mode watch ---
    @err_catcher(name=__name__)
    def watchPath(self, path):
        """Record a path's current mtime for task-mode change detection."""
        if path:
            path = os.path.normpath(path)
            self._taskWatch[path] = statMtime(path)

    @err_catcher(name=__name__)
    def markTaskFileSeen(self, entity, department, task):
        """After writing a task file ourselves, so it isn't taken for a
        change by someone else and trigger a needless rebuild."""
        self.watchPath(self.tasks.dataPath(entity, department, task))

    @err_catcher(name=__name__)
    def pollTaskFiles(self):
        """Task mode: notice what other users changed in task files.

        Shot mode data lives in two files that are cheap to check every
        tick, but task mode keeps each task in its own file, plus new tasks
        and departments appear as new folders. Checking those is only a
        timestamp read per path, but there can be hundreds on a network
        share, so it runs on a background thread every TASK_POLL_INTERVAL
        seconds. This picks up the finished result on a later tick and flags
        a rebuild, which onSyncTick() performs once nobody is mid-edit.

        This is only about seeing other people's work sooner: saving never
        depends on it, because every save merges against the files as they
        are at that moment (see mergeRowValues).
        """
        if not self.taskMode() or not self._taskWatch:
            return

        result = self._taskPollResult
        if result is not None:
            self._taskPollResult = None
            gen, mtimes = result
            # A result started before the last rebuild compares against the
            # wrong baseline; drop it.
            if gen == self._taskWatchGen and any(
                self._taskWatch.get(p) != m for p, m in mtimes.items()
            ):
                self._taskChangesPending = True

        if self._taskPollThread is not None and self._taskPollThread.is_alive():
            return
        if time.time() - self._lastTaskPoll < TASK_POLL_INTERVAL:
            return
        self._lastTaskPoll = time.time()

        paths = list(self._taskWatch)
        gen = self._taskWatchGen

        def run():
            # Plain file-system calls only - nothing here may touch Qt.
            self._taskPollResult = (gen, dict((p, statMtime(p)) for p in paths))

        self._taskPollThread = threading.Thread(
            target=run, name="ProductionTrackerTaskPoll"
        )
        self._taskPollThread.daemon = True
        self._taskPollThread.start()

    @err_catcher(name=__name__)
    def liveSync(self):
        self._syncing = True
        try:
            state = self.captureTreeState()
            # If we couldn't safely snapshot local edits, skip this sync rather
            # than rebuild and risk discarding unsaved changes.
            if state is None:
                return
            self.refresh(preserve=state)
            import datetime

            stamp = datetime.datetime.now().strftime("%H:%M:%S")
            base = self.l_status.text().split("   —")[0]
            self.l_status.setText("%s   —  synced remote changes at %s" % (base, stamp))
        finally:
            self._syncing = False

    # -------------------------------------------------------- state helpers -
    @err_catcher(name=__name__)
    def entityKey(self, entity):
        if entity.get("type") == "shot":
            return ("shot", entity.get("episode", ""), entity.get("sequence", ""), entity.get("shot", ""))
        if entity.get("type") == "fxlayer":
            return ("fxlayer", entity.get("fx_id", ""))
        if entity.get("type") == "task":
            return (
                "task",
                self.entityKey(entity.get("parent") or {}),
                entity.get("department", ""),
                entity.get("task", ""),
            )
        # Prism hands back asset paths with the OS separator ("Env\\TreeA" on
        # Windows), so normalise or the same asset keys differently depending
        # on where the dict came from.
        return ("asset", (entity.get("asset_path") or "").replace("\\", "/"))

    @err_catcher(name=__name__)
    def nodePath(self, item):
        parts = []
        cur = item
        while cur is not None and cur is not self.tree.invisibleRootItem():
            if cur.data(0, ROLE_ISLEAF):
                parts.append(("leaf", cur.text(COL_NAME)))
            else:
                parts.append(("node", cur.text(COL_NAME).split("  (")[0]))
            cur = cur.parent()
        return tuple(reversed(parts))

    @err_catcher(name=__name__)
    def isDirty(self, item):
        if item.data(0, ROLE_ISNEW):
            return True
        snap = item.data(0, ROLE_SNAPSHOT) or {}
        vals = self.getRowValues(item)
        return any(
            vals.get(k) != snap.get(k)
            for k in ("assignee", "status", "department", "due", "range", "desc", "notes", "name")
        )

    @err_catcher(name=__name__)
    def captureTreeState(self):
        state = {
            "dirty": {},
            "expanded": set(),
            "scroll": self.tree.verticalScrollBar().value(),
            "selected": None,
        }

        # Only the fields actually edited are kept, so re-applying them after
        # the rebuild can't put stale copies of the other fields back over
        # newer values just read from disk. New rows themselves are rebuilt
        # from self.newRows, but what has been typed into them only survives
        # by being captured here too.
        for item in self.iterLeafItems() + self.rollupItems():
            if not self.isDirty(item):
                continue
            snap = item.data(0, ROLE_SNAPSHOT) or {}
            edited = {
                k: v for k, v in self.getRowValues(item).items() if v != snap.get(k)
            }
            if edited:
                state["dirty"][self.entityKey(item.data(0, ROLE_ENTITY))] = edited

        def walkNodes(parent):
            for i in range(parent.childCount()):
                child = parent.child(i)
                if not child.data(0, ROLE_ISLEAF):
                    if child.isExpanded():
                        state["expanded"].add(self.nodePath(child))
                    walkNodes(child)

        walkNodes(self.tree.invisibleRootItem())

        sel = self.tree.currentItem()
        if sel is not None and (sel.data(0, ROLE_ISLEAF) or sel.data(0, ROLE_ROLLUP)):
            state["selected"] = self.entityKey(sel.data(0, ROLE_ENTITY))
        return state

    @err_catcher(name=__name__)
    def restoreTreeState(self, state):
        # Re-apply unsaved local edits so live sync never discards them.
        dirty = state.get("dirty", {})
        selectItem = None
        for item in self.iterLeafItems() + self.rollupItems():
            entity = item.data(0, ROLE_ENTITY)
            key = self.entityKey(entity)
            if key in dirty:
                values = dict(dirty[key])
                if "notes" in values:
                    # Keep comments that arrived with the rebuild as well.
                    values["notes"] = TrackerComments.merge_comments(
                        item.data(0, ROLE_NOTES), values["notes"]
                    )
                self.setRowValues(item, values)
            if state.get("selected") is not None and key == state["selected"]:
                selectItem = item

        # Restore folder expansion.
        expanded = state.get("expanded", set())

        def walkNodes(parent):
            for i in range(parent.childCount()):
                child = parent.child(i)
                if not child.data(0, ROLE_ISLEAF):
                    child.setExpanded(self.nodePath(child) in expanded)
                    walkNodes(child)

        if expanded:
            walkNodes(self.tree.invisibleRootItem())
        else:
            self.tree.expandToDepth(3 if self.taskMode() else 2)

        if selectItem is not None:
            self.tree.setCurrentItem(selectItem)

        self.tree.verticalScrollBar().setValue(state.get("scroll", 0))

    @err_catcher(name=__name__)
    def setRowValues(self, item, values):
        """Show `values` in a row's widgets.

        Only the keys present are applied, and a widget that already shows
        the value is left alone, so a field someone is typing in keeps its
        cursor when a save or sync pushes the same value back into it.
        """
        entity = item.data(0, ROLE_ENTITY) or {}

        def setText(widget, key):
            if widget is not None and key in values and widget.text() != values[key]:
                widget.setText(values[key])

        def setCombo(widget, key):
            if (
                widget is not None
                and key in values
                and widget.currentText() != values[key]
            ):
                widget.setCurrentText(values[key])

        setCombo(self.cellWidget(item, COL_ASSIGNEE), "assignee")
        setCombo(self.cellWidget(item, COL_STATUS), "status")
        setCombo(self.cellWidget(item, COL_DEPARTMENT), "department")

        dueW = self.cellWidget(item, COL_DUE)
        if dueW is not None and "due" in values and dueW.isoDate() != values["due"]:
            iso = values["due"]
            if iso:
                d = QDate.fromString(iso, "yyyy-MM-dd")
                if d.isValid():
                    dueW.dateEdit.setDate(d)
            else:
                dueW.clear()

        if entity.get("type") == "shot":
            setText(self.cellWidget(item, COL_RANGE), "range")
        if entity.get("type") == "fxlayer":
            setText(self.cellWidget(item, COL_NAME), "name")
        setText(self.cellWidget(item, COL_DESC), "desc")

        if "notes" in values:
            notesVal = values["notes"] or TrackerComments.empty_data()
            item.setData(0, ROLE_NOTES, notesVal)
            self.updateCommentIndicator(item)
            if item is self._notesItem:
                self.commentsPanel.refreshData(notesVal)

    # ---------------------------------------------------------- notes panel -
    @err_catcher(name=__name__)
    def updateNotesPanel(self):
        items = self.tree.selectedItems()
        item = items[0] if items else None
        if item is None or not item.data(0, ROLE_ISLEAF):
            self._notesItem = None
            self.commentsPanel.clearEntity()
            return

        entity = item.data(0, ROLE_ENTITY)
        self._notesItem = item
        if entity.get("type") == "fxlayer":
            nameEdit = self.cellWidget(item, COL_NAME)
            fxName = (nameEdit.text().strip() if nameEdit else "") or "FX layer"
            parent = item.parent()
            shotName = parent.text(COL_NAME) if parent is not None else ""
            label = "%s / %s (VFX)" % (shotName, fxName) if shotName else "%s (VFX)" % fxName
        elif entity.get("type") == "task":
            parent = entity.get("parent") or {}
            where = self.leafDisplayName(parent, False)
            if parent.get("type") == "shot" and parent.get("sequence"):
                where = "%s / %s" % (parent["sequence"], where)
            label = "%s / %s / %s" % (
                where,
                entity.get("departmentLabel") or entity.get("department", ""),
                entity.get("task", ""),
            )
        else:
            label = self.leafDisplayName(entity, item.data(0, ROLE_ISNEW))
            if entity.get("type") == "shot" and entity.get("sequence"):
                label = "%s / %s" % (entity["sequence"], label)
        notesVal = item.data(0, ROLE_NOTES) or TrackerComments.empty_data()
        self.commentsPanel.setEntity(label, notesVal)
        # Viewing a row's comments counts as reading them.
        self.markItemRead(item)

    @err_catcher(name=__name__)
    def onCommentsChanged(self):
        if self._notesItem is None:
            return
        self._notesItem.setData(0, ROLE_NOTES, copy.deepcopy(self.commentsPanel.getData()))
        # The user just authored/edited here, so treat it as read and refresh
        # the row's comment bubble.
        self.markItemRead(self._notesItem)
        self.scheduleAutoSave()

    # ----------------------------------------------------------- auto-save --
    @err_catcher(name=__name__)
    def scheduleAutoSave(self, *args):
        """(Re)start the debounced auto-save countdown after an edit."""
        if self._building or self._applyingRemote:
            return
        self.saveTimer.start()

    @err_catcher(name=__name__)
    def flushAutoSave(self):
        """Write any changed existing rows. New rows are left for explicit Save."""
        if self._syncing:
            self.saveTimer.start()  # try again shortly
            return
        try:
            self.core.configs.clearCache()
        except Exception:
            pass

        saved = 0
        errors = []
        self._conflicts = []
        for item in self.iterLeafItems():
            if item.data(0, ROLE_ISNEW):
                continue
            ok, errs = self.writeExistingItem(item)
            if ok:
                saved += 1
            errors.extend(errs)

        for item in self.rollupItems():
            if item.data(0, ROLE_ISNEW):
                continue
            ok, errs = self.writeRollupItem(item)
            if ok:
                saved += 1
            errors.extend(errs)

        fxSaved, fxErrs = self.saveAllFxLayers()
        saved += fxSaved
        errors.extend(fxErrs)

        if saved:
            self._lastSyncMtimes = self.currentMtimes()
            import datetime

            stamp = datetime.datetime.now().strftime("%H:%M:%S")
            self.l_status.setText("Auto-saved %d change(s) at %s" % (saved, stamp))
        if self._conflicts:
            self.l_status.setText(self.conflictMessage())
        if errors:
            self.l_status.setText("Auto-save issue: %s" % errors[0])

    # ------------------------------------------------------ comments/notify -
    @err_catcher(name=__name__)
    def currentTrackerUsername(self):
        """The identifier comments/mentions/reminders are authored/keyed as.

        Prefers the roster username (so it lines up with mentions and the
        assignee dropdown); falls back to the raw Prism username so people
        who aren't in the roster yet can still comment.
        """
        if self.currentUser and self.currentUser.get("username"):
            return self.currentUser["username"]
        return getattr(self.core, "username", "") or "unknown"

    @err_catcher(name=__name__)
    def displayNameFor(self, username):
        for u in self.teamUsers:
            if (u.get("username") or "").lower() == (username or "").lower():
                return u.get("full_name") or username
        return username or "Someone"

    @err_catcher(name=__name__)
    def notifyEntityLabel(self, entity):
        """Self-contained 'where is this comment' label for notification
        emails: project name plus the sequence/shot or full asset path, so a
        mention/reply/reminder email is understandable without opening the
        tracker first.
        """
        project = getattr(self.core, "projectName", "") or ""
        if entity.get("type") == "task":
            parentLabel = self.notifyEntityLabel(entity.get("parent") or {})
            return "%s / %s / %s" % (
                parentLabel,
                entity.get("departmentLabel") or entity.get("department", ""),
                entity.get("task", ""),
            )
        if entity.get("type") == "shot":
            seq = entity.get("sequence") or ""
            shot = entity.get("shot") or ""
            where = "%s / %s" % (seq, shot) if seq else shot
        elif entity.get("type") == "asset":
            where = entity.get("asset_path") or self.leafDisplayName(entity, False)
        else:
            where = self.leafDisplayName(entity, False)
        return "%s / %s" % (project, where) if project else where

    @err_catcher(name=__name__)
    def processNotesForSave(self, entity, label, remoteMeta, values, snapshot):
        """Merge local comment edits with the freshest remote copy and fire
        any mention / reply / remind-me notification emails triggered by
        content genuinely added in THIS save.

        Only content found in `values["notes"]` but absent from the just-read
        `remoteMeta` is treated as "new" for notification purposes, so two
        Prism sessions saving around the same time never both email for the
        same remote change - each session only ever notifies for what it
        itself is committing.
        """
        remoteNotes = TrackerComments.load_notes_value(
            (remoteMeta or {}).get(NOTES_KEY, {}).get("value")
        )
        localNotes = values.get("notes") or TrackerComments.empty_data()

        # With user management off there is nobody to notify: merge and go.
        if not self.userMgmtEnabled:
            return TrackerComments.merge_comments(remoteNotes, localNotes)

        users = self.teamUsers

        newComments, newReplies = TrackerComments.find_new_items(remoteNotes, localNotes)
        newlyChecked = TrackerComments.find_newly_checked(remoteNotes, localNotes)

        for c in newComments:
            byName = self.displayNameFor(c.get("author"))
            for uname in c.get("mentions") or []:
                if uname and uname.lower() != (c.get("author") or "").lower():
                    self.notify.notifyMention(uname, byName, label, c.get("text", ""), users)

        for parent, reply in newReplies:
            byName = self.displayNameFor(reply.get("author"))
            parentAuthor = parent.get("author")
            if parentAuthor and parentAuthor.lower() != (reply.get("author") or "").lower():
                self.notify.notifyReply(parentAuthor, byName, label, reply.get("text", ""), users)
            for uname in reply.get("mentions") or []:
                if uname and uname.lower() != (reply.get("author") or "").lower():
                    self.notify.notifyMention(uname, byName, label, reply.get("text", ""), users)
            for rem in parent.get("reminders") or []:
                if rem.get("type") == "any_reply":
                    sub = rem.get("subscriber")
                    if sub and sub.lower() != (reply.get("author") or "").lower():
                        self.notify.notifyReminder(
                            sub, "a reply was posted", label, reply.get("text", ""), users
                        )

        for comment, _line, taskLabel in newlyChecked:
            for rem in comment.get("reminders") or []:
                if rem.get("type") == "todo_done" and rem.get("subscriber"):
                    self.notify.notifyReminder(
                        rem["subscriber"], "a to-do was completed", label, taskLabel, users
                    )

        # Status / department transitions: fire matching reminders on every
        # comment attached to this entity. Uses the freshly-read remote value
        # as "before", so this only fires for the change *this* client is
        # committing right now (checked at save/close time, per design).
        remoteStatus = (remoteMeta or {}).get(STATUS_KEY, {}).get("value", STATUS_NOT_STARTED)
        newStatus = values.get("status", STATUS_NOT_STARTED)
        remoteDept = (remoteMeta or {}).get(DEPT_KEY, {}).get("value", DEPT_NONE)
        remoteDept = DEPARTMENT_RENAMES.get(remoteDept, remoteDept)
        newDept = values.get("department", DEPT_NONE)

        def allComments(data):
            for c in data.get("comments") or []:
                yield c
                for r in c.get("replies") or []:
                    yield r

        if remoteStatus != newStatus:
            for c in allComments(localNotes):
                for rem in c.get("reminders") or []:
                    sub = rem.get("subscriber")
                    if not sub:
                        continue
                    if rem.get("type") == "status_change" or (
                        rem.get("type") == "status_is" and rem.get("value") == newStatus
                    ):
                        self.notify.notifyReminder(
                            sub, "status changed to '%s'" % newStatus, label, "", users
                        )

        if remoteDept != newDept:
            for c in allComments(localNotes):
                for rem in c.get("reminders") or []:
                    if rem.get("type") == "dept_change" and rem.get("subscriber"):
                        self.notify.notifyReminder(
                            rem["subscriber"], "department changed to '%s'" % newDept, label, "", users
                        )

        return TrackerComments.merge_comments(remoteNotes, localNotes)

    # --------------------------------------------------------------- merge --
    @err_catcher(name=__name__)
    def shortEntityLabel(self, entity):
        """notifyEntityLabel() without the project name, for the status bar."""
        label = self.notifyEntityLabel(entity)
        project = getattr(self.core, "projectName", "") or ""
        prefix = project + " / "
        return label[len(prefix):] if project and label.startswith(prefix) else label

    @err_catcher(name=__name__)
    def entityFieldValues(self, entity, meta, isNew=False):
        """A shot/asset's stored fields, normalised the way its row shows them."""
        department = meta.get(DEPT_KEY, {}).get("value", DEPT_NONE)
        return {
            "assignee": self.resolveAssignee(meta.get(ASSIGNEE_KEY, {}).get("value", "")),
            "status": meta.get(STATUS_KEY, {}).get("value", STATUS_NOT_STARTED),
            "department": DEPARTMENT_RENAMES.get(department, department),
            "due": meta.get(DUE_KEY, {}).get("value", ""),
            "range": (
                self.shotRangeText(entity, isNew) if entity.get("type") == "shot" else ""
            ),
            "desc": "" if isNew else self.getEntityDescription(entity, meta),
        }

    @err_catcher(name=__name__)
    def taskFieldValues(self, data):
        """A task's stored fields (from its info file), normalised likewise."""
        return {
            "assignee": self.resolveAssignee(data.get(TASK_KEYS["assignee"], "") or ""),
            "status": data.get(TASK_KEYS["status"], "") or STATUS_NOT_STARTED,
            "due": data.get(TASK_KEYS["due"], "") or "",
            "desc": data.get(TASK_KEYS["desc"], "") or "",
        }

    @err_catcher(name=__name__)
    def mergeRowValues(self, values, snapshot, stored, fields, label):
        """Three-way merge of a row against what is stored right now.

        `values` is what the row shows, `snapshot` what was stored when this
        window last read or saved it, `stored` what is stored at this moment.

        A field this user left alone takes the stored value, so someone
        else's newer change to it survives instead of being overwritten by
        this window's stale copy. A field both changed keeps this user's value
        (the later save wins) and is recorded in self._conflicts so the
        status bar can say so.
        """
        merged = dict(values)
        for key in fields:
            if key not in stored:
                continue
            mine, base, theirs = values.get(key), snapshot.get(key), stored[key]
            if mine == base:
                merged[key] = theirs
            elif theirs != base and theirs != mine:
                self._conflicts.append("%s (%s)" % (label, key))
        return merged

    @err_catcher(name=__name__)
    def applySavedValues(self, item, values):
        """After a save: show the merged result and make it the new baseline.

        Fields merged in from someone else's save are pushed into the
        widgets, and the snapshot is taken from the widgets afterwards so it
        matches exactly what getRowValues() will report on the next check.
        """
        self._applyingRemote = True
        try:
            self.setRowValues(item, values)
        finally:
            self._applyingRemote = False
        snapshot = self.getRowValues(item)
        snapshot["notes"] = copy.deepcopy(snapshot.get("notes"))
        item.setData(0, ROLE_SNAPSHOT, snapshot)

    @err_catcher(name=__name__)
    def conflictMessage(self):
        """Status bar note for fields both this user and someone else changed."""
        if not self._conflicts:
            return ""
        shown = self._conflicts[:3]
        more = len(self._conflicts) - len(shown)
        return "Also changed by someone else, your value was kept: %s%s" % (
            ", ".join(shown),
            " and %d more" % more if more > 0 else "",
        )

    # ---------------------------------------------------------------- write --
    @err_catcher(name=__name__)
    def writeExistingItem(self, item):
        """Persist a single existing entity's changed fields; updates snapshot.

        Every field is merged against a fresh read (see mergeRowValues), so
        only what this user changed is written and whatever someone else has
        saved to the other fields in the meantime survives. Comments merge by
        id against the same fresh copy, so a concurrent comment/reply from
        another session is never clobbered either.

        Returns (savedBool, [errorStrings]).
        """
        entity = item.data(0, ROLE_ENTITY)
        snapshot = item.data(0, ROLE_SNAPSHOT) or {}
        values = self.getRowValues(item)
        errors = []

        # FX layers aren't Prism entities; they're persisted on their parent
        # shot's metadata via saveAllFxLayers(), not written individually.
        if entity.get("type") == "fxlayer":
            return (False, errors)

        # Task rows live in the task's own info file, not in entity metadata.
        if entity.get("type") == "task":
            return self.writeTaskItem(item, entity, snapshot, values)

        if all(values.get(k) == snapshot.get(k) for k in ENTITY_FIELDS + ("notes",)):
            return (False, errors)

        try:
            meta = self.core.entities.getMetaData(entity) or {}
            stored = self.entityFieldValues(entity, meta)
            values = self.mergeRowValues(
                values, snapshot, stored, ENTITY_FIELDS, self.shortEntityLabel(entity)
            )
            errors.extend(self.writeRangeAndAssetDesc(entity, values, stored))
            values["notes"] = self.processNotesForSave(
                entity, self.notifyEntityLabel(entity), meta, values, snapshot
            )
            meta[STATUS_KEY] = {"value": values["status"], "show": True}
            meta[ASSIGNEE_KEY] = {"value": values["assignee"], "show": True}
            meta[DEPT_KEY] = {"value": values["department"], "show": True}
            meta[DUE_KEY] = {"value": values["due"], "show": True}
            meta[NOTES_KEY] = {"value": values["notes"], "show": False}
            if entity.get("type") == "shot":
                meta[DESC_KEY] = {"value": values["desc"], "show": True}
            self.core.entities.setMetaData(entity=entity, metaData=meta)
        except Exception as e:
            errors.append("%s (meta): %s" % (self.leafDisplayName(entity, False), e))
            return (False, errors)

        self.applySavedValues(item, values)
        return (True, errors)

    @err_catcher(name=__name__)
    def writeRangeAndAssetDesc(self, entity, values, stored):
        """Write a shot's frame range / an asset's description if they differ
        from what is stored.

        Both go through Prism's own setters rather than the metadata block,
        and are shared by the two tracker modes. Returns [errorStrings].
        """
        errors = []
        if entity.get("type") == "shot" and values["range"] != stored.get("range"):
            frameRange = self.parseRange(values["range"])
            if frameRange:
                try:
                    self.core.entities.setShotRange(entity, frameRange[0], frameRange[1])
                except Exception as e:
                    errors.append("%s (range): %s" % (entity.get("shot"), e))

        # Asset descriptions live in Assetinfo (separate from metadata).
        if entity.get("type") == "asset" and values["desc"] != stored.get("desc"):
            try:
                assetName = self.core.entities.getAssetNameFromPath(entity["asset_path"])
                self.core.entities.setAssetDescription(assetName, values["desc"])
            except Exception as e:
                errors.append("%s (desc): %s" % (self.leafDisplayName(entity, False), e))
        return errors

    @err_catcher(name=__name__)
    def writeRollupItem(self, item):
        """Persist a task-mode shot/asset row's own fields.

        Only the description and (for shots) the frame range belong to the
        entity in task mode, and they are written to the same place the
        one-row-per-shot mode uses, so both modes show the same values. The
        shot description is patched into the metadata on its own, leaving
        the shot-mode status/assignee/etc. keys in there untouched.

        Returns (savedBool, [errorStrings]).
        """
        entity = item.data(0, ROLE_ENTITY)
        snapshot = item.data(0, ROLE_SNAPSHOT) or {}
        values = self.getRowValues(item)
        fields = ("desc", "range")
        if all(values.get(k) == snapshot.get(k) for k in fields):
            return (False, [])

        errors = []
        try:
            meta = self.core.entities.getMetaData(entity) or {}
            stored = self.entityFieldValues(entity, meta)
            values = self.mergeRowValues(
                values, snapshot, stored, fields, self.shortEntityLabel(entity)
            )
            errors.extend(self.writeRangeAndAssetDesc(entity, values, stored))
            if entity.get("type") == "shot" and values["desc"] != stored["desc"]:
                meta[DESC_KEY] = {"value": values["desc"], "show": True}
                self.core.entities.setMetaData(entity=entity, metaData=meta)
        except Exception as e:
            errors.append("%s (desc): %s" % (self.leafDisplayName(entity, False), e))

        if errors:
            # Keep the old snapshot so the next save retries.
            return (False, errors)

        self.applySavedValues(item, values)
        return (True, errors)

    @err_catcher(name=__name__)
    def writeTaskItem(self, item, entity, snapshot, values):
        """Persist one task row into its own info file.

        Mirrors writeExistingItem: only writes when something changed, merges
        every field against the file as it is right now, and writes only the
        keys that end up different, so keys added by Prism or by another
        artist survive and comments merge instead of overwriting a
        concurrent reply.

        Returns (savedBool, [errorStrings]).
        """
        errors = []
        if all(values.get(k) == snapshot.get(k) for k in TASK_FIELDS + ("notes",)):
            return (False, errors)

        parent = entity.get("parent") or {}
        department = entity.get("department", "")
        task = entity.get("task", "")

        try:
            remote = self.tasks.read(parent, department, task)
            stored = self.taskFieldValues(remote)
            values = self.mergeRowValues(
                values, snapshot, stored, TASK_FIELDS, self.shortEntityLabel(entity)
            )
            # processNotesForSave expects Prism's metadata shape, so the flat
            # task keys are wrapped to match before merging/notifying.
            remoteNotes = remote.get(TASK_KEYS["notes"])
            remoteMeta = {
                NOTES_KEY: {"value": remoteNotes},
                STATUS_KEY: {"value": stored["status"]},
                DEPT_KEY: {"value": DEPT_NONE},
            }
            values["notes"] = self.processNotesForSave(
                entity, self.notifyEntityLabel(entity), remoteMeta, values, snapshot
            )

            changes = {
                TASK_KEYS[k]: values[k] for k in TASK_FIELDS if values[k] != stored[k]
            }
            if values["notes"] != TrackerComments.load_notes_value(remoteNotes):
                changes[TASK_KEYS["notes"]] = values["notes"]
            if changes:
                self.tasks.write(parent, department, task, changes)
                self.markTaskFileSeen(parent, department, task)
        except Exception as e:
            errors.append("%s / %s: %s" % (department, task, e))
            return (False, errors)

        self.applySavedValues(item, values)
        self.refreshRollupFor(item)
        return (True, errors)

    # ------------------------------------------------------------ add task --
    @err_catcher(name=__name__)
    def addTaskTo(self, item, department=""):
        """Create a real Prism department/task under a shot or asset."""
        entity = self.entityForNode(item)
        if entity.get("type") not in ("shot", "asset"):
            return

        dlg = AddTaskDialog(
            self.core, self.tasks, entity, parent=self, department=department
        )
        if dlg.exec_() != QDialog.Accepted:
            return

        # Create what we can and report the rest, rather than stopping at the
        # first failure and leaving the batch half done with no explanation.
        created = 0
        errors = []
        for row in dlg.getRows():
            error = self.tasks.createTask(entity, row["department"], row["task"])
            if error:
                errors.append("%s / %s: %s" % (row["department"], row["task"], error))
            else:
                created += 1

        if created:
            self.refresh(preserve=self.captureTreeState())
            self.refreshPrismUI()
        if errors:
            self.core.popup(
                "Created %d task(s). These failed:\n\n%s"
                % (created, "\n".join(errors)),
                parent=self,
            )

    @err_catcher(name=__name__)
    def closeEvent(self, event):
        try:
            if getattr(self, "syncTimer", None):
                self.syncTimer.stop()
            # Flush any pending edits so nothing is lost on close.
            if getattr(self, "saveTimer", None) and self.saveTimer.isActive():
                self.saveTimer.stop()
                self.flushAutoSave()
                # Reflect the flushed changes in Prism's Project Browser.
                self.refreshPrismUI()
            # Persist the comment read-state.
            if getattr(self, "readSaveTimer", None):
                self.readSaveTimer.stop()
            self.flushReadState()
        except Exception:
            pass
        super(ProductionTrackerDlg, self).closeEvent(event)

    # --------------------------------------------------------- tree build ---
    @err_catcher(name=__name__)
    def makeSectionItem(self, text):
        item = QTreeWidgetItem(self.tree)
        item.setText(COL_NAME, text)
        item.setFirstColumnSpanned(True)
        item.setData(0, ROLE_ISLEAF, False)
        f = item.font(COL_NAME)
        f.setBold(True)
        f.setPointSize(f.pointSize() + 1)
        item.setFont(COL_NAME, f)
        item.setForeground(COL_NAME, QBrush(QColor("#93c5fd")))
        item.setBackground(COL_NAME, QBrush(QColor("#1f2937")))
        return item

    @err_catcher(name=__name__)
    def makeFolderItem(self, parent, text):
        item = QTreeWidgetItem(parent)
        item.setText(COL_NAME, text)
        item.setFirstColumnSpanned(True)
        item.setData(0, ROLE_ISLEAF, False)
        item.setData(0, ROLE_ENTITY, None)
        f = item.font(COL_NAME)
        f.setBold(True)
        item.setFont(COL_NAME, f)
        icon = self.style().standardIcon(QStyle.SP_DirIcon)
        item.setIcon(COL_NAME, icon)
        return item

    @err_catcher(name=__name__)
    def findChildFolder(self, parent, text):
        for i in range(parent.childCount()):
            child = parent.child(i)
            if not child.data(0, ROLE_ISLEAF) and child.text(COL_NAME).split("  (")[0] == text:
                return child
        return None

    @err_catcher(name=__name__)
    def getOrCreateFolder(self, parent, text):
        existing = self.findChildFolder(parent, text)
        if existing:
            return existing
        return self.makeFolderItem(parent, text)

    @err_catcher(name=__name__)
    def buildShotTree(self, sequences, shots):
        # Group shots by (episode, sequence).
        grouped = {}
        for shot in shots:
            key = (shot.get("episode", ""), shot.get("sequence", ""))
            grouped.setdefault(key, []).append(shot)

        # Ensure every known sequence appears, even if it has no shots yet.
        seqKeys = []
        seen = set()
        for seq in sequences:
            key = (seq.get("episode", ""), seq.get("sequence", ""))
            if key not in seen:
                seen.add(key)
                seqKeys.append(key)
        for key in grouped:
            if key not in seen:
                seen.add(key)
                seqKeys.append(key)

        seqKeys.sort(key=lambda k: (self.core.naturalKeys(k[0]), self.core.naturalKeys(k[1])))

        episodeNodes = {}
        for episode, sequence in seqKeys:
            if not sequence:
                continue
            if self._episodes and episode:
                if episode not in episodeNodes:
                    episodeNodes[episode] = self.getOrCreateFolder(self.shotsRoot, episode)
                seqParent = episodeNodes[episode]
            else:
                seqParent = self.shotsRoot

            seqNode = self.getOrCreateFolder(seqParent, sequence)
            for shot in grouped.get((episode, sequence), []):
                self.addEntityItem(seqNode, shot)

    @err_catcher(name=__name__)
    def buildAssetTree(self, assets):
        for asset in assets:
            parent, name = self.ensureAssetParent(asset["asset_path"])
            asset["_leafName"] = name
            self.addEntityItem(parent, asset)

    @err_catcher(name=__name__)
    def ensureAssetParent(self, assetPath):
        parts = [p for p in assetPath.replace("\\", "/").split("/") if p]
        name = parts[-1] if parts else assetPath
        folders = parts[:-1]
        parent = self.assetsRoot
        for folder in folders:
            parent = self.getOrCreateFolder(parent, folder)
        return parent, name

    @err_catcher(name=__name__)
    def ensureShotParent(self, entity):
        episode = entity.get("episode", "")
        sequence = entity.get("sequence", "")
        parent = self.shotsRoot
        if self._episodes and episode:
            parent = self.getOrCreateFolder(parent, episode)
        parent = self.getOrCreateFolder(parent, sequence)
        return parent

    # ------------------------------------------------- departments / tasks --
    @err_catcher(name=__name__)
    def addEntityItem(self, parent, entity, isNew=False):
        """Add a shot/asset, laid out for whichever mode is active."""
        if self.taskMode():
            return self.addTaskModeEntity(parent, entity, isNew=isNew)
        return self.addLeafItem(parent, entity, isNew=isNew)

    @err_catcher(name=__name__)
    def addTaskModeEntity(self, parent, entity, isNew=False):
        """A shot/asset as a rollup row, with department folders beneath it.

        Status, assignee and due date belong to each task under it, so the
        row summarises those instead (see refreshRollup). The description
        and frame range are the entity's own, though, so they stay editable
        here and are stored exactly where the one-row-per-shot mode stores
        them - the same values show up in both modes (see writeRollupItem).

        A row that has not been saved yet exists only in this window, so
        there is nothing on disk to read a thumbnail or task list from; it
        shows as pending until Save creates it in Prism.
        """
        item = QTreeWidgetItem(parent)
        item.setData(0, ROLE_ISLEAF, False)
        item.setData(0, ROLE_ENTITY, entity)
        item.setData(0, ROLE_ISNEW, isNew)
        item.setData(0, ROLE_ROLLUP, True)

        item.setText(COL_NAME, self.leafDisplayName(entity, isNew))
        item.setText(
            COL_TYPE,
            ("Shot" if entity.get("type") == "shot" else "Asset")
            + (" (new)" if isNew else ""),
        )

        if isNew:
            f = item.font(COL_NAME)
            f.setItalic(True)
            item.setFont(COL_NAME, f)
            item.setForeground(COL_NAME, QBrush(QColor("#22c55e")))
            item.setToolTip(COL_NAME, "New - will be created in Prism on Save")
        else:
            item.setData(0, ROLE_THUMB, self.getThumbnail(entity))
            self.refreshRollupIcon(item)

        desc = "" if isNew else self.getEntityDescription(entity)
        self.tree.setItemWidget(item, COL_DESC, self.wrapCell(self.makeDescEdit(desc)))

        rangeText = ""
        if entity.get("type") == "shot":
            rangeText = self.shotRangeText(entity, isNew)
            self.tree.setItemWidget(
                item, COL_RANGE, self.wrapCell(self.makeRangeEdit(rangeText))
            )
        else:
            item.setText(COL_RANGE, "-")
            item.setForeground(COL_RANGE, QBrush(QColor("#6b7280")))
            item.setTextAlignment(COL_RANGE, Qt.AlignCenter)

        # Same shape as a leaf snapshot so isDirty()/captureTreeState() treat
        # both alike; only "desc" and "range" are ever written from it.
        item.setData(
            0,
            ROLE_SNAPSHOT,
            {
                "assignee": "",
                "status": STATUS_NOT_STARTED,
                "department": DEPT_NONE,
                "due": "",
                "range": rangeText,
                "desc": desc,
                "notes": TrackerComments.empty_data(),
            },
        )

        if isNew:
            self.refreshRollup(item)
            return item

        # Watched for new departments/tasks. Stat before listing, so anything
        # created in between is noticed rather than missed.
        self.watchPath(self.tasks.departmentsFolder(entity))
        for abbreviation, longName in self.tasks.departments(entity):
            self.watchPath(self.tasks.departmentFolder(entity, abbreviation))
            # Kept even when empty, as the Project Browser does, so the
            # department can be right-clicked to add its first task.
            taskNames = self.tasks.tasks(entity, abbreviation)
            deptNode = self.makeFolderItem(item, longName)
            deptNode.setData(0, ROLE_ENTITY, None)
            deptNode.setData(0, ROLE_DEPT, (abbreviation, longName))
            deptNode.setIcon(COL_NAME, QIcon(self.deptSwatch(longName)))
            for task in taskNames:
                self.addTaskItem(deptNode, entity, abbreviation, longName, task)
            deptNode.setExpanded(True)

        self.refreshRollup(item)
        item.setExpanded(True)
        return item

    @err_catcher(name=__name__)
    def deptSwatch(self, departmentLabel):
        """A small colour chip for a department folder row.

        Prism's own department names ("Lighting") are not the tracker's
        configurable ones ("Comp", "Rendering", ...), so most will not be in
        the colour table. Rather than render everything the same grey, fall
        back to the same deterministic name-to-palette hash the avatars use,
        which keeps each department a stable, distinct colour.
        """
        color = self.deptColors.get(departmentLabel) or self.team.colorForName(
            departmentLabel
        )
        pm = QPixmap(12, 12)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(color)))
        p.drawRoundedRect(0, 2, 11, 8, 3, 3)
        p.end()
        return pm

    @err_catcher(name=__name__)
    def addTaskItem(self, parent, entity, department, departmentLabel, task):
        """One tracked row for a Prism task."""
        taskEntity = {
            "type": "task",
            "parent": entity,
            "department": department,
            "departmentLabel": departmentLabel,
            "task": task,
        }

        item = QTreeWidgetItem(parent)
        item.setData(0, ROLE_ISLEAF, True)
        item.setData(0, ROLE_ENTITY, taskEntity)
        item.setData(0, ROLE_ISNEW, False)

        self.watchPath(self.tasks.dataPath(entity, department, task))
        data = self.tasks.read(entity, department, task)

        item.setText(COL_NAME, task)
        item.setText(COL_TYPE, "Task")
        item.setForeground(COL_TYPE, QBrush(QColor("#9ca3af")))

        # The task's own note, kept in its info file. The shot's description
        # (Prism's, shared with the other mode) is on the rollup row above.
        desc = data.get(TASK_KEYS["desc"], "") or ""
        descEdit = self.makeDescEdit(desc, placeholder="Task description...")
        descEdit.setToolTip(
            "Stored on this task only. The shot/asset description is on its "
            "own row above."
        )
        self.tree.setItemWidget(item, COL_DESC, self.wrapCell(descEdit))

        assignee = self.resolveAssignee(data.get(TASK_KEYS["assignee"], "") or "")
        if assignee:
            self._taskAssignees.add(assignee)
        assigneeCombo = self.makeAssigneeCombo(assignee)
        assigneeCombo.currentTextChanged.connect(
            lambda _t, it=item: self.onTaskRowChanged(it)
        )
        self.tree.setItemWidget(item, COL_ASSIGNEE, self.wrapCell(assigneeCombo))

        status = data.get(TASK_KEYS["status"], "") or STATUS_NOT_STARTED
        statusCombo = self.makeStatusCombo(status)
        statusCombo.currentTextChanged.connect(
            lambda _t, it=item: self.onTaskRowChanged(it)
        )
        self.tree.setItemWidget(item, COL_STATUS, self.wrapCell(statusCombo))

        # Frame range and department are properties of the shot, not the task.
        item.setText(COL_RANGE, "-")
        item.setForeground(COL_RANGE, QBrush(QColor("#6b7280")))
        item.setTextAlignment(COL_RANGE, Qt.AlignCenter)

        due = data.get(TASK_KEYS["due"], "") or ""
        dueWidget = DueDateWidget(due)
        dueWidget.dateEdit.dateChanged.connect(
            lambda _d, it=item: self.onTaskRowChanged(it)
        )
        self.tree.setItemWidget(item, COL_DUE, self.wrapCell(dueWidget))

        notes = TrackerComments.load_notes_value(data.get(TASK_KEYS["notes"]))
        item.setData(0, ROLE_NOTES, notes)
        self.updateCommentIndicator(item)

        item.setData(
            0,
            ROLE_SNAPSHOT,
            {
                "assignee": assignee,
                "status": status,
                "department": DEPT_NONE,
                "due": due,
                "range": "",
                "desc": desc,
                "notes": copy.deepcopy(notes),
            },
        )
        self.applyRowColor(item)
        return item

    @err_catcher(name=__name__)
    def onTaskRowChanged(self, item):
        """A task row edit: recolour it, refresh its shot rollup, autosave."""
        self.applyRowColor(item)
        self.refreshRollupFor(item)
        self.scheduleAutoSave()

    @err_catcher(name=__name__)
    def rollupAncestor(self, item):
        """The shot/asset rollup row above a task row (or None)."""
        cur = item.parent() if item is not None else None
        while cur is not None:
            if cur.data(0, ROLE_ROLLUP):
                return cur
            cur = cur.parent()
        return None

    @err_catcher(name=__name__)
    def refreshRollupFor(self, item):
        rollup = self.rollupAncestor(item)
        if rollup is not None:
            self.refreshRollup(rollup)

    @err_catcher(name=__name__)
    def taskRowsUnder(self, item):
        rows = []

        def walk(node):
            for i in range(node.childCount()):
                child = node.child(i)
                entity = child.data(0, ROLE_ENTITY) or {}
                if child.data(0, ROLE_ISLEAF) and entity.get("type") == "task":
                    rows.append(child)
                else:
                    walk(child)

        walk(item)
        return rows

    @err_catcher(name=__name__)
    def refreshRollup(self, item):
        """Summarise the tasks under a shot/asset onto its own row.

        Status column: how many tasks are finished. Due column: the earliest
        due date still outstanding, so a shot shows its next deadline.
        """
        rows = self.taskRowsUnder(item)
        if not rows:
            item.setText(COL_STATUS, "No tasks")
            item.setForeground(COL_STATUS, QBrush(QColor("#6b7280")))
            item.setText(COL_DUE, "")
            for col in range(COL_COUNT):
                item.setData(col, ROLE_ROWCOLOR, None)
                item.setData(col, ROLE_FADEROW, False)
            return

        done = 0
        dues = []
        for row in rows:
            values = self.getRowValues(row)
            if values.get("status") == STATUS_COMPLETED:
                done += 1
            elif values.get("due"):
                dues.append(values["due"])

        allDone = done == len(rows)
        item.setText(COL_STATUS, "%d / %d complete" % (done, len(rows)))
        item.setTextAlignment(COL_STATUS, Qt.AlignCenter)
        item.setForeground(
            COL_STATUS, QBrush(QColor("#22c55e" if allDone else "#9ca3af"))
        )

        # Tint the whole row once every task under it is finished, matching
        # the "wrapped up" tint a completed row gets in the other mode.
        tint = (
            self.blendColor(COMPLETE_APPROVED_COLOR, alpha=COMPLETE_APPROVED_ALPHA)
            if allDone
            else None
        )
        for col in range(COL_COUNT):
            item.setData(col, ROLE_ROWCOLOR, tint)
            item.setData(col, ROLE_FADEROW, False)

        item.setText(COL_DUE, min(dues) if dues else "")
        item.setTextAlignment(COL_DUE, Qt.AlignCenter)
        item.setForeground(COL_DUE, QBrush(QColor("#9ca3af")))

    @err_catcher(name=__name__)
    def rollupItems(self):
        """Every shot/asset summary row (empty outside task mode)."""
        found = []

        def walk(node):
            for i in range(node.childCount()):
                child = node.child(i)
                if child.data(0, ROLE_ROLLUP):
                    found.append(child)
                walk(child)

        walk(self.tree.invisibleRootItem())
        return found

    @err_catcher(name=__name__)
    def refreshAllRollups(self):
        for item in self.rollupItems():
            self.refreshRollup(item)

    # ---------------------------------------------------------- leaf items --
    @err_catcher(name=__name__)
    def getThumbnail(self, entity):
        try:
            return self.core.entities.getEntityPreview(entity, THUMB_W, THUMB_H)
        except Exception:
            return None

    @err_catcher(name=__name__)
    def leafDisplayName(self, entity, isNew):
        if entity.get("type") == "shot":
            return entity.get("shot") or ""
        if entity.get("type") == "fxlayer":
            return entity.get("name") or "FX layer"
        if entity.get("type") == "task":
            return entity.get("task") or ""
        return entity.get("_leafName") or (entity.get("asset_path") or "").split("/")[-1]

    @err_catcher(name=__name__)
    def makeStatusCombo(self, current):
        cb = NoScrollComboBox()
        cb.addItems(STATUSES)
        cb.setCurrentText(current if current in STATUSES else STATUS_NOT_STARTED)

        def style(text):
            color = STATUS_COLORS.get(text, NEUTRAL_COLOR)
            cb.setStyleSheet(
                "QComboBox{border: none; border-radius: 10px; padding: 2px 10px; color: white;"
                "background-color: %s;}" % color
            )

        style(cb.currentText())
        cb.currentTextChanged.connect(style)
        cb.currentTextChanged.connect(self.scheduleAutoSave)
        return cb

    @err_catcher(name=__name__)
    def makeDepartmentCombo(self, current, departments):
        cb = NoScrollComboBox()
        names = list(departments)
        # As above: keep an unknown stored department instead of dropping it.
        if current and current not in names:
            names.append(current)
        cb.addItems(names)
        cb.setCurrentText(current if current in names else DEPT_NONE)

        def style(text):
            color = self.deptColors.get(text, NEUTRAL_COLOR)
            cb.setStyleSheet(
                "QComboBox{border: none; border-radius: 10px; padding: 2px 10px; color: white;"
                "background-color: %s;}" % color
            )

        style(cb.currentText())
        cb.currentTextChanged.connect(style)
        cb.currentTextChanged.connect(self.scheduleAutoSave)
        return cb

    @err_catcher(name=__name__)
    def makeAssigneeCombo(self, current):
        cb = NoScrollComboBox()
        cb.setEditable(True)
        cb.setStyleSheet("QComboBox{background: transparent;}")
        cb.addItem("")
        for name in self.assigneeList:
            icon = self.avatarFor(name)
            if icon is not None:
                cb.addItem(icon, name)
            else:
                cb.addItem(name)
        if current:
            if cb.findText(current) == -1:
                icon = self.avatarFor(current)
                if icon is not None:
                    cb.addItem(icon, current)
                else:
                    cb.addItem(current)
            cb.setCurrentText(current)
        else:
            cb.setCurrentIndex(0)
        cb.currentTextChanged.connect(self.scheduleAutoSave)
        return cb

    @err_catcher(name=__name__)
    def makeDescEdit(self, text, placeholder="Description..."):
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.setStyleSheet("QLineEdit{background: transparent;}")
        edit.setText(text)
        edit.textEdited.connect(self.scheduleAutoSave)
        return edit

    @err_catcher(name=__name__)
    def makeRangeEdit(self, text):
        edit = QLineEdit()
        edit.setPlaceholderText("start-end")
        edit.setStyleSheet("QLineEdit{background: transparent;}")
        edit.setAlignment(Qt.AlignCenter)
        edit.setText(text)
        edit.textEdited.connect(self.scheduleAutoSave)
        return edit

    @err_catcher(name=__name__)
    def shotRangeText(self, entity, isNew):
        """'start-end' for a shot: from Prism, or from Add Shot if unsaved."""
        if isNew:
            if entity.get("_start") is not None and entity.get("_end") is not None:
                return "%s-%s" % (entity["_start"], entity["_end"])
            return ""
        frange = self.core.entities.getShotRange(entity)
        return "%s-%s" % (frange[0], frange[1]) if frange else ""

    @err_catcher(name=__name__)
    def addLeafItem(self, parent, entity, isNew=False):
        item = QTreeWidgetItem(parent)
        item.setData(0, ROLE_ISLEAF, True)
        item.setData(0, ROLE_ENTITY, entity)
        item.setData(0, ROLE_ISNEW, isNew)

        meta = {} if isNew else self.core.entities.getMetaData(entity)

        # Name + thumbnail icon
        item.setText(COL_NAME, self.leafDisplayName(entity, isNew))
        pixmap = None if isNew else self.getThumbnail(entity)
        item.setData(0, ROLE_THUMB, pixmap)
        base = self.thumbFor(item)
        if base:
            item.setIcon(COL_NAME, QIcon(base))
        if isNew:
            f = item.font(COL_NAME)
            f.setItalic(True)
            item.setFont(COL_NAME, f)
            item.setForeground(COL_NAME, QBrush(QColor("#22c55e")))
            item.setToolTip(COL_NAME, "New - will be created in Prism on Save")

        # Type
        item.setText(
            COL_TYPE,
            ("Shot" if entity.get("type") == "shot" else "Asset") + (" (new)" if isNew else ""),
        )

        # Description (imported from Prism)
        desc = "" if isNew else self.getEntityDescription(entity, meta)
        self.tree.setItemWidget(item, COL_DESC, self.wrapCell(self.makeDescEdit(desc)))

        # Assignee
        assignee = self.resolveAssignee(meta.get(ASSIGNEE_KEY, {}).get("value", ""))
        assigneeCombo = self.makeAssigneeCombo(assignee)
        assigneeCombo.currentTextChanged.connect(
            lambda _t, it=item: self.applyRowColor(it)
        )
        self.tree.setItemWidget(item, COL_ASSIGNEE, self.wrapCell(assigneeCombo))

        # Status
        status = meta.get(STATUS_KEY, {}).get("value", STATUS_NOT_STARTED)
        statusCombo = self.makeStatusCombo(status)
        statusCombo.currentTextChanged.connect(lambda _t, it=item: self.applyRowColor(it))
        self.tree.setItemWidget(item, COL_STATUS, self.wrapCell(statusCombo))

        # Department (informational only)
        department = meta.get(DEPT_KEY, {}).get("value", DEPT_NONE)
        department = DEPARTMENT_RENAMES.get(department, department)
        departments = (
            self.deptAsset if entity.get("type") == "asset" else self.deptShot
        )
        deptCombo = self.makeDepartmentCombo(department, departments)
        deptCombo.currentTextChanged.connect(lambda _t, it=item: self.applyRowColor(it))
        self.tree.setItemWidget(item, COL_DEPARTMENT, self.wrapCell(deptCombo))

        # Frame range (shots only)
        rangeText = ""
        if entity.get("type") == "shot":
            rangeText = self.shotRangeText(entity, isNew)
            self.tree.setItemWidget(
                item, COL_RANGE, self.wrapCell(self.makeRangeEdit(rangeText))
            )
        else:
            item.setText(COL_RANGE, "-")
            item.setForeground(COL_RANGE, QBrush(QColor("#6b7280")))
            item.setTextAlignment(COL_RANGE, Qt.AlignCenter)

        # Due date
        due = meta.get(DUE_KEY, {}).get("value", "")
        dueWidget = DueDateWidget(due)
        dueWidget.dateEdit.dateChanged.connect(self.scheduleAutoSave)
        self.tree.setItemWidget(item, COL_DUE, self.wrapCell(dueWidget))

        # Comments/notes (rendered + edited via the side comments panel).
        notes = TrackerComments.load_notes_value(meta.get(NOTES_KEY, {}).get("value"))
        item.setData(0, ROLE_NOTES, notes)
        self.updateCommentIndicator(item)

        # Snapshot of saved values for change detection.
        item.setData(
            0,
            ROLE_SNAPSHOT,
            {
                "assignee": assignee,
                "status": status,
                "department": department,
                "due": due,
                "range": rangeText,
                "desc": desc,
                "notes": copy.deepcopy(notes),
            },
        )
        self.applyRowColor(item)

        # Load any FX layers tracked in parallel under this shot.
        if entity.get("type") == "shot" and not isNew:
            fxLayers = meta.get(FXLAYERS_KEY, {}).get("value") or []
            for layer in fxLayers:
                if isinstance(layer, dict):
                    self.addFxLayerItem(item, layer)
            if fxLayers:
                item.setExpanded(True)
            item.setData(0, ROLE_FXSNAPSHOT, self.collectFxLayers(item))
        return item

    # ---------------------------------------------------------- FX layers ---
    @err_catcher(name=__name__)
    def addFxLayerItem(self, shotItem, data):
        """Build one FX-layer child row under a shot. `data` is a layer dict.

        FX layers are lightweight tracker rows (no Prism entity, no thumbnail,
        thinner than shots) used to track VFX work done in parallel with the
        shot. They are persisted on the parent shot's metadata.
        """
        item = QTreeWidgetItem(shotItem)
        entity = {"type": "fxlayer", "fx_id": data.get("id") or uuid.uuid4().hex}
        item.setData(0, ROLE_ISLEAF, True)
        item.setData(0, ROLE_ENTITY, entity)
        item.setData(0, ROLE_ISNEW, False)

        # No thumbnail -> a thinner row than the parent shots.
        for c in range(COL_COUNT):
            item.setSizeHint(c, QSize(0, 26))

        # Name (editable label so multiple FX layers are distinguishable).
        name = data.get("name", "") or ""
        entity["name"] = name
        nameEdit = QLineEdit()
        nameEdit.setPlaceholderText("FX layer name...")
        nameEdit.setStyleSheet(
            "QLineEdit{background: transparent; color:#c4b5fd; font-style: italic;}"
        )
        nameEdit.setText(name)

        def onNameEdited(txt, it=item, ent=entity):
            ent["name"] = txt
            if it is self._notesItem:
                self.updateNotesPanel()
            self.scheduleAutoSave()

        nameEdit.textEdited.connect(onNameEdited)
        self.tree.setItemWidget(item, COL_NAME, self.wrapCell(nameEdit))

        # Type
        item.setText(COL_TYPE, "VFX")
        item.setForeground(COL_TYPE, QBrush(QColor("#c4b5fd")))

        # Description
        descEdit = self.makeDescEdit(data.get("desc", "") or "")
        self.tree.setItemWidget(item, COL_DESC, self.wrapCell(descEdit))

        # Assignee (can differ from the parent shot's assignee).
        assignee = self.resolveAssignee(data.get("assignee", ""))
        assigneeCombo = self.makeAssigneeCombo(assignee)
        assigneeCombo.currentTextChanged.connect(
            lambda _t, it=item: self.applyRowColor(it)
        )
        self.tree.setItemWidget(item, COL_ASSIGNEE, self.wrapCell(assigneeCombo))

        # Status
        status = data.get("status", STATUS_NOT_STARTED)
        statusCombo = self.makeStatusCombo(status)
        statusCombo.currentTextChanged.connect(lambda _t, it=item: self.applyRowColor(it))
        self.tree.setItemWidget(item, COL_STATUS, self.wrapCell(statusCombo))

        # Department: locked to VFX.
        deptCombo = self.makeDepartmentCombo(self.deptFx, [self.deptFx])
        deptCombo.setEnabled(False)
        self.tree.setItemWidget(item, COL_DEPARTMENT, self.wrapCell(deptCombo))

        # No frame range for FX layers.
        item.setText(COL_RANGE, "-")
        item.setForeground(COL_RANGE, QBrush(QColor("#6b7280")))
        item.setTextAlignment(COL_RANGE, Qt.AlignCenter)

        # Due date
        dueWidget = DueDateWidget(data.get("due", ""))
        dueWidget.dateEdit.dateChanged.connect(self.scheduleAutoSave)
        self.tree.setItemWidget(item, COL_DUE, self.wrapCell(dueWidget))

        # Comments/notes
        notes = TrackerComments.load_notes_value(data.get("notes"))
        item.setData(0, ROLE_NOTES, notes)

        item.setData(
            0,
            ROLE_SNAPSHOT,
            {
                "assignee": assignee,
                "status": status,
                "department": self.deptFx,
                "due": data.get("due", "") or "",
                "range": "",
                "desc": data.get("desc", "") or "",
                "notes": copy.deepcopy(notes),
                "name": name,
            },
        )
        self.applyRowColor(item)
        return item

    @err_catcher(name=__name__)
    def collectFxLayers(self, shotItem):
        """Build the saveable list of FX-layer dicts from a shot's children."""
        layers = []
        for i in range(shotItem.childCount()):
            child = shotItem.child(i)
            entity = child.data(0, ROLE_ENTITY) or {}
            if entity.get("type") != "fxlayer":
                continue
            v = self.getRowValues(child)
            layers.append(
                {
                    "id": entity.get("fx_id"),
                    "name": v.get("name", ""),
                    "assignee": v.get("assignee", ""),
                    "status": v.get("status", STATUS_NOT_STARTED),
                    "department": self.deptFx,
                    "due": v.get("due", ""),
                    "desc": v.get("desc", ""),
                    "notes": v.get("notes") or TrackerComments.empty_data(),
                }
            )
        return layers

    @err_catcher(name=__name__)
    def writeFxLayers(self, shotItem):
        """Persist a shot's FX layers into its metadata. Returns [errors].

        Merged against the stored list the same way rows are (see
        mergeFxLayers), so a layer someone else added, removed or edited
        since this window read the shot is not undone by this save.
        """
        entity = shotItem.data(0, ROLE_ENTITY) or {}
        if entity.get("type") != "shot":
            return []
        local = self.collectFxLayers(shotItem)
        base = shotItem.data(0, ROLE_FXSNAPSHOT) or []
        try:
            meta = self.core.entities.getMetaData(entity) or {}
            stored = meta.get(FXLAYERS_KEY, {}).get("value") or []
            layers = self.mergeFxLayers(
                local, base, stored, self.shortEntityLabel(entity)
            )
            if layers:
                meta[FXLAYERS_KEY] = {"value": layers, "show": False}
            else:
                meta.pop(FXLAYERS_KEY, None)
            self.core.entities.setMetaData(entity=entity, metaData=meta)
        except Exception as e:
            return ["%s (fx): %s" % (self.leafDisplayName(entity, False), e)]

        if layers != local:
            # Someone else's changes were merged in: show them.
            self.rebuildFxLayers(shotItem, layers)
        else:
            for child in self.fxChildren(shotItem):
                child.setData(0, ROLE_SNAPSHOT, self.getRowValues(child))
        shotItem.setData(0, ROLE_FXSNAPSHOT, self.collectFxLayers(shotItem))
        return []

    @err_catcher(name=__name__)
    def normalizeFxLayer(self, layer):
        """A stored FX layer dict in the shape collectFxLayers() produces."""
        return {
            "id": layer.get("id"),
            "name": (layer.get("name") or "").strip(),
            "assignee": self.resolveAssignee(layer.get("assignee", "")),
            "status": layer.get("status", STATUS_NOT_STARTED),
            "department": self.deptFx,
            "due": layer.get("due", "") or "",
            "desc": layer.get("desc", "") or "",
            "notes": TrackerComments.load_notes_value(layer.get("notes")),
        }

    @err_catcher(name=__name__)
    def mergeFxLayers(self, local, base, stored, label):
        """Three-way merge of a shot's FX layer list, layer by layer (by id).

        local: what this window shows; base: what it last read or saved;
        stored: what is on disk now. Layers added on either side are kept,
        layers removed on either side are dropped - unless the other side
        edited it meanwhile, in which case keeping it loses nothing. Fields of
        a layer present everywhere merge like a row's (mergeRowValues).
        """
        baseById = dict((l.get("id"), l) for l in base)
        storedById = dict(
            (l.get("id"), self.normalizeFxLayer(l))
            for l in stored
            if isinstance(l, dict)
        )
        localIds = set(l.get("id") for l in local)

        out = []
        for layer in local:
            lid = layer.get("id")
            old = baseById.get(lid)
            theirs = storedById.get(lid)
            if old is None:
                out.append(layer)  # added here
            elif theirs is None:
                if layer != old:
                    out.append(layer)  # removed there, but edited here
            else:
                merged = self.mergeRowValues(
                    layer,
                    old,
                    theirs,
                    FX_FIELDS,
                    "%s / %s" % (label, layer.get("name") or "FX"),
                )
                merged["notes"] = TrackerComments.merge_comments(
                    theirs["notes"], layer.get("notes")
                )
                out.append(merged)

        for lid, theirs in storedById.items():
            if lid in localIds:
                continue
            old = baseById.get(lid)
            if old is None or theirs != old:
                out.append(theirs)  # added there, or removed here but edited there
        return out

    @err_catcher(name=__name__)
    def fxChildren(self, shotItem):
        return [
            shotItem.child(i)
            for i in range(shotItem.childCount())
            if (shotItem.child(i).data(0, ROLE_ENTITY) or {}).get("type") == "fxlayer"
        ]

    @err_catcher(name=__name__)
    def rebuildFxLayers(self, shotItem, layers):
        """Replace a shot's FX rows with `layers`, keeping the selection."""
        current = self.tree.currentItem()
        selectedId = None
        if current is not None and current.parent() is shotItem:
            selectedId = (current.data(0, ROLE_ENTITY) or {}).get("fx_id")

        self._applyingRemote = True
        try:
            for child in self.fxChildren(shotItem):
                if child is self._notesItem:
                    self._notesItem = None
                shotItem.removeChild(child)
            reselect = None
            for layer in layers:
                row = self.addFxLayerItem(shotItem, layer)
                if layer.get("id") == selectedId:
                    reselect = row
        finally:
            self._applyingRemote = False

        self.applyFilter()
        self.updateStatusLabel()
        if reselect is not None:
            self.tree.setCurrentItem(reselect)
        else:
            self.updateNotesPanel()

    @err_catcher(name=__name__)
    def saveAllFxLayers(self):
        """Write FX layers for any shot whose FX children changed."""
        saved = 0
        errors = []
        for shotItem in self.iterShotItems():
            if shotItem.data(0, ROLE_ISNEW):
                continue
            layers = self.collectFxLayers(shotItem)
            snap = shotItem.data(0, ROLE_FXSNAPSHOT)
            if not layers and not snap:
                continue
            if layers != (snap or []):
                errs = self.writeFxLayers(shotItem)
                if errs:
                    errors.extend(errs)
                else:
                    saved += 1
        return saved, errors

    @err_catcher(name=__name__)
    def addFxLayer(self, shotItem):
        entity = shotItem.data(0, ROLE_ENTITY) or {}
        if entity.get("type") != "shot":
            return
        if shotItem.data(0, ROLE_ISNEW):
            self.core.popup(
                "Please save the new shot first, then add FX layers to it.",
                parent=self,
            )
            return
        data = {
            "id": uuid.uuid4().hex,
            "name": "FX",
            "assignee": "",
            "status": STATUS_NOT_STARTED,
            "department": self.deptFx,
            "due": "",
            "desc": "",
            "notes": TrackerComments.empty_data(),
        }
        item = self.addFxLayerItem(shotItem, data)
        shotItem.setExpanded(True)
        # Persist immediately so it survives refreshes / live-sync.
        errs = self.writeFxLayers(shotItem)
        if errs:
            self.core.popup("\n".join(errs), parent=self)
        else:
            self._lastSyncMtimes = self.currentMtimes()
        # The save may have rebuilt the FX rows (merging in someone else's
        # changes), so look the new row up again rather than reuse `item`.
        for child in self.fxChildren(shotItem):
            if (child.data(0, ROLE_ENTITY) or {}).get("fx_id") == data["id"]:
                item = child
                break
        self.applyFilter()
        self.updateStatusLabel()
        self.tree.setCurrentItem(item)
        self.tree.scrollToItem(item)

    @err_catcher(name=__name__)
    def deleteFxLayer(self, item):
        entity = item.data(0, ROLE_ENTITY) or {}
        if entity.get("type") != "fxlayer":
            return
        nameEdit = self.cellWidget(item, COL_NAME)
        name = (nameEdit.text().strip() if nameEdit else "") or "FX layer"
        result = self.core.popupQuestion(
            "Delete the FX layer '%s'?" % name,
            buttons=["Delete", "Cancel"],
            parent=self,
        )
        if result != "Delete":
            return
        shotItem = item.parent()
        if item is self._notesItem:
            self._notesItem = None
        if shotItem is not None:
            shotItem.removeChild(item)
            errs = self.writeFxLayers(shotItem)
            if errs:
                self.core.popup("\n".join(errs), parent=self)
            else:
                self._lastSyncMtimes = self.currentMtimes()
        self.updateStatusLabel()
        self.updateNotesPanel()
        self.l_status.setText("Deleted FX layer '%s'." % name)

    # ------------------------------------------------------- leaf helpers ---
    @err_catcher(name=__name__)
    def iterLeafItems(self):
        items = []

        def walk(parent):
            for i in range(parent.childCount()):
                child = parent.child(i)
                if child.data(0, ROLE_ISLEAF):
                    items.append(child)
                # Descend even into leaves so FX-layer children are included.
                walk(child)

        walk(self.tree.invisibleRootItem())
        return items

    @err_catcher(name=__name__)
    def iterShotItems(self):
        return [
            it
            for it in self.iterLeafItems()
            if (it.data(0, ROLE_ENTITY) or {}).get("type") == "shot"
        ]

    @err_catcher(name=__name__)
    def cellWidget(self, item, col):
        holder = self.tree.itemWidget(item, col)
        if holder is None:
            return None
        lo = holder.layout()
        if lo and lo.count():
            return lo.itemAt(0).widget()
        return holder

    @err_catcher(name=__name__)
    def getRowValues(self, item):
        assigneeCb = self.cellWidget(item, COL_ASSIGNEE)
        statusCb = self.cellWidget(item, COL_STATUS)
        deptCb = self.cellWidget(item, COL_DEPARTMENT)
        dueW = self.cellWidget(item, COL_DUE)
        descEdit = self.cellWidget(item, COL_DESC)
        entity = item.data(0, ROLE_ENTITY)

        values = {
            "assignee": assigneeCb.currentText().strip() if assigneeCb else "",
            "status": statusCb.currentText() if statusCb else STATUS_NOT_STARTED,
            "department": deptCb.currentText() if deptCb else DEPT_NONE,
            "due": dueW.isoDate() if dueW else "",
            "range": "",
            "desc": descEdit.text() if descEdit else "",
            "notes": item.data(0, ROLE_NOTES) or TrackerComments.empty_data(),
        }
        if entity.get("type") == "shot":
            rangeEdit = self.cellWidget(item, COL_RANGE)
            values["range"] = rangeEdit.text().strip() if rangeEdit else ""
        if entity.get("type") == "fxlayer":
            nameEdit = self.cellWidget(item, COL_NAME)
            values["name"] = nameEdit.text().strip() if nameEdit else entity.get("name", "")
        return values

    # ------------------------------------------------------------- filter ---
    @err_catcher(name=__name__)
    def applyFilter(self, *args):
        text = self.e_search.text().strip().lower()
        if self.userMgmtEnabled and self.cb_filterAssignee.count():
            mode = self.cb_filterAssignee.currentData()
        else:
            mode = "__all__"
        meName = self.currentUser.get("full_name") if self.currentUser else None

        def assigneeMatch(item):
            if mode in (None, "__all__"):
                return True
            assignee = (self.getRowValues(item).get("assignee") or "").strip()
            if mode == "__me__":
                return bool(meName) and assignee == meName
            if mode == "__unassigned__":
                return assignee == ""
            return assignee == mode

        def leafName(item):
            entity = item.data(0, ROLE_ENTITY) or {}
            if entity.get("type") == "fxlayer":
                w = self.cellWidget(item, COL_NAME)
                return (w.text() if w else entity.get("name", "")).lower()
            return item.text(COL_NAME).lower()

        def showSubtree(item):
            item.setHidden(False)
            for i in range(item.childCount()):
                showSubtree(item.child(i))

        def walk(parent):
            anyVisible = False
            for i in range(parent.childCount()):
                child = parent.child(i)
                if child.data(0, ROLE_ISLEAF):
                    match = (not text) or (text in leafName(child))
                    match = match and assigneeMatch(child)
                    # A shot stays visible if any of its FX children match.
                    fxVisible = walk(child) if child.childCount() else False
                    visible = match or fxVisible
                    child.setHidden(not visible)
                    anyVisible = anyVisible or visible
                elif (
                    child.data(0, ROLE_ROLLUP)
                    and ((not text) or text in child.text(COL_NAME).lower())
                    and mode in (None, "__all__")
                ):
                    # In task mode the shot is a folder, but it is still a
                    # row of its own: show it when it matches (or nothing is
                    # being filtered), with everything under it. Otherwise a
                    # shot with no tasks yet - or a new, unsaved one - would
                    # count as an empty folder and vanish.
                    showSubtree(child)
                    anyVisible = True
                else:
                    childVisible = walk(child)
                    child.setHidden(not childVisible)
                    anyVisible = anyVisible or childVisible
            return anyVisible

        walk(self.tree.invisibleRootItem())

    @err_catcher(name=__name__)
    def updateNodeCounts(self):
        def count(parent):
            total = 0
            for i in range(parent.childCount()):
                child = parent.child(i)
                if child.data(0, ROLE_ROLLUP):
                    # A shot/asset counts as one, not as its task count.
                    count(child)
                    total += 1
                elif child.data(0, ROLE_ISLEAF):
                    total += 1
                else:
                    c = count(child)
                    base = child.text(COL_NAME).split("  (")[0]
                    child.setText(COL_NAME, "%s  (%d)" % (base, c))
                    total += c
            return total

        for root in (self.shotsRoot, self.assetsRoot):
            c = count(root)
            base = root.text(COL_NAME).split("  (")[0]
            root.setText(COL_NAME, "%s  (%d)" % (base, c))

    @err_catcher(name=__name__)
    def updateStatusLabel(self):
        leaves = self.iterLeafItems()
        rows = leaves + self.rollupItems()
        shots = sum(1 for i in rows if (i.data(0, ROLE_ENTITY) or {}).get("type") == "shot")
        assets = sum(1 for i in rows if (i.data(0, ROLE_ENTITY) or {}).get("type") == "asset")
        fx = sum(1 for i in leaves if (i.data(0, ROLE_ENTITY) or {}).get("type") == "fxlayer")
        tasks = sum(1 for i in leaves if (i.data(0, ROLE_ENTITY) or {}).get("type") == "task")
        pending = len(self.newRows)
        msg = "%d shots  |  %d assets" % (shots, assets)
        if tasks:
            msg += "  |  %d tasks" % tasks
        if fx:
            msg += "  |  %d FX layers" % fx
        if pending:
            msg += "  |  %d unsaved new item(s)" % pending
        self.l_status.setText(msg)

    # ---------------------------------------------------------------- add ---
    @err_catcher(name=__name__)
    def existingSequenceNames(self):
        names = set()
        for entity in self._existingEntities:
            if entity.get("type") == "shot" and entity.get("sequence"):
                names.add(entity["sequence"])
        for entity in self.newRows:
            if entity.get("type") == "shot" and entity.get("sequence"):
                names.add(entity["sequence"])
        return sorted(names)

    @err_catcher(name=__name__)
    def existingShotKeys(self):
        """{'sequence/shot'} already in the project or queued unsaved."""
        keys = set()
        for entity in self._existingEntities + self.newRows:
            if entity.get("type") != "shot":
                continue
            episode = entity.get("episode", "")
            if episode:
                key = "%s / %s / %s" % (episode, entity.get("sequence", ""),
                                        entity.get("shot", ""))
            else:
                key = "%s / %s" % (entity.get("sequence", ""), entity.get("shot", ""))
            keys.add(key.lower())
        return keys

    @err_catcher(name=__name__)
    def existingAssetKeys(self):
        """{'asset/path'} already in the project or queued unsaved."""
        keys = set()
        for entity in self._existingEntities + self.newRows:
            if entity.get("type") == "asset":
                keys.add((entity.get("asset_path") or "").lower())
        return keys

    @err_catcher(name=__name__)
    def onAddShot(self, sequence="", episode=""):
        dlg = AddShotDialog(
            self.core,
            parent=self,
            sequences=self.existingSequenceNames(),
            sequence=sequence,
            existing=self.existingShotKeys(),
            episodes=self.loadEpisodes() if self._episodes else None,
            episode=episode,
        )
        if dlg.exec_() != QDialog.Accepted:
            return

        item = None
        for data in dlg.getRows():
            entity = {
                "type": "shot",
                "sequence": data["sequence"],
                "shot": data["shot"],
                "_start": data["start"],
                "_end": data["end"],
            }
            if self._episodes:
                entity["episode"] = data.get("episode", "")
            self.newRows.append(entity)
            parent = self.ensureShotParent(entity)
            item = self.addEntityItem(parent, entity, isNew=True)
            parent.setExpanded(True)

        self.shotsRoot.setExpanded(True)
        if item is not None:
            self.tree.scrollToItem(item)
        self.updateNodeCounts()
        self.applyFilter()
        self.updateStatusLabel()

    @err_catcher(name=__name__)
    def onAddAsset(self, folder=""):
        dlg = AddAssetDialog(
            self.core, parent=self, folder=folder, existing=self.existingAssetKeys()
        )
        if dlg.exec_() != QDialog.Accepted:
            return

        item = None
        for data in dlg.getRows():
            entity = {"type": "asset", "asset_path": data["asset_path"]}
            parent, entity["_leafName"] = self.ensureAssetParent(entity["asset_path"])
            self.newRows.append(entity)
            item = self.addEntityItem(parent, entity, isNew=True)
            parent.setExpanded(True)

        self.assetsRoot.setExpanded(True)
        if item is not None:
            self.tree.scrollToItem(item)
        self.updateNodeCounts()
        self.applyFilter()
        self.updateStatusLabel()

    # ----------------------------------------------------- context menu -----
    @err_catcher(name=__name__)
    def sectionOf(self, item):
        """Which top-level section a node sits under: 'shots', 'assets' or ''."""
        cur = item
        while cur is not None:
            if cur is self.shotsRoot:
                return "shots"
            if cur is self.assetsRoot:
                return "assets"
            cur = cur.parent()
        return ""

    @err_catcher(name=__name__)
    def entityForNode(self, item):
        """The shot/asset an item belongs to.

        Works from a leaf row, a task row, a department folder or a rollup,
        so the context menu can act on 'the entity I clicked inside'.
        """
        cur = item
        while cur is not None:
            entity = cur.data(0, ROLE_ENTITY) or {}
            if entity.get("type") == "task":
                return entity.get("parent") or {}
            if entity.get("type") in ("shot", "asset"):
                return entity
            cur = cur.parent()
        return {}

    @err_catcher(name=__name__)
    def sequenceForNode(self, item):
        """Sequence name to pre-fill Add Shot with (or '')."""
        entity = self.entityForNode(item)
        if entity.get("type") == "shot":
            return entity.get("sequence", "")
        # A sequence folder is a direct child of the shots section (or of an
        # episode folder under it).
        cur = item
        while cur is not None and not cur.data(0, ROLE_ISLEAF):
            parent = cur.parent()
            if parent is self.shotsRoot or (
                parent is not None
                and parent.parent() is self.shotsRoot
                and self._episodes
            ):
                return cur.text(COL_NAME).split("  (")[0]
            cur = parent
        return ""

    @err_catcher(name=__name__)
    def episodeForNode(self, item):
        """Episode to pre-fill Add Shot with (or "")."""
        if not self._episodes:
            return ""
        entity = self.entityForNode(item)
        if entity.get("type") == "shot":
            return entity.get("episode", "")
        # Otherwise walk up to the folder directly under the shots section.
        cur = item
        while cur is not None and cur is not self.shotsRoot:
            if cur.parent() is self.shotsRoot:
                return cur.text(COL_NAME).split("  (")[0]
            cur = cur.parent()
        return ""

    @err_catcher(name=__name__)
    def assetFolderForNode(self, item):
        """Asset folder path to pre-fill Add Asset with (or '')."""
        entity = self.entityForNode(item)
        if entity.get("type") == "asset":
            path = (entity.get("asset_path") or "").replace("\\", "/")
            return path.rsplit("/", 1)[0] if "/" in path else ""

        parts = []
        cur = item
        while cur is not None and cur is not self.assetsRoot:
            if not cur.data(0, ROLE_ISLEAF):
                parts.append(cur.text(COL_NAME).split("  (")[0])
            cur = cur.parent()
        return "/".join(reversed(parts))

    @err_catcher(name=__name__)
    def onTreeContextMenu(self, pos):
        """Right-click anywhere in the tree.

        What is offered depends on where the click landed, the way the Prism
        Project Browser behaves: a section or folder offers to add something
        into it, a row offers actions on that row.
        """
        item = self.tree.itemAt(pos)
        menu = QMenu(self)

        if item is not None:
            self.tree.setCurrentItem(item)
            self.buildNodeMenu(menu, item)
        else:
            # Empty space: offer both, since there is no context to narrow it.
            menu.addAction("Add Shot").triggered.connect(lambda: self.onAddShot())
            menu.addAction("Add Asset").triggered.connect(lambda: self.onAddAsset())

        if not menu.isEmpty():
            menu.exec_(self.tree.viewport().mapToGlobal(pos))

    @err_catcher(name=__name__)
    def buildNodeMenu(self, menu, item):
        entity = item.data(0, ROLE_ENTITY) or {}
        etype = entity.get("type")
        isLeaf = bool(item.data(0, ROLE_ISLEAF))
        isRollup = bool(item.data(0, ROLE_ROLLUP))
        section = self.sectionOf(item)
        dept = item.data(0, ROLE_DEPT)

        # --- FX layers only have one action ---------------------------------
        if etype == "fxlayer":
            menu.addAction("Delete FX Layer").triggered.connect(
                lambda: self.deleteFxLayer(item)
            )
            return

        # --- creating things ------------------------------------------------
        if dept:
            # A department folder: adding here means adding a task to it.
            menu.addAction("Add Task...").triggered.connect(
                lambda: self.addTaskTo(item, department=dept[0])
            )
            menu.addSeparator()
        elif self.taskMode() and (isRollup or etype == "task"):
            actAddTask = menu.addAction("Add Task...")
            node = item if isRollup else self.rollupAncestor(item)
            if node is not None and node.data(0, ROLE_ISNEW):
                # Its folders do not exist yet, so Prism has nowhere to
                # create a department in.
                actAddTask.setEnabled(False)
                actAddTask.setToolTip(
                    "Save this shot first - it has not been created in Prism yet."
                )
            else:
                actAddTask.triggered.connect(lambda: self.addTaskTo(item))
            menu.addSeparator()

        # Adding a sibling shot/asset is offered from anywhere inside a
        # section, including task-mode rows and department folders, the same
        # as from a shot row in the other mode.
        if section == "assets":
            folder = self.assetFolderForNode(item)
            label = "Add Asset in '%s'" % folder if folder else "Add Asset"
            menu.addAction(label).triggered.connect(
                lambda: self.onAddAsset(folder=folder)
            )
            menu.addSeparator()
        elif section == "shots":
            sequence = self.sequenceForNode(item)
            episode = self.episodeForNode(item)
            label = "Add Shot in '%s'" % sequence if sequence else "Add Shot"
            menu.addAction(label).triggered.connect(
                lambda: self.onAddShot(sequence=sequence, episode=episode)
            )
            if etype == "shot" and not self.taskMode():
                menu.addAction("Add FX Layer").triggered.connect(
                    lambda: self.addFxLayer(item)
                )
            menu.addSeparator()

        # --- folder shortcuts -----------------------------------------------
        # A row that has not been saved yet has no folders on disk, so the
        # shortcuts are shown but inert rather than opening a "does not
        # exist" popup.
        node = item if isRollup else self.rollupAncestor(item)
        pendingRow = bool(item.data(0, ROLE_ISNEW)) or bool(
            node is not None and node.data(0, ROLE_ISNEW)
        )
        folderPath = self.entityFolderPath(entity) if entity else ""
        if folderPath:
            actOpen = menu.addAction("Open Folder in Explorer")
            actOpen.setEnabled(not pendingRow)
            if pendingRow:
                actOpen.setToolTip("Save first - this has not been created yet.")
            actOpen.triggered.connect(lambda: self.openInExplorer(folderPath))
            if etype == "shot":
                renderPath = self.mediaFolderPath(folderPath, "3drenders")
                compPath = self.mediaFolderPath(folderPath, "2drenders")
                actRender = menu.addAction("Open Render Folder")
                actRender.setEnabled(bool(renderPath) and not pendingRow)
                actRender.triggered.connect(lambda: self.openInExplorer(renderPath))
                actComp = menu.addAction("Open Comp Folder")
                actComp.setEnabled(bool(compPath) and not pendingRow)
                actComp.triggered.connect(lambda: self.openInExplorer(compPath))
            menu.addSeparator()

        # --- acting on a row ------------------------------------------------
        # Shots and assets can be renamed or deleted in either mode; a task
        # is a Prism folder and is managed in the Project Browser, not here.
        if (isLeaf or isRollup) and etype in ("shot", "asset"):
            menu.addAction("Rename").triggered.connect(
                lambda: self.renameEntity(item)
            )
            menu.addSeparator()
            menu.addAction("Delete").triggered.connect(
                lambda: self.deleteEntity(item)
            )

    @err_catcher(name=__name__)
    def mediaFolderPath(self, entityFolder, structureKey):
        """Folder holding a shot's renders, resolved from the PROJECT STRUCTURE.

        `structureKey` is a Prism folder-structure key ("3drenders" /
        "2drenders"). Those templates end at the per-identifier level
        (".../Renders/3dRender/@identifier@"), so we resolve with a
        placeholder identifier and step back up one level to get the folder
        that holds them all.

        Resolving instead of joining "Renders/3dRender" by hand means a
        project that renamed those folders still opens the right place.
        """
        if not entityFolder:
            return ""
        try:
            resolved = self.core.projects.getResolvedProjectStructurePath(
                structureKey,
                context={"entity_path": entityFolder, "identifier": "_"},
            )
        except Exception:
            resolved = ""
        if resolved:
            return os.path.dirname(resolved)
        return ""

    @err_catcher(name=__name__)
    def openInExplorer(self, path):
        """Open a folder in the OS file manager (cross-platform)."""
        if path and os.path.isdir(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            return
        # Not found: show the exact path we tried plus what actually exists in
        # the parent, so a structure/naming mismatch is easy to spot.
        msg = "Folder does not exist:\n\n%s" % (path or "(empty path)")
        parent = os.path.dirname(path) if path else ""
        if parent and os.path.isdir(parent):
            try:
                entries = sorted(os.listdir(parent))
            except Exception as e:
                entries = ["<listing failed: %s>" % e]
            msg += "\n\nParent exists:\n%s\n\nContents:\n- %s" % (
                parent, "\n- ".join(entries) if entries else "(empty)"
            )
        elif parent:
            msg += "\n\nParent is also missing:\n%s" % parent
        self.core.popup(msg, parent=self)

    @err_catcher(name=__name__)
    def entityFolderPath(self, entity):
        """Absolute path of the shot/asset/task folder on disk (or '')."""
        if entity.get("type") == "task":
            return self.tasks.taskFolder(
                entity.get("parent") or {},
                entity.get("department", ""),
                entity.get("task", ""),
            )
        try:
            path = self.core.getEntityPath(entity=entity)
        except Exception:
            path = ""
        return os.path.normpath(path) if path else ""

    @err_catcher(name=__name__)
    def folderHasFiles(self, path):
        """True if the folder tree contains any actual files (not just empty
        sub-folders created by Prism when the entity was made)."""
        if not path or not os.path.exists(path):
            return False
        for _root, _dirs, files in os.walk(path):
            if files:
                return True
        return False

    @err_catcher(name=__name__)
    def renameEntity(self, item):
        entity = item.data(0, ROLE_ENTITY)
        isNew = item.data(0, ROLE_ISNEW)
        isShot = entity.get("type") == "shot"
        curName = self.leafDisplayName(entity, isNew)

        newName, ok = QInputDialog.getText(
            self,
            "Rename %s" % ("Shot" if isShot else "Asset"),
            "New name:",
            QLineEdit.Normal,
            curName,
        )
        if not ok:
            return
        newName = (newName or "").strip()
        if not newName or newName == curName:
            return
        if "/" in newName or "\\" in newName:
            self.core.popup("The name may not contain slashes.", parent=self)
            return

        # New (unsaved) rows are only renamed locally.
        if isNew:
            if isShot:
                entity["shot"] = newName
            else:
                parentPath = entity["asset_path"].replace("\\", "/").rsplit("/", 1)
                entity["asset_path"] = (
                    (parentPath[0] + "/" + newName) if len(parentPath) == 2 else newName
                )
                entity["_leafName"] = newName
            self.refresh(preserve=self.captureTreeState())
            return

        if isShot:
            newShot = dict(entity)
            newShot["shot"] = newName
            if self.entityFolderPath(newShot) and os.path.exists(self.entityFolderPath(newShot)):
                self.core.popup("A shot named '%s' already exists." % newName, parent=self)
                return
            try:
                # renameShot handles metadata and prompts on locked files.
                self.core.entities.renameShot(entity, newShot)
            except Exception as e:
                self.core.popup("Could not rename shot:\n\n%s" % e, parent=self)
                return
        else:
            oldPath = self.entityFolderPath(entity)
            if not oldPath or not os.path.exists(oldPath):
                self.core.popup("Asset folder not found on disk.", parent=self)
                return
            newPath = os.path.join(os.path.dirname(oldPath), newName)
            if os.path.exists(newPath):
                self.core.popup("An asset named '%s' already exists." % newName, parent=self)
                return
            try:
                os.rename(oldPath, newPath)
            except (OSError, PermissionError) as e:
                self.core.popup(
                    "Could not rename the asset folder.  It may be in use by "
                    "another program.\n\n%s" % e,
                    parent=self,
                )
                return
            self.migrateAssetData(
                self.core.entities.getAssetNameFromPath(oldPath),
                newName,
            )

        self.refresh(preserve=self.captureTreeState())
        self.l_status.setText("Renamed '%s' to '%s'." % (curName, newName))

    @err_catcher(name=__name__)
    def migrateAssetData(self, oldName, newName):
        """Move an asset's description + tracker metadata to the new name."""
        try:
            path = self.core.configs.getConfigPath(config="assetinfo")
            data = self.core.getConfig(configPath=path) or {}
            if oldName in data:
                data[newName] = data.pop(oldName)
            if "assets" in data and oldName in data["assets"]:
                data["assets"][newName] = data["assets"].pop(oldName)
            self.core.setConfig(data=data, configPath=path, updateNestedData=False)
        except Exception:
            pass

    @err_catcher(name=__name__)
    def deleteEntity(self, item):
        entity = item.data(0, ROLE_ENTITY)
        isNew = item.data(0, ROLE_ISNEW)
        isShot = entity.get("type") == "shot"
        name = self.leafDisplayName(entity, isNew)
        kind = "shot" if isShot else "asset"

        # Unsaved new rows: just drop them, nothing on disk yet.
        if isNew:
            result = self.core.popupQuestion(
                "Remove the unsaved new %s '%s'?" % (kind, name),
                buttons=["Remove", "Cancel"],
                parent=self,
            )
            if result != "Remove":
                return
            try:
                self.newRows.remove(entity)
            except ValueError:
                pass
            self.refresh(preserve=self.captureTreeState())
            return

        folder = self.entityFolderPath(entity)

        # Guard: never let users delete a folder that still holds real data.
        if self.folderHasFiles(folder):
            self.core.popup(
                "The %s folder for '%s' is not empty and contains files.\n\n"
                "To avoid accidentally deleting work, it was not deleted.\n"
                "Please review and delete it manually if you are sure:\n\n%s"
                % (kind, name, folder),
                parent=self,
            )
            return

        result = self.core.popupQuestion(
            "Delete the %s '%s'?\n\nThe (empty) folder will be removed:\n%s"
            % (kind, name, folder or "(no folder on disk)"),
            buttons=["Delete", "Cancel"],
            parent=self,
        )
        if result != "Delete":
            return

        try:
            if folder and os.path.exists(folder):
                shutil.rmtree(folder)
        except (OSError, PermissionError) as e:
            self.core.popup(
                "Could not delete the folder.  It may be in use by another "
                "program.\n\n%s" % e,
                parent=self,
            )
            return

        self.cleanupEntityMetadata(entity)
        self.refresh(preserve=self.captureTreeState())
        self.l_status.setText("Deleted %s '%s'." % (kind, name))

    @err_catcher(name=__name__)
    def cleanupEntityMetadata(self, entity):
        """Remove leftover tracker metadata for a deleted entity."""
        try:
            if entity.get("type") == "asset":
                path = self.core.configs.getConfigPath(config="assetinfo")
                data = self.core.getConfig(configPath=path) or {}
                name = self.core.entities.getAssetNameFromPath(entity["asset_path"])
                data.pop(name, None)
                if "assets" in data:
                    data["assets"].pop(name, None)
                self.core.setConfig(data=data, configPath=path, updateNestedData=False)
            else:
                path = self.core.configs.getConfigPath(config="shotinfo")
                data = self.core.getConfig(configPath=path) or {}
                seq = entity.get("sequence", "")
                shot = entity.get("shot", "")
                episode = entity.get("episode", "")

                # "shots" is always keyed by sequence, but Prism nests
                # "shotRanges" under the episode when a project uses them, so
                # the range would otherwise be left behind as an orphan.
                if "shots" in data and seq in data["shots"]:
                    data["shots"][seq].pop(shot, None)

                ranges = data.get("shotRanges") or {}
                if episode and episode in ranges:
                    if seq in ranges[episode]:
                        ranges[episode][seq].pop(shot, None)
                elif seq in ranges:
                    ranges[seq].pop(shot, None)

                self.core.setConfig(data=data, configPath=path, updateNestedData=False)
        except Exception:
            pass

    @err_catcher(name=__name__)
    def openAbout(self):
        dlg = AboutDialog(self.core, parent=self)
        dlg.exec_()

    @err_catcher(name=__name__)
    def openSettings(self):
        """Edit the status/department options, then rebuild with the new ones."""
        import TrackerConfigDlg

        dlg = TrackerConfigDlg.TrackerConfigDlg(self.core, parent=self)
        if dlg.exec_() == QDialog.Accepted:
            self.refresh(preserve=self.captureTreeState())

    @err_catcher(name=__name__)
    def openNotifyLog(self):
        dlg = TrackerNotifyLogDlg.NotifyLogDialog(self.core, self.notify, parent=self)
        dlg.exec_()

    # --------------------------------------------------------------- save ---
    @err_catcher(name=__name__)
    def parseRange(self, text):
        text = (text or "").strip()
        if not text:
            return None
        for sep in ("-", ":", " "):
            if sep in text:
                parts = [p for p in text.split(sep) if p.strip()]
                if len(parts) == 2:
                    try:
                        return [int(parts[0]), int(parts[1])]
                    except ValueError:
                        return None
        return None

    @err_catcher(name=__name__)
    def refreshPrismUI(self):
        """Refresh Prism's Project Browser, if it is open, so changes made
        here (new shots/assets, frame ranges) show up without a manual
        refresh. Prism doesn't watch for these changes on its own.
        """
        pb = getattr(self.core, "pb", None)
        if pb and self.core.isObjectValid(pb) and pb.isVisible():
            pb.refreshUI()

    @err_catcher(name=__name__)
    def repairShowFlags(self):
        """Unset "show" on structured metadata written before it was fixed.

        Such an entry makes Prism's own Scene Browser raise as soon as the
        entity is selected, so it is repaired here rather than left for the
        user to hit. Only entities that actually carry the bad flag are
        written, so this costs nothing once a project is clean.
        """
        fixed = 0
        for entity in self._existingEntities:
            try:
                meta = self.core.entities.getMetaData(entity) or {}
            except Exception:
                continue

            changed = False
            for key in UNSHOWN_KEYS:
                entry = meta.get(key)
                if isinstance(entry, dict) and entry.get("show"):
                    entry["show"] = False
                    changed = True

            if changed:
                try:
                    self.core.entities.setMetaData(entity=entity, metaData=meta)
                    fixed += 1
                except Exception:
                    pass
        return fixed

    @err_catcher(name=__name__)
    def onSave(self):
        # Pull the latest data so our merge is based on current values.
        try:
            self.core.configs.clearCache()
        except Exception:
            pass

        # Do this before refreshPrismUI() below, or refreshing the Project
        # Browser would walk straight into the entry that breaks it.
        self.repairShowFlags()

        created = 0
        updated = 0
        errors = []
        self._conflicts = []

        # In task mode a shot/asset is a rollup rather than a leaf, so it has
        # to be collected explicitly or a new one would never be created and
        # an edited description/range never written.
        for item in self.iterLeafItems() + self.rollupItems():
            entity = item.data(0, ROLE_ENTITY)
            isNew = item.data(0, ROLE_ISNEW)
            isRollup = bool(item.data(0, ROLE_ROLLUP))

            # Create the entity in Prism if it is new.
            if isNew:
                frameRange = None
                if entity.get("type") == "shot":
                    frameRange = self.parseRange(self.getRowValues(item)["range"])
                createEntity = {"type": entity["type"]}
                if entity["type"] == "shot":
                    createEntity["sequence"] = entity["sequence"]
                    createEntity["shot"] = entity["shot"]
                    if entity.get("episode"):
                        createEntity["episode"] = entity["episode"]
                    if frameRange is None and entity.get("_start") is not None:
                        frameRange = [entity["_start"], entity["_end"]]
                else:
                    createEntity["asset_path"] = entity["asset_path"]
                try:
                    self.core.entities.createEntity(createEntity, frameRange=frameRange)
                    created += 1
                except Exception as e:
                    errors.append("%s: %s" % (self.leafDisplayName(entity, True), e))
                    continue
                # The row is now a real entity; persist its fields below.
                item.setData(0, ROLE_ENTITY, createEntity)
                item.setData(0, ROLE_ISNEW, False)

            if isRollup:
                ok, errs = self.writeRollupItem(item)
            else:
                ok, errs = self.writeExistingItem(item)
            if ok:
                updated += 1
            errors.extend(errs)

        fxSaved, fxErrs = self.saveAllFxLayers()
        updated += fxSaved
        errors.extend(fxErrs)

        self.newRows = []

        if errors:
            self.core.popup(
                "Saved with %d issue(s):\n\n%s" % (len(errors), "\n".join(errors)),
                parent=self,
            )

        self.refresh()
        self.refreshPrismUI()
        msg = "Saved. %d created, %d updated." % (created, updated)
        if self._conflicts:
            msg += "  " + self.conflictMessage()
        self.l_status.setText(msg)
