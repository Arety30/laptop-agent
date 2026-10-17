"""Turn a natural-language request into a properly formatted email.

    draft = await compose("from arety to user@example.com saying the meeting moved to 5pm")
    -> {"account": "arety", "to": "user@example.com", "subject": "...", "body": "...",
        "purpose": "...", "register": "...", "warnings": [...]}

Pipeline:
  1. parse_request()  — deterministic: pull out account / to / subject / the actual message.
  2. mailbrain.build_plan() — decide purpose, audience, formality, structure, greeting, sign-off.
  3. the LLM fills prose INTO that plan (mailbrain.render_prompt); mailbrain.enforce() then
     guarantees the result obeys it (structure, register, greeting + sign-off, no markdown).
  4. no LLM? mailbrain.fallback_body() produces a correctly structured email anyway.
"""
from __future__ import annotations

import json
import re

import llm
import mailbrain
import mailstyle

SIGN_OFF_NAME = mailbrain.SIGN_OFF_NAME
STYLE_DIR = mailstyle.OUT


def _style_block(to: str | None, topic: str) -> str:
    """ONLY the short derived style rules (greeting/sign-off/length/voice).

    We deliberately do NOT feed the raw example emails to the model: a 1.5B copies
    few-shot examples verbatim into its answer (leaking real past emails). The example
    bank is still built by mailstyle for reference/fine-tuning, just not shown here.
    """
    try:
        prof = json.loads((STYLE_DIR / "profile.json").read_text())
    except Exception:
        return ""
    rules = mailstyle.style_summary(prof)
    return ("HOW ABRAR WRITES (match these habits, do NOT copy anyone's wording):\n" + rules) if rules else ""
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _accounts() -> dict[str, str]:
    """name -> email for every configured account."""
    try:
        from pathlib import Path
        d = json.loads((Path.home() / ".config/proton-mail-mcp/accounts.json").read_text())
        return {a["name"].lower(): a["email"].lower() for a in d["accounts"]}
    except Exception:  # noqa: BLE001
        return {}


def _resolve_account(token: str, accounts: dict[str, str]) -> str | None:
    t = token.lower().strip()
    if t in accounts:
        return t
    for name, mail in accounts.items():
        if t == mail or t == mail.split("@")[0]:
            return name
    return None


def parse_request(text: str) -> dict:
    """Pull out account / to / subject / instructions without an LLM."""
    accounts = _accounts()
    out: dict = {"account": None, "to": None, "subject": None, "instructions": text.strip(), "warnings": []}
    emails = EMAIL_RE.findall(text)

    m = re.search(r"\bfrom\s+(\S+)", text, re.I)
    if m:
        acct = _resolve_account(m.group(1).strip(",.;"), accounts)
        if acct:
            out["account"] = acct
            emails = [e for e in emails if e.lower() != accounts[acct]]
        else:
            out["warnings"].append(f"unknown sender account '{m.group(1)}' — using default")

    m = re.search(r"\bto\s+([\w.+-]+@[\w-]+\.[\w.-]+)", text, re.I)
    if m:
        out["to"] = m.group(1)
    elif emails:
        out["to"] = emails[0]

    m = re.search(r"\bsubject(?:\s+is|\s*[:=])?\s+[\"“']?(.+?)[\"”']?(?:\s+(?:and|body|saying|message|text)\b|$)", text, re.I)
    if m:
        out["subject"] = m.group(1).strip(" .,")

    # what should the email say? strip the routing words so the LLM sees only content
    ADDR = r"[\w.+-]+@[\w-]+\.[\w.-]+"
    body_src = text
    body_src = re.sub(r"\bfrom\s+\S+", " ", body_src, flags=re.I)
    body_src = re.sub(r"\bsubject(?:\s+is|\s*[:=])?\s+[\"“']?.+?[\"”']?(?=\s+(?:and|body|saying|message|text)\b|$)", " ", body_src, flags=re.I)
    # "tell X that …", "email X about …", "write to X saying …", "let X know …", "send an email to X"
    REL = r"(?:my\s+(?:friend|buddy|bro|mate|pal|teacher|boss|colleague|mom|dad)\s+)?"
    body_src = re.sub(rf"^\s*(?:please\s+)?(?:send|write|compose|draft|shoot)\s+(?:an?\s+)?(?:e-?mai?l?|messsage|message|msg|mail|note)\s*(?:to\s+{REL}{ADDR})?\s*", " ", body_src, flags=re.I)
    body_src = re.sub(rf"^\s*(?:tell|e-?mai?l?|message|msg|mail|write\s+to|let|ask|remind|inform|complain(?:ing)?|reply)\s+(?:to\s+)?{REL}{ADDR}\s*(?:know\s+)?", " ", body_src, flags=re.I)
    body_src = re.sub(r"^\s*(?:complain(?:ing|t)?|reply)\s+(?:to\s+\S+\s+)?", " ", body_src, flags=re.I)
    body_src = re.sub(rf"\bto\s+{REL}{ADDR}", " ", body_src, flags=re.I)
    body_src = re.sub(rf"\b{REL[:-1]}{ADDR}", " ", body_src, flags=re.I)   # leftover "my friend <addr>"
    # "about <topic>: <content>"  ->  subject = topic, body = content
    m = re.match(r"^\s*about\s+(.+?)\s*[:\-–]\s+(.+)$", body_src, re.I | re.S)
    if m and not out["subject"]:
        out["subject"] = m.group(1).strip(" .,").capitalize()
        body_src = m.group(2)
    elif re.match(r"^\s*about\s+", body_src, re.I) and not out["subject"]:
        topic = re.sub(r"^\s*about\s+", "", body_src, flags=re.I)
        out["subject"] = topic.split(",")[0].split(".")[0].strip()[:60].capitalize()
    body_src = re.sub(r"^\s*(?:that|saying|says|that says|which says|with the (?:message|body|text)|:)\s*[:,-]?\s*", " ", body_src, flags=re.I)
    out["instructions"] = re.sub(r"\s+", " ", body_src).strip(" ,.:;-") or text.strip()
    return out


