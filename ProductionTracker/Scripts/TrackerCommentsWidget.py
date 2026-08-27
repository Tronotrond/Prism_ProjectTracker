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
# Production Tracker - threaded comments panel
#
# Replaces the old single-string "notes" side panel with a small comment
# system: post/reply, "[] label" checkbox/to-do lines, @mention autocomplete
# (matches full name / username / email) and @remindme subscriptions. See
# TrackerComments.py for the underlying data model and merge logic.
#
####################################################


import copy
import datetime
import re

from qtpy.QtCore import Qt, Signal
from qtpy.QtGui import QTextCursor
from qtpy.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QFrame,
    QScrollArea,
    QCheckBox,
    QTextEdit,
    QPushButton,
    QToolButton,
    QTabWidget,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
)

from PrismUtils.Decorators import err_catcher_plugin as err_catcher

import TrackerComments


def _userByUsername(users, username):
    username = (username or "").lower()
    for u in users or []:
        if (u.get("username") or "").lower() == username:
            return u
    return None


def _displayName(users, username):
    u = _userByUsername(users, username)
    if u and u.get("full_name"):
        return u["full_name"]
    return username or "Unknown"


def _formatWhen(epoch):
    if not epoch:
        return ""
    try:
        return datetime.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


# A line that is exactly "[]", "[ ]" or "[x]" (optional indent) followed by
# the space the user just typed - the trigger to auto-convert it into a
# checkbox glyph live, as they type (not just after the comment is posted).
_CHECKBOX_TRIGGER_RE = re.compile(r"^(\s*)(\[( |x|X)?\]) $")

# Compose-box hints. The plain variant is used while user management is off,
# where '@' has no special meaning.
PLACEHOLDER_FULL = (
    "Write a comment... start a line with [] for a to-do, @name to mention, "
    "@remindme to subscribe"
)
PLACEHOLDER_PLAIN = "Write a comment... start a line with [] for a to-do"


