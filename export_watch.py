#!/usr/bin/env python3
"""Watch a Gmail inbox for the ChatGPT "data export ready" email, download the zip before the
24h link expires, unpack it into workspace/mail_style/sources/chatgpt/, rebuild the style corpus.

    /usr/bin/python3 export_watch.py user@example.com   (loops until done; logs to stdout)
"""
import email
import imaplib
import json
import re
import ssl
import subprocess
import sys
import time
import urllib.request
import zipfile
from datetime import datetime
from email.header import decode_header, make_header
from pathlib import Path

HERE = Path(__file__).parent
SRC = HERE / "workspace" / "mail_style" / "sources"
STATE = SRC / ".export_watch_seen"
ENV = Path.home() / ".config/proton-mail-mcp/proton.env"
INTERVAL = 120
START = datetime.now()
LINK_RE = re.compile(r"https://chatgpt\.com/backend-api/estuary/content\?[^\s\"'<>]+")


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def creds(addr: str) -> str:
    env = {}
    for l in ENV.read_text().splitlines():
        if "=" in l and not l.lstrip().startswith("#"):
            k, v = l.split("=", 1); env[k.strip()] = v.strip()
    for i in range(1, 6):
        if env.get(f"GMAIL_{i}_EMAIL", "").lower() == addr.lower():
            return env.get(f"GMAIL_{i}_PASSWORD", "").replace(" ", "")
    sys.exit(f"no app password for {addr} in {ENV}")


def _text(msg) -> str:
    out = []
    for part in msg.walk():
        if part.get_content_type() in ("text/plain", "text/html"):
            try:
                p = part.get_payload(decode=True)
                out.append(p.decode(part.get_content_charset() or "utf-8", errors="replace"))
            except Exception:
                pass
    return "\n".join(out)


def find_link(addr: str, pw: str) -> str | None:
    seen = set(STATE.read_text().split()) if STATE.exists() else set()
    c = imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=ssl.create_default_context())
    c.login(addr, pw)
    try:
        for folder in ('"INBOX"', '"[Gmail]/Spam"', '"[Gmail]/All Mail"'):
            if c.select(folder, readonly=True)[0] != "OK":
                continue
            since = START.strftime("%d-%b-%Y")   # ignore old export mails whose links are long expired
            typ, d = c.uid("search", None, f'SINCE {since} OR (FROM "openai.com") (SUBJECT "export")')
            for uid in d[0].split()[-20:]:
                key = f"{folder}:{uid.decode()}"
                if key in seen:
                    continue
                typ, data = c.uid("fetch", uid, "(BODY.PEEK[])")
                raw = next((x[1] for x in data if isinstance(x, tuple)), None)
                if not raw:
                    continue
                m = email.message_from_bytes(raw)
                subj = str(make_header(decode_header(m.get("Subject", ""))))
                body = _text(m)
                links = LINK_RE.findall(body.replace("&amp;", "&"))
                if links:
                    log(f"found export mail: {subj!r} in {folder}")
                    seen.add(key); STATE.write_text(" ".join(seen))
                    return links[0]
    finally:
        c.logout()
    return None


def download(url: str) -> Path | None:
    dest = SRC / "chatgpt-export.zip"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=600) as r:
        data = r.read()
    if not data.startswith(b"PK"):
        log("download was not a zip:", data[:120]); return None
    dest.write_bytes(data)
    log(f"downloaded {len(data)/1e6:.1f} MB -> {dest}")
    out = SRC / "chatgpt"; out.mkdir(exist_ok=True)
    with zipfile.ZipFile(dest) as z:
        names = [n for n in z.namelist() if n.endswith((".json", ".html"))]
        z.extractall(out, members=names)
    log(f"unpacked {len(names)} files into {out}")
    return out


def main():
    addr = sys.argv[1] if len(sys.argv) > 1 else "user@example.com"
    pw = creds(addr)
    SRC.mkdir(parents=True, exist_ok=True)
    log(f"watching {addr} for the ChatGPT export link (every {INTERVAL}s)")
    while True:
        try:
            url = find_link(addr, pw)
            if url:
                out = download(url)
                if out:
                    r = subprocess.run([str(HERE / ".venv/bin/python"), str(HERE / "mailstyle.py")],
                                       capture_output=True, text=True, timeout=600)
                    log("style corpus rebuilt:", (r.stdout or r.stderr).strip()[-400:])
                    log("done — exiting")
                    return
        except Exception as e:  # noqa: BLE001
            log("error:", str(e)[:200])
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
