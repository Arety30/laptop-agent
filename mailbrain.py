"""Email intelligence for the composer — the judgement a 1B model can't be trusted with,
done in plain Python so the model only has to write prose inside a plan we hand it.

Given the request text + recipient + sending account, build_plan() decides:
  • purpose   — what kind of email this is (request, thank-you, apology, application, …)
  • audience  — who it's to and how formal to be (formal / professional / friendly)
  • structure — the ordered parts the body must contain, for THIS purpose
  • greeting / sign-off — matched to the formality and to what we know about the person
  • tone notes — mood + do/don't, fed to the model and enforced afterwards

Recipient memory lives in ~/.config/proton-mail-mcp/recipients.json (learned from the
style corpus + updated as mail is sent), so the bot remembers who people are.

Nothing here needs the network or the LLM; it is all testable offline (see tests/test_mailbrain.py).
"""
from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path

SIGN_OFF_NAME = "Abrar"
FULL_NAME = "Syed Abrar Ali"
RECIPIENTS_FILE = Path(os.environ.get(
    "MAIL_RECIPIENTS_FILE", "~/.config/proton-mail-mcp/recipients.json")).expanduser()
CORPUS_EMAILS = Path(__file__).parent / "workspace" / "mail_style" / "emails.jsonl"

# --------------------------------------------------------------------------- formality
# Domains that imply an organisation → default to a formal/professional register.
_ORG_TLDS = (".gov", ".edu", ".org", ".ac.", ".mil")
_ORG_HINTS = ("school", "college", "university", "academy", "institute", "ministry",
              "support", "info", "admin", "hr", "careers", "jobs", "noreply", "contact",
              "sales", "billing", "office", "team", "help", "service")
_FREE_MAIL = ("gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "proton.me",
              "protonmail.com", "pm.me", "icloud.com")

# formality: 3 = formal (stranger/official), 2 = professional (teacher/known business),
#            1 = friendly (someone he knows casually)
GREETINGS = {
    3: ("Dear {name},", "Dear Sir/Madam,", "To whom it may concern,"),
    2: ("Dear {name},", "Hello {name},", "Hi {name},"),
    1: ("Hi {name},", "Hey {name},", "Hi,"),
}
SIGNOFFS = {
    3: (f"Sincerely,\n{FULL_NAME}", f"Kind regards,\n{FULL_NAME}", f"Yours sincerely,\n{FULL_NAME}"),
    2: (f"Best regards,\n{SIGN_OFF_NAME}", f"Regards,\n{SIGN_OFF_NAME}", f"Thanks,\n{SIGN_OFF_NAME}"),
    1: (f"Thanks,\n{SIGN_OFF_NAME}", f"Cheers,\n{SIGN_OFF_NAME}", f"— {SIGN_OFF_NAME}"),
}

