"""Limb 2 — HANDS & LEGS: the real (uncustomized) OpenManus, driven by LOCAL
Ollama (qwen2.5-coder:3b brain + granite3.2-vision:2b eyes) so NO API key is
needed. Its browser runs HEADED but pinned to the invisible Xvfb display :99, so
a window never touches the real desktop. This gives the twin a full autonomous
agent — it googles, opens sites, clicks, types and reads a page via a vision
model + a virtual mouse/keyboard instead of any paid API.

We shell out to OpenManus's OWN venv (its deps + Python live there) via its
`main.py --prompt` CLI, keeping the two environments clean."""
from __future__ import annotations

import asyncio
import os
import shutil

import config

HOME = config.OPENMANUS_HOME
VDISPLAY = os.getenv("BROWSER_DISPLAY", ":99")


def available() -> dict:
    py = HOME / ".venv" / "bin" / "python"
    return {
        "openmanus": HOME.is_dir() and (HOME / "main.py").exists(),
        "venv": py.exists(),
        "config": (HOME / "config" / "config.toml").exists(),
        "xvfb": shutil.which("Xvfb") is not None,
        "display": VDISPLAY,
    }


# OpenManus needs to hold a browser + (swapped) an Ollama model. On this 7GB
# laptop that only fits when heavy apps are closed. Refuse below this so we WARN
# instead of letting the kernel OOM-kill the user's apps.
_MIN_FREE_MB = int(os.getenv("OPENMANUS_MIN_FREE_MB", "2600"))


def _free_mb() -> int:
    try:
        for ln in open("/proc/meminfo"):
            if ln.startswith("MemAvailable:"):
                return int(ln.split()[1]) // 1024
    except Exception:  # noqa: BLE001
        pass
    return 0


def _ensure_display() -> bool:
    """Reuse the GUI limb's invisible display so nothing opens on the desktop."""
    try:
        import gui
        return gui._ensure_display()
    except Exception:  # noqa: BLE001
        return os.path.exists(f"/tmp/.X11-unix/X{VDISPLAY.lstrip(':')}")


def _clean(raw: str) -> str:
    """Strip OpenManus/browser-use logger noise, keep the agent's real thoughts
    and results so the Discord reply stays readable."""
    keep: list[str] = []
    for ln in raw.splitlines():
        s = ln.rstrip()
        if not s:
            continue
        low = s.lower()
        if any(t in low for t in (
            "unsupported python version", "browseruse logging", "anonymized telemetry",
            "token usage", "processing your request", "request processing completed",
            "| info", "| warning", "| error", "httpx", "requestsdependencywarning",
            "warnings.warn", "vaapi", "matplotlib", "roi:",
        )):
            continue
        keep.append(s)
    text = "\n".join(keep).strip()
    return text[-3500:] if text else raw.strip()[-1500:]


async def run(prompt: str, timeout: int = 300) -> dict:
    """Run one autonomous OpenManus task on the invisible display. Returns
    {ok, output, error}. Long-running — the bot should show a typing indicator."""
    main = HOME / "main.py"
    if not main.exists():
        return {"ok": False, "error": f"OpenManus main.py not found at {main}"}
    py = HOME / ".venv" / "bin" / "python"
    python_exe = str(py) if py.exists() else "python3"
    free = _free_mb()
    if free < _MIN_FREE_MB:
        return {"ok": False, "error": (
            f"not enough free RAM — {free}MB free, need ~{_MIN_FREE_MB}MB. "
            "OpenManus runs a browser + a model; close Firefox / other heavy apps "
            "and try again (or use the lighter !search / !read which fit anytime).")}
    if not _ensure_display():
        return {"ok": False, "error": "no virtual display (install xvfb)"}

    env = {
        **os.environ,
        "DISPLAY": VDISPLAY,               # browser → invisible :99
        "PYTHONUNBUFFERED": "1",
        "ANONYMIZED_TELEMETRY": "false",   # browser-use: no phone-home
    }
    try:
        proc = await asyncio.create_subprocess_exec(
            python_exe, "main.py", "--prompt", prompt,
            cwd=str(HOME), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"ok": False, "error": f"timed out after {timeout}s (local models are slow)"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:300]}

    raw = out.decode("utf-8", "replace")
    return {"ok": proc.returncode == 0, "output": _clean(raw),
            "error": None if proc.returncode == 0 else f"exit {proc.returncode}"}
