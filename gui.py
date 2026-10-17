"""GUI limb — a virtual mouse + keyboard the bot drives on the INVISIBLE virtual
display (:99), via xdotool + scrot (reliable, no python-display deps). Nothing
ever appears on the real desktop. Pair screenshot() with vision.analyze() for
see-then-act ('computer use'). Owner-gated at the bot layer."""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time

import config

_VDISPLAY = os.getenv("BROWSER_DISPLAY", ":99")
SHOTS = config.WORKSPACE / "outputs"
SHOTS.mkdir(parents=True, exist_ok=True)
_BTN = {"left": "1", "middle": "2", "right": "3"}


def _ensure_display() -> bool:
    if shutil.which("Xvfb") is None:
        return False
    num = _VDISPLAY.lstrip(":")
    if not os.path.exists(f"/tmp/.X11-unix/X{num}"):
        try:
            subprocess.Popen(
                ["Xvfb", _VDISPLAY, "-screen", "0", "1280x900x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            time.sleep(1.2)
        except Exception:  # noqa: BLE001
            return False
    return os.path.exists(f"/tmp/.X11-unix/X{num}")


async def _x(*args: str, timeout: int = 15) -> dict:
    """Run a command against the virtual display."""
    if not _ensure_display():
        return {"ok": False, "error": "no virtual display (install xvfb)"}
    env = {**os.environ, "DISPLAY": _VDISPLAY}
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:150]}
    return {"ok": proc.returncode == 0, "out": out.decode("utf-8", "replace").strip(),
            "err": err.decode("utf-8", "replace").strip()}


async def move(x: int, y: int) -> dict:
    r = await _x("xdotool", "mousemove", str(x), str(y))
    return {"ok": r["ok"], "at": [x, y]} if r["ok"] else r


async def click(x: int | None = None, y: int | None = None, button: str = "left") -> dict:
    if x is not None and y is not None:
        await _x("xdotool", "mousemove", str(x), str(y))
    r = await _x("xdotool", "click", _BTN.get(button, "1"))
    return {"ok": r["ok"], "clicked": [x, y], "button": button} if r["ok"] else r


async def type_text(text: str) -> dict:
    r = await _x("xdotool", "type", "--clearmodifiers", "--delay", "12", text)
    return {"ok": r["ok"], "typed": len(text)} if r["ok"] else r


async def press(key: str) -> dict:
    r = await _x("xdotool", "key", key)          # e.g. Return, Tab, ctrl+a, alt+F4
    return {"ok": r["ok"], "pressed": key} if r["ok"] else r


async def scroll(amount: int) -> dict:
    btn = "4" if amount > 0 else "5"             # 4 = up, 5 = down
    for _ in range(min(abs(amount), 20)):
        await _x("xdotool", "click", btn)
    return {"ok": True, "scrolled": amount}


async def screenshot(name: str = "gui") -> dict:
    if shutil.which("scrot") is None:
        return {"ok": False, "error": "scrot not installed"}
    dst = SHOTS / f"{name}.png"
    r = await _x("scrot", "-o", str(dst))
    return {"ok": r["ok"], "path": str(dst)} if r["ok"] else r


async def size() -> dict:
    r = await _x("xdotool", "getdisplaygeometry")
    if not r["ok"]:
        return r
    try:
        w, h = r["out"].split()
        return {"ok": True, "size": [int(w), int(h)]}
    except Exception:  # noqa: BLE001
        return {"ok": True, "raw": r["out"]}


async def launch(cmd: str) -> dict:
    """Launch a GUI app ON the virtual display (e.g. a browser) so the bot can
    then click/type in it. Detached; returns immediately."""
    if not _ensure_display():
        return {"ok": False, "error": "no virtual display"}
    try:
        subprocess.Popen(cmd, shell=True, env={**os.environ, "DISPLAY": _VDISPLAY},
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return {"ok": True, "launched": cmd}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:150]}


def available() -> dict:
    return {"xdotool": shutil.which("xdotool") is not None,
            "scrot": shutil.which("scrot") is not None,
            "xvfb": shutil.which("Xvfb") is not None, "display": _VDISPLAY}
