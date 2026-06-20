#!/usr/bin/env python3
"""Find inactive members of the counting WhatsApp group from the local wacli store.

This is a *local admin tool*, not part of the public scoreboard: it prints real
names and phone numbers so you can see who in the group never joins in. Its
output is for your eyes only -- do not commit or publish it.

How it works
------------
* Group membership lives in ``wacli.db`` (``group_participants``), but members
  are identified by their privacy ``@lid`` JID.
* Messages, however, are stored against the sender's *phone* JID
  (``...@s.whatsapp.net``).
* ``session.db`` (``whatsmeow_lid_map``) bridges the two: ``lid -> phone``.

So we map every participant lid to a phone, look up that phone's last message
(and message count) in the group, resolve a display name from the contact card,
and bucket everyone into: never posted / inactive since N days / active.

Usage
-----
    python3 scripts/find_inactive.py                 # 7-day threshold, text report
    python3 scripts/find_inactive.py --days 3        # stricter threshold
    python3 scripts/find_inactive.py --json          # machine-readable
    python3 scripts/find_inactive.py --include-left  # also list ex-members who posted

Environment (shared with generate_stats.py):
    WACLI_STORE_DIR        default ~/.wacli
    SCOREBOARD_GROUP_JID   default 120363412337892492@g.us
    SCOREBOARD_TZ          default Europe/Berlin
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

WACLI_STORE = Path(os.environ.get("WACLI_STORE_DIR", Path.home() / ".wacli"))
MESSAGES_DB = WACLI_STORE / "wacli.db"
SESSION_DB = WACLI_STORE / "session.db"
GROUP_JID = os.environ.get("SCOREBOARD_GROUP_JID", "120363412337892492@g.us")
TIMEZONE = os.environ.get("SCOREBOARD_TZ", "Europe/Berlin")


def tz():
    if ZoneInfo is not None:
        try:
            return ZoneInfo(TIMEZONE)
        except Exception:
            pass
    return timezone.utc


def load_lid_map(session_db: Path) -> dict[str, str]:
    """lid (digits) -> phone (digits)."""
    if not session_db.exists():
        return {}
    conn = sqlite3.connect(f"file:{session_db}?mode=ro", uri=True)
    try:
        return {str(lid): str(pn) for lid, pn in
                conn.execute("SELECT lid, pn FROM whatsmeow_lid_map")}
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


def participant_phone(user_jid: str, lid_map: dict[str, str]) -> str | None:
    """Resolve a participant JID to a phone-number string."""
    local = user_jid.split("@", 1)[0]
    if user_jid.endswith("@lid"):
        return lid_map.get(local)
    if user_jid.endswith("@s.whatsapp.net"):
        return local
    return None


def load_contacts(conn: sqlite3.Connection) -> dict[str, str]:
    """phone digits -> best available display name."""
    names: dict[str, str] = {}
    for phone, push, full, first in conn.execute(
        "SELECT phone, push_name, full_name, first_name FROM contacts"
    ):
        if not phone:
            continue
        name = (full or "").strip() or (push or "").strip() or (first or "").strip()
        if name:
            names[str(phone)] = name
    return names


def collect_activity(conn: sqlite3.Connection, group_jid: str) -> dict[str, dict]:
    """phone digits -> {count, last_ts} for everyone who posted in the group."""
    activity: dict[str, dict] = {}
    for sender, ts in conn.execute(
        "SELECT sender_jid, ts FROM messages WHERE chat_jid = ? AND sender_jid LIKE '%@s.whatsapp.net'",
        (group_jid,),
    ):
        phone = sender.split("@", 1)[0]
        rec = activity.get(phone)
        if rec is None:
            activity[phone] = {"count": 1, "last_ts": ts}
        else:
            rec["count"] += 1
            if ts > rec["last_ts"]:
                rec["last_ts"] = ts
    return activity


def label(phone: str, contacts: dict[str, str]) -> str:
    name = contacts.get(phone)
    if name:
        return name
    tail = phone[-4:] if len(phone) >= 4 else phone
    return f"unknown (\u2022\u2022{tail})"


def main() -> int:
    ap = argparse.ArgumentParser(description="Find inactive members of the counting group.")
    ap.add_argument("--days", type=int, default=7,
                    help="silence threshold in days for the 'inactive' bucket (default 7)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a text report")
    ap.add_argument("--include-left", action="store_true",
                    help="also list senders who posted but are no longer members")
    ap.add_argument("--group", default=GROUP_JID, help="group JID")
    args = ap.parse_args()

    if not MESSAGES_DB.exists():
        print(f"wacli store not found: {MESSAGES_DB}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(f"file:{MESSAGES_DB}?mode=ro", uri=True)
    lid_map = load_lid_map(SESSION_DB)
    contacts = load_contacts(conn)
    activity = collect_activity(conn, args.group)

    participants = [r[0] for r in conn.execute(
        "SELECT user_jid FROM group_participants WHERE group_jid = ?", (args.group,))]
    roles = {r[0]: r[1] for r in conn.execute(
        "SELECT user_jid, role FROM group_participants WHERE group_jid = ?", (args.group,))}
    conn.close()

    now = datetime.now(timezone.utc).timestamp()
    cutoff = now - args.days * 86400

    members: list[dict] = []
    seen_phones: set[str] = set()
    unresolved = 0
    for jid in participants:
        phone = participant_phone(jid, lid_map)
        if phone is None:
            unresolved += 1
            continue
        seen_phones.add(phone)
        act = activity.get(phone)
        last_ts = act["last_ts"] if act else None
        members.append({
            "phone": phone,
            "name": label(phone, contacts),
            "role": roles.get(jid, "member"),
            "messages": act["count"] if act else 0,
            "last_ts": last_ts,
        })

    never = [m for m in members if m["last_ts"] is None]
    inactive = [m for m in members if m["last_ts"] is not None and m["last_ts"] < cutoff]
    active = [m for m in members if m["last_ts"] is not None and m["last_ts"] >= cutoff]
    left = []
    if args.include_left:
        for phone, act in activity.items():
            if phone not in seen_phones:
                left.append({
                    "phone": phone,
                    "name": label(phone, contacts),
                    "role": "left",
                    "messages": act["count"],
                    "last_ts": act["last_ts"],
                })

    never.sort(key=lambda m: m["name"].lower())
    inactive.sort(key=lambda m: m["last_ts"])
    left.sort(key=lambda m: -m["messages"])

    if args.json:
        out = {
            "group": args.group,
            "generated_at": datetime.now(tz()).isoformat(timespec="seconds"),
            "threshold_days": args.days,
            "totals": {
                "participants": len(participants),
                "resolved": len(members),
                "unresolved_lids": unresolved,
                "never_posted": len(never),
                "inactive": len(inactive),
                "active": len(active),
            },
            "never_posted": never,
            "inactive": inactive,
            "left_but_posted": left,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    def fmt_ts(ts):
        return datetime.fromtimestamp(ts, tz()).strftime("%Y-%m-%d %H:%M")

    print(f"Group: {args.group}")
    print(f"Participants: {len(participants)}  (resolved {len(members)}, "
          f"unresolved lids {unresolved})")
    print(f"Active < {args.days}d: {len(active)}   "
          f"Inactive \u2265 {args.days}d: {len(inactive)}   "
          f"Never posted: {len(never)}")

    print(f"\n=== NEVER POSTED ({len(never)}) ===")
    for m in never:
        star = "*" if m["role"] in ("admin", "superadmin") else " "
        print(f" {star} {m['name']}  (+{m['phone']})")

    print(f"\n=== INACTIVE \u2265 {args.days}d ({len(inactive)}) ===")
    for m in inactive:
        star = "*" if m["role"] in ("admin", "superadmin") else " "
        print(f" {star} {m['name']:<28} last {fmt_ts(m['last_ts'])}  "
              f"({m['messages']} msgs)  (+{m['phone']})")

    if args.include_left:
        print(f"\n=== LEFT BUT POSTED ({len(left)}) ===")
        for m in left:
            print(f"   {m['name']:<28} last {fmt_ts(m['last_ts'])}  ({m['messages']} msgs)")

    print("\n(* = group admin. Phone numbers shown for local admin use only -- "
          "do not commit or share this output.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
