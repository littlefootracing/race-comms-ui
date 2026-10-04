# Littlefoot Racing — Setup Guide

## 1. Deploy Relay Server to Railway

```bash
cd relay-server
npm install
# Push to Railway via GitHub or Railway CLI:
railway login
railway init
railway up
# Note your Railway public URL: wss://your-app.railway.app
```

## 2. Pi Setup

### SSH into Pi (enable SSH first if needed)
```bash
# On Pi with monitor/keyboard:
sudo systemctl enable ssh && sudo systemctl start ssh
```

### Install Python deps on Pi
```bash
pip3 install websockets RPi.GPIO
```

### Copy files to Pi
```bash
rsync -av pi/ pi@raspberrypi.local:/home/pi/race_comms/pi/
```

### Set your Railway URL
Edit `/home/pi/race_comms/pi/cockpit.service` — replace `wss://your-relay.railway.app` with your actual URL.

### Install and enable systemd services
```bash
sudo cp /home/pi/race_comms/pi/cockpit.service /etc/systemd/system/
sudo cp /home/pi/race_comms/pi/chromium.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable cockpit.service chromium.service
sudo systemctl start cockpit.service
```

### Boot into graphical target (for Chromium kiosk)
```bash
sudo systemctl set-default graphical.target
sudo reboot
```

## 3. Pit Dashboard
Open `pit-dashboard/index.html` directly from the relay server URL, or host it alongside the relay. Update the WebSocket URL in the HTML if hosting separately.

## 4. GPIO Button Wiring
| Button | GPIO (BCM) | Wire to |
|--------|-----------|---------|
| BOX REQUEST | 17 | GND when pressed |
| FUEL LOW    | 27 | GND when pressed |
| ALL GOOD    | 22 | GND when pressed |
| MECH ISSUE  | 23 | GND when pressed |

Pins use internal pull-up. Button = normally open, connects pin to GND.

## Files
```
race_comms/
├── relay-server/
│   ├── server.js       ← Node.js WS relay (deploy to Railway)
│   ├── package.json
│   └── railway.toml
├── pit-dashboard/
│   └── index.html      ← Pit crew browser app
└── pi/
    ├── pi_main.py      ← GPIO + relay client + local WS server
    ├── cockpit.html    ← Fullscreen kiosk display
    ├── cockpit.service ← systemd for pi_main.py
    └── chromium.service← systemd for Chromium kiosk
```
