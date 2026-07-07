#!/usr/bin/env python3
"""Correction bot for the counting group.

`generate_stats.py` writes ``docs/data/correction.json`` whenever the group has
pushed the count off-chain by more than the silent tolerance (MAX_GAP) but the
deviation is too fresh to be trusted as a real resync. This script reads that
signal, and -- at most once per cooldown, never twice for the same mistake --
posts a friendly correction to the group tagging whoever broke the chain.

It is deliberately conservative:

  * It keeps a small state file so the identical correction is never re-sent.
  * It honours a cooldown so the group is never spammed.
  * It refuses to send if the offender cannot be tagged (no resolvable LID),
    writing the intended message to a pending file for the operator instead.
  * Set ``SCOREBOARD_BOT_DISABLE=1`` to turn sending off entirely (the intended
    message is still written to the pending file).

Sending uses ``wacli send text --to <group> --message "... @<lid> ..."
--mention <lid>`` -- the ``@<lid>`` token in the text is what renders as a ping.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CORRECTION_FILE = Path(
    os.environ.get("SCOREBOARD_CORRECTION_FILE", REPO_ROOT / "docs" / "data" / "correction.json")
)
STATE_FILE = Path(
    os.environ.get("SCOREBOARD_BOT_STATE", REPO_ROOT / "correction_state.json")
)
PENDING_FILE = Path(
    os.environ.get("SCOREBOARD_BOT_PENDING", REPO_ROOT / "pending_corrections.txt")
)

GROUP_JID = os.environ.get("SCOREBOARD_GROUP_JID", "120363412337892492@g.us")
COOLDOWN = int(os.environ.get("SCOREBOARD_BOT_COOLDOWN", "600"))
WACLI = os.environ.get("WACLI_BIN", "wacli")


def log(*a) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), "[correction-bot]", *a, file=sys.stderr)


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def fmt(n: int) -> str:
    return f"{n:,}".replace(",", ".")


# A little pool of cheeky German openers. One is picked deterministically from
# the mistake itself, so the same blunder always yields the same text (good for
# dedupe) while different blunders get some variety instead of one canned line.
# Each line contains a single "{who}" slot for the tagged offender.
TAUNTS = [
    "{who} kann nicht zählen.",
    "{who} hat die Zahlen neu erfunden.",
    "{who} war wohl schon beim Bierchen.",
    "{who} zählt wie ein Kindergartenkind.",
    "{who} hat den Faden verloren.",
    "{who} sollte nochmal in die Grundschule.",
    "{who} bricht hier alle Rekorde – im Falschzählen.",
    "{who} hätte besser die Finger benutzt.",
    "{who} hat gerade den Zähler massakriert.",
    "{who} macht Mathe zum Verbrechen.",
]


def compose_message(sig: dict) -> str:
    """A playful German correction that tags the offender, tells the group the
    real count and where to pick it back up. The taunt is chosen deterministically
    from the mistake so it stays stable per-blunder but varies across blunders."""
    lid = sig.get("offender_lid")
    who = f"@{lid}" if lid else "Achtung"
    correct = sig["correct_count"]
    nxt = sig["expected_next"]
    wrong = sig["wrong_value"]

    seed = sig.get("dedupe_key") or f"{correct}:{wrong}"
    idx = int(hashlib.sha1(seed.encode()).hexdigest(), 16) % len(TAUNTS)
    opener = TAUNTS[idx].format(who=who)

    return (
        f"{opener} 🚨\n\n"
        f"Korrekter Count: {fmt(correct)}\n"
        f"Weiter geht's mit {fmt(nxt)}\n\n"
        f"Prost! 🍺"
    )


def main() -> int:
    sig = load_json(CORRECTION_FILE)
    if not sig:
        return 0  # nothing to correct

    dedupe = sig.get("dedupe_key") or f"{sig.get('correct_count')}:{sig.get('offender_key')}"
    state = load_json(STATE_FILE) or {}
    now = time.time()

    if state.get("last_dedupe") == dedupe:
        return 0  # already handled this exact mistake
    last_sent = float(state.get("last_sent", 0) or 0)
    if now - last_sent < COOLDOWN:
        log(f"cooldown active ({int(COOLDOWN - (now - last_sent))}s left); skipping")
        return 0

    message = compose_message(sig)
    lid = sig.get("offender_lid")

    def remember(sent: bool) -> None:
        STATE_FILE.write_text(json.dumps(
            {"last_dedupe": dedupe, "last_sent": now if sent else last_sent,
             "last_message": message}, ensure_ascii=False, indent=2))

    def stash_pending(reason: str) -> None:
        with PENDING_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  ({reason})\n{message}\n\n")

    if not lid:
        log("offender has no resolvable LID; writing to pending instead of sending")
        stash_pending("no LID to tag")
        remember(sent=False)
        return 0

    if os.environ.get("SCOREBOARD_BOT_DISABLE") == "1":
        log("SCOREBOARD_BOT_DISABLE=1; writing to pending instead of sending")
        stash_pending("bot disabled")
        remember(sent=False)
        return 0

    # This group addresses every participant by their linked-identity JID
    # (`<lid>@lid`), not by phone. Passing a bare number makes wacli treat it as a
    # phone (`<n>@s.whatsapp.net`), which is not a real member -- so the @token
    # renders as dead text instead of a ping. Qualify the LID as a JID so it
    # matches the participant and the mention actually lights up.
    mention_jid = f"{lid}@lid" if "@" not in str(lid) else str(lid)
    cmd = [
        WACLI, "send", "text",
        "--to", GROUP_JID,
        "--message", message,
        "--mention", mention_jid,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=120)
    except subprocess.CalledProcessError as exc:
        log(f"wacli send failed: {exc.stderr.strip() or exc}")
        stash_pending("send failed")
        remember(sent=False)
        return 1
    except Exception as exc:  # noqa: BLE001
        log(f"wacli send error: {exc}")
        stash_pending("send error")
        remember(sent=False)
        return 1

    log(f"sent correction (count={sig['correct_count']}, tagged {mention_jid})")
    remember(sent=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
