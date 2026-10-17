"""Real web search + page reading, using only the Python standard library
(no extra pip deps). Search hits DuckDuckGo's HTML endpoints; page reading
fetches and strips a URL to readable text. Used by the chatbot and the
web-viewing agent so they answer from real data instead of hallucinating."""
from __future__ import annotations

import asyncio
import html as _html
import json
import os
import re
import urllib.parse
import urllib.request

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Only launch Chromium for search when at least this much RAM is free, so a
# browser launch can never OOM/overheat this 7GB laptop. ~1.4GB headroom.
_BROWSER_MIN_MB = int(os.getenv("BROWSER_SEARCH_MIN_MB", "1400"))


def _free_mb() -> int:
    try:
        for ln in open("/proc/meminfo"):
            if ln.startswith("MemAvailable:"):
                return int(ln.split()[1]) // 1024
    except Exception:  # noqa: BLE001
        return 0
    return 0


# ── search ─────────────────────────────────────────────────────────────────
async def search(query: str, n: int = 5) -> list[dict]:
    """Combine the most reliable sources: DuckDuckGo Instant Answer (facts) +
    a REAL Chromium browser (Bing, renders JS, no rate-limits) + HTTP scrape
    as a last resort. This is why we no longer depend on flaky DDG scraping."""
    results: list[dict] = []
    seen: set[str] = set()

    def _add(items):
        for it in items or []:
            key = (it.get("url") or it.get("title", "")).lower()
            if key and key not in seen:
                seen.add(key)
                results.append(it)

    # 0) Google (Gemini with Google Search grounding) — Abrar's choice of search engine.
    #    When it answers, the DuckDuckGo/Bing fallbacks below are skipped.
    try:
        _add(await asyncio.to_thread(_google, query, n))
    except Exception:  # noqa: BLE001
        pass
    if results:
        return results[:n]

    # 1) Instant Answer API — fast + authoritative for factual questions
    try:
        ia = await asyncio.to_thread(_instant_answer, query)
        _add([ia] if ia else [])
    except Exception:  # noqa: BLE001
        pass

    # 2) Chromium GOOGLE search on the INVISIBLE display :99 (browser.py forces
    #    DISPLAY=:99 so no window ever opens on the real desktop). Lightweight:
    #    Chromium only, no extra model. Skipped when RAM is tight so a browser
    #    launch can never OOM/overheat the laptop. Force on/off with BROWSER_SEARCH.
    force = os.getenv("BROWSER_SEARCH")  # "1" = always, "0" = never
    use_browser = force == "1" or (force != "0" and _free_mb() >= _BROWSER_MIN_MB)
    if use_browser:
        try:
            import browser
            _add(await browser.search(query, n))
        except Exception:  # noqa: BLE001
            pass

    # 3) HTTP scrape fallback if still thin
    if len(results) < 2:
        try:
            _add(await asyncio.to_thread(_search_sync, query, n))
        except Exception:  # noqa: BLE001
            pass
    return results[:n]


def _gemini_key() -> str:
    key = os.getenv("GEMINI_API_KEY", "")
    if not key:
        try:
            for ln in open(os.path.expanduser("~/Workspace/main.env")):
                if ln.startswith("GEMINI_API_KEY="):
                    key = ln.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
    return key


def _google(query: str, n: int) -> list[dict]:
    """Google Search through Gemini's grounding tool (the plain Custom Search API is blocked for
    this key). Returns Google's answer first, then the pages it was based on."""
    key = _gemini_key()
    if not key:
        return []
    body = {"contents": [{"parts": [{"text": (
                "Search Google and answer this with the specific facts found (names, places, numbers). "
                "Be brief, no more than five short lines: " + query)}]}],
            "tools": [{"google_search": {}}]}
    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "x-goog-api-key": key})
    with urllib.request.urlopen(req, timeout=25) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    cand = (d.get("candidates") or [{}])[0]
    answer = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", [])).strip()
    chunks = [c.get("web", {}) for c in (cand.get("groundingMetadata") or {}).get("groundingChunks", [])]
    if not answer or not chunks:
        return []       # not grounded in a real search: let the other sources try
    out = [{"title": "Google: " + query, "url": "https://www.google.com/search?q=" + urllib.parse.quote(query),
            "snippet": answer}]
    for c in chunks[: max(0, n - 1)]:
        out.append({"title": c.get("title", ""), "url": "", "snippet": ""})   # the uri is a long Google redirect, useless to read out
    return out


def _instant_answer(query: str) -> dict | None:
    """DuckDuckGo Instant Answer API — reliable JSON, no scraping/rate-limits.
    Great for facts (capitals, people, definitions)."""
    url = "https://api.duckduckgo.com/?" + urllib.parse.urlencode(
        {"q": query, "format": "json", "no_html": 1, "skip_disambig": 1})
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=12) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    ans = (d.get("Answer") or d.get("AbstractText") or "").strip()
    src = d.get("AbstractURL") or ""
    if not ans:
        for t in (d.get("RelatedTopics") or []):
            if isinstance(t, dict) and t.get("Text"):
                ans, src = t["Text"], t.get("FirstURL", src)
                break
    if ans:
        return {"title": d.get("Heading") or query, "url": src, "snippet": ans}
    return None


