#!/usr/bin/env python3
"""Generate the anonymized counting scoreboard from local WhatsApp data.

Two sources are fused into one chronological timeline:

  * the wacli SQLite store  -- live, keeps updating as `wacli sync` runs, knows
    each sender's phone JID and resolves @mentions via the LID map;
  * an optional `_chat.txt` export -- the most complete record (starts at
    message #1) and carries every sender's display name inline.

From the merged timeline we reconstruct the official 1->1,000,000 counting
sequence, attribute every count to a player (honouring @mentions that redirect a
point to someone else) and write a pile of fun statistics to
docs/data/stats.json for the static site.

No phone numbers or raw JIDs ever leave this machine: players are keyed by a
salted hash and labelled only with their WhatsApp display name.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import chat_export  # noqa: E402

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

# --------------------------------------------------------------------------- #
# Configuration (override via environment)
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).resolve().parent.parent
WACLI_STORE = Path(os.environ.get("WACLI_STORE_DIR", Path.home() / ".wacli"))
MESSAGES_DB = WACLI_STORE / "wacli.db"
SESSION_DB = WACLI_STORE / "session.db"

GROUP_JID = os.environ.get("SCOREBOARD_GROUP_JID", "120363412337892492@g.us")
TARGET = int(os.environ.get("SCOREBOARD_TARGET", "1000000"))
OUTPUT = Path(os.environ.get("SCOREBOARD_OUTPUT", REPO_ROOT / "docs" / "data" / "stats.json"))
TIMEZONE = os.environ.get("SCOREBOARD_TZ", "Europe/Berlin")

# Optional full chat export. When present it is treated as the authoritative
# history for everything up to its last message; wacli supplies anything newer.
EXPORT_FILE = Path(os.environ.get("SCOREBOARD_CHAT_EXPORT", REPO_ROOT / "_chat.txt"))

# Salt keeps the player ids stable across runs but unlinkable to phone numbers
# by anyone who only has the published file. Stored locally, never committed.
SALT_FILE = WACLI_STORE / "scoreboard_salt"

# Manual phone -> display name overrides for players the automatic resolver
# can't name (e.g. active only after the export, no contact card). Local only,
# never committed. JSON object: {"<phone digits>": "Display Name"}.
NAME_OVERRIDES_FILE = Path(
    os.environ.get("SCOREBOARD_NAME_OVERRIDES", WACLI_STORE / "player_names.json")
)

LID_MENTION_RE = re.compile(r"@(\d{6,})")
NUMBER_RE = re.compile(r"\d[\d.,]*")

# Sequence-reconstruction tuning.
SMALL_GAP = 3        # bridge tiny gaps (a single un-synced message) silently
MAX_GAP = 80         # larger gaps must be confirmed by a continuing chain
SOON_WINDOW = 50     # how many later messages count as "soon" for confirmation


def load_salt() -> str:
    if SALT_FILE.exists():
        return SALT_FILE.read_text().strip()
    salt = hashlib.sha256(os.urandom(32)).hexdigest()
    try:
        SALT_FILE.write_text(salt)
        SALT_FILE.chmod(0o600)
    except OSError:
        pass
    return salt


def load_name_overrides() -> dict[str, str]:
    """Manual phone -> name map for players the resolver can't name. Local only."""
    if not NAME_OVERRIDES_FILE.exists():
        return {}
    try:
        data = json.loads(NAME_OVERRIDES_FILE.read_text())
    except (OSError, ValueError):
        return {}
    out: dict[str, str] = {}
    for phone, name in data.items():
        digits = re.sub(r"\D", "", str(phone))
        name = str(name).strip()
        if digits and name:
            out[digits] = name
    return out


def tz():
    if ZoneInfo is not None:
        try:
            return ZoneInfo(TIMEZONE)
        except Exception:
            pass
    return timezone.utc


