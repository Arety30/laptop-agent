#!/usr/bin/env python3
"""Build Abrar's writing-style corpus for the email composer (no LLM, no training — light on RAM).

Sources (all optional, whatever exists is used):
  • Sent folders: Gmail (GMAIL_n_* in proton.env, IMAP) and every hydroxide/Proton account
    (accounts.json, IMAP on 127.0.0.1:1144)                                -> emails
  • workspace/mail_style/sources/*.mbox  (Google Takeout Mail)              -> emails (only ones Abrar wrote)
  • workspace/mail_style/sources/**/conversations.json  (ChatGPT export)   -> voice (Abrar's prompts)
  • workspace/mail_style/sources/**/MyActivity.{json,html} (Gemini Takeout) -> voice
  • YouTube Takeout comments.csv, twin/messages.jsonl, logs/bot.log         -> voice

Outputs in workspace/mail_style/:
  emails.jsonl   {to, subject, body, account, date}      voice.jsonl  {text, source}
  profile.json   greetings / sign-offs / length / habits  sft_email.jsonl  chat-format rows for a future LoRA

    ./.venv/bin/python mailstyle.py            (or `!mailtrain` in Discord)
"""
from __future__ import annotations

import csv
import email
import glob
import html as htmlmod
import imaplib
import json
import mailbox
import re
import ssl
import sys
from collections import Counter
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "workspace" / "mail_style"
SRC = OUT / "sources"
ENV = Path.home() / ".config/proton-mail-mcp/proton.env"
ACCTS = Path.home() / ".config/proton-mail-mcp/accounts.json"
MY_NAMES = ("abrar", "arety", "USER")


# ------------------------------------------------------------------ helpers
def _env() -> dict:
    d = {}
    if ENV.exists():
        for l in ENV.read_text().splitlines():
            if "=" in l and not l.lstrip().startswith("#"):
                k, v = l.split("=", 1); d[k.strip()] = v.strip().strip('"').strip("'")
    return d


def my_addresses() -> set[str]:
    e = _env()
    s = {v.lower() for k, v in e.items() if k.endswith("_EMAIL") and v}
    if ACCTS.exists():
        s |= {a["email"].lower() for a in json.loads(ACCTS.read_text())["accounts"]}
    return s


def _hdr(v) -> str:
    try:
        return str(make_header(decode_header(v))) if v else ""
    except Exception:
        return str(v or "")


def _strip_html(h: str) -> str:
    h = re.sub(r"(?is)<(script|style).*?</\1>", "", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", h)
    return htmlmod.unescape(re.sub(r"<[^>]+>", "", h))


def body_of(msg) -> str:
    plain, html_ = None, None
    for part in msg.walk():
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        try:
            p = part.get_payload(decode=True)
            t = p.decode(part.get_content_charset() or "utf-8", errors="replace") if p else ""
        except Exception:
            continue
        ct = part.get_content_type()
        if ct == "text/plain" and plain is None:
            plain = t
        elif ct == "text/html" and html_ is None:
            html_ = t
    return clean_body(plain if plain is not None else _strip_html(html_ or ""))


def clean_body(t: str) -> str:
    """Drop quoted replies, forwarded blocks and trailing signatures/boilerplate."""
    lines = []
    for l in t.replace("\r", "").split("\n"):
        if re.match(r"^\s*(>|On .{5,120} wrote:|-{3,} ?(Original|Forwarded) ?Message|From: .+@|Sent from my|Get Outlook)", l):
            break
        lines.append(l.rstrip())
    t = "\n".join(lines)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t


def looks_mine(addr: str, mine: set[str]) -> bool:
    a = addr.lower()
    return a in mine or any(n in a for n in MY_NAMES)


# ------------------------------------------------------------------ email collectors
def _imap_sent(conn: imaplib.IMAP4, folder: str, account: str, mine: set[str], limit: int) -> list[dict]:
    out = []
    typ, _ = conn.select(f'"{folder}"', readonly=True)
    if typ != "OK":
        return out
    typ, d = conn.uid("search", None, "ALL")
    for uid in d[0].split()[-limit:]:
        typ, data = conn.uid("fetch", uid, "(BODY.PEEK[])")
        raw = next((x[1] for x in data if isinstance(x, tuple)), None)
        if not raw:
            continue
        m = email.message_from_bytes(raw)
        frm = parseaddr(m.get("From", ""))[1]
        if not looks_mine(frm, mine):
            continue
        b = body_of(m)
        if len(b.split()) < 3:
            continue
        try:
            date = parsedate_to_datetime(m.get("Date")).date().isoformat()
        except Exception:
            date = ""
        out.append({"account": account, "from": frm, "to": _hdr(m.get("To")), "subject": _hdr(m.get("Subject")),
                    "body": b[:4000], "date": date})
    return out


def collect_gmail(mine: set[str], limit=300) -> list[dict]:
    e, out = _env(), []
    for i in range(1, 6):
        addr, pw = e.get(f"GMAIL_{i}_EMAIL"), e.get(f"GMAIL_{i}_PASSWORD", "").replace(" ", "")
        if not addr or not pw:
            continue
        try:
            c = imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=ssl.create_default_context())
            c.login(addr, pw)
            out += _imap_sent(c, "[Gmail]/Sent Mail", addr, mine, limit)
            c.logout()
        except Exception as ex:  # noqa: BLE001
            print(f"  gmail {addr}: {str(ex)[:80]}", file=sys.stderr)
    return out


