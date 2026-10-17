"""
RaceBox Mini S — BLE protocol (rev 9) + BLE client + simulator.

- Framer:  reassembles UBX-style packets from arbitrarily split BLE notifications
- decode_data(): 80-byte 0xFF 0x01 data message -> dict (SI-ish units)
- build_packet(): builds command packets (unlock, standalone recording, ...)
- RaceBoxBLE:  bleak client, auto-reconnect, calls on_sample(dict)
- simulate():  fake car lapping a loop at 25 Hz through the real framer/decoder
"""

import asyncio
import logging
import math
import os
import struct
from typing import Callable, Optional

log = logging.getLogger("littlefoot.racebox")

# ── UUIDs ────────────────────────────────────────────────────────────────────
UART_SERVICE = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
UART_RX      = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"  # write commands here
UART_TX      = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"  # notifications from device
NAME_PREFIXES = ("RaceBox Mini S", "RaceBox Micro", "RaceBox Mini")

# ── Message class/IDs ────────────────────────────────────────────────────────
CLS = 0xFF
ID_DATA       = 0x01
ID_ACK        = 0x02
ID_NACK       = 0x03
ID_HISTORY    = 0x21
ID_REC_STATUS = 0x22
ID_DOWNLOAD   = 0x23
ID_ERASE      = 0x24
ID_REC_CONFIG = 0x25
ID_REC_STATE  = 0x26
ID_GNSS_CFG   = 0x27
ID_UNLOCK     = 0x30

SYNC = b"\xb5\x62"
MAX_PAYLOAD = 504


def checksum(body: bytes) -> bytes:
    """Fletcher-8 over class, id, length and payload."""
    a = b = 0
    for byte in body:
        a = (a + byte) & 0xFF
        b = (b + a) & 0xFF
    return bytes((a, b))


def build_packet(msg_class: int, msg_id: int, payload: bytes = b"") -> bytes:
    body = bytes((msg_class, msg_id)) + struct.pack("<H", len(payload)) + payload
    return SYNC + body + checksum(body)


class Framer:
    """Byte FIFO that yields (class, id, payload) for each valid packet.

    BLE notifications may hold part of a packet or several packets; bad
    checksums and garbage are skipped by resyncing on the next 0xB5 0x62.
    """

    def __init__(self):
        self._buf = bytearray()
        self.bad_checksums = 0

    def feed(self, data: bytes):
        self._buf += data
        out = []
        buf = self._buf
        while True:
            i = buf.find(SYNC)
            if i < 0:
                # keep a trailing 0xB5 in case the sync byte pair is split
                del buf[: len(buf) - 1 if buf.endswith(b"\xb5") else len(buf)]
                break
            if i:
                del buf[:i]
            if len(buf) < 6:
                break
            length = buf[4] | (buf[5] << 8)
            if length > MAX_PAYLOAD:
                del buf[:2]
                continue
            total = 6 + length + 2
            if len(buf) < total:
                break
            body = bytes(buf[2 : 6 + length])
            if checksum(body) == bytes(buf[6 + length : total]):
                out.append((buf[2], buf[3], bytes(buf[6 : 6 + length])))
                del buf[:total]
            else:
                self.bad_checksums += 1
                del buf[:2]  # resync past this false start
        return out


# ── Data message ─────────────────────────────────────────────────────────────
_DATA_FMT = struct.Struct("<IHBBBBBBIiBBBBiiiiIIiiIIHBBhhhhhh")
assert _DATA_FMT.size == 80


