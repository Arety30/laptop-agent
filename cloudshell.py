"""Google Cloud Shell as the cloud bot's sandbox — code runs on Google's free VM, not the laptop.

Uses the Cloud Shell API (cloudshell.googleapis.com) with the gcloud OAuth refresh token that is
already on disk (~/.config/gcloud/legacy_credentials/<acct>/adc.json). gcloud itself is not needed.
Cloud Shell is started on demand (:start with our ssh public key), then we ssh to port 6000.

Public API (async):
    run(cmd, timeout=120)          -> {"ok", "out", "code"}
    put(local, remote)             -> bool           (scp to the VM)
    get(remote, local)             -> bool           (scp from the VM)
    run_script(code, lang, files_dir) -> {"ok","out","files":[local paths]}  write+run a script,
                                      fetch anything new it left in ~/outputs
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import shlex
import subprocess
import time
import urllib.parse
import urllib.request

ACCT = os.getenv("CLOUDSHELL_ACCOUNT", "user@example.com")
ADC = os.path.expanduser(f"~/.config/gcloud/legacy_credentials/{ACCT}/adc.json")
KEY = os.path.expanduser("~/.ssh/google_compute_engine")
API = "https://cloudshell.googleapis.com/v1/users/me/environments/default"
OUT_REMOTE = "outputs"          # ~/outputs on the VM

_tok: tuple[str, float] = ("", 0.0)
_env: dict = {}                 # last known {sshHost, sshPort, sshUsername, state}
_lock = asyncio.Lock()


def available() -> bool:
    return os.path.exists(ADC) and os.path.exists(KEY)


def _token() -> str:
    global _tok
    if _tok[1] > time.time() + 60:
        return _tok[0]
    a = json.load(open(ADC))
    data = urllib.parse.urlencode({"client_id": a["client_id"], "client_secret": a["client_secret"],
                                   "refresh_token": a["refresh_token"], "grant_type": "refresh_token"}).encode()
    t = json.load(urllib.request.urlopen(urllib.request.Request("https://oauth2.googleapis.com/token", data), timeout=30))
    _tok = (t["access_token"], time.time() + int(t.get("expires_in", 3600)))
    return _tok[0]


def _api(path: str = "", body: dict | None = None) -> dict:
    req = urllib.request.Request(API + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + _token(), "Content-Type": "application/json"},
                                 method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _ensure_running_sync() -> dict:
    """Start Cloud Shell if needed and return env dict with ssh details."""
    global _env
    e = _api()
    if e.get("state") != "RUNNING":
        pub = " ".join(open(KEY + ".pub").read().split()[:2])   # no comment: API 500s on it
        keys = e.get("publicKeys", [])
        op = _api(":start", {} if pub in keys else {"publicKeys": [pub]})
        name = op.get("name", "")
        for _ in range(60):
            time.sleep(3)
            o = json.load(urllib.request.urlopen(urllib.request.Request(
                "https://cloudshell.googleapis.com/v1/" + name, headers={"Authorization": "Bearer " + _token()}), timeout=30))
            if o.get("done"):
                break
        e = _api()
    _env = {k: e.get(k) for k in ("sshHost", "sshPort", "sshUsername", "state")}
    if e.get("state") != "RUNNING" or not e.get("sshHost"):
        raise RuntimeError(f"Cloud Shell not running (state={e.get('state')})")
    return _env


def _ssh_base() -> list[str]:
    return ["-p", str(_env["sshPort"]), "-i", KEY, "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=20"]


def _target() -> str:
    return f"{_env['sshUsername']}@{_env['sshHost']}"


def _run_sync(cmd: str, timeout: int) -> dict:
    for attempt in (1, 2):
        try:
            if not _env.get("sshHost"):
                _ensure_running_sync()
            p = subprocess.run(["ssh", "-T", *_ssh_base(), _target(), "--", cmd],
                               capture_output=True, text=True, timeout=timeout)
            if p.returncode == 255 and attempt == 1:      # stale host (Cloud Shell moved) -> refresh
                _env.clear(); continue
            out = (p.stdout + p.stderr).strip()
            return {"ok": p.returncode == 0, "out": out, "code": p.returncode}
        except subprocess.TimeoutExpired:
            return {"ok": False, "out": f"timed out after {timeout}s", "code": -1}
        except Exception as ex:  # noqa: BLE001
            if attempt == 2:
                return {"ok": False, "out": f"cloud shell error: {ex}", "code": -1}
            _env.clear()
    return {"ok": False, "out": "cloud shell unreachable", "code": -1}


async def run(cmd: str, timeout: int = 120) -> dict:
    async with _lock:
        return await asyncio.to_thread(_run_sync, cmd, timeout)


def _scp(src: str, dst: str) -> bool:
    p = subprocess.run(["scp", "-q", "-P", str(_env["sshPort"]), "-i", KEY, "-o", "StrictHostKeyChecking=no",
                        "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", src, dst],
                       capture_output=True, text=True, timeout=180)
    return p.returncode == 0


async def put(local: str, remote: str) -> bool:
    async with _lock:
        if not _env.get("sshHost"):
            await asyncio.to_thread(_ensure_running_sync)
        return await asyncio.to_thread(_scp, local, f"{_target()}:{remote}")


async def get(remote: str, local: str) -> bool:
    async with _lock:
        if not _env.get("sshHost"):
            await asyncio.to_thread(_ensure_running_sync)
        return await asyncio.to_thread(_scp, f"{_target()}:{remote}", local)


_RUNNERS = {"python": "python3 {f}", "bash": "bash {f}", "node": "node {f}", "sh": "bash {f}"}


async def run_script(code: str, lang: str = "python", files_dir: str = "/tmp", timeout: int = 180) -> dict:
    """Run a script in ~/outputs on the VM; return stdout and any new files it produced (fetched locally)."""
    lang = lang if lang in _RUNNERS else "python"
    ext = {"python": "py", "bash": "sh", "sh": "sh", "node": "js"}[lang]
    fname = f"job_{int(time.time())}.{ext}"
    setup = (f"mkdir -p ~/{OUT_REMOTE} && cd ~/{OUT_REMOTE} && ls -1 > /tmp/.before && "
             f"cat > {fname} <<'__EOF__'\n{code}\n__EOF__\n"
             f"timeout {timeout} " + _RUNNERS[lang].format(f=fname) + " 2>&1; echo \"__RC=$?\"; "
             f"ls -1 | grep -vxF -f /tmp/.before | grep -v '^job_' > /tmp/.new; cat /tmp/.new | sed 's/^/__NEW=/'")
    r = await run(setup, timeout=timeout + 40)
    new, lines, rc = [], [], r["code"]
    for ln in r["out"].splitlines():
        if ln.startswith("__NEW="):
            new.append(ln[6:])
        elif ln.startswith("__RC="):
            rc = int(ln[5:] or -1)
        else:
            lines.append(ln)
    os.makedirs(files_dir, exist_ok=True)
    fetched = []
    for n in new[:8]:
        local = os.path.join(files_dir, os.path.basename(n))
        if await get(f"{OUT_REMOTE}/{shlex.quote(n)}", local) and os.path.getsize(local) <= 24_000_000:
            fetched.append(local)
    return {"ok": rc == 0, "out": "\n".join(lines).strip(), "files": fetched}
