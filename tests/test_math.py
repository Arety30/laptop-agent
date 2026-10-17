#!/usr/bin/env python3
"""Math test suite for the twin — auto-graded.

Checks (a) routing: maths must NOT trigger a web search, and (b) accuracy: the
right number appears in the reply. Also compares the chat model against the
deep-reasoning model (!think) so we know which to route maths to.

    ./.venv/bin/python tests/test_math.py            # chat model
    ./.venv/bin/python tests/test_math.py --reason   # deepseek reasoner
    ./.venv/bin/python tests/test_math.py --both
"""
from __future__ import annotations

import asyncio
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import orchestrator  # noqa: E402

G, R, Y, O = "\033[92m", "\033[91m", "\033[93m", "\033[0m"

# (question, accepted answers as strings found in the reply)
CASES = [
    # basic arithmetic
    ("whats 2+2", ["4"]),
    ("what is 15 times 7", ["105"]),
    ("whats 144 divided by 12", ["12"]),
    ("what is 87 minus 39", ["48"]),
    ("whats 2 to the power of 10", ["1024"]),
    # order of operations
    ("what is 2 + 3 * 4", ["14"]),
    ("whats (8 + 4) / 3", ["4"]),
    # percentages
    ("what is 15% of 240", ["36"]),
    ("whats 20 percent of 850", ["170"]),
    ("if something costs 80 and theres 25% off whats the price", ["60"]),
    # fractions / decimals
    ("what is 3/4 as a decimal", ["0.75", ".75"]),
    ("whats 1/2 plus 1/4", ["3/4", "0.75", ".75"]),
    # word problems
    ("if i have 5 boxes with 12 apples each how many apples do i have", ["60"]),
    ("a train goes 60 km in 45 minutes what is its speed in km/h", ["80"]),
    ("i buy 3 things at 250 each how much total", ["750"]),
    ("if 8 people share 96 sweets equally how many each", ["12"]),
    # multi-step
    ("i have 500 rupees i spend 120 then 85 how much is left", ["295"]),
    ("whats the average of 10 20 and 60", ["30"]),
    # algebra
    ("solve for x: 3x + 5 = 20", ["5"]),
    ("if 2x = 18 what is x", ["9"]),
    # units / time
    ("how many minutes in 3.5 hours", ["210"]),
    ("how many seconds in 2 minutes", ["120"]),
]


def _has(answer: str, accepted: list[str]) -> bool:
    """True if any accepted answer appears as a standalone number/token."""
    a = answer.lower().replace(",", "")
    for acc in accepted:
        if re.search(rf"(?<![\d.]){re.escape(acc.lower())}(?![\d])", a):
            return True
    return False


async def run(mode: str) -> tuple[int, int]:
    ok = bad = 0
    for q, accepted in CASES:
        searched = orchestrator._wants_web(q)
        if mode == "reason":
            reply = await orchestrator.deep_reason(q)
        else:
            reply = await orchestrator.chat(q, "")
        good = _has(reply, accepted)
        flags = []
        if searched:
            flags.append("web-searched!")
        if good:
            ok += 1
            print(f"  {G}✓{O} {q[:46]:48} {Y}{reply[:60].strip()}{O}"
                  + (f" {R}[{'; '.join(flags)}]{O}" if flags else ""))
        else:
            bad += 1
            print(f"  {R}✗{O} {q[:46]:48} want {accepted[0]}\n"
                  f"      got: {reply[:110].strip()}"
                  + (f"  {R}[{'; '.join(flags)}]{O}" if flags else ""))
    return ok, bad


async def main() -> None:
    modes = []
    if "--both" in sys.argv:
        modes = ["chat", "reason"]
    elif "--reason" in sys.argv:
        modes = ["reason"]
    else:
        modes = ["chat"]

    total_bad = 0
    for m in modes:
        print(f"── maths via {'deep reasoner (deepseek-r1)' if m == 'reason' else 'chat model'} ──")
        ok, bad = await run(m)
        total_bad += bad
        pct = 100 * ok // max(1, ok + bad)
        print(f"  {ok}/{ok+bad} correct ({pct}%)\n")
    sys.exit(1 if total_bad else 0)


if __name__ == "__main__":
    asyncio.run(main())
