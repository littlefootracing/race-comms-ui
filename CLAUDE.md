# Littlefoot Racing — Pit Comms

## Quick Reference

### View the Pit Dashboard
```bash
open /Users/jfalicea/Documents/race_comms/pit-dashboard/index.html
```
Plain HTML file — no build step. Requires the relay server WebSocket URL to be set inside `index.html`.

Check/update the relay URL:
```bash
grep -n 'wss\|ws://' pit-dashboard/index.html
```

### Architecture
- `relay-server/` — Node.js WebSocket relay, deployed to Railway
- `pit-dashboard/index.html` — Pit crew browser app (connects to relay via WSS)
- `pi/pi_main.py` — Runs on Raspberry Pi in car; reads GPIO buttons, connects to relay
- `pi/cockpit.html` — Fullscreen kiosk display shown inside the car

### Run the Relay Locally (dev)
```bash
cd relay-server && npm install && node server.js
```

### Deploy Relay to Railway
```bash
cd relay-server
railway login && railway up
```

### GPIO Button Map (BCM pins, active-low)
| Button       | GPIO |
|--------------|------|
| BOX REQUEST  | 17   |
| FUEL LOW     | 27   |
| ALL GOOD     | 22   |
| ACKNOWLEDGE  | 23   |
