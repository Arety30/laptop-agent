"""Limb 3 — EARS + MOUTH.

Ears  = faster-whisper (CTranslate2, CPU, tiny model already cached) → transcribe
        any voice note / audio file the user sends in DM. Light + cool: no PyTorch,
        the tiny model is ~75MB and loads lazily, then stays warm.
Mouth = gTTS (Google Translate TTS) → speak a reply as an mp3. No API key, just a
        light HTTPS request. Falls back gracefully offline.

Kept deliberately small so it fits the 7GB / single-fan laptop."""
from __future__ import annotations

import asyncio
import os

import config

SHOTS = config.WORKSPACE / "outputs"
SHOTS.mkdir(parents=True, exist_ok=True)

_MODEL_SIZE = os.getenv("WHISPER_MODEL", "tiny")
_LANG = os.getenv("WHISPER_LANG", "")          # "" = auto-detect
_model = None                                  # lazy singleton


def available() -> dict:
    have_ears = have_mouth = False
    try:
        import faster_whisper  # noqa: F401
        have_ears = True
    except Exception:  # noqa: BLE001
        pass
    try:
        import gtts  # noqa: F401
        have_mouth = True
    except Exception:  # noqa: BLE001
        pass
    return {"ears": have_ears, "mouth": have_mouth, "model": _MODEL_SIZE}


def _load():
    """Load the tiny model once, from the local HF cache (no download)."""
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        # int8 on CPU = smallest RAM + fastest; single thread keeps heat low.
        _model = WhisperModel(_MODEL_SIZE, device="cpu", compute_type="int8",
                              cpu_threads=max(1, (os.cpu_count() or 2) - 1))
    return _model


def _transcribe_sync(path: str) -> dict:
    model = _load()
    kw = {"beam_size": 1, "vad_filter": True}   # beam 1 = fast/cool
    if _LANG:
        kw["language"] = _LANG
    segments, info = model.transcribe(path, **kw)
    text = " ".join(s.text.strip() for s in segments).strip()
    return {"ok": bool(text), "text": text,
            "lang": getattr(info, "language", None)}


async def transcribe(path: str) -> dict:
    """EARS: turn an audio file (ogg/opus/mp3/wav/m4a) into text."""
    if not os.path.exists(path):
        return {"ok": False, "error": "audio file not found"}
    try:
        return await asyncio.to_thread(_transcribe_sync, path)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}


def _speak_sync(text: str, name: str) -> dict:
    from gtts import gTTS
    dst = SHOTS / f"{name}.mp3"
    gTTS(text=text[:600], lang=os.getenv("TTS_LANG", "en")).save(str(dst))
    return {"ok": True, "path": str(dst)}


async def speak(text: str, name: str = "reply") -> dict:
    """MOUTH: synthesize speech (mp3) via Google TTS. No API key."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "nothing to say"}
    try:
        return await asyncio.to_thread(_speak_sync, text, name)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
