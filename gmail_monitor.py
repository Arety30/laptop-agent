#!/usr/bin/env python3
"""Watch a Gmail inbox and print every new email — from, subject, and the body text.

    /usr/bin/python3 gmail_monitor.py [address] [--all]     # default: user@example.com
        --all   also dump the last 20 existing messages on startup (default: only NEW mail)

Prints to stdout and appends to workspace/mail_monitor.log. Uses the App Password from
~/.config/proton-mail-mcp/proton.env (GMAIL_n_EMAIL / GMAIL_n_PASSWORD). Polls every 30 s.
"""
import email
import html as htmlmod
import imaplib
import re
import ssl
import sys
import time
from datetime import datetime
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

ENV = Path.home() / ".config/proton-mail-mcp/proton.env"
LOG = Path(__file__).parent / "workspace" / "mail_monitor.log"
INTERVAL = 30
MAX_BODY = 3000


def creds(addr: str) -> str:
    env = {}
    for l in ENV.read_text().splitlines():
        if "=" in l and not l.lstrip().startswith("#"):
            k, v = l.split("=", 1); env[k.strip()] = v.strip()
    for i in range(1, 6):
        if env.get(f"GMAIL_{i}_EMAIL", "").lower() == addr.lower():
            return env.get(f"GMAIL_{i}_PASSWORD", "").replace(" ", "")
    sys.exit(f"no app password for {addr} in {ENV}")


def hdr(v) -> str:
    try:
        return str(make_header(decode_header(v))) if v else ""
    except Exception:
        return str(v or "")


def body_text(msg) -> str:
    plain, html_ = None, None
    for part in msg.walk():
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        try:
            p = part.get_payload(decode=True)
            t = p.decode(part.get_content_charset() or "utf-8", errors="replace") if p else ""
        except Exception:
            continue
        if part.get_content_type() == "text/plain" and plain is None:
            plain = t
        elif part.get_content_type() == "text/html" and html_ is None:
            html_ = t
    if plain is None:
        h = re.sub(r"(?is)<(script|style).*?</\1>", "", html_ or "")
        h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>", "\n", h)
        plain = htmlmod.unescape(re.sub(r"<[^>]+>", "", h))
    plain = plain.replace("\u034f", "").replace("\u200c", "").replace("\xad", "")   # invisible preheader filler
    plain = re.sub(r"\(\s*https?://\S+\s*\)", "", plain)                              # tracking links in ( )
    plain = re.sub(r"https?://\S{60,}", "[link]", plain)                                # very long URLs
    plain = re.sub(r"[ \t]+", " ", plain)
    plain = re.sub(r"\n\s*\n+", "\n\n", plain).strip()
    return plain[:MAX_BODY] + ("\n…[truncated]" if len(plain) > MAX_BODY else "")


def show(uid: str, msg) -> None:
    try:
        date = parsedate_to_datetime(msg.get("Date")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        date = msg.get("Date", "")
    name, addr = parseaddr(hdr(msg.get("From")))
    atts = [hdr(p.get_filename()) for p in msg.walk() if p.get_filename()]
    block = (f"\n{'=' * 78}\n📩 NEW MAIL  uid={uid}  {date}\n"
             f"From:    {name} <{addr}>\nTo:      {hdr(msg.get('To'))}\nSubject: {hdr(msg.get('Subject'))}\n"
             + (f"Attachments: {', '.join(atts)}\n" if atts else "")
             + f"{'-' * 78}\n{body_text(msg)}\n{'=' * 78}\n")
    print(block, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(block)


def connect(addr: str, pw: str) -> imaplib.IMAP4_SSL:
    c = imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=ssl.create_default_context())
    c.login(addr, pw)
    c.select("INBOX", readonly=True)
    return c


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    addr = args[0] if args else "user@example.com"
    dump_existing = "--all" in sys.argv
    pw = creds(addr)
    c = connect(addr, pw)
    typ, d = c.uid("search", None, "ALL")
    uids = d[0].split()
    last = int(uids[-1]) if uids else 0
    print(f"{datetime.now():%H:%M:%S} watching INBOX of {addr} — {len(uids)} messages, new mail printed every {INTERVAL}s "
          f"(log: {LOG})", flush=True)
    if dump_existing:
        for u in uids[-20:]:
            typ, data = c.uid("fetch", u, "(BODY.PEEK[])")
            raw = next((x[1] for x in data if isinstance(x, tuple)), None)
            if raw:
                show(u.decode(), email.message_from_bytes(raw))
    while True:
        time.sleep(INTERVAL)
        try:
            try:
                c.noop()
            except Exception:
                c = connect(addr, pw)
            typ, d = c.uid("search", None, f"UID {last + 1}:*")
            for u in d[0].split():
                if int(u) <= last:
                    continue
                typ, data = c.uid("fetch", u, "(BODY.PEEK[])")
                raw = next((x[1] for x in data if isinstance(x, tuple)), None)
                if raw:
                    show(u.decode(), email.message_from_bytes(raw))
                last = max(last, int(u))
        except Exception as e:  # noqa: BLE001
            print(f"{datetime.now():%H:%M:%S} error: {str(e)[:150]} — reconnecting", flush=True)
            try:
                c = connect(addr, pw)
            except Exception:
                pass


if __name__ == "__main__":
    main()
