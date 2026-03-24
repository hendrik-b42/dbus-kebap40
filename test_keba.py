#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
KEBA P40 Test- und Diagnoseskript
==================================
Dieses Skript testet die Modbus TCP Verbindung zur Keba P40
und liest alle Register aus - OHNE etwas zu schreiben.

Kann auf JEDEM Rechner im selben Netzwerk ausgefuehrt werden,
auch ausserhalb von Venus OS (braucht nur Python 3, keine
zusaetzlichen Bibliotheken).

Aufruf:
  python3 test_keba.py <IP-Adresse>
  python3 test_keba.py 192.168.1.100
  python3 test_keba.py 192.168.1.100 --write-test   # Vorsicht: schreibt!
"""

import sys
import struct
import socket
import time
import argparse

# ---------------------------------------------------------------------------
# Modbus TCP Konstanten (lt. Keba Handbuch V1.02, Section 2, S.7-8)
# ---------------------------------------------------------------------------
KEBA_PORT = 502
KEBA_UNIT_ID = 255
FC_READ = 3

# ---------------------------------------------------------------------------
# Farb-Hilfsfunktionen fuer Terminal-Ausgabe
# ---------------------------------------------------------------------------
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"

def ok(text):
    return f"{GREEN}OK{RESET} {text}"

def fail(text):
    return f"{RED}FEHLER{RESET} {text}"

def warn(text):
    return f"{YELLOW}WARNUNG{RESET} {text}"

def header(text):
    return f"\n{BOLD}{CYAN}{'=' * 60}\n {text}\n{'=' * 60}{RESET}"


# ---------------------------------------------------------------------------
# Minimaler Modbus TCP Client (identisch zum Haupttreiber, nur Lesen)
# ---------------------------------------------------------------------------

class ModbusTCPReader:
    def __init__(self, host, port=KEBA_PORT, unit_id=KEBA_UNIT_ID, timeout=5.0):
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.timeout = timeout
        self._socket = None
        self._tid = 0

    def connect(self):
        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket.settimeout(self.timeout)
            self._socket.connect((self.host, self.port))
            return True
        except Exception as e:
            self._socket = None
            return False, str(e)

    def disconnect(self):
        if self._socket:
            self._socket.close()
            self._socket = None

    def read_register(self, register):
        """Liest ein UINT32 Register (2 Words) per FC3."""
        if not self._socket:
            return None

        self._tid = (self._tid + 1) % 65536
        request = struct.pack(">HHHBBHH",
            self._tid, 0, 6, self.unit_id, FC_READ, register, 2)

        try:
            self._socket.sendall(request)
            resp = self._recv(9)
            if resp is None:
                return None

            _, _, _, _, fc, byte_count = struct.unpack(">HHHBBB", resp)
            if fc != FC_READ or byte_count != 4:
                return None

            data = self._recv(4)
            if data is None:
                return None

            return struct.unpack(">I", data)[0]

        except Exception:
            return None

    def write_register(self, register, value):
        """Schreibt ein UINT16 Register per FC6. NUR fuer --write-test!"""
        if not self._socket:
            return False

        self._tid = (self._tid + 1) % 65536
        request = struct.pack(">HHHBBHH",
            self._tid, 0, 6, self.unit_id, 6, register, value & 0xFFFF)

        try:
            self._socket.sendall(request)
            resp = self._recv(12)
            if resp is None:
                return False
            _, _, _, _, fc = struct.unpack(">HHHBB", resp[:8])
            return fc == 6
        except Exception:
            return False

    def _recv(self, count):
        data = b""
        while len(data) < count:
            chunk = self._socket.recv(count - len(data))
            if not chunk:
                return None
            data += chunk
        return data


# ---------------------------------------------------------------------------
# Testfunktionen
# ---------------------------------------------------------------------------

def test_network(host, port):
    """Test 1: Netzwerkverbindung pruefen."""
    print(header("Test 1: Netzwerk-Erreichbarkeit"))

    # Ping-artige TCP-Verbindung
    print(f"  Verbinde zu {host}:{port}...")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(5)
        start = time.time()
        s.connect((host, port))
        latency = (time.time() - start) * 1000
        s.close()
        print(f"  {ok(f'TCP Port {port} erreichbar (Latenz: {latency:.0f}ms)')}")
        return True
    except socket.timeout:
        print(f"  {fail(f'Timeout - Keba auf {host}:{port} nicht erreichbar')}")
        print(f"  -> Ist die Keba im selben Netzwerk?")
        print(f"  -> Ist Modbus TCP aktiviert? (KEBA eMobility App oder OCPP)")
        return False
    except ConnectionRefusedError:
        print(f"  {fail(f'Verbindung abgelehnt auf Port {port}')}")
        print(f"  -> Modbus TCP ist vermutlich NICHT aktiviert auf der Keba!")
        print(f"  -> Aktivierung: KEBA eMobility App > Einstellungen > Modbus TCP")
        return False
    except Exception as e:
        print(f"  {fail(f'Verbindungsfehler: {e}')}")
        return False


def test_modbus_read(host):
    """Test 2: Alle lesbaren Register auslesen."""
    print(header("Test 2: Modbus TCP Register lesen"))

    reader = ModbusTCPReader(host)
    result = reader.connect()
    if result is not True and isinstance(result, tuple):
        print(f"  {fail(f'Modbus-Verbindung fehlgeschlagen: {result[1]}')}")
        return False, None

    if not reader._socket:
        print(f"  {fail('Modbus-Verbindung fehlgeschlagen')}")
        return False, None

    print(f"  {ok('Modbus TCP Verbindung hergestellt')}")
    print()

    results = {}
    success_count = 0
    fail_count = 0

    # Alle lesbaren Register lt. Handbuch Section 3
    registers = [
        # (Register, Name, Einheit, Divisor, Handbuch-Referenz)
        (1000, "Charging State",       "",      1,    "Section 3.3.1, S.12"),
        (1004, "Cable State",          "",      1,    "Section 3.3.2, S.12"),
        (1006, "Error Code",           "",      1,    "Section 3.3.3, S.13"),
        (1008, "Current L1",           "mA",   1,    "Section 3.1.1, S.9"),
        (1010, "Current L2",           "mA",   1,    "Section 3.1.2, S.9"),
        (1012, "Current L3",           "mA",   1,    "Section 3.1.3, S.9"),
        (1020, "Active Power",         "mW",   1,    "Section 3.2.1, S.10"),
        (1036, "Total Energy",         "0.1Wh", 1,   "Section 3.2.2, S.10"),
        (1040, "Voltage L1",           "V",     1,    "Section 3.2.4, S.10"),
        (1042, "Voltage L2",           "V",     1,    "Section 3.2.5, S.11"),
        (1044, "Voltage L3",           "V",     1,    "Section 3.2.6, S.11"),
        (1046, "Power Factor",         "0.1%",  1,    "Section 3.2.3, S.10"),
        (1100, "Max Current",          "mA",   1,    "Section 3.5.1, S.16"),
        (1110, "Max Supported Current","mA",   1,    "Section 3.5.2, S.16"),
        (1200, "Fast Charging State",  "",      1,    "Section 3.5.3, S.16"),
        (1014, "Serial Number",        "",      1,    "Section 3.4.1, S.14"),
        (1016, "Product Type",         "",      1,    "Section 3.4.2, S.14"),
        (1018, "Firmware Version",     "",      1,    "Section 3.4.3, S.15"),
        (1700, "HW Revision Device",   "",      1,    "Section 3.4.4, S.15"),
        (1702, "HW Revision KC-MS10",  "",      1,    "Section 3.4.5, S.15"),
        (1500, "RFID Tag",             "",      1,    "Section 3.6.1, S.17"),
        (1502, "Session Energy",       "0.1Wh", 1,   "Section 3.6.2, S.17"),
        (1550, "Phase Switch Source",  "",      1,    "Section 3.7.1, S.18*"),
        (1552, "Phase Switch State",   "",      1,    "Section 3.7.2, S.18*"),
        (1600, "Failsafe Current",     "mA",   1,    "Section 3.8.1, S.19"),
        (1602, "Failsafe Timeout",     "s",     1,    "Section 3.8.2, S.19"),
    ]

    for reg, name, unit, div, ref in registers:
        time.sleep(0.1)  # Kurze Pause zwischen Lesevorgaengen
        value = reader.read_register(reg)

        if value is not None:
            results[reg] = value
            success_count += 1
            # Formatierte Ausgabe
            display = format_value(reg, value, unit)
            print(f"  {GREEN}[{reg:5d}]{RESET} {name:25s} = {display:>15s}  ({ref})")
        else:
            fail_count += 1
            marker = "*" if "*" in ref else ""
            print(f"  {YELLOW}[{reg:5d}]{RESET} {name:25s} = {'nicht lesbar':>15s}  ({ref}){' (ggf. FW-Update noetig)' if marker else ''}")

    reader.disconnect()

    print(f"\n  Ergebnis: {success_count} gelesen, {fail_count} fehlgeschlagen")
    return True, results


def format_value(reg, value, unit):
    """Formatiert Register-Werte fuer die Anzeige."""
    if reg == 1000:
        states = {0: "Start-up", 1: "Nicht bereit", 2: "Bereit",
                  3: "Laedt", 4: "Fehler", 5: "Unterbrochen (Temp.)"}
        return f"{value} ({states.get(value, '?')})"
    elif reg == 1004:
        cables = {0: "Kein Kabel", 1: "Kabel an Station", 3: "Kabel verriegelt",
                  5: "Kabel am Fahrzeug", 7: "Kabel verriegelt+Fahrzeug"}
        return f"{value} ({cables.get(value, '?')})"
    elif reg == 1200:
        return f"{value} ({'Aktiv' if value == 1 else 'Inaktiv'})"
    elif reg == 1018:
        major = value // 10000
        medium = (value % 10000) // 100
        minor = value % 100
        return f"{major}.{medium}.{minor}"
    elif reg == 1550:
        sources = {0: "Nicht verfuegbar", 1: "OCPP", 2: "REST API", 3: "Modbus TCP"}
        return f"{value} ({sources.get(value, '?')})"
    elif reg == 1552:
        return f"{value} ({'1-phasig' if value == 1 else '3-phasig' if value == 3 else '?'})"
    elif unit == "mA" and value > 0:
        return f"{value} mA ({value/1000:.1f} A)"
    elif unit == "mW" and value > 0:
        return f"{value} mW ({value/1000:.1f} W)"
    elif unit == "0.1Wh" and value > 0:
        return f"{value} ({value * 0.1 / 1000:.2f} kWh)"
    elif unit == "0.1%":
        return f"{value} ({value/10:.1f}%)"
    elif unit:
        return f"{value} {unit}"
    else:
        return str(value)


def test_interpret(results):
    """Test 3: Ergebnisse interpretieren und Warnungen ausgeben."""
    print(header("Test 3: Analyse und Empfehlungen"))

    if not results:
        print(f"  {fail('Keine Daten zum Analysieren')}")
        return

    issues = []
    infos = []

    # Charging State
    state = results.get(1000)
    if state is not None:
        if state == 4:
            error = results.get(1006, 0)
            issues.append(f"Wallbox meldet FEHLER (Error Code: {error})")
        elif state == 5:
            issues.append("Wallbox ist im Suspended-Modus (Temperatur zu hoch)")
        elif state == 1:
            infos.append("Wallbox nicht bereit (kein Fahrzeug / Autorisierung noetig)")
        elif state == 2:
            infos.append("Wallbox bereit - wartet auf Fahrzeug-Reaktion")
        elif state == 3:
            infos.append("Wallbox laedt gerade aktiv")

    # Firmware
    fw = results.get(1018)
    if fw is not None:
        if fw < 10201:
            issues.append(
                f"Firmware < 1.2.1 erkannt! "
                f"Bekannter Bug: Register 1502/1036 melden Wh statt 0.1 Wh "
                f"(Handbuch S.24, Known Bugs)")

    # Phase switching
    ps_source = results.get(1550)
    if ps_source is not None:
        if ps_source == 0:
            infos.append("Phasenumschaltung: Nicht verfuegbar (ggf. FW-Update noetig)")
        elif ps_source != 3:
            infos.append(
                f"Phasenumschaltung: Aktuell Quelle={ps_source} "
                f"(fuer Modbus TCP muss Register 5050 auf 3 gesetzt werden)")
    else:
        infos.append("Phasenumschaltung: Register 1550 nicht lesbar (FW-Update noetig)")

    # Failsafe
    fs_timeout = results.get(1602, 0)
    fs_current = results.get(1600, 0)
    if fs_timeout == 0:
        infos.append(
            "EMS Failsafe ist DEAKTIVIERT. Der Treiber wird ihn beim Start "
            "konfigurieren (Register 5016/5018)")
    else:
        infos.append(
            f"EMS Failsafe aktiv: Timeout={fs_timeout}s, "
            f"Strom={fs_current}mA")

    # Max current
    max_hw = results.get(1110, 0)
    max_set = results.get(1100, 0)
    if max_hw > 0:
        infos.append(f"Hardware-Max: {max_hw/1000:.0f}A, Konfigurations-Max: {max_set/1000:.0f}A")

    # Fast charging
    fast = results.get(1200, 0)
    if fast == 1:
        issues.append(
            "Fast Charging ist AKTIV! In diesem Modus kann der Ladestrom "
            "NICHT per Modbus TCP gesteuert werden (Handbuch Section 3.5.3)")

    # Ausgabe
    if issues:
        print(f"  {RED}{BOLD}Probleme:{RESET}")
        for issue in issues:
            print(f"    {RED}!{RESET} {issue}")
        print()

    if infos:
        print(f"  {CYAN}Informationen:{RESET}")
        for info in infos:
            print(f"    - {info}")
        print()

    if not issues:
        print(f"  {GREEN}{BOLD}Alles sieht gut aus fuer den Treiber-Einsatz!{RESET}")


def test_write_current(host, current_ma):
    """
    Optionaler Schreibtest: Setzt den Ladestrom und liest ihn zurueck.
    NUR mit --write-test ausfuehren!
    """
    print(header(f"Schreibtest: Ladestrom auf {current_ma} mA setzen"))

    if current_ma != 0 and (current_ma < 6000 or current_ma > 32000):
        print(f"  {fail(f'Ungueltiger Wert! Erlaubt: 0 oder 6000-32000 mA (Handbuch S.20)')}")
        return False

    reader = ModbusTCPReader(host)
    result = reader.connect()
    if not reader._socket:
        print(f"  {fail('Verbindung fehlgeschlagen')}")
        return False

    # Vorher: aktuellen Status lesen
    state = reader.read_register(1000)
    power_before = reader.read_register(1020)
    print(f"  Vorher: State={state}, Power={power_before}mW")

    if state not in (2, 3):
        print(f"  {warn('Wallbox nicht im State 2 oder 3 - Schreibbefehl hat ggf. keinen Effekt')}")

    # Schreiben
    print(f"  Schreibe Register 5004 = {current_ma}...")
    success = reader.write_register(5004, current_ma)

    if success:
        print(f"  {ok(f'Register 5004 = {current_ma} geschrieben')}")

        # Warten und nochmal lesen
        print(f"  Warte 3s und lese Status...")
        time.sleep(3)
        state_after = reader.read_register(1000)
        power_after = reader.read_register(1020)
        print(f"  Nachher: State={state_after}, Power={power_after}mW")
    else:
        print(f"  {fail('Schreibbefehl fehlgeschlagen')}")

    reader.disconnect()
    return success


def simulate_surplus(results):
    """Simuliert die PV-Ueberschussberechnung mit den gelesenen Werten."""
    print(header("Simulation: PV-Ueberschussberechnung"))

    voltage = results.get(1040, 230)
    if voltage == 0:
        voltage = 230

    max_current_ma = results.get(1110, 32000)
    min_current_ma = 6000
    buffer_w = 200

    print(f"  Spannung L1: {voltage} V")
    print(f"  Max. Strom (HW): {max_current_ma} mA ({max_current_ma/1000:.0f} A)")
    print(f"  Min. Strom: {min_current_ma} mA ({min_current_ma/1000:.0f} A)")
    print(f"  Puffer: {buffer_w} W")
    print()

    # Simuliere verschiedene Ueberschuss-Szenarien
    scenarios = [
        ("Kein Ueberschuss (500W Bezug)",     500),
        ("Wenig Ueberschuss (-500W)",        -500),
        ("Minimum 1-phasig (~1600W)",       -1600),
        ("Mittlerer Ueberschuss (-3000W)",  -3000),
        ("Minimum 3-phasig (~4500W)",       -4500),
        ("Viel Ueberschuss (-7000W)",       -7000),
        ("Maximum (-10000W)",              -10000),
    ]

    print(f"  {'Szenario':<40s} {'Grid':>8s} {'1-Phase':>10s} {'3-Phasen':>10s}")
    print(f"  {'-'*40} {'-'*8} {'-'*10} {'-'*10}")

    for desc, grid_w in scenarios:
        for phases in (1, 3):
            available = (-grid_w) - buffer_w  # Kein laufender Wallbox-Verbrauch
            if available <= 0:
                current_ma = 0
            else:
                current_a = available / (voltage * phases)
                current_ma = int(current_a * 1000)
                if current_ma < min_current_ma:
                    current_ma = 0
                if current_ma > max_current_ma:
                    current_ma = max_current_ma

            if phases == 1:
                c1 = f"{current_ma/1000:.1f}A" if current_ma > 0 else "Pause"
            else:
                c3 = f"{current_ma/1000:.1f}A" if current_ma > 0 else "Pause"

        power_1p = (int(available / voltage * 1000) if available > 0 else 0)
        if power_1p < min_current_ma:
            power_1p = 0

        print(f"  {desc:<40s} {grid_w:>7}W {c1:>10s} {c3:>10s}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="KEBA P40 Modbus TCP Test- und Diagnosetool",
        epilog="Dieses Skript liest nur Register und schreibt NICHTS (ausser mit --write-test)."
    )
    parser.add_argument("host", help="IP-Adresse der Keba P40 Wallbox")
    parser.add_argument("--port", type=int, default=502, help="Modbus TCP Port (Standard: 502)")
    parser.add_argument("--write-test", type=int, metavar="MA",
                        help="VORSICHT: Setzt Ladestrom in mA (0 oder 6000-32000). "
                             "Nur verwenden wenn du weisst was du tust!")

    args = parser.parse_args()

    print(f"\n{BOLD}KEBA P40 Modbus TCP Diagnose{RESET}")
    print(f"Ziel: {args.host}:{args.port}")
    print(f"Modus: {'Nur Lesen (sicher)' if args.write_test is None else f'SCHREIBTEST ({args.write_test} mA)'}")
    print()

    # Test 1: Netzwerk
    if not test_network(args.host, args.port):
        print(f"\n{RED}Abbruch: Netzwerk nicht erreichbar.{RESET}")
        sys.exit(1)

    # Test 2: Register lesen
    success, results = test_modbus_read(args.host)
    if not success:
        print(f"\n{RED}Abbruch: Modbus TCP Kommunikation fehlgeschlagen.{RESET}")
        sys.exit(1)

    # Test 3: Analyse
    test_interpret(results)

    # Simulation
    if results:
        simulate_surplus(results)

    # Optionaler Schreibtest
    if args.write_test is not None:
        print(f"\n{YELLOW}{BOLD}ACHTUNG: Schreibtest veraendert den Ladestrom der Wallbox!{RESET}")
        confirm = input(f"  Wirklich {args.write_test} mA auf Register 5004 schreiben? (ja/nein): ")
        if confirm.lower() in ("ja", "j", "yes", "y"):
            test_write_current(args.host, args.write_test)
        else:
            print("  Schreibtest abgebrochen.")

    print(f"\n{BOLD}Fertig.{RESET}\n")


if __name__ == "__main__":
    main()
