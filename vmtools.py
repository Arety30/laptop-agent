"""The VM subsystem — real isolation, not just namespaces.

Architecture:
    host (your laptop)  ->  KVM virtual machine  ->  Docker containers

`<vm>` actions and `!vm` run inside that VM (or inside a container inside it),
so risky/sandboxed work can't touch the laptop. Host shell commands still run on
the laptop via tools.run_shell — that separation is the whole point.

Managed by vm/vmctl.sh; this module just talks to it.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import config

_VM_DIR = Path(__file__).resolve().parent / "vm"
_VMCTL = _VM_DIR / "vmctl.sh"
_KEY = _VM_DIR / "id_ed25519"
_SSH_PORT = "2222"
_SSH_OPTS = ["-i", str(_KEY), "-p", _SSH_PORT,
             "-o", "StrictHostKeyChecking=no",
             "-o", "UserKnownHostsFile=/dev/null",
             "-o", "LogLevel=ERROR",
             "-o", "ConnectTimeout=8"]


async def _run(cmd: list[str], timeout: int = 120) -> dict:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except FileNotFoundError:
        return {"ok": False, "error": f"{cmd[0]} not found"}
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"timed out after {timeout}s"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    return {"ok": proc.returncode == 0, "returncode": proc.returncode,
            "stdout": out.decode("utf-8", "replace"),
            "stderr": err.decode("utf-8", "replace")}


def _ssh(remote_cmd: str) -> list[str]:
    return ["ssh", *_SSH_OPTS, "agent@127.0.0.1", remote_cmd]


async def is_up() -> bool:
    if not _KEY.exists():
        return False
    r = await _run(_ssh("true"), timeout=12)
    return bool(r.get("ok"))


async def run_in_vm(cmd: str, timeout: int | None = None) -> dict:
    """Run a command INSIDE the sandbox container, inside the VM.

    Falls back to running directly in the VM if the container isn't there.
    """
    if not config.VM_ENABLED:
        return {"ok": False, "error": "VM disabled (VM_ENABLED=false)"}
    if not _KEY.exists():
        return {"ok": False, "error": "VM not built yet — run: bash vm/setup_vm.sh"}
    if not await is_up():
        return {"ok": False,
                "error": "VM is not running — start it: bash vm/vmctl.sh start --free-ram"}

    t = timeout or config.SHELL_TIMEOUT
    safe = cmd.replace('"', '\\"')
    # prefer the container (defence in depth); fall back to the VM itself
    r = await _run(_ssh(f'docker exec sandbox sh -lc "{safe}"'), timeout=t)
    if not r.get("ok") and "No such container" in (r.get("stderr") or ""):
        r = await _run(_ssh(f'sh -lc "{safe}"'), timeout=t)
        r["note"] = "ran in VM (sandbox container missing)"
    return r


async def run_in_vm_host(cmd: str, timeout: int | None = None) -> dict:
    """Run a command in the VM itself (not the container) — e.g. docker admin."""
    if not await is_up():
        return {"ok": False, "error": "VM is not running"}
    safe = cmd.replace('"', '\\"')
    return await _run(_ssh(f'sh -lc "{safe}"'), timeout=timeout or config.SHELL_TIMEOUT)


async def open_url(url: str) -> dict:
    """Fetch a URL from inside the VM (browsing isolated from the laptop)."""
    return await run_in_vm(f"curl -sL --max-time 30 {shell_quote(url)} | head -c 4000")


async def status() -> dict:
    """Full picture: is the VM up, and what containers run inside it."""
    if not _KEY.exists():
        return {"ok": False, "built": False, "running": False,
                "error": "not built — run: bash vm/setup_vm.sh"}
    up = await is_up()
    if not up:
        return {"ok": True, "built": True, "running": False,
                "hint": "start: bash vm/vmctl.sh start --free-ram"}
    ps = await _run(_ssh("docker ps --format '{{.Names}} ({{.Image}}) {{.Status}}'"), 25)
    return {"ok": True, "built": True, "running": True,
            "containers": [l for l in (ps.get("stdout") or "").splitlines() if l.strip()]}


async def start(free_ram: bool = True) -> dict:
    """Boot the VM from the bot (long — cloud-init installs docker on first run)."""
    if not _VMCTL.exists():
        return {"ok": False, "error": "vm/vmctl.sh missing"}
    args = ["bash", str(_VMCTL), "start"] + (["--free-ram"] if free_ram else [])
    return await _run(args, timeout=600)


async def stop() -> dict:
    return await _run(["bash", str(_VMCTL), "stop"], timeout=120)


def shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def available() -> bool:
    return _KEY.exists() and shutil.which("qemu-system-x86_64") is not None