def _search_sync(query: str, n: int) -> list[dict]:
    results: list[dict] = []
    seen: set[str] = set()

    def _add(items):
        for it in items:
            key = (it.get("url") or it.get("title", "")).lower()
            if key and key not in seen:
                seen.add(key)
                results.append(it)

    # 1) Instant Answer API — reliable, authoritative snippet for facts (retry once)
    for _ in range(2):
        try:
            ia = _instant_answer(query)
            if ia:
                _add([ia])
                break
        except Exception:  # noqa: BLE001
            continue

    # 2) optional ddgs lib if present
    try:
        from ddgs import DDGS  # type: ignore
        with DDGS() as d:
            _add([{"title": r.get("title", ""), "url": r.get("href", ""),
                   "snippet": r.get("body", "")} for r in d.text(query, max_results=n)])
    except Exception:  # noqa: BLE001
        pass

    # 3) HTML scrape (lite first — more stable), retry, accept only non-empty
    if len(results) < n:
        for _attempt in range(2):
            for endpoint in ("https://lite.duckduckgo.com/lite/",
                             "https://html.duckduckgo.com/html/"):
                try:
                    r = _ddg_scrape(endpoint, query, n)
                    if r:
                        _add(r)
                        break
                except Exception:  # noqa: BLE001
                    continue
            if len(results) >= n:
                break
    return results[:n]


def _ddg_scrape(endpoint: str, query: str, n: int) -> list[dict]:
    data = urllib.parse.urlencode({"q": query}).encode()
    req = urllib.request.Request(endpoint, data=data, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        page = r.read().decode("utf-8", "replace")

    results: list[dict] = []
    # result anchors: class="result__a" (html) or plain links (lite)
    for m in re.finditer(r'<a[^>]+class="result__a"[^>]*href="(.*?)"[^>]*>(.*?)</a>',
                         page, re.DOTALL):
        href, title = m.group(1), _clean(m.group(2))
        results.append({"title": title, "url": _unwrap(href), "snippet": ""})
        if len(results) >= n:
            break
    if not results:  # lite layout fallback
        for m in re.finditer(r'<a[^>]+href="(http[^"]+)"[^>]*class="result-link"[^>]*>(.*?)</a>',
                             page, re.DOTALL):
            results.append({"title": _clean(m.group(2)), "url": _unwrap(m.group(1)), "snippet": ""})
            if len(results) >= n:
                break
    # attach snippets when present
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', page, re.DOTALL)
    for i, s in enumerate(snippets[:len(results)]):
        results[i]["snippet"] = _clean(s)
    return results


def _unwrap(href: str) -> str:
    """DuckDuckGo wraps links as /l/?uddg=<encoded-real-url>."""
    if href.startswith("//"):
        href = "https:" + href
    q = urllib.parse.urlparse(href).query
    params = urllib.parse.parse_qs(q)
    if "uddg" in params:
        return params["uddg"][0]
    return href


# ── page reading ───────────────────────────────────────────────────────────
async def fetch(url: str, max_chars: int = 4000) -> dict:
    return await asyncio.to_thread(_fetch_sync, url, max_chars)


def _fetch_sync(url: str, max_chars: int) -> dict:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read(2_000_000).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "url": url, "error": str(e)}
    title, text = _extract(raw)
    return {"ok": True, "url": url, "title": title, "text": text[:max_chars]}


def _extract(page: str) -> tuple[str, str]:
    try:
        from bs4 import BeautifulSoup  # type: ignore
        soup = BeautifulSoup(page, "html.parser")
        for t in soup(["script", "style", "nav", "header", "footer", "noscript", "svg"]):
            t.extract()
        title = (soup.title.string.strip() if soup.title and soup.title.string else "")
        text = soup.get_text("\n")
    except Exception:  # noqa: BLE001
        tm = re.search(r"<title[^>]*>(.*?)</title>", page, re.DOTALL | re.IGNORECASE)
        title = _clean(tm.group(1)) if tm else ""
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", page)
        text = re.sub(r"(?s)<[^>]+>", "\n", text)
        text = _html.unescape(text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return title, "\n".join(lines)


def _clean(s: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def format_results(results: list[dict]) -> str:
    if not results:
        return "(no results found)"
    out = []
    for i, r in enumerate(results, 1):
        line = f"{i}. {r['title']}\n   {r['url']}"
        if r.get("snippet"):
            line += f"\n   {r['snippet'][:220]}"
        out.append(line)
    return "\n".join(out)
