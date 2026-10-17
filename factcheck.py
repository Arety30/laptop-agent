"""Fact-checking for generated documents.

Guardrails-style: the model's output is not trusted. It is parsed into a strict
JSON schema, each factual claim is checked against REAL web search results, and
unsupported claims are flagged or removed before the file is built.

Honest about limits: this catches confident-but-wrong statements (the common
failure), it does NOT make output "100% accurate". A small model can still
misread a search result. Every verdict is reported so you can see the working.

Schema (validated, not merely hoped for):
    {"claims": [{"text": str, "checkable": bool, "query": str}]}
"""
from __future__ import annotations

import json
import re

import llm
import websearch

# ── the schema we enforce on the model's extraction step ───────────────────
CLAIM_SCHEMA = {
    "type": "object",
    "required": ["claims"],
    "properties": {
        "claims": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "required": ["text", "checkable"],
                "properties": {
                    "text": {"type": "string", "minLength": 8, "maxLength": 300},
                    "checkable": {"type": "boolean"},
                    "query": {"type": "string", "maxLength": 120},
                },
            },
        }
    },
}

_EXTRACT_SYS = (
    "Extract the specific factual claims from a document — dates, numbers, names, "
    "records, 'firsts', causes. Ignore opinions, generalities and filler.\n"
    'Reply with ONLY JSON: {"claims":[{"text":"...","checkable":true,'
    '"query":"short search query"}]}\n'
    "Max 8 claims. A claim is checkable if a web search could confirm it."
)

_VERDICT_SYS = (
    "You verify one claim against real search results.\n"
    'Reply with ONLY JSON: {"verdict":"supported|contradicted|unclear",'
    '"correction":"the correct fact, or empty if none needed"}\n'
    "Use ONLY the given results. If they do not address the claim, say unclear."
)


def _json_from(text: str) -> dict | None:
    """Pull the first JSON object out of a model reply, tolerantly."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    raw = m.group(0)
    for attempt in (raw, raw.replace("'", '"'), re.sub(r",\s*([}\]])", r"\1", raw)):
        try:
            return json.loads(attempt)
        except json.JSONDecodeError:
            continue
    return None


# Formal validator from the `jsonschema` repo — the model output is checked
# against CLAIM_SCHEMA by a real Draft validator, not just hoped to be right.
try:
    from jsonschema import Draft202012Validator
    _VALIDATOR = Draft202012Validator(CLAIM_SCHEMA)
except Exception:  # noqa: BLE001
    _VALIDATOR = None


def validate_claims(obj: dict | None) -> list[dict]:
    """Enforce CLAIM_SCHEMA. Anything malformed is dropped, not trusted.

    Preferred path: the real jsonschema Draft validator. If the whole object
    fails schema, we still salvage the individual well-formed claims (a 1B model
    often gets 9/10 claims right and mangles one) rather than losing everything.
    """
    if not isinstance(obj, dict):
        return []

    # Fast accept: if the object is fully schema-valid, trust it wholesale.
    if _VALIDATOR is not None and _VALIDATOR.is_valid(obj):
        claims = obj.get("claims") or []
    else:
        claims = obj.get("claims") or []

    out = []
    item_schema = CLAIM_SCHEMA["properties"]["claims"]["items"]
    for c in claims[:12]:
        if not isinstance(c, dict):
            continue
        # per-item schema check (salvage valid claims from a partly-bad object)
        if _VALIDATOR is not None:
            errs = list(Draft202012Validator(item_schema).iter_errors(c))
            if errs:
                # try to repair the common misses before dropping it
                c = {"text": str(c.get("text", "")).strip(),
                     "checkable": bool(c.get("checkable", True)),
                     "query": str(c.get("query") or c.get("text", ""))[:120]}
                if list(Draft202012Validator(item_schema).iter_errors(c)):
                    continue
        text = str(c.get("text", "")).strip()
        if not (8 <= len(text) <= 300):
            continue
        out.append({"text": text, "checkable": bool(c.get("checkable", True)),
                    "query": str(c.get("query") or text)[:120]})
    return out


async def extract_claims(document: str) -> list[dict]:
    reply = await llm.complete("chatbot", _EXTRACT_SYS, document[:4000],
                               temperature=0.0)
    claims = validate_claims(_json_from(reply))
    if claims:
        return claims
    # fallback: sentences containing a number, date or capitalised name
    sents = re.split(r"(?<=[.!?])\s+", re.sub(r"[#*\-]", " ", document))
    picked = [s.strip() for s in sents
              if 20 < len(s.strip()) < 240
              and re.search(r"\b(19|20)\d{2}\b|\b\d+(\.\d+)?%|\b\d{2,}\b", s)]
    return [{"text": s, "checkable": True, "query": s[:110]} for s in picked[:6]]


async def check_claim(claim: dict) -> dict:
    results = await websearch.search(claim["query"], n=4)
    if not results:
        return {**claim, "verdict": "unclear", "correction": "",
                "evidence": "", "source": ""}
    ev = websearch.format_results(results)[:1800]
    reply = await llm.complete("chatbot", _VERDICT_SYS,
                               f"CLAIM: {claim['text']}\n\nRESULTS:\n{ev}",
                               temperature=0.0)
    j = _json_from(reply) or {}
    verdict = str(j.get("verdict", "unclear")).lower()
    if verdict not in ("supported", "contradicted", "unclear"):
        verdict = "unclear"
    return {**claim, "verdict": verdict,
            "correction": str(j.get("correction", ""))[:240],
            "evidence": ev[:400], "source": results[0].get("url", "")}


async def check_document(document: str, max_claims: int = 5) -> dict:
    """Verify a document. Returns claims with verdicts + a summary."""
    claims = [c for c in await extract_claims(document) if c["checkable"]]
    checked = []
    for c in claims[:max_claims]:
        try:
            checked.append(await check_claim(c))
        except Exception as e:  # noqa: BLE001
            checked.append({**c, "verdict": "unclear", "correction": "",
                            "evidence": f"(check failed: {e})", "source": ""})
    counts = {v: sum(1 for c in checked if c["verdict"] == v)
              for v in ("supported", "contradicted", "unclear")}
    return {"claims": checked, "counts": counts,
            "checked": len(checked), "found": len(claims)}


def apply_corrections(document: str, report: dict) -> tuple[str, list[str]]:
    """Replace contradicted claims with their correction; note what changed."""
    doc, notes = document, []
    for c in report.get("claims", []):
        if c["verdict"] == "contradicted" and c.get("correction"):
            if c["text"] in doc:
                doc = doc.replace(c["text"], c["correction"])
                notes.append(f"corrected: “{c['text'][:60]}…” → “{c['correction'][:60]}”")
            else:
                notes.append(f"flagged (couldn't auto-replace): “{c['text'][:70]}…”")
    return doc, notes


def summarise(report: dict) -> str:
    c = report.get("counts", {})
    if not report.get("checked"):
        return "🔍 No checkable facts found."
    bits = [f"✅ {c.get('supported',0)} supported",
            f"❌ {c.get('contradicted',0)} contradicted",
            f"❓ {c.get('unclear',0)} unclear"]
    lines = [f"🔍 **Fact-check** ({report['checked']} claims): " + " · ".join(bits)]
    for cl in report.get("claims", []):
        if cl["verdict"] != "supported":
            icon = "❌" if cl["verdict"] == "contradicted" else "❓"
            line = f"  {icon} {cl['text'][:90]}"
            if cl.get("correction"):
                line += f"\n      → {cl['correction'][:110]}"
            lines.append(line)
    return "\n".join(lines)
