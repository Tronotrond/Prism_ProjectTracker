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
# Production Tracker - comment/note data model
#
# The old per-entity "notes" field was a single free-text string. This
# module upgrades it into a small threaded-comment system while keeping it
# in the SAME metadata slot (tracker_notes), so no new metadata key or
# migration step is needed anywhere else in the plugin:
#
#   {
#     "comments": [
#       {
#         "id": "<uuid hex>",
#         "author": "<username>",
#         "text": "<raw typed text; '[] label' lines become checkboxes>",
#         "created": <epoch float>,
#         "modified": <epoch float>,
#         "mentions": ["username", ...],
#         "reminders": [{"id":..., "type":..., "value":..., "subscriber":...}],
#         "replies": [ {same shape as a comment, minus "replies" (flat)} ],
#         "deleted": <bool, optional - True once the owner deletes it>,
#         "edited": <bool, optional - True once the owner edits it>,
#       },
#       ...
#     ]
#   }
#
# Comments/replies are id-stable: the owner can edit the text (re-deriving
# mentions/reminders) or soft-delete (clears text/mentions/reminders and sets
# "deleted": True - the id/author/created stay so the merge below and the
# "Comment deleted" placeholder still work). Nothing is ever removed from
# the list outright; that keeps the multi-user merge in ProductionTrackerDlg
# simple and safe: concurrent edits from two Prism sessions union together
# by id (with "modified" as the tiebreaker) instead of clobbering each other
# or resurrecting a deletion.
#
####################################################


import ast
import json
import re
import time
import uuid


# A line that starts with "[]" / "[ ]" / "[x]" (optional leading whitespace)
# is rendered as a clickable checkbox instead of plain text.
CHECKBOX_RE = re.compile(r"^(\s*)\[( |x|X)?\]\s*(.*)$")

# The compose box (TrackerCommentsWidget.MentionEdit) converts a just-typed
# "[] "/"[ ] "/"[x] " into one of these ballot-box glyphs live, purely for
# visual feedback while typing. normalize_checkbox_glyphs() converts them
# back to the canonical bracket form below before anything is stored.
_GLYPH_UNCHECKED = "\u2610"
_GLYPH_CHECKED = "\u2611"
_GLYPH_RE = re.compile(r"^(\s*)([\u2610\u2611])(.*)$")


# @remindme dropdown options: (type key, label). "status_is" is a special
# case - after picking it the caller shows a second dropdown of statuses and
# stores the chosen one in the reminder's "value".
REMINDER_TYPES = [
    ("status_change", "Status changes"),
    ("todo_done", "To-do completed"),
    ("any_reply", "Any replies"),
    ("status_is", "Status changes to..."),
    ("dept_change", "Department changes"),
]
REMINDER_LABELS = dict(REMINDER_TYPES)

# Tokens the compose box inserts inline when a user picks an @remindme /
# @mention option, e.g. "@remindme[Department changes] " or
# "@remindme[Status changes to: Completed] " / "@Jane Doe ".
_REMINDER_TOKEN_RE = re.compile(r"@remindme\[([^\]]+)\]")
_STATUS_IS_RE = re.compile(r"^Status changes to:\s*(.+)$")
_LABEL_TO_TYPE = {label: key for key, label in REMINDER_TYPES if key != "status_is"}


def new_id():
    return uuid.uuid4().hex


