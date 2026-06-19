# bierchentrinkchen — Zähler-Scoreboard

hier wird bierchen getrinktchet

Eine WhatsApp-Gruppe zählt gemeinsam von **1 bis 1.000.000**. Dieses Repo baut
daraus ein Scoreboard: wer hat am meisten gezählt, längste Serie, die letzten
24 h, ein Bier-Verlaufsdiagramm und ein paar Auszeichnungen — im Retro-Pixel-Look.

Die Seite ist statisch (`docs/`, gehostet via GitHub Pages aus `main`) und liest
eine einzige, anonymisierte Statistikdatei **dynamisch** vom separaten Branch
`scoreboard-data` (via `raw.githubusercontent.com`). So bleiben die häufigen
Daten-Updates aus der `main`-Historie heraus und lösen keinen Pages-Rebuild aus.
Ein lokaler Daemon hält die Datei auf dem Daten-Branch aktuell.

## Wie es funktioniert

```
WhatsApp ──(wacli sync)──▶ ~/.wacli/*.db ──┐
                                            ├─▶ generate_stats.py ─▶ stats.json ──▶ Branch scoreboard-data
optionaler Voll-Export  _chat.txt ─────────┘                                              │
                                                                                          ▼
                                       GitHub Pages (main /docs)  ◀──(raw, fetch)──  Frontend liest stats.json
```

1. **`wacli`** synchronisiert die Gruppennachrichten in eine lokale SQLite-DB
   (`~/.wacli/wacli.db` + `session.db`). Diese Dateien verlassen den Rechner nie.
2. **`scripts/generate_stats.py`** liest die DB **nur lesend**, rekonstruiert die
   offizielle Zählfolge, rechnet Statistiken aus und schreibt `stats.json`.
3. **`scripts/run_loop.sh`** verbindet beides zu einem Daemon: synchronisieren,
   neu generieren und — nur wenn der Zählerstand gestiegen ist — die Datei per
   git-Plumbing auf den Branch `scoreboard-data` publizieren (ohne Checkout).
4. **GitHub Pages** serviert `docs/` aus `main`; das Frontend lädt `stats.json`
   zur Laufzeit vom Branch `scoreboard-data` und rendert daraus alles.

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

| Variable                  | Default           | Zweck                                              |
| ------------------------- | ----------------- | -------------------------------------------------- |
| `SCOREBOARD_INTERVAL`     | `30`              | Abstand zwischen den Zyklen (Sekunden)             |
| `SCOREBOARD_DATA_BRANCH`  | `scoreboard-data` | Branch, auf den die Statistik publiziert wird      |
| `SCOREBOARD_DATA_PATH`    | `stats.json`      | Dateipfad der Statistik auf dem Daten-Branch       |
| `SCOREBOARD_REMOTE`       | `origin`          | git-Remote für den Push                            |
| `SCOREBOARD_NO_PUSH`      | —                 | `1` = Daten-Commit bauen, aber nicht pushen        |
| `SCOREBOARD_SYNC_ARGS`    | —                 | zusätzliche Argumente für `wacli sync`             |

Der Daemon pollt in festem Abstand (`SCOREBOARD_INTERVAL`, Standard 30s).
Publiziert wird ausschließlich, wenn der Zählerstand tatsächlich gestiegen ist —
und zwar per git-Plumbing direkt auf den Daten-Branch, ohne diesen auszuchecken
und ohne den `main`-Arbeitsbaum anzufassen.

Der Daemon ist fehlertolerant: schlägt ein Sync oder Push fehl, wird im nächsten
Zyklus erneut versucht. Mit `Ctrl+C` (SIGINT/SIGTERM) beendet er sich sauber nach
dem laufenden Zyklus.

### Automatischer Start beim Login (macOS / launchd)

Damit der Daemon den Rechnerstart überlebt und automatisch läuft, gibt es einen
fertigen launchd-Agenten. Der Daemon publiziert die Statistik auf den Daten-Branch
und kann daher aus jedem Worktree laufen (egal welcher Branch ausgecheckt ist).

```bash
# einmalig installieren (füllt Pfade automatisch, startet sofort)
deploy/install-launchd.sh

# Status / Logs
launchctl print gui/$(id -u)/com.bierchentrinkchen.scoreboard
tail -f ~/Library/Logs/bierchentrinkchen-scoreboard.log

# wieder entfernen
deploy/install-launchd.sh uninstall
```

Voraussetzungen für den Dauerbetrieb ohne Nachfragen:

- `wacli auth` wurde einmal ausgeführt (Session liegt in `~/.wacli/`).
- `git push` funktioniert **ohne interaktive Passworteingabe** — also via
  SSH-Remote oder einem Credential-Helper (`git config --global credential.helper
  osxkeychain` und einmal manuell pushen, oder `gh auth login`). Sonst bleibt der
  Daemon beim Push hängen.

Der Agent setzt `SCOREBOARD_DATA_BRANCH=scoreboard-data` und
`SCOREBOARD_INTERVAL=30`; anpassen in
`deploy/com.bierchentrinkchen.scoreboard.plist` und neu installieren.

Auf Linux entspricht das einem analogen systemd-User-Service (`Restart=always`),
der `scripts/run_loop.sh` startet — die Vorlage lässt sich 1:1 übertragen.

## GitHub Pages aktivieren

Einmalig: **Repo → Settings → Pages → Build and deployment → Source: „Deploy from
a branch"**, Branch auf **`main`** und Ordner **`/docs`** setzen. Die Seite
erscheint dann unter `https://<user>.github.io/<repo>/`. Die statische Seite muss
nur bei Code-Änderungen neu deployt werden; die Zahlen aktualisiert das Frontend
zur Laufzeit direkt vom Branch `scoreboard-data` (kein Pages-Rebuild nötig).

## Wichtige Dateien

| Pfad                        | Zweck                                            |
| --------------------------- | ------------------------------------------------ |
| `scripts/generate_stats.py` | Datenpipeline: DB/Export → `stats.json`          |
| `scripts/chat_export.py`    | Parser für `_chat.txt`                           |
| `scripts/run_loop.sh`       | Sync-/Generier-/Publizier-Daemon                 |
| `deploy/install-launchd.sh` | Installiert den Daemon als launchd-Agent (macOS) |
| `docs/index.html`           | Frontend-Markup                                  |
| `docs/style.css`            | Retro-Pixel-Theme                                |
| `docs/app.js`               | Rendering; lädt `stats.json` vom Daten-Branch    |
| `stats.json` (Branch `scoreboard-data`) | Veröffentlichte, anonymisierte Statistik |

## Voraussetzungen

- [`wacli`](https://github.com/openclaw/wacli) (`brew install openclaw/tap/wacli`),
  einmal authentifiziert: `wacli auth`
- Python 3 (nur Standardbibliothek)
- `git` mit push-Recht auf den Remote
