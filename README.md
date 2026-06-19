# bierchentrinkchen — Zähler-Scoreboard

hier wird bierchen getrinktchet

Eine WhatsApp-Gruppe zählt gemeinsam von **1 bis 1.000.000**. Dieses Repo baut
daraus ein Scoreboard: wer hat am meisten gezählt, längste Serie, die letzten
24 h, ein Bier-Verlaufsdiagramm und ein paar Auszeichnungen — im Retro-Pixel-Look.

Die Seite ist statisch (`docs/`) und liest eine einzige, anonymisierte
Datendatei `docs/data/stats.json`. Ein lokaler Daemon hält diese Datei aktuell.

## Wie es funktioniert

```
WhatsApp ──(wacli sync)──▶ ~/.wacli/*.db ──┐
                                            ├─▶ generate_stats.py ─▶ docs/data/stats.json ─▶ GitHub Pages
optionaler Voll-Export  _chat.txt ─────────┘
```

1. **`wacli`** synchronisiert die Gruppennachrichten in eine lokale SQLite-DB
   (`~/.wacli/wacli.db` + `session.db`). Diese Dateien verlassen den Rechner nie.
2. **`scripts/generate_stats.py`** liest die DB **nur lesend**, rekonstruiert die
   offizielle Zählfolge, rechnet Statistiken aus und schreibt `stats.json`.
3. **`scripts/run_loop.sh`** verbindet beides zu einem Daemon: synchronisieren,
   neu generieren, bei Änderung committen & pushen.
4. **GitHub Pages** serviert `docs/` — das Frontend rendert alles aus `stats.json`.

### Datenquellen-Merge

Wenn eine WhatsApp-Exportdatei `_chat.txt` im Repo-Wurzelverzeichnis liegt, wird
sie als maßgebliche Historie bis zu ihrer letzten Nachricht genutzt; `wacli`
liefert alles Neuere. Ohne Export läuft alles allein aus dem `wacli`-Store.
Die `_chat.txt` ist **gitignored** und wird nie veröffentlicht.

## Privatsphäre

Es verlassen **keine Telefonnummern oder JIDs** den Rechner. Spieler werden über
einen gesalzenen Hash (`~/.wacli/scoreboard_salt`, lokal) anonym identifiziert;
nur Anzeigename und Aggregatwerte landen in `stats.json`.

Namens-Logik, höchste Priorität zuerst:

1. **Selbst-gesetzter WhatsApp-Name** (push/business) — so wie ihn die Gruppe
   sieht, wird unverändert veröffentlicht.
2. **Manueller Override** aus `~/.wacli/player_names.json` (lokal, gitignored).
3. **Privater Adressbuch-Kontakt** → reduziert auf `[og] Vorname N.`
   (Nachname nur als Initiale, Titel wie „Dr."/„Prof." entfernt). So leakt kein
   vollständiger Name aus dem persönlichen Adressbuch des Betreibers.
4. sonst **`Anonymous ••1234`** (letzte 4 Ziffern als stabiler Suffix).

Die Lücke der „nur-Backfill"-Spieler ohne Push-Namen schließt sich mit der Zeit
von selbst: sobald `wacli` live eine neue Nachricht dieser Person sieht, lernt es
deren selbst-gesetzten Namen.

### Manuelle Namens-Overrides

```bash
# ~/.wacli/player_names.json  (lokal, nie committet)
{
  "491701234567": "mrX"
}
```

Schlüssel ist die Telefonnummer (nur Ziffern), Wert der Anzeigename. Nach dem
Bearbeiten einmal `python3 scripts/generate_stats.py` laufen lassen.

## Lokal ansehen

```bash
python3 scripts/generate_stats.py          # stats.json erzeugen
cd docs && python3 -m http.server 8765      # dann http://localhost:8765/ öffnen
```

## Dauerbetrieb (Daemon)

Synchronisiert standardmäßig **einmal pro Minute**, generiert neu und pusht nur
bei tatsächlicher Änderung:

```bash
scripts/run_loop.sh
# z. B. dauerhaft im Hintergrund:
nohup scripts/run_loop.sh > ~/scoreboard.log 2>&1 &
```

Konfiguration über Umgebungsvariablen:

| Variable                | Default        | Zweck                                  |
| ----------------------- | -------------- | -------------------------------------- |
| `SCOREBOARD_INTERVAL`   | `60`           | Sekunden zwischen den Zyklen           |
| `SCOREBOARD_BRANCH`     | aktueller      | Branch für Commit/Push                 |
| `SCOREBOARD_NO_PUSH`    | —              | `1` = committen, aber nicht pushen     |
| `SCOREBOARD_SYNC_ARGS`  | —              | zusätzliche Argumente für `wacli sync` |

Der Daemon ist fehlertolerant: schlägt ein Sync oder Push fehl, wird im nächsten
Zyklus erneut versucht. Mit `Ctrl+C` (SIGINT/SIGTERM) beendet er sich sauber nach
dem laufenden Zyklus. Das eigentliche Scheduling (launchd/systemd/tmux) bleibt dir
überlassen.

## GitHub Pages aktivieren

Einmalig: **Repo → Settings → Pages → Build and deployment → Source: „Deploy from
a branch"**, Branch auf den gepushten Branch und Ordner **`/docs`** setzen. Die
Seite erscheint dann unter `https://<user>.github.io/<repo>/`. Jeder Push des
Daemons (der `docs/data/stats.json` aktualisiert) aktualisiert die Seite.

## Wichtige Dateien

| Pfad                        | Zweck                                            |
| --------------------------- | ------------------------------------------------ |
| `scripts/generate_stats.py` | Datenpipeline: DB/Export → `stats.json`          |
| `scripts/chat_export.py`    | Parser für `_chat.txt`                           |
| `scripts/run_loop.sh`       | Sync-/Generier-/Push-Daemon                      |
| `docs/index.html`           | Frontend-Markup                                  |
| `docs/style.css`            | Retro-Pixel-Theme                                |
| `docs/app.js`               | Rendering aus `stats.json`                       |
| `docs/data/stats.json`      | Veröffentlichte, anonymisierte Statistik         |

## Voraussetzungen

- [`wacli`](https://github.com/openclaw/wacli) (`brew install openclaw/tap/wacli`),
  einmal authentifiziert: `wacli auth`
- Python 3 (nur Standardbibliothek)
- `git` mit push-Recht auf den Remote
