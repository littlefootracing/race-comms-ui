#!/bin/bash
# Littlefoot Racing — cockpit kiosk launcher (labwc autostart on Pi OS Bookworm).
#
# Keeps Chromium fullscreen on the cockpit page and relaunches it if it crashes,
# UNLESS maintenance mode is on. Maintenance mode = any of these files exists:
#   /run/littlefoot/nokiosk   created by the button combo (pi_main.py); cleared on reboot
#   /boot/firmware/nokiosk    create it from a Mac/PC with the SD card plugged in; stays until deleted
#   ~/nokiosk                 touch it over SSH; stays until deleted
#
#   chromium-kiosk.sh --resume   clears the temporary flag and starts the cockpit again

FLAGS=(/run/littlefoot/nokiosk /boot/firmware/nokiosk "$HOME/nokiosk")

if [ "$1" = "--resume" ]; then
  rm -f /run/littlefoot/nokiosk "$HOME/nokiosk"
  if [ -e /boot/firmware/nokiosk ]; then
    echo "Delete /boot/firmware/nokiosk first: sudo rm /boot/firmware/nokiosk" >&2
    exit 1
  fi
  pgrep -f "chromium-kiosk.sh" | grep -vx "$$" > /dev/null && exit 0  # already running
  setsid "$0" > /dev/null 2>&1 < /dev/null &
  exit 0
fi

maintenance() {
  for f in "${FLAGS[@]}"; do [ -e "$f" ] && return 0; done
  return 1
}

# Wait for pi_main.py HTTP server to be ready
for i in $(seq 1 20); do
  curl -sf http://localhost:8080/cockpit.html > /dev/null && break
  sleep 1
done

CHROMIUM=$(command -v chromium || command -v chromium-browser)

while ! maintenance; do
  "$CHROMIUM" \
    --ozone-platform=wayland \
    --kiosk \
    --noerrdialogs \
    --disable-infobars \
    --no-first-run \
    --disable-session-crashed-bubble \
    --disable-restore-session-state \
    --password-store=basic \
    http://localhost:8080/cockpit.html
  sleep 2
done
echo "Maintenance mode — kiosk not started"