class MentionEdit(QTextEdit):
    """A QTextEdit with '@mention' and '@remindme' autocomplete popups."""

    def __init__(self, parent=None):
        super(MentionEdit, self).__init__(parent)
        self._users = []
        self._statuses = []
        self._mentionsEnabled = True
        self._mode = None  # None | "mention" | "reminder" | "reminder_status"
        self._triggerPos = -1

        # IMPORTANT: this must be a real top-level window that never takes
        # keyboard focus/activation. Qt.Popup grabs the keyboard as soon as
        # it's shown, which would swallow every keystroke typed after '@'
        # (e.g. "remindme") before it ever reaches this text edit. Using a
        # non-activating Tool window instead means all keys keep going to
        # the text edit as normal, and we drive the popup's selection
        # ourselves (see keyPressEvent) via Up/Down/Enter/Escape.
        self._popup = QListWidget(None)
        self._popup.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus
        )
        self._popup.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self._popup.setFocusPolicy(Qt.NoFocus)
        self._popup.setUniformItemSizes(True)
        self._popup.hide()
        self._popup.itemClicked.connect(self._acceptItem)

    # ----------------------------------------------------------- setup -----
    def setUsers(self, users):
        self._users = users or []

    def setStatuses(self, statuses):
        self._statuses = statuses or []

    def setMentionsEnabled(self, enabled):
        """Turn '@mention' / '@remindme' autocomplete on or off.

        Off while user management is disabled: there is no roster to mention
        and nothing would be notified, so '@' stays an ordinary character.
        """
        self._mentionsEnabled = bool(enabled)
        if not self._mentionsEnabled:
            self._closePopup()
            self._mode = None

    def resetCompose(self):
        self._mode = None
        self._closePopup()

    # -------------------------------------------------------------- input --
    def keyPressEvent(self, event):
        if self._popup.isVisible():
            key = event.key()
            if key == Qt.Key_Down:
                row = min(self._popup.currentRow() + 1, self._popup.count() - 1)
                self._popup.setCurrentRow(row)
                return
            if key == Qt.Key_Up:
                row = max(self._popup.currentRow() - 1, 0)
                self._popup.setCurrentRow(row)
                return
            if key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Tab):
                item = self._popup.currentItem()
                if item is not None:
                    self._acceptItem(item)
                    return
            if key == Qt.Key_Escape:
                self._closePopup()
                self._mode = None
                return
        super(MentionEdit, self).keyPressEvent(event)
        self._checkTrigger()
        self._maybeAutoConvertCheckbox()

    def focusOutEvent(self, event):
        super(MentionEdit, self).focusOutEvent(event)
        self._closePopup()

    def _closePopup(self):
        self._popup.hide()

    def _maybeAutoConvertCheckbox(self):
        """Turn a just-typed '[]'/'[ ]'/'[x]' + space into a checkbox glyph
        live, so a to-do line visibly "becomes" a checkbox as you type
        instead of only rendering once the comment is posted. The glyph is
        converted back to the canonical '[ ] '/'[x] ' text on send (see
        TrackerComments.normalize_checkbox_glyphs) so storage/parsing/merge
        never need to know about it.
        """
        cursor = self.textCursor()
        block = cursor.block().text()
        posInBlock = cursor.positionInBlock()
        head = block[:posInBlock]
        m = _CHECKBOX_TRIGGER_RE.match(head)
        if not m:
            return
        indent, bracket, mark = m.groups()
        glyph = "\u2611" if (mark or "").lower() == "x" else "\u2610"
        startPos = cursor.block().position() + len(indent)
        endPos = startPos + len(bracket)
        c = self.textCursor()
        c.setPosition(startPos)
        c.setPosition(endPos, QTextCursor.KeepAnchor)
        c.insertText(glyph)
        c.setPosition(startPos + len(glyph) + 1)  # land right after the trailing space
        self.setTextCursor(c)

    def _checkTrigger(self):
        if not self._mentionsEnabled:
            return

        if self._mode == "reminder_status":
            return  # status list stays open until the user picks one

        cursor = self.textCursor()
        blockText = cursor.block().text()
        posInBlock = cursor.positionInBlock()
        before = blockText[:posInBlock]
        at = before.rfind("@")
        if at == -1 or (at > 0 and not before[at - 1].isspace()):
            self._closePopup()
            self._mode = None
            return

        query = before[at + 1:]
        if len(query) > 40:
            self._closePopup()
            self._mode = None
            return

        self._triggerPos = cursor.block().position() + at
        if query.lower().replace(" ", "").startswith("remind"):
            self._mode = "reminder"
            self._populateReminderList()
        else:
            self._mode = "mention"
            if not self._populateMentionList(query):
                return
        self._showPopup()

    def _populateMentionList(self, query):
        q = query.strip().lower()
        self._popup.clear()
        matches = []
        for u in self._users:
            haystack = " ".join(
                [u.get("full_name", ""), u.get("username", ""), u.get("email", "")]
            ).lower()
            if not q or q in haystack:
                matches.append(u)
        matches = matches[:8]
        if not matches:
            self._closePopup()
            return False
        for u in matches:
            label = "%s  (%s)" % (u.get("full_name") or u.get("username", ""), u.get("username", ""))
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, ("mention", u.get("username", ""), u.get("full_name") or u.get("username", "")))
            self._popup.addItem(item)
        self._popup.setCurrentRow(0)
        return True

    def _populateReminderList(self):
        self._popup.clear()
        for key, label in TrackerComments.REMINDER_TYPES:
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, ("reminder", key, label))
            self._popup.addItem(item)
        self._popup.setCurrentRow(0)

    def _populateStatusList(self):
        self._popup.clear()
        for s in self._statuses:
            item = QListWidgetItem(s)
            item.setData(Qt.UserRole, ("status", s, s))
            self._popup.addItem(item)
        self._popup.setCurrentRow(0)

    def _showPopup(self):
        rect = self.cursorRect()
        pt = self.viewport().mapToGlobal(rect.bottomLeft())
        self._popup.move(pt)
        rows = max(1, min(self._popup.count(), 8))
        self._popup.resize(260, rows * 24 + 6)
        self._popup.show()

    def _acceptItem(self, item):
        kind, key, label = item.data(Qt.UserRole)
        if kind == "mention":
            self._replaceTrigger("@%s " % label)
            self._closePopup()
            self._mode = None
        elif kind == "reminder":
            if key == "status_is":
                self._replaceTrigger("@remindme[Status changes to: ")
                self._mode = "reminder_status"
                self._populateStatusList()
                self._showPopup()
            else:
                self._replaceTrigger("@remindme[%s] " % label)
                self._closePopup()
                self._mode = None
        elif kind == "status":
            cursor = self.textCursor()
            cursor.insertText("%s] " % key)
            self.setTextCursor(cursor)
            self._closePopup()
            self._mode = None
        self.setFocus()

    def _replaceTrigger(self, text):
        end = self.textCursor().position()
        cursor = self.textCursor()
        cursor.setPosition(self._triggerPos)
        cursor.setPosition(end, QTextCursor.KeepAnchor)
        cursor.insertText(text)
        self.setTextCursor(cursor)


