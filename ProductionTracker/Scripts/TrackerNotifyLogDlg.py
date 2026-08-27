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
# Production Tracker - notification log viewer
#
# Read-only view of TrackerNotify's send log for this project (mentions/
# replies/reminders): every attempt, success or failure, so a silently-
# swallowed SMTP error is actually visible somewhere instead of just
# disappearing. Reachable via Configuration -> View Notification Log.
#
# Notifications that never ran because they are switched off are NOT logged
# (that is a settings state, not a failed send), so the view says so up top
# instead of leaving an empty table looking like a bug.
#
####################################################


import datetime

from qtpy.QtCore import Qt
from qtpy.QtGui import QColor
from qtpy.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QTableWidget,
    QTableWidgetItem,
    QPushButton,
    QLabel,
    QHeaderView,
    QMessageBox,
)

from PrismUtils.Decorators import err_catcher_plugin as err_catcher


def _formatWhen(epoch):
    if not epoch:
        return ""
    try:
        return datetime.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


class NotifyLogDialog(QDialog):
    """Read-only view of the notification send log."""

    def __init__(self, core, notify, parent=None):
        super(NotifyLogDialog, self).__init__(parent)
        self.core = core
        self.notify = notify
        self.setWindowTitle("Notification Log")
        self.core.parentWindow(self, parent=parent)
        self.resize(780, 420)
        self.setupUi()
        self.refresh()

    @err_catcher(name=__name__)
    def setupUi(self):
        lo = QVBoxLayout()
        self.setLayout(lo)

        self.l_info = QLabel(
            "Every mention / reply / remind-me email this plugin has attempted to send, "
            "most recent first."
        )
        self.l_info.setStyleSheet("color:#9ca3af;")
        self.l_info.setWordWrap(True)
        lo.addWidget(self.l_info)

        self.l_state = QLabel("")
        self.l_state.setWordWrap(True)
        self.l_state.hide()
        lo.addWidget(self.l_state)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Time", "Kind", "To", "Subject", "Result"])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        lo.addWidget(self.table, 1)

        btnRow = QHBoxLayout()
        self.b_refresh = QPushButton("Refresh")
        self.b_clear = QPushButton("Clear Log")
        self.b_close = QPushButton("Close")
        btnRow.addWidget(self.b_refresh)
        btnRow.addWidget(self.b_clear)
        btnRow.addStretch()
        btnRow.addWidget(self.b_close)
        lo.addLayout(btnRow)

        self.b_refresh.clicked.connect(self.refresh)
        self.b_clear.clicked.connect(self.onClear)
        self.b_close.clicked.connect(self.accept)

    @err_catcher(name=__name__)
    def refreshState(self):
        """Say plainly why the log may be empty."""
        if self.notify.isActive():
            self.l_state.hide()
            return
        self.l_state.setText(
            "Email notifications are currently switched off for this project, "
            "so nothing is being sent. Turn them on in Production Tracker -> "
            "User Management."
        )
        self.l_state.setStyleSheet("color:#f59e0b;")
        self.l_state.show()

    @err_catcher(name=__name__)
    def refresh(self):
        self.refreshState()
        entries = list(reversed(self.notify.getLogEntries()))
        self.table.setRowCount(len(entries))
        for row, e in enumerate(entries):
            success = e.get("success")
            result = "OK" if success else "FAILED: %s" % (e.get("error") or "unknown error")
            cells = [
                _formatWhen(e.get("time")),
                e.get("kind", ""),
                ", ".join(e.get("to") or []),
                e.get("subject", ""),
                result,
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == 4:
                    item.setForeground(QColor("#4ade80" if success else "#f87171"))
                self.table.setItem(row, col, item)

    @err_catcher(name=__name__)
    def onClear(self):
        res = QMessageBox.question(
            self,
            "Clear log",
            "Clear the notification log? This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if res == QMessageBox.Yes:
            self.notify.clearLog()
            self.refresh()
