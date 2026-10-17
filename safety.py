"""Guardrails. Not a security boundary against a hostile operator — it's a
seatbelt so an autonomous 1.5B model can't nuke your machine by accident."""
from __future__ import annotations

import re
from pathlib import Path

import config

# Irreversible / data-destroying. Blocked for EVERYONE — a typo here costs you
# the machine, and no prank is worth `mkfs` on the wrong device.
_CATASTROPHIC = [
    r"\brm\s+-[a-z]*r[a-z]*f?\s+(/|~|\$HOME)(\s|$)",  # rm -rf / , rm -rf ~
    r"\bmkfs\b",
    r"\bdd\b.*\bof=/dev/",
    r">\s*/dev/sd[a-z]",
    r":\(\)\s*\{",                # fork bomb :(){ :|:& };:
    r"\bchmod\s+-R\s+0*7*77\s+/(\s|$)",
    r"\buserdel\b",
]

# Risky but legitimate for the OWNER typing `$ …` himself. Still blocked for the
# autonomous agents, which must never power off the laptop mid-task or pipe the
# internet into a shell.
_AGENT_ONLY_BLOCKED = [
    r"\bshutdown\b|\breboot\b|\bhalt\b|\bpoweroff\b",
    r"\bpasswd\b",
    r"\bcurl\b.*\|\s*(sudo\s+)?(bash|sh)\b",   # curl | bash
    r"\bwget\b.*\|\s*(sudo\s+)?(bash|sh)\b",
    r"\bgit\b.*\bpush\b.*--force",
    r"\b>\s*/etc/",
]

_BLOCKED_CMD_PATTERNS = _CATASTROPHIC + _AGENT_ONLY_BLOCKED   # back-compat

# Paths the file tools refuse to touch even inside the sandbox.
_BLOCKED_PATH_PREFIXES = [
    "/etc", "/usr", "/bin", "/sbin", "/boot", "/dev", "/proc", "/sys", "/var/lib",
]


def is_owner(user_id: int) -> bool:
    # Open mode: if OWNER_ID is unset (0), everyone who can message the bot is
    # allowed. Set OWNER_ID to lock the bot to just you. Because this bot runs
    # shell commands, "open mode" only makes sense in a private/DM-only server.
    if config.OWNER_ID == 0:
        return True
    return user_id == config.OWNER_ID


def check_command(cmd: str, source: str = "agent") -> tuple[bool, str]:
    """Return (allowed, reason).

    source="agent"  the 7-agent pipeline — full lockdown. An autonomous small
                    model must never power off the laptop or pipe curl into sh.
    source="user"   Abrar typing `$ …` himself. It's his machine, so power
                    commands are his call; only irreversible data loss stays
                    blocked (a typo'd `mkfs` isn't a decision, it's an accident).
    """
    if not config.ALLOW_SHELL:
        return False, "shell execution is disabled (ALLOW_SHELL=false)"
    low = cmd.strip()
    if not low:
        return False, "empty command"

    patterns = _CATASTROPHIC if source == "user" else _BLOCKED_CMD_PATTERNS
    for pat in patterns:
        if re.search(pat, low, re.IGNORECASE):
            return False, f"blocked by safety rule: /{pat}/"
    return True, "ok"


def resolve_in_sandbox(path: str | Path) -> Path:
    """Resolve a path and confirm it stays inside SANDBOX_ROOT and off system dirs.
    Raises ValueError if it escapes."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = (config.SANDBOX_ROOT / p)
    p = p.resolve()

    root = config.SANDBOX_ROOT
    if root not in p.parents and p != root:
        raise ValueError(f"path {p} is outside sandbox root {root}")
    for bad in _BLOCKED_PATH_PREFIXES:
        if str(p) == bad or str(p).startswith(bad + "/"):
            raise ValueError(f"path {p} is a protected system location")
    return p