def collect_proton(mine: set[str], limit=300) -> list[dict]:
    out = []
    if not ACCTS.exists():
        return out
    for a in json.loads(ACCTS.read_text())["accounts"]:
        if a.get("provider") != "hydroxide":
            continue
        try:
            c = imaplib.IMAP4("127.0.0.1", 1144)
            c.login(a["email"], a["password"])
            out += _imap_sent(c, "Sent", a["email"], mine, limit)
            c.logout()
        except Exception as ex:  # noqa: BLE001
            print(f"  proton {a['email']}: {str(ex)[:80]}", file=sys.stderr)
    return out


def collect_mbox(mine: set[str]) -> list[dict]:
    out = []
    for f in glob.glob(str(SRC / "**" / "*.mbox"), recursive=True):
        for m in mailbox.mbox(f):
            frm = parseaddr(m.get("From", ""))[1]
            if not looks_mine(frm, mine):
                continue
            b = body_of(m)
            if len(b.split()) < 3:
                continue
            out.append({"account": frm, "from": frm, "to": _hdr(m.get("To")), "subject": _hdr(m.get("Subject")),
                        "body": b[:4000], "date": ""})
    return out


# ------------------------------------------------------------------ voice collectors
def _voice_ok(t: str) -> bool:
    t = t.strip()
    return 3 <= len(t.split()) <= 120 and not t.startswith(("Traceback", "$ ", "http")) and "```" not in t


def collect_chatgpt() -> list[dict]:
    out = []
    for f in glob.glob(str(SRC / "**" / "conversations.json"), recursive=True):
        try:
            convs = json.loads(Path(f).read_text())
        except Exception:
            continue
        for conv in convs:
            for node in (conv.get("mapping") or {}).values():
                msg = node.get("message") or {}
                if (msg.get("author") or {}).get("role") != "user":
                    continue
                parts = (msg.get("content") or {}).get("parts") or []
                text = " ".join(p for p in parts if isinstance(p, str)).strip()
                if _voice_ok(text):
                    out.append({"text": text[:1500], "source": "chatgpt"})
    return out


def collect_gemini() -> list[dict]:
    out = []
    for f in glob.glob(str(SRC / "**" / "MyActivity.json"), recursive=True):
        try:
            for item in json.loads(Path(f).read_text()):
                t = item.get("title", "")
                if t.startswith("Prompted "):
                    t = t[len("Prompted "):]
                    if _voice_ok(t):
                        out.append({"text": t[:1500], "source": "gemini"})
        except Exception:
            pass
    for f in glob.glob(str(SRC / "**" / "MyActivity.html"), recursive=True):
        try:
            for t in re.findall(r"Prompted (.+?)<br", Path(f).read_text(errors="replace")):
                t = htmlmod.unescape(re.sub(r"<[^>]+>", "", t)).strip()
                if _voice_ok(t):
                    out.append({"text": t[:1500], "source": "gemini"})
        except Exception:
            pass
    return out


def collect_misc() -> list[dict]:
    out = []
    for f in glob.glob("~/Turbowarp/**/comments.csv", recursive=True) + glob.glob(str(SRC / "**" / "comments.csv"), recursive=True):
        try:
            for row in csv.DictReader(open(f, encoding="utf-8", errors="replace")):
                raw = row.get("Comment text", "")
                try:
                    raw = json.loads(raw).get("text", raw)
                except Exception:
                    pass
                if _voice_ok(raw):
                    out.append({"text": raw, "source": "youtube"})
        except Exception:
            pass
    tw = HERE / "twin" / "messages.jsonl"
    if tw.exists():
        for l in tw.read_text().splitlines():
            try:
                t = json.loads(l).get("abrar", "")
                if _voice_ok(t):
                    out.append({"text": t[:800], "source": "twin"})
            except Exception:
                pass
    log = HERE / "logs" / "bot.log"
    if log.exists():
        for m in re.finditer(r"MSG from \S+ \(id=\d+\) in \w+: '(.+?)'\n", log.read_text(errors="replace")):
            t = m.group(1)
            if _voice_ok(t) and not t.startswith("!"):
                out.append({"text": t[:800], "source": "discord"})
    return out