# Curated emoji set, grouped into categories (kept as explicit strings so
# multi-codepoint emoji - e.g. those with variation selectors - stay intact).
EMOJI_CATEGORIES = [
    ("\U0001F600 Smileys", [
        "\U0001F600", "\U0001F603", "\U0001F604", "\U0001F601", "\U0001F606",
        "\U0001F605", "\U0001F602", "\U0001F923", "\U0001F60A", "\U0001F607",
        "\U0001F642", "\U0001F643", "\U0001F609", "\U0001F60C", "\U0001F60D",
        "\U0001F970", "\U0001F618", "\U0001F617", "\U0001F61A", "\U0001F619",
        "\U0001F60B", "\U0001F61B", "\U0001F61D", "\U0001F61C", "\U0001F92A",
        "\U0001F914", "\U0001F928", "\U0001F9D0", "\U0001F913", "\U0001F60E",
        "\U0001F929", "\U0001F973", "\U0001F60F", "\U0001F612", "\U0001F61E",
        "\U0001F614", "\U0001F615", "\U0001F644", "\U0001F62C", "\U0001F925",
        "\U0001F622", "\U0001F62D", "\U0001F624", "\U0001F620", "\U0001F621",
        "\U0001F92F", "\U0001F633", "\U0001F975", "\U0001F976", "\U0001F631",
        "\U0001F628", "\U0001F630", "\U0001F625", "\U0001F613", "\U0001F917",
        "\U0001F92D", "\U0001F92B", "\U0001F636", "\U0001F610", "\U0001F611",
        "\U0001F62C", "\U0001F60C", "\U0001F634", "\U0001F971", "\U0001F922",
        "\U0001F92E", "\U0001F927", "\U0001F637", "\U0001F912", "\U0001F915",
    ]),
    ("\U0001F44D Gestures", [
        "\U0001F44D", "\U0001F44E", "\U0001F44C", "\U0001F44F", "\U0001F64C",
        "\U0001F64F", "\U0001F91D", "\U0001F4AA", "\u270C\uFE0F", "\U0001F91E",
        "\U0001F91F", "\U0001F918", "\U0001F919", "\U0001F448", "\U0001F449",
        "\U0001F446", "\U0001F447", "\u261D\uFE0F", "\u270B", "\U0001F44B",
        "\U0001F590\uFE0F", "\U0001F596", "\U0001F44A", "\u270A", "\U0001F64C",
        "\U0001F450", "\U0001F932", "\U0001F644", "\U0001F937", "\U0001F926",
    ]),
    ("\u2764\uFE0F Symbols", [
        "\u2764\uFE0F", "\U0001F9E1", "\U0001F49B", "\U0001F49A", "\U0001F499",
        "\U0001F49C", "\U0001F5A4", "\U0001F90D", "\U0001F90E", "\U0001F494",
        "\U0001F4AF", "\u2728", "\u2B50", "\U0001F31F", "\U0001F4AB",
        "\u26A1", "\U0001F525", "\U0001F389", "\U0001F38A", "\u2705",
        "\u274C", "\u2757", "\u2753", "\u26A0\uFE0F", "\U0001F4A4",
        "\U0001F44C", "\U0001F197", "\U0001F195", "\U0001F51D", "\U0001F6AB",
    ]),
    ("\U0001F436 Animals", [
        "\U0001F436", "\U0001F431", "\U0001F42D", "\U0001F439", "\U0001F430",
        "\U0001F98A", "\U0001F43B", "\U0001F43C", "\U0001F428", "\U0001F42F",
        "\U0001F981", "\U0001F42E", "\U0001F437", "\U0001F438", "\U0001F435",
        "\U0001F414", "\U0001F427", "\U0001F426", "\U0001F984", "\U0001F41D",
        "\U0001F98B", "\U0001F40C", "\U0001F422", "\U0001F419", "\U0001F988",
        "\U0001F433", "\U0001F42C", "\U0001F420", "\U0001F41F", "\U0001F995",
    ]),
    ("\U0001F354 Food", [
        "\U0001F34F", "\U0001F34E", "\U0001F350", "\U0001F34A", "\U0001F34B",
        "\U0001F34C", "\U0001F349", "\U0001F347", "\U0001F353", "\U0001F352",
        "\U0001F351", "\U0001F96D", "\U0001F34D", "\U0001F965", "\U0001F345",
        "\U0001F951", "\U0001F354", "\U0001F35F", "\U0001F355", "\U0001F32E",
        "\U0001F37F", "\U0001F369", "\U0001F36A", "\U0001F382", "\U0001F370",
        "\U0001F36B", "\U0001F36C", "\U0001F36D", "\u2615", "\U0001F37A",
        "\U0001F37B", "\U0001F942", "\U0001F377",
    ]),
    ("\U0001F680 Objects", [
        "\U0001F4BB", "\U0001F5A5\uFE0F", "\U0001F4F1", "\u2328\uFE0F", "\U0001F5B1\uFE0F",
        "\U0001F4BE", "\U0001F4F7", "\U0001F3A5", "\U0001F3AC", "\U0001F3A8",
        "\U0001F3AE", "\U0001F579\uFE0F", "\U0001F4DA", "\U0001F4DD", "\u270F\uFE0F",
        "\U0001F4CC", "\U0001F4CE", "\U0001F512", "\U0001F511", "\U0001F4A1",
        "\U0001F514", "\u23F0", "\U0001F3AF", "\U0001F3C6", "\U0001F381",
        "\U0001F4E6", "\U0001F680", "\u2699\uFE0F", "\U0001F527", "\U0001F4C5",
    ]),
]


