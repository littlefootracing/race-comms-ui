#!/usr/bin/env python3
"""
Littlefoot Racing — Pi Main
- Connects to Railway WebSocket relay as "car"
- Reads 4 GPIO buttons via gpiozero and sends button_press messages
- Runs a local WebSocket server on port 8765 for cockpit.html
- Runs a local HTTP server on port 8080 serving cockpit.html
- RaceBox Mini S over BLE (or simulator): live telemetry + GPS lap timing → relay
  RACEBOX_MODE=ble (default) | sim | off
"""

import asyncio
import json
import logging
import os
import signal
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import websockets
from websockets import serve as ws_serve

import netinfo
import racebox
from laptimer import LapTimer, line_from_point

# ── GPIO setup ──────────────────────────────────────────────────────────────
try:
    from gpiozero import Button
    GPIO_AVAILABLE = True
except ImportError:
    GPIO_AVAILABLE = False
    logging.warning("gpiozero not available — running in simulation mode")

BUTTON_PINS = {
    17: "BOX_REQUEST",
    27: "FUEL_LOW",
    22: "ALL_GOOD",
    23: "ACKNOWLEDGE",
}

# ── Config ───────────────────────────────────────────────────────────────────
RELAY_URL      = os.environ.get("RELAY_URL", "wss://littlefoot-race-comms-production.up.railway.app")
LOCAL_WS_PORT  = 8765
LOCAL_HTTP_PORT = 8080
PI_DIR = Path(__file__).parent

RACEBOX_MODE   = os.environ.get("RACEBOX_MODE", "ble")   # ble | sim | off
TELEMETRY_HZ   = 5                                        # relay rate; lap timing uses full 25 Hz
START_LINE_FILE = PI_DIR / "startline.json"
RACEBOX_FILE    = PI_DIR / "racebox.json"                # RaceBox picked on the setup screen
MIN_SPEED_FOR_LINE_KPH = 20
# Changes whenever cockpit.html is redeployed; the open kiosk page reloads itself when it sees a new value
_ck = PI_DIR / "cockpit.html"
COCKPIT_BUILD = f"{int(_ck.stat().st_mtime)}-{_ck.stat().st_size}" if _ck.exists() else "dev"                               # heading is only trustworthy when moving

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("littlefoot")


# ─────────────────────────────────────────────────────────────────────────────
# Shared state
# ─────────────────────────────────────────────────────────────────────────────
class State:
    current_msg: dict = {"type": "pit_message", "code": "STANDBY", "text": "Standing by", "sub": ""}
    local_clients: set = set()
    relay_send_queue: asyncio.Queue = None  # set in main() — reliable (buttons, laps), retried
    live_queue: asyncio.Queue = None        # set in main() — latest-wins telemetry, never retried
    laptimer: LapTimer = None
    last_sample: dict = None
    racebox_status: dict = {"type": "racebox_status", "connected": False, "state": "off"}
    relay_connected: bool = False
    racebox_t: asyncio.Task = None
    last_sample_t: float = 0.0

state = State()


# ─────────────────────────────────────────────────────────────────────────────
# Local WebSocket server (cockpit.html connects here)
# ─────────────────────────────────────────────────────────────────────────────
async def local_ws_handler(websocket):
    state.local_clients.add(websocket)
    log.info("cockpit connected")
    try:
        await websocket.send(json.dumps({"type": "hello", "build": COCKPIT_BUILD}))
        await websocket.send(json.dumps(state.current_msg))
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            asyncio.create_task(handle_cockpit_cmd(websocket, msg))
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        state.local_clients.discard(websocket)
        log.info("cockpit disconnected")


async def broadcast_to_cockpit(msg: dict):
    state.current_msg = msg
    if not state.local_clients:
        return
    data = json.dumps(msg)
    dead = set()
    for ws in state.local_clients:
        try:
            await ws.send(data)
        except Exception:
            dead.add(ws)
    state.local_clients -= dead


