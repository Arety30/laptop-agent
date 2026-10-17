"""Regression tests for the email composer's deterministic parser (no LLM needed).
Run: ./.venv/bin/python tests/test_mail.py"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mailcompose as mc  # noqa: E402

CASES = [
    ("from arety to user@example.com subject test 3 saying this is a test",
     {"to": "user@example.com", "subject": "test 3", "instructions": "this is a test"}),
    ("tell bob@example.com the standup moved to 5pm and to bring the budget sheet",
     {"to": "bob@example.com", "subject": None, "instructions": "the standup moved to 5pm and to bring the budget sheet"}),
    ("email user@example.com about the competition project: I finished the app and will demo it monday",
     {"to": "user@example.com", "subject": "The competition project", "instructions": "I finished the app and will demo it monday"}),
    ("send an email to user@example.com saying I'd like to know when the draw results come out",
     {"to": "user@example.com", "subject": None, "instructions": "I'd like to know when the draw results come out"}),
    ("let user@example.com know that I can't make it tomorrow, sorry",
     {"to": "user@example.com", "subject": None, "instructions": "I can't make it tomorrow, sorry"}),
    ("send an emai to user@example.com saying test",
     {"to": "user@example.com", "instructions": "test"}),
    ("write to user@example.com subject: Weekly update and body: all tasks done",
     {"to": "user@example.com", "subject": "Weekly update"}),
]

fails = 0
for text, want in CASES:
    got = mc.parse_request(text)
    for k, v in want.items():
        if got.get(k) != v:
            fails += 1
            print(f"FAIL {text!r}\n   {k}: want {v!r} got {got.get(k)!r}")
# account resolution: "from arety" must resolve when accounts.json has it; unknown sender -> warning
acc = mc.parse_request("from nobody-here to user@example.com saying hi")
assert acc["warnings"], "unknown sender should warn"
print(f"{len(CASES) - fails}/{len(CASES)} parser cases pass" + (" — FAILURES ABOVE" if fails else ""))
sys.exit(1 if fails else 0)
