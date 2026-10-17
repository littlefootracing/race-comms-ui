"""
GPS lap timer — start/finish line crossing detection.

The line is a finite segment (two lat/lon points). A lap is recorded each time
the car's path crosses it in the intended direction. Crossing time is
interpolated between the two 25 Hz samples that straddle the line.
"""

import math
from typing import Optional

WEEK_MS = 7 * 24 * 3600 * 1000
M_PER_DEG_LAT = 110_540.0
M_PER_DEG_LON = 111_320.0


def line_from_point(lat: float, lon: float, heading_deg: float, half_width_m: float = 30.0) -> dict:
    """Line through (lat, lon) perpendicular to heading, crossed in the heading direction."""
    h = math.radians(heading_deg)
    # direction of travel (east, north) and its perpendicular
    de, dn = math.sin(h), math.cos(h)
    pe, pn = -dn, de
    dlat = pn * half_width_m / M_PER_DEG_LAT
    dlon = pe * half_width_m / (M_PER_DEG_LON * math.cos(math.radians(lat)))
    return {
        "a": [lat - dlat, lon - dlon],
        "b": [lat + dlat, lon + dlon],
        "heading": heading_deg % 360,
    }


class LapTimer:
    def __init__(self, line: Optional[dict] = None, min_lap_s: float = 20.0, min_speed_kph: float = 10.0):
        self.min_lap_ms = int(min_lap_s * 1000)
        self.min_speed_kph = min_speed_kph
        self.laps: list = []
        self.best_ms: Optional[int] = None
        self.lap_start_ms: Optional[float] = None   # itow of the last crossing
        self._prev = None                            # (x, y, itow) in line-local meters
        self.set_line(line)

    # ── line ────────────────────────────────────────────────────────────
    def set_line(self, line: Optional[dict]):
        self.line = line
        self.lap_start_ms = None
        self._prev = None
        if line:
            self._lat0, self._lon0 = line["a"]
            self._kx = M_PER_DEG_LON * math.cos(math.radians(self._lat0))
            ax, ay = self._xy(*line["a"])
            bx, by = self._xy(*line["b"])
            self._seg = (ax, ay, bx, by)
            h = math.radians(line["heading"])
            self._dir = (math.sin(h), math.cos(h))   # (east, north)

    def reset_laps(self):
        self.laps = []
        self.best_ms = None
        self.lap_start_ms = None

    def _xy(self, lat, lon):
        return (lon - self._lon0) * self._kx, (lat - self._lat0) * M_PER_DEG_LAT

    # ── per-sample update ───────────────────────────────────────────────
    def update(self, s: dict) -> Optional[dict]:
        """Feed one decoded sample. Returns a lap dict when a lap completes."""
        if not self.line or not s["fix_ok"]:
            self._prev = None
            return None
        x, y = self._xy(s["lat"], s["lon"])
        cur = (x, y, s["itow_ms"])
        prev, self._prev = self._prev, cur
        if prev is None or s["speed_kph"] < self.min_speed_kph:
            return None

        t_frac = self._cross_fraction(prev, cur)
        if t_frac is None:
            return None
        dt = cur[2] - prev[2]
        if dt < 0:
            dt += WEEK_MS
        cross_ms = prev[2] + t_frac * dt

        if self.lap_start_ms is None:
            self.lap_start_ms = cross_ms
            return None
        lap_ms = cross_ms - self.lap_start_ms
        if lap_ms < 0:
            lap_ms += WEEK_MS
        if lap_ms < self.min_lap_ms:
            return None   # double-trigger / wobble at the line

        self.lap_start_ms = cross_ms
        lap_ms = int(round(lap_ms))
        is_best = self.best_ms is None or lap_ms < self.best_ms
        if is_best:
            self.best_ms = lap_ms
        lap = {"n": len(self.laps) + 1, "ms": lap_ms, "best": is_best}
        self.laps.append(lap)
        return lap

    def _cross_fraction(self, p, q) -> Optional[float]:
        """Fraction along p->q where it crosses the line segment, or None."""
        ax, ay, bx, by = self._seg
        rx, ry = q[0] - p[0], q[1] - p[1]
        sx, sy = bx - ax, by - ay
        denom = rx * sy - ry * sx
        if denom == 0:
            return None
        t = ((ax - p[0]) * sy - (ay - p[1]) * sx) / denom   # along p->q
        u = ((ax - p[0]) * ry - (ay - p[1]) * rx) / denom   # along a->b
        if not (0.0 <= t < 1.0 and 0.0 <= u <= 1.0):
            return None
        if rx * self._dir[0] + ry * self._dir[1] <= 0:
            return None   # crossing the wrong way
        return t

    # ── helpers for telemetry ───────────────────────────────────────────
    def current_lap_ms(self, itow_ms: float) -> Optional[int]:
        if self.lap_start_ms is None:
            return None
        d = itow_ms - self.lap_start_ms
        if d < 0:
            d += WEEK_MS
        return int(d)

    def snapshot(self) -> dict:
        return {
            "type": "lap_state",
            "line": self.line,
            "laps": self.laps[-200:],
            "best_ms": self.best_ms,
        }