async def send_cockpit(msg: dict) -> int:
    """Send to every cockpit screen without changing the displayed pit message. Returns how many got it."""
    data, sent = json.dumps(msg), 0
    for ws in list(state.local_clients):
        try:
            await ws.send(data)
            sent += 1
        except Exception:
            state.local_clients.discard(ws)
    return sent


# ─────────────────────────────────────────────────────────────────────────────
# Local HTTP server (serves cockpit.html)
# ─────────────────────────────────────────────────────────────────────────────
class SilentHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PI_DIR), **kwargs)

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")   # always serve the freshly deployed page
        super().end_headers()


def start_http_server():
    server = HTTPServer(("0.0.0.0", LOCAL_HTTP_PORT), SilentHandler)
    log.info(f"HTTP server on :{LOCAL_HTTP_PORT}")
    server.serve_forever()


# ─────────────────────────────────────────────────────────────────────────────
# GPIO buttons via gpiozero
# ─────────────────────────────────────────────────────────────────────────────
def setup_buttons(loop: asyncio.AbstractEventLoop):
    if not GPIO_AVAILABLE:
        log.info("Simulation mode — no GPIO buttons")
        return []

    buttons = []
    for pin, code in BUTTON_PINS.items():
        btn = Button(pin, pull_up=True, bounce_time=0.05)

        def make_handler(c):
            def handler():
                import time
                log.info(f"Button pressed → {c}")
                msg = {"type": "button_press", "code": c, "ts": int(time.time() * 1000)}
                loop.call_soon_threadsafe(state.relay_send_queue.put_nowait, msg)
            return handler

        btn.when_pressed = make_handler(code)
        buttons.append(btn)
        log.info(f"GPIO {pin} → {code}")

    log.info("GPIO buttons ready")
    return buttons  # keep refs alive


# ─────────────────────────────────────────────────────────────────────────────
# Maintenance combos (hold two buttons together)
#   ALL GOOD + ACKNOWLEDGE, 3 s  → exit kiosk to the desktop (Wi-Fi, terminal, etc.)
#   FUEL LOW + ACKNOWLEDGE, 5 s  → safe shutdown (protects the SD card)
# BOX REQUEST is never part of a combo so a real box call can't trigger one.
# ─────────────────────────────────────────────────────────────────────────────
NOKIOSK_FLAG = Path("/run/littlefoot/nokiosk")
COMBOS = [
    ({"ALL_GOOD", "ACKNOWLEDGE"}, 3.0, "exit_kiosk"),
    ({"FUEL_LOW", "ACKNOWLEDGE"}, 5.0, "shutdown"),
]


async def exit_kiosk():
    NOKIOSK_FLAG.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(NOKIOSK_FLAG.parent, 0o1777)   # let the desktop user clear it with --resume
    NOKIOSK_FLAG.touch()
    await broadcast_to_cockpit({"type": "pit_message", "code": "STANDBY",
                                "text": "Maintenance mode", "sub": "Exiting to desktop"})
    await asyncio.sleep(1.5)
    await (await asyncio.create_subprocess_exec("pkill", "-f", "chromium")).wait()
    log.warning("Maintenance mode: kiosk closed")


async def safe_shutdown():
    await broadcast_to_cockpit({"type": "pit_message", "code": "STANDBY",
                                "text": "Shutting down", "sub": "Wait for the green light to stop"})
    await asyncio.sleep(2)
    log.warning("Shutdown requested from buttons")
    await asyncio.create_subprocess_exec("systemctl", "poweroff")


async def combo_watcher(buttons):
    if not buttons:
        return
    by_code = dict(zip(BUTTON_PINS.values(), buttons))
    held_since = {}
    fired = set()
    while True:
        pressed = {c for c, b in by_code.items() if b.is_pressed}
        now = time.monotonic()
        for keys, hold_s, action in COMBOS:
            if keys <= pressed:
                start = held_since.setdefault(action, now)
                if now - start >= hold_s and action not in fired:
                    fired.add(action)
                    await (exit_kiosk() if action == "exit_kiosk" else safe_shutdown())
            else:
                held_since.pop(action, None)
                fired.discard(action)
        await asyncio.sleep(0.1)


