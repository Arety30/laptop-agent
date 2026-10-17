"""Document & presentation generation — the bot's output side.

Until now the bot could only *read* files. This lets it produce real deliverables:

    markdown  --marp-->      slides.html / slides.pdf / slides.pptx
    markdown  --python-pptx--> .pptx built slide-by-slide
    html/slides --decktape--> .pdf
    markdown/qmd --quarto-->  polished report (html/pdf/docx)
    html      --libreoffice--> .pdf   (always-available fallback)

Everything writes into workspace/outputs/ and returns the path so the bot can
attach it in Discord.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import config

OUT_DIR = config.WORKSPACE / "outputs"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Reuse the system browser so marp/decktape don't each fetch their own chromium.
_CHROME = shutil.which("chromium") or shutil.which("chromium-browser") \
    or shutil.which("google-chrome")


def _tool(name: str) -> str | None:
    return shutil.which(name)


async def _sh(cmd: list[str], timeout: int = 180, env: dict | None = None) -> dict:
    e = {**os.environ, **(env or {})}
    if _CHROME:
        e.setdefault("CHROME_PATH", _CHROME)
        e.setdefault("PUPPETEER_EXECUTABLE_PATH", _CHROME)
    try:
        p = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=e)
        out, err = await asyncio.wait_for(p.communicate(), timeout=timeout)
    except FileNotFoundError:
        return {"ok": False, "error": f"{cmd[0]} not installed"}
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"timed out after {timeout}s"}
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "error": str(ex)}
    return {"ok": p.returncode == 0,
            "stdout": out.decode("utf-8", "replace")[-1500:],
            "stderr": err.decode("utf-8", "replace")[-1500:]}


# ── slides from markdown (marp) ────────────────────────────────────────────
async def marp_slides(markdown: str, name: str = "slides",
                      fmt: str = "pdf") -> dict:
    """fmt: pdf | html | pptx"""
    marp = _tool("marp") or _tool("marp-cli")
    src = OUT_DIR / f"{name}.md"
    if not markdown.lstrip().startswith("---"):
        markdown = "---\nmarp: true\npaginate: true\n---\n\n" + markdown
    src.write_text(markdown, "utf-8")
    dst = OUT_DIR / f"{name}.{fmt}"

    if marp:
        flag = {"pdf": "--pdf", "html": "--html", "pptx": "--pptx"}.get(fmt, "--pdf")
        r = await _sh([marp, str(src), flag, "--allow-local-files",
                       "-o", str(dst)], 240)
        if r.get("ok") and dst.exists():
            return {"ok": True, "path": str(dst), "tool": "marp"}
        err = r.get("stderr") or r.get("error")
    else:
        err = "marp not installed"

    # fallback: render the markdown to simple HTML, convert with LibreOffice
    fb = await html_to_pdf(_md_to_html(markdown), name)
    if fb.get("ok"):
        return {**fb, "note": f"marp unavailable ({err}); used LibreOffice"}
    return {"ok": False, "error": err}


def _md_to_html(md: str) -> str:
    """Minimal markdown -> HTML (no dependency); good enough for slides/reports."""
    import html as _h
    import re
    body, in_ul = [], False
    for raw in md.splitlines():
        if raw.strip().startswith("---"):
            body.append("<hr/>")
            continue
        line = _h.escape(raw)
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"\*(.+?)\*", r"<i>\1</i>", line)
        line = re.sub(r"`(.+?)`", r"<code>\1</code>", line)
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            if in_ul:
                body.append("</ul>"); in_ul = False
            body.append(f"<h{len(m.group(1))}>{m.group(2)}</h{len(m.group(1))}>")
        elif re.match(r"^\s*[-*]\s+", line):
            if not in_ul:
                body.append("<ul>"); in_ul = True
            body.append("<li>" + re.sub(r"^\s*[-*]\s+", "", line) + "</li>")
        elif line.strip():
            if in_ul:
                body.append("</ul>"); in_ul = False
            body.append(f"<p>{line}</p>")
    if in_ul:
        body.append("</ul>")
    return ("<!doctype html><meta charset='utf-8'>"
            "<style>body{font-family:sans-serif;margin:40px;line-height:1.5}"
            "h1,h2{color:#222}code{background:#eee;padding:2px 4px}</style>"
            + "\n".join(body))


# ── pptx built directly (python-pptx) ──────────────────────────────────────
def pptx_from_markdown(markdown: str, name: str = "deck") -> dict:
    """Each '# Heading' starts a slide; bullets become bullet points."""
    try:
        from pptx import Presentation
        from pptx.util import Pt
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"python-pptx not installed ({e})"}

    prs = Presentation()
    blank, bullet = prs.slide_layouts[5], prs.slide_layouts[1]
    title, points = None, []

    def flush():
        nonlocal title, points
        if title is None:
            return
        s = prs.slides.add_slide(bullet if points else blank)
        s.shapes.title.text = title[:120]
        if points and len(s.placeholders) > 1:
            tf = s.placeholders[1].text_frame
            tf.text = points[0][:200]
            for p in points[1:12]:
                para = tf.add_paragraph()
                para.text = p[:200]
                para.level = 0
                para.font.size = Pt(18)
        title, points = None, []

    for raw in markdown.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            flush()
            title = line.lstrip("#").strip()
        elif line.startswith(("-", "*")) and title is not None:
            points.append(line.lstrip("-* ").strip())
        elif line and title is not None and len(points) < 12:
            points.append(line)
    flush()

    if not prs.slides:
        return {"ok": False, "error": "no '# headings' found to make slides from"}
    dst = OUT_DIR / f"{name}.pptx"
    prs.save(str(dst))
    return {"ok": True, "path": str(dst), "slides": len(prs.slides._sldIdLst),
            "tool": "python-pptx"}


# ── html/slides -> pdf (decktape, else libreoffice) ────────────────────────
async def html_to_pdf(html: str, name: str = "doc") -> dict:
    src = OUT_DIR / f"{name}.html"
    src.write_text(html, "utf-8")
    dst = OUT_DIR / f"{name}.pdf"

    lo = _tool("libreoffice") or _tool("soffice")
    if lo:
        r = await _sh([lo, "--headless", "--convert-to", "pdf",
                       "--outdir", str(OUT_DIR), str(src)], 240)
        if dst.exists():
            return {"ok": True, "path": str(dst), "tool": "libreoffice"}
        return {"ok": False, "error": r.get("stderr") or r.get("error")}
    return {"ok": False, "error": "no html->pdf converter available"}


async def decktape_pdf(slides_url_or_file: str, name: str = "deck") -> dict:
    dt = _tool("decktape")
    if not dt:
        return {"ok": False, "error": "decktape not installed"}
    dst = OUT_DIR / f"{name}.pdf"
    r = await _sh([dt, "automatic", slides_url_or_file, str(dst)], 300)
    if dst.exists():
        return {"ok": True, "path": str(dst), "tool": "decktape"}
    return {"ok": False, "error": r.get("stderr") or r.get("error")}


# ── quarto reports ─────────────────────────────────────────────────────────
async def quarto_render(markdown: str, name: str = "report",
                        to: str = "html") -> dict:
    q = _tool("quarto")
    src = OUT_DIR / f"{name}.qmd"
    src.write_text(markdown, "utf-8")
    if not q:
        fb = await html_to_pdf(_md_to_html(markdown), name)
        return ({**fb, "note": "quarto not installed; used LibreOffice"}
                if fb.get("ok") else {"ok": False, "error": "quarto not installed"})
    r = await _sh([q, "render", str(src), "--to", to], 300)
    dst = OUT_DIR / f"{name}.{ 'pdf' if to == 'pdf' else to }"
    if dst.exists():
        return {"ok": True, "path": str(dst), "tool": "quarto"}
    return {"ok": False, "error": r.get("stderr") or r.get("error")}


def available() -> dict:
    try:
        import pptx  # noqa: F401
        has_pptx = True
    except Exception:  # noqa: BLE001
        has_pptx = False
    return {"marp": bool(_tool("marp")), "decktape": bool(_tool("decktape")),
            "quarto": bool(_tool("quarto")), "python-pptx": has_pptx,
            "libreoffice": bool(_tool("libreoffice") or _tool("soffice")),
            "chromium": bool(_CHROME), "out_dir": str(OUT_DIR)}
