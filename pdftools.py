"""PDF reading for the bot, with an OCR fallback.

Two kinds of PDF turn up:
  * text-layer PDFs  -> pdftotext extracts instantly
  * scanned PDFs     -> zero extractable text; needs OCR (tesseract)

The bot previously saved PDFs and had nothing to say about them (it even told
Abrar to go use an online converter). This module fixes that.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

_PDFTOTEXT = shutil.which("pdftotext")
_PDFTOPPM = shutil.which("pdftoppm")
_TESSERACT = shutil.which("tesseract")
_PDFINFO = shutil.which("pdfinfo")


async def _sh(cmd: list[str], timeout: int = 120) -> tuple[int, str, str]:
    try:
        p = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(p.communicate(), timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return 1, "", str(e)
    return p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def page_count(path: str | Path) -> int:
    if not _PDFINFO:
        return 0
    _, out, _ = await _sh([_PDFINFO, str(path)], 30)
    for line in out.splitlines():
        if line.lower().startswith("pages:"):
            try:
                return int(line.split(":")[1].strip())
            except ValueError:
                return 0
    return 0


async def extract_text(path: str | Path, max_chars: int = 6000) -> dict:
    """Fast path: pull the text layer out of a PDF."""
    if not _PDFTOTEXT:
        return {"ok": False, "error": "pdftotext not installed"}
    rc, out, err = await _sh([_PDFTOTEXT, "-layout", str(path), "-"], 90)
    if rc != 0:
        return {"ok": False, "error": err[:200] or "pdftotext failed"}
    text = out.strip()
    return {"ok": True, "text": text[:max_chars], "chars": len(text),
            "scanned": len(text.replace(" ", "").replace("\n", "")) < 50}


async def ocr(path: str | Path, pages: int = 5, dpi: int = 200,
              max_chars: int = 6000) -> dict:
    """Slow path: OCR a scanned PDF, one page at a time to stay memory-light."""
    if not (_PDFTOPPM and _TESSERACT):
        return {"ok": False, "error": "tesseract/pdftoppm not installed"}
    total = await page_count(path) or pages
    n = min(pages, total)
    chunks: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        for p in range(1, n + 1):
            stem = str(Path(td) / f"pg{p}")
            rc, _, _ = await _sh(
                [_PDFTOPPM, "-f", str(p), "-l", str(p), "-r", str(dpi),
                 "-gray", "-png", str(path), stem], 90)
            if rc != 0:
                continue
            img = next(iter(sorted(Path(td).glob(f"pg{p}*.png"))), None)
            if not img:
                continue
            rc, out, _ = await _sh([_TESSERACT, str(img), "-", "--psm", "6"], 120)
            if rc == 0 and out.strip():
                chunks.append(f"--- page {p} ---\n{out.strip()}")
            img.unlink(missing_ok=True)
    text = "\n\n".join(chunks)
    return {"ok": bool(text), "text": text[:max_chars], "pages_ocred": len(chunks),
            "total_pages": total,
            "error": "" if text else "OCR produced no text"}


async def read(path: str | Path, ocr_pages: int = 5, max_chars: int = 6000) -> dict:
    """Read a PDF however it needs to be read. Returns {ok, text, method, …}."""
    r = await extract_text(path, max_chars)
    if r.get("ok") and not r.get("scanned"):
        return {**r, "method": "text-layer"}

    o = await ocr(path, pages=ocr_pages, max_chars=max_chars)
    if o.get("ok"):
        return {**o, "method": "ocr",
                "note": f"scanned PDF — OCR'd first {o['pages_ocred']} of "
                        f"{o['total_pages']} pages"}
    return {"ok": False, "method": "none",
            "error": o.get("error") or r.get("error") or "could not read PDF"}


def available() -> dict:
    return {"pdftotext": bool(_PDFTOTEXT), "tesseract": bool(_TESSERACT),
            "pdftoppm": bool(_PDFTOPPM)}
