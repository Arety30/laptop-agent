"""Format solved maths results for Discord, grouped by week.

Reads twin/math_dataset.json (questions, weeks, real answers) and
twin/math_results.json (what each model answered) and renders a readable report:

    📗 Week 1
      ✅ Q1  How many van loads?          → 12
      ❌ Q2  An L-ish puzzle              → said 16, actually 5
"""
from __future__ import annotations

import json
from pathlib import Path

_TWIN = Path(__file__).resolve().parent / "twin"
_DATA = _TWIN / "math_dataset.json"
_RESULTS = _TWIN / "math_results.json"


def _load() -> tuple[dict, dict]:
    try:
        data = json.loads(_DATA.read_text("utf-8"))
    except Exception:  # noqa: BLE001
        data = {}
    try:
        res = json.loads(_RESULTS.read_text("utf-8"))
    except Exception:  # noqa: BLE001
        res = {}
    return data, res


def models() -> list[str]:
    return list(_load()[1].keys())


def _title(question: str, limit: int = 34) -> str:
    """The puzzle's name is the leading phrase before the body text."""
    t = question.strip()
    for stop in (". ", "? ", "! "):
        if stop in t[:60]:
            t = t.split(stop)[0]
            break
    return (t[:limit] + "…") if len(t) > limit else t


def report(model: str | None = None, week: int | None = None,
           only_wrong: bool = False, max_chars: int = 1850) -> list[str]:
    """Return Discord-sized chunks, grouped by week."""
    data, res = _load()
    if not data or not res:
        return ["No maths results yet — run `tests/solve_math_book.py` first."]

    model = model or next(iter(res))
    run = res.get(model)
    if not run:
        return [f"No results for `{model}`. Have: {', '.join(res) or 'none'}"]

    by_n = {q["n"]: q for q in data.get("solve", []) + data.get("train", [])}
    rows = run.get("rows", [])

    weeks: dict[int, list[str]] = {}
    for r in rows:
        q = by_n.get(r["n"], {})
        wk = q.get("week") or 0
        if week is not None and wk != week:
            continue
        good = r.get("ok")
        if only_wrong and good:
            continue
        mark = "✅" if good else "❌"
        got, want = r.get("got"), r.get("want")
        tail = f"→ **{want}**" if good else f"→ said `{got}`, actually **{want}**"
        weeks.setdefault(wk, []).append(
            f"{mark} Q{r['n']} {_title(q.get('question', ''))} {tail}")

    if not weeks:
        return ["Nothing to show for that filter."]

    total = len(rows)
    correct = sum(1 for r in rows if r.get("ok"))
    head = (f"📐 **Maths book results — `{model}`**\n"
            f"{correct}/{total} correct ({100*correct//max(1,total)}%) "
            f"· *The Ultimate Mathematical Challenge*\n")

    chunks, buf = [], head
    for wk in sorted(weeks):
        block = f"\n📗 **Week {wk}**\n" + "\n".join(f"  {l}" for l in weeks[wk]) + "\n"
        if len(buf) + len(block) > max_chars:
            chunks.append(buf)
            buf = block
        else:
            buf += block
    if buf.strip():
        chunks.append(buf)
    return chunks


def summary() -> str:
    """One-line scoreboard across every model benchmarked."""
    _, res = _load()
    if not res:
        return "No maths runs recorded yet."
    lines = ["📊 **Maths scoreboard** (same puzzles, same prompt)"]
    for m, r in sorted(res.items(), key=lambda kv: -kv[1].get("pct", 0)):
        lines.append(f"  `{m}` — {r.get('correct')}/{r.get('total')} "
                     f"({r.get('pct')}%)")
    return "\n".join(lines)
