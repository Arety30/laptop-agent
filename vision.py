"""Screenshot / image understanding via a local vision model (granite3.2-vision).
Images are downscaled first — full-res screenshots tokenize into tens of
thousands of tokens and blow the context window (and RAM)."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request

import config

_DEFAULT_PROMPT = ("Describe this image/screenshot in detail. If it contains UI, "
                   "explain what's on screen. Transcribe any visible text exactly.")

# Bot runs under systemd with a minimal PATH; find ffmpeg explicitly.
_FFMPEG = (shutil.which("ffmpeg")
           or next((p for p in ("ffmpeg",
                                "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg")
                    if os.path.exists(p)), None))


def _downscale(data: bytes, box: int = 384) -> bytes:
    """Fit the image inside box×box (preserving aspect) so it tokenizes small.
    Falls back to the original bytes if ffmpeg isn't available or fails."""
    if not _FFMPEG:
        return data
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fi:
            fi.write(data)
            src = fi.name
        dst = src + ".small.png"
        subprocess.run(
            [_FFMPEG, "-y", "-loglevel", "error", "-i", src,
             "-vf", f"scale='min({box},iw)':'min({box},ih)':"
                    f"force_original_aspect_ratio=decrease", dst],
            check=True, timeout=30,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        out = open(dst, "rb").read()
        os.unlink(src)
        os.unlink(dst)
        return out or data
    except Exception:  # noqa: BLE001
        return data


async def analyze(image_bytes: bytes, prompt: str | None = None) -> str:
    small = await asyncio.to_thread(_downscale, image_bytes)
    payload = {
        "model": config.VISION_MODEL,
        "messages": [{
            "role": "user",
            "content": prompt or _DEFAULT_PROMPT,
            "images": [base64.b64encode(small).decode()],
        }],
        "stream": False,
        "keep_alive": "2m",
        "options": {"temperature": 0.3, "num_ctx": 8192, "num_predict": 512},
    }

    def _post() -> str:
        req = urllib.request.Request(
            f"{config.OLLAMA_HOST}/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.loads(r.read().decode())
        return (data.get("message", {}).get("content") or "").strip()

    try:
        return await asyncio.to_thread(_post)
    except Exception as e:  # noqa: BLE001
        return f"(couldn't analyze the image: {e})"
