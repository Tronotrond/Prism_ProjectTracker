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
# Production Tracker - Settings dialog
#
# Picks the tracker mode and edits the per-project department options: add,
# remove, rename, reorder and recolour. Reached via Configuration -> Settings.
#
# "Approved" is not listed here. It is appended as the last department of
# both lists automatically and marks the end of the pipeline, so "finished"
# stays one fixed, recognisable value.
#
####################################################


from qtpy.QtCore import *
from qtpy.QtGui import *
from qtpy.QtWidgets import *

from PrismUtils.Decorators import err_catcher_plugin as err_catcher

import TrackerSettings


COL_COLOR = 0
COL_NAME = 1
COL_COUNT = 2

ROLE_COLOR = Qt.UserRole + 1

SWATCH_W = 34
SWATCH_H = 16

ARROW_SIZE = 12
ARROW_COLOR = "#cbd5e1"


def makeSwatch(color):
    """A solid rounded colour chip.

    Drawn as an icon rather than set as the item's background: Prism styles
    its tables with a stylesheet, and a stylesheet background wins over
    QTableWidgetItem.setBackground, which left the column looking empty.
    """
    pm = QPixmap(SWATCH_W, SWATCH_H)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(QPen(QColor("#00000060")))
    p.setBrush(QBrush(QColor(color)))
    p.drawRoundedRect(0, 0, SWATCH_W - 1, SWATCH_H - 1, 4, 4)
    p.end()
    return pm


def makeArrow(up):
    """A solid triangle icon pointing up or down.

    Painted rather than left to QToolButton.setArrowType: the style draws
    up-arrows and down-arrows from different stylesheet rules, so the two
    reorder buttons came out looking nothing like each other. Drawing both
    here keeps them a matching pair.
    """
    pm = QPixmap(ARROW_SIZE, ARROW_SIZE)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(QColor(ARROW_COLOR)))

    inset = 1.5
    left = inset
    right = ARROW_SIZE - inset
    top = inset + 1
    bottom = ARROW_SIZE - inset - 1
    middle = ARROW_SIZE / 2.0
    if up:
        points = [QPointF(middle, top), QPointF(right, bottom), QPointF(left, bottom)]
    else:
        points = [QPointF(left, top), QPointF(right, top), QPointF(middle, bottom)]
    p.drawPolygon(QPolygonF(points))
    p.end()
    return pm


class OptionTable(QTableWidget):
    """An editable Name + Colour list."""

    def __init__(self, parent=None):
        super(OptionTable, self).__init__(0, COL_COUNT, parent)
        self.setHorizontalHeaderLabels(["Colour", "Name"])
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
        )
        self.verticalHeader().setDefaultSectionSize(28)
        self.setIconSize(QSize(SWATCH_W, SWATCH_H))

        hh = self.horizontalHeader()
        self.setColumnWidth(COL_COLOR, 70)
        hh.setSectionResizeMode(COL_COLOR, QHeaderView.Fixed)
        hh.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)

        self.cellDoubleClicked.connect(self._onCellDoubleClicked)

    # ------------------------------------------------------------- rows ----
    def setRows(self, rows):
        self.blockSignals(True)
        self.setRowCount(0)
        for row in rows or []:
            self._append(row)
        self.blockSignals(False)

    def _append(self, data):
        row = self.rowCount()
        self.insertRow(row)

        swatch = QTableWidgetItem()
        swatch.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        swatch.setToolTip("Double-click to change the colour")
        swatch.setTextAlignment(Qt.AlignCenter)
        self.setItem(row, COL_COLOR, swatch)
        self._setColor(row, data.get("color") or TrackerSettings.NEUTRAL_COLOR)

        self.setItem(row, COL_NAME, QTableWidgetItem(data.get("name", "")))

    def addRow(self, name="New department", color="#3b82f6"):
        self.blockSignals(True)
        self._append({"name": name, "color": color})
        self.blockSignals(False)
        row = self.rowCount() - 1
        self.setCurrentCell(row, COL_NAME)
        self.editItem(self.item(row, COL_NAME))

    def removeSelected(self):
        row = self.currentRow()
        if row >= 0:
            self.removeRow(row)

    def move(self, delta):
        row = self.currentRow()
        target = row + delta
        if row < 0 or target < 0 or target >= self.rowCount():
            return
        rows = self.rows()
        rows[row], rows[target] = rows[target], rows[row]
        self.setRows(rows)
        self.setCurrentCell(target, COL_NAME)

    def rows(self):
        """Current contents; blank names are dropped."""
        out = []
        for row in range(self.rowCount()):
            nameItem = self.item(row, COL_NAME)
            name = nameItem.text().strip() if nameItem else ""
            if not name:
                continue
            out.append({"name": name, "color": self._color(row)})
        return out

    # ----------------------------------------------------------- colour ----
    def _color(self, row):
        item = self.item(row, COL_COLOR)
        return (item.data(ROLE_COLOR) if item else "") or TrackerSettings.NEUTRAL_COLOR

    def _setColor(self, row, color):
        item = self.item(row, COL_COLOR)
        if not item:
            return
        item.setData(ROLE_COLOR, color)
        item.setIcon(QIcon(makeSwatch(color)))

    def pickColor(self):
        row = self.currentRow()
        if row < 0:
            return False
        chosen = QColorDialog.getColor(QColor(self._color(row)), self, "Pick colour")
        if not chosen.isValid():
            return False
        self.blockSignals(True)
        self._setColor(row, chosen.name())
        self.blockSignals(False)
        return True

    def _onCellDoubleClicked(self, row, col):
        if col == COL_COLOR:
            self.pickColor()


