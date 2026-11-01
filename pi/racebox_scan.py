#!/usr/bin/env python3
"""
RaceBox bench test — run this on the Pi with the RaceBox switched on next to it.

  python3 racebox_scan.py            scan, list everything nearby, then stream live data
  python3 racebox_scan.py --list     scan only
  python3 racebox_scan.py AA:BB:..   stream from a specific device

No OS-level Bluetooth pairing is needed (don't pair it in the desktop Bluetooth menu).
The RaceBox accepts ONE connection at a time: close the RaceBox app on your phone first.
Stop the cockpit service while testing so it isn't holding the connection:
  sudo systemctl stop cockpit.service
"""

import asyncio
import sys
import time

import racebox


async def scan():
    from bleak import BleakScanner
    print("Scanning 10 s for Bluetooth LE devices…")
    found = await BleakScanner.discover(timeout=10, return_adv=True)
    rb = []
    for addr, (dev, adv) in sorted(found.items(), key=lambda kv: -kv[1][1].rssi):
        name = dev.name or adv.local_name or ""
        is_rb = name.startswith(racebox.NAME_PREFIXES)
        if is_rb:
            rb.append(dev)
        if is_rb or name:
            print(f"  {'>>' if is_rb else '  '} {addr}  {adv.rssi:>4} dBm  {name}")
    print(f"\n{len(found)} devices seen, {len(rb)} RaceBox.")
    if not rb:
        print("No RaceBox found. Check: it's on (LED blinking), phone app closed, "
              "Pi Bluetooth up (`bluetoothctl show` → Powered: yes; `sudo rfkill unblock bluetooth`).")
    return rb


async def stream(address):
    last = [0.0]
    count = [0]

    def on_sample(s):
        count[0] += 1
        now = time.monotonic()
        if now - last[0] < 0.5:     # print 2x/s; device sends 25 Hz
            return
        last[0] = now
        fix = "FIX" if s["fix_ok"] else "no fix"
        print(f"{fix:6} sats={s['sats']:2}  {s['lat']:.6f},{s['lon']:.6f}  "
              f"{s['speed_kph']:5.1f} km/h  hdg={s['heading']:5.1f}  "
              f"G lon={s['gx']:+.2f} lat={s['gy']:+.2f}  batt={s['batt_pct']}%"
              f"{' chg' if s['charging'] else ''}  [{count[0]} pkts]")

    def on_status(st):
        print(f"-- {st.get('state')} {st.get('name', '')}")

    print("Streaming — Ctrl+C to stop. GPS fix can take 30–60 s outdoors, much longer indoors.")
    await racebox.RaceBoxBLE(on_sample, on_status, address=address).run()


async def main():
    args = sys.argv[1:]
    if args and args[0] != "--list":
        await stream(args[0])
        return
    rb = await scan()
    if rb and "--list" not in args:
        print(f"\nTo always use this unit, add to cockpit.service:\n"
              f"  Environment=RACEBOX_ADDRESS={rb[0].address}\n")
        await stream(rb[0].address)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