# ------------------------------------------------------------------ profile
def build_profile(emails: list[dict], voice: list[dict]) -> dict:
    greet, signoff, lengths, lower, excl = Counter(), Counter(), [], 0, 0
    for e in emails:
        lines = [l.strip() for l in e["body"].split("\n") if l.strip()]
        if not lines:
            continue
        if re.match(r"^(hi|hello|hey|dear|good (morning|afternoon|evening)|salam|assalam)", lines[0], re.I):
            greet[re.sub(r"[,!.].*$", "", lines[0]).strip()[:30]] += 1
        for l in lines[-3:]:
            if re.match(r"^(thanks|thank you|best|regards|kind regards|cheers|sincerely|take care|jazak)", l, re.I):
                signoff[l.strip(" ,.")[:30]] += 1
        lengths.append(len(e["body"].split()))
        lower += e["body"] == e["body"].lower()
        excl += "!" in e["body"]
    vtexts = [v["text"] for v in voice]
    v_lower = sum(t == t.lower() for t in vtexts)
    words = Counter(w for t in vtexts for w in re.findall(r"[a-z']{3,}", t.lower()))
    stop = set("the and for you that this with have are was but not can what how just like your from about".split())
    return {
        "email_count": len(emails), "voice_count": len(voice),
        "greetings": greet.most_common(5), "sign_offs": signoff.most_common(5),
        "avg_email_words": round(sum(lengths) / len(lengths)) if lengths else None,
        "email_all_lowercase_ratio": round(lower / len(emails), 2) if emails else None,
        "voice_all_lowercase_ratio": round(v_lower / len(vtexts), 2) if vtexts else None,
        "uses_exclamation_ratio": round(excl / len(emails), 2) if emails else None,
        "favourite_words": [w for w, _ in words.most_common(40) if w not in stop][:20],
        "sources": Counter(v["source"] for v in voice),
    }


def style_summary(p: dict) -> str:
    """Human-readable rules derived from the profile, injected into the composer prompt."""
    bits = []
    if p.get("greetings"):
        bits.append("Usual greeting: " + " / ".join(g for g, _ in p["greetings"][:2]))
    if p.get("sign_offs"):
        bits.append("Usual sign-off: " + " / ".join(s for s, _ in p["sign_offs"][:2]))
    if p.get("avg_email_words"):
        bits.append(f"Typical length: about {p['avg_email_words']} words")
    if (p.get("voice_all_lowercase_ratio") or 0) > 0.6:
        bits.append("Casual voice: mostly lowercase, short sentences, direct — but emails to strangers stay properly capitalised")
    if (p.get("uses_exclamation_ratio") or 0) > 0.4:
        bits.append("Comfortable with an exclamation mark for thanks")
    return "\n".join(f"- {b}" for b in bits)


# ------------------------------------------------------------------ main
def build() -> dict:
    OUT.mkdir(parents=True, exist_ok=True); SRC.mkdir(exist_ok=True)
    mine = my_addresses()
    emails = collect_gmail(mine) + collect_proton(mine) + collect_mbox(mine)
    seen, uniq = set(), []
    for e in emails:
        k = (e["subject"], e["body"][:120])
        junk = e["subject"].startswith("[laptop-agent]") or re.search(r"\btest\b", e["subject"], re.I) or len(e["body"].split()) < 8
        if k not in seen and not junk:
            seen.add(k); uniq.append(e)
    voice = collect_chatgpt() + collect_gemini() + collect_misc()
    vseen, vuniq = set(), []
    for v in voice:
        if v["text"] not in vseen:
            vseen.add(v["text"]); vuniq.append(v)
    prof = build_profile(uniq, vuniq)
    (OUT / "emails.jsonl").write_text("\n".join(json.dumps(e) for e in uniq))
    (OUT / "voice.jsonl").write_text("\n".join(json.dumps(v) for v in vuniq))
    (OUT / "profile.json").write_text(json.dumps(prof, indent=2, default=list))
    sft = [{"messages": [
        {"role": "system", "content": "You write emails as Abrar, in his own style."},
        {"role": "user", "content": f"Write an email to {e['to']} about: {e['subject']}"},
        {"role": "assistant", "content": f"Subject: {e['subject']}\n\n{e['body']}"}]} for e in uniq]
    (OUT / "sft_email.jsonl").write_text("\n".join(json.dumps(r) for r in sft))
    return prof


if __name__ == "__main__":
    p = build()
    print(json.dumps({k: v for k, v in p.items() if k != "favourite_words"}, indent=1, default=list))
    print("\nstyle rules:\n" + style_summary(p))
