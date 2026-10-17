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
pip3 install websockets RPi.GPIO bleak
```
`bleak` is only needed for the RaceBox (see section 5).

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

## 5. RaceBox Mini S (telemetry + lap timing)
`pi_main.py` connects to the RaceBox over BLE (Pi needs Bluetooth, `bleak` installed) and sends the pit
speed, G-force, GPS position/health, battery and lap times at 5 Hz. Lap detection itself runs on the Pi at the full 25 Hz.

| Env var (add as `Environment=` in `cockpit.service`) | Meaning |
|---|---|
| `RACEBOX_MODE` | `ble` (default), `sim` (fake car lapping a loop — no hardware needed), `off` |
| `RACEBOX_ADDRESS` | Optional BLE address; otherwise the first device named "RaceBox Mini S…" is used |
| `RACEBOX_RECORD=1` | Also start the Mini S's on-board standalone recording (backup log, RaceBox's recommended filters) |
| `RACEBOX_CODE` | Memory security code, only if you enabled memory protection on the device |
| `RACEBOX_SIM_LAP_S` | Simulated lap length in seconds (default 45) |

**Lap timing:** the device has no lap timer, so the pit sets a start/finish line from the dashboard:
tap **Set S/F line** while the car is moving on track. The line is placed across the car's direction of
travel where it is at that moment. Anywhere works — lap times only need a consistent line. It is saved to
`pi/startline.json` and survives reboots. Laps under 20 s are ignored as double triggers.

**Try it without hardware:** `RACEBOX_MODE=sim python3 pi/pi_main.py`, or run the protocol/lap-timer tests:
`python3 -m unittest discover -s pi -p 'test_*.py'`.

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
    ├── pi_main.py      ← GPIO + relay client + local WS server + RaceBox
    ├── racebox.py      ← RaceBox BLE protocol, BLE client, simulator
    ├── laptimer.py     ← GPS start/finish line lap timing
    ├── test_racebox.py ← protocol + lap timer tests (no hardware)
    ├── cockpit.html    ← Fullscreen kiosk display
    ├── cockpit.service ← systemd for pi_main.py
    └── chromium.service← systemd for Chromium kiosk
```
