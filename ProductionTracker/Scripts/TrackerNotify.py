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
# Production Tracker - email notifications
#
# Sends "you were mentioned / someone replied / reminder" emails through an
# SMTP relay. The relay host, sender address and on/off switches live in the
# project's own tracker config (see TrackerSettings) and are edited in the
# User Management window - there is no shared file and nothing to configure
# in code.
#
# Notifications are inactive unless user management is enabled, notifications
# are enabled, and an SMTP host has been entered. Sending is best-effort and
# never raises into the caller: a dead relay must never interrupt saving.
#
####################################################


import json
import os
import smtplib
import threading
import time
from email.mime.text import MIMEText

from PrismUtils.Decorators import err_catcher_plugin as err_catcher

import TrackerSettings
import TrackerTeam


MAX_LOG_ENTRIES = 500

_logLock = threading.Lock()


class TrackerNotify(object):
    """Send best-effort notification emails for the current project."""

    def __init__(self, core):
        self.core = core
        self.settings = TrackerSettings.TrackerSettings(core)
        self.team = TrackerTeam.TrackerTeam(core)

    # ------------------------------------------------------------ settings --
    @err_catcher(name=__name__)
    def getSettings(self):
        return self.settings.getSettings()

    @err_catcher(name=__name__)
    def isActive(self):
        """Notifications only send when fully configured and switched on."""
        return self.settings.notificationsActive()

    # --------------------------------------------------------------- lookup -
    @err_catcher(name=__name__)
    def emailForUsername(self, username, users=None):
        if not username:
            return None
        users = users if users is not None else self.team.loadUsers()
        for u in users:
            if (u.get("username") or "").lower() == username.lower():
                return u.get("email") or None
        return None

    # ----------------------------------------------------------------- log --
    @err_catcher(name=__name__)
    def getLogPath(self):
        return self.settings.getLogPath()

    @err_catcher(name=__name__)
    def getLogEntries(self):
        """Return all logged send attempts (oldest first), newest last."""
        path = self.getLogPath()
        if not path:
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                entries = json.load(f)
            return entries if isinstance(entries, list) else []
        except Exception:
            return []

    @err_catcher(name=__name__)
    def clearLog(self):
        path = self.getLogPath()
        if not path:
            return
        with _logLock:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass

    def _appendLog(self, entry):
        path = self.getLogPath()
        if not path:
            return
        with _logLock:
            entries = []
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, list):
                    entries = loaded
            except Exception:
                entries = []
            entries.append(entry)
            if len(entries) > MAX_LOG_ENTRIES:
                entries = entries[-MAX_LOG_ENTRIES:]
            try:
                folder = os.path.dirname(path)
                if folder and not os.path.exists(folder):
                    os.makedirs(folder)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(entries, f, indent=2)
            except Exception:
                pass  # logging must never break the tracker either

    # ---------------------------------------------------------------- send --
    @err_catcher(name=__name__)
    def sendMail(self, toAddrs, subject, body, kind="general"):
        """Fire-and-forget send on a background thread; never raises.

        Every real attempt (success or failure) is appended to the project's
        send log (see getLogEntries/clearLog) so a dead or unreachable relay
        is at least visible somewhere instead of silently disappearing.
        Switched-off notifications return silently and are not logged - that
        is a configuration state, not a failed send.
        """
        if not self.isActive():
            return

        settings = self.getSettings()
        toAddrs = [a for a in (toAddrs or []) if a]
        if not toAddrs:
            return

        fromAddr = settings["from_addr"] or "prism-tracker@localhost"

        def _send():
            entry = {
                "time": time.time(),
                "kind": kind,
                "to": toAddrs,
                "subject": subject,
            }
            try:
                msg = MIMEText(body, "plain", "utf-8")
                msg["Subject"] = subject
                msg["From"] = fromAddr
                msg["To"] = ", ".join(toAddrs)
                with smtplib.SMTP(
                    settings["smtp_host"], int(settings["smtp_port"]), timeout=10
                ) as s:
                    if settings.get("use_tls"):
                        s.starttls()
                    s.sendmail(fromAddr, toAddrs, msg.as_string())
                entry["success"] = True
                entry["error"] = None
            except Exception as e:
                entry["success"] = False
                entry["error"] = str(e)
            self._appendLog(entry)  # notification failures must never break the tracker

        threading.Thread(target=_send, daemon=True).start()

    # ------------------------------------------------------- message builders
    @err_catcher(name=__name__)
    def notifyMention(self, toUsername, byDisplayName, entityLabel, text, users=None):
        email = self.emailForUsername(toUsername, users)
        if not email:
            return
        subject = "[Production Tracker] %s mentioned you on %s" % (byDisplayName, entityLabel)
        body = "%s mentioned you in a comment on %s:\n\n%s" % (byDisplayName, entityLabel, text)
        self.sendMail([email], subject, body, kind="mention")

    @err_catcher(name=__name__)
    def notifyReply(self, toUsername, byDisplayName, entityLabel, text, users=None):
        email = self.emailForUsername(toUsername, users)
        if not email:
            return
        subject = "[Production Tracker] %s replied to your comment on %s" % (
            byDisplayName,
            entityLabel,
        )
        body = "%s replied to your comment on %s:\n\n%s" % (byDisplayName, entityLabel, text)
        self.sendMail([email], subject, body, kind="reply")

    @err_catcher(name=__name__)
    def notifyReminder(self, toUsername, reasonLabel, entityLabel, text, users=None):
        email = self.emailForUsername(toUsername, users)
        if not email:
            return
        subject = "[Production Tracker] Reminder: %s on %s" % (reasonLabel, entityLabel)
        body = "You asked to be reminded when %s on %s." % (reasonLabel, entityLabel)
        if text:
            body += "\n\nComment:\n%s" % text
        self.sendMail([email], subject, body, kind="reminder")
