# Plex Encoding-Automatisierung (H.264 → H.265)

Halbautomatische Umwandlung neuer Filme und Serien von H.264 nach H.265 (x265) mit HandBrakeCLI auf einem Mac. Vor jedem Encoding wird nachgefragt, welche Dateien encodiert werden sollen.

## Bestandteile

| Datei | Aufgabe |
|---|---|
| `encoding.py` | Scan der Bibliothek, Filter, Zustandsverwaltung, Obsidian-Notiz, Auswahl → Warteschlange |
| `encode.sh` | Worker: arbeitet `queue/` mit HandBrakeCLI und `preset.json` ab |
| `local.plex-encoding.plist` | launchd-Agent (`QueueDirectories`), startet `encode.sh`, sobald ein Auftrag in `queue/` liegt |
| `status.sh` | Live-Anzeige im Terminal (Fortschritt, fps, Restzeit, offene Dateien) |
| `preset.json` | Aus der HandBrake-App exportiertes Preset (x265, RF 22, MKV) |

## Ablauf

1. `python3 encoding.py scan` sucht Videos ohne x265/HEVC im Namen. Herausgefiltert werden:
   Release-Typen (LD, MD, TS, Telesync, CAM, TC, Line/Mic Dubbed), Dateien, die schon fertig in `_ENCODED` liegen,
   Dateien in der Warteschlange, abgewählte Dateien und der Altbestand.
2. Die nummerierte Liste wird bestätigt, z. B. `python3 encoding.py queue alle -3 -7 --skip-rest`.
   Das schreibt eine Batch-Datei nach `queue/`.
3. launchd erkennt den Auftrag und startet `encode.sh`. Encodiert wird nach `_ENCODED/.inprogress/`,
   erst nach Erfolg wird die Datei nach `_ENCODED/` verschoben. Ergebnisse stehen in `logs/results.tsv`.
4. Die fertigen Dateien werden manuell geprüft und aufs NAS verschoben (Umbenennung auf x265).

Schritt 1 und 2 lassen sich automatisieren, z. B. mit einem geplanten Claude-Task, der regelmässig scannt
und per Push-Nachricht nach der Auswahl fragt. Alles funktioniert aber auch von Hand über die Befehle oben.

## Weitere Befehle

```bash
python3 encoding.py status          # Fortschritt
python3 encoding.py skip 4          # Datei dauerhaft ausblenden (bis sich der Name ändert)
python3 encoding.py skipped         # ausgeblendete Dateien anzeigen
python3 encoding.py unskip 2        # wieder freigeben
zsh status.sh                       # Live-Anzeige
```

## Installation (macOS)

1. HandBrakeCLI (gleiche Version wie die App) von handbrake.fr nach `/usr/local/bin` kopieren.
2. Ordner anlegen, z. B. `~/scripts/encoding`, und alle Dateien hineinlegen.
3. **Pfade anpassen**, die Vorgaben sind Beispielwerte:
   - `encoding.py`: `ROOT_CANDIDATES`, `ENCODED_CANDIDATES`, `NOTE_DIR_CANDIDATES`
   - `encode.sh`: `FILME`, `SERIEN`, `OUT`, `PRESET_NAME` (interner Name des Presets in der JSON)
   - `local.plex-encoding.plist`: alle Pfade mit `/Users/YOUR_USER/...`
4. Dienst aktivieren:
   ```bash
   chmod +x encode.sh status.sh
   cp local.plex-encoding.plist ~/Library/LaunchAgents/
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.plex-encoding.plist
   ```
5. Optional für die Übernahme einer alten Historie: `python3 encoding.py import-legacy <cache.json> <notiz.md>`

Abschalten: `launchctl bootout gui/$(id -u)/local.plex-encoding`
