# KEBA P40 Venus OS Treiber

Venus OS D-Bus-Treiber für die **KEBA KeContact P40** Wallbox. Kommuniziert über Modbus TCP und integriert die Wallbox als vollwertigen EV-Charger in Victron Venus OS — inklusive intelligenter PV-Überschussladung.

## Features

- **PV-Überschussladung**: Automatische Anpassung des Ladestroms an verfügbare Solarleistung
- **Drei Batterie-Strategien**: Priorisierung zwischen Hausbatterie und E-Auto konfigurierbar
- **Sanftes Ramping**: Stufenweise Strom-Anpassung, kein abruptes Schalten
- **Phasenumschaltung**: Automatischer Wechsel 1↔3 Phasen je nach Überschuss (sobald FW unterstützt)
- **Failsafe**: Konfigurierbares Verhalten bei Verbindungsverlust
- **Dry-Run Modus**: Alles lesen und berechnen, aber nichts schreiben — ideal zum Testen
- **Standalone Test-Tool**: Diagnose-Skript ohne Venus-OS-Abhängigkeiten

## Voraussetzungen

- Venus OS 3.x auf Raspberry Pi (oder anderem Venus-Gerät)
- KEBA KeContact P40 mit aktiviertem Modbus TCP (via KEBA eMobility App oder OCPP)
- Keba und Raspberry Pi im selben Netzwerk
- SSH/Root-Zugang zum Venus OS
- **git** auf dem Venus OS (für velib_python Download — das Installskript versucht es automatisch via `opkg` zu installieren)

## Installation

```bash
# Dateien auf den Venus OS Raspberry Pi kopieren (z.B. via SCP)
scp -r . root@<venus-ip>:/tmp/dbus-keba-p40/

# Auf dem Venus OS:
cd /tmp/dbus-keba-p40
chmod +x install.sh
./install.sh
```

