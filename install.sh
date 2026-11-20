#!/bin/sh
# Home Display installieren (Raspberry Pi OS Bookworm, Raspberry Pi Zero W)
# Aufruf im entpackten Paket-Ordner:  sh install.sh
# Kann gefahrlos noch einmal laufen: vorhandene Einstellungen (config.json) bleiben.
set -e
cd "$(dirname "$0")"
ZIEL="$HOME/openhab-display"
NUTZER="$(id -un)"

echo "== 1/5 Pakete installieren (dauert auf dem Pi Zero einige Minuten)"
sudo apt-get update
sudo apt-get install -y python3-pil python3-spidev python3-lgpio python3-qrcode fonts-dejavu-core network-manager

echo "== 2/5 SPI für das Display einschalten"
sudo raspi-config nonint do_spi 0
sudo usermod -aG spi,gpio "$NUTZER"

echo "== 3/5 Programm nach $ZIEL kopieren"
mkdir -p "$ZIEL"
cp -r . "$ZIEL/"; rm -rf "$ZIEL/.git" "$ZIEL/bilder" "$ZIEL/README.md"
if [ ! -f "$ZIEL/config.json" ]; then
    cp start-config.json "$ZIEL/config.json"
    echo "   Start-Einstellungen: ohne openHAB, nur Wiener Linien"
else
    echo "   Vorhandene Einstellungen bleiben"
fi

echo "== 4/5 Dienst einrichten (startet beim Hochfahren)"
sed -e "s|^User=.*|User=$NUTZER|" -e "s|/home/admin/openhab-display|$ZIEL|g" \
    "$ZIEL/openhab-display.service" | sudo tee /etc/systemd/system/openhab-display.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable openhab-display
# Hotspot: jede Adresse fuehrt zum Pi (Einrichtung oeffnet sich am Handy von selbst)
sudo mkdir -p /etc/NetworkManager/dnsmasq-shared.d
sudo cp "$ZIEL/hotspot-dnsmasq.conf" /etc/NetworkManager/dnsmasq-shared.d/home-display.conf

echo "== 5/5 Fertig."
echo "Jetzt den Pi neu starten:  sudo reboot"
echo "Danach im Browser:  http://$(hostname).local:8080"
