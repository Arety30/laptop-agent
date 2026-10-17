"""Grounding guard — the last cheap check between a 1B model and a confident lie.

Two pure-text passes, no LLM/search cost, no heat:

  collapse_repeats(text)         -> kill the stutter loops small models fall into
  hedge_if_unverified(text, had_search)
                                 -> if the reply states a SPECIFIC checkable fact
                                    (a year, a score, a 'first/biggest', a %), and
                                    nothing was actually searched, and it isn't
                                    already hedging, append an honest hedge instead
                                    of letting it assert something it can't know.

This does not make the model correct — it makes it stop *sounding* certain when
it has no ground to stand on. Verifying for real is factcheck.py's job.
"""
from __future__ import annotations

import re

# ── stutter / repetition ────────────────────────────────────────────────────
_WORD_RUN = re.compile(r"\b(\w{2,})(\s+\1\b){2,}", re.I)        # "no no no" -> "no"
_SENT_RUN = re.compile(r"([^.!?\n]{6,}?[.!?])(\s*\1){1,}", re.I)  # dup sentence


def collapse_repeats(text: str) -> str:
    t = text or ""
    t = _WORD_RUN.sub(r"\1", t)
    t = _SENT_RUN.sub(r"\1", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()


# ── unverified specific-claim detection ─────────────────────────────────────
# Signals that a statement is a checkable, specific fact (not an opinion/chit-chat)
_SPECIFIC = re.compile(
    r"(?<!\d)(19|20)\d{2}(?!\d)"                       # a year
    r"|\b\d+\s*[-–]\s*\d+\b"                           # a score / range
    r"|\b\d+(\.\d+)?\s*(%|percent|million|billion|km|kg|°c|degrees)\b"
    r"|\b(first|last|only|biggest|largest|smallest|fastest|tallest|richest|oldest)\b"
    r"|\b(won|beat|defeated|invented|founded|discovered|died|born|launched|released)\b"
    r"|\bcapital of\b|\bpresident of\b|\bceo of\b",
    re.I,
)
# Already hedging? then leave it alone.
_HEDGED = re.compile(
    r"\b(i think|i'?m not sure|not certain|might be|maybe|probably|could be|"
    r"if i recall|afaik|i believe|not 100|don'?t quote me|look (it|that) up|"
    r"want me to (check|search|look)|correct me if)\b",
    re.I,
)
# Questions / offers aren't assertions.
_QUESTION_ONLY = re.compile(r"^[^.!]*\?\s*$")

_HEDGE = " — though i'm not 100% sure, want me to look it up?"


def states_unverified_fact(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 12 or _QUESTION_ONLY.match(t):
        return False
    return bool(_SPECIFIC.search(t)) and not _HEDGED.search(t)


def hedge_if_unverified(text: str, had_search: bool) -> str:
    """Append an honest hedge to a confident, specific, UN-searched claim."""
    if had_search:
        return text
    t = (text or "").strip()
    if not states_unverified_fact(t):
        return t
    # attach the hedge onto the end, before any trailing question of its own
    return (t.rstrip(".! ") + _HEDGE)