# --------------------------------------------------------------------------- purposes
# Each purpose: keywords to detect it, the structural parts of the body (in order),
# and a one-line brief handed to the model. Order matters — earlier = higher priority.
PURPOSES: dict[str, dict] = {
    "application": {
        "kw": r"\b(apply|application|applying|position|vacancy|internship|job|cover letter|"
              r"resume|cv|candidate|role of)\b",
        "structure": ["greeting", "state the role you're applying for and where you saw it",
                      "1-2 lines on why you're a good fit / relevant experience",
                      "mention attachments (CV/portfolio) if any", "polite request to consider you",
                      "sign-off"],
        "brief": "A job/opportunity application. Confident but humble, specific about the role.",
        "subject": "Application for {topic}",
    },
    "complaint": {
        "kw": r"\b(complaint|complain|refund|not working|broken|faulty|disappointed|unacceptable|"
              r"charged|overcharged|wrong order|defective|delay(ed)?|never (arrived|received))\b",
        "structure": ["greeting", "state the problem plainly with order/reference and date",
                      "what impact it caused", "the resolution you want (refund/replacement/fix)",
                      "sign-off"],
        "brief": "A complaint. Firm and factual, never rude; state the fix you expect.",
        "subject": "Regarding {topic}",
    },
    "apology": {
        "kw": r"\b(sorry|apolog(y|ise|ize|ies)|my (bad|mistake|fault)|regret|didn'?t mean)\b",
        "structure": ["greeting", "a clear, brief apology", "one line of context (not an excuse)",
                      "how you'll make it right", "sign-off"],
        "brief": "An apology. Sincere and short, own it, no over-explaining.",
        "subject": "Apologies regarding {topic}",
    },
    "thanks": {
        "kw": r"\b(thank(s| you)|grateful|appreciate|appreciated|means a lot)\b",
        "structure": ["greeting", "thank them specifically for what they did",
                      "why it mattered / one warm line", "sign-off"],
        "brief": "A thank-you. Warm and specific about what you're thanking them for.",
        "subject": "Thank you",
    },
    "scheduling": {
        "kw": r"\b(meeting|schedule|reschedule|appointment|call|available|availability|"
              r"book a|set up a|catch up|find a time|calendar|slot)\b",
        "structure": ["greeting", "the purpose of the meeting", "propose day/time or ask for theirs",
                      "confirm how (call/in person/link)", "sign-off"],
        "brief": "A scheduling email. Clear about purpose and propose a concrete time.",
        "subject": "Meeting request: {topic}",
    },
    "followup": {
        "kw": r"\b(follow(ing)? up|circle back|checking in|any update|just wondering if|"
              r"haven'?t heard|gentle reminder|reminder about|still waiting)\b",
        "structure": ["greeting", "reference the earlier message/request and when",
                      "politely ask for an update", "sign-off"],
        "brief": "A follow-up. Polite, brief, references the earlier thread, no pressure.",
        "subject": "Following up: {topic}",
    },
    "inquiry": {
        "kw": r"\b(inquir(e|y)|enquir(e|y)|would like to know|could you (tell|let|send|provide)|"
              r"wondering (about|if)|how much|what (is|are) the|do you (have|offer)|price|cost|quote|"
              r"more information|details about|is it possible)\b",
        "structure": ["greeting", "one line of who you are / why you're asking (if a stranger)",
                      "the specific question(s)", "thank them / say you look forward to their reply",
                      "sign-off"],
        "brief": "An inquiry/request for information. Get to the question quickly and specifically.",
        "subject": "Inquiry about {topic}",
    },
    "submission": {
        "kw": r"\b(submit(ting)?|attached|please find|here is (my|the)|sending (you )?(my|the)|"
              r"my project|my assignment|deliverable|for your review)\b",
        "structure": ["greeting", "say what you're submitting and for what",
                      "one line on what's included / how to view it", "invite questions", "sign-off"],
        "brief": "Submitting work. State what's attached and what it's for.",
        "subject": "Submission: {topic}",
    },
    "introduction": {
        "kw": r"\b(introduce myself|my name is|reaching out|reach out|wanted to connect|"
              r"get in touch|came across your)\b",
        "structure": ["greeting", "who you are in one line", "why you're reaching out",
                      "a clear next step or ask", "sign-off"],
        "brief": "An introduction / cold outreach. Say who you are and why, then a clear ask.",
        "subject": "Introduction — {topic}",
    },
    "invitation": {
        "kw": r"\b(invite|invitation|you'?re invited|join us|rsvp|would love to have you|"
              r"attend|our event)\b",
        "structure": ["greeting", "what the event is + why you'd like them there",
                      "when and where", "how to RSVP", "sign-off"],
        "brief": "An invitation. Warm; give the what/when/where and how to reply.",
        "subject": "Invitation: {topic}",
    },
    "update": {
        "kw": r"\b(update|let you know|informing you|heads up|for your (information|awareness)|"
              r"status|progress|wanted to share|fyi)\b",
        "structure": ["greeting", "the update in one or two clear lines", "any action needed from them",
                      "sign-off"],
        "brief": "A status update / notification. Lead with the update, flag any action needed.",
        "subject": "Update: {topic}",
    },
    "request": {   # generic ask — broad fallback before 'general'
        "kw": r"\b(could you|can you|would you|please (send|share|help|approve|confirm|review|do)|"
              r"i (need|would like|want) you to|requesting|kindly)\b",
        "structure": ["greeting", "one line of context if needed", "the specific request",
                      "why / by when if relevant", "thanks + sign-off"],
        "brief": "A request. State exactly what you're asking for and by when.",
        "subject": "Request: {topic}",
    },
    "general": {
        "kw": r".",
        "structure": ["greeting", "the message in one to three short paragraphs", "sign-off"],
        "brief": "A general message. Say it clearly and courteously.",
        "subject": "{topic}",
    },
}