def parse_numbers(text: str) -> list[int]:
    """Extract plausible counting integers, dropping thousands separators."""
    out: list[int] = []
    for tok in NUMBER_RE.findall(text):
        cleaned = tok.replace(".", "").replace(",", "")
        if not cleaned.isdigit():
            continue
        # split a glued group like "1670 1671" is handled by regex already;
        # here we just guard against absurdly long digit runs (phone numbers).
        if len(cleaned) > 7:
            continue
        out.append(int(cleaned))
    return out


def norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", name or "").strip().lower()


# Honorifics stripped before reducing a private contact name to first name +
# last initial, so "Dr. Prof. Jakob Bauer" becomes "[og] Jakob B." not "[og] Dr. ...".
_TITLES = {"dr", "prof", "mr", "mrs", "ms", "herr", "frau", "sir"}


def og_label(name: str) -> str | None:
    """Reduce a private address-book name to a low-identifiability public label:
    first name + last-name initial, marked with an [og] prefix
    (e.g. "Felix Wild" -> "[og] Felix W.").
    """
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p]
    parts = [p for p in parts if p.lower().strip(".") not in _TITLES]
    parts = [p for p in parts if any(ch.isalnum() for ch in p)]
    if not parts:
        return None
    first = parts[0]
    if len(parts) == 1:
        return f"[og] {first}"
    return f"[og] {first} {parts[-1][0].upper()}."


def norm_text(text: str) -> str:
    """Normalise message text so the same message matches across both sources."""
    t = chat_export.MEDIA_RE.sub(" ", text or "")
    t = t.replace("\u200e", "").replace("\u202f", "")
    return re.sub(r"\s+", " ", t).strip().lower()


class NameResolver:
    """Resolve a phone to a display name, separating self-set names from the
    operator's private address-book entries.

    WhatsApp distinguishes:
      * push_name / business_name -- the name the *person themselves* set, which
        everyone in the group sees. Safe to publish.
      * first_name / full_name    -- the name *you* saved them under in your
        phone's contacts. Private to you; must never be published.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.lid_by_phone: dict[str, str] = {}
        self.phone_by_lid: dict[str, str] = {}
        self.self_set: dict[str, str] = {}     # push/business, keyed by jid id
        self.addressbook: dict[str, str] = {}  # full/first, keyed by jid id
        self._load()

    def _load(self) -> None:
        for lid, pn in self.conn.execute("SELECT lid, pn FROM whatsmeow_lid_map"):
            self.lid_by_phone[pn] = lid
            self.phone_by_lid[lid] = pn
        for jid, first, full, push, business in self.conn.execute(
            "SELECT their_jid, first_name, full_name, push_name, business_name "
            "FROM whatsmeow_contacts"
        ):
            ident = jid.split("@", 1)[0]
            pub = (push or "").strip() or (business or "").strip()
            priv = (full or "").strip() or (first or "").strip()
            if pub and ident not in self.self_set:
                self.self_set[ident] = pub
            if priv and ident not in self.addressbook:
                self.addressbook[ident] = priv

    def phone_for_lid(self, lid: str) -> str | None:
        return self.phone_by_lid.get(lid)

    def _lookup(self, table: dict[str, str], phone: str) -> str | None:
        val = table.get(phone)
        if not val:
            lid = self.lid_by_phone.get(phone)
            if lid:
                val = table.get(lid)
        return val if (val and any(ch.isalnum() for ch in val)) else None

    def self_set_name(self, phone: str) -> str | None:
        """The person's own WhatsApp display name (safe to publish)."""
        return self._lookup(self.self_set, phone)

    def in_addressbook(self, phone: str) -> bool:
        """True if you have this person saved in your phone contacts."""
        return self._lookup(self.addressbook, phone) is not None

    def addressbook_name(self, phone: str) -> str | None:
        """Your private contact label for this person (must not be published raw)."""
        return self._lookup(self.addressbook, phone)

    def addressbook_name_set(self) -> set[str]:
        """Normalised set of all your private contact names (for leak checks)."""
        return {norm_name(v) for v in self.addressbook.values() if v}