def extract_mentions(text, users):
    """Return the set of usernames actually still '@mentioned' in text.

    Mentions are inserted (and highlighted) as the user's DISPLAY NAME, e.g.
    "@Jane Doe" / "@Kai Andersen-Wu" - which contain spaces - so a
    plain "@\\w+" token scan can't recover them (it stops at the first space
    and never lines up with the username anyway). Instead we look for each
    known user's "@<full name>" / "@<username>" form as a substring, longest
    first, blanking each match so a shorter form (e.g. a first name that is
    also someone's username) can't re-match inside a longer one.

    Re-derived from the final text (rather than tracked as the user types)
    so that backspacing/erasing a mention before posting removes it too.
    """
    found = set()
    haystack = text or ""

    # Candidate needles: full name and username for every known user, longest
    # first so "@Jane Doe" wins over a bare "@Jane"/username substring.
    candidates = []
    for u in users or []:
        uname = (u.get("username") or "").strip()
        if not uname:
            continue
        for form in ((u.get("full_name") or "").strip(), uname):
            if form and form.lower() != "remindme":
                candidates.append((form, uname))
    candidates.sort(key=lambda c: len(c[0]), reverse=True)

    lowerhay = haystack.lower()
    for form, uname in candidates:
        needle = ("@" + form).lower()
        idx = lowerhay.find(needle)
        while idx != -1:
            end = idx + len(needle)
            # Require a boundary after the name so "@Jane" doesn't match
            # inside "@Janeway" and a username needle doesn't match a
            # longer name it is a prefix of.
            after = lowerhay[end] if end < len(lowerhay) else " "
            if not (after.isalnum() or after in "_.-"):
                found.add(uname)
                # Blank this occurrence (same length keeps indices aligned).
                lowerhay = lowerhay[:idx] + (" " * len(needle)) + lowerhay[end:]
            idx = lowerhay.find(needle, idx + 1)
    return found


def extract_reminders(text, subscriber):
    """Return the reminder dicts for the '@remindme[...]' tokens actually
    still present in text (rather than tracked separately as the user
    types), so erasing an @remindme token before posting drops it instead
    of leaving a stale reminder attached to the comment. `subscriber` is
    the username to notify - defaults to whoever wrote the comment.
    """
    out = []
    for m in _REMINDER_TOKEN_RE.finditer(text or ""):
        content = m.group(1).strip()
        sm = _STATUS_IS_RE.match(content)
        if sm:
            rtype, value = "status_is", sm.group(1).strip()
        elif content in _LABEL_TO_TYPE:
            rtype, value = _LABEL_TO_TYPE[content], None
        else:
            continue
        out.append(
            {"id": new_id(), "type": rtype, "value": value, "subscriber": subscriber}
        )
    return out


def now():
    return time.time()


def empty_data():
    return {"comments": []}


def _coerce_comment(c):
    """Fill in the standard keys on a recovered comment/reply dict."""
    if not isinstance(c, dict):
        return None
    c.setdefault("id", new_id())
    c.setdefault("author", "unknown")
    c.setdefault("text", "")
    c.setdefault("created", 0)
    c.setdefault("modified", c.get("created", 0) or 0)
    c.setdefault("mentions", [])
    c.setdefault("reminders", [])
    return c


def _recover_serialized_notes(raw):
    """Recover a comments dict that was accidentally stored as its string form.

    Some entities ended up with the whole ``{'comments': [...]}`` structure
    serialized to a plain string (Python ``repr`` with single quotes, or JSON).
    Loading that as a legacy note dumps the raw text as one giant comment, so
    here we try to parse it back into the real structure. Returns the recovered
    dict, or None if ``raw`` isn't a serialized comments structure.
    """
    s = (raw or "").strip()
    if not (s.startswith("{") and "comments" in s):
        return None
    parsed = None
    for loader in (json.loads, ast.literal_eval):
        try:
            parsed = loader(s)
            break
        except Exception:
            continue
    if not (isinstance(parsed, dict) and isinstance(parsed.get("comments"), list)):
        return None
    out = []
    for c in parsed["comments"]:
        cc = _coerce_comment(c)
        if cc is None:
            continue
        cc["replies"] = [
            r for r in (_coerce_comment(x) for x in (cc.get("replies") or [])) if r
        ]
        out.append(cc)
    return {"comments": out}


def load_notes_value(raw):
    """Coerce whatever is stored under tracker_notes into the comments dict.

    Handles: an already-structured dict, a comments dict accidentally stored as
    its string form (auto-repaired), a legacy plain-text note, or empty/None
    (brand new entity / never had notes).
    """
    if isinstance(raw, dict) and isinstance(raw.get("comments"), list):
        return raw
    if isinstance(raw, str) and raw.strip():
        recovered = _recover_serialized_notes(raw)
        if recovered is not None:
            return recovered
        # Legacy plain-text note -> becomes the first comment, authored
        # "legacy" so it's obvious it predates the comment system.
        return {
            "comments": [
                {
                    "id": new_id(),
                    "author": "legacy",
                    "text": raw,
                    "created": 0,
                    "modified": 0,
                    "mentions": [],
                    "reminders": [],
                    "replies": [],
                }
            ]
        }
    return empty_data()