# explicit register overrides the user can type
_FORCE_FORMAL = re.compile(r"\b(formal(ly)?|professional(ly)?|official(ly)?|polite(ly)?)\b", re.I)
_FORCE_CASUAL = re.compile(r"\b(casual(ly)?|friendly|chill|relaxed|informal(ly)?)\b", re.I)
_MENTIONS_ATTACH = re.compile(r"\b(attach(ed|ing|ment)?|cv|resume|portfolio|file|document|pdf)\b", re.I)

# casual tokens to clean out of formal/professional mail
_CASUAL_FIX = {
    r"\bu\b": "you", r"\bur\b": "your", r"\br\b": "are", r"\bpls\b": "please", r"\bplz\b": "please",
    r"\bthx\b": "thanks", r"\bcuz\b": "because", r"\bwanna\b": "want to", r"\bgonna\b": "going to",
    r"\bgotta\b": "have to", r"\blemme\b": "let me", r"\bkinda\b": "kind of", r"\byeah\b": "yes",
    r"\bnah\b": "no", r"\bok\b": "okay", r"\btbh\b": "", r"\blol\b": "", r"\bimma\b": "I'm going to",
}


# --------------------------------------------------------------------------- recipient memory
def _load_recipients() -> dict:
    try:
        return json.loads(RECIPIENTS_FILE.read_text())
    except Exception:
        return {}


def _save_recipients(d: dict) -> None:
    RECIPIENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    RECIPIENTS_FILE.write_text(json.dumps(d, indent=2))
    try:
        RECIPIENTS_FILE.chmod(0o600)
    except OSError:
        pass


def name_from_address(addr: str, display: str = "") -> str:
    """Best-effort human name: a display name if present, else the local part cleaned up."""
    display = re.sub(r"[<>\"]", "", display or "").strip()
    if display and "@" not in display:
        return display.split()[0].capitalize() if display.islower() else display.split()[0]
    local = addr.split("@")[0]
    local = re.sub(r"[._-]+", " ", re.sub(r"\d+", "", local)).strip()
    first = local.split()[0] if local else ""
    generic = ("info", "support", "admin", "contact", "hr", "team", "noreply", "no-reply",
               "careers", "jobs", "sales", "billing", "office", "help", "service", "mail")
    if len(first) >= 2 and first.lower() not in generic:
        return first.capitalize()
    return ""


def remember(addr: str, *, name: str = "", formality: int | None = None,
             subject: str = "", relationship: str = "") -> None:
    """Record/refresh what we know about a recipient after sending them mail."""
    if not addr:
        return
    d = _load_recipients()
    r = d.setdefault(addr.lower(), {"count": 0, "subjects": []})
    r["count"] = r.get("count", 0) + 1
    if name and not r.get("name"):
        r["name"] = name
    if relationship:
        r["relationship"] = relationship
    if formality:
        r["formality"] = formality
    if subject:
        r.setdefault("subjects", [])
        r["subjects"] = ([subject] + r["subjects"])[:5]
    r["last_seen"] = date.today().isoformat()
    _save_recipients(d)


def learn_from_corpus() -> int:
    """Seed recipient memory from emails Abrar actually sent (idempotent)."""
    if not CORPUS_EMAILS.exists():
        return 0
    d = _load_recipients()
    n = 0
    for line in CORPUS_EMAILS.read_text().splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        m = re.search(r"([\w.+-]+@[\w.-]+)", e.get("to", ""))
        if not m:
            continue
        addr = m.group(1).lower()
        disp = e["to"].split("<")[0] if "<" in e["to"] else ""
        r = d.setdefault(addr, {"count": 0, "subjects": []})
        r["count"] = r.get("count", 0) + 1
        if not r.get("name"):
            nm = name_from_address(addr, disp)
            if nm:
                r["name"] = nm
        if e.get("subject") and e["subject"] not in r.get("subjects", []):
            r.setdefault("subjects", []).insert(0, e["subject"])
            r["subjects"] = r["subjects"][:5]
        n += 1
    _save_recipients(d)
    return n


