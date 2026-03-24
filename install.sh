#!/bin/bash
# =============================================================================
# Installationsskript: KEBA P40 Venus OS Treiber
# =============================================================================
#
# Installation direkt aus GitHub:
#   git clone https://github.com/hendrik-b42/dbus-kebap40.git /data/dbus-keba-p40
#   bash /data/dbus-keba-p40/install.sh
#
# Oder von einem beliebigen Verzeichnis:
#   chmod +x install.sh && ./install.sh
#
# Voraussetzungen:
#   - Venus OS 3.x auf Raspberry Pi
#   - Keba P40 im selben Netzwerk, Modbus TCP aktiviert
#   - SSH/Root-Zugang zum Venus OS
#
# Basiert auf:
#   https://github.com/victronenergy/venus/wiki/howto-add-a-driver-to-Venus

set -e

INSTALL_DIR="/data/dbus-keba-p40"
SERVICE_DIR="/opt/victronenergy/service/dbus-keba-p40"
LOG_DIR="/var/log/dbus-keba-p40"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
GITHUB_REPO="https://github.com/hendrik-b42/dbus-kebap40.git"

echo "=== KEBA P40 Venus OS Treiber Installation ==="
echo ""

# Pruefen ob Venus OS
if [ ! -d "/opt/victronenergy" ]; then
    echo "FEHLER: Dies scheint kein Venus OS System zu sein."
    echo "        /opt/victronenergy nicht gefunden."
    exit 1
fi

# --- git sicherstellen (wird fuer velib_python und ggf. Clone benoetigt) ---
ensure_git() {
    if command -v git &> /dev/null; then
        return 0
    fi
    echo "   git nicht gefunden, installiere git via opkg..."
    if command -v opkg &> /dev/null; then
        opkg update && opkg install git
    else
        echo "FEHLER: git nicht gefunden und opkg nicht verfuegbar."
        echo "        Bitte git manuell installieren."
        exit 1
    fi
}

# 0. velib_python herunterladen falls nicht vorhanden
EXT_DIR="${INSTALL_DIR}/ext/velib_python"
if [ ! -d "${EXT_DIR}" ]; then
    echo "[0/6] Lade velib_python herunter..."
    mkdir -p "${INSTALL_DIR}/ext"
    ensure_git
    git clone --depth 1 https://github.com/victronenergy/velib_python.git "${EXT_DIR}"
else
    echo "[0/6] velib_python bereits vorhanden, uebersprungen."
fi

# 1. Dateien nach /data kopieren (ueberlebt Firmware-Updates)
echo "[1/6] Kopiere Dateien nach ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}/log"

if [ "${SCRIPT_DIR}" != "${INSTALL_DIR}" ]; then
    cp "${SCRIPT_DIR}/dbus-keba-p40.py" "${INSTALL_DIR}/"
    cp "${SCRIPT_DIR}/log/run" "${INSTALL_DIR}/log/"
    cp "${SCRIPT_DIR}/run" "${INSTALL_DIR}/"
    cp "${SCRIPT_DIR}/config.ini.example" "${INSTALL_DIR}/"
    echo "   Dateien kopiert."
else
    echo "   Dateien liegen bereits in ${INSTALL_DIR}, Kopieren uebersprungen."
fi

# Config aus Template erstellen wenn noch nicht vorhanden
if [ ! -f "${INSTALL_DIR}/config.ini" ]; then
    cp "${INSTALL_DIR}/config.ini.example" "${INSTALL_DIR}/config.ini"
    echo "   WICHTIG: Bitte ${INSTALL_DIR}/config.ini anpassen!"
    echo "   Mindestens [Keba] host = <IP-Adresse der Wallbox>"
else
    echo "   config.ini existiert bereits, wird nicht ueberschrieben."
fi

# 2. Ausfuehrbar machen
echo "[2/6] Setze Berechtigungen..."
chmod +x "${INSTALL_DIR}/run"
chmod +x "${INSTALL_DIR}/log/run"
chmod +x "${INSTALL_DIR}/dbus-keba-p40.py"

# 3. Log-Verzeichnis erstellen
echo "[3/6] Erstelle Log-Verzeichnis..."
mkdir -p "${LOG_DIR}"

# 4. Service-Symlink erstellen
echo "[4/6] Erstelle Service-Symlink..."
rm -f "${SERVICE_DIR}"
ln -s "${INSTALL_DIR}" "${SERVICE_DIR}"
echo "   ${SERVICE_DIR} -> ${INSTALL_DIR}"

# 5. rc.local Eintrag fuer Persistenz nach Firmware-Updates
echo "[5/6] Konfiguriere Autostart (rc.local)..."
RC_LOCAL="/data/rc.local"
RC_ENTRY="ln -sfn ${INSTALL_DIR} ${SERVICE_DIR}"

if [ -f "${RC_LOCAL}" ]; then
    if grep -q "dbus-keba-p40" "${RC_LOCAL}"; then
        echo "   rc.local Eintrag existiert bereits."
    else
        echo "" >> "${RC_LOCAL}"
        echo "# KEBA P40 Venus OS Treiber" >> "${RC_LOCAL}"
        echo "${RC_ENTRY}" >> "${RC_LOCAL}"
        echo "   Eintrag zu rc.local hinzugefuegt."
    fi
else
    echo "#!/bin/bash" > "${RC_LOCAL}"
    echo "" >> "${RC_LOCAL}"
    echo "# KEBA P40 Venus OS Treiber" >> "${RC_LOCAL}"
    echo "${RC_ENTRY}" >> "${RC_LOCAL}"
    chmod +x "${RC_LOCAL}"
    echo "   rc.local erstellt."
fi

echo ""
echo "=== Installation abgeschlossen ==="
echo ""
echo "Naechste Schritte:"
echo "  1. Konfiguration anpassen:  nano ${INSTALL_DIR}/config.ini"
echo "     -> [Keba] host = <IP-Adresse deiner Wallbox>"
echo ""
echo "  2. Sicherstellen, dass Modbus TCP auf der Keba aktiviert ist"
echo "     (ueber KEBA eMobility App oder OCPP)"
echo ""
echo "  3. Service starten:  svc -u ${SERVICE_DIR}"
echo "     Service stoppen:  svc -d ${SERVICE_DIR}"
echo "     Logs ansehen:     tail -f ${LOG_DIR}/current | tai64nlocal"
echo ""
echo "  4. Oder einfach: reboot"
echo ""
echo "Update vom GitHub:"
echo "  cd ${INSTALL_DIR} && git pull && bash install.sh"
