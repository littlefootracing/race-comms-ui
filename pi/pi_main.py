#!/usr/bin/env python3
"""
Littlefoot Racing — Pi Main
- Connects to Railway WebSocket relay as "car"
- Reads 4 GPIO buttons via gpiozero and sends button_press messages
- Runs a local WebSocket server on port 8765 for cockpit.html
- Runs a local HTTP server on port 8080 serving cockpit.html
"""

import asyncio
import json
import logging
import os
import signal
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import websockets
from websockets import serve as ws_serve

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
    relay_send_queue: asyncio.Queue = None  # set in main()

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

                async def sender():
                    while True:
                        msg = await state.relay_send_queue.get()
                        try:
                            await ws.send(json.dumps(msg))
                            log.info(f"→ relay: {msg['code']}")
                        except Exception as e:
                            log.warning(f"Send failed: {e}")
                            await state.relay_send_queue.put(msg)
                            break

                recv_task   = asyncio.create_task(receive_loop(ws))
                sender_task = asyncio.create_task(sender())
                done, pending = await asyncio.wait(
                    [recv_task, sender_task],
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


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
async def main():
    state.relay_send_queue = asyncio.Queue()
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

    # Graceful shutdown
    stop = loop.create_future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set_result, None)

    await stop
    log.info("Shutting down…")
    relay_task.cancel()
    local_ws.close()
    await local_ws.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
