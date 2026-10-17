"""LangChain → Ollama router. Primary path is langchain_ollama.ChatOllama.
Falls back to Ollama's HTTP API directly if LangChain isn't importable
(handy on bleeding-edge Python), so the bot runs either way."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

import config

# Qwen3 emits <think>…</think> reasoning; we disable it (/no_think) for speed and
# low RAM, and strip any that slips through so agents/parsers see clean output.
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _clean(text: str) -> str:
    return _THINK.sub("", text or "").strip()


def _is_qwen3(role: str) -> bool:
    return _model_for(role).startswith("qwen3")

_USING_LANGCHAIN = False
try:
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    from langchain_ollama import ChatOllama

    _USING_LANGCHAIN = True
except Exception:  # noqa: BLE001
    ChatOllama = None  # type: ignore


_CLIENTS: dict[str, object] = {}


def _model_for(role: str) -> str:
    return config.MODEL_ROUTING.get(role, config.LIGHT_MODEL)


def _options_for(role: str) -> dict:
    if role == "reasoner":
        return {"temperature": 0.4, "num_ctx": 8192}
    if role in config.CODER_ROLES:
        return {"temperature": 0.2, "num_ctx": 8192}
    if role in ("chatbot", "twin"):
        # low temp + short cap so a 1B model stays on-fact and doesn't embellish/ramble
        return {"temperature": 0.25, "num_ctx": 4096, "num_predict": 110}
    return {"temperature": 0.6, "num_ctx": 4096}


def _client(role: str, temperature: float | None = None):
    model = _model_for(role)
    opts = _options_for(role)
    temp = opts["temperature"] if temperature is None else temperature
    key = f"{role}:{model}:{temp}"
    if key not in _CLIENTS:
        _CLIENTS[key] = ChatOllama(
            model=model,
            base_url=config.OLLAMA_HOST,
            temperature=temp,
            num_ctx=opts["num_ctx"],
        )
    return _CLIENTS[key]


async def complete(role: str, system: str, user: str,
                   temperature: float | None = None,
                   history: list[tuple[str, str]] | None = None) -> str:
    """One-shot completion. `temperature` overrides the role default. `history` is
    prior turns [(role, content), ...] passed as REAL messages (not a text blob),
    so small models handle multi-turn context without regurgitating."""
    if config.LLM_BACKEND == "openrouter" and config.OPENROUTER_API_KEY:
        return _clean(await _raw_openrouter(role, system, user, temperature, history))
    if _is_qwen3(role):
        user = f"{user}\n\n/no_think"   # disable Qwen3 reasoning trace
    if _USING_LANGCHAIN:
        client = _client(role, temperature)
        msgs: list = [SystemMessage(content=system)]
        for r, c in (history or []):
            msgs.append(AIMessage(content=c) if r == "assistant" else HumanMessage(content=c))
        msgs.append(HumanMessage(content=user))
        resp = await client.ainvoke(msgs)
        return _clean(resp.content)
    return _clean(await _raw_ollama(role, system, user, temperature, history))


async def _raw_ollama(role: str, system: str, user: str,
                      temperature: float | None = None,
                      history: list[tuple[str, str]] | None = None) -> str:
    """Fallback: call Ollama /api/chat directly (no LangChain)."""
    import asyncio

    opts = dict(_options_for(role))
    if temperature is not None:
        opts["temperature"] = temperature
    messages = [{"role": "system", "content": system}]
    for r, c in (history or []):
        messages.append({"role": "assistant" if r == "assistant" else "user", "content": c})
    messages.append({"role": "user", "content": user})
    payload = {
        "model": _model_for(role),
        "messages": messages,
        "stream": False,
        "think": False,
        "options": opts,
    }

    def _post() -> str:
        req = urllib.request.Request(
            f"{config.OLLAMA_HOST}/api/chat",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=600) as r:
            data = json.loads(r.read().decode())
        return (data.get("message", {}).get("content") or "").strip()

    return await asyncio.to_thread(_post)


async def _raw_openrouter(role: str, system: str, user: str,
                          temperature: float | None = None,
                          history: list[tuple[str, str]] | None = None) -> str:
    """Call OpenRouter's OpenAI-compatible chat API (cloud brain, :free models)."""
    import asyncio

    opts = _options_for(role)
    temp = opts["temperature"] if temperature is None else temperature
    messages = [{"role": "system", "content": system}]
    for r, c in (history or []):
        messages.append({"role": "assistant" if r == "assistant" else "user", "content": c})
    messages.append({"role": "user", "content": user})
    payload = {
        "model": config.OPENROUTER_MODEL,
        "messages": messages,
        "temperature": temp,
        "max_tokens": opts.get("num_predict", 512),
        "stream": False,
    }

    def _post() -> str:
        req = urllib.request.Request(
            f"{config.OPENROUTER_BASE}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + config.OPENROUTER_API_KEY,
                "HTTP-Referer": "https://example.com",
                "X-Title": "laptop-agent",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            try:
                msg = json.loads(body).get("error", {}).get("message", body)
            except Exception:  # noqa: BLE001
                msg = body
            raise RuntimeError(f"OpenRouter {e.code}: {msg[:200]}") from None
        return (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()

    return await asyncio.to_thread(_post)


def backend_name() -> str:
    if config.LLM_BACKEND == "openrouter" and config.OPENROUTER_API_KEY:
        return f"openrouter ({config.OPENROUTER_MODEL})"
    return "langchain_ollama.ChatOllama" if _USING_LANGCHAIN else "ollama-http (fallback)"