# --------------------------------------------------------------------------- classification
def classify_purpose(text: str) -> str:
    t = text.lower()
    for key, spec in PURPOSES.items():
        if key == "general":
            continue
        if re.search(spec["kw"], t):
            return key
    return "general"


# signals that the message itself is casual, and topics that force a serious register
_CASUAL_TOKENS = re.compile(
    r"\b(hey|yo|sup|bro|bruh|dude|lol|lmao|haha+|wanna|gonna|gotta|lemme|chill|"
    r"hang ?out|hangout|tonight|later|the thing|movie|game|gaming|party|pizza|"
    r"catch up|u|ur|thx|cya|nah|yeah|cool|dope|bet|fr|ngl)\b", re.I)
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF]")
_FORMAL_TOPIC = re.compile(
    r"\b(appl(y|ication|ying)|internship|position|vacancy|job|resume|cv|refund|complain(t)?|"
    r"invoice|inquiry|enquiry|quotation|quote|official|to whom|dear sir|sir|madam|"
    r"regarding your (order|account)|proposal|contract|payment)\b", re.I)


def _casual_signal(text: str) -> bool:
    """Does the request itself read casually? (slang / emoji / all-lowercase)."""
    t = text.strip()
    score = 0
    if _CASUAL_TOKENS.search(t):
        score += 1
    if _EMOJI.search(t):
        score += 1
    letters = [c for c in t if c.isalpha()]
    if letters and sum(c.islower() for c in letters) / len(letters) > 0.95 and not t[:1].isupper():
        score += 1          # written all-lowercase, like a text message
    return score >= 2


def classify_formality(to: str | None, text: str, recipients: dict | None = None) -> int:
    """3 formal, 2 professional, 1 friendly. Register is chosen from three things, in order:
    an explicit instruction, who the recipient is (memory then domain), and finally the
    tone of the message itself — but a serious topic or an org recipient always stays formal."""
    if _FORCE_FORMAL.search(text):
        return 3
    if _FORCE_CASUAL.search(text):
        return 1
    recipients = _load_recipients() if recipients is None else recipients
    if to and to.lower() in recipients and recipients[to.lower()].get("formality"):
        return recipients[to.lower()]["formality"]
    if re.search(r"\b(my (friend|buddy|bro|mate|pal)|tell my)\b", text, re.I):
        return 1
    if not to:
        # no recipient: infer from tone, but a serious topic stays professional
        return 1 if (_casual_signal(text) and not _FORMAL_TOPIC.search(text)) else 2
    domain = to.split("@")[-1].lower()
    local = to.split("@")[0].lower()
    local_tokens = set(re.split(r"[._+-]+", local))     # token match, so "chris" isn't read as "hr"
    if any(domain.endswith(t) for t in _ORG_TLDS) or any(h in domain for h in _ORG_HINTS) \
            or any(h in local_tokens for h in _ORG_HINTS):
        return 3
    if domain not in _FREE_MAIL:      # a company's own domain → stay formal
        return 3
    # free-mail personal address: casual if the message reads casual AND it isn't a serious topic
    if _casual_signal(text) and not _FORMAL_TOPIC.search(text):
        return 1
    return 2


def audience(to: str | None, text: str) -> dict:
    recipients = _load_recipients()
    rec = recipients.get((to or "").lower(), {})
    name = rec.get("name") or name_from_address(to or "") if to else ""
    formality = classify_formality(to, text, recipients)
    return {
        "to": to, "name": name, "formality": formality,
        "known": bool(rec.get("count")), "relationship": rec.get("relationship", ""),
        "past_subjects": rec.get("subjects", []),
    }


def _pick(options: tuple, name: str) -> str:
    """Choose greeting/sign-off: prefer a named variant when we have a name."""
    if name:
        for o in options:
            if "{name}" in o:
                return o.format(name=name)
        return options[0].format(name=name) if "{name}" in options[0] else options[0]
    for o in options:
        if "{name}" not in o:
            return o
    return re.sub(r"\s*\{name\}", "", options[-1])


