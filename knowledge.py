"""Grounds the twin in Abrar's REAL data (twin/metadata.json, built by
twin/build_metadata.py from his full Google Takeout: 36k watched videos, 3k
searches, subscriptions, playlists, notebooks, drive).

`fact_for(message)` returns a one-line FACT to inject into the prompt when the
message mentions something he actually watches/searches/likes — so the twin
answers from real data instead of guessing."""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

_META = Path(__file__).resolve().parent / "twin" / "metadata.json"

try:
    _D = json.loads(_META.read_text("utf-8"))
except Exception:  # noqa: BLE001
    _D = {}

_yt = _D.get("youtube_watch", {})

# ── channels he actually watches (top watched + subscriptions) ─────────────
_COUNTS: dict[str, int] = {c["channel"]: c["count"] for c in _yt.get("top_channels", [])}
for s in _D.get("subscriptions", []):
    _COUNTS.setdefault(s, 0)
_CHANNELS = list(_COUNTS)

# ── topics he actually watches/searches (data-driven, from real keywords) ──
_TOPIC_LABELS = {
    "ninjago": "LEGO Ninjago", "dreamzz": "LEGO DreamZzz", "lego": "LEGO",
    "osman": "Kurulus Osman", "kurulus": "Kurulus Osman", "ertugrul": "Ertugrul",
    "avatar": "Avatar/Korra", "korra": "Avatar/Korra", "bionicle": "Bionicle",
    "marvel": "Marvel", "avengers": "Marvel", "spiderman": "Spider-Man",
    "thor": "Marvel", "flash": "The Flash", "barry": "The Flash",
    "quran": "Quran", "surah": "Quran", "nasheed": "nasheeds", "islam": "Islam",
    "muslim": "Islam", "muhammad": "Islamic history", "ghazi": "Ertugrul/Ghazi",
    "tarek": "Mohamed Tarek (nasheeds)",
    "carterpcs": "PC building", "pc": "PC building", "tech": "tech",
    "apple": "Apple/tech", "python": "Python", "coding": "coding", "ai": "AI",
    "llm": "LLMs", "ollama": "Ollama", "ubuntu": "Ubuntu", "raspberry": "Raspberry Pi",
    "esp32": "ESP32", "turbowarp": "TurboWarp",
    "barca": "Barcelona", "barcelona": "Barcelona", "ronaldo": "football",
    "messi": "football", "mbappe": "football", "football": "football",
    "recipe": "cooking videos", "food": "food videos",
    "riddle": "riddles", "urdu": "Urdu content", "funny": "comedy shorts",
    "minecraft": "Minecraft", "roblox": "Roblox", "fifa": "FIFA",
}
# only keep topics actually present in his data
_REAL_TERMS = {k["term"] for k in _yt.get("top_title_keywords", [])} | \
              {k["term"] for k in _D.get("youtube_searches", {}).get("top_keywords", [])}
_ALWAYS = ("turbowarp", "ollama", "esp32", "raspberry", "dreamzz", "korra",
           "minecraft", "roblox", "pc", "ai", "llm", "coding", "python", "ubuntu",
           "barca", "barcelona", "football", "quran", "nasheed")
_TOPICS = {kw: lab for kw, lab in _TOPIC_LABELS.items()
           if kw in _REAL_TERMS or kw in _ALWAYS}

_NORM = [(c, re.sub(r"[^a-z0-9]", "", c.lower())) for c in _CHANNELS]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def channels_mentioned(message: str) -> list[str]:
    """Channels from his real data referenced in the message (typo tolerant)."""
    m = _norm(message)
    if len(m) < 4:
        return []
    hits = []
    for name, cn in _NORM:
        if len(cn) < 6:
            continue
        mm = difflib.SequenceMatcher(None, cn, m).find_longest_match(0, len(cn), 0, len(m))
        if mm.size >= max(6, int(len(cn) * 0.75)):
            hits.append(name)
    hits.sort(key=lambda c: _COUNTS.get(c, 0), reverse=True)
    return hits[:5]


def topics_mentioned(message: str) -> list[str]:
    m = message.lower()
    out = []
    for kw, label in _TOPICS.items():
        if re.search(rf"\b{re.escape(kw)}", m) and label not in out:
            out.append(label)
    return out[:5]


def fact_for(message: str) -> str:
    """One-line grounding fact, or '' if the message doesn't touch his real data."""
    parts = []
    chans = channels_mentioned(message)
    if chans:
        tagged = [f"{c} (watch a lot)" if _COUNTS.get(c, 0) > 50 else c for c in chans]
        parts.append("you watch " + ", ".join(tagged) + " on youtube")
    topics = topics_mentioned(message)
    if topics:
        parts.append("you're into " + ", ".join(topics))
    if not parts:
        return ""
    return ("FACT (this is TRUE about you — agree with it, don't contradict it): "
            + "; ".join(parts) + ". So YES you like/watch them — reply as yourself.")


def stats() -> dict:
    """Quick summary of what the twin knows about him."""
    return {
        "videos_watched": _yt.get("total_videos", 0),
        "channels_known": len(_CHANNELS),
        "searches": _D.get("youtube_searches", {}).get("total", 0),
        "topics": len(_TOPICS),
        "playlists": _D.get("playlists", {}),
        "notebooks": _D.get("notebooklm", {}).get("count", 0),
    }
