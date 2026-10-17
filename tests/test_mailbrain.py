"""Offline tests for the email-intelligence layer — no LLM, no network.
Run:  ./.venv/bin/python tests/test_mailbrain.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mailbrain as mb  # noqa: E402

# isolate recipient memory to a temp file so tests never touch real data
mb.RECIPIENTS_FILE = Path(tempfile.mkdtemp()) / "recipients.json"

fails = []
_total = 0


def check(cond, msg):
    global _total
    _total += 1
    if not cond:
        fails.append(msg)


# ---- purpose classification ----------------------------------------------------------
PURPOSE_CASES = [
    ("I want to apply for the software internship at your company", "application"),
    ("my order never arrived and I want a refund", "complaint"),
    ("so sorry I missed the call earlier, my fault", "apology"),
    ("thank you so much for helping me with the project", "thanks"),
    ("can we schedule a meeting next week to discuss the budget", "scheduling"),
    ("just following up on my email from last week", "followup"),
    ("I'd like to know how much the balloon festival stall costs", "inquiry"),
    ("please find my assignment attached for your review", "submission"),
    ("my name is Abrar and I'm reaching out about a collaboration", "introduction"),
    ("you're invited to our science fair on Friday, please rsvp", "invitation"),
    ("just a heads up that the server will be down tonight", "update"),
    ("could you please send me the report by tomorrow", "request"),
    ("hope you are doing well", "general"),
]
for text, want in PURPOSE_CASES:
    got = mb.classify_purpose(text)
    check(got == want, f"purpose {text!r}: want {want} got {got}")

# ---- formality -----------------------------------------------------------------------
check(mb.classify_formality("user@example.com", "hi sir", {}) == 3, "school domain -> formal")
check(mb.classify_formality("user@example.com", "job app", {}) == 3, "hr@ -> formal")
check(mb.classify_formality("user@example.com", "applying", {}) == 3, "company domain -> formal")
check(mb.classify_formality("user@example.com", "Please share the meeting notes", {}) == 2, "neutral gmail -> professional")
check(mb.classify_formality("user@example.com", "tell my friend bob hi", {}) == 1, "friend cue -> friendly")
check(mb.classify_formality("user@example.com", "write this formally please", {}) == 3, "force formal")
check(mb.classify_formality("user@example.com", "keep it casual", {}) == 1, "force casual")
check(mb.classify_formality(None, "just a note", {}) == 2, "no recipient -> professional")
# content-based casual detection (free-mail + casual tone, no forced cue)
check(mb.classify_formality("user@example.com", "yo wanna hang out tonight lol", {}) == 1, "slangy gmail -> friendly")
check(mb.classify_formality("user@example.com", "i'll be late to the thing tonight", {}) == 1, "lowercase social gmail -> friendly")
check(mb.classify_formality("user@example.com", "The quarterly report is attached for your review", {}) == 2, "proper gmail -> professional")
check(mb.classify_formality("user@example.com", "yo bro can you refund my order lol", {}) == 2, "casual words but refund topic -> not friendly")
check(mb.classify_formality("user@example.com", "yo wanna hang tonight", {}) == 3, "org domain stays formal even if casual")

# ---- greeting / sign-off selection ---------------------------------------------------
check(mb._pick(mb.GREETINGS[3], "Sara") == "Dear Sara,", "formal named greeting")
check(mb._pick(mb.GREETINGS[3], "") == "Dear Sir/Madam,", "formal no-name greeting")
check(mb._pick(mb.GREETINGS[1], "sara") == "Hi sara,", "friendly named greeting")
check("Syed Abrar Ali" in mb._pick(mb.SIGNOFFS[3], "x"), "formal signoff uses full name")
check(mb._pick(mb.SIGNOFFS[1], "x").endswith("Abrar"), "friendly signoff uses Abrar")

# ---- build_plan ----------------------------------------------------------------------
plan = mb.build_plan("apply for the internship", "user@example.com", None, "the internship")
check(plan["purpose"] == "application", "plan purpose")
check(plan["register"] == "formal", "plan register formal for company")
check(plan["greeting"].startswith("Dear"), "plan greeting formal")
check(plan["subject"].lower().startswith("application for"), f"plan subject: {plan['subject']}")
check(plan["structure"][0] == "greeting" and plan["structure"][-1] == "sign-off", "plan structure ends")

# ---- enforce: markdown, slang, greeting+signoff insertion ----------------------------
messy = "**Hey** u should totally send me ur report cuz i need it, thx"
plan_prof = mb.build_plan(messy, "user@example.com", None, "report")
out = mb.enforce(messy, plan_prof)
check("**" not in out and "*" not in out, "enforce strips markdown")
import re as _re  # word-boundary check so "your" isn't mistaken for stray "ur"
_slang_left = [w for w in ("u", "ur", "cuz", "thx") if _re.search(rf"\b{w}\b", out)]
check(not _slang_left, f"enforce de-slangs (left: {_slang_left}): {out!r}")
check(out.split("\n")[0].startswith(("Dear", "Hello", "Hi")), "enforce adds greeting")
low = out.lower()
check(any(w in low for w in ("regards", "sincerely", "thanks", "best")), "enforce adds sign-off")

# body that already has greeting + signoff shouldn't get a second one
good = "Dear Sara,\n\nThe report is attached.\n\nBest regards,\nAbrar"
plan2 = mb.build_plan("send report", "user@example.com", None, "report")
out2 = mb.enforce(good, plan2)
check(out2.lower().count("dear sara") == 1, "no duplicate greeting")
check(out2.count("Abrar") == 1, "no duplicate signoff")

# ---- fallback body -------------------------------------------------------------------
fb = mb.fallback_body(mb.build_plan("thanks for the help", "user@example.com", None, "help"),
                      "thanks a lot for helping me yesterday")
check(fb.split("\n")[0].startswith(("Dear", "Hello", "Hi")), "fallback has greeting")
check(fb.strip().endswith("Abrar"), "fallback has signoff")
check("**" not in fb, "fallback no markdown")

# ---- recipient memory roundtrip ------------------------------------------------------
mb.remember("user@example.com", name="Newperson", formality=1, subject="hello")
rec = mb._load_recipients()
check("user@example.com" in rec, "recipient saved")
check(rec["user@example.com"]["formality"] == 1, "recipient formality saved")
# now formality classification should honour the memory
check(mb.classify_formality("user@example.com", "hi", rec) == 1, "memory overrides default formality")

# ---- factual safety gate -------------------------------------------------------------
check(bool(mb.unsupported_specifics("We can play Overwatch on Xbox tonight.",
                                   "want to hang out tonight")),
      "rejects invented game/platform")
check(bool(mb.unsupported_specifics("Order 4821 arrived on 2023-02-15.",
                                   "order 4821 arrived wrong")),
      "rejects invented date")
check(not mb.unsupported_specifics("Could Monday at 10am work for you?",
                                   "ask if Monday 10am works"),
      "allows requested time")

print(f"{_total - len(fails)}/{_total} checks passed, {len(fails)} failed")
for f in fails:
    print("  FAIL:", f)
sys.exit(1 if fails else 0)