def build_plan(text: str, to: str | None, subject: str | None, topic: str) -> dict:
    purpose = classify_purpose(text)
    spec = PURPOSES[purpose]
    aud = audience(to, text)
    f = aud["formality"]
    greeting = _pick(GREETINGS[f], aud["name"])
    signoff = _pick(SIGNOFFS[f], aud["name"])
    reg = {3: "formal", 2: "professional", 1: "friendly"}[f]
    subj = subject or spec["subject"].format(topic=(topic or "your message").strip().rstrip(".:"))
    subj = subj[:1].upper() + subj[1:]
    return {
        "purpose": purpose, "brief": spec["brief"], "structure": spec["structure"],
        "register": reg, "formality": f, "greeting": greeting, "signoff": signoff,
        "audience": aud, "subject": subj[:120],
        "mentions_attachment": bool(_MENTIONS_ATTACH.search(text)),
    }


# --------------------------------------------------------------------------- prompt + enforcement
def render_prompt(plan: dict, instructions: str) -> tuple[str, str]:
    """(system, user) for the LLM, driven entirely by the plan."""
    aud = plan["audience"]
    who = f"{aud['name']}" if aud["name"] else "the recipient"
    known = ""
    if aud["known"]:
        known = f" You have emailed {who} before"
        if aud["past_subjects"]:
            known += f" (past subjects: {'; '.join(aud['past_subjects'][:2])})"
        known += "."
    system = (
        f"You are an assistant that writes emails on behalf of {SIGN_OFF_NAME}.\n"
        f"This is a {plan['register'].upper()} email. {plan['brief']}{known}\n"
        "Write ONLY the email itself — greeting through sign-off — and nothing else: "
        "no preamble, no explanation, no JSON, no quotes around it.\n"        "The body MUST follow this structure, one idea per short paragraph:\n"
        + "\n".join(f"  {i+1}. {s}" for i, s in enumerate(plan["structure"])) + "\n"
        f"Start with exactly this greeting line: {plan['greeting']}\n"
        f"End with exactly this sign-off:\n{plan['signoff']}\n"
        "Plain text only — no markdown, no asterisks, no headings, no bullet characters.\n"
        "CRITICAL: use ONLY the facts written in the request below. Do NOT add products, technologies "
        "(e.g. \"AI\", \"mobile app\"), names, dates, numbers, or claims that are not explicitly there. "
        "Rephrase, never embellish. Be brief — a short email of 2 to 5 sentences is ideal.\n"
        "Never write placeholder brackets like [date], [product] or [size]; if a detail isn't given, leave it out. "
        "Do not include any 'Forwarded message', quoted thread, headers (To:/From:), or links.\n"
        + ("Do not mention any attachment.\n" if not plan["mentions_attachment"] else "")
        + ("Keep it warm but correct; contractions are fine.\n" if plan["formality"] == 1
           else "Keep it polished and courteous; avoid slang and contractions where natural.\n")
    )
    user = f"Subject: {plan['subject']}\nWhat {SIGN_OFF_NAME} wants to say: {instructions}"
    return system, user


def _strip_markdown(t: str) -> str:
    t = re.sub(r"\*\*|__|`", "", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"^\s*[-*•]\s+", "", t, flags=re.M)
    return t


