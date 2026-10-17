"""The hands of the agents: run shell commands and read/write/edit files.
Every call is routed through safety.py first."""
from __future__ import annotations

import asyncio
from pathlib import Path

import config
import safety


async def run_shell(cmd: str, cwd: str | None = None, timeout: int | None = None,
                    source: str = "agent") -> dict:
    """source="user" for commands Abrar types himself (`$ …`); "agent" for the
    autonomous pipeline, which gets the stricter blocklist."""
    ok, reason = safety.check_command(cmd, source=source)
    if not ok:
        return {"ok": False, "returncode": -1, "stdout": "", "stderr": reason}

    workdir = str(safety.resolve_in_sandbox(cwd)) if cwd else str(config.WORKSPACE)
    timeout = timeout or config.SHELL_TIMEOUT
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return {"ok": False, "returncode": -1, "stdout": "",
                "stderr": f"timed out after {timeout}s"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "returncode": -1, "stdout": "", "stderr": str(e)}

    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": _clip(out.decode("utf-8", "replace")),
        "stderr": _clip(err.decode("utf-8", "replace")),
    }


def read_file(path: str, max_bytes: int = 40_000) -> dict:
    try:
        p = safety.resolve_in_sandbox(path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not p.exists():
        return {"ok": False, "error": f"no such file: {p}"}
    try:
        data = p.read_text("utf-8", "replace")[:max_bytes]
        return {"ok": True, "path": str(p), "content": data}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def write_file(path: str, content: str) -> dict:
    try:
        p = safety.resolve_in_sandbox(path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, "utf-8")
        return {"ok": True, "path": str(p), "bytes": len(content)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def edit_file(path: str, find: str, replace: str) -> dict:
    """Exact-string replace (first occurrence)."""
    try:
        p = safety.resolve_in_sandbox(path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not p.exists():
        return {"ok": False, "error": f"no such file: {p}"}
    text = p.read_text("utf-8", "replace")
    if find not in text:
        return {"ok": False, "error": "find-string not present in file"}
    p.write_text(text.replace(find, replace, 1), "utf-8")
    return {"ok": True, "path": str(p)}


def list_dir(path: str = ".") -> dict:
    try:
        p = safety.resolve_in_sandbox(path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not p.exists():
        return {"ok": False, "error": f"no such path: {p}"}
    if p.is_file():
        return {"ok": True, "path": str(p), "entries": [p.name], "note": "is a file"}
    entries = []
    for c in sorted(p.iterdir()):
        try:
            size = c.stat().st_size if c.is_file() else 0
        except OSError:
            size = 0
        entries.append(("d " if c.is_dir() else "- ") + c.name + (f" ({size}b)" if size else ""))
    return {"ok": True, "path": str(p), "entries": entries}


def append_file(path: str, content: str) -> dict:
    try:
        p = safety.resolve_in_sandbox(path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(content)
        return {"ok": True, "path": str(p), "appended": len(content)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def make_dir(path: str) -> dict:
    try:
        p = safety.resolve_in_sandbox(path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    try:
        p.mkdir(parents=True, exist_ok=True)
        return {"ok": True, "path": str(p)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def delete_path(path: str) -> dict:
    import shutil
    try:
        p = safety.resolve_in_sandbox(path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not p.exists():
        return {"ok": False, "error": f"no such path: {p}"}
    try:
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        return {"ok": True, "deleted": str(p)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def move_path(src: str, dst: str) -> dict:
    import shutil
    try:
        s = safety.resolve_in_sandbox(src)
        d = safety.resolve_in_sandbox(dst)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not s.exists():
        return {"ok": False, "error": f"no such source: {s}"}
    try:
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(s), str(d))
        return {"ok": True, "from": str(s), "to": str(d)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _clip(s: str, limit: int = 6000) -> str:
    return s if len(s) <= limit else s[:limit] + f"\n… [truncated {len(s) - limit} chars]"
