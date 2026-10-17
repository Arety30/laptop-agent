"""Adaptive user profile — the bot's long-term memory of how Abrar behaves and
what he prefers, so the chatbot adapts over time. Persisted as JSON; injected
into the chatbot's context and grown from conversations."""
from __future__ import annotations

import json

import config

_MAX_NOTES = 40


def _load() -> dict:
    try:
        return json.loads(config.PROFILE_PATH.read_text("utf-8"))
    except Exception:  # noqa: BLE001
        return {"notes": []}


def _save(data: dict) -> None:
    try:
        config.PROFILE_PATH.write_text(json.dumps(data, indent=2), "utf-8")
    except Exception:  # noqa: BLE001
        pass


def remember(note: str) -> bool:
    """Add a durable observation/preference. Returns True if newly added."""
    note = note.strip()
    if not note:
        return False
    data = _load()
    notes = data.setdefault("notes", [])
    # de-dup (case-insensitive substring)
    low = note.lower()
    for n in notes:
        if low in n.lower() or n.lower() in low:
            return False
    notes.append(note)
    del notes[:-_MAX_NOTES]  # keep the most recent N
    _save(data)
    return True


def get_context() -> str:
    """Formatted profile for injection into the chatbot prompt."""
    notes = _load().get("notes", [])
    if not notes:
        return ""
    lines = "\n".join(f"- {n}" for n in notes[-_MAX_NOTES:])
    return f"What you've learned about the user (adapt to this):\n{lines}"


def all_notes() -> list[str]:
    return _load().get("notes", [])


def clear() -> None:
    _save({"notes": []})