def iter_entries(notes):
    """Yield every comment and reply dict in a notes value (flat)."""
    for c in (notes or {}).get("comments", []) or []:
        if not isinstance(c, dict):
            continue
        yield c
        for r in c.get("replies", []) or []:
            if isinstance(r, dict):
                yield r


def active_count(notes):
    """Number of non-deleted comments + replies in a notes value."""
    return sum(1 for e in iter_entries(notes) if not e.get("deleted"))


def latest_activity(notes, exclude_author=None):
    """Latest created/modified epoch across non-deleted comments/replies.

    Entries authored by `exclude_author` are ignored so a user's own
    comments never count as "unread" to themselves.
    """
    latest = 0.0
    for e in iter_entries(notes):
        if e.get("deleted"):
            continue
        if exclude_author and e.get("author") == exclude_author:
            continue
        t = max(e.get("modified") or 0, e.get("created") or 0)
        if t > latest:
            latest = t
    return latest


def new_comment(author, text, mentions=None, reminders=None):
    t = now()
    return {
        "id": new_id(),
        "author": author,
        "text": normalize_checkbox_glyphs(text),
        "created": t,
        "modified": t,
        "mentions": list(mentions or []),
        "reminders": list(reminders or []),
        "replies": [],
    }


def new_reply(author, text, mentions=None, reminders=None):
    return new_comment(author, text, mentions, reminders)


def parse_checkbox_lines(text):
    """Yield (lineIndex, indent, checked, label) for every checkbox line."""
    for i, line in enumerate(normalize_checkbox_glyphs(text or "").split("\n")):
        m = CHECKBOX_RE.match(line)
        if m:
            yield i, m.group(1), (m.group(2) or "").lower() == "x", m.group(3)


def render_lines(text):
    """Split comment text into renderable line descriptors.

    Returns a list of dicts, either:
      {"type": "checkbox", "line": i, "checked": bool, "label": str}
      {"type": "text", "text": str}
    """
    lines = []
    for i, raw in enumerate(normalize_checkbox_glyphs(text or "").split("\n")):
        m = CHECKBOX_RE.match(raw)
        if m:
            lines.append(
                {
                    "type": "checkbox",
                    "line": i,
                    "checked": (m.group(2) or "").lower() == "x",
                    "label": m.group(3),
                }
            )
        elif raw.strip():
            lines.append({"type": "text", "text": raw})
    return lines


def toggle_checkbox_line(text, lineIndex):
    """Flip the checked state of one checkbox line; returns the new text."""
    lines = normalize_checkbox_glyphs(text or "").split("\n")
    if lineIndex < 0 or lineIndex >= len(lines):
        return text
    m = CHECKBOX_RE.match(lines[lineIndex])
    if not m:
        return text
    checked = (m.group(2) or "").lower() == "x"
    mark = " " if checked else "x"
    lines[lineIndex] = "%s[%s] %s" % (m.group(1), mark, m.group(3))
    return "\n".join(lines)


def normalize_checkbox_glyphs(text):
    """Convert live compose-box checkbox glyphs back to canonical '[ ] '/
    '[x] ' bracket text used for storage/parsing/merge/toggling. Idempotent -
    safe to call on text that's already in bracket form (or has neither)."""
    out = []
    for line in (text or "").split("\n"):
        m = _GLYPH_RE.match(line)
        if m:
            indent, glyph, rest = m.groups()
            mark = "x" if glyph == _GLYPH_CHECKED else " "
            out.append("%s[%s]%s" % (indent, mark, rest))
        else:
            out.append(line)
    return "\n".join(out)


