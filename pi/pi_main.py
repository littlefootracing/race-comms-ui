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
MIN_SPEED_FOR_LINE_KPH = 20                               # heading is only trustworthy when moving

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

state = State()


# ─────────────────────────────────────────────────────────────────────────────
# Local WebSocket server (cockpit.html connects here)
# ─────────────────────────────────────────────────────────────────────────────
async def local_ws_handler(websocket):
    state.local_clients.add(websocket)
    log.info("cockpit connected")
    try:
        await websocket.send(json.dumps(state.current_msg))
        async for _ in websocket:
            pass
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


# ─────────────────────────────────────────────────────────────────────────────
# Local HTTP server (serves cockpit.html)
# ─────────────────────────────────────────────────────────────────────────────
class SilentHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PI_DIR), **kwargs)

    def log_message(self, fmt, *args):
        pass


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

        except Exception as e:
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
        await racebox.RaceBoxBLE(on_racebox_sample, on_racebox_status).run()


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

    # HTTP server thread
    http_thread = threading.Thread(target=start_http_server, daemon=True)
    http_thread.start()

    # Local WebSocket server
    local_ws = await ws_serve(local_ws_handler, "0.0.0.0", LOCAL_WS_PORT)
    log.info(f"Local WS server on :{LOCAL_WS_PORT}")

    # Railway relay
    relay_task = asyncio.create_task(relay_client())

    # RaceBox
    racebox_t = asyncio.create_task(racebox_task()) if RACEBOX_MODE != "off" else None

    # Graceful shutdown
    stop = loop.create_future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set_result, None)

    await stop
    log.info("Shutting down…")
    relay_task.cancel()
    if racebox_t:
        racebox_t.cancel()
    local_ws.close()
    await local_ws.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
