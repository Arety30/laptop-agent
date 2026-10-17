"""Central configuration. Everything secret lives in .env (never commit it)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")


def _int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


# ── Discord ────────────────────────────────────────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
OWNER_ID = _int("OWNER_ID")                 # your Discord user id — only you can drive it
WORK_CHANNEL_ID = _int("WORK_CHANNEL_ID")   # optional: plain msgs here start a task
# Public-side identifiers (not used to log in — the token does that; kept for reference)
APPLICATION_ID = os.getenv("APPLICATION_ID", "")
DISCORD_PUBLIC_KEY = os.getenv("DISCORD_PUBLIC_KEY", "")

# ── Ollama / models ────────────────────────────────────────────────────────
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")

# Custom Ollama models (Modelfiles in models/) — tuned params baked into the
# model itself. abrar-coder/abrar-chat are llama3.2:1b, abrar-vision is
# granite3.2-vision:2b, abrar-reason is deepseek-r1:1.5b (shared weights).
CODER_MODEL = os.getenv("CODER_MODEL", "abrar-coder")
LIGHT_MODEL = os.getenv("LIGHT_MODEL", "abrar-chat")
VISION_MODEL = os.getenv("VISION_MODEL", "abrar-vision")
REASON_MODEL = os.getenv("REASON_MODEL", "abrar-reason")
TWIN_MODEL = os.getenv("TWIN_MODEL", "abrar-twin")               # Abrar's LLM twin

# Role → model routing (first five = coder model, report + chat = light model)
MODEL_ROUTING = {
    "planner": CODER_MODEL,
    "plan_reviewer": CODER_MODEL,
    "resource_adder": CODER_MODEL,
    "coder": CODER_MODEL,
    "tester": CODER_MODEL,
    "debugger": CODER_MODEL,
    "report_writer": LIGHT_MODEL,
    "chatbot": LIGHT_MODEL,
    "web_agent": LIGHT_MODEL,
    "reasoner": REASON_MODEL,
    "twin": TWIN_MODEL,
}

# ── OpenRouter cloud brain ──────────────────────────────────────────────────
# Set LLM_BACKEND=openrouter to route every role through OpenRouter instead of
# local Ollama (works with the laptop/Pi off; uses :free models only).
LLM_BACKEND = os.getenv("LLM_BACKEND", "ollama").strip().lower()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE = os.getenv("OPENROUTER_BASE", "https://openrouter.ai/api/v1")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemma-4-31b-it:free")

# Coder-family roles get a bigger context window + low temperature ("more context power")
CODER_ROLES = {"planner", "plan_reviewer", "resource_adder", "coder", "tester", "debugger"}

# ── Filesystem sandbox ─────────────────────────────────────────────────────
# Files/commands are confined to this root. Defaults to your home so the agent
# can actually "work on your laptop", but system paths are blocked in safety.py.
SANDBOX_ROOT = Path(os.getenv("SANDBOX_ROOT", str(Path.home()))).resolve()
WORKSPACE = Path(os.getenv("WORKSPACE", str(ROOT / "workspace"))).resolve()
WORKSPACE.mkdir(parents=True, exist_ok=True)

# Where uploaded screenshots/files land, and the adaptive user-profile store
UPLOADS_DIR = WORKSPACE / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
PROFILE_PATH = ROOT / "user_profile.json"

# Twin fine-tuning: collect Abrar's real messages here as training data
TWIN_DIR = ROOT / "twin"
TWIN_DIR.mkdir(parents=True, exist_ok=True)
TWIN_DATA = TWIN_DIR / "messages.jsonl"

# ── Safety toggles ─────────────────────────────────────────────────────────
DM_ONLY = _bool("DM_ONLY", False)          # if true, ignore all server-channel messages
ALLOW_SHELL = _bool("ALLOW_SHELL", True)   # host (laptop) shell execution
AUTO_LEARN = _bool("AUTO_LEARN", False)    # auto-add profile notes from chats (off: too noisy on 1B)
SHELL_TIMEOUT = _int("SHELL_TIMEOUT", 180)
MAX_AGENT_ITERATIONS = _int("MAX_AGENT_ITERATIONS", 8)  # tool-loop cap per agent

# ── HTTP (behind Apache) ───────────────────────────────────────────────────
BOT_HTTP_HOST = os.getenv("BOT_HTTP_HOST", "127.0.0.1")
BOT_HTTP_PORT = _int("BOT_HTTP_PORT", 8765)

# Bot -> n8n email webhook, routed THROUGH Apache (proxy /n8n/ -> n8n:5678)
N8N_EMAIL_WEBHOOK = os.getenv(
    "N8N_EMAIL_WEBHOOK", "http://127.0.0.1/n8n/webhook/send-email"
)
EMAIL_TO = os.getenv("EMAIL_TO", "user@example.com")
EMAIL_ON_COMPLETE = _bool("EMAIL_ON_COMPLETE", True)

# Bot -> n8n token/API-key provisioner webhook, also routed through Apache
N8N_WATCH_WEBHOOK = os.getenv(
    "N8N_WATCH_WEBHOOK", "http://127.0.0.1/n8n/webhook/file-event"
)

N8N_TOKEN_WEBHOOK = os.getenv(
    "N8N_TOKEN_WEBHOOK", "http://127.0.0.1/n8n/webhook/provision-token"
)

# ── OpenManus (optional, off by default) ───────────────────────────────────
OPENMANUS_ENABLED = _bool("OPENMANUS_ENABLED", False)
OPENMANUS_HOME = Path(os.getenv("OPENMANUS_HOME", str(Path.home() / "OpenManus")))

# ── VM subsystem (sandboxed desktop where you sign into Google/Proton) ──────
VM_ENABLED = _bool("VM_ENABLED", True)
VM_CONTAINER = os.getenv("VM_CONTAINER", "la-webtop")   # docker container name
VM_URL = os.getenv("VM_URL", "http://127.0.0.1:3000")   # webtop UI you sign into

# ── System monitoring / alerts ─────────────────────────────────────────────
MONITOR_ENABLED = _bool("MONITOR_ENABLED", True)
MONITOR_INTERVAL = _int("MONITOR_INTERVAL", 60)         # seconds between checks
ALERTS_ENABLED = _bool("ALERTS_ENABLED", True)          # DM you when a threshold trips
ALERT_CPU = _int("ALERT_CPU", 92)
ALERT_MEM = _int("ALERT_MEM", 90)
ALERT_DISK = _int("ALERT_DISK", 90)

LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