def decode_data(p: bytes) -> dict:
    (itow, year, month, day, hour, minute, sec, validity, t_acc, nano,
     fix, fix_flags, dt_flags, sats, lon, lat, wgs_alt, msl_alt, h_acc, v_acc,
     speed, heading, s_acc, hd_acc, pdop, latlon_flags, batt,
     gx, gy, gz, rx, ry, rz) = _DATA_FMT.unpack(p)
    return {
        "itow_ms": itow,
        "utc": (year, month, day, hour, minute, sec, nano),
        "fix": fix,
        "fix_ok": fix == 3 and bool(fix_flags & 1) and not (latlon_flags & 1),
        "sats": sats,
        "lat": lat / 1e7,
        "lon": lon / 1e7,
        "alt_m": msl_alt / 1000.0,
        "h_acc_m": h_acc / 1000.0,
        "speed_kph": speed * 0.0036,          # mm/s -> km/h
        "heading": heading / 1e5,             # degrees, 0 = north
        "pdop": pdop / 100.0,
        "batt_pct": batt & 0x7F,
        "charging": bool(batt & 0x80),
        "gx": gx / 1000.0,                    # g, X = front/back
        "gy": gy / 1000.0,                    # g, Y = right/left
        "gz": gz / 1000.0,                    # g, Z = up/down
        "rot_x": rx / 100.0,                  # deg/s roll
        "rot_y": ry / 100.0,                  # deg/s pitch
        "rot_z": rz / 100.0,                  # deg/s yaw
    }


def encode_data(lat, lon, speed_kph, heading, itow_ms, gx=0.0, gy=0.0, gz=1.0,
                rot_z=0.0, sats=12, batt=90, fix_ok=True) -> bytes:
    """Build a full 0xFF 0x01 packet (used by the simulator and tests)."""
    payload = _DATA_FMT.pack(
        int(itow_ms) & 0xFFFFFFFF, 2026, 1, 1, 0, 0, 0, 0x37, 25, 0,
        3 if fix_ok else 0, 1 if fix_ok else 0, 0xEA, sats,
        int(round(lon * 1e7)), int(round(lat * 1e7)), 0, 0, 900, 1800,
        int(round(speed_kph / 0.0036)), int(round((heading % 360) * 1e5)), 200, 0,
        120, 0 if fix_ok else 1, batt,
        int(round(gx * 1000)), int(round(gy * 1000)), int(round(gz * 1000)),
        0, 0, int(round(rot_z * 100)),
    )
    return build_packet(CLS, ID_DATA, payload)


# ── Commands (Mini S) ────────────────────────────────────────────────────────
def cmd_unlock(code: int) -> bytes:
    return build_packet(CLS, ID_UNLOCK, struct.pack("<I", code))


def cmd_recording(enable: bool, rate: int = 0, wait_fix=True, stationary=True,
                  no_fix=True, auto_shutdown=True, wait_data=True,
                  speed_mms=1389, stationary_s=30, no_fix_s=30, shutdown_s=300) -> bytes:
    """Standalone recording config (rate 0 = 25 Hz). Defaults = RaceBox's recommended setup."""
    flags = (wait_fix | stationary << 1 | no_fix << 2 | auto_shutdown << 3 | wait_data << 4)
    return build_packet(CLS, ID_REC_CONFIG, struct.pack(
        "<BBBBHHHH", int(enable), rate, flags, 0, speed_mms, stationary_s, no_fix_s, shutdown_s))


def cmd_recording_status() -> bytes:
    return build_packet(CLS, ID_REC_STATUS)


