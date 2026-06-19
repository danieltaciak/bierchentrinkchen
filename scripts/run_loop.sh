#!/usr/bin/env bash
#
# Scoreboard daemon.
#
# Runs forever. Once per interval it:
#   1. pulls new WhatsApp messages into the local wacli store (`wacli sync --once`)
#   2. regenerates docs/data/stats.json from the merged history
#   3. commits & pushes, but only when the generated stats actually changed
#
# Nothing here schedules itself: just start it (optionally under nohup, tmux, a
# launchd/systemd unit, ...) and leave it running.
#
# Configuration (environment):
#   SCOREBOARD_MIN_INTERVAL  fastest poll, used while counting is active (default 20)
#   SCOREBOARD_MAX_INTERVAL  slowest poll, used when the group is quiet  (default 300)
#   SCOREBOARD_INTERVAL      legacy fixed interval; if set, pins min=max  (optional)
#   SCOREBOARD_BRANCH        branch to commit/push to          (default current)
#   SCOREBOARD_NO_PUSH       set to 1 to commit but never push (default unset)
#   SCOREBOARD_SYNC_ARGS     extra args for `wacli sync`       (default empty)
# Plus every variable understood by scripts/generate_stats.py.
#
# Polling is adaptive: after a cycle that raised the count it drops back to
# MIN_INTERVAL (stay responsive while people are counting); after each quiet
# cycle it backs off geometrically toward MAX_INTERVAL.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

STATS_FILE="docs/data/stats.json"
PY="${PYTHON:-python3}"
BRANCH="${SCOREBOARD_BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"

if [ -n "${SCOREBOARD_INTERVAL:-}" ]; then
  MIN_INTERVAL="$SCOREBOARD_INTERVAL"
  MAX_INTERVAL="$SCOREBOARD_INTERVAL"
else
  MIN_INTERVAL="${SCOREBOARD_MIN_INTERVAL:-20}"
  MAX_INTERVAL="${SCOREBOARD_MAX_INTERVAL:-300}"
fi
[ "$MIN_INTERVAL" -lt 1 ] && MIN_INTERVAL=1
[ "$MAX_INTERVAL" -lt "$MIN_INTERVAL" ] && MAX_INTERVAL="$MIN_INTERVAL"
interval="$MIN_INTERVAL"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# Read current_count from a stats.json file; prints an integer or "" on failure.
read_count() {
  "$PY" - "$1" <<'PYEOF' 2>/dev/null || true
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        v = json.load(fh).get("current_count")
    print(int(v))
except Exception:
    pass
PYEOF
}

# Read current_count from the last committed version of the stats file.
read_committed_count() {
  git show "HEAD:${STATS_FILE}" 2>/dev/null | "$PY" - <<'PYEOF' 2>/dev/null || true
import json, sys
try:
    print(int(json.load(sys.stdin).get("current_count")))
except Exception:
    pass
PYEOF
}

running=1
trap 'running=0; log "shutting down after current cycle"' INT TERM

if ! command -v wacli >/dev/null 2>&1; then
  log "FATAL: wacli not found on PATH"; exit 1
fi

log "scoreboard daemon starting (interval ${MIN_INTERVAL}-${MAX_INTERVAL}s adaptive, branch=${BRANCH})"

while [ "$running" -eq 1 ]; do
  cycle_start=$(date +%s)
  advanced=0

  # 1. fetch new messages (best-effort; never fatal)
  if ! wacli sync --once ${SCOREBOARD_SYNC_ARGS:-} >/tmp/scoreboard_sync.log 2>&1; then
    log "warning: wacli sync failed (see /tmp/scoreboard_sync.log); using existing data"
  fi

  # 2. regenerate stats
  if ! "$PY" scripts/generate_stats.py >/tmp/scoreboard_gen.log 2>&1; then
    log "error: generate_stats.py failed:"; sed 's/^/    /' /tmp/scoreboard_gen.log
  else
    # 3. commit & push only when the published count actually increased.
    #    This guards against regressions (e.g. a missing _chat.txt export that
    #    would drop the count and everyone's points) ever reaching the site,
    #    and against re-publishing an unchanged count on every cycle.
    if ! git diff --quiet -- "$STATS_FILE"; then
      new_count="$(read_count "$STATS_FILE")"
      prev_count="$(read_committed_count)"
      : "${prev_count:=-1}"

      if [ -z "$new_count" ]; then
        log "warning: could not read new count; discarding regenerated stats"
        git checkout -q -- "$STATS_FILE"
      elif [ "$new_count" -gt "$prev_count" ]; then
        advanced=1
        git add "$STATS_FILE"
        git commit -q -m "data: update scoreboard (count ${new_count})" \
          -m "Automated stats refresh." \
          --trailer "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
        log "committed update (count ${prev_count} -> ${new_count})"
        if [ "${SCOREBOARD_NO_PUSH:-0}" != "1" ]; then
          if git push -q origin "HEAD:${BRANCH}"; then
            log "pushed to origin/${BRANCH}"
          else
            log "warning: git push failed; will retry next cycle"
          fi
        fi
      else
        log "count did not increase (${prev_count} -> ${new_count}); not pushing"
        git checkout -q -- "$STATS_FILE"
      fi
    else
      log "no change in ${STATS_FILE}"
    fi
  fi

  [ "$running" -eq 1 ] || break

  # adaptive interval: snap to MIN after progress, back off geometrically
  # (x2, capped at MAX) through quiet cycles.
  if [ "$advanced" -eq 1 ]; then
    interval="$MIN_INTERVAL"
  else
    interval=$(( interval * 2 ))
    [ "$interval" -gt "$MAX_INTERVAL" ] && interval="$MAX_INTERVAL"
  fi

  # sleep the remainder of the interval, but stay responsive to signals
  elapsed=$(( $(date +%s) - cycle_start ))
  remaining=$(( interval - elapsed ))
  [ "$remaining" -lt 1 ] && remaining=1
  for _ in $(seq "$remaining"); do
    [ "$running" -eq 1 ] || break
    sleep 1
  done
done

log "daemon stopped"
