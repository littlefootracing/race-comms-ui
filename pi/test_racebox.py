"""Run: python3 -m unittest pi/test_racebox.py   (from repo root; no hardware or deps needed)"""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import racebox
import laptimer

# Example data packet from the RaceBox BLE Protocol rev 9 doc (page 7)
DOC_PACKET = bytes.fromhex(
    "B5 62 FF 01 50 00 A0 E7 0C 07 E6 07 01 0A 08 33 08 37 19 00 00 00 2A AD 4D 0E 03 01 EA 0B C6 93 "
    "E1 0D 3B 37 6F 19 61 8C 09 00 0F 01 09 00 9C 03 00 00 2C 07 00 00 23 00 00 00 00 00 00 00 D0 00 "
    "00 00 88 A9 DD 00 2C 01 00 59 FD FF 71 00 CE 03 2F FF 56 00 FC FF 06 DB"
)


class ProtocolTests(unittest.TestCase):
    def test_doc_packet_frames_and_decodes(self):
        out = racebox.Framer().feed(DOC_PACKET)
        self.assertEqual(len(out), 1)
        cls, mid, payload = out[0]
        self.assertEqual((cls, mid, len(payload)), (0xFF, 0x01, 80))
        d = racebox.decode_data(payload)
        self.assertEqual(d["itow_ms"], 118286240)
        self.assertEqual(d["utc"][:6], (2022, 1, 10, 8, 51, 8))
        self.assertTrue(d["fix_ok"])
        self.assertEqual(d["sats"], 11)
        self.assertAlmostEqual(d["lon"], 23.2887238, places=7)
        self.assertAlmostEqual(d["lat"], 42.6719035, places=7)
        self.assertAlmostEqual(d["alt_m"], 590.095, places=3)
        self.assertAlmostEqual(d["speed_kph"], 0.126, places=3)
        self.assertEqual(d["batt_pct"], 89)
        self.assertFalse(d["charging"])
        self.assertAlmostEqual(d["gx"], -0.003)
        self.assertAlmostEqual(d["gy"], 0.113)
        self.assertAlmostEqual(d["gz"], 0.974)
        self.assertAlmostEqual(d["rot_x"], -2.09)
        self.assertAlmostEqual(d["rot_y"], 0.86)
        self.assertAlmostEqual(d["rot_z"], -0.04)

    def test_split_notifications_reassemble(self):
        f = racebox.Framer()
        got = []
        for i in range(0, len(DOC_PACKET), 7):          # tiny MTU
            got += f.feed(DOC_PACKET[i:i + 7])
        self.assertEqual(len(got), 1)

    def test_multiple_packets_and_garbage(self):
        f = racebox.Framer()
        got = f.feed(b"\x00\xb5" + DOC_PACKET + DOC_PACKET[:30])
        got += f.feed(DOC_PACKET[30:])
        self.assertEqual(len(got), 2)

    def test_bad_checksum_skipped_then_recovers(self):
        bad = bytearray(DOC_PACKET)
        bad[-1] ^= 0xFF
        f = racebox.Framer()
        got = f.feed(bytes(bad) + DOC_PACKET)
        self.assertEqual(len(got), 1)
        self.assertEqual(f.bad_checksums, 1)

    def test_command_packets_match_doc(self):
        self.assertEqual(racebox.cmd_recording_status().hex(" "), "b5 62 ff 22 00 00 21 62")
        self.assertEqual(racebox.cmd_unlock(123456).hex(" "), "b5 62 ff 30 04 00 40 e2 01 00 56 08")
        self.assertEqual(
            racebox.cmd_recording(True).hex(" "),
            "b5 62 ff 25 0c 00 01 00 1f 00 6d 05 1e 00 1e 00 2c 01 2b 15")

    def test_encode_decode_roundtrip(self):
        pkt = racebox.encode_data(27.45, -81.35, 120.0, 271.5, 5000, gx=0.5, gy=-1.2, rot_z=-12.5)
        (_, _, payload), = racebox.Framer().feed(pkt)
        d = racebox.decode_data(payload)
        self.assertAlmostEqual(d["lat"], 27.45, places=6)
        self.assertAlmostEqual(d["speed_kph"], 120.0, places=1)
        self.assertAlmostEqual(d["heading"], 271.5, places=3)
        self.assertAlmostEqual(d["gy"], -1.2)
        self.assertTrue(d["fix_ok"])


def sim_samples(lap_s, laps, hz=25):
    """Samples straight from the simulator's track model."""
    n = int(lap_s * hz * laps)
    for i in range(n):
        t = i / hz
        lat, lon, hdg = racebox._sim_track_point((t / lap_s) % 1.0)
        pkt = racebox.encode_data(lat, lon, 100.0, hdg, itow_ms=1_000_000 + int(t * 1000))
        (_, _, payload), = racebox.Framer().feed(pkt)
        yield racebox.decode_data(payload)


class LapTimerTests(unittest.TestCase):
    def setUp(self):
        lat, lon, hdg = racebox._sim_track_point(0.02)
        self.line = laptimer.line_from_point(lat, lon, hdg)

    def test_laps_are_timed_accurately(self):
        lt = laptimer.LapTimer(self.line)
        laps = [l for s in sim_samples(45.0, 4) if (l := lt.update(s))]
        self.assertEqual(len(laps), 3)               # first crossing only starts the clock
        for l in laps:
            self.assertAlmostEqual(l["ms"], 45000, delta=60)   # well inside one 40 ms sample
        self.assertTrue(laps[0]["best"])

    def test_no_line_no_laps(self):
        lt = laptimer.LapTimer(None)
        self.assertEqual([l for s in sim_samples(45.0, 2) if (l := lt.update(s))], [])

    def test_wrong_direction_ignored(self):
        flipped = dict(self.line, heading=(self.line["heading"] + 180) % 360)
        lt = laptimer.LapTimer(flipped)
        self.assertEqual([l for s in sim_samples(45.0, 3) if (l := lt.update(s))], [])

    def test_min_lap_filters_double_trigger(self):
        # min lap longer than the real lap: the 45 s crossing is ignored (clock keeps
        # running), so the next crossing at 90 s is the first valid lap
        lt = laptimer.LapTimer(self.line, min_lap_s=60)
        laps = [l for s in sim_samples(45.0, 3) if (l := lt.update(s))]
        self.assertEqual(len(laps), 1)
        self.assertAlmostEqual(laps[0]["ms"], 90000, delta=60)

    def test_no_fix_ignored(self):
        lt = laptimer.LapTimer(self.line)
        for s in sim_samples(45.0, 2):
            s["fix_ok"] = False
            self.assertIsNone(lt.update(s))


if __name__ == "__main__":
    unittest.main()