def build_message_text(media_caption: str | None, text: str | None) -> str:
    cap = (media_caption or "").strip()
    txt = (text or "").strip()
    # caption usually duplicates text for images; prefer the longer one.
    return cap if len(cap) >= len(txt) else txt


def main() -> int:
    if not MESSAGES_DB.exists():
        print(f"messages db not found: {MESSAGES_DB}", file=sys.stderr)
        return 1

    salt = load_salt()
    name_overrides = load_name_overrides()

    def pid(phone: str) -> str:
        return hashlib.sha1(f"{salt}:{phone}".encode()).hexdigest()[:12]

    mconn = sqlite3.connect(f"file:{MESSAGES_DB}?mode=ro", uri=True)
    sconn = sqlite3.connect(f"file:{SESSION_DB}?mode=ro", uri=True) if SESSION_DB.exists() else None
    resolver = NameResolver(sconn) if sconn else None

    wacli_rows = mconn.execute(
        """
        SELECT ts, sender_jid, media_type, media_caption, text
        FROM messages
        WHERE chat_jid = ?
          AND revoked = 0 AND deleted_for_me = 0
          AND sender_jid LIKE '%@s.whatsapp.net'
          AND (reaction_emoji IS NULL OR reaction_emoji = '')
        ORDER BY ts ASC, rowid ASC
        """,
        (GROUP_JID,),
    ).fetchall()
    mconn.close()

    zone = tz()

    # ----- normalise wacli messages into source-agnostic records ------------
    def wacli_message(row):
        ts, sender_jid, media_type, media_caption, text = row
        phone = sender_jid.split("@", 1)[0]
        raw = build_message_text(media_caption, text)
        mention_lids = LID_MENTION_RE.findall(raw)
        clean = LID_MENTION_RE.sub(" ", raw).strip()
        mention_phones = []
        if resolver:
            for lid in mention_lids:
                ph = resolver.phone_for_lid(lid)
                if ph:
                    mention_phones.append(ph)
        return {
            "ts": float(ts),
            "phone": phone,                 # always known for wacli
            "sender_name": None,
            "mention_phones": mention_phones,
            "mention_names": [],
            "media_type": media_type or "",
            "clean": clean,
            "raw": raw,
        }

    wacli_msgs = [wacli_message(r) for r in wacli_rows]

    # ----- load the optional chat export ------------------------------------
    export_msgs = []
    if EXPORT_FILE.exists():
        try:
            export_msgs = [m for m in chat_export.parse(str(EXPORT_FILE), TIMEZONE)
                           if not m.is_system]
        except Exception as exc:  # pragma: no cover
            print(f"warning: could not parse export {EXPORT_FILE}: {exc}",
                  file=sys.stderr)

    # ----- learn name<->phone links by matching the two sources -------------
    # Match on (minute, normalised text); only trust a match when the wacli side
    # has a single candidate sender, so we never mislabel a player.
    wacli_by_key: dict[tuple, set] = defaultdict(set)
    for m in wacli_msgs:
        nt = norm_text(m["raw"])
        if nt:
            wacli_by_key[(int(m["ts"] // 60), nt)].add(m["phone"])

    name_votes: dict[str, Counter] = defaultdict(Counter)   # normname -> phone votes
    phone_name_votes: dict[str, Counter] = defaultdict(Counter)  # phone -> sender votes
    norm_to_display: dict[str, Counter] = defaultdict(Counter)

    for em in export_msgs:
        norm_to_display[norm_name(em.sender)][em.sender] += 1
        nt = norm_text(em.text)
        if not nt:
            continue
        minute = int(em.ts // 60)
        cand: set = set()
        for mm in (minute - 1, minute, minute + 1):
            cand |= wacli_by_key.get((mm, nt), set())
        if len(cand) == 1:
            phone = next(iter(cand))
            name_votes[norm_name(em.sender)][phone] += 1
            phone_name_votes[phone][em.sender] += 1

    name_to_phone = {nm: votes.most_common(1)[0][0]
                     for nm, votes in name_votes.items() if votes}
    phone_to_export_name = {ph: votes.most_common(1)[0][0]
                            for ph, votes in phone_name_votes.items() if votes}

    def key_for_name(name: str) -> str:
        nm = norm_name(name)
        phone = name_to_phone.get(nm)
        return f"p:{phone}" if phone else f"n:{nm}"

    ab_name_set = resolver.addressbook_name_set() if resolver else set()

    def display_for(key: str) -> str:
        # Naming policy, highest priority first:
        #   1. self-set WhatsApp name (push/business) -- safe to publish as-is
        #   2. manual override
        #   3. private address-book contact -> reduced "[og] First L." label
        #   4. otherwise anonymous
        if key.startswith("p:"):
            phone = key[2:]
            tail = phone[-4:] if len(phone) >= 4 else phone
            anon = f"Anonymous \u2022\u2022{tail}"

            if resolver:
                self_set = resolver.self_set_name(phone)
                if self_set:
                    return self_set
            override = name_overrides.get(phone)
            if override:
                return override
            if resolver:
                ab = resolver.addressbook_name(phone)
                if ab:
                    return og_label(ab) or anon
            # Export display name is only safe when this phone is NOT in your
            # address book (handled above); here it is a genuine group name.
            name = phone_to_export_name.get(phone)
            if name and any(c.isalnum() for c in name):
                return name
            return anon

        # Export-only player with no matched phone. If the group-presented name
        # is one of your private contacts, reduce it to an [og] label.
        nm = key[2:]
        override = name_overrides.get(nm)
        if override:
            return override
        votes = norm_to_display.get(nm)
        display = votes.most_common(1)[0][0] if votes else nm.title()
        if nm in ab_name_set:
            return og_label(display) or f"Anonymous \u2022\u2022{pid(key)[:4]}"
        return display

    # ----- build the unified timeline ---------------------------------------
    # The export is authoritative for everything up to its final message; wacli
    # supplies anything newer (and is the only source when no export is present).
    export_last_ts = max((m.ts for m in export_msgs), default=0.0)

    unified: list[dict] = []
    for em in export_msgs:
        unified.append({
            "ts": em.ts,
            "key": key_for_name(em.sender),
            "mention_keys": [key_for_name(n) for n in em.mentions]
            + [f"p:{resolver.phone_for_lid(l)}" for l in em.mention_lids
               if resolver and resolver.phone_for_lid(l)],
            "media_type": "image" if (em.has_media and not em.text) else "",
            "clean": em.text,
            "raw": em.text,
        })
    for m in wacli_msgs:
        if m["ts"] <= export_last_ts:
            continue
        unified.append({
            "ts": m["ts"],
            "key": f"p:{m['phone']}",
            "mention_keys": [f"p:{p}" for p in m["mention_phones"]],
            "media_type": m["media_type"],
            "clean": m["clean"],
            "raw": m["raw"],
        })
    unified.sort(key=lambda x: x["ts"])
    total_messages = len(unified)

    # ----- pre-parse every message ------------------------------------------
    number_index: dict[int, list[int]] = defaultdict(list)
    for i, msg in enumerate(unified):
        msg["numbers"] = parse_numbers(msg["clean"])
        for n in msg["numbers"]:
            number_index[n].append(i)

    def soon(n: int, i: int) -> bool:
        """True if number n appears in a message shortly after index i."""
        return any(i < j <= i + SOON_WINDOW for j in number_index.get(n, ()))

    def sustained(n: int, i: int) -> bool:
        """A real count: the chain demonstrably continues past n."""
        return soon(n + 1, i) and (soon(n + 2, i) or soon(n + 3, i))

    # ----- reconstruct the official sequence + attribute every count ---------
    # The history is peppered with chatter numbers (years, prices, jokes). We
    # track a monotonic high-water mark and only accept a number when it
    # plausibly continues the official chain:
    #   * exactly current+1                      -> always
    #   * within a tiny gap (un-synced message)  -> always
    #   * within a larger gap AND the chain is
    #     seen continuing past it                -> accept (bridges patchy history)
    #   * anything else (junk / corrections)     -> ignored
    current = 0
    seeded = False
    events: list[dict] = []  # one per scored number
    biggest_msg = {"count": 0, "phone": None, "text": "", "ts": 0}

    for i, msg in enumerate(unified):
        ts = msg["ts"]
        sender_key = msg["key"]
        nums = msg["numbers"]
        # Captionless image -> assume it is simply the next number in line.
        if not nums and msg["media_type"] == "image" and not msg["clean"] and seeded:
            nums = [current + 1]

        accepted: list[int] = []
        for n in nums:
            if not seeded:
                if sustained(n, i):
                    current = n
                    seeded = True
                    accepted.append(current)
                continue
            if n == current + 1:
                current += 1
                accepted.append(current)
            elif n <= current:
                continue  # repeat / correction
            elif n <= current + SMALL_GAP:
                current = n
                accepted.append(current)
            elif n <= current + MAX_GAP and sustained(n, i):
                current = n
                accepted.append(current)
            # else: junk outlier -> ignore
        if not accepted:
            continue

        mention_keys = msg["mention_keys"]
        k = len(accepted)
        if not mention_keys:
            targets = [sender_key] * k
        elif len(mention_keys) == 1:
            targets = [mention_keys[0]] * k
        elif len(mention_keys) == k:
            targets = mention_keys
        else:
            targets = [sender_key] * k

        for n, target in zip(accepted, targets):
            events.append({
                "n": n,
                "phone": target,
                "scorer": sender_key,
                "ts": ts,
            })

        if k > biggest_msg["count"]:
            biggest_msg = {"count": k, "phone": sender_key,
                           "text": msg["raw"][:120], "ts": ts}

    if not events:
        print("no counting events found", file=sys.stderr)

    now = datetime.now(timezone.utc)
    cutoff_24h = (now - timedelta(hours=24)).timestamp()

    # ----- per-player aggregation -------------------------------------------
    points = Counter()
    last24 = Counter()
    assists_given = Counter()
    assists_received = Counter()
    night = Counter()       # 0-5h local
    early = Counter()       # 5-9h local
    last_ts = {}
    first_ts = {}

    for e in events:
        ph = e["phone"]
        points[ph] += 1
        if e["ts"] >= cutoff_24h:
            last24[ph] += 1
        if e["scorer"] != ph:
            assists_given[e["scorer"]] += 1
            assists_received[ph] += 1
        local = datetime.fromtimestamp(e["ts"], zone)
        if local.hour < 5:
            night[ph] += 1
        elif local.hour < 9:
            early[ph] += 1
        last_ts[ph] = max(last_ts.get(ph, 0), e["ts"])
        first_ts[ph] = min(first_ts.get(ph, e["ts"]), e["ts"])

    # ----- streaks (consecutive numbers by the same player) ------------------
    longest_streak = Counter()
    overall_streak = {"phone": None, "len": 0, "end_n": 0}
    run_phone = None
    run_len = 0
    for e in events:
        if e["phone"] == run_phone:
            run_len += 1
        else:
            run_phone = e["phone"]
            run_len = 1
        if run_len > longest_streak[run_phone]:
            longest_streak[run_phone] = run_len
        if run_len > overall_streak["len"]:
            overall_streak = {"phone": run_phone, "len": run_len, "end_n": e["n"]}

    # current streak (tail of the sequence)
    cur_streak_phone = events[-1]["phone"] if events else None
    cur_streak_len = 0
    for e in reversed(events):
        if e["phone"] == cur_streak_phone:
            cur_streak_len += 1
        else:
            break

    # ----- time-based records ------------------------------------------------
    per_day = Counter()
    per_hour = Counter()
    for e in events:
        local = datetime.fromtimestamp(e["ts"], zone)
        per_day[local.strftime("%Y-%m-%d")] += 1
        per_hour[local.hour] += 1

    busiest_day = max(per_day.items(), key=lambda x: x[1], default=(None, 0))

    name_of = display_for

    def player_obj(ph: str) -> dict:
        return {
            "id": pid(ph),
            "name": name_of(ph),
            "points": points[ph],
            "pct": round(100 * points[ph] / current, 2) if current else 0,
            "last24h": last24[ph],
            "longest_streak": longest_streak[ph],
            "assists_given": assists_given[ph],
            "assists_received": assists_received[ph],
            "night_counts": night[ph],
            "early_counts": early[ph],
            "last_ts": last_ts.get(ph, 0),
            "first_ts": first_ts.get(ph, 0),
        }

    players = [player_obj(ph) for ph in points]
    players.sort(key=lambda p: (-p["points"], p["name"].lower()))
    for i, p in enumerate(players, 1):
        p["rank"] = i

    by_id = {p["id"]: p for p in players}

    def top(counter: Counter, n=10):
        return [
            {"id": pid(ph), "name": name_of(ph), "value": v}
            for ph, v in counter.most_common(n) if v > 0
        ]

    def record(counter: Counter):
        if not counter:
            return None
        ph, v = counter.most_common(1)[0]
        if v <= 0:
            return None
        return {"id": pid(ph), "name": name_of(ph), "value": v}

    timeline = []
    if per_day:
        d0 = datetime.strptime(min(per_day), "%Y-%m-%d")
        d1 = datetime.strptime(max(per_day), "%Y-%m-%d")
        running = 0
        d = d0
        while d <= d1:
            key = d.strftime("%Y-%m-%d")
            running += per_day.get(key, 0)
            timeline.append({"date": key, "count": per_day.get(key, 0), "total": running})
            d += timedelta(days=1)

    recent = [
        {"n": e["n"], "id": pid(e["phone"]), "name": name_of(e["phone"]), "ts": e["ts"]}
        for e in events[-20:]
    ][::-1]

    stats = {
        "generated_at": now.isoformat(),
        "timezone": TIMEZONE,
        "group_name": "1 Million leckere Bierle",
        "target": TARGET,
        "current_count": current,
        "progress_pct": round(100 * current / TARGET, 4) if TARGET else 0,
        "num_players": len(players),
        "total_messages": total_messages,
        "first_count_ts": events[0]["ts"] if events else 0,
        "last_count_ts": events[-1]["ts"] if events else 0,
        "players": players,
        "leaderboards": {
            "all_time": [p["id"] for p in players[:25]],
            "last_24h": [t["id"] for t in top(last24, 15)],
            "longest_streak": top(longest_streak, 10),
            "assists": top(assists_given, 10),
            "night_owls": top(night, 10),
            "early_birds": top(early, 10),
        },
        "records": {
            "longest_streak": (
                {**(record(longest_streak) or {}), "end_n": overall_streak["end_n"]}
                if record(longest_streak) else None
            ),
            "current_streak": (
                {"id": pid(cur_streak_phone), "name": name_of(cur_streak_phone),
                 "value": cur_streak_len}
                if cur_streak_phone else None
            ),
            "biggest_message": (
                {"id": pid(biggest_msg["phone"]), "name": name_of(biggest_msg["phone"]),
                 "value": biggest_msg["count"], "text": biggest_msg["text"]}
                if biggest_msg["phone"] else None
            ),
            "busiest_day": {"date": busiest_day[0], "value": busiest_day[1]} if busiest_day[0] else None,
            "night_owl": record(night),
            "early_bird": record(early),
            "top_assist": record(assists_given),
        },
        "timeline": timeline,
        "hour_histogram": [per_hour.get(h, 0) for h in range(24)],
        "recent": recent,
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(stats, ensure_ascii=False, indent=2))
    if sconn:
        sconn.close()

    print(
        f"wrote {OUTPUT} | count={current} players={len(players)} "
        f"events={len(events)} messages={total_messages}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
