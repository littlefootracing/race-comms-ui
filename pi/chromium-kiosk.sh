#!/bin/bash
# Wait for pi_main.py HTTP server to be ready
for i in $(seq 1 20); do
  curl -sf http://localhost:8080/cockpit.html > /dev/null && break
  sleep 1
done

exec chromium \
  --ozone-platform=wayland \
  --kiosk \
  --noerrdialogs \
  --disable-infobars \
  --no-first-run \
  --disable-session-crashed-bubble \
  --disable-restore-session-state \
  --password-store=basic \
  http://localhost:8080/cockpit.html