class EmojiPicker(QWidget):
    """Small click-driven emoji palette shown near the compose box.

    Uses Qt.Popup so it auto-closes when the user clicks elsewhere. It never
    needs keyboard focus (selection is by mouse click), so the keyboard-grab
    caveat that applies to the @mention autocomplete popup doesn't matter
    here.
    """

    emojiSelected = Signal(str)

    def __init__(self, parent=None):
        super(EmojiPicker, self).__init__(parent)
        self.setWindowFlags(Qt.Popup)
        self.setObjectName("emojiPicker")
        self.setStyleSheet(
            "#emojiPicker{background-color:#2b2b2b; border:1px solid #3f3f46;"
            " border-radius:6px;}"
        )

        outer = QVBoxLayout()
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(0)
        self.setLayout(outer)

        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        outer.addWidget(tabs)

        btnStyle = (
            "QToolButton{border:none; padding:2px; font-size:18px;"
            " background:transparent;}"
            "QToolButton:hover{background-color:#3f3f46; border-radius:4px;}"
        )
        for title, emojis in EMOJI_CATEGORIES:
            page = QScrollArea()
            page.setWidgetResizable(True)
            page.setFrameShape(QFrame.NoFrame)
            host = QWidget()
            grid = QGridLayout()
            grid.setContentsMargins(2, 2, 2, 2)
            grid.setSpacing(2)
            host.setLayout(grid)
            cols = 8
            for i, emoji in enumerate(emojis):
                b = QToolButton()
                b.setText(emoji)
                b.setAutoRaise(True)
                b.setStyleSheet(btnStyle)
                b.setToolTip(emoji)
                b.clicked.connect(lambda _checked=False, e=emoji: self._onPick(e))
                grid.addWidget(b, i // cols, i % cols)
            page.setWidget(host)
            tabs.addTab(page, title)

        self.setFixedSize(320, 240)

    def _onPick(self, emoji):
        # Keep the palette open so several emoji can be added in a row; it
        # closes on click-outside (Qt.Popup) or Escape.
        self.emojiSelected.emit(emoji)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        super(EmojiPicker, self).keyPressEvent(event)

    def popupAt(self, globalBottomLeft):
        """Show the palette anchored above the given global point (so it opens
        upward from the emoji button, over the compose box)."""
        x = globalBottomLeft.x()
        y = globalBottomLeft.y() - self.height()
        self.move(x, y)
        self.show()


class CommentCard(QFrame):
    """One rendered comment or reply: avatar/name/time header + body lines."""

    replyRequested = Signal(str)
    checkboxToggled = Signal(str, int)
    editSaveRequested = Signal(str, str)  # commentId, newText
    deleteRequested = Signal(str)

    def __init__(self, comment, users, team, currentUsername=None, statuses=None,
                 isReply=False, parent=None):
        super(CommentCard, self).__init__(parent)
        self.commentId = comment.get("id")
        self.comment = comment
        self.users = users
        self._statuses = statuses or []
        self._editWidget = None
        self.setObjectName("commentCard")
        self.setStyleSheet(
            "#commentCard{background-color:%s; border-radius:6px;}"
            % ("#2b2b2b" if not isReply else "#262626")
        )

        outer = QVBoxLayout()
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(4)
        self.setLayout(outer)

        isDeleted = bool(comment.get("deleted"))
        isOwner = bool(currentUsername) and comment.get("author") == currentUsername and not isDeleted

        header = QHBoxLayout()
        header.setSpacing(6)
        author = comment.get("author", "")
        displayName = _displayName(users, author)
        u = _userByUsername(users, author)
        color = u.get("color") if u else team.colorForName(author)
        avatarLbl = QLabel()
        avatarLbl.setPixmap(team.makeAvatar(displayName, color, username=author, size=22))
        header.addWidget(avatarLbl)

        nameLbl = QLabel(displayName)
        nf = nameLbl.font()
        nf.setBold(True)
        nameLbl.setFont(nf)
        header.addWidget(nameLbl)

        whenLbl = QLabel(_formatWhen(comment.get("created")))
        whenLbl.setStyleSheet("color:#9ca3af;")
        header.addWidget(whenLbl)

        if comment.get("edited") and not isDeleted:
            editedLbl = QLabel("(edited)")
            editedLbl.setStyleSheet("color:#6b7280; font-size:10px;")
            header.addWidget(editedLbl)

        header.addStretch()

        iconBtnStyle = "QToolButton{border:none; padding:2px; font-size:12px;}"

        if not isReply and not isDeleted:
            replyBtn = QToolButton()
            replyBtn.setText("\u21A9")  # reply arrow
            replyBtn.setToolTip("Reply")
            replyBtn.setAutoRaise(True)
            replyBtn.setStyleSheet(iconBtnStyle)
            replyBtn.clicked.connect(lambda: self.replyRequested.emit(self.commentId))
            header.addWidget(replyBtn)

        if isOwner:
            editBtn = QToolButton()
            editBtn.setText("\u270F")  # pencil
            editBtn.setToolTip("Edit")
            editBtn.setAutoRaise(True)
            editBtn.setStyleSheet(iconBtnStyle)
            editBtn.clicked.connect(self._enterEditMode)
            header.addWidget(editBtn)

            deleteBtn = QToolButton()
            deleteBtn.setText("\U0001F5D1")  # wastebasket
            deleteBtn.setToolTip("Delete")
            deleteBtn.setAutoRaise(True)
            deleteBtn.setStyleSheet(iconBtnStyle)
            deleteBtn.clicked.connect(self._confirmDelete)
            header.addWidget(deleteBtn)

        outer.addLayout(header)

        self.bodyLo = QVBoxLayout()
        self.bodyLo.setContentsMargins(0, 0, 0, 0)
        self.bodyLo.setSpacing(4)
        outer.addLayout(self.bodyLo)

        if isDeleted:
            lbl = QLabel("Comment deleted")
            lbl.setStyleSheet("color:#6b7280; font-style: italic;")
            self.bodyLo.addWidget(lbl)
        else:
            self._buildViewBody()

    # ------------------------------------------------------------- helpers --
    def _clearBody(self):
        while self.bodyLo.count():
            item = self.bodyLo.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
            elif item.layout() is not None:
                _clearLayout(item.layout())

    def _buildViewBody(self):
        self._clearBody()
        comment = self.comment
        users = self.users
        mentions = comment.get("mentions") or []
        for line in TrackerComments.render_lines(comment.get("text", "")):
            if line["type"] == "checkbox":
                row = QHBoxLayout()
                row.setContentsMargins(0, 0, 0, 0)
                cb = QCheckBox()
                cb.setChecked(line["checked"])
                cb.toggled.connect(
                    lambda _checked, li=line["line"]: self.checkboxToggled.emit(self.commentId, li)
                )
                row.addWidget(cb)
                lbl = QLabel(TrackerComments.highlight_html(line["label"], mentions, users))
                lbl.setTextFormat(Qt.RichText)
                lbl.setWordWrap(True)
                lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
                if line["checked"]:
                    lbl.setStyleSheet("color:#6b7280; text-decoration: line-through;")
                row.addWidget(lbl, 1)
                self.bodyLo.addLayout(row)
            else:
                lbl = QLabel(TrackerComments.highlight_html(line["text"], mentions, users))
                lbl.setTextFormat(Qt.RichText)
                lbl.setWordWrap(True)
                lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
                self.bodyLo.addWidget(lbl)

        reminders = comment.get("reminders") or []
        if reminders:
            labels = []
            for r in reminders:
                lbl = TrackerComments.REMINDER_LABELS.get(r.get("type"), r.get("type"))
                if r.get("type") == "status_is" and r.get("value"):
                    lbl = "Status changes to: %s" % r["value"]
                who = _displayName(users, r.get("subscriber"))
                labels.append("%s (notify %s)" % (lbl, who))
            tag = QLabel("\U0001F514 " + "; ".join(labels))
            tag.setStyleSheet("color:#f59e0b; font-size:11px;")
            tag.setWordWrap(True)
            self.bodyLo.addWidget(tag)

    def _enterEditMode(self):
        self._clearBody()
        self._editWidget = MentionEdit()
        self._editWidget.setUsers(self.users)
        self._editWidget.setStatuses(self._statuses)
        self._editWidget.setPlainText(self.comment.get("text", ""))
        self._editWidget.setFixedHeight(70)
        self.bodyLo.addWidget(self._editWidget)

        btnRow = QHBoxLayout()
        btnRow.addStretch()
        cancelBtn = QPushButton("Cancel")
        cancelBtn.clicked.connect(self._cancelEdit)
        btnRow.addWidget(cancelBtn)
        saveBtn = QPushButton("Save")
        saveBtn.clicked.connect(self._saveEdit)
        btnRow.addWidget(saveBtn)
        self.bodyLo.addLayout(btnRow)

        self._editWidget.setFocus()
        cursor = self._editWidget.textCursor()
        cursor.movePosition(QTextCursor.End)
        self._editWidget.setTextCursor(cursor)

    def _cancelEdit(self):
        self._editWidget = None
        self._buildViewBody()

    def _saveEdit(self):
        if self._editWidget is None:
            return
        text = self._editWidget.toPlainText().strip()
        if not text:
            return
        self._editWidget = None
        self.editSaveRequested.emit(self.commentId, text)

    def _confirmDelete(self):
        res = QMessageBox.question(
            self,
            "Delete comment",
            "Delete this comment? This cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if res == QMessageBox.Yes:
            self.deleteRequested.emit(self.commentId)


def _clearLayout(layout):
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.deleteLater()
        elif item.layout() is not None:
            _clearLayout(item.layout())


class CommentsPanel(QWidget):
    """Side panel: scrollable comment thread + a compose box with @mentions."""

    changed = Signal()

    def __init__(self, core, team, parent=None):
        super(CommentsPanel, self).__init__(parent)
        self.core = core
        self.team = team
        self.users = []
        self.statuses = []
        self.mentionsEnabled = True
        self.currentUsername = ""
        self.currentDisplayName = ""
        self._data = TrackerComments.empty_data()
        self._replyTargetId = None
        self._enabled = False
        self.setupUi()

    # ------------------------------------------------------------------ UI --
    @err_catcher(name=__name__)
    def setupUi(self):
        lo = QVBoxLayout()
        lo.setContentsMargins(8, 0, 0, 0)
        lo.setSpacing(6)
        self.setLayout(lo)

        title = QLabel("Comments")
        tf = title.font()
        tf.setBold(True)
        title.setFont(tf)
        lo.addWidget(title)

        self.l_target = QLabel("Select a shot or asset")
        self.l_target.setStyleSheet("color:#9ca3af;")
        self.l_target.setWordWrap(True)
        lo.addWidget(self.l_target)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.cardsHost = QWidget()
        self.cardsLo = QVBoxLayout()
        self.cardsLo.setContentsMargins(0, 0, 0, 0)
        self.cardsLo.setSpacing(6)
        self.cardsLo.addStretch(1)
        self.cardsHost.setLayout(self.cardsLo)
        self.scroll.setWidget(self.cardsHost)
        lo.addWidget(self.scroll, 1)

        self.l_replyBanner = QLabel("")
        self.l_replyBanner.setStyleSheet(
            "background-color:#1f2937; color:#93c5fd; padding:3px 6px; border-radius:4px;"
        )
        self.l_replyBanner.hide()
        self.b_cancelReply = QToolButton()
        self.b_cancelReply.setText("x")
        self.b_cancelReply.clicked.connect(self.cancelReply)
        replyRow = QHBoxLayout()
        replyRow.addWidget(self.l_replyBanner, 1)
        replyRow.addWidget(self.b_cancelReply)
        self.b_cancelReply.hide()
        lo.addLayout(replyRow)

        self.compose = MentionEdit()
        self.compose.setPlaceholderText(PLACEHOLDER_FULL)
        self.compose.setFixedHeight(70)
        self.compose.setEnabled(False)
        lo.addWidget(self.compose)

        sendRow = QHBoxLayout()
        self.b_emoji = QToolButton()
        self.b_emoji.setText("\U0001F642")  # slightly smiling face
        self.b_emoji.setToolTip("Insert emoji")
        self.b_emoji.setAutoRaise(True)
        self.b_emoji.setStyleSheet("QToolButton{font-size:16px; border:none; padding:2px;}")
        self.b_emoji.setEnabled(False)
        self.b_emoji.clicked.connect(self._showEmojiPicker)
        sendRow.addWidget(self.b_emoji)
        sendRow.addStretch()
        self.b_send = QPushButton("Comment")
        self.b_send.setEnabled(False)
        self.b_send.clicked.connect(self.onSend)
        sendRow.addWidget(self.b_send)
        lo.addLayout(sendRow)

        self.setMinimumWidth(300)

        self._emojiPicker = None

    # ------------------------------------------------------------- emoji ---
    @err_catcher(name=__name__)
    def _showEmojiPicker(self):
        if not self._enabled:
            return
        if self._emojiPicker is None:
            self._emojiPicker = EmojiPicker(self)
            self._emojiPicker.emojiSelected.connect(self._insertEmoji)
        pt = self.b_emoji.mapToGlobal(self.b_emoji.rect().topLeft())
        self._emojiPicker.popupAt(pt)

    @err_catcher(name=__name__)
    def _insertEmoji(self, emoji):
        cursor = self.compose.textCursor()
        cursor.insertText(emoji)
        self.compose.setTextCursor(cursor)

    # ---------------------------------------------------------------- data --
    @err_catcher(name=__name__)
    def setUsers(self, users):
        self.users = users or []
        self.compose.setUsers(self.users)

    @err_catcher(name=__name__)
    def setStatuses(self, statuses):
        self.statuses = statuses or []
        self.compose.setStatuses(self.statuses)

    @err_catcher(name=__name__)
    def setMentionsEnabled(self, enabled):
        """Mentions/reminders follow the project's user-management switch."""
        self.mentionsEnabled = bool(enabled)
        self.compose.setMentionsEnabled(self.mentionsEnabled)
        if self.mentionsEnabled:
            self.compose.setPlaceholderText(PLACEHOLDER_FULL)
        else:
            self.compose.setPlaceholderText(PLACEHOLDER_PLAIN)

    @err_catcher(name=__name__)
    def setCurrentUser(self, username, displayName):
        self.currentUsername = username
        self.currentDisplayName = displayName

    @err_catcher(name=__name__)
    def clearEntity(self):
        self._data = TrackerComments.empty_data()
        self._replyTargetId = None
        self._enabled = False
        self.l_target.setText("Select a shot or asset")
        self.compose.clear()
        self.compose.resetCompose()
        self.compose.setEnabled(False)
        self.b_send.setEnabled(False)
        self.b_emoji.setEnabled(False)
        self.cancelReply()
        self._rebuildCards()

    @err_catcher(name=__name__)
    def setEntity(self, label, notesData):
        self._data = copy.deepcopy(notesData) if notesData else TrackerComments.empty_data()
        self._replyTargetId = None
        self._enabled = True
        self.l_target.setText(label)
        self.compose.clear()
        self.compose.resetCompose()
        self.compose.setEnabled(True)
        self.b_send.setEnabled(True)
        self.b_emoji.setEnabled(True)
        self.cancelReply()
        self._rebuildCards()

    @err_catcher(name=__name__)
    def refreshData(self, notesData):
        """Update the rendered thread (e.g. after an autosave merge) without
        disturbing anything the user is currently typing in the compose box."""
        self._data = copy.deepcopy(notesData) if notesData else TrackerComments.empty_data()
        self._rebuildCards()

    @err_catcher(name=__name__)
    def getData(self):
        return self._data

    # ------------------------------------------------------------- actions --
    @err_catcher(name=__name__)
    def _rebuildCards(self):
        while self.cardsLo.count() > 1:
            item = self.cardsLo.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        insertAt = 0
        for comment in self._data.get("comments") or []:
            card = CommentCard(
                comment, self.users, self.team,
                currentUsername=self.currentUsername, statuses=self.statuses,
                isReply=False,
            )
            card.replyRequested.connect(self.startReply)
            card.checkboxToggled.connect(self.onCheckboxToggled)
            card.editSaveRequested.connect(self.onEditSaved)
            card.deleteRequested.connect(self.onDeleteRequested)
            self.cardsLo.insertWidget(insertAt, card)
            insertAt += 1
            for reply in comment.get("replies") or []:
                rw = QWidget()
                rlo = QHBoxLayout()
                rlo.setContentsMargins(20, 0, 0, 0)
                rw.setLayout(rlo)
                rcard = CommentCard(
                    reply, self.users, self.team,
                    currentUsername=self.currentUsername, statuses=self.statuses,
                    isReply=True,
                )
                rcard.checkboxToggled.connect(self.onCheckboxToggled)
                rcard.editSaveRequested.connect(self.onEditSaved)
                rcard.deleteRequested.connect(self.onDeleteRequested)
                rlo.addWidget(rcard)
                self.cardsLo.insertWidget(insertAt, rw)
                insertAt += 1

    @err_catcher(name=__name__)
    def startReply(self, commentId):
        target = TrackerComments.find_by_id(self._data, commentId)
        if target is None:
            return
        self._replyTargetId = commentId
        who = _displayName(self.users, target.get("author"))
        self.l_replyBanner.setText("Replying to %s" % who)
        self.l_replyBanner.show()
        self.b_cancelReply.show()
        self.compose.setFocus()

    @err_catcher(name=__name__)
    def cancelReply(self):
        self._replyTargetId = None
        self.l_replyBanner.hide()
        self.b_cancelReply.hide()

    @err_catcher(name=__name__)
    def onCheckboxToggled(self, commentId, lineIndex):
        if not self._enabled:
            return
        target = TrackerComments.find_by_id(self._data, commentId)
        if target is None:
            return
        target["text"] = TrackerComments.toggle_checkbox_line(target.get("text", ""), lineIndex)
        target["modified"] = TrackerComments.now()
        self._rebuildCards()
        self.changed.emit()

    @err_catcher(name=__name__)
    def onEditSaved(self, commentId, newText):
        if not self._enabled:
            return
        target = TrackerComments.find_by_id(self._data, commentId)
        if target is None or target.get("author") != self.currentUsername:
            return
        target["text"] = TrackerComments.normalize_checkbox_glyphs(newText)
        target["mentions"] = sorted(TrackerComments.extract_mentions(newText, self.users))
        target["reminders"] = TrackerComments.extract_reminders(newText, target.get("author"))
        target["modified"] = TrackerComments.now()
        target["edited"] = True
        self._rebuildCards()
        self.changed.emit()

    @err_catcher(name=__name__)
    def onDeleteRequested(self, commentId):
        if not self._enabled:
            return
        target = TrackerComments.find_by_id(self._data, commentId)
        if target is None or target.get("author") != self.currentUsername:
            return
        target["deleted"] = True
        target["text"] = ""
        target["mentions"] = []
        target["reminders"] = []
        target["modified"] = TrackerComments.now()
        self._rebuildCards()
        self.changed.emit()

    @err_catcher(name=__name__)
    def onSend(self):
        if not self._enabled:
            return
        text = self.compose.toPlainText().strip()
        if not text:
            return
        author = self.currentUsername or "unknown"
        mentions = sorted(TrackerComments.extract_mentions(text, self.users))
        reminders = TrackerComments.extract_reminders(text, author)

        if self._replyTargetId:
            parent = TrackerComments.find_by_id(self._data, self._replyTargetId)
            if parent is not None:
                reply = TrackerComments.new_reply(author, text, mentions, reminders)
                parent.setdefault("replies", []).append(reply)
        else:
            comment = TrackerComments.new_comment(author, text, mentions, reminders)
            self._data.setdefault("comments", []).append(comment)

        self.compose.clear()
        self.compose.resetCompose()
        self.cancelReply()
        self._rebuildCards()
        self.changed.emit()
