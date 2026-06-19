#!/usr/bin/env bash
#
# Installiert den Scoreboard-Daemon als launchd-Agent (macOS), sodass er beim
# Login automatisch startet und laeuft. Erneutes Ausfuehren aktualisiert die
# Konfiguration (idempotent).
#
#   deploy/install-launchd.sh            # installieren / aktualisieren
#   deploy/install-launchd.sh uninstall  # wieder entfernen
#
# Voraussetzung: `wacli auth` wurde schon einmal ausgefuehrt und `git push`
# funktioniert ohne interaktive Passworteingabe (Credential-Helper / SSH).

set -euo pipefail

LABEL="com.bierchentrinkchen.scoreboard"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_SRC="$REPO_DIR/deploy/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

uninstall() {
  echo "Entferne $LABEL ..."
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST_DST"
  echo "Entfernt."
}

if [ "${1:-}" = "uninstall" ]; then
  uninstall
  exit 0
fi

WACLI_BIN="$(command -v wacli || true)"
if [ -z "$WACLI_BIN" ]; then
  echo "FEHLER: wacli nicht im PATH gefunden. Erst installieren/authentifizieren." >&2
  exit 1
fi
WACLI_BIN_DIR="$(dirname "$WACLI_BIN")"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

sed -e "s#__REPO_DIR__#$REPO_DIR#g" \
    -e "s#__WACLI_BIN_DIR__#$WACLI_BIN_DIR#g" \
    -e "s#__HOME__#$HOME#g" \
    "$PLIST_SRC" > "$PLIST_DST"

echo "Geschrieben: $PLIST_DST"

# bereits geladene Version sauber ersetzen
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST_DST"
launchctl enable "$DOMAIN/$LABEL"
launchctl kickstart -k "$DOMAIN/$LABEL"

echo
echo "Installiert und gestartet. Nuetzliche Befehle:"
echo "  launchctl print $DOMAIN/$LABEL      # Status"
echo "  tail -f \"$HOME/Library/Logs/bierchentrinkchen-scoreboard.log\""
echo "  deploy/install-launchd.sh uninstall  # stoppen & entfernen"
