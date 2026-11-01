"""
Wi-Fi / system info for the cockpit setup screen, via NetworkManager's nmcli.
pi_main.py runs as root, so nmcli can scan and connect without polkit prompts.
"""

import asyncio
import re
import socket

_SPLIT = re.compile(r"(?<!\\):")


def _fields(line: str):
    """Split one `nmcli -t` line on unescaped colons and unescape the parts."""
    return [p.replace("\\:", ":").replace("\\\\", "\\") for p in _SPLIT.split(line)]


async def _run(*args, timeout=20):
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        return proc.returncode, out.decode(errors="replace").strip()
    except FileNotFoundError:
        return 127, f"{args[0]} not found"
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timed out"


async def wifi_status() -> dict:
    """Current Wi-Fi link: {connected, ssid, signal, ip, iface}."""
    st = {"connected": False, "ssid": None, "signal": None, "ip": None, "iface": None}
    rc, out = await _run("nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device")
    if rc != 0:
        st["error"] = out
        return st
    for line in out.splitlines():
        dev, typ, state, conn = (_fields(line) + [""] * 4)[:4]
        if typ == "wifi":
            st["iface"] = dev
            if state == "connected":
                st["connected"] = True
                st["ssid"] = conn
            break
    if st["iface"]:
        rc, out = await _run("nmcli", "-t", "-f", "IP4.ADDRESS", "device", "show", st["iface"])
        for line in out.splitlines():
            if line.startswith("IP4.ADDRESS"):
                st["ip"] = line.split(":", 1)[1].split("/")[0]
                break
    if st["connected"]:
        rc, out = await _run("nmcli", "-t", "-f", "ACTIVE,SIGNAL,SSID", "device", "wifi", "list",
                             "--rescan", "no")
        for line in out.splitlines():
            f = _fields(line)
            if len(f) >= 3 and f[0] == "yes":
                st["signal"] = int(f[1] or 0)
                st["ssid"] = f[2] or st["ssid"]
                break
    return st


async def saved_networks() -> set:
    rc, out = await _run("nmcli", "-t", "-f", "NAME,TYPE", "connection", "show")
    return {f[0] for f in map(_fields, out.splitlines()) if len(f) >= 2 and "wireless" in f[1]}


async def wifi_scan() -> list:
    """Nearby networks, strongest first: [{ssid, signal, secure, saved, active}]."""
    rc, out = await _run("nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL,SECURITY", "device", "wifi",
                         "list", "--rescan", "yes", timeout=30)
    saved = await saved_networks()
    nets = {}
    for line in out.splitlines() if rc == 0 else []:
        f = _fields(line)
        if len(f) < 4 or not f[1]:
            continue            # hidden network
        ssid, sig = f[1], int(f[2] or 0)
        cur = nets.get(ssid)
        if cur is None or sig > cur["signal"] or f[0] == "yes":
            nets[ssid] = {"ssid": ssid, "signal": sig, "secure": f[3] not in ("", "--"),
                          "saved": ssid in saved, "active": f[0] == "yes" or bool(cur and cur["active"])}
    return sorted(nets.values(), key=lambda n: (not n["active"], -n["signal"]))


async def wifi_connect(ssid: str, password: str = None) -> tuple:
    """Join a network. Saved networks reconnect with their stored password."""
    was_saved = ssid in await saved_networks()
    if not password and was_saved:
        rc, out = await _run("nmcli", "connection", "up", "id", ssid, timeout=45)
    else:
        args = ["nmcli", "device", "wifi", "connect", ssid]
        if password:
            args += ["password", password]
        rc, out = await _run(*args, timeout=45)
        if rc != 0 and not was_saved:
            await _run("nmcli", "connection", "delete", "id", ssid)   # don't keep a bad profile
    return rc == 0, out.splitlines()[-1] if out else ""


def hostname() -> str:
    return socket.gethostname()
