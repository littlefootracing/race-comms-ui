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

## 6. Getting out of the kiosk (Wi-Fi, terminal, shutdown)

The cockpit runs from labwc autostart (`~/.config/labwc/autostart` → `chromium-kiosk.sh`).
If `chromium.service` is enabled too, turn it off — it relaunches Chromium every 5 s and fights you:
`sudo systemctl disable --now chromium.service`

**Exit to desktop / shut down from the car buttons** (BOX REQUEST is never used, so a real box call can't trigger these):

| Hold together | For | Does |
|---|---|---|
| ALL GOOD + ACKNOWLEDGE | 3 s | Closes the kiosk → normal desktop (Wi-Fi menu, terminal, power menu) |
| FUEL LOW + ACKNOWLEDGE | 5 s | Safe shutdown — use this instead of pulling power |

The first press of each button still sends its normal message to the pit, so tell the pit before doing this.

**Other ways in:**
- Keyboard plugged in: `Ctrl+Alt+F2` gives a text login, or `Alt+F4` then quickly open a terminal.
- SD card in a Mac: create an empty file named `nokiosk` on the `bootfs` drive. Kiosk stays off until you delete it (`sudo rm /boot/firmware/nokiosk`).
- SSH: `touch ~/nokiosk && pkill -f chromium`.

**Back to the cockpit:** double-click **Start Cockpit** on the desktop (also in the app menu), reboot, or run `~/race_comms/pi/chromium-kiosk.sh --resume`.

**Wi-Fi from a terminal** (NetworkManager on Bookworm) — save every network the car will see, highest priority first:
```bash
nmcli dev wifi list
sudo nmcli dev wifi connect "STARLINK-SSID" password "..."
sudo nmcli dev wifi connect "Phone Hotspot" password "..."
sudo nmcli connection modify "STARLINK-SSID" connection.autoconnect-priority 20
sudo nmcli connection modify "Phone Hotspot" connection.autoconnect-priority 10
```
Or `sudo nmtui` for a menu. Power off from a terminal: `sudo poweroff`.

## 7. Bench-test the RaceBox
```bash
sudo apt install -y python3-bleak          # or: pip3 install --break-system-packages bleak
sudo systemctl stop cockpit.service        # frees the BLE connection
cd ~/race_comms/pi && python3 racebox_scan.py
```
Close the RaceBox phone app first — the device takes one connection at a time. Don't pair it in the
desktop Bluetooth menu; it isn't needed. Copy the address it prints into `cockpit.service` as
`Environment=RACEBOX_ADDRESS=...`, then `sudo systemctl daemon-reload && sudo systemctl start cockpit.service`.
The pit dashboard header should turn green ("RaceBox").

## 8. Setup screen in the cockpit (Wi-Fi + RaceBox)
Tap the status chip in the top-right corner of the cockpit (Wi-Fi / RaceBox / Pit dots).
- **Wi-Fi:** see the current network, signal and IP. *Change network* lists nearby networks; saved and
  open ones join with a tap, new ones pop up an on-screen keyboard for the password.
- **RaceBox:** connection, GPS fix, satellites, battery. *Find RaceBox* scans ~8 s; tap your unit to pair it
  (saved to `pi/racebox.json`, survives reboots and deploys). *Use any RaceBox* goes back to first-found.
- **Pit link:** relay status and whether a start/finish line is set, plus hold-to-confirm *Exit to desktop*
  and *Shut down*.
Any pit call (BOX, PIT NOW, …) closes the setup screen immediately; it also closes itself after 2 min.

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
    ├── racebox_scan.py ← bench test: find the RaceBox and print live data
    ├── netinfo.py      ← Wi-Fi status/scan/join for the setup screen (nmcli)
    ├── install.sh      ← one-shot Pi setup (deploy-pi.sh runs it)
    ├── cockpit.html    ← Fullscreen kiosk display
    ├── cockpit.service ← systemd for pi_main.py
    └── chromium.service← systemd for Chromium kiosk
```
