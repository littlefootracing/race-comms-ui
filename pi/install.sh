#!/bin/bash
# Littlefoot Racing — one-shot Pi setup. Safe to re-run (deploy-pi.sh runs it every time).
# Expects the repo's pi/ folder at ~/race_comms/pi. Run as your normal user (uses sudo).
set -e
PI_DIR="$HOME/race_comms/pi"
USER_NAME="$(id -un)"

# deploy-pi.sh passes the sudo password (from the Mac's gitignored .pi-password) so setup runs unattended
if [ -n "$SUDO_PASS" ]; then
  sudo() { printf '%s\n' "$SUDO_PASS" | command sudo -S -p '' "$@"; }
fi

echo "== Packages"
sudo apt-get update -qq
sudo apt-get install -y python3-websockets python3-gpiozero python3-lgpio python3-bleak \
  chromium curl rsync

echo "== Hardware access"
sudo usermod -aG gpio,bluetooth "$USER_NAME" || true
sudo rfkill unblock bluetooth || true

echo "== cockpit.service (pi_main.py: relay, buttons, RaceBox)"
sed "s#/home/jfalicea#$HOME#g" "$PI_DIR/cockpit.service" > /tmp/cockpit.service
sudo cp /tmp/cockpit.service /etc/systemd/system/cockpit.service
if systemctl list-unit-files chromium.service >/dev/null 2>&1 && systemctl is-enabled chromium.service >/dev/null 2>&1; then
  sudo systemctl disable --now chromium.service    # old kiosk service; relaunched Chromium every 5 s
fi
sudo systemctl daemon-reload
sudo systemctl enable cockpit.service
sudo systemctl restart cockpit.service

echo "== Kiosk (labwc autostart)"
chmod +x "$PI_DIR/chromium-kiosk.sh"
mkdir -p "$HOME/.config/labwc"
touch "$HOME/.config/labwc/autostart"
grep -qs chromium-kiosk.sh "$HOME/.config/labwc/autostart" || \
  echo "$PI_DIR/chromium-kiosk.sh &" >> "$HOME/.config/labwc/autostart"

echo "== Start Cockpit launcher (desktop icon + app menu) for after 'Exit to desktop'"
LAUNCHER="[Desktop Entry]
Type=Application
Name=Start Cockpit
Comment=Return to the Littlefoot Racing cockpit screen
Exec=$PI_DIR/chromium-kiosk.sh --resume
Icon=applications-games
Terminal=false
Categories=Utility;"
mkdir -p "$HOME/.local/share/applications" "$HOME/Desktop"
printf '%s\n' "$LAUNCHER" > "$HOME/.local/share/applications/littlefoot-cockpit.desktop"
printf '%s\n' "$LAUNCHER" > "$HOME/Desktop/Start Cockpit.desktop"
chmod +x "$HOME/Desktop/Start Cockpit.desktop" "$HOME/.local/share/applications/littlefoot-cockpit.desktop"
# open .desktop files without the "Execute / Open?" prompt in the file manager
mkdir -p "$HOME/.config/libfm"
if [ -f "$HOME/.config/libfm/libfm.conf" ]; then
  grep -q '^quick_exec=' "$HOME/.config/libfm/libfm.conf" \
    && sed -i 's/^quick_exec=.*/quick_exec=1/' "$HOME/.config/libfm/libfm.conf" \
    || sed -i '/^\[config\]/a quick_exec=1' "$HOME/.config/libfm/libfm.conf"
else
  printf '[config]\nquick_exec=1\n' > "$HOME/.config/libfm/libfm.conf"
fi

echo "== Boot to desktop with auto-login"
sudo raspi-config nonint do_boot_behaviour B4 || echo "!! set Desktop Autologin in raspi-config manually"

echo
echo "Setup done. Reboot to start the cockpit:  sudo reboot"
echo "Button combos: ALL GOOD+ACK 3 s = desktop, FUEL LOW+ACK 5 s = shutdown."
echo "RaceBox test:  sudo systemctl stop cockpit.service && cd $PI_DIR && python3 racebox_scan.py"
