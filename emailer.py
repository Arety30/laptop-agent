"""Mail for the bot — talks to the local mail MCP server (~/proton-mail-mcp) over its
streamable-HTTP endpoint (127.0.0.1:8973/mcp). Same server ChatGPT/Antigravity use, so the
bot sees every account: the three Proton addresses (via hydroxide) and the self-hosted mailbridge.

Public API (all async):
    send_email(subject, body, to=None, account=None)  -> {"ok": bool, ...}   (kept for bot.py/report emails)
    accounts()      list_messages(account, limit, unread_only)   read_message(uid, account)
    search(query, account, limit)                                 tool(name, **args)  (raw)
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import urllib.request

import config

MCP_URL = os.getenv("MAIL_MCP_URL", "http://127.0.0.1:8973/mcp")
DEFAULT_ACCOUNT = os.getenv("MAIL_DEFAULT_ACCOUNT", "personal")

_ids = itertools.count(1)
_LIST_TOOLS = {"list_accounts", "list_messages", "search_messages", "list_folders"}  # FastMCP splits lists into items
_session: str | None = None


def _post(payload: dict, session: str | None) -> tuple[dict | None, str | None]:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(MCP_URL, data=json.dumps(payload).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        sid = r.headers.get("Mcp-Session-Id") or session
        raw = r.read().decode("utf-8", "replace")
        ctype = r.headers.get("Content-Type", "")
    if not raw.strip():
        return None, sid
    if "text/event-stream" in ctype:
        msgs = [json.loads(l[5:].strip()) for l in raw.splitlines() if l.startswith("data:")]
        for m in reversed(msgs):
            if "result" in m or "error" in m:
                return m, sid
        return (msgs[-1] if msgs else None), sid
    return json.loads(raw), sid


def _call_sync(name: str, args: dict) -> dict:
    global _session
    for attempt in (1, 2):
        try:
            if _session is None:
                init = {"jsonrpc": "2.0", "id": next(_ids), "method": "initialize", "params": {
                    "protocolVersion": "2025-03-26", "capabilities": {},
                    "clientInfo": {"name": "laptop-agent", "version": "1"}}}
                _, sid = _post(init, None)
                _session = sid
                _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, _session)
            resp, _ = _post({"jsonrpc": "2.0", "id": next(_ids), "method": "tools/call",
                             "params": {"name": name, "arguments": args}}, _session)
            if resp is None:
                raise RuntimeError("empty MCP response")
            if "error" in resp:
                raise RuntimeError(resp["error"].get("message", str(resp["error"])))
            res = resp["result"]
            texts = [c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"]
            if res.get("isError"):
                raise RuntimeError(" ".join(texts)[:400])
            parsed = []
            for t in texts:
                try:
                    parsed.append(json.loads(t))
                except Exception:
                    parsed.append(t)
            if not parsed:
                return {"ok": True, "data": [] if name in _LIST_TOOLS else ""}
            return {"ok": True, "data": parsed if name in _LIST_TOOLS else parsed[0]}
        except urllib.error.HTTPError as e:  # stale session -> re-initialize once
            _session = None
            if attempt == 2 or e.code not in (400, 404):
                return {"ok": False, "error": f"MCP HTTP {e.code}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)[:300]}
    return {"ok": False, "error": "MCP call failed"}


async def tool(name: str, **args) -> dict:
    return await asyncio.to_thread(_call_sync, name, {k: v for k, v in args.items() if v is not None})


async def send_email(subject: str, body: str, to: str | None = None, account: str | None = None) -> dict:
    return await tool("send_email", to=to or config.EMAIL_TO, subject=subject, body=body,
                      account=account or DEFAULT_ACCOUNT)


async def accounts() -> dict:
    return await tool("list_accounts")


async def list_messages(account: str | None = None, limit: int = 10, unread_only: bool = False) -> dict:
    return await tool("list_messages", account=account or DEFAULT_ACCOUNT, limit=limit, unread_only=unread_only)


async def read_message(uid: str, account: str | None = None, max_chars: int = 3500) -> dict:
    return await tool("read_message", uid=str(uid), account=account or DEFAULT_ACCOUNT, max_chars=max_chars)


async def search(query: str, account: str | None = None, limit: int = 10) -> dict:
    return await tool("search_messages", query=query, account=account or DEFAULT_ACCOUNT, limit=limit)


# ---- Discord formatting helpers -------------------------------------------------------------
def fmt_list(items: list[dict], account: str) -> str:
    if not items:
        return f"📭 `{account}` — no messages."
    lines = [f"📬 **{account}** — latest {len(items)}:"]
    for m in items:
        flag = "🔵" if m.get("unread") else "⚪"
        frm = (m.get("from") or "?").replace("<", "‹").replace(">", "›")[:40]
        lines.append(f"{flag} `{m['uid']}` {m.get('date','')[:10]} **{frm}** — {m.get('subject','')[:70]}")
    lines.append("_`!read <uid> [account]` to open one_")
    return "\n".join(lines)


def fmt_message(m: dict) -> str:
    head = (f"**From:** {m.get('from','')}\n**To:** {m.get('to','')}\n**Date:** {m.get('date','')}\n"
            f"**Subject:** {m.get('subject','')}\n")
    if m.get("attachments"):
        head += f"**Attachments:** {', '.join(m['attachments'])}\n"
    return head + "\n" + (m.get("body") or "(empty)")
