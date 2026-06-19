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
#   SCOREBOARD_INTERVAL   seconds between cycles            (default 60)
#   SCOREBOARD_BRANCH     branch to commit/push to          (default current)
#   SCOREBOARD_NO_PUSH    set to 1 to commit but never push (default unset)
#   SCOREBOARD_SYNC_ARGS  extra args for `wacli sync`       (default empty)
# Plus every variable understood by scripts/generate_stats.py.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

INTERVAL="${SCOREBOARD_INTERVAL:-60}"
STATS_FILE="docs/data/stats.json"
PY="${PYTHON:-python3}"
BRANCH="${SCOREBOARD_BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

running=1
trap 'running=0; log "shutting down after current cycle"' INT TERM

if ! command -v wacli >/dev/null 2>&1; then
  log "FATAL: wacli not found on PATH"; exit 1
fi

log "scoreboard daemon starting (interval=${INTERVAL}s, branch=${BRANCH})"

while [ "$running" -eq 1 ]; do
  cycle_start=$(date +%s)

  # 1. fetch new messages (best-effort; never fatal)
  if ! wacli sync --once ${SCOREBOARD_SYNC_ARGS:-} >/tmp/scoreboard_sync.log 2>&1; then
    log "warning: wacli sync failed (see /tmp/scoreboard_sync.log); using existing data"
  fi

  # 2. regenerate stats
  if ! "$PY" scripts/generate_stats.py >/tmp/scoreboard_gen.log 2>&1; then
    log "error: generate_stats.py failed:"; sed 's/^/    /' /tmp/scoreboard_gen.log
  else
    # 3. commit & push only when the published file changed
    if ! git diff --quiet -- "$STATS_FILE"; then
      count=$("$PY" -c "import json,sys; print(json.load(open('$STATS_FILE'))['current_count'])" 2>/dev/null || echo '?')
      git add "$STATS_FILE"
      git commit -q -m "data: update scoreboard (count ${count})" \
        -m "Automated stats refresh." \
        --trailer "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
      log "committed update (count=${count})"
      if [ "${SCOREBOARD_NO_PUSH:-0}" != "1" ]; then
        if git push -q origin "HEAD:${BRANCH}"; then
          log "pushed to origin/${BRANCH}"
        else
          log "warning: git push failed; will retry next cycle"
        fi
      fi
    else
      log "no change in ${STATS_FILE}"
    fi
  fi

  [ "$running" -eq 1 ] || break

  # sleep the remainder of the interval, but stay responsive to signals
  elapsed=$(( $(date +%s) - cycle_start ))
  remaining=$(( INTERVAL - elapsed ))
  [ "$remaining" -lt 1 ] && remaining=1
  for _ in $(seq "$remaining"); do
    [ "$running" -eq 1 ] || break
    sleep 1
  done
done

log "daemon stopped"
