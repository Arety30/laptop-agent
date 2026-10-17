"""Program-aided maths solving.

A 1-2B model is bad at mental arithmetic but much better at *writing a few lines
of Python*. So instead of asking for the answer, we ask for a short program,
execute it in a locked-down subprocess, and read the number it prints.

This is the standard "program-aided language model" trick, and it's the only
lever we have that doesn't require a bigger model.

Execution safety: model-written code runs in a separate process with no network
helpers, a whitelist of imports, a hard timeout, and output caps. It is still
model-generated code, so it runs with `-I` (isolated) and never touches the
bot's own process.
"""
from __future__ import annotations

import asyncio
import re
import sys
import tempfile
from pathlib import Path

import llm

_ALLOWED_IMPORTS = {"math", "itertools", "fractions", "decimal", "collections",
                    "re", "functools", "statistics", "random"}

_CODE_SYS = (
    "You solve maths puzzles by writing a short Python program.\n"
    "Rules:\n"
    "- Output ONLY a python code block, nothing else.\n"
    "- The program must print exactly one line: the final numeric answer.\n"
    "- Brute force is fine and encouraged (loop over possibilities).\n"
    "- You may import only: math, itertools, fractions, decimal, collections, "
    "functools, statistics.\n"
    "- No input(), no file access, no network.\n"
    "Example:\n"
    "```python\n"
    "total = 100\n"
    "print(total // 4)\n"
    "```"
)

_CODE_RE = re.compile(r"```(?:python)?\s*(.*?)```", re.S | re.I)


def _extract_code(reply: str) -> str | None:
    m = _CODE_RE.search(reply or "")
    code = (m.group(1) if m else reply or "").strip()
    if not code or "print" not in code:
        return None
    for imp in re.findall(r"^\s*(?:from|import)\s+([a-zA-Z_][\w.]*)", code, re.M):
        if imp.split(".")[0] not in _ALLOWED_IMPORTS:
            return None
    if re.search(r"\b(open|exec|eval|__import__|input|subprocess|os\.|socket)\b", code):
        return None
    return code


async def run_code(code: str, timeout: int = 12) -> dict:
    """Execute model-written code in an isolated subprocess."""
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "prog.py"
        f.write_text(code, "utf-8")
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-S", str(f),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=td)
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return {"ok": False, "error": f"timed out after {timeout}s"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}
    return {"ok": proc.returncode == 0,
            "stdout": out.decode("utf-8", "replace").strip()[:400],
            "stderr": err.decode("utf-8", "replace").strip()[:300]}


def _last_number(s: str) -> str | None:
    nums = re.findall(r"-?\d+(?:\.\d+)?(?:/\d+)?", s or "")
    return nums[-1] if nums else None


async def solve(question: str, model: str | None = None,
                role: str = "chatbot", attempts: int = 2) -> dict:
    """Solve by writing+running code. Returns {answer, method, code, output}."""
    import config

    prev = config.MODEL_ROUTING.get(role)
    if model:
        config.MODEL_ROUTING[role] = model
    try:
        for attempt in range(attempts):
            nudge = "" if attempt == 0 else (
                "\nYour last program failed. Write a simpler, correct one.")
            reply = await llm.complete(role, _CODE_SYS + nudge, question,
                                       temperature=0.2 if attempt else 0.0)
            code = _extract_code(reply)
            if not code:
                continue
            r = await run_code(code)
            if r.get("ok") and r.get("stdout"):
                ans = _last_number(r["stdout"])
                if ans is not None:
                    return {"answer": ans, "method": "code", "code": code,
                            "output": r["stdout"]}
        return {"answer": None, "method": "code-failed", "code": None, "output": ""}
    finally:
        if model and prev is not None:
            config.MODEL_ROUTING[role] = prev