# ─────────────────────────────────────────────────────────────────────────────
# Railway relay connection
# ─────────────────────────────────────────────────────────────────────────────
async def relay_client():
    backoff = 2
    while True:
        try:
            log.info(f"Connecting to relay: {RELAY_URL}")
            async with websockets.connect(
                RELAY_URL,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
            ) as ws:
                backoff = 2
                await ws.send(json.dumps({"type": "register", "role": "car"}))
                log.info("Registered with relay as 'car'")
                state.relay_connected = True
                # resync pit with current RaceBox / lap state
                await ws.send(json.dumps(state.racebox_status))
                if state.laptimer:
                    await ws.send(json.dumps(state.laptimer.snapshot()))

                async def sender():
                    while True:
                        msg = await state.relay_send_queue.get()
                        try:
                            await ws.send(json.dumps(msg))
                            log.info(f"→ relay: {msg.get('code') or msg['type']}")
                        except Exception as e:
                            log.warning(f"Send failed: {e}")
                            await state.relay_send_queue.put(msg)
                            break

                async def live_sender():
                    while True:
                        msg = await state.live_queue.get()
                        await ws.send(json.dumps(msg))   # dropped, not retried, on failure

                recv_task   = asyncio.create_task(receive_loop(ws))
                sender_task = asyncio.create_task(sender())
                live_task   = asyncio.create_task(live_sender())
                done, pending = await asyncio.wait(
                    [recv_task, sender_task, live_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                state.relay_connected = False

        except Exception as e:
            state.relay_connected = False
            log.warning(f"Relay error: {e} — retrying in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def receive_loop(ws):
    async for raw in ws:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if msg.get("type") == "pit_message":
            log.info(f"← relay: {msg.get('code')}")
            await broadcast_to_cockpit(msg)
        elif msg.get("type") == "set_start_line":
            set_start_line_here()
        elif msg.get("type") == "clear_start_line":
            set_line(None)
        elif msg.get("type") == "reset_drive_timer":
            n = await send_cockpit({"type": "reset_drive_timer"})
            log.info(f"Drive timer reset from pit ({n} cockpit screen(s))")
            publish({"type": "drive_timer_reset", "ok": n > 0,
                     "reason": "" if n else "Cockpit screen not connected"})
        elif msg.get("type") == "reset_laps":
            state.laptimer.reset_laps()
            publish(state.laptimer.snapshot())


# ─────────────────────────────────────────────────────────────────────────────
# RaceBox telemetry + lap timing
# ─────────────────────────────────────────────────────────────────────────────
def publish(msg: dict):
    """Reliable send to pit (retried on reconnect)."""
    state.relay_send_queue.put_nowait(msg)


def publish_live(msg: dict):
    """Latest-wins send to pit: if the previous sample hasn't gone out, replace it."""
    q = state.live_queue
    if q.full():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
    q.put_nowait(msg)


def load_start_line():
    try:
        return json.loads(START_LINE_FILE.read_text())
    except (OSError, ValueError):
        return None


def set_line(line):
    state.laptimer.set_line(line)
    try:
        if line:
            START_LINE_FILE.write_text(json.dumps(line))
        else:
            START_LINE_FILE.unlink(missing_ok=True)
    except OSError as e:
        log.warning(f"Could not persist start line: {e}")
    log.info("Start line %s", "set" if line else "cleared")
    publish(state.laptimer.snapshot())


def set_start_line_here():
    """Pit asked: put the start/finish line where the car is now, across its direction of travel."""
    s = state.last_sample
    if not s or not s["fix_ok"]:
        publish({"type": "line_result", "ok": False, "reason": "No GPS fix"})
    elif s["speed_kph"] < MIN_SPEED_FOR_LINE_KPH:
        publish({"type": "line_result", "ok": False,
                 "reason": f"Car must be moving (>{MIN_SPEED_FOR_LINE_KPH} km/h) to set direction"})
    else:
        set_line(line_from_point(s["lat"], s["lon"], s["heading"]))
        publish({"type": "line_result", "ok": True, "reason": ""})


def on_racebox_status(status: dict):
    state.racebox_status = {"type": "racebox_status", **status}
    publish(state.racebox_status)


_last_tele = 0.0


def on_racebox_sample(s: dict):
    global _last_tele
    state.last_sample = s
    state.last_sample_t = time.monotonic()
    lt = state.laptimer

    lap = lt.update(s)                          # full-rate lap detection
    if lap:
        log.info("Lap %d: %.3f%s", lap["n"], lap["ms"] / 1000, "  (best)" if lap["best"] else "")
        publish({"type": "lap", **lap, "ts": int(time.time() * 1000)})
        publish(lt.snapshot())

    now = time.monotonic()
    if now - _last_tele < 1.0 / TELEMETRY_HZ:
        return
    _last_tele = now
    publish_live({
        "type": "telemetry",
        "ts": int(time.time() * 1000),
        "speed_kph": round(s["speed_kph"], 1),
        "lat": round(s["lat"], 6), "lon": round(s["lon"], 6),
        "heading": round(s["heading"]),
        "gx": round(s["gx"], 2), "gy": round(s["gy"], 2), "gz": round(s["gz"], 2),
        "rot_z": round(s["rot_z"], 1),
        "fix": s["fix_ok"], "sats": s["sats"], "h_acc": round(s["h_acc_m"], 1),
        "batt": s["batt_pct"], "charging": s["charging"],
        "lap_n": len(lt.laps) + 1 if lt.lap_start_ms is not None else None,
        "lap_ms": lt.current_lap_ms(s["itow_ms"]),
    })


async def racebox_task():
    if RACEBOX_MODE == "sim":
        log.info("RaceBox SIMULATOR")
        if not state.laptimer.line:      # preset a line on the sim's front straight
            lat, lon, hdg = racebox._sim_track_point(0.02)
            state.laptimer.set_line(line_from_point(lat, lon, hdg))
        await racebox.simulate(on_racebox_sample, lap_s=float(os.environ.get("RACEBOX_SIM_LAP_S", 45)),
                               on_status=on_racebox_status)
    else:
        try:
            import bleak  # noqa: F401
        except ImportError:
            log.error("bleak not installed (pip3 install bleak) — RaceBox disabled")
            on_racebox_status({"connected": False, "state": "off"})
            return
        await racebox.RaceBoxBLE(on_racebox_sample, on_racebox_status, address=chosen_racebox()).run()


# ─────────────────────────────────────────────────────────────────────────────
# RaceBox selection (setup screen)
# ─────────────────────────────────────────────────────────────────────────────
def chosen_racebox():
    """Address picked on the setup screen, else RACEBOX_ADDRESS, else None (first RaceBox found)."""
    try:
        return json.loads(RACEBOX_FILE.read_text()).get("address") or None
    except (OSError, ValueError):
        return os.environ.get("RACEBOX_ADDRESS") or None


def start_racebox():
    if RACEBOX_MODE != "off":
        state.racebox_t = asyncio.create_task(racebox_task())


async def stop_racebox():
    t, state.racebox_t = state.racebox_t, None
    if t:
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass


async def racebox_scan(seconds=8.0):
    """Pause the RaceBox link, list nearby RaceBoxes, resume. A connected unit doesn't advertise,
    so the current one is added from the live status."""
    if RACEBOX_MODE == "sim":
        return [{"address": "SIM", "name": "RaceBox SIM", "rssi": -40}]
    try:
        from bleak import BleakScanner
    except ImportError:
        return []
    await stop_racebox()
    try:
        found = await BleakScanner.discover(timeout=seconds, return_adv=True)
        out = []
        for addr, (dev, adv) in found.items():
            name = dev.name or adv.local_name or ""
            if name.startswith(racebox.NAME_PREFIXES):
                out.append({"address": addr, "name": name, "rssi": adv.rssi})
        out.sort(key=lambda d: -d["rssi"])
        held = await racebox.bluez_connected_racebox()   # paired at OS level → not advertising
        if held and not any(d["address"] == held[0] for d in out):
            out.insert(0, {"address": held[0], "name": held[1], "rssi": None})
        return out
    finally:
        start_racebox()


async def racebox_select(address):
    try:
        if address:
            RACEBOX_FILE.write_text(json.dumps({"address": address}))
        else:
            RACEBOX_FILE.unlink(missing_ok=True)
    except OSError as e:
        log.warning(f"Could not save RaceBox choice: {e}")
    log.info(f"RaceBox selection → {address or 'auto (first found)'}")
    await stop_racebox()
    start_racebox()


# ─────────────────────────────────────────────────────────────────────────────
# Cockpit setup screen commands (local WS only — never from the relay)
# ─────────────────────────────────────────────────────────────────────────────
async def sys_status() -> dict:
    s = state.last_sample
    fresh = s is not None and time.monotonic() - state.last_sample_t < 3
    return {
        "type": "sys_status",
        "hostname": netinfo.hostname(),
        "wifi": await netinfo.wifi_status(),
        "relay": {"connected": state.relay_connected, "url": RELAY_URL},
        "racebox": {
            **{k: v for k, v in state.racebox_status.items() if k != "type"},
            "mode": RACEBOX_MODE,
            "chosen": chosen_racebox(),
            "sample": {k: s[k] for k in ("fix_ok", "sats", "batt_pct", "charging", "speed_kph", "h_acc_m")}
                      if fresh else None,
        },
        "laps": bool(state.laptimer and state.laptimer.snapshot().get("line")),
    }


async def handle_cockpit_cmd(ws, msg: dict):
    async def reply(m):
        try:
            await ws.send(json.dumps(m))
        except Exception:
            pass

    t = msg.get("type")
    try:
        if t == "sys_status_req":
            await reply(await sys_status())
        elif t == "wifi_scan":
            await reply({"type": "wifi_list", "networks": await netinfo.wifi_scan()})
        elif t == "wifi_connect":
            ok, detail = await netinfo.wifi_connect(msg.get("ssid", ""), msg.get("password") or None)
            log.info(f"Wi-Fi connect {msg.get('ssid')!r}: {'ok' if ok else detail}")
            await reply({"type": "wifi_result", "ok": ok, "ssid": msg.get("ssid"), "detail": detail})
        elif t == "racebox_scan":
            await reply({"type": "racebox_list", "devices": await racebox_scan()})
        elif t == "racebox_select":
            await racebox_select(msg.get("address"))
            await reply({"type": "racebox_selected", "address": msg.get("address")})
        elif t == "exit_kiosk":
            await exit_kiosk()
        elif t == "shutdown":
            await safe_shutdown()
    except Exception as e:
        log.warning(f"cockpit command {t} failed: {e}")
        await reply({"type": "cmd_error", "cmd": t, "error": str(e)})


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
async def main():
    state.relay_send_queue = asyncio.Queue()
    state.live_queue = asyncio.Queue(maxsize=1)
    state.laptimer = LapTimer(load_start_line())
    loop = asyncio.get_running_loop()

    # GPIO — keep button objects alive in this scope
    buttons = setup_buttons(loop)
    combo_t = asyncio.create_task(combo_watcher(buttons))

    # HTTP server thread
    http_thread = threading.Thread(target=start_http_server, daemon=True)
    http_thread.start()

    # Local WebSocket server
    local_ws = await ws_serve(local_ws_handler, "0.0.0.0", LOCAL_WS_PORT)
    log.info(f"Local WS server on :{LOCAL_WS_PORT}")

    # Railway relay
    relay_task = asyncio.create_task(relay_client())

    # RaceBox
    start_racebox()

    # Graceful shutdown
    stop = loop.create_future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set_result, None)

    await stop
    log.info("Shutting down…")
    relay_task.cancel()
    combo_t.cancel()
    if state.racebox_t:
        state.racebox_t.cancel()
    local_ws.close()
    await local_ws.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