def highlight_html(text, mentionUsernames, users):
    """Small HTML-escaper that also bolds/colors @mentions for QLabel display."""
    import html

    nameByUser = {}
    for u in users or []:
        if u.get("username"):
            nameByUser[u["username"]] = u.get("full_name") or u["username"]

    escaped = html.escape(text or "")
    for uname in mentionUsernames or []:
        display = nameByUser.get(uname, uname)
        needle = html.escape("@" + display)
        if needle in escaped:
            escaped = escaped.replace(
                needle,
                '<span style="color:#60a5fa; font-weight:bold;">%s</span>' % needle,
            )
    return escaped


# --- merge (concurrent multi-user editing) ---------------------------------
def _merge_list(remoteList, localList):
    """Union-merge two lists of comment/reply dicts by id.

    Non-reply fields: whichever copy has the later `modified` wins. `replies`
    are always merged separately so a reply never gets dropped just because
    the parent's other fields lost the "latest wins" pick.
    """
    byId = {}
    order = []
    for c in remoteList or []:
        cid = c.get("id")
        if cid is None:
            continue
        if cid not in byId:
            order.append(cid)
        byId[cid] = dict(c)

    for c in localList or []:
        cid = c.get("id")
        if cid is None:
            continue
        if cid not in byId:
            byId[cid] = dict(c)
            order.append(cid)
            continue
        existing = byId[cid]
        newer = c if (c.get("modified", 0) or 0) >= (existing.get("modified", 0) or 0) else existing
        merged = dict(newer)
        if "replies" in existing or "replies" in c:
            merged["replies"] = _merge_list(existing.get("replies"), c.get("replies"))
        byId[cid] = merged

    return [byId[cid] for cid in order]


def merge_comments(remoteData, localData):
    remoteComments = (remoteData or {}).get("comments") or []
    localComments = (localData or {}).get("comments") or []
    return {"comments": _merge_list(remoteComments, localComments)}


# --- diffing (used to detect "what did *I* just add", for notifications) ---
def find_new_items(remoteData, localData):
    """Return (newComments, newReplies) present in localData but not remote.

    newReplies is a list of (parentComment, reply) tuples. Callers use this
    so notification emails are only ever sent for content genuinely authored
    in this save, never for content merely pulled in from another user's
    concurrent edit (which would otherwise cause duplicate emails).
    """
    remoteIds = set()
    remoteReplyIds = set()
    for c in (remoteData or {}).get("comments") or []:
        remoteIds.add(c.get("id"))
        for r in c.get("replies") or []:
            remoteReplyIds.add(r.get("id"))

    newComments = []
    newReplies = []
    for c in (localData or {}).get("comments") or []:
        if c.get("id") not in remoteIds:
            newComments.append(c)
        for r in c.get("replies") or []:
            if r.get("id") not in remoteReplyIds:
                newReplies.append((c, r))
    return newComments, newReplies


def find_newly_checked(remoteData, localData):
    """Return [(comment, lineIndex, label), ...] for checkboxes that are
    checked in localData but weren't checked (or didn't exist) in remote."""
    remoteText = {}
    for c in (remoteData or {}).get("comments") or []:
        remoteText[c.get("id")] = c.get("text", "")
        for r in c.get("replies") or []:
            remoteText[r.get("id")] = r.get("text", "")

    out = []

    def scan(comment):
        oldText = remoteText.get(comment.get("id"), "")
        oldChecked = {i: checked for i, _ind, checked, _lbl in parse_checkbox_lines(oldText)}
        for i, _ind, checked, label in parse_checkbox_lines(comment.get("text", "")):
            if checked and not oldChecked.get(i, False):
                out.append((comment, i, label))

    for c in (localData or {}).get("comments") or []:
        scan(c)
        for r in c.get("replies") or []:
            scan(r)
    return out


def find_by_id(data, targetId):
    """Locate a comment or reply dict by id anywhere in the (flat) thread."""
    for c in (data or {}).get("comments") or []:
        if c.get("id") == targetId:
            return c
        for r in c.get("replies") or []:
            if r.get("id") == targetId:
                return r
    return None
