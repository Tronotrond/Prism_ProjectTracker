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
# Production Tracker - User Management dialog
#
# One place to configure everything people-related for THIS project:
#
#   * a master Enable/Disable switch for the whole user system,
#   * email notification settings (relay host + sender address),
#   * the team roster (Full name, Username, Email, avatar colour),
#   * an Import button that pulls a roster out of another Prism project.
#
# Everything is stored inside the project (see TrackerSettings), so there is
# no shared network location and no global state.
#
# With the master switch off the tracker stops using the roster entirely:
# comments are authored under the plain Prism username and mentions and
# notifications are inactive.
#
####################################################


import os
import random

from qtpy.QtCore import *
from qtpy.QtGui import *
from qtpy.QtWidgets import *

from PrismUtils.Decorators import err_catcher_plugin as err_catcher

import TrackerSettings
import TrackerTeam


COL_AVATAR = 0
COL_NAME = 1
COL_USER = 2
COL_EMAIL = 3
COL_COUNT = 4

ROLE_COLOR = Qt.UserRole + 1


class UserManagerDlg(QDialog):
    def __init__(self, core, plugin, parent=None):
        super(UserManagerDlg, self).__init__()
        self.core = core
        self.plugin = plugin
        self.settings = TrackerSettings.TrackerSettings(core)
        self.team = TrackerTeam.TrackerTeam(core)

        self.core.parentWindow(self, parent=parent)
        self.setWindowTitle("Production Tracker - User Management")
        self.resize(760, 620)

        self.setupUi()
        self.loadAll()

    # ------------------------------------------------------------------ UI --
    @err_catcher(name=__name__)
    def setupUi(self):
        lo = QVBoxLayout()
        lo.setContentsMargins(10, 10, 10, 10)
        lo.setSpacing(8)
        self.setLayout(lo)

        # --- Master switch + Import -----------------------------------------
        topRow = QHBoxLayout()
        self.cb_enabled = QCheckBox("Enable user management for this project")
        f = self.cb_enabled.font()
        f.setBold(True)
        self.cb_enabled.setFont(f)
        self.cb_enabled.setToolTip(
            "Off: comments are posted under the plain Prism username, and "
            "mentions and notifications are inactive.\n"
            "On: the team roster below drives assignees, @mentions, "
            "reminders and email notifications."
        )
        topRow.addWidget(self.cb_enabled)
        topRow.addStretch()

        self.b_import = QPushButton("Import...")
        self.b_import.setToolTip(
            "Pick another Prism project folder and pull its team roster into "
            "this project."
        )
        topRow.addWidget(self.b_import)
        lo.addLayout(topRow)

        self.l_hint = QLabel("")
        self.l_hint.setWordWrap(True)
        self.l_hint.setStyleSheet("color:#9ca3af;")
        lo.addWidget(self.l_hint)

        # --- Everything the master switch gates -----------------------------
        self.body = QWidget()
        bodyLo = QVBoxLayout()
        bodyLo.setContentsMargins(0, 4, 0, 0)
        bodyLo.setSpacing(8)
        self.body.setLayout(bodyLo)
        lo.addWidget(self.body, 1)

        bodyLo.addWidget(self.buildNotifyGroup())
        bodyLo.addWidget(self.buildTeamGroup(), 1)

        # --- Save / Close ---------------------------------------------------
        footer = QHBoxLayout()
        footer.addStretch()
        self.b_save = QPushButton("Save")
        self.b_save.setStyleSheet(
            "QPushButton{background-color:#2563eb; color:white; font-weight:bold;"
            " padding:4px 16px;} QPushButton:hover{background-color:#1d4ed8;}"
        )
        self.b_close = QPushButton("Close")
        footer.addWidget(self.b_save)
        footer.addWidget(self.b_close)
        lo.addLayout(footer)

        self.cb_enabled.toggled.connect(self.updateEnabledStates)
        self.cb_notify.toggled.connect(self.updateEnabledStates)
        self.b_import.clicked.connect(self.onImport)
        self.b_add.clicked.connect(self.onAdd)
        self.b_remove.clicked.connect(self.onRemove)
        self.b_pickColor.clicked.connect(self.onPickColor)
        self.b_recolor.clicked.connect(self.onRecolor)
        self.b_save.clicked.connect(self.onSave)
        self.b_close.clicked.connect(self.close)

    @err_catcher(name=__name__)
    def buildNotifyGroup(self):
        grp = QGroupBox("Email Notifications")
        glo = QVBoxLayout()
        grp.setLayout(glo)

        self.cb_notify = QCheckBox("Enable email notifications")
        self.cb_notify.setToolTip(
            "Send an email when someone is @mentioned, when a comment is "
            "replied to, or when a @remindme subscription fires."
        )
        glo.addWidget(self.cb_notify)

        self.notifyFields = QWidget()
        form = QFormLayout()
        form.setContentsMargins(18, 4, 0, 0)
        self.notifyFields.setLayout(form)

        hostRow = QHBoxLayout()
        self.e_smtpHost = QLineEdit()
        self.e_smtpHost.setPlaceholderText("smtp.example.com")
        self.sp_smtpPort = QSpinBox()
        self.sp_smtpPort.setRange(1, 65535)
        self.sp_smtpPort.setValue(25)
        self.sp_smtpPort.setFixedWidth(80)
        hostRow.addWidget(self.e_smtpHost, 1)
        hostRow.addWidget(QLabel("Port:"))
        hostRow.addWidget(self.sp_smtpPort)
        form.addRow("SMTP server:", hostRow)

        self.e_fromAddr = QLineEdit()
        self.e_fromAddr.setPlaceholderText("prism-tracker@example.com")
        self.e_fromAddr.setToolTip("Address notification emails are sent from.")
        form.addRow("Sender address:", self.e_fromAddr)

        self.cb_tls = QCheckBox("Use STARTTLS")
        form.addRow("", self.cb_tls)

        glo.addWidget(self.notifyFields)
        return grp

    @err_catcher(name=__name__)
    def buildTeamGroup(self):
        grp = QGroupBox("Team Members")
        glo = QVBoxLayout()
        grp.setLayout(glo)

        info = QLabel(
            "The 'Username' should match the system/Prism login so the tracker "
            "can recognise each person; 'Email' is only needed for notifications."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color:#9ca3af;")
        glo.addWidget(info)

        self.table = QTableWidget(0, COL_COUNT)
        self.table.setHorizontalHeaderLabels(["Avatar", "Full Name", "Username", "Email"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
        )
        hh = self.table.horizontalHeader()
        self.table.setColumnWidth(COL_AVATAR, 60)
        hh.setSectionResizeMode(COL_AVATAR, QHeaderView.Fixed)
        hh.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        hh.setSectionResizeMode(COL_USER, QHeaderView.Stretch)
        hh.setSectionResizeMode(COL_EMAIL, QHeaderView.Stretch)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.itemChanged.connect(self.onItemChanged)
        glo.addWidget(self.table, 1)

        btnLo = QHBoxLayout()
        self.b_add = QPushButton("Add User")
        self.b_remove = QPushButton("Remove Selected")
        self.b_pickColor = QPushButton("Pick Color...")
        self.b_recolor = QPushButton("Random Color")
        btnLo.addWidget(self.b_add)
        btnLo.addWidget(self.b_remove)
        btnLo.addWidget(self.b_pickColor)
        btnLo.addWidget(self.b_recolor)
        btnLo.addStretch()
        glo.addLayout(btnLo)
        return grp

    # ------------------------------------------------------------- states ---
    @err_catcher(name=__name__)
    def updateEnabledStates(self, *args):
        on = self.cb_enabled.isChecked()
        self.body.setEnabled(on)
        self.notifyFields.setEnabled(on and self.cb_notify.isChecked())
        if on:
            self.l_hint.setText(
                "Assignees, @mentions, @remindme subscriptions and avatars use "
                "the roster below."
            )
        else:
            self.l_hint.setText(
                "User management is off: comments are posted under the plain "
                "Prism username, and mentions and notifications are inactive. "
                "The roster below is kept, just unused."
            )

    # --------------------------------------------------------------- data ---
    @err_catcher(name=__name__)
    def loadAll(self):
        if not self.settings.hasProject():
            self.core.popup("No project is currently open.", parent=self)

        s = self.settings.getSettings()
        self.cb_enabled.setChecked(s["enabled"])
        self.cb_notify.setChecked(s["notify_enabled"])
        self.e_smtpHost.setText(s["smtp_host"])
        self.sp_smtpPort.setValue(s["smtp_port"])
        self.e_fromAddr.setText(s["from_addr"])
        self.cb_tls.setChecked(s["use_tls"])

        # ignoreEnabled: the roster must stay visible/editable even while the
        # feature is switched off.
        self.setUsers(self.team.loadUsers(ignoreEnabled=True))
        self.updateEnabledStates()

    @err_catcher(name=__name__)
    def setUsers(self, users):
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for u in users or []:
            self.appendRow(u)
        self.table.blockSignals(False)

    @err_catcher(name=__name__)
    def appendRow(self, user):
        row = self.table.rowCount()
        self.table.insertRow(row)

        avatarItem = QTableWidgetItem()
        avatarItem.setFlags(Qt.ItemIsEnabled)
        avatarItem.setTextAlignment(Qt.AlignCenter)
        avatarItem.setData(ROLE_COLOR, user.get("color", ""))
        self.table.setItem(row, COL_AVATAR, avatarItem)

        self.table.setItem(row, COL_NAME, QTableWidgetItem(user.get("full_name", "")))
        self.table.setItem(row, COL_USER, QTableWidgetItem(user.get("username", "")))
        self.table.setItem(row, COL_EMAIL, QTableWidgetItem(user.get("email", "")))
        self.refreshAvatar(row)

    @err_catcher(name=__name__)
    def refreshAvatar(self, row):
        nameItem = self.table.item(row, COL_NAME)
        userItem = self.table.item(row, COL_USER)
        avatarItem = self.table.item(row, COL_AVATAR)
        if not avatarItem:
            return
        color = avatarItem.data(ROLE_COLOR)
        name = nameItem.text() if nameItem else ""
        username = userItem.text() if userItem else ""
        if not color:
            color = self.team.colorForName(name or username)
            avatarItem.setData(ROLE_COLOR, color)
        pm = self.team.makeAvatar(name, color, username=username, size=28)
        avatarItem.setIcon(QIcon(pm))

    @err_catcher(name=__name__)
    def onItemChanged(self, item):
        if item.column() in (COL_NAME, COL_USER):
            self.refreshAvatar(item.row())

    @err_catcher(name=__name__)
    def usedColors(self):
        used = set()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, COL_AVATAR)
            if item and item.data(ROLE_COLOR):
                used.add(item.data(ROLE_COLOR))
        return used

    @err_catcher(name=__name__)
    def onAdd(self):
        free = [c for c in TrackerTeam.AVATAR_PALETTE if c not in self.usedColors()]
        color = random.choice(free) if free else random.choice(TrackerTeam.AVATAR_PALETTE)
        self.appendRow({"full_name": "New User", "username": "", "email": "", "color": color})
        row = self.table.rowCount() - 1
        self.table.setCurrentCell(row, COL_NAME)
        self.table.editItem(self.table.item(row, COL_NAME))

    @err_catcher(name=__name__)
    def onRemove(self):
        rows = sorted({i.row() for i in self.table.selectedItems()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)

    @err_catcher(name=__name__)
    def onRecolor(self):
        row = self.table.currentRow()
        if row < 0:
            return
        avatarItem = self.table.item(row, COL_AVATAR)
        if avatarItem:
            avatarItem.setData(ROLE_COLOR, random.choice(TrackerTeam.AVATAR_PALETTE))
            self.refreshAvatar(row)

    @err_catcher(name=__name__)
    def onPickColor(self):
        row = self.table.currentRow()
        if row < 0:
            self.core.popup("Select a user row first.", parent=self)
            return
        avatarItem = self.table.item(row, COL_AVATAR)
        if not avatarItem:
            return
        current = avatarItem.data(ROLE_COLOR) or "#3b82f6"
        color = QColorDialog.getColor(QColor(current), self, "Pick avatar color")
        if color.isValid():
            avatarItem.setData(ROLE_COLOR, color.name())
            self.refreshAvatar(row)

    # ------------------------------------------------------------- import ---
    @err_catcher(name=__name__)
    def onImport(self):
        """Pull a roster out of another Prism project.

        The user picks the PROJECT folder - we locate the tracker config
        inside it ourselves, using that project's own folder structure, so
        nobody has to know where the data is kept.
        """
        folder = QFileDialog.getExistingDirectory(
            self, "Select a Prism project folder", self.importStartDir()
        )
        if not folder:
            return

        if not self.settings.isPrismProject(folder):
            self.core.popup(
                "That folder is not a Prism project (no project config was "
                "found):\n\n%s" % folder,
                parent=self,
            )
            return

        configPath = self.settings.findConfigInProject(folder)
        if not configPath:
            self.core.popup(
                "That project has no Production Tracker user data:\n\n%s" % folder,
                parent=self,
            )
            return

        imported = self.settings.importUsersFrom(folder)
        if not imported:
            self.core.popup(
                "No team members were found in that project's tracker data:"
                "\n\n%s" % configPath,
                parent=self,
            )
            return

        if self.table.rowCount() == 0:
            self.setUsers(imported)
            self.core.popup(
                "Imported %d team member(s).\n\nReview the list and press Save "
                "to store them in this project." % len(imported),
                severity="info",
                parent=self,
            )
            return

        result = self.core.popupQuestion(
            "This project already has %d team member(s) and the selected "
            "project has %d.\n\nMerge keeps the current list and adds only "
            "people who aren't in it yet.\nReplace discards the current list."
            "\n\nNothing is written until you press Save."
            % (self.table.rowCount(), len(imported)),
            buttons=["Merge", "Replace", "Cancel"],
            parent=self,
        )

        if result == "Replace":
            self.setUsers(imported)
            self.core.popup(
                "Replaced the list with %d imported team member(s).\n\nPress "
                "Save to store them in this project." % len(imported),
                severity="info",
                parent=self,
            )
        elif result == "Merge":
            added, skipped = self.mergeUsers(imported)
            self.core.popup(
                "Added %d team member(s); %d were already present.\n\nPress "
                "Save to store them in this project." % (added, skipped),
                severity="info",
                parent=self,
            )

    @err_catcher(name=__name__)
    def importStartDir(self):
        """Start the browser next to the current project, not at random."""
        current = getattr(self.core, "projectPath", "") or ""
        parent = os.path.dirname(os.path.normpath(current)) if current else ""
        return parent if parent and os.path.isdir(parent) else current

    @err_catcher(name=__name__)
    def mergeUsers(self, imported):
        """Append people who aren't in the table yet. Returns (added, skipped).

        Identity is the username when there is one (that's what mentions and
        the current-user match key on); otherwise the full name.
        """
        existing = set()
        for u in self.collectUsers():
            existing.add(self.userKey(u))

        added = 0
        skipped = 0
        for u in imported:
            if self.userKey(u) in existing:
                skipped += 1
                continue
            self.appendRow(u)
            existing.add(self.userKey(u))
            added += 1
        return added, skipped

    @err_catcher(name=__name__)
    def userKey(self, user):
        username = (user.get("username") or "").strip().lower()
        if username:
            return ("u", username)
        return ("n", (user.get("full_name") or "").strip().lower())

    # --------------------------------------------------------------- save ---
    @err_catcher(name=__name__)
    def collectUsers(self):
        users = []
        seen = set()
        for row in range(self.table.rowCount()):
            name = self.table.item(row, COL_NAME).text().strip() if self.table.item(row, COL_NAME) else ""
            username = self.table.item(row, COL_USER).text().strip() if self.table.item(row, COL_USER) else ""
            email = self.table.item(row, COL_EMAIL).text().strip() if self.table.item(row, COL_EMAIL) else ""
            color = self.table.item(row, COL_AVATAR).data(ROLE_COLOR) if self.table.item(row, COL_AVATAR) else ""
            if not name and not username:
                continue
            key = (username.lower(), name.lower())
            if key in seen:
                continue
            seen.add(key)
            users.append(
                {
                    "full_name": name,
                    "username": username,
                    "email": email,
                    "color": color or self.team.colorForName(name or username),
                }
            )
        return users

    @err_catcher(name=__name__)
    def collectSettings(self):
        return {
            "enabled": self.cb_enabled.isChecked(),
            "notify_enabled": self.cb_notify.isChecked(),
            "smtp_host": self.e_smtpHost.text().strip(),
            "smtp_port": self.sp_smtpPort.value(),
            "use_tls": self.cb_tls.isChecked(),
            "from_addr": self.e_fromAddr.text().strip(),
        }

    @err_catcher(name=__name__)
    def onSave(self):
        if not self.settings.hasProject():
            self.core.popup(
                "No project is open, so there is nowhere to save to.", parent=self
            )
            return

        settings = self.collectSettings()
        if settings["enabled"] and settings["notify_enabled"]:
            if not settings["smtp_host"]:
                self.core.popup(
                    "Enter an SMTP server, or switch email notifications off.",
                    parent=self,
                )
                return
            if not settings["from_addr"]:
                self.core.popup(
                    "Enter a sender address, or switch email notifications off.",
                    parent=self,
                )
                return

        users = self.collectUsers()
        try:
            self.settings.saveSettings(settings)
            self.team.saveUsers(users)
        except Exception as e:
            self.core.popup(
                "Could not save the tracker settings:\n\n%s" % e, parent=self
            )
            return

        if not settings["enabled"]:
            msg = "Saved. User management is off for this project."
        elif users:
            msg = "Saved %d team member(s)." % len(users)
        else:
            # Enabled with an empty roster is legal but does nothing useful,
            # so say what is missing rather than reporting "0 team members".
            msg = (
                "Saved, but no team members have been added yet.\n\n"
                "Use 'Add User' so assignees, @mentions and notifications "
                "have someone to refer to."
            )
        self.core.popup(msg, severity="info", parent=self)
        self.accept()
