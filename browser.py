"""Real browser automation via Playwright.

Until now `<open>` could only fetch raw HTML — useless for anything rendered by
JavaScript. This drives an actual Chromium: it can read SPA pages, screenshot
them (so the vision model can look), and fill/click.

Uses the system chromium already on the laptop rather than downloading
Playwright's own ~300MB browser bundle.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import config

SHOTS = config.WORKSPACE / "outputs"
SHOTS.mkdir(parents=True, exist_ok=True)

_CHROME = (shutil.which("chromium") or shutil.which("chromium-browser")
           or shutil.which("google-chrome"))

# Chromium ALWAYS runs on a private virtual display so a window NEVER opens on
# the real desktop. The bot's env has DISPLAY=:0 (the session) — we override it.
_VDISPLAY = os.getenv("BROWSER_DISPLAY", ":99")


def _ensure_display() -> None:
    """Guarantee an invisible Xvfb display is up and route Chromium to it."""
    if shutil.which("Xvfb") is None:
        return  # no xvfb — nothing we can do; leave DISPLAY as-is
    num = _VDISPLAY.lstrip(":")
    if not os.path.exists(f"/tmp/.X11-unix/X{num}"):
        try:
            subprocess.Popen(
                ["Xvfb", _VDISPLAY, "-screen", "0", "1280x900x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            time.sleep(1.2)
        except Exception:  # noqa: BLE001
            return
    os.environ["DISPLAY"] = _VDISPLAY   # force chromium onto the virtual display


def available() -> dict:
    try:
        import playwright  # noqa: F401
        ok = True
    except Exception:  # noqa: BLE001
        ok = False
    return {"playwright": ok, "chromium": _CHROME or None}


_UA_REAL = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


async def _page(pw, headless: bool = True):
    """Use Playwright's OWN bundled Chromium (not the confined snap desktop one),
    ALWAYS on the virtual display so no window ever reaches the real desktop.
    Set BROWSER_USE_SYSTEM_CHROMIUM=1 to force the system binary instead."""
    _ensure_display()                                 # DISPLAY -> :99 (invisible)
    launch: dict = {"headless": headless,
                    "args": ["--no-sandbox", "--disable-dev-shm-usage",
                             "--disable-blink-features=AutomationControlled"]}
    if os.getenv("BROWSER_USE_SYSTEM_CHROMIUM") and _CHROME:
        launch["executable_path"] = _CHROME          # opt-in fallback only
    browser = await pw.chromium.launch(**launch)      # else Playwright's bundled chromium
    ctx = await browser.new_context(viewport={"width": 1280, "height": 900},
                                    user_agent=_UA_REAL, locale="en-US")
    return browser, await ctx.new_page()


async def read(url: str, max_chars: int = 6000, wait_ms: int = 2500) -> dict:
    """Load a URL in a real browser and return the RENDERED text."""
    try:
        from playwright.async_api import async_playwright
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"playwright not installed ({e})"}
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        async with async_playwright() as pw:
            browser, page = await _page(pw)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(wait_ms)
                title = await page.title()
                text = await page.evaluate(
                    "() => document.body ? document.body.innerText : ''")
            finally:
                await browser.close()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:300]}
    text = "\n".join(l.strip() for l in (text or "").splitlines() if l.strip())
    return {"ok": True, "url": url, "title": title, "text": text[:max_chars]}


def _unbing(href: str) -> str:
    """Bing wraps results in /ck/a?...&u=a1<base64>. Decode to the real URL."""
    import base64
    import urllib.parse
    if "bing.com/ck/a" not in href:
        return href
    u = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("u", [""])[0]
    if u.startswith("a1"):
        try:
            data = u[2:]
            data += "=" * (-len(data) % 4)
            return base64.urlsafe_b64decode(data).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
    return href


_GOOGLE_EXTRACT = """() => {
    const out = [], seen = new Set();
    document.querySelectorAll('#search h3, #rso h3').forEach(h3 => {
        const a = h3.closest('a'); if (!a) return;
        const href = a.href;
        if (!href || href.includes('google.') || href.includes('gstatic') || seen.has(href)) return;
        const g = a.closest('.g, div[data-hveid]') || a.parentElement;
        const sn = g ? g.querySelector('.VwiC3b, div[data-sncf], [data-content-feature] span') : null;
        seen.add(href);
        out.push({title: h3.innerText, url: href, snippet: sn ? sn.innerText.slice(0, 260) : ''});
    });
    return out.slice(0, 10);
}"""


async def search(query: str, n: int = 6) -> list[dict]:
    """GOOGLE search via Chromium on the invisible virtual display (:99, no window
    ever). Returns [{title, url, snippet}]; empty on failure so callers fall back."""
    try:
        from playwright.async_api import async_playwright
    except Exception:  # noqa: BLE001
        return []
    import urllib.parse
    url = "https://www.google.com/search?q=" + urllib.parse.quote(query) + "&hl=en&num=10"
    try:
        async with async_playwright() as pw:
            browser, page = await _page(pw, headless=False)   # headed on :99 so Google renders
            try:
                for _ in range(2):
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                        break
                    except Exception:  # noqa: BLE001
                        await page.wait_for_timeout(1200)
                await page.wait_for_timeout(1400)
                raw = await page.evaluate(_GOOGLE_EXTRACT)
            finally:
                await browser.close()
    except Exception:  # noqa: BLE001
        return []
    seen, results = set(), []
    for r in raw:
        u = r.get("url") or ""
        if u and u not in seen:
            seen.add(u)
            results.append({"title": (r.get("title") or "").strip(),
                            "url": u, "snippet": (r.get("snippet") or "").strip()})
        if len(results) >= n:
            break
    return results


async def read_top(query: str, n: int = 3, chars: int = 1800) -> dict:
    """OpenManus-style: GOOGLE the query, then OPEN & READ the top N result pages —
    all in one Chromium session on the invisible virtual display.
    Returns {results:[{title,url,snippet}], pages:[{url,title,text}]}."""
    try:
        from playwright.async_api import async_playwright
    except Exception:  # noqa: BLE001
        return {"results": [], "pages": []}
    import urllib.parse
    gurl = "https://www.google.com/search?q=" + urllib.parse.quote(query) + "&hl=en&num=10"
    results, pages = [], []
    try:
        async with async_playwright() as pw:
            browser, page = await _page(pw, headless=False)
            try:
                for _ in range(2):
                    try:
                        await page.goto(gurl, wait_until="domcontentloaded", timeout=25000)
                        break
                    except Exception:  # noqa: BLE001
                        await page.wait_for_timeout(1200)
                await page.wait_for_timeout(1400)
                results = await page.evaluate(_GOOGLE_EXTRACT)
                for r in results[:n]:
                    try:
                        await page.goto(r["url"], wait_until="domcontentloaded", timeout=18000)
                        await page.wait_for_timeout(900)
                        title = await page.title()
                        text = await page.evaluate(
                            "() => document.body ? document.body.innerText : ''")
                        text = "\n".join(l.strip() for l in (text or "").splitlines() if l.strip())
                        pages.append({"url": r["url"], "title": title, "text": text[:chars]})
                    except Exception:  # noqa: BLE001
                        continue
            finally:
                await browser.close()
    except Exception:  # noqa: BLE001
        pass
    return {"results": results, "pages": pages}


async def screenshot(url: str, name: str = "page", full: bool = True) -> dict:
    """Screenshot a page — pair with vision.analyze() to actually 'see' it."""
    try:
        from playwright.async_api import async_playwright
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"playwright not installed ({e})"}
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    dst = SHOTS / f"{name}.png"
    try:
        async with async_playwright() as pw:
            browser, page = await _page(pw)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(2500)
                await page.screenshot(path=str(dst), full_page=full)
            finally:
                await browser.close()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:300]}
    return {"ok": True, "path": str(dst), "url": url}


async def pdf(url: str, name: str = "page") -> dict:
    """Print a page to PDF (headless chromium only)."""
    try:
        from playwright.async_api import async_playwright
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"playwright not installed ({e})"}
    dst = SHOTS / f"{name}.pdf"
    try:
        async with async_playwright() as pw:
            browser, page = await _page(pw)
            try:
                await page.goto(url, wait_until="networkidle", timeout=40000)
                await page.pdf(path=str(dst), format="A4", print_background=True)
            finally:
                await browser.close()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:300]}
    return {"ok": True, "path": str(dst)}