# ── BLE client ───────────────────────────────────────────────────────────────
class RaceBoxBLE:
    """Connects to the first RaceBox found, streams decoded samples, reconnects."""

    def __init__(self, on_sample: Callable[[dict], None],
                 on_status: Optional[Callable[[dict], None]] = None,
                 address: Optional[str] = None):
        self.on_sample = on_sample
        self.on_status = on_status or (lambda s: None)
        self.address = address or os.environ.get("RACEBOX_ADDRESS")
        self.unlock_code = os.environ.get("RACEBOX_CODE")      # memory security code, if enabled
        self.record = os.environ.get("RACEBOX_RECORD") == "1"  # standalone recording backup

    async def run(self):
        from bleak import BleakClient, BleakScanner  # imported lazily: optional dep

        backoff = 2
        while True:
            try:
                self.on_status({"connected": False, "state": "scanning"})
                dev = await self._find(BleakScanner)
                if dev is None:
                    raise RuntimeError("no RaceBox found")
                log.info(f"RaceBox found: {dev.name} [{dev.address}]")
                framer = Framer()
                disconnected = asyncio.Event()

                def on_disc(_):
                    disconnected.set()

                def on_notify(_, data: bytearray):
                    for cls, mid, payload in framer.feed(bytes(data)):
                        if cls != CLS:
                            continue
                        if mid == ID_DATA and len(payload) == 80:
                            self.on_sample(decode_data(payload))
                        elif mid in (ID_ACK, ID_NACK):
                            log.info("RaceBox %s for 0x%02X 0x%02X",
                                     "ACK" if mid == ID_ACK else "NACK",
                                     payload[0] if payload else 0, payload[1] if len(payload) > 1 else 0)

                async with BleakClient(dev, disconnected_callback=on_disc) as client:
                    backoff = 2
                    await client.start_notify(UART_TX, on_notify)
                    self.on_status({"connected": True, "state": "streaming", "name": dev.name})
                    log.info("RaceBox streaming")
                    if self.unlock_code:
                        await client.write_gatt_char(UART_RX, cmd_unlock(int(self.unlock_code)), response=False)
                    if self.record:
                        await client.write_gatt_char(UART_RX, cmd_recording(True), response=False)
                        log.info("RaceBox standalone recording requested")
                    await disconnected.wait()
                log.warning("RaceBox disconnected")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"RaceBox error: {e} — retrying in {backoff}s")
            self.on_status({"connected": False, "state": "disconnected"})
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _find(self, BleakScanner):
        if self.address:
            return await BleakScanner.find_device_by_address(self.address, timeout=10)
        return await BleakScanner.find_device_by_filter(
            lambda d, ad: bool(d.name) and d.name.startswith(NAME_PREFIXES), timeout=10)


# ── Simulator ────────────────────────────────────────────────────────────────
# A ~1.3 km oval near Sebring-ish coordinates; start/finish is on the front straight.
SIM_CENTER = (27.4500, -81.3500)


def _sim_track_point(s: float):
    """s in [0,1) around the loop -> (lat, lon, heading_deg). Stadium oval."""
    straight, radius = 0.0040, 0.0012  # degrees lat/lon-ish; good enough for a fake track
    perim = 2 * straight + 2 * math.pi * radius
    d = (s % 1.0) * perim
    cy, cx = SIM_CENTER
    if d < straight:                                  # front straight, heading east
        return cy - radius, cx - straight / 2 + d, 90.0
    d -= straight
    arc = math.pi * radius
    if d < arc:                                       # right-hand turn, east -> west
        a = d / radius
        return (cy - radius * math.cos(a), cx + straight / 2 + radius * math.sin(a),
                90.0 + math.degrees(a))
    d -= arc
    if d < straight:                                  # back straight, heading west
        return cy + radius, cx + straight / 2 - d, 270.0
    d -= straight
    a = d / radius
    return (cy + radius * math.cos(a), cx - straight / 2 - radius * math.sin(a),
            270.0 + math.degrees(a))


async def simulate(on_sample: Callable[[dict], None], lap_s: float = 45.0, hz: int = 25,
                   on_status: Optional[Callable[[dict], None]] = None):
    """Emit samples as if from a RaceBox, via real packet encode -> Framer -> decode."""
    if on_status:
        on_status({"connected": True, "state": "simulated", "name": "RaceBox SIM"})
    framer = Framer()
    t = 0.0
    prev_heading = None
    while True:
        s = (t / lap_s) % 1.0
        lat, lon, hdg = _sim_track_point(s)
        # speed varies around the lap so G / speed traces look alive
        speed = 95 + 35 * math.sin(2 * math.pi * s * 2)
        yaw = 0.0 if prev_heading is None else ((hdg - prev_heading + 540) % 360 - 180) * hz
        prev_heading = hdg
        pkt = encode_data(lat, lon, speed, hdg, itow_ms=int(t * 1000) + 1_000_000,
                          gx=0.35 * math.cos(2 * math.pi * s * 2),
                          gy=min(max(yaw / 40, -1.5), 1.5), rot_z=yaw)
        for cls, mid, payload in framer.feed(pkt):
            if cls == CLS and mid == ID_DATA:
                on_sample(decode_data(payload))
        t += 1.0 / hz
        await asyncio.sleep(1.0 / hz)