Das Installationsskript:
1. Klont [velib_python](https://github.com/victronenergy/velib_python) nach `ext/velib_python/` (wird für die D-Bus-Kommunikation benötigt). Falls `git` nicht vorhanden ist, wird es automatisch via `opkg` installiert.
2. Kopiert Dateien nach `/data/dbus-keba-p40/` (überlebt Firmware-Updates)
3. Erstellt den daemontools-Service unter `/opt/victronenergy/service/`
4. Richtet Autostart via `/data/rc.local` ein

> **Hinweis:** Falls git auf dem Venus OS nicht verfügbar ist und `opkg` nicht funktioniert, kann velib_python auch manuell heruntergeladen werden:
> ```bash
> mkdir -p /data/dbus-keba-p40/ext
> git clone --depth 1 https://github.com/victronenergy/velib_python.git /data/dbus-keba-p40/ext/velib_python
> ```

## Konfiguration

Nach der Installation **muss** die Konfiguration angepasst werden:

```bash
cp /data/dbus-keba-p40/config.ini.example /data/dbus-keba-p40/config.ini
nano /data/dbus-keba-p40/config.ini
```

### Wichtigste Einstellungen

| Parameter | Sektion | Standard | Beschreibung |
|-----------|---------|----------|--------------|
| `host` | `[Keba]` | `192.168.1.100` | **IP-Adresse der Wallbox (muss angepasst werden!)** |
| `dry_run` | `[General]` | `true` | Testmodus — auf `false` setzen wenn alles passt |
| `strategy` | `[Battery]` | `battery_above_soc` | Batterie-Strategie (siehe unten) |
| `soc_threshold` | `[Battery]` | `80` | Ab welchem Batterie-SOC das Auto laden darf |
| `min_soc` | `[Battery]` | `20` | Unter diesem SOC wird EV-Ladung pausiert |
| `surplus_buffer_w` | `[Charging]` | `200` | Sicherheitspuffer in Watt (Hausverbrauch-Schwankungen) |
| `min_current_ma` | `[Charging]` | `6000` | Minimum 6A lt. Keba Handbuch |
| `failsafe_current_ma` | `[Charging]` | `0` | 0 = Ladung stoppen bei Verbindungsverlust |
| `failsafe_timeout_s` | `[Charging]` | `30` | Sekunden bis Failsafe greift |

### Batterie-Strategien

Der Treiber bietet drei Strategien, wie PV-Überschuss zwischen Hausbatterie und E-Auto aufgeteilt wird:

**`grid_only`** (konservativ)
- Nur was wirklich ins Netz eingespeist wird, geht ins Auto
- Batterie hat **immer** Vorrang
- Gut wenn die Batterie klein ist und für den Abend gebraucht wird

**`battery_above_soc`** (empfohlen)
- Unter dem SOC-Schwellwert (z.B. 80%) hat die Batterie Vorrang
- Darüber wird die Batterie-Ladeleistung auch für das Auto freigegeben
- Bester Kompromiss: Batterie hat Reserve, Auto bekommt trotzdem genug PV

**`ev_first`** (aggressiv)
- Auto hat **immer** Vorrang vor der Batterie
- Batterie wird erst geladen wenn das Auto voll ist
- Sinnvoll wenn das Auto viel Strom braucht

## Lademodi

| Modus | Wert | Beschreibung |
|-------|------|--------------|
| **Manual** | `0` | Fester Ladestrom über Venus OS GUI / MQTT (`SetCurrent`) |
| **Auto** | `1` | PV-Überschussladung (Standard) |
| **Scheduled** | `2` | Zeitbasiert (noch nicht implementiert) |

Im **manuellen Modus** wird der über `SetCurrent` eingestellte Wert direkt an die Keba geschrieben — keine PV-Berechnung, keine Batterie-Strategie. Der Ladestrom muss über die Venus OS Oberfläche oder per MQTT gesetzt werden.

## Service-Verwaltung

```bash
# Service starten
svc -u /opt/victronenergy/service/dbus-keba-p40

# Service stoppen
svc -d /opt/victronenergy/service/dbus-keba-p40

# Logs anzeigen (Live)
tail -f /var/log/dbus-keba-p40/current | tai64nlocal

# Neustart nach Konfigurationsänderung
svc -d /opt/victronenergy/service/dbus-keba-p40 && sleep 2 && svc -u /opt/victronenergy/service/dbus-keba-p40
```

## Test & Diagnose

Das Diagnoseskript `test_keba.py` kann auf **jedem Rechner** im selben Netzwerk ausgeführt werden — braucht nur Python 3, keine zusätzlichen Bibliotheken:

```bash
# Verbindung testen + alle Register lesen (schreibt nichts)
python3 test_keba.py 192.168.1.100

# Mit Schreibtest (Vorsicht: setzt tatsächlich Ladestrom!)
python3 test_keba.py 192.168.1.100 --write-test 12000
```

Das Skript prüft:
- Netzwerk-Konnektivität (TCP Port 502)
- Alle Modbus-Register (Ladezustand, Ströme, Spannungen, Energie, etc.)
- Firmware-Kompatibilität (bekannter Bug < 1.2.1)
- Phasenumschaltung-Support
- Failsafe-Konfiguration
- PV-Überschuss-Simulation (was würde bei verschiedenen Szenarien passieren)

## Deinstallation

```bash
chmod +x /data/dbus-keba-p40/uninstall.sh
/data/dbus-keba-p40/uninstall.sh
```

Die `config.ini` bleibt als Backup erhalten.

## Architektur

```
Venus OS (Raspberry Pi)
├── D-Bus Service: com.victronenergy.evcharger.keba_p40
│   ├── Modbus TCP Client ──── KEBA P40 Wallbox (Port 502)
│   ├── PV-Überschusslogik
│   │   ├── Grid Power    ← /Ac/Grid/Power (D-Bus)
│   │   ├── Battery SOC   ← /Dc/Battery/Soc (D-Bus)
│   │   └── Battery Power ← /Dc/Battery/Power (D-Bus)
│   └── Publiziert: Leistung, Strom, Spannung, Status, Energie
│
├── /data/dbus-keba-p40/        ← Treiber-Dateien (persistent)
│   ├── dbus-keba-p40.py        ← Haupttreiber
│   ├── config.ini              ← Konfiguration
│   ├── run                     ← daemontools Startskript
│   └── log/run                 ← Logging-Skript
│
└── /opt/victronenergy/service/ ← Service-Symlink
```

### Update-Zyklus (alle 2 Sekunden)

1. **Lesen**: Alle Keba-Register via Modbus TCP (Strom, Spannung, Leistung, Status)
2. **Publizieren**: Werte auf dem Venus OS D-Bus aktualisieren
3. **Berechnen**: PV-Überschuss ermitteln (Rolling Average über 5 Messungen)
4. **Regeln**: Ladestrom berechnen, rampen, Phasenumschaltung prüfen
5. **Schreiben**: Neuen Ladestrom an Keba senden (min. 5s zwischen Schreibvorgängen)

## Dateien

| Datei | Beschreibung |
|-------|--------------|
| `dbus-keba-p40.py` | Haupttreiber (Modbus TCP Client + D-Bus Service + PV-Logik) |
| `config.ini` | Konfigurationsdatei |
| `test_keba.py` | Standalone Diagnose- und Testskript |
| `install.sh` | Installationsskript für Venus OS |
| `uninstall.sh` | Deinstallationsskript |
| `run` | daemontools Service-Startskript |
| `log/run` | daemontools Logging-Skript |

## Technische Details

- **Protokoll**: Modbus TCP (raw sockets, keine externen Bibliotheken)
- **Abhängigkeiten**: Nur Python 3 Standardbibliothek + [velib_python](https://github.com/victronenergy/velib_python) (muss in `ext/velib_python/` geklont werden, das Installskript erledigt das automatisch)
- **Keba Handbuch**: KeContact P40 Modbus TCP Programmers Guide V1.02
- **Schreibintervall**: Minimum 5 Sekunden (lt. Keba Handbuch)
- **Leseintervall**: Minimum 0.5 Sekunden (lt. Keba Handbuch)

## Lizenz

MIT
