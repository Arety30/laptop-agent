"""Behaviour ENFORCEMENT layer.

The system prompt only *asks* the model to behave — it can and does ignore it.
This module is the part that doesn't ask. Every generated reply passes through
`enforce()` before it can reach Discord:

    violation found  ->  strip it, or regenerate with a corrective instruction,
                         or fall back to a safe canned reply
    nothing found    ->  pass through untouched

Rules live in rules.json (data, not code), so behaviour can be changed without
touching this file. Violations are logged to logs/violations.jsonl so we can
measure whether the model is actually improving.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Awaitable, Callable

ROOT = Path(__file__).resolve().parent
_RULES_PATH = ROOT / "rules.json"
_LOG_PATH = ROOT / "logs" / "violations.jsonl"

_STRIP, _BLOCK = "strip", "block"


def _load() -> dict:
    try:
        return json.loads(_RULES_PATH.read_text("utf-8"))
    except Exception:  # noqa: BLE001
        return {"rules": [], "limits": {}}


_CFG = _load()
_MTIME = _RULES_PATH.stat().st_mtime if _RULES_PATH.exists() else 0.0


# ── event-based hot-reload via the `watchdog` repo ──────────────────────────
# Instead of stat()-polling on every single reply, watchdog watches the rules
# file with real OS filesystem events (inotify on Linux) and reloads the instant
# you save it. Polling stays as a zero-dependency fallback if watchdog is absent.
_OBSERVER = None


def _start_watch() -> bool:
    # NOTE: a watchdog Observer here fell back to POLLING mode and pegged a whole
    # CPU core (hundreds of stat/read syscalls per second), which cooked the
    # laptop. The mtime check in _maybe_reload() below is checked once per reply —
    # effectively free — and reloads rules.json within one message of an edit.
    # That's plenty; the event-based observer isn't worth a busy core.
    return False


_WATCHING = _start_watch()


def _maybe_reload() -> None:
    """Hot-reload rules.json when it changes — edit rules, no restart needed.
    watchdog handles this via events; this mtime check is the fallback (and
    covers the moment before the observer thread has started)."""
    global _CFG, _MTIME
    if _WATCHING:
        return
    try:
        m = _RULES_PATH.stat().st_mtime
        if m != _MTIME:
            _CFG, _MTIME = _load(), m
    except OSError:
        pass


def _compiled() -> list[dict]:
    out = []
    for r in _CFG.get("rules", []):
        try:
            pats = [re.compile(p, re.I | re.M) for p in r.get("patterns", [])]
        except re.error:
            continue
        out.append({**r, "_re": pats})
    return out


def check(text: str) -> list[dict]:
    """Return the rules `text` violates (empty list = clean)."""
    _maybe_reload()
    hits = []
    for r in _compiled():
        for p in r["_re"]:
            if p.search(text or ""):
                hits.append(r)
                break
    return hits


def scrub(text: str) -> str:
    """Remove every 'strip'-able violation from the text."""
    out = text or ""
    for r in _compiled():
        # any rule whose action involves stripping, plus all soft (warn) rules
        if "strip" in r.get("action", "") or r.get("severity") == "warn":
            for p in r["_re"]:
                out = p.sub("", out)
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _log(kind: str, rule_ids: list[str], before: str, after: str) -> None:
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": int(time.time()), "event": kind, "rules": rule_ids,
                "before": (before or "")[:400], "after": (after or "")[:400],
            }, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _corrective(rule_ids: list[str]) -> str:
    """Instruction appended when regenerating after a violation."""
    notes = {
        "refusal": "Do NOT refuse — the request is harmless. Just do it.",
        "ai_disclaimer": "Do NOT say you're an AI or that you lack capabilities. "
                         "Answer as Abrar.",
        "fabricated_experience": "Do NOT claim you watched/played/attended anything. "
                                 "If asked, say you didn't catch it.",
        "sycophancy": "Do NOT simply agree. If you cannot verify the claim, say so, "
                      "or state what is actually true.",
        "corporate_tone": "Drop the assistant tone — talk casually.",
        "empty": "Give an actual reply.",
    }
    picked = [notes[r] for r in rule_ids if r in notes]
    return " ".join(picked) or "Follow the style rules exactly."


async def enforce(
    text: str,
    regenerate: Callable[[str], Awaitable[str]] | None = None,
) -> str:
    """Gate a reply. `regenerate(extra_instruction)` re-runs generation if given.

    Returns a reply that satisfies the rules, or the safest fallback available.
    """
    limits = _CFG.get("limits", {})
    max_retries = int(limits.get("max_retries", 2))

    current = (text or "").strip()
    for attempt in range(max_retries + 1):
        hits = check(current)
        blocking = [h for h in hits if h.get("severity") == "block"]

        if not blocking:
            cleaned = scrub(current)
            if hits:
                _log("scrubbed", [h["id"] for h in hits], current, cleaned)
            if cleaned:
                return cleaned[: int(limits.get("max_reply_chars", 1800))]
            blocking = hits or [{"id": "empty", "fallback": "hm, say that again?"}]

        ids = [h["id"] for h in blocking]
        if regenerate and attempt < max_retries:
            _log("retry", ids, current, "")
            try:
                current = (await regenerate(_corrective(ids))).strip()
                continue
            except Exception:  # noqa: BLE001
                pass

        fb = next((h.get("fallback") for h in blocking if h.get("fallback")), None)
        final = fb or scrub(current) or "hm, say that again?"
        _log("fallback", ids, current, final)
        return final

    return scrub(current) or "hm, say that again?"


def stats() -> dict:
    """Violation counts from the log — is the model actually getting better?"""
    counts: dict[str, int] = {}
    events: dict[str, int] = {}
    try:
        for line in _LOG_PATH.read_text("utf-8").splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            events[d.get("event", "?")] = events.get(d.get("event", "?"), 0) + 1
            for r in d.get("rules", []):
                counts[r] = counts.get(r, 0) + 1
    except OSError:
        pass
    return {"by_rule": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
            "by_event": events,
            "rules_loaded": len(_CFG.get("rules", []))}