class TableBlock(QWidget):
    """An OptionTable plus its Add / Remove / reorder / colour buttons."""

    def __init__(self, title, hint="", parent=None):
        super(TableBlock, self).__init__(parent)
        lo = QVBoxLayout()
        lo.setContentsMargins(0, 0, 0, 0)
        self.setLayout(lo)

        if title:
            label = QLabel(title)
            f = label.font()
            f.setBold(True)
            label.setFont(f)
            lo.addWidget(label)

        if hint:
            h = QLabel(hint)
            h.setWordWrap(True)
            h.setStyleSheet("color:#9ca3af;")
            lo.addWidget(h)

        self.table = OptionTable()
        # Tall enough for the default lists plus a couple of additions before
        # it needs to scroll.
        self.table.setMinimumHeight(200)
        lo.addWidget(self.table, 1)

        btnLo = QHBoxLayout()
        self.b_add = QPushButton("Add")
        self.b_remove = QPushButton("Remove")
        self.b_color = QPushButton("Colour...")
        self.b_up = QToolButton()
        self.b_up.setIcon(QIcon(makeArrow(True)))
        self.b_up.setIconSize(QSize(ARROW_SIZE, ARROW_SIZE))
        self.b_up.setToolTip("Move up")
        self.b_down = QToolButton()
        self.b_down.setIcon(QIcon(makeArrow(False)))
        self.b_down.setIconSize(QSize(ARROW_SIZE, ARROW_SIZE))
        self.b_down.setToolTip("Move down")
        for w in (self.b_add, self.b_remove, self.b_color, self.b_up, self.b_down):
            btnLo.addWidget(w)
        btnLo.addStretch()

        self.l_tail = QLabel(
            "'%s' is added automatically as the last entry."
            % TrackerSettings.APPROVED_DEPT
        )
        self.l_tail.setStyleSheet("color:#9ca3af;")
        btnLo.addWidget(self.l_tail)
        lo.addLayout(btnLo)

        self.b_add.clicked.connect(lambda: self.table.addRow())
        self.b_remove.clicked.connect(self.table.removeSelected)
        self.b_color.clicked.connect(self.table.pickColor)
        self.b_up.clicked.connect(lambda: self.table.move(-1))
        self.b_down.clicked.connect(lambda: self.table.move(1))


