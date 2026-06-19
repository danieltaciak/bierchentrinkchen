#!/usr/bin/env bash
#
# Scoreboard daemon.
#
# Runs forever. Once per interval it:
#   1. pulls new WhatsApp messages into the local wacli store (`wacli sync --once`)
#   2. regenerates docs/data/stats.json from the merged history
#   3. publishes that file to the data branch -- but only when the count rose
#
# The page itself lives on `main` (static) and reads the stats file dynamically
# from the data branch via raw.githubusercontent. Data is therefore kept out of
# `main`'s history and never triggers a Pages rebuild. Publishing is done with
# git plumbing (hash-object / mktree / commit-tree), so the daemon never has to
# check the branch out and never touches the working tree it runs in.
#
# Nothing here schedules itself: just start it (optionally under nohup, tmux, a
# launchd/systemd unit, ...) and leave it running.
#
# Configuration (environment):
#   SCOREBOARD_INTERVAL      seconds between cycles (default 30)
#   SCOREBOARD_DATA_BRANCH   branch the stats file is published to (default scoreboard-data)
#   SCOREBOARD_DATA_PATH     path of the stats file on that branch  (default stats.json)
#   SCOREBOARD_REMOTE        git remote to push to                  (default origin)
#   SCOREBOARD_NO_PUSH       set to 1 to build commits but never push (default unset)
#   SCOREBOARD_SYNC_ARGS     extra args for `wacli sync`       (default empty)
# Plus every variable understood by scripts/generate_stats.py.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

STATS_FILE="docs/data/stats.json"
PY="${PYTHON:-python3}"
REMOTE="${SCOREBOARD_REMOTE:-origin}"
DATA_BRANCH="${SCOREBOARD_DATA_BRANCH:-scoreboard-data}"
DATA_PATH="${SCOREBOARD_DATA_PATH:-stats.json}"
TRACKING_REF="refs/remotes/${REMOTE}/${DATA_BRANCH}"

INTERVAL="${SCOREBOARD_INTERVAL:-30}"
[ "$INTERVAL" -lt 1 ] && INTERVAL=1

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

# Read current_count from the stats file currently published on the data branch.
read_published_count() {
  git show "${TRACKING_REF}:${DATA_PATH}" 2>/dev/null | "$PY" -c '
import json, sys
try:
    print(int(json.load(sys.stdin).get("current_count")))
except Exception:
    pass
' 2>/dev/null || true
}

# Publish $STATS_FILE to the data branch as $DATA_PATH using git plumbing,
# without checking the branch out. Echoes the new commit sha on success.
publish_stats() {
  local count="$1" blob tree parent commit
  blob=$(git hash-object -w "$STATS_FILE") || return 1
  tree=$(printf '100644 blob %s\t%s\n' "$blob" "$DATA_PATH" | git mktree) || return 1
  parent=$(git rev-parse -q --verify "$TRACKING_REF" || true)
  if [ -n "$parent" ]; then
    commit=$(git commit-tree "$tree" -p "$parent" \
      -m "data: update scoreboard (count ${count})" \
      -m "Automated stats refresh." \
      -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>") || return 1
  else
    commit=$(git commit-tree "$tree" \
      -m "data: update scoreboard (count ${count})" \
      -m "Automated stats refresh." \
      -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>") || return 1
  fi
  printf '%s\n' "$commit"
}

running=1
trap 'running=0; log "shutting down after current cycle"' INT TERM

if ! command -v wacli >/dev/null 2>&1; then
  log "FATAL: wacli not found on PATH"; exit 1
fi

log "scoreboard daemon starting (interval ${INTERVAL}s, publishing ${DATA_PATH} -> ${REMOTE}/${DATA_BRANCH})"

while [ "$running" -eq 1 ]; do
  cycle_start=$(date +%s)

  # 1. fetch new messages (best-effort; never fatal)
  if ! wacli sync --once ${SCOREBOARD_SYNC_ARGS:-} >/tmp/scoreboard_sync.log 2>&1; then
    log "warning: wacli sync failed (see /tmp/scoreboard_sync.log); using existing data"
  fi

  # keep the local tracking ref current so the count comparison and commit
  # parent reflect what is actually published (best-effort, never fatal).
  git fetch -q "$REMOTE" "$DATA_BRANCH" 2>/dev/null \
    && git update-ref "$TRACKING_REF" FETCH_HEAD 2>/dev/null || true

  # 2. regenerate stats
  if ! "$PY" scripts/generate_stats.py >/tmp/scoreboard_gen.log 2>&1; then
    log "error: generate_stats.py failed:"; sed 's/^/    /' /tmp/scoreboard_gen.log
  else
    # 3. publish to the data branch only when the count actually increased.
    #    This guards against regressions (e.g. a missing _chat.txt export that
    #    would drop the count and everyone's points) ever reaching the site,
    #    and against re-publishing an unchanged count on every cycle.
    new_count="$(read_count "$STATS_FILE")"
    prev_count="$(read_published_count)"
    : "${prev_count:=-1}"

    if [ -z "$new_count" ]; then
      log "warning: could not read new count; skipping publish"
    elif [ "$new_count" -gt "$prev_count" ]; then
      commit="$(publish_stats "$new_count")"
      if [ -z "$commit" ]; then
        log "error: failed to build data commit; will retry next cycle"
      elif [ "${SCOREBOARD_NO_PUSH:-0}" = "1" ]; then
        git update-ref "$TRACKING_REF" "$commit"
        log "built data commit ${commit:0:12} (count ${prev_count} -> ${new_count}); push skipped"
      elif git push -q "$REMOTE" "${commit}:refs/heads/${DATA_BRANCH}"; then
        git update-ref "$TRACKING_REF" "$commit"
        log "published count ${prev_count} -> ${new_count} to ${REMOTE}/${DATA_BRANCH}"
      else
        log "warning: git push failed; will retry next cycle"
      fi
    else
      log "count did not increase (${prev_count} -> ${new_count}); not publishing"
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
