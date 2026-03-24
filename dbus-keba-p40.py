#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Venus OS D-Bus driver for KEBA KeContact P40 Wallbox
=====================================================
Communicates with the Keba P40 via Modbus TCP and publishes
the wallbox as com.victronenergy.evcharger on the Venus OS D-Bus.

Includes PV surplus charging logic: reads the grid meter value from
Venus OS D-Bus and adjusts the Keba charging current accordingly.

Based on:
- KEBA KeContact P40 Modbus TCP Programmers Guide V1.02 (04/2025)
- Venus OS D-Bus API: https://github.com/victronenergy/venus/wiki/dbus-api
- velib_python: https://github.com/victronenergy/velib_python

Author: Hendrik Bohn
License: MIT
"""

import sys
import os
import logging
import configparser
import struct
import socket
import time
import threading

# Venus OS uses velib_python, pre-installed on Venus OS devices.
# For development, clone https://github.com/victronenergy/velib_python
# and add it to PYTHONPATH.
sys.path.insert(1, os.path.join(os.path.dirname(__file__), "ext", "velib_python"))

try:
    from vedbus import VeDbusService
    import dbus
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib
except ImportError as e:
    print(f"Fehler beim Importieren der Venus OS Bibliotheken: {e}")
    print("Dieses Skript muss auf einem Venus OS System laufen.")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [dbus-keba-p40] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("dbus-keba-p40")


# ---------------------------------------------------------------------------
# Keba P40 Modbus TCP Register Definitions
# Source: KeContact P40 Modbus TCP Programmers Guide V1.02
# ---------------------------------------------------------------------------

# Protocol constants (from Guide Section 2, p.7-8)
KEBA_MODBUS_PORT = 502
KEBA_UNIT_ID = 255  # Fixed per Guide p.8
KEBA_FC_READ = 3    # Function Code 3: Read Holding Registers
KEBA_FC_WRITE = 6   # Function Code 6: Write Single Register

# Read interval: min 0.5s per Guide p.8
# Write interval: min 5s per Guide p.8
KEBA_READ_INTERVAL_S = 1.0
KEBA_WRITE_INTERVAL_S = 5.0

# --- Readable Registers (Section 3) ---

# Current measurement (Section 3.1, p.9)
REG_CURRENT_L1 = 1008       # Charging current phase 1, mA, UINT32, ro
REG_CURRENT_L2 = 1010       # Charging current phase 2, mA, UINT32, ro
REG_CURRENT_L3 = 1012       # Charging current phase 3, mA, UINT32, ro

# Power and energy measurements (Section 3.2, p.10-11)
REG_ACTIVE_POWER = 1020     # Active power, mW, UINT32, ro
REG_TOTAL_ENERGY = 1036     # Total energy, 0.1 Wh, UINT32, ro
REG_POWER_FACTOR = 1046     # Power factor, 0.1 %, UINT32, ro
REG_VOLTAGE_L1 = 1040       # Voltage phase 1, V, UINT32, ro
REG_VOLTAGE_L2 = 1042       # Voltage phase 2, V, UINT32, ro
REG_VOLTAGE_L3 = 1044       # Voltage phase 3, V, UINT32, ro

# State information (Section 3.3, p.12-13)
REG_CHARGING_STATE = 1000   # Charging state, UINT32, ro
# Values: 0=Start-up, 1=Not ready, 2=Ready (waiting for EV),
#         3=Charging, 4=Error, 5=Suspended (temp too high)
REG_CABLE_STATE = 1004      # Cable state, UINT32, ro
REG_ERROR_CODE = 1006       # Error code, UINT32, ro

# Product information (Section 3.4, p.14-15)
REG_SERIAL = 1014           # Serial number, UINT32, ro
REG_PRODUCT = 1016          # Product type and features, UINT32, ro
REG_FIRMWARE = 1018         # Software package version, UINT32, ro
REG_HW_REVISION = 1700      # Hardware revision device, UINT32, ro

# Charging limits (Section 3.5, p.16)
REG_MAX_CURRENT = 1100      # Max charging current, mA, UINT32, ro
REG_MAX_SUPPORTED = 1110    # Max supported current (HW limit), mA, UINT32, ro
REG_FAST_CHARGE_STATE = 1200  # Fast charging state, UINT32, ro
# Values: 0=Deactivated, 1=Active (current not controllable via Modbus)

# Session information (Section 3.6, p.17)
REG_RFID_TAG = 1500         # RFID card UID (first 4 bytes), UINT32, ro
REG_SESSION_ENERGY = 1502   # Charged energy this session, 0.1 Wh, UINT32, ro

# Phase switching settings (Section 3.7, p.18)
# NOTE: Register 1550 and 1552 require a future software update!
REG_PHASE_SWITCH_SOURCE = 1550  # Phase switching source, UINT32, ro
# Values: 0=Not available, 1=OCPP, 2=REST API, 3=Modbus TCP
REG_PHASE_SWITCH_STATE = 1552   # Phase switching state, UINT32, ro
# Values: 1=1-phase, 3=3-phase

# EMS Failsafe settings (Section 3.8, p.19)
REG_FAILSAFE_CURRENT_SETTING = 1600  # Failsafe current setting, mA, UINT32, ro
REG_FAILSAFE_TIMEOUT_SETTING = 1602  # Failsafe timeout setting, s, UINT32, ro

# --- Writable Registers (Section 4, p.20-23) ---
# All writable registers are UINT16, Function Code 6 (FC6)

REG_SET_CURRENT = 5004      # Set charging current, mA, UINT16, wo
# Values: 0=Suspend, 6000-32000=Set current in mA (Section 4.1, p.20)

REG_SET_ENERGY = 5010       # Set energy limit, 10 Wh, UINT16, wo
# Values: 0=Delete limit, >0=Energy limit (Section 4.2, p.20)

REG_UNLOCK_PLUG = 5012      # Unlock plug, UINT16, wo
# Values: 0=Unlock (Section 4.3, p.21)

# NOTE: Register 5014 requires a future software update!
REG_ENABLE_STATION = 5014   # Enable/Disable station, UINT16, wo
# Values: 0=Disable, 1=Enable (Section 4.4, p.21)

# NOTE: Registers 5050 and 5052 require a future software update!
REG_SET_PHASE_SOURCE = 5050  # Set phase switching source, UINT16, wo
# Values: 0=Deactivated, 1=Profiles (OCPP/PV), 2=REST API, 3=Modbus TCP
# (Section 4.5, p.21)
REG_TRIGGER_PHASE_SWITCH = 5052  # Trigger phase switch, UINT16, wo
# Values: 0=1-phase, 1=3-phase (Section 4.6, p.22)

REG_FAILSAFE_CURRENT = 5016  # Failsafe current, mA, UINT16, wo
# Values: 0=Suspend on lost connection, 6000-32000=Failsafe current
# (Section 4.7.1, p.23)

REG_FAILSAFE_TIMEOUT = 5018  # Failsafe timeout, s, UINT16, wo
# Values: 0=Deactivate failsafe, 5-600=Timeout in seconds
# (Section 4.7.2, p.23)

REG_FAST_CHARGING = 5200     # Activate fast charging, UINT16, wo
# Values: 1=Activate (cannot deactivate via Modbus!) (Section 4.8, p.23)


# ---------------------------------------------------------------------------
# Keba Charging State -> Venus OS EV Charger Status mapping
# ---------------------------------------------------------------------------
# Keba states (Guide Section 3.3.1, p.12):
#   0 = Start-up
#   1 = Not ready (not connected / locked by auth)
#   2 = Ready (waiting for EV reaction)
#   3 = Charging active
#   4 = Error
#   5 = Suspended (temperature too high)
#
# Venus OS evcharger Status values (from community drivers):
#   0 = Disconnected
#   1 = Connected
#   2 = Charging
#   3 = Charged
#   4 = Waiting for sun
#   5 = Waiting for RFID
#   6 = Waiting for start
#   7 = Low SOC
#   8 = Ground error
#   9 = Welded contacts error
#  10 = CP input test error (EVSE)
#  11 = Residual current detected (EVSE)
#  21 = Charging limit (power)
#  22 = Charging limit (SOC)
#  23 = Charging limit (grid)
#  24 = Charging limit (schedule)

KEBA_TO_VENUS_STATUS = {
    0: 0,   # Start-up -> Disconnected
    1: 0,   # Not ready -> Disconnected
    2: 1,   # Ready (waiting for EV) -> Connected
    3: 2,   # Charging -> Charging
    4: 10,  # Error -> EVSE error
    5: 6,   # Suspended (temp) -> Waiting for start
}


# ---------------------------------------------------------------------------
# Modbus TCP Client (raw sockets, no external dependencies)
# ---------------------------------------------------------------------------
# Implements Modbus TCP as described in Guide Section 2, p.8:
# Frame: Transaction ID (2) + Protocol ID (2) + Length (2) + Unit ID (1)
#        + Function Code (1) + Data (n)

class KebaModbusTCP:
    """
    Minimal Modbus TCP client for the Keba P40.

    Uses raw TCP sockets per the Modbus TCP standard (IEC 61158).
    The Keba P40 supports FC3 (Read) and FC6 (Write) only, and
    can only read one register at a time (max 2 words / UINT32).
    Reference: Guide Section 2, p.8
    """

    def __init__(self, host, port=KEBA_MODBUS_PORT, unit_id=KEBA_UNIT_ID, timeout=5.0):
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.timeout = timeout
        self._socket = None
        self._transaction_id = 0
        self._lock = threading.Lock()
        self._last_write_time = 0

    def connect(self):
        """Establish TCP connection to Keba P40."""
        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket.settimeout(self.timeout)
            self._socket.connect((self.host, self.port))
            log.info(f"Verbunden mit Keba P40 auf {self.host}:{self.port}")
            return True
        except Exception as e:
            log.error(f"Verbindung zu Keba P40 fehlgeschlagen: {e}")
            self._socket = None
            return False

    def disconnect(self):
        """Close TCP connection."""
        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None

    def is_connected(self):
        return self._socket is not None

    def _next_transaction_id(self):
        self._transaction_id = (self._transaction_id + 1) % 65536
        return self._transaction_id

    def read_register(self, register):
        """
        Read a single UINT32 register from the Keba P40.

        Per Guide p.8: FC3, max reading length is 2 words (UINT32).
        Register address starts at 0. Registers must be sent in decimal.

        Returns the UINT32 value or None on error.
        """
        with self._lock:
            if not self._socket:
                return None

            try:
                tid = self._next_transaction_id()

                # Build Modbus TCP frame (Guide p.8):
                # Transaction ID: 2 bytes
                # Protocol ID: 0x0000 (Modbus TCP)
                # Length: 6 bytes (Unit ID + FC + Register + Quantity)
                # Unit ID: 255 (Guide p.8)
                # FC: 3 (Read Holding Registers)
                # Register: 2 bytes
                # Quantity: 2 words (for UINT32)
                request = struct.pack(
                    ">HHHBBHH",
                    tid,        # Transaction ID
                    0,          # Protocol ID (0 for Modbus TCP)
                    6,          # Length of remaining bytes
                    self.unit_id,  # Unit ID (255)
                    KEBA_FC_READ,  # Function Code 3
                    register,   # Starting register address
                    2,          # Number of registers to read (2 = UINT32)
                )

                self._socket.sendall(request)

                # Read response header (7 bytes MBAP + 2 bytes FC+ByteCount)
                response = self._recv_exact(9)
                if response is None:
                    return None

                resp_tid, resp_proto, resp_len, resp_unit, resp_fc, byte_count = \
                    struct.unpack(">HHHBBB", response)

                if resp_fc != KEBA_FC_READ:
                    # Error response
                    log.warning(f"Modbus Fehler bei Register {register}: FC={resp_fc:#x}")
                    return None

                if byte_count != 4:
                    log.warning(f"Unerwartete Byte-Anzahl: {byte_count} bei Register {register}")
                    return None

                # Read 4 data bytes (UINT32)
                data = self._recv_exact(4)
                if data is None:
                    return None

                value = struct.unpack(">I", data)[0]
                return value

            except Exception as e:
                log.error(f"Lesefehler Register {register}: {e}")
                self.disconnect()
                return None

    def write_register(self, register, value):
        """
        Write a UINT16 value to a single register on the Keba P40.

        Per Guide p.8: FC6 (Write Single Register).
        Write interval minimum: 5 seconds (Guide p.8).

        Returns True on success, False on error.
        """
        with self._lock:
            if not self._socket:
                return False

            # Enforce minimum write interval of 5s (Guide p.8)
            now = time.monotonic()
            elapsed = now - self._last_write_time
            if elapsed < KEBA_WRITE_INTERVAL_S:
                wait = KEBA_WRITE_INTERVAL_S - elapsed
                log.debug(f"Warte {wait:.1f}s vor naechstem Schreibbefehl (min. 5s lt. Handbuch)")
                time.sleep(wait)

            try:
                tid = self._next_transaction_id()

                # Build Modbus TCP Write frame:
                # FC6: Write Single Register (UINT16)
                request = struct.pack(
                    ">HHHBBHH",
                    tid,           # Transaction ID
                    0,             # Protocol ID
                    6,             # Length
                    self.unit_id,  # Unit ID (255)
                    KEBA_FC_WRITE, # Function Code 6
                    register,      # Register address
                    value & 0xFFFF,  # UINT16 value
                )

                self._socket.sendall(request)

                # Read response (echo of request for FC6)
                response = self._recv_exact(12)
                if response is None:
                    return False

                resp_tid, resp_proto, resp_len, resp_unit, resp_fc = \
                    struct.unpack(">HHHBB", response[:8])

                self._last_write_time = time.monotonic()

                if resp_fc != KEBA_FC_WRITE:
                    log.warning(f"Schreibfehler Register {register}: FC={resp_fc:#x}")
                    return False

                log.debug(f"Register {register} = {value} geschrieben")
                return True

            except Exception as e:
                log.error(f"Schreibfehler Register {register}: {e}")
                self.disconnect()
                return False

    def _recv_exact(self, count):
        """Receive exactly count bytes from socket."""
        data = b""
        while len(data) < count:
            try:
                chunk = self._socket.recv(count - len(data))
                if not chunk:
                    log.error("Verbindung geschlossen")
                    self.disconnect()
                    return None
                data += chunk
            except socket.timeout:
                log.error("Timeout beim Empfangen")
                self.disconnect()
                return None
        return data


# ---------------------------------------------------------------------------
# Venus OS D-Bus EV Charger Service
# ---------------------------------------------------------------------------

class KebaP40Service:
    """
    Venus OS D-Bus service for the Keba KeContact P40.

    Publishes the wallbox as com.victronenergy.evcharger and implements
    PV surplus charging by reading the grid meter from Venus OS D-Bus.
    """

    # Venus OS evcharger modes
    MODE_MANUAL = 0
    MODE_AUTO = 1       # PV surplus mode
    MODE_SCHEDULED = 2

    def __init__(self, config):
        self.config = config
        self.keba_host = config.get("Keba", "host")
        self.keba_port = config.getint("Keba", "port", fallback=KEBA_MODBUS_PORT)

        # Charging parameters
        self.min_current_ma = config.getint("Charging", "min_current_ma", fallback=6000)
        self.failsafe_current_ma = config.getint("Charging", "failsafe_current_ma", fallback=0)
        self.failsafe_timeout_s = config.getint("Charging", "failsafe_timeout_s", fallback=30)
        self.surplus_buffer_w = config.getint("Charging", "surplus_buffer_w", fallback=200)
        self.ramp_step_ma = config.getint("Charging", "ramp_step_ma", fallback=1000)
        self.phase_switching = config.getboolean("Charging", "phase_switching", fallback=True)

        # Batterie-Prioritaet
        # Strategie: grid_only, battery_above_soc, ev_first
        self.battery_strategy = config.get("Battery", "strategy", fallback="battery_above_soc")
        self.battery_soc_threshold = config.getint("Battery", "soc_threshold", fallback=80)
        self.battery_min_soc = config.getint("Battery", "min_soc", fallback=20)

        # Dry-Run Modus: liest Daten, berechnet alles, aber schreibt NICHTS
        self.dry_run = config.getboolean("General", "dry_run", fallback=False)
        if self.dry_run:
            log.warning("=== DRY-RUN MODUS AKTIV: Es wird NICHTS zur Wallbox geschrieben! ===")

        # Internal state
        self._mode = self.MODE_AUTO  # Default: PV surplus mode
        self._start_stop = 1         # Charging enabled
        self._set_current_ma = 0     # Current target (mA)
        self._max_current_ma = 32000
        self._position = 1           # 1 = AC Output (after inverter)
        self._current_phases = 3     # Current phase count (1 or 3)
        self._charging_start_time = 0
        self._last_current_write = 0
        self._surplus_history = []   # Rolling average of surplus

        # Keba live data cache
        self._keba_data = {}

        # Modbus TCP client
        self.modbus = KebaModbusTCP(
            self.keba_host,
            self.keba_port,
            timeout=config.getfloat("Keba", "timeout", fallback=5.0),
        )

        # D-Bus setup
        self._dbusservice = None
        self._dbus_conn = None
        self._setup_dbus()

    # --- D-Bus Service Setup ---

    def _setup_dbus(self):
        """
        Register the EV charger service on the Venus OS D-Bus.

        Service name: com.victronenergy.evcharger.keba_p40
        Paths based on Venus OS D-Bus API and community EV charger drivers.
        """
        servicename = "com.victronenergy.evcharger.keba_p40"
        device_instance = self.config.getint("Venus", "device_instance", fallback=40)

        self._dbusservice = VeDbusService(servicename, register=False)

        # --- Management paths (mandatory per Venus OS D-Bus API) ---
        self._dbusservice.add_path("/Mgmt/ProcessName", __file__)
        self._dbusservice.add_path("/Mgmt/ProcessVersion", "1.0.0")
        self._dbusservice.add_path("/Mgmt/Connection", f"Modbus TCP {self.keba_host}:{self.keba_port}")

        # --- Product identification (mandatory per Venus OS D-Bus API) ---
        self._dbusservice.add_path("/DeviceInstance", device_instance)
        self._dbusservice.add_path("/ProductId", 0xFFFF)
        self._dbusservice.add_path("/ProductName", "KEBA KeContact P40")
        self._dbusservice.add_path("/FirmwareVersion", "0.0.0")
        self._dbusservice.add_path("/HardwareVersion", 0)
        self._dbusservice.add_path("/Serial", "")
        self._dbusservice.add_path("/Connected", 0)
        self._dbusservice.add_path("/CustomName", "KEBA P40 Wallbox")

        # --- EV Charger specific paths ---

        # AC measurements
        self._dbusservice.add_path("/Ac/Power", 0, gettextcallback=lambda p, v: f"{v:.0f}W")
        self._dbusservice.add_path("/Ac/L1/Power", 0, gettextcallback=lambda p, v: f"{v:.0f}W")
        self._dbusservice.add_path("/Ac/L2/Power", 0, gettextcallback=lambda p, v: f"{v:.0f}W")
        self._dbusservice.add_path("/Ac/L3/Power", 0, gettextcallback=lambda p, v: f"{v:.0f}W")
        self._dbusservice.add_path("/Ac/Energy/Forward", 0, gettextcallback=lambda p, v: f"{v:.2f}kWh")

        # Current
        self._dbusservice.add_path("/Current", 0, gettextcallback=lambda p, v: f"{v:.1f}A")

        # Controllable settings (writable from Venus OS GUI / MQTT)
        self._dbusservice.add_path(
            "/MaxCurrent", 32,
            gettextcallback=lambda p, v: f"{v:.0f}A",
            writeable=True,
            onchangecallback=self._on_max_current_changed,
        )
        self._dbusservice.add_path(
            "/SetCurrent", 0,
            gettextcallback=lambda p, v: f"{v:.0f}A",
            writeable=True,
            onchangecallback=self._on_set_current_changed,
        )
        self._dbusservice.add_path(
            "/Mode", self.MODE_AUTO,
            writeable=True,
            onchangecallback=self._on_mode_changed,
        )
        self._dbusservice.add_path(
            "/StartStop", 1,
            writeable=True,
            onchangecallback=self._on_start_stop_changed,
        )
        self._dbusservice.add_path(
            "/AutoStart", 1,
            writeable=True,
            onchangecallback=self._on_auto_start_changed,
        )

        # Status
        self._dbusservice.add_path("/Status", 0)
        self._dbusservice.add_path("/ChargingTime", 0, gettextcallback=lambda p, v: f"{v}s")
        self._dbusservice.add_path("/Model", "KeContact P40")

        # Position: 0 = AC Input (grid side), 1 = AC Output (inverter side)
        self._dbusservice.add_path("/Position", self._position, writeable=True)

        # NrOfPhases - important for Venus OS power calculations
        self._dbusservice.add_path("/NrOfPhases", 3)

        # Register the service on D-Bus
        self._dbusservice.register()
        log.info(f"D-Bus Service registriert: {servicename} (DeviceInstance={device_instance})")

    # --- D-Bus Callbacks ---

    def _on_max_current_changed(self, path, value):
        """Handle MaxCurrent change from Venus OS."""
        if value is not None:
            self._max_current_ma = int(value) * 1000
            log.info(f"MaxCurrent geaendert: {value}A")
        return True

    def _on_set_current_changed(self, path, value):
        """Handle SetCurrent change from Venus OS (manual mode)."""
        if value is not None and self._mode == self.MODE_MANUAL:
            target_ma = int(value) * 1000
            self._set_current_ma = max(0, min(target_ma, self._max_current_ma))
            log.info(f"SetCurrent geaendert (manuell): {value}A")
        return True

    def _on_mode_changed(self, path, value):
        """Handle Mode change from Venus OS."""
        if value is not None:
            self._mode = int(value)
            mode_names = {0: "Manuell", 1: "Automatisch (PV)", 2: "Geplant"}
            log.info(f"Modus geaendert: {mode_names.get(self._mode, 'Unbekannt')}")
        return True

    def _on_start_stop_changed(self, path, value):
        """Handle StartStop change from Venus OS."""
        if value is not None:
            self._start_stop = int(value)
            log.info(f"StartStop geaendert: {'Ein' if self._start_stop else 'Aus'}")
            if not self._start_stop:
                # Immediately suspend charging (Register 5004 = 0, Guide p.20)
                self._write_charging_current(0)
        return True

    def _on_auto_start_changed(self, path, value):
        return True

    # --- Venus OS D-Bus Readings (Grid, Battery, PV) ---

    def _read_dbus_value(self, service, path, fallback=None):
        """Read a single value from Venus OS D-Bus."""
        try:
            bus = dbus.SystemBus()
            proxy = bus.get_object(service, path)
            val = proxy.GetValue()
            return float(val) if val is not None else fallback
        except Exception:
            return fallback

    def _read_grid_power(self):
        """
        Read total grid power from Venus OS D-Bus.

        Venus OS path: com.victronenergy.system /Ac/Grid/Power
        Positive = importing from grid
        Negative = exporting to grid (surplus)

        Returns total grid power in Watts, or None if unavailable.
        """
        val = self._read_dbus_value("com.victronenergy.system", "/Ac/Grid/Power")
        if val is not None:
            return val

        # Fallback: per-phase sum
        try:
            l1 = self._read_dbus_value("com.victronenergy.system", "/Ac/Grid/L1/Power", 0)
            l2 = self._read_dbus_value("com.victronenergy.system", "/Ac/Grid/L2/Power", 0)
            l3 = self._read_dbus_value("com.victronenergy.system", "/Ac/Grid/L3/Power", 0)
            return l1 + l2 + l3
        except Exception as e:
            log.warning(f"Grid-Meter nicht lesbar: {e}")
            return None

    def _read_battery_soc(self):
        """
        Read battery State of Charge from Venus OS D-Bus.

        Venus OS path: com.victronenergy.system /Dc/Battery/Soc
        Returns SOC in percent (0-100), or None if no battery.
        """
        return self._read_dbus_value("com.victronenergy.system", "/Dc/Battery/Soc")

    def _read_battery_power(self):
        """
        Read battery charge/discharge power from Venus OS D-Bus.

        Venus OS path: com.victronenergy.system /Dc/Battery/Power
        Positive = battery is charging (consuming PV)
        Negative = battery is discharging (feeding loads)

        Returns power in Watts, or None if no battery.
        """
        return self._read_dbus_value("com.victronenergy.system", "/Dc/Battery/Power")

    def _read_pv_power(self):
        """
        Read total PV production from Venus OS D-Bus.

        Sums DC-coupled (/Dc/Pv/Power) and AC-coupled (/Ac/PvOnOutput/Power).
        Returns total PV power in Watts, or 0 if unavailable.
        """
        dc_pv = self._read_dbus_value("com.victronenergy.system", "/Dc/Pv/Power", 0)
        ac_pv = self._read_dbus_value("com.victronenergy.system", "/Ac/PvOnOutput/Power", 0)
        return dc_pv + ac_pv

    # --- Keba Modbus Communication ---

    def _read_keba_data(self):
        """
        Read all relevant registers from the Keba P40.

        Register addresses and data types per Guide Sections 3.1-3.8.
        Note: Only one register can be read at a time (Guide p.8).
        """
        if not self.modbus.is_connected():
            if not self.modbus.connect():
                return False

        data = {}

        # Read in order of importance for PV surplus logic
        registers = [
            ("charging_state", REG_CHARGING_STATE),     # Section 3.3.1
            ("active_power_mw", REG_ACTIVE_POWER),      # Section 3.2.1
            ("current_l1_ma", REG_CURRENT_L1),          # Section 3.1.1
            ("current_l2_ma", REG_CURRENT_L2),          # Section 3.1.2
            ("current_l3_ma", REG_CURRENT_L3),          # Section 3.1.3
            ("voltage_l1", REG_VOLTAGE_L1),             # Section 3.2.4
            ("voltage_l2", REG_VOLTAGE_L2),             # Section 3.2.5
            ("voltage_l3", REG_VOLTAGE_L3),             # Section 3.2.6
            ("total_energy", REG_TOTAL_ENERGY),         # Section 3.2.2
            ("session_energy", REG_SESSION_ENERGY),     # Section 3.6.2
            ("cable_state", REG_CABLE_STATE),           # Section 3.3.2
            ("error_code", REG_ERROR_CODE),             # Section 3.3.3
            ("max_current_ma", REG_MAX_CURRENT),        # Section 3.5.1
            ("max_supported_ma", REG_MAX_SUPPORTED),    # Section 3.5.2
            ("fast_charge_state", REG_FAST_CHARGE_STATE),  # Section 3.5.3
            ("power_factor", REG_POWER_FACTOR),         # Section 3.2.3
        ]

        # Read phase switching state if enabled
        if self.phase_switching:
            registers.append(("phase_switch_source", REG_PHASE_SWITCH_SOURCE))  # Section 3.7.1
            registers.append(("phase_switch_state", REG_PHASE_SWITCH_STATE))    # Section 3.7.2

        for name, reg in registers:
            value = self.modbus.read_register(reg)
            if value is not None:
                data[name] = value
            else:
                # If a critical register fails, abort
                if name in ("charging_state",):
                    log.error(f"Kritisches Register {name} ({reg}) nicht lesbar")
                    return False

        self._keba_data = data
        return True

    def _write_charging_current(self, current_ma):
        """
        Set charging current on the Keba P40.

        Register 5004 (Guide Section 4.1, p.20):
        - 0 = Suspend charging session
        - 6000-32000 = Charging current in mA

        Note: Write interval minimum 5 seconds (Guide p.8).
        """
        if not self.modbus.is_connected():
            return False

        # Enforce valid range per Guide Section 4.1
        if current_ma > 0 and current_ma < self.min_current_ma:
            current_ma = 0  # Below minimum -> suspend
        if current_ma > 32000:
            current_ma = 32000

        if self.dry_run:
            log.info(f"[DRY-RUN] Wuerde Ladestrom setzen: {current_ma} mA "
                     f"(Register 5004) - NICHT geschrieben")
            self._last_current_write = current_ma
            return True

        success = self.modbus.write_register(REG_SET_CURRENT, int(current_ma))
        if success:
            self._last_current_write = current_ma
            log.debug(f"Ladestrom gesetzt: {current_ma} mA")
        return success

    def _setup_failsafe(self):
        """
        Configure EMS Failsafe on the Keba P40.

        Per Guide Section 4.7 (p.22-23):
        - Register 5018: Failsafe Timeout (0=deactivate, 5-600s)
        - Register 5016: Failsafe Current (0=suspend, 6000-32000 mA)

        If the Modbus TCP connection is lost for longer than the timeout,
        the Keba falls back to the failsafe current.
        """
        if self.dry_run:
            log.info(f"[DRY-RUN] Wuerde EMS Failsafe konfigurieren: "
                     f"Timeout={self.failsafe_timeout_s}s, Strom={self.failsafe_current_ma}mA")
            return

        if self.failsafe_timeout_s > 0:
            log.info(f"Konfiguriere EMS Failsafe: Timeout={self.failsafe_timeout_s}s, "
                      f"Strom={self.failsafe_current_ma}mA")
            self.modbus.write_register(REG_FAILSAFE_TIMEOUT, self.failsafe_timeout_s)
            time.sleep(KEBA_WRITE_INTERVAL_S)
            self.modbus.write_register(REG_FAILSAFE_CURRENT, self.failsafe_current_ma)
        else:
            log.info("EMS Failsafe deaktiviert")
            self.modbus.write_register(REG_FAILSAFE_TIMEOUT, 0)

    def _setup_phase_switching(self):
        """
        Enable phase switching via Modbus TCP if supported.

        Register 5050 (Guide Section 4.5, p.21):
        Value 3 = Phase switching via Modbus TCP

        NOTE: This register requires a future software update per Guide!
        The driver attempts to set it but handles failure gracefully.
        """
        if not self.phase_switching:
            return

        if self.dry_run:
            log.info("[DRY-RUN] Wuerde Phasenumschaltung via Modbus TCP aktivieren (Register 5050=3)")
            return

        log.info("Versuche Phasenumschaltung via Modbus TCP zu aktivieren (Register 5050=3)")
        success = self.modbus.write_register(REG_SET_PHASE_SOURCE, 3)
        if not success:
            log.warning("Phasenumschaltung via Modbus TCP nicht verfuegbar. "
                        "Moeglicherweise ist ein Firmware-Update erforderlich "
                        "(siehe Guide Section 4.5, Hinweis).")
            self.phase_switching = False

    # --- PV Surplus Charging Logic ---

    def _calculate_surplus_current(self, grid_power_w):
        """
        Calculate the target charging current based on PV surplus
        and battery priority strategy.

        Strategies (configurable in [Battery] section of config.ini):

        1. "grid_only" (konservativ):
           Nur echter Netz-Ueberschuss geht ins Auto. Die Batterie wird
           vom ESS-System vollstaendig normal geladen/entladen. Das EV
           bekommt nur, was sonst ins Netz eingespeist wuerde.
           -> Batterie hat immer Vorrang.

        2. "battery_above_soc" (empfohlen):
           Unter dem SOC-Schwellwert hat die Batterie Vorrang (wie grid_only).
           Ueber dem Schwellwert wird die Leistung, die der ESS in die
           Batterie stecken wuerde, als zusaetzlicher Ueberschuss fuer
           das EV betrachtet.
           -> Batterie bis X% laden, dann Auto.

        3. "ev_first" (aggressiv):
           Die Batterie-Ladeleistung wird immer als verfuegbarer Ueberschuss
           mit eingerechnet. Die Batterie wird nur geladen, wenn das Auto
           voll ist oder nicht angeschlossen ist.
           -> Auto hat Vorrang.

        In allen Faellen: Wenn die Batterie unter min_soc faellt, wird
        die EV-Ladung pausiert um die Batterie zu schuetzen.
        """
        # Current wallbox power consumption
        current_wallbox_power = self._keba_data.get("active_power_mw", 0) / 1000.0  # mW -> W

        # Read battery state from Venus OS D-Bus
        battery_soc = self._read_battery_soc()
        battery_power = self._read_battery_power()  # >0 = charging, <0 = discharging

        # Safety: if battery below minimum SOC, don't charge EV
        if battery_soc is not None and battery_soc < self.battery_min_soc:
            log.info(f"Batterie SOC {battery_soc:.0f}% < Minimum {self.battery_min_soc}% "
                     f"-> EV-Ladung pausiert (Batterie-Schutz)")
            return 0

        # Base surplus = what goes to grid + what wallbox already uses
        available_w = (-grid_power_w) + current_wallbox_power - self.surplus_buffer_w

        # Apply battery strategy
        if self.battery_strategy == "battery_above_soc" and battery_soc is not None:
            if battery_soc >= self.battery_soc_threshold:
                # Above threshold: battery charging power is also available for EV
                if battery_power is not None and battery_power > 0:
                    available_w += battery_power
                    log.debug(f"Batterie SOC {battery_soc:.0f}% >= {self.battery_soc_threshold}%: "
                              f"+{battery_power:.0f}W Batterie-Ladeleistung fuer EV verfuegbar")
            else:
                log.debug(f"Batterie SOC {battery_soc:.0f}% < {self.battery_soc_threshold}%: "
                          f"Batterie hat Vorrang")

        elif self.battery_strategy == "ev_first":
            # Always count battery charging power as available for EV
            if battery_power is not None and battery_power > 0:
                available_w += battery_power
                log.debug(f"EV-First: +{battery_power:.0f}W Batterie-Ladeleistung umgeleitet")

        elif self.battery_strategy == "grid_only":
            # Only true grid surplus, battery is managed by ESS independently
            log.debug(f"Grid-Only: Nur Netz-Ueberschuss ({-grid_power_w:.0f}W)")

        else:
            log.warning(f"Unbekannte Batterie-Strategie: {self.battery_strategy}, "
                        f"verwende grid_only")

        # Log decision
        if battery_soc is not None:
            log.debug(f"Ueberschuss-Berechnung: Grid={grid_power_w:.0f}W, "
                      f"Batterie={battery_power:.0f}W (SOC={battery_soc:.0f}%), "
                      f"Wallbox={current_wallbox_power:.0f}W -> "
                      f"Verfuegbar={available_w:.0f}W")

        if available_w <= 0:
            return 0

        # Get voltage for current calculation
        voltage = self._keba_data.get("voltage_l1", 230)
        if voltage == 0:
            voltage = 230  # Fallback

        # Calculate current based on phases
        phases = self._current_phases
        target_current_a = available_w / (voltage * phases)
        target_current_ma = int(target_current_a * 1000)

        # Clamp to valid range (Guide Section 4.1: 6000-32000 mA)
        hw_max = self._keba_data.get("max_supported_ma", 32000)
        if target_current_ma < self.min_current_ma:
            return 0  # Not enough surplus
        if target_current_ma > min(self._max_current_ma, hw_max):
            target_current_ma = min(self._max_current_ma, hw_max)

        return target_current_ma

    def _should_switch_phases(self, target_current_ma, grid_power_w):
        """
        Determine if a phase switch would be beneficial.

        Strategy:
        - If on 3 phases and surplus is too low -> switch to 1 phase
        - If on 1 phase and surplus is high enough for 3 phases -> switch to 3

        Thresholds based on minimum 6A (Guide Section 4.1):
        - 1-phase minimum: ~1380W (6A * 230V)
        - 3-phase minimum: ~4140W (6A * 230V * 3)
        """
        if not self.phase_switching:
            return None  # No switch needed

        voltage = self._keba_data.get("voltage_l1", 230)
        if voltage == 0:
            voltage = 230

        current_wallbox_power = self._keba_data.get("active_power_mw", 0) / 1000.0
        available_w = (-grid_power_w) + current_wallbox_power - self.surplus_buffer_w

        min_1phase_w = self.min_current_ma / 1000.0 * voltage * 1
        min_3phase_w = self.min_current_ma / 1000.0 * voltage * 3

        if self._current_phases == 3 and available_w < min_3phase_w and available_w >= min_1phase_w:
            log.info(f"Phasenumschaltung: 3->1 Phase (Ueberschuss {available_w:.0f}W < {min_3phase_w:.0f}W)")
            return 1
        elif self._current_phases == 1 and available_w >= min_3phase_w * 1.2:
            # 1.2x hysteresis to avoid oscillation
            log.info(f"Phasenumschaltung: 1->3 Phasen (Ueberschuss {available_w:.0f}W >= {min_3phase_w * 1.2:.0f}W)")
            return 3

        return None  # No switch

    def _trigger_phase_switch(self, target_phases):
        """
        Trigger phase switching on the Keba P40.

        Register 5052 (Guide Section 4.6, p.22):
        - 0 = 1-phase
        - 1 = 3-phase (default)

        Prerequisite: Register 5050 must be set to 3 (Modbus TCP).
        During switch, charging is suspended, then resumed.

        NOTE: This register requires a future software update per Guide!
        """
        if target_phases == 1:
            value = 0
        elif target_phases == 3:
            value = 1
        else:
            return False

        if self.dry_run:
            log.info(f"[DRY-RUN] Wuerde Phasenumschaltung auf {target_phases} Phase(n) "
                     f"ausloesen (Register 5052={value})")
            return True

        log.info(f"Phasenumschaltung auf {target_phases} Phase(n)...")
        success = self.modbus.write_register(REG_TRIGGER_PHASE_SWITCH, value)
        if success:
            self._current_phases = target_phases
            self._dbusservice["/NrOfPhases"] = target_phases
            log.info(f"Phasenumschaltung auf {target_phases} Phase(n) ausgeloest")
        else:
            log.warning("Phasenumschaltung fehlgeschlagen")
        return success

    # --- Smooth current ramping ---

    def _ramp_current(self, current_ma, target_ma):
        """
        Smoothly ramp charging current towards target.

        Avoids abrupt changes that could stress the car's onboard charger
        or cause grid instability. Step size is configurable.
        """
        if target_ma == 0 and current_ma > 0:
            return 0  # Immediate stop when target is 0

        diff = target_ma - current_ma
        if abs(diff) <= self.ramp_step_ma:
            return target_ma

        if diff > 0:
            return current_ma + self.ramp_step_ma
        else:
            return current_ma - self.ramp_step_ma

    # --- Update D-Bus from Keba Data ---

    def _update_dbus(self):
        """
        Update Venus OS D-Bus paths with current Keba data.

        Converts Keba register values to Venus OS units:
        - Current: mA -> A
        - Power: mW -> W
        - Energy: 0.1 Wh -> kWh
        - Charging state: Keba codes -> Venus OS status codes
        """
        d = self._keba_data

        # AC measurements
        power_w = d.get("active_power_mw", 0) / 1000.0  # mW -> W
        self._dbusservice["/Ac/Power"] = round(power_w, 1)

        # Per-phase power estimation from current and voltage
        v1 = d.get("voltage_l1", 0)
        v2 = d.get("voltage_l2", 0)
        v3 = d.get("voltage_l3", 0)
        i1 = d.get("current_l1_ma", 0) / 1000.0  # mA -> A
        i2 = d.get("current_l2_ma", 0) / 1000.0
        i3 = d.get("current_l3_ma", 0) / 1000.0

        self._dbusservice["/Ac/L1/Power"] = round(v1 * i1, 1) if v1 and i1 else 0
        self._dbusservice["/Ac/L2/Power"] = round(v2 * i2, 1) if v2 and i2 else 0
        self._dbusservice["/Ac/L3/Power"] = round(v3 * i3, 1) if v3 and i3 else 0

        # Total energy (Register 1036, Unit: 0.1 Wh -> kWh)
        # Known bug: FW < 1.2.1 reports in Wh instead of 0.1 Wh (Guide p.24)
        total_energy_kwh = d.get("total_energy", 0) * 0.1 / 1000.0
        self._dbusservice["/Ac/Energy/Forward"] = round(total_energy_kwh, 2)

        # Current (use max of 3 phases as representative)
        max_current_a = max(i1, i2, i3)
        self._dbusservice["/Current"] = round(max_current_a, 1)

        # Charging state -> Venus OS status
        keba_state = d.get("charging_state", 0)
        venus_status = KEBA_TO_VENUS_STATUS.get(keba_state, 0)

        # If in PV surplus mode and surplus is too low but car is connected
        if (self._mode == self.MODE_AUTO and keba_state == 2
                and self._set_current_ma == 0):
            venus_status = 4  # Waiting for sun

        self._dbusservice["/Status"] = venus_status

        # Charging time tracking
        if keba_state == 3 and self._charging_start_time == 0:
            self._charging_start_time = time.monotonic()
        elif keba_state != 3:
            self._charging_start_time = 0

        if self._charging_start_time > 0:
            self._dbusservice["/ChargingTime"] = int(time.monotonic() - self._charging_start_time)
        else:
            self._dbusservice["/ChargingTime"] = 0

        # MaxCurrent from hardware (Guide Section 3.5.2)
        hw_max = d.get("max_supported_ma", 32000)
        self._dbusservice["/MaxCurrent"] = hw_max // 1000

        # SetCurrent reflects what we're currently commanding
        self._dbusservice["/SetCurrent"] = self._set_current_ma // 1000

        # Phase count
        phase_state = d.get("phase_switch_state", 3)
        if phase_state in (1, 3):
            self._current_phases = phase_state
            self._dbusservice["/NrOfPhases"] = phase_state

        # Connection status
        self._dbusservice["/Connected"] = 1

    def _read_product_info(self):
        """
        Read static product information once at startup.

        Registers: 1014 (Serial), 1016 (Product), 1018 (Firmware),
                   1700 (HW Revision) - Guide Sections 3.4.1-3.4.4
        """
        serial = self.modbus.read_register(REG_SERIAL)
        if serial is not None:
            self._dbusservice["/Serial"] = str(serial)
            log.info(f"Keba Seriennummer: {serial}")

        firmware = self.modbus.read_register(REG_FIRMWARE)
        if firmware is not None:
            # Firmware as 5-digit number MMMMM -> M.M.M (Guide Section 3.4.3)
            major = firmware // 10000
            medium = (firmware % 10000) // 100
            minor = firmware % 100
            fw_str = f"{major}.{medium}.{minor}"
            self._dbusservice["/FirmwareVersion"] = fw_str
            log.info(f"Keba Firmware: {fw_str}")

        hw_rev = self.modbus.read_register(REG_HW_REVISION)
        if hw_rev is not None:
            self._dbusservice["/HardwareVersion"] = hw_rev

        product = self.modbus.read_register(REG_PRODUCT)
        if product is not None:
            log.info(f"Keba Produkt-Key: {product}")

    # --- Main Update Loop ---

    def _update(self):
        """
        Main update cycle, called by GLib.timeout_add.

        1. Read Keba registers via Modbus TCP
        2. Update Venus OS D-Bus paths
        3. Calculate PV surplus and adjust charging current
        4. Handle phase switching if applicable

        Returns True to keep the timer running (GLib convention).
        """
        try:
            # Step 1: Read Keba data
            if not self._read_keba_data():
                self._dbusservice["/Connected"] = 0
                # Try to reconnect
                self.modbus.disconnect()
                time.sleep(1)
                if self.modbus.connect():
                    self._dbusservice["/Connected"] = 1
                return True  # Keep timer running

            # Step 2: Update D-Bus
            self._update_dbus()

            # Step 3: Charging logic
            keba_state = self._keba_data.get("charging_state", 0)
            fast_charge = self._keba_data.get("fast_charge_state", 0)

            # If fast charging is active, current cannot be controlled (Guide Section 3.5.3)
            if fast_charge == 1:
                log.debug("Fast Charging aktiv - Strom nicht steuerbar")
                return True

            # If charging is disabled via StartStop
            if not self._start_stop:
                if self._last_current_write != 0:
                    self._write_charging_current(0)
                return True

            # Only adjust current when car is connected and ready/charging
            if keba_state not in (2, 3):
                return True

            if self._mode == self.MODE_AUTO:
                # --- PV Surplus Mode ---
                grid_power = self._read_grid_power()
                if grid_power is not None:
                    # Rolling average over last readings for stability
                    self._surplus_history.append(grid_power)
                    if len(self._surplus_history) > 5:
                        self._surplus_history.pop(0)
                    avg_grid = sum(self._surplus_history) / len(self._surplus_history)

                    # Calculate target current from surplus
                    target_ma = self._calculate_surplus_current(avg_grid)

                    # Check phase switching
                    new_phases = self._should_switch_phases(target_ma, avg_grid)
                    if new_phases is not None:
                        self._trigger_phase_switch(new_phases)
                        return True  # Wait for next cycle after phase switch

                    # Ramp towards target
                    target_ma = self._ramp_current(self._set_current_ma, target_ma)
                    self._set_current_ma = target_ma

                    # Write to Keba (respects 5s write interval internally)
                    self._write_charging_current(target_ma)

            elif self._mode == self.MODE_MANUAL:
                # --- Manual Mode ---
                # Use the SetCurrent value directly
                self._write_charging_current(self._set_current_ma)

        except Exception as e:
            log.error(f"Fehler im Update-Zyklus: {e}", exc_info=True)

        return True  # Keep timer running (GLib requirement)

    # --- Startup ---

    def start(self):
        """
        Start the Keba P40 driver.

        1. Connect to Keba via Modbus TCP
        2. Read product info
        3. Configure failsafe
        4. Setup phase switching
        5. Start periodic update loop
        """
        log.info("=== KEBA P40 Venus OS Treiber startet ===")
        log.info(f"Keba Host: {self.keba_host}:{self.keba_port}")
        log.info(f"Min. Ladestrom: {self.min_current_ma} mA")
        log.info(f"Phasenumschaltung: {'Ja' if self.phase_switching else 'Nein'}")
        log.info(f"PV Puffer: {self.surplus_buffer_w} W")
        strategy_names = {
            "grid_only": "Nur Netz-Ueberschuss (Batterie hat immer Vorrang)",
            "battery_above_soc": f"Batterie bis {self.battery_soc_threshold}% SOC, dann EV",
            "ev_first": "EV hat immer Vorrang vor Batterie",
        }
        log.info(f"Batterie-Strategie: {strategy_names.get(self.battery_strategy, self.battery_strategy)}")
        log.info(f"Batterie Min-SOC: {self.battery_min_soc}% (EV-Ladung pausiert darunter)")
        if self.dry_run:
            log.warning("=== DRY-RUN: Alle Berechnungen aktiv, aber KEIN Schreiben zur Wallbox ===")

        # Connect to Keba
        retry_count = 0
        while not self.modbus.connect():
            retry_count += 1
            log.warning(f"Verbindungsversuch {retry_count} fehlgeschlagen, warte 10s...")
            time.sleep(10)
            if retry_count > 10:
                log.error("Konnte keine Verbindung zur Keba P40 herstellen!")
                sys.exit(1)

        # Read product info once
        self._read_product_info()

        # Configure failsafe (Guide Section 4.7)
        time.sleep(KEBA_WRITE_INTERVAL_S)
        self._setup_failsafe()

        # Setup phase switching (Guide Section 4.5)
        time.sleep(KEBA_WRITE_INTERVAL_S)
        self._setup_phase_switching()

        # Start update loop (every 2 seconds)
        update_interval_ms = int(config.getfloat("Charging", "update_interval_s", fallback=2.0) * 1000)
        GLib.timeout_add(update_interval_ms, self._update)

        log.info("Treiber gestartet. Warte auf Daten...")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config():
    """Load configuration from config.ini next to this script."""
    config = configparser.ConfigParser()
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")

    if not os.path.exists(config_path):
        log.error(f"Konfigurationsdatei nicht gefunden: {config_path}")
        log.error("Bitte config.ini erstellen (siehe config.ini.example)")
        sys.exit(1)

    config.read(config_path)

    # Validate required settings
    if not config.has_option("Keba", "host"):
        log.error("Konfiguration fehlt: [Keba] host (IP-Adresse der Wallbox)")
        sys.exit(1)

    return config


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Setup D-Bus main loop
    DBusGMainLoop(set_as_default=True)

    # Load configuration
    config = load_config()

    # Set log level from config
    log_level = config.get("General", "log_level", fallback="INFO").upper()
    log.setLevel(getattr(logging, log_level, logging.INFO))

    # Create and start service
    service = KebaP40Service(config)
    service.start()

    # Run GLib main loop
    log.info("GLib MainLoop gestartet")
    mainloop = GLib.MainLoop()
    try:
        mainloop.run()
    except KeyboardInterrupt:
        log.info("Beende...")
        service.modbus.disconnect()
        sys.exit(0)