async def compose(text: str, default_account: str) -> dict:
    p = parse_request(text)
    p["account"] = p["account"] or default_account
    plan = mailbrain.build_plan(text, p["to"], p["subject"], p["instructions"])
    system, user = mailbrain.render_prompt(plan, p["instructions"])
    style = _style_block(p["to"], p["instructions"])
    body = None
    try:
        raw = (await llm.complete("planner", system + ("\n\n" + style if style else ""), user, temperature=0.1)).strip()
        cand = raw
        if raw.startswith("{"):                      # model wrapped it in JSON anyway
            try:
                cand = json.loads(raw).get("body", raw)
            except Exception:
                mm = re.search(r'\"body\"\s*:\s*\"(.*)\"', raw, re.S)
                if mm:
                    cand = mm.group(1).encode().decode("unicode_escape")
        if isinstance(cand, str) and len(cand.split()) >= 3:
            candidate = mailbrain.enforce(cand, plan)
            issues = mailbrain.unsupported_specifics(candidate, p["instructions"])
            if issues:
                p["warnings"].append("Draft added unsupported details — used the faithful structured version")
            else:
                body = candidate
    except Exception as e:  # noqa: BLE001
        p["warnings"].append(f"LLM unavailable ({str(e)[:60]}) — used structured fallback")
    if not body:
        body = mailbrain.fallback_body(plan, p["instructions"])
    return {"account": p["account"], "to": p["to"], "subject": plan["subject"], "body": body,
            "purpose": plan["purpose"], "register": plan["register"],
            "recipient": plan["audience"]["name"] or (p["to"] or ""), "warnings": p["warnings"]}


def remember_sent(draft: dict) -> None:
    """After a draft is actually sent, record the recipient + our register choice."""
    to = draft.get("to")
    if not to:
        return
    f = {"formal": 3, "professional": 2, "friendly": 1}.get(draft.get("register", ""), None)
    name = draft.get("recipient", "")
    if "@" in name:            # recipient fell back to the email address — not a real name
        name = ""
    mailbrain.remember(to, name=name, formality=f, subject=draft.get("subject", ""))


def preview(d: dict) -> str:
    warn = ("\n".join(f"⚠️ {w}" for w in d["warnings"]) + "\n") if d.get("warnings") else ""
    tag = ""
    if d.get("purpose"):
        tag = f"_{d['purpose']} · {d.get('register','')}_\n"
    return (f"{warn}📝 **Draft** {tag}**From:** `{d['account']}`\n**To:** {d['to'] or '❌ no recipient found'}\n"
            f"**Subject:** {d['subject']}\n```\n{d['body']}\n```\n"
            "Reply **yes** to send, **no** to discard, or tell me what to change.")
