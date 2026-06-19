"""Parser for WhatsApp `_chat.txt` exports.

The export is the most complete record of the group: it starts at message #1
and carries each sender's display name inline, which lets us (a) reconstruct the
counting sequence from the very beginning and (b) put real names on players that
the wacli store never learned.

Only structure is parsed here; all counting/scoring logic lives in
generate_stats.py so the two message sources share one code path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

# [DD.MM.YY, HH:MM:SS] Sender Name: body
HEADER_RE = re.compile(
    r"^\u200e?\[(\d{2})\.(\d{2})\.(\d{2}), (\d{2}):(\d{2}):(\d{2})\] (.*?): (.*)$"
)

# Mentions are wrapped in Unicode first-strong isolates: @\u2068~Name\u2069
MENTION_RE = re.compile(r"@\u2068~?([^\u2069]+)\u2069")
# A bare numeric LID mention occasionally slips through: @228719910215820
MENTION_LID_RE = re.compile(r"@(\d{6,})")

MEDIA_RE = re.compile(
    r"\u200e?(Bild|Sticker|GIF|Video|Audio|Dokument|Kontaktkarte|Standort|Umfrage)"
    r" weggelassen"
)

# Lines that are system notices, not real messages.
SYSTEM_MARKERS = (
    "hat die Gruppe erstellt",
    "hat dich hinzugefügt",
    "wurde hinzugefügt",
    "hat die Gruppenbeschreibung",
    "hat das Gruppenbild",
    "hat den Gruppennamen",
    "Nachrichten und Anrufe sind Ende-zu-Ende",
    "hinzugefügt.",
    "hat die Gruppe verlassen",
    "Sicherheitsnummer",
    "hat diese Nachricht gelöscht",
    "Diese Nachricht wurde gelöscht",
)


@dataclass
class ExportMessage:
    ts: float                       # unix seconds (UTC)
    sender: str                     # cleaned display name
    mentions: list[str] = field(default_factory=list)  # cleaned display names
    mention_lids: list[str] = field(default_factory=list)
    text: str = ""                  # body with media markers stripped
    has_media: bool = False
    is_system: bool = False


def clean_name(name: str) -> str:
    """Strip the WhatsApp nickname tilde and bidi/format control characters."""
    return name.lstrip("~\u200e\u202f\u2068\u2069 ").strip()


def _zone(tz_name: str):
    if ZoneInfo is not None:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            pass
    return None


def parse(path: str, tz_name: str = "Europe/Berlin") -> list[ExportMessage]:
    """Parse an export file into chronological ExportMessage records."""
    zone = _zone(tz_name)
    out: list[ExportMessage] = []
    pending: ExportMessage | None = None
    raw_body: list[str] = []

    def flush():
        nonlocal pending, raw_body
        if pending is None:
            return
        body = "\n".join(raw_body)
        pending.has_media = bool(MEDIA_RE.search(body))
        pending.mentions = [clean_name(m) for m in MENTION_RE.findall(body)]
        pending.mention_lids = MENTION_LID_RE.findall(body)
        text = MENTION_RE.sub(" ", body)
        text = MEDIA_RE.sub(" ", text)
        text = text.replace("\u200e", "").replace("\u202f", "")
        pending.text = re.sub(r"\s+", " ", text).strip()
        if any(mark in body for mark in SYSTEM_MARKERS) and not pending.text:
            pending.is_system = True
        out.append(pending)
        pending = None
        raw_body = []

    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n").rstrip("\r")
            m = HEADER_RE.match(line)
            if m:
                flush()
                dd, mo, yy, HH, MM, SS, sender, body = m.groups()
                naive = datetime(2000 + int(yy), int(mo), int(dd),
                                 int(HH), int(MM), int(SS))
                if zone is not None:
                    ts = naive.replace(tzinfo=zone).timestamp()
                else:  # assume CEST (UTC+2) if zoneinfo is unavailable
                    ts = naive.timestamp() - 2 * 3600
                pending = ExportMessage(ts=ts, sender=clean_name(sender))
                raw_body = [body]
            elif pending is not None:
                raw_body.append(line)
    flush()
    return out
