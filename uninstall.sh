#!/bin/bash
# =============================================================================
# Deinstallation: KEBA P40 Venus OS Treiber
# =============================================================================

set -e

SERVICE_DIR="/opt/victronenergy/service/dbus-keba-p40"
INSTALL_DIR="/data/dbus-keba-p40"
LOG_DIR="/var/log/dbus-keba-p40"

echo "=== KEBA P40 Venus OS Treiber Deinstallation ==="

# Service stoppen
if [ -L "${SERVICE_DIR}" ]; then
    echo "Stoppe Service..."
    svc -d "${SERVICE_DIR}" 2>/dev/null || true
    sleep 2
    rm -f "${SERVICE_DIR}"
fi

# Dateien entfernen (config.ini bleibt als Backup)
echo "Entferne Dateien..."
rm -f "${INSTALL_DIR}/dbus-keba-p40.py"
rm -f "${INSTALL_DIR}/run"
rm -rf "${INSTALL_DIR}/log"
echo "   config.ini bleibt in ${INSTALL_DIR}/ als Backup"

# rc.local bereinigen
if [ -f "/data/rc.local" ]; then
    sed -i '/dbus-keba-p40/d' /data/rc.local
    sed -i '/KEBA P40/d' /data/rc.local
    echo "rc.local bereinigt"
fi

# Logs entfernen
rm -rf "${LOG_DIR}"

echo ""
echo "=== Deinstallation abgeschlossen ==="
echo "Verbleibende Konfiguration: ${INSTALL_DIR}/config.ini"
