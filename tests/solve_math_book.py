#!/usr/bin/env python3
"""Feed puzzles from the maths book to the bot and auto-grade the answers.

    ./.venv/bin/python tests/solve_math_book.py --limit 20            # chat model
    ./.venv/bin/python tests/solve_math_book.py --limit 20 --reason   # deepseek-r1
    ./.venv/bin/python tests/solve_math_book.py --all                 # full 60% set

Results append to twin/math_results.json so runs are resumable.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import orchestrator  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent.parent / "twin"
DATA = HERE / "math_dataset.json"
RESULTS = HERE / "math_results.json"

G, R, Y, O = "\033[92m", "\033[91m", "\033[93m", "\033[0m"

PROMPT = ("Solve this puzzle. Think briefly, then end with the final answer on its "
          "own last line in the form 'ANSWER: <value>'.\n\n")


def _extract(reply: str) -> str | None:
    """Pull the model's final answer out of its reply."""
    m = re.search(r"ANSWER:\s*\**\s*(-?[\d,]+(?:\.\d+)?(?:/\d+)?|[A-E])\b",
                  reply, re.I)
    if m:
        return m.group(1).replace(",", "")
    nums = re.findall(r"-?\d[\d,]*(?:\.\d+)?", reply.replace("\n", " "))
    return nums[-1].replace(",", "") if nums else None


def _correct(got: str | None, want: str) -> bool:
    if got is None:
        return False
    try:
        return abs(float(got) - float(want)) < 1e-6
    except ValueError:
        return got.strip().upper() == want.strip().upper()


async def main() -> None:
    data = json.loads(DATA.read_text("utf-8"))
    pool = [q for q in data["solve"] if q.get("short")]

    limit = None
    if "--all" not in sys.argv:
        limit = 20
        for i, a in enumerate(sys.argv):
            if a == "--limit" and i + 1 < len(sys.argv):
                limit = int(sys.argv[i + 1])
    use_reason = "--reason" in sys.argv
    model = None
    for i, a in enumerate(sys.argv):
        if a == "--model" and i + 1 < len(sys.argv):
            model = sys.argv[i + 1]
    mode = model or ("reason" if use_reason else "chat")
    items = pool[:limit] if limit else pool

    print(f"── solving {len(items)} puzzles via {mode} ──")
    ok = bad = 0
    rows = []
    t0 = time.time()
    for i, q in enumerate(items, 1):
        try:
            # dedicated maths path: no web search, no persona enforcement
            reply = await orchestrator.solve_math(q["question"][:900],
                                                  use_reasoner=use_reason,
                                                  model=model)
        except Exception as e:  # noqa: BLE001
            reply = f"(error {e})"
        got = _extract(reply)
        good = _correct(got, q["short"])
        ok, bad = ok + int(good), bad + int(not good)
        rows.append({"n": q["n"], "want": q["short"], "got": got, "ok": good,
                     "reply": reply[:300]})
        mark = f"{G}✓{O}" if good else f"{R}✗{O}"
        print(f"  {mark} #{q['n']:>3}  want {q['short']:>6}  got {str(got):>6}   "
              f"{Y}{q['question'][:48]}{O}")
        if i % 10 == 0:
            pct = 100 * ok // max(1, ok + bad)
            print(f"      … {ok}/{ok+bad} ({pct}%)  {time.time()-t0:.0f}s elapsed")

    pct = 100 * ok // max(1, ok + bad)
    print(f"\n  RESULT: {ok}/{ok+bad} correct ({pct}%) in {time.time()-t0:.0f}s "
          f"via {mode}")

    out = {}
    if RESULTS.exists():
        try:
            out = json.loads(RESULTS.read_text("utf-8"))
        except Exception:  # noqa: BLE001
            out = {}
    out[mode] = {"correct": ok, "total": ok + bad, "pct": pct, "rows": rows}
    RESULTS.write_text(json.dumps(out, indent=1, ensure_ascii=False), "utf-8")
    print(f"  saved -> {RESULTS}")


if __name__ == "__main__":
    asyncio.run(main())