def enforce(body: str, plan: dict) -> str:
    """Make any draft obey the plan: no markdown, right register, greeting + sign-off present."""
    body = _strip_markdown(body).strip()
    # cut anything from a leaked forwarded/quoted block onward (tiny models paste examples)
    body = re.split(r"(?im)^\s*(-{3,}\s*forwarded|-{5,}|on .{5,80} wrote:|>+ )", body)[0]
    body = re.sub(r"(?im)^\s*(to|from|cc|subject|name|id|class|section|school id)\s*:.*$", "", body)
    body = re.sub(r"\[[^\]\n]{1,40}\]", "", body)          # drop [date] / [product] placeholders
    body = re.sub(r"https?://\S+|<https?://\S+>", "", body)  # drop any leaked links
    body = re.sub(r"[ \t]{2,}", " ", body).strip()
    if plan["formality"] >= 2:      # de-slang formal/professional mail
        for pat, repl in _CASUAL_FIX.items():
            body = re.sub(pat, repl, body, flags=re.I)
        body = re.sub(r"[ \t]{2,}", " ", body)
    # drop lines the model leaks from the prompt (instructions, "here's the email", stray JSON)
    _meta = re.compile(r"(?i)^\s*(plain text( only)?\.?$|no markdown|markdown|do not\b|don'?t\b|"
                       r"only (return|output|reply|json)|use exactly|as instructed|"
                       r"here(?:'?s| is) (?:the|your) (?:email|draft|message)|subject\s*:|"
                       r"\{?\"?(subject|body)\"?\s*[:=]|```)")
    lines = [ln.rstrip() for ln in body.split("\n") if not _meta.match(ln.strip())]
    while lines and not lines[0].strip():
        lines.pop(0)
    # greeting — but only treat line 1 as a salutation if it LOOKS like one
    # (short, or ends with a comma), so a sentence like "Hey, thanks for..." that
    # runs on isn't mistaken for a proper greeting line.
    greet_word = re.compile(r"^(hi|hello|hey|dear|good (morning|afternoon|evening)|to whom)\b", re.I)
    first = lines[0].strip() if lines else ""
    is_greeting = bool(greet_word.match(first)) and (first.endswith(",") or len(first.split()) <= 4)
    if not is_greeting:
        lines = [plan["greeting"], ""] + lines
    elif len(lines) > 1 and lines[1].strip():
        lines.insert(1, "")        # blank line after greeting
    # sign-off — find it if the model wrote one, else append the plan's
    signoff_start = re.compile(r"^\s*(sincerely|regards|best regards|best|kind regards|warm regards|"
                               r"thanks|thank you|cheers|yours( sincerely| faithfully| truly)?|—)\b", re.I)
    idx = next((i for i, ln in enumerate(lines) if signoff_start.match(ln.strip())), None)
    if idx is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(plan["signoff"])
    elif idx > 0 and lines[idx - 1].strip():
        lines.insert(idx, "")      # ensure a blank line before the sign-off
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    # capitalise the first content line for formal/professional
    if plan["formality"] >= 2:
        parts = text.split("\n\n", 1)
        if len(parts) == 2 and parts[1]:
            parts[1] = parts[1][:1].upper() + parts[1][1:]
            text = "\n\n".join(parts)
    return text


def unsupported_specifics(body: str, instructions: str) -> list[str]:
    """Return high-confidence signs that a small model added facts.

    A draft may use polite connective language that wasn't in the request; it
    must not invent a game, platform, product, date, order detail, or number.
    The caller can safely choose the structured fallback when this detects one.
    """
    given = instructions.lower()
    issues: list[str] = []

    # Numbers/times/dates are facts. Ignore only a token present in the request.
    supplied = set(re.findall(r"\b\d+(?::\d+)?\b", given))
    for token in set(re.findall(r"\b\d+(?::\d+)?\b", body.lower())):
        if token not in supplied:
            issues.append(f"invented number/date: {token}")

    # Brand/product names are never safe to add unprompted.
    products = ("steam", "xbox", "playstation", "overwatch", "minecraft", "fortnite",
                "discord", "whatsapp", "google drive", "ai", "artificial intelligence",
                "kpi", "financial data")
    for item in products:
        if item in body.lower() and item not in given:
            issues.append(f"invented specific: {item}")

    # New all-caps terms usually indicate a made-up product, metric, or acronym.
    for token in set(re.findall(r"\b[A-Z]{2,}\b", body)):
        if token.lower() not in given:
            issues.append(f"invented acronym: {token}")
    return issues


def fallback_body(plan: dict, instructions: str) -> str:
    """A cleanly structured body when the LLM is unavailable — greeting, the message, sign-off."""
    msg = instructions.strip()
    msg = msg[:1].upper() + msg[1:] if msg else "I wanted to get in touch."
    if not re.search(r"[.!?]$", msg):
        msg += "."
    body = f"{plan['greeting']}\n\n{msg}\n\n{plan['signoff']}"
    return enforce(body, plan)