class TrackerConfigDlg(QDialog):
    """Per-project department options."""

    def __init__(self, core, parent=None):
        super(TrackerConfigDlg, self).__init__()
        self.core = core
        self.settings = TrackerSettings.TrackerSettings(core)

        self.core.parentWindow(self, parent=parent)
        self.setWindowTitle("Production Tracker - Settings")
        self.resize(700, 720)

        self.setupUi()
        self.loadAll()

    # ------------------------------------------------------------------ UI --
    @err_catcher(name=__name__)
    def setupUi(self):
        lo = QVBoxLayout()
        lo.setContentsMargins(10, 10, 10, 10)
        lo.setSpacing(8)
        self.setLayout(lo)

        # --- Tracker mode ---------------------------------------------------
        modeGroup = QGroupBox("Tracker Mode")
        modeLo = QVBoxLayout()
        modeGroup.setLayout(modeLo)

        self.rb_modeShot = QRadioButton("One task per shot")
        self.rb_modeShot.setToolTip(
            "One row per shot and asset, with a Department column you set "
            "yourself."
        )
        self.rb_modeTasks = QRadioButton("Use Prism departments and tasks")
        self.rb_modeTasks.setToolTip(
            "Group each shot by its real Prism departments and the tasks "
            "inside them, as the Project Browser shows them."
        )
        modeLo.addWidget(self.rb_modeShot)
        modeLo.addWidget(self.rb_modeTasks)

        self.l_modeHint = QLabel("")
        self.l_modeHint.setWordWrap(True)
        self.l_modeHint.setStyleSheet("color:#9ca3af;")
        modeLo.addWidget(self.l_modeHint)
        lo.addWidget(modeGroup)

        self.rb_modeShot.toggled.connect(self.updateModeHint)

        # Everything below only applies to the "one task per shot" mode, so
        # it is hidden wholesale when Prism departments/tasks are in use -
        # there the departments come from Prism, not from this list.
        self.deptSection = QWidget()
        deptLo = QVBoxLayout()
        deptLo.setContentsMargins(0, 0, 0, 0)
        deptLo.setSpacing(8)
        self.deptSection.setLayout(deptLo)
        lo.addWidget(self.deptSection, 1)

        note = QLabel(
            "Departments for this project. Shots and assets move through "
            "different pipelines, so each has its own list, and '%s' is always "
            "added last to mark the end of the pipeline.\n\n"
            "Renaming a department does not rewrite shots and assets that "
            "already use the old name - they keep it, and it stays available "
            "in their dropdown until you set them to something else."
            % TrackerSettings.APPROVED_DEPT
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#9ca3af;")
        deptLo.addWidget(note)

        self.shotDeptBlock = TableBlock("Shot departments")
        self.assetDeptBlock = TableBlock("Asset departments")
        deptLo.addWidget(self.shotDeptBlock, 1)
        deptLo.addWidget(self.assetDeptBlock, 1)

        fxGroup = QGroupBox("FX layer department")
        fxLo = QHBoxLayout()
        fxGroup.setLayout(fxLo)
        fxLo.addWidget(QLabel("Name:"))
        self.e_fxName = QLineEdit()
        self.e_fxName.setToolTip("FX layer rows are locked to this single department.")
        fxLo.addWidget(self.e_fxName, 1)
        self.b_fxColor = QPushButton("Colour...")
        self.b_fxColor.setFixedWidth(90)
        fxLo.addWidget(self.b_fxColor)
        self.l_fxSwatch = QLabel()
        self.l_fxSwatch.setFixedSize(SWATCH_W, 20)
        fxLo.addWidget(self.l_fxSwatch)
        deptLo.addWidget(fxGroup)

        # --- Footer ---------------------------------------------------------
        footer = QHBoxLayout()
        self.b_defaults = QPushButton("Restore Defaults")
        self.b_defaults.setToolTip("Reset every department back to the defaults.")
        footer.addWidget(self.b_defaults)
        footer.addStretch()
        self.b_save = QPushButton("Save")
        self.b_save.setStyleSheet(
            "QPushButton{background-color:#2563eb; color:white; font-weight:bold;"
            " padding:4px 16px;} QPushButton:hover{background-color:#1d4ed8;}"
        )
        self.b_cancel = QPushButton("Cancel")
        footer.addWidget(self.b_save)
        footer.addWidget(self.b_cancel)
        lo.addLayout(footer)

        self.b_fxColor.clicked.connect(self.onPickFxColor)
        self.b_defaults.clicked.connect(self.onRestoreDefaults)
        self.b_save.clicked.connect(self.onSave)
        self.b_cancel.clicked.connect(self.reject)

    @err_catcher(name=__name__)
    def updateModeHint(self, *args):
        """Say what the chosen mode changes, and hide what it does not use."""
        taskMode = self.rb_modeTasks.isChecked()
        self.deptSection.setVisible(not taskMode)
        self.b_defaults.setVisible(not taskMode)
        # Let the dialog shrink back down when the section is hidden.
        self.resize(self.width(), self.sizeHint().height())

        if taskMode:
            self.l_modeHint.setText(
                "Each task is tracked on its own and the shot row becomes a "
                "summary of the tasks under it. The Department column is "
                "hidden, since the tree groups by department instead. Task "
                "data is stored with the task, so it is separate from "
                "anything entered in the other mode."
            )
        else:
            self.l_modeHint.setText(
                "Each shot and asset is a single tracked row with its own "
                "Department setting."
            )

    # --------------------------------------------------------------- data ---
    @err_catcher(name=__name__)
    def loadAll(self):
        if not self.settings.hasProject():
            self.core.popup("No project is currently open.", parent=self)

        mode = self.settings.getTrackerMode()
        self.rb_modeTasks.setChecked(mode == TrackerSettings.MODE_TASKS)
        self.rb_modeShot.setChecked(mode != TrackerSettings.MODE_TASKS)
        self.updateModeHint()

        depts = self.settings.getDepartments()
        self.shotDeptBlock.table.setRows(depts["shot"])
        self.assetDeptBlock.table.setRows(depts["asset"])
        self.setFxDepartment(depts["fxlayer"])

    @err_catcher(name=__name__)
    def setFxDepartment(self, fx):
        self.e_fxName.setText(fx.get("name", ""))
        self._fxColor = fx.get("color") or TrackerSettings.NEUTRAL_COLOR
        self.refreshFxSwatch()

    @err_catcher(name=__name__)
    def refreshFxSwatch(self):
        self.l_fxSwatch.setStyleSheet(
            "background:%s; border:1px solid #3d3d3d;" % self._fxColor
        )

    @err_catcher(name=__name__)
    def onPickFxColor(self):
        chosen = QColorDialog.getColor(QColor(self._fxColor), self, "Pick colour")
        if chosen.isValid():
            self._fxColor = chosen.name()
            self.refreshFxSwatch()

    @err_catcher(name=__name__)
    def onRestoreDefaults(self):
        if self.core.popupQuestion(
            "Restore the default departments?\n\nThis only changes the options "
            "offered from now on; shots and assets keep the values they "
            "already have.",
            buttons=["Restore", "Cancel"],
            parent=self,
        ) != "Restore":
            return

        defaults = TrackerSettings.DEFAULT_DEPARTMENTS
        self.shotDeptBlock.table.setRows([dict(d) for d in defaults["shot"]])
        self.assetDeptBlock.table.setRows([dict(d) for d in defaults["asset"]])
        self.setFxDepartment(dict(defaults["fxlayer"]))

    # --------------------------------------------------------------- save ---
    @err_catcher(name=__name__)
    def duplicateName(self, rows):
        seen = set()
        for r in rows:
            key = r["name"].lower()
            if key in seen:
                return r["name"]
            seen.add(key)
        return None

    @err_catcher(name=__name__)
    def validate(self, rows, label):
        """Returns an error string, or None when the list is usable."""
        dup = self.duplicateName(rows)
        if dup:
            return "Two %s departments are both called '%s'." % (label, dup)
        for r in rows:
            if r["name"].lower() == TrackerSettings.APPROVED_DEPT.lower():
                return (
                    "'%s' is added to every list automatically, so it cannot "
                    "be added by hand. Remove it from the %s departments."
                    % (TrackerSettings.APPROVED_DEPT, label)
                )
        return None

    @err_catcher(name=__name__)
    def onSave(self):
        if not self.settings.hasProject():
            self.core.popup(
                "No project is open, so there is nowhere to save to.", parent=self
            )
            return

        shotDepts = self.shotDeptBlock.table.rows()
        assetDepts = self.assetDeptBlock.table.rows()
        for rows, label in ((shotDepts, "shot"), (assetDepts, "asset")):
            error = self.validate(rows, label)
            if error:
                self.core.popup(error, parent=self)
                return

        fxName = self.e_fxName.text().strip()
        if not fxName:
            self.core.popup(
                "The FX layer department needs a name.\n\nFX layer rows are "
                "locked to it, so it cannot be blank.",
                parent=self,
            )
            return

        departments = {
            "shot": shotDepts,
            "asset": assetDepts,
            "fxlayer": {"name": fxName, "color": self._fxColor},
        }

        mode = (
            TrackerSettings.MODE_TASKS
            if self.rb_modeTasks.isChecked()
            else TrackerSettings.MODE_SHOT
        )

        try:
            self.settings.saveTrackerMode(mode)
            self.settings.saveDepartments(departments)
        except Exception as e:
            self.core.popup("Could not save the settings:\n\n%s" % e, parent=self)
            return

        self.accept()
