"""System monitoring of the HOST laptop (psutil). Used for the `!stats` command
and background threshold alerts. Requires the bot to run on the host, not in a
container, to see real laptop metrics."""
from __future__ import annotations

try:
    import psutil
    _HAVE = True
except Exception:  # noqa: BLE001
    _HAVE = False


def available() -> bool:
    return _HAVE


def snapshot() -> dict:
    if not _HAVE:
        return {"error": "psutil not installed"}
    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    data: dict = {
        "cpu_percent": psutil.cpu_percent(interval=0.4),
        "cpu_per_core": psutil.cpu_percent(interval=0.0, percpu=True),
        "load_avg": _load(),
        "mem": {"percent": vm.percent, "used_gb": round(vm.used / 1e9, 2),
                "total_gb": round(vm.total / 1e9, 2)},
        "swap": {"percent": sm.percent, "used_gb": round(sm.used / 1e9, 2)},
        "uptime_h": _uptime_hours(),
        "disks": _disks(),
        "temps": _temps(),
        "battery": _battery(),
        "net": _net(),
        "top": _top_procs(),
    }
    return data


def format_report(s: dict) -> str:
    if "error" in s:
        return f"⚠️ monitoring unavailable: {s['error']}"
    lines = ["📊 **Laptop status**"]
    lines.append(f"CPU **{s['cpu_percent']}%**  |  load {s['load_avg']}  |  up {s['uptime_h']}h")
    m = s["mem"]
    lines.append(f"RAM **{m['percent']}%** ({m['used_gb']}/{m['total_gb']} GB)"
                 f"  |  swap {s['swap']['percent']}%")
    if s["battery"]:
        b = s["battery"]
        plug = "🔌" if b["plugged"] else "🔋"
        lines.append(f"{plug} battery {b['percent']}%")
    for d in s["disks"]:
        lines.append(f"💾 {d['mount']} {d['percent']}% ({d['used_gb']}/{d['total_gb']} GB)")
    if s["temps"]:
        lines.append("🌡️ " + ", ".join(f"{k} {v}°C" for k, v in s["temps"].items()))
    if s["top"]:
        top = ", ".join(f"{p['name']}({p['cpu']}%)" for p in s["top"][:5])
        lines.append(f"🔥 top: {top}")
    return "\n".join(lines)


def check_thresholds(s: dict, cpu=90.0, mem=90.0, disk=90.0) -> list[str]:
    """Return a list of human-readable alerts for anything over threshold."""
    if "error" in s:
        return []
    alerts = []
    if s["cpu_percent"] >= cpu:
        alerts.append(f"🚨 CPU at {s['cpu_percent']}%")
    if s["mem"]["percent"] >= mem:
        alerts.append(f"🚨 RAM at {s['mem']['percent']}%")
    for d in s["disks"]:
        if d["percent"] >= disk:
            alerts.append(f"🚨 disk {d['mount']} at {d['percent']}%")
    for name, t in (s["temps"] or {}).items():
        if t >= 97:
            alerts.append(f"🔥 {name} at {t}°C")
    return alerts


# ── helpers ────────────────────────────────────────────────────────────────
def _load():
    try:
        import os
        return tuple(round(x, 2) for x in os.getloadavg())
    except Exception:  # noqa: BLE001
        return None


def _uptime_hours():
    try:
        import time
        return round((time.time() - psutil.boot_time()) / 3600, 1)
    except Exception:  # noqa: BLE001
        return None


def _disks():
    out = []
    for part in psutil.disk_partitions(all=False):
        try:
            u = psutil.disk_usage(part.mountpoint)
        except Exception:  # noqa: BLE001
            continue
        if u.total < 1e9:  # skip tiny pseudo-mounts
            continue
        out.append({"mount": part.mountpoint, "percent": u.percent,
                    "used_gb": round(u.used / 1e9, 1), "total_gb": round(u.total / 1e9, 1)})
    return out


def _temps():
    try:
        temps = psutil.sensors_temperatures()
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for chip, entries in (temps or {}).items():
        for e in entries:
            if e.current:
                out[e.label or chip] = round(e.current, 1)
    return out


def _battery():
    try:
        b = psutil.sensors_battery()
        return {"percent": round(b.percent), "plugged": b.power_plugged} if b else None
    except Exception:  # noqa: BLE001
        return None


def _net():
    try:
        n = psutil.net_io_counters()
        return {"sent_mb": round(n.bytes_sent / 1e6, 1), "recv_mb": round(n.bytes_recv / 1e6, 1)}
    except Exception:  # noqa: BLE001
        return {}


def _top_procs():
    procs = []
    for p in psutil.process_iter(["name", "cpu_percent", "memory_percent"]):
        try:
            procs.append({"name": p.info["name"], "cpu": round(p.info["cpu_percent"] or 0, 1),
                          "mem": round(p.info["memory_percent"] or 0, 1)})
        except Exception:  # noqa: BLE001
            continue
    procs.sort(key=lambda x: x["cpu"], reverse=True)
    return procs[:8]
