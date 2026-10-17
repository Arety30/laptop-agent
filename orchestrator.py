"""The brain: parses agent action blocks, runs the autonomous tool-loop, and
drives the seven-agent pipeline
    planner → plan_reviewer → coder → tester → debugger → report_writer
plus a standalone chatbot for casual messaging.

`progress` is an async callback(str) the bot uses to post chatty updates.
"""
from __future__ import annotations

import re
from typing import Awaitable, Callable

import agents
import config
import enforcer
import grounding
import knowledge
import llm
import userprofile as profile
import tools
import vmtools
import websearch

Progress = Callable[[str], Awaitable[None]]

# ── action parsing ─────────────────────────────────────────────────────────
_SHELL = re.compile(r"<shell>(.*?)</shell>", re.DOTALL)
_VM = re.compile(r"<vm>(.*?)</vm>", re.DOTALL)
_WEBSEARCH = re.compile(r"<search>(.*?)</search>", re.DOTALL)
_WEBOPEN = re.compile(r"<open>(.*?)</open>", re.DOTALL)
_READ = re.compile(r"<read>(.*?)</read>", re.DOTALL)
_LS = re.compile(r"<ls>(.*?)</ls>", re.DOTALL)
_WRITE = re.compile(r'<write\s+path="(.*?)">(.*?)</write>', re.DOTALL)
_APPEND = re.compile(r'<append\s+path="(.*?)">(.*?)</append>', re.DOTALL)
_EDIT = re.compile(r'<edit\s+path="(.*?)"\s+find="(.*?)">(.*?)</edit>', re.DOTALL)
_MKDIR = re.compile(r"<mkdir>(.*?)</mkdir>", re.DOTALL)
_MOVE = re.compile(r'<move\s+from="(.*?)"\s+to="(.*?)"\s*/>', re.DOTALL)
_DELETE = re.compile(r"<delete>(.*?)</delete>", re.DOTALL)
_DONE = re.compile(r"<done>(.*?)</done>", re.DOTALL)


async def _execute_actions(text: str, progress: Progress) -> tuple[str, str | None]:
    """Run every action block found in `text`.
    Returns (observations, done_summary_or_None)."""
    obs: list[str] = []

    for m in _WRITE.finditer(text):
        path, content = m.group(1).strip(), m.group(2).strip("\n")
        r = tools.write_file(path, content)
        await progress(f"✍️  write `{path}` → {'ok' if r['ok'] else r.get('error')}")
        obs.append(f"[write {path}] {r}")

    for m in _APPEND.finditer(text):
        path, content = m.group(1).strip(), m.group(2).strip("\n")
        r = tools.append_file(path, content)
        await progress(f"➕  append `{path}` → {'ok' if r['ok'] else r.get('error')}")
        obs.append(f"[append {path}] {r}")

    for m in _EDIT.finditer(text):
        path, find, repl = m.group(1).strip(), m.group(2), m.group(3)
        r = tools.edit_file(path, find, repl)
        await progress(f"🩹  edit `{path}` → {'ok' if r['ok'] else r.get('error')}")
        obs.append(f"[edit {path}] {r}")

    for m in _MKDIR.finditer(text):
        path = m.group(1).strip()
        r = tools.make_dir(path)
        obs.append(f"[mkdir {path}] {r}")

    for m in _MOVE.finditer(text):
        src, dst = m.group(1).strip(), m.group(2).strip()
        r = tools.move_path(src, dst)
        await progress(f"🚚  move `{src}` → `{dst}` ({'ok' if r['ok'] else r.get('error')})")
        obs.append(f"[move {src}->{dst}] {r}")

    for m in _DELETE.finditer(text):
        path = m.group(1).strip()
        r = tools.delete_path(path)
        await progress(f"🗑️  delete `{path}` → {'ok' if r['ok'] else r.get('error')}")
        obs.append(f"[delete {path}] {r}")

    for m in _READ.finditer(text):
        path = m.group(1).strip()
        r = tools.read_file(path)
        obs.append(f"[read {path}]\n{r.get('content', r.get('error'))}")

    for m in _LS.finditer(text):
        path = m.group(1).strip()
        r = tools.list_dir(path)
        obs.append(f"[ls {path}]\n" + "\n".join(r.get("entries", [r.get("error", "")])))

    for m in _SHELL.finditer(text):
        cmd = m.group(1).strip()
        await progress(f"⚙️  laptop `$ {cmd[:120]}`")
        r = await tools.run_shell(cmd)
        tail = (r["stdout"] or r["stderr"])[-500:]
        obs.append(f"[shell rc={r['returncode']}] {cmd}\n{r['stdout']}\n{r['stderr']}")
        if tail.strip():
            await progress(f"↳ `{tail.strip()[:180]}`")

    for m in _VM.finditer(text):
        cmd = m.group(1).strip()
        await progress(f"🖥️  VM `$ {cmd[:120]}`")
        r = await vmtools.run_in_vm(cmd)
        out = (r.get("stdout") or r.get("stderr") or r.get("error") or "")
        obs.append(f"[vm rc={r.get('returncode')}] {cmd}\n{out}")
        if out.strip():
            await progress(f"↳ VM `{out.strip()[-180:]}`")

    for m in _WEBSEARCH.finditer(text):
        q = m.group(1).strip()
        await progress(f"🔎  web search `{q[:100]}`")
        results = await websearch.search(q, n=5)
        obs.append(f"[search {q}]\n{websearch.format_results(results)}")

    for m in _WEBOPEN.finditer(text):
        url = m.group(1).strip()
        await progress(f"🌐  open `{url[:100]}`")
        page = await websearch.fetch(url, 3500)
        if page.get("ok"):
            obs.append(f"[open {url}] {page['title']}\n{page['text']}")
        else:
            obs.append(f"[open {url}] ERROR {page.get('error')}")

    done = _DONE.search(text)
    return "\n\n".join(obs) if obs else "(no actions executed)", (
        done.group(1).strip() if done else None
    )


async def run_agent_loop(role: str, goal: str, progress: Progress,
                         max_iters: int | None = None) -> tuple[str, list[str]]:
    """Run an autonomous agent until it emits <done> or hits the iteration cap.
    Returns (final_summary, transcript)."""
    max_iters = max_iters or config.MAX_AGENT_ITERATIONS
    system = agents.SYSTEM_PROMPTS[role]
    transcript: list[str] = []
    convo = f"GOAL:\n{goal}\n\nBegin."

    for i in range(1, max_iters + 1):
        reply = await llm.complete(role, system, convo)
        transcript.append(f"--- {role} step {i} ---\n{reply}")
        obs, done = await _execute_actions(reply, progress)
        if done is not None:
            return done, transcript
        convo = (
            f"GOAL:\n{goal}\n\n"
            f"Your last actions produced these results:\n{obs}\n\n"
            f"Continue. If the goal is met, emit <done>SUMMARY</done>."
        )
    return f"(stopped after {max_iters} steps without <done>)", transcript


# ── the pipeline ───────────────────────────────────────────────────────────
async def run_pipeline(task: str, progress: Progress) -> dict:
    log: dict = {"task": task}

    # optional: hand the whole thing to OpenManus instead of the built-in loop
    if config.OPENMANUS_ENABLED:
        import openmanus
        await progress("🌐 Delegating to OpenManus…")
        log["openmanus"] = await openmanus.run(task)

    # 1. plan
    await progress("🧠 **Planner** is thinking through the approach…")
    plan = await llm.complete("planner", agents.SYSTEM_PROMPTS["planner"], f"TASK:\n{task}")
    log["plan"] = plan
    await progress(f"📋 Plan:\n```\n{plan[:1200]}\n```")

    # 2. review (one revision round)
    await progress("🔍 **Plan-reviewer** is checking it…")
    review = await llm.complete(
        "plan_reviewer", agents.SYSTEM_PROMPTS["plan_reviewer"],
        f"TASK:\n{task}\n\nPROPOSED PLAN:\n{plan}",
    )
    log["review"] = review
    if review.strip().upper().startswith("REVISED"):
        plan = review.split("\n", 1)[1] if "\n" in review else review
        await progress("♻️ Reviewer revised the plan; using the updated version.")
    else:
        await progress("✅ Reviewer approved the plan.")

    # 3. add resources (install deps, make dirs, fetch assets the plan needs)
    await progress("📦 **Resource adder** is preparing what the build needs…")
    res_goal = f"TASK:\n{task}\n\nAPPROVED PLAN:\n{plan}"
    resources, res_tx = await run_agent_loop("resource_adder", res_goal, progress)
    log["resources"] = {"summary": resources, "transcript": res_tx}
    await progress(f"📦 Resources: {resources[:300]}")

    # 4. code
    await progress("👩‍💻 **Coder** is implementing…")
    coder_goal = (f"TASK:\n{task}\n\nAPPROVED PLAN:\n{plan}\n\n"
                  f"RESOURCES ALREADY PREPARED (installed/created for you):\n{resources}")
    coder_summary, coder_tx = await run_agent_loop("coder", coder_goal, progress)
    log["coder"] = {"summary": coder_summary, "transcript": coder_tx}
    await progress(f"✅ Coder: {coder_summary[:300]}")

    # 4. test
    await progress("🧪 **Tester** is verifying it…")
    test_goal = (f"TASK:\n{task}\n\nWhat the coder did:\n{coder_summary}\n\n"
                 f"Verify it actually works.")
    test_verdict, test_tx = await run_agent_loop("tester", test_goal, progress)
    log["tester"] = {"summary": test_verdict, "transcript": test_tx}
    passed = "PASS" in test_verdict.upper() and "FAIL" not in test_verdict.upper()
    await progress(f"{'🟢' if passed else '🔴'} Tester: {test_verdict[:300]}")

    # 5. debug (only if tests failed)
    if not passed:
        await progress("🐞 **Debugger** is fixing the failure…")
        dbg_goal = (f"TASK:\n{task}\n\nTester reported:\n{test_verdict}\n\n"
                    f"Diagnose and fix until it works.")
        dbg_summary, dbg_tx = await run_agent_loop("debugger", dbg_goal, progress)
        log["debugger"] = {"summary": dbg_summary, "transcript": dbg_tx}
        await progress(f"🔧 Debugger: {dbg_summary[:300]}")
    else:
        log["debugger"] = {"summary": "skipped (tests passed)"}

    # 6. report
    await progress("📝 **Report-writer** is summarising for you…")
    report_input = (
        f"TASK:\n{task}\n\nPLAN:\n{plan}\n\n"
        f"RESOURCES: {resources}\n\n"
        f"CODER: {coder_summary}\n\nTESTER: {test_verdict}\n\n"
        f"DEBUGGER: {log['debugger']['summary']}"
    )
    report = await llm.complete("report_writer", agents.SYSTEM_PROMPTS["report_writer"],
                                report_input)
    log["report"] = report
    log["passed"] = passed
    return log


# ── chatbot with REAL web search + web-viewing agent ───────────────────────
_SEARCH_ACT = re.compile(r"<search>(.*?)</search>", re.DOTALL)
_OPEN_ACT = re.compile(r"<open>(.*?)</open>", re.DOTALL)
_TAG_STRIP = re.compile(r"</?(search|open|done)>", re.IGNORECASE)

# phrases that clearly mean "this needs the live web", so we search even if the
# little model forgets to emit <search>
_SEARCH_TRIGGERS = (
    "search the web", "search web", "look up", "look it up", "google",
    "on the web", "latest", "current", "right now", "today", "this week",
    "this year", "news", "who won", "did they win", "did they", "winner",
    "score", "scores", "final score", "standings", "results", "result of",
    "price", "weather", "recent", "happening", "released", "release date",
    "who is", "who are", "when did", "when is", "where is", "how many",
    "research", "look into", "find out", "info on", "information on",
)

# factual questions we should look up rather than answer from memory
_WH = re.compile(r"^\s*(who|what|when|where|which|whose|how (many|much|old|far|long)|"
                 r"did|does|do |is |are |was |were |name the|list the)", re.I)
# ...but NOT these casual / self-referential / personal-preference ones (answer as
# yourself, don't web-search them)
_CASUAL = ("how are you", "how's it going", "hows it going", "how are u", "what's up",
           "whats up", "who are you", "what are you", "your name", "my name",
           "who am i", "how do you", "what can you do", "what do you do",
           "1+1", "2+2", "remind me", "what did i", "remember", "do you",
           "fav ", "fav/", "favorite", "favourite", "you like", "you play",
           "you watch", "you into", "you prefer", "you think", "your opinion",
           "wanna", "want to", "you feel", "you enjoy",
           # definitions/explanations the model already knows — no search
           "what does", "what's the meaning", "whats the meaning", "meaning of",
           "definition of", "what is the meaning", "explain",
           # "did/have YOU ..." = personal, answer as self
           "did you", "have you", "you ever", "you been")


# chatty/emotional statements — never web-search these, just talk
_CONVERSATIONAL = (
    "bro", "lol", "lmao", "stop the act", "no way", "i just", "we have been",
    "we were", "you and i", "us two", "remember when", "i think you", "trust me",
    "im serious", "i'm serious", "come on", "cmon", "dude", "mate", "yaar",
    "shut up", "stop it", "for real", "fr fr", "swear", "wdym", "wym",
)


# signals that the message contains a CHECKABLE CLAIM — these win over casual
# phrasing, so "bro windows 67 by google is..." still gets verified
_CLAIM = (
    re.compile(r"\b(made|created|developed|owned|built|released|launched|invented)\s+by\b"),
    re.compile(r"\b(you )?(heard|know) (of|about)\b"),
    re.compile(r"\b(right|yeah|correct|isnt it|isn't it)\s*\??$"),
    re.compile(r"\b(is|are|was|were|will be|gonna be)\s+(the\s+)?(best|first|new|biggest|"
               r"fastest|latest|worst|only)\b"),
    re.compile(r"\b[a-z]+\s?\d{1,3}\b.*\b(by|from|of)\b"),      # "windows 67 by google"
    re.compile(r"(?<!\d)(19|20)\d{2}(?!\d)"),                    # any year, even "fifa2024"
    re.compile(r"\b(won|beat|scored|signed|died|born|launched)\b.*(?<!\d)(19|20)\d{2}"),
    # sports/result claims ("england won ... fifa", "scored a hattrick") — classic
    # bait the model otherwise just agrees with
    re.compile(r"\b(won|beat|scored|hat.?trick|hattrick)\b.*\b(fifa|world ?cup|final|"
               r"league|cup|match|england|argentina|france|spain|brazil|messi|ronaldo)\b"),
    re.compile(r"\b(fifa|world ?cup)\b.*\b(won|winner|champion|final|score)\b"),
)


def _has_claim(m: str) -> bool:
    return any(p.search(m) for p in _CLAIM)


# "…right?" alone is a weak signal: it also ends questions about HIM. The rest of
# _CLAIM (attribution, years, sports results) are strong, checkable claims.
_CLAIM_STRONG = tuple(p for p in _CLAIM if "isnt it" not in p.pattern)
_PERSONAL_PREF = re.compile(r"\b(you|u|ur|your)\s+(like|liked|love|hate|prefer|enjoy|"
                            r"watch|watched|play|played|use|used|got|have)\b")


def _about_him(m: str) -> bool:
    """A question about his own tastes/habits — answer as self, never web-search."""
    return bool(_PERSONAL_PREF.search(m)) and not any(p.search(m) for p in _CLAIM_STRONG)


# maths/calculation — compute it, never web-search it
_MATH = (
    re.compile(r"\d\s*[\+\-\*/x×÷^]\s*\d"),                     # 2+2, 15 * 7
    re.compile(r"\b\d+(\.\d+)?\s*%|\bpercent\b"),                # 15% of 240
    re.compile(r"\b(plus|minus|times|multiplied|divided|square root|squared|cubed)\b"),
    re.compile(r"\bsolve for\b|\b\d*x\s*[\+\-=]"),               # solve for x: 3x+5=20
    re.compile(r"\baverage of\b|\bsum of\b|\btotal of\b"),
    re.compile(r"\bhow many\b.*\bin\b.*\d|\bhow many\b.*\d.*\b(each|per)\b"),
    re.compile(r"\b\d+\b.*\b\d+\b.*\bhow (many|much)\b"),        # "5 boxes ... 12 apples ... how many"
    re.compile(r"\bhow (many|much)\b.*\b\d+\b.*\b\d+\b"),
    re.compile(r"\b\d+\s*(km|kg|m|cm|mins?|minutes?|hours?|secs?|seconds?|rupees?|dollars?)\b"
               r".*\b(how|what|whats)\b"),
    re.compile(r"\b(whats|what is|calculate|work out)\b.*\d.*\b(of|divided|times|plus|minus)\b"),
)


def _is_math(m: str) -> bool:
    return any(p.search(m) for p in _MATH)


def _wants_web(msg: str) -> bool:
    """True if the message is a factual/current-info question worth searching —
    Abrar prefers a real search over a hallucinated guess."""
    m = msg.lower().strip()
    # maths is computed, never looked up
    if _is_math(m):
        return False
    # a checkable claim gets VERIFIED before anything else — even wrapped in
    # "did you see…/have you heard…" bait ("did you see england won fifa2024"),
    # so the model can't just agree with a false premise and fabricate details
    # "you like ninjago right" is about him, not a lookup — but "gemma3 made by
    # alibaba, right?" still goes to verification via _CLAIM_STRONG.
    if _about_him(m):
        return False
    if _has_claim(m):
        return True
    # questions about HIM ("you like X right?") are never lookups — answer as self
    if any(c in m for c in _CASUAL):
        return False
    # chatty statements aimed at YOU aren't lookups (e.g. "bro stop the act")
    if any(c in m for c in _CONVERSATIONAL):
        return False
    # conversational follow-ups ("which one", "which mach are you talkin about")
    # refer to the chat, not the web — searching them returns nonsense
    if re.search(r"\b(you|u)\s+(talkin|talking|mean|on about|sayin|saying)\b", m):
        return False
    if len(m.split()) <= 4 and re.match(r"^(which|what|who|and|but|so|why|how)\b", m):
        return False
    # a claim put to you for confirmation ("messi is english right?") — verify it
    # with a real search instead of agreeing or shrugging
    if re.search(r"\b(right|yeah|correct|isnt it|isn't it|no\?)\s*\??$", m) and len(m.split()) >= 3:
        return True
    # attribution claims ("gemma3 made by alibaba", "heard of X by Y") — verify,
    # these are the classic bait the model otherwise just agrees with
    if re.search(r"\b(made|created|developed|owned|built|released)\s+by\b", m):
        return True
    if re.search(r"\b(you )?(heard|know) (of|about)\b", m) and len(m.split()) >= 4:
        return True
    if any(t in m for t in _SEARCH_TRIGGERS):
        return True
    return bool(_WH.match(m)) and len(m) > 12


def _strip_tags(s: str) -> str:
    s = _SEARCH_ACT.sub("", s)
    s = _OPEN_ACT.sub("", s)
    return _TAG_STRIP.sub("", s).strip()


_ROLEPLAY = re.compile(r"\*[^*\n]{1,50}\*")           # *shrugs*, *chuckles*
# 1B/3B models sometimes narrate about "the user" instead of talking TO him
_THIRD_PERSON = re.compile(r"\bthe user (is|was|has|seems|appears|says|said|claims|"
                           r"makes|made|wants|asked)\b[^.!?]*[.!?]\s*", re.I)
_PREFIX = re.compile(r"^\s*(me|you|abrar|assistant|bot|user|twin)\s*:\s*", re.I)
_FULLNAME = re.compile(r"\bSyed\s+Abrar\s+Ali\b", re.I)
_FAKE_SOURCE = re.compile(r"\n*\s*\(?\s*source\s*:.*$", re.I | re.S)


def _clean_reply(text: str, had_search: bool = False) -> str:
    """Strip the artifacts a 1B model leaks: roleplay asterisks, role prefixes,
    the full name, and invented 'Source:' lines when no real search happened."""
    t = _strip_tags(text)
    t = _ROLEPLAY.sub("", t)
    t = _THIRD_PERSON.sub("", t)
    t = _FULLNAME.sub("", t)
    for _ in range(3):                                # peel repeated "me: you: ..."
        nt = _PREFIX.sub("", t)
        if nt == t:
            break
        t = nt
    if not had_search:                                # no web search => no citation
        t = _FAKE_SOURCE.sub("", t)
    t = grounding.collapse_repeats(t)                 # kill 1B stutter loops
    t = re.sub(r"[ \t]{2,}", " ", t).strip()
    return t or "…"


def _parse_history(history: str, max_turns: int = 6) -> list[tuple[str, str]]:
    """Turn the 'User: ..\nMe: ..' log into real (role, content) message turns."""
    turns: list[tuple[str, str]] = []
    for line in (history or "").splitlines():
        line = line.strip()
        if line.startswith("User:"):
            turns.append(("user", line[5:].strip()))
        elif line.startswith("Me:"):
            turns.append(("assistant", line[3:].strip()))
        elif turns:                      # continuation of the previous message
            r, c = turns[-1]
            turns[-1] = (r, f"{c} {line}".strip())
    return [t for t in turns if t[1]][-max_turns:]


async def chat(message: str, history: str = "", extra_context: str = "") -> str:
    """Casual chatbot (llama3.2:1b): real web search, adaptive user profile, and
    optional image/file context via extra_context."""
    sys = agents.SYSTEM_PROMPTS["chatbot"]
    prof = profile.get_context()
    if prof:
        sys = f"{sys}\n\n{prof}"

    ctx = ""
    if extra_context:
        ctx += extra_context.strip() + "\n\n"
    fact = knowledge.fact_for(message)   # ground answers in his real YouTube data
    if fact:
        ctx += fact + "\n\n"

    # history goes in as REAL message turns (not a text blob) — a 1B model
    # regurgitates blobs verbatim instead of treating them as context
    turns = _parse_history(history)

    query = None
    om = None
    # FAST PATH: if it obviously needs a lookup, skip the wasted pass-1 LLM call
    # (a 1B call is ~15s on CPU) and go straight to search + a single answer pass.
    if _wants_web(message):
        query = re.sub(r"(?i)\b(search( the)? web( for)?|look ?up|google)\b", "",
                       message).strip() or message
    else:
        # pass 1: casual answer (the model may still choose to search/open)
        reply = await llm.complete("chatbot", sys, f"{ctx}{message}", history=turns)
        sm = _SEARCH_ACT.search(reply)
        if sm:
            query = sm.group(1).strip()
        om = _OPEN_ACT.search(reply)

    if not query and not om:
        # ENFORCED: the reply must satisfy rules.json, or it gets regenerated /
        # replaced. The prompt asks; this guarantees.
        async def _regen(correction: str) -> str:
            out = await llm.complete("chatbot", f"{sys}\n\nIMPORTANT: {correction}",
                                     f"{ctx}{message}", history=turns)
            return _clean_reply(out, had_search=False)

        gated = await enforcer.enforce(_clean_reply(reply, had_search=False), _regen)
        return grounding.hedge_if_unverified(gated, had_search=False)

    # gather REAL web context
    web_ctx = ""
    source_url = ""
    if query:
        results = await websearch.search(query, n=4)
        web_ctx += "WEB SEARCH RESULTS:\n" + websearch.format_results(results) + "\n\n"
        # snippets are enough for chat — skip the extra full-page fetch (much faster)
        if results:
            source_url = results[0]["url"]
    if om:
        page = await websearch.fetch(om.group(1).strip(), 2500)
        if page.get("ok"):
            source_url = source_url or page.get("url", "")
            web_ctx += f"PAGE ({page['title']}) {page['url']}:\n{page['text'][:2000]}\n\n"

    if not web_ctx.strip():
        return "couldn't find anything on that, sorry"

    # pass 2: answer FAITHFULLY from results (very low temp so a 1B model doesn't
    # wander off and invent names). Keep it short, no made-up source line.
    # minimal system + no chat history: a focused factual answer, avoids profile bleed
    fact_sys = ("You are Abrar: casual, short, lowercase-ish, first person. You check "
                "things before believing them and you're not afraid to say something is "
                "wrong. Never add unrelated info about yourself.")
    prompt = (
        f"Your friend just said: {message}\n\n{web_ctx}"
        "Reply DIRECTLY TO HIM (say 'you', never 'the user'), 1-3 short sentences, "
        "using ONLY the results above:\n"
        "• If he's wrong or made it up, say so ('nah thats not a thing') and then say "
        "what's ACTUALLY true — name the right company/person/fact.\n"
        "• If he's right, confirm it briefly.\n"
        "• If the results don't cover it, say you couldn't find anything on it.\n"
        "Copy names/numbers exactly from the results. Do NOT write a Source line."
    )
    final = await llm.complete("chatbot", fact_sys, prompt, temperature=0.1)
    final = _FAKE_SOURCE.sub("", _clean_reply(final, had_search=True)).strip()
    if not final:
        return "couldn't find a clear answer on that"
    # if the model itself says it found nothing, don't slap a source link on it —
    # citing a page that doesn't answer the question is worse than saying "dunno"
    low = final.lower()
    if any(p in low for p in ("unable to find", "couldn't find", "could not find",
                              "no information", "not sure", "dont know", "don't know",
                              "cant find", "can't find", "didn't find")):
        return final
    # only cite a source if it plausibly relates — a random youtube/shopping link
    # under a factual correction is worse than no link at all
    final = await enforcer.enforce(final)          # ENFORCED, same as the chat path
    if source_url and _source_is_relevant(source_url, message, final):
        return f"{final}\nsource: {source_url}"
    return final


_JUNK_DOMAINS = ("youtube.com", "youtu.be", "tiktok.com", "pinterest.", "amazon.",
                 "ebay.", "facebook.com", "instagram.com", "quora.com", "reddit.com/r/all")


_TRUSTED_DOMAINS = ("wikipedia.org", "arxiv.org", "britannica.com", ".gov", ".edu",
                    "bbc.co", "bbc.com", "reuters.com", "apnews.com", "nature.com",
                    "espn.com", "uefa.com", "fifa.com", "docs.python.org",
                    "developer.mozilla.org", "github.com", "ollama.com")


def _source_is_relevant(url: str, question: str, answer: str) -> bool:
    u = url.lower()
    if any(d in u for d in _JUNK_DOMAINS):
        return False
    if any(d in u for d in _TRUSTED_DOMAINS):     # authoritative: cite regardless
        return True
    # does the URL share a meaningful word with the question or the answer?
    words = {w for w in re.findall(r"[a-z]{4,}", (question + " " + answer).lower())
             if w not in ("that", "this", "with", "from", "your", "thing", "isnt",
                          "thats", "have", "been", "what", "about", "there")}
    return any(w in u for w in words)


_MATH_SYS = (
    "You are a careful mathematician solving competition puzzles. Work through the "
    "problem step by step, keep it brief, and finish with the final answer alone on "
    "the last line in exactly this form:\nANSWER: <value>\n"
    "The value must be a plain number (or a single letter for multiple choice). "
    "Never write a URL. If you cannot solve it, still give your best numeric guess."
)


async def solve_math(question: str, use_reasoner: bool = False,
                     model: str | None = None) -> str:
    """Solve a maths problem.

    Deliberately bypasses web search AND persona enforcement: those exist for
    chatting as Abrar, and they corrupt maths answers (a search turns a puzzle
    into a link, and the persona rules can replace a valid answer with a
    fallback line). This is a pure model->answer path.
    """
    role = "reasoner" if use_reasoner else "chatbot"
    if model:
        # benchmark an arbitrary ollama model without touching config/.env
        prev = config.MODEL_ROUTING.get(role)
        config.MODEL_ROUTING[role] = model
        try:
            out = await llm.complete(role, _MATH_SYS, question, temperature=0.1)
        finally:
            if prev is not None:
                config.MODEL_ROUTING[role] = prev
    else:
        out = await llm.complete(role, _MATH_SYS, question, temperature=0.1)
    return (out or "").strip()


_DOC_SYS = {
    "slides": ("You write presentation content in Markdown for the marp slide tool.\n"
               "Rules: start each slide with '# Title', 3-5 short bullets per slide, "
               "separate slides with a line containing only '---'. 5-8 slides. "
               "No preamble, no explanation — output ONLY the markdown."),
    "pptx": ("You write presentation content in Markdown.\n"
             "Rules: each slide is '# Title' followed by 3-5 short '- bullet' lines. "
             "5-8 slides. No preamble — output ONLY the markdown."),
    "report": ("You write a clear document in Markdown.\n"
               "Rules: start with '# Title', then sections with '## Heading' and short "
               "paragraphs plus bullets where useful. 400-700 words. "
               "No preamble — output ONLY the markdown."),
}
_DOC_SYS["pdf"] = _DOC_SYS["report"]


async def write_document(kind: str, topic: str) -> str:
    """Generate the Markdown body for a document/deck about `topic`."""
    sys = _DOC_SYS.get(kind, _DOC_SYS["report"])
    md = await llm.complete("chatbot", sys, f"Topic: {topic}", temperature=0.4)
    md = _clean_reply(md or "", had_search=False)
    md = re.sub(r"^```(?:markdown|md)?\s*|\s*```$", "", md.strip(), flags=re.M)
    if "#" not in md:                       # ensure at least one heading exists
        md = f"# {topic.title()[:80]}\n\n{md}"
    return md.strip()


async def deep_reason(question: str, history: str = "") -> str:
    """Deep reasoning via deepseek-r1 (its <think> trace is stripped by llm.py)."""
    sys = agents.SYSTEM_PROMPTS["reasoner"]
    prof = profile.get_context()
    if prof:
        sys = f"{sys}\n\n{prof}"
    ctx = f"Recent conversation:\n{history}\n\n" if history else ""
    ans = await llm.complete(
        "reasoner", sys,
        f"{ctx}Problem: {question}\n\nReason it through, then give a clear final answer.")
    return ans or "(no answer)"


_LEARN_BANNED = ("offline", "online", " away", "is back", "left ", "restart", "reboot",
                 "join", "remote", "stabiliz", "in an hour", "in about", "will be",
                 "logging", "logged", "presence", "status", "mood", "currently")


async def learn(history: str) -> list[str]:
    """Pull durable preferences/facts from what the USER said and store them.
    Only user lines are used (never the assistant's replies, which may be
    hallucinated) — this is what prevents the profile from being poisoned."""
    user_lines = [ln for ln in history.splitlines()
                  if ln.strip().lower().startswith("user:")]
    text = "\n".join(user_lines).strip()
    if not text:
        return []
    sys = ("From ONLY what the user said, extract DURABLE facts or preferences about "
           "the user worth remembering long-term (name, location, tools, interests, "
           "projects, how they like replies).\n"
           "STRICT RULES:\n"
           "- Use ONLY information the user EXPLICITLY stated about himself.\n"
           "- NEVER record his status, availability, presence, mood, or session events "
           "(offline/away/back/restarting/joining later, etc.).\n"
           "- NEVER infer or invent anything not directly stated.\n"
           "- If nothing durable was stated, output exactly: NONE\n"
           "Output 0-2 short factual bullet lines.")
    out = await llm.complete("chatbot", sys, f"User said:\n{text}\n\nDurable notes:")
    if out.strip().upper().startswith("NONE"):
        return []
    added = []
    for line in out.splitlines():
        line = line.strip("-*•\t ").strip()
        low = line.lower()
        if len(line) > 6 and not low.startswith("none") \
                and not any(b in low for b in _LEARN_BANNED) \
                and profile.remember(line):
            added.append(line)
    return added


async def twin_chat(message: str, history: str = "") -> str:
    """Talk to Abrar's LLM twin — it replies in the first person AS Abrar."""
    sys = agents.SYSTEM_PROMPTS["twin"]
    fact = knowledge.fact_for(message)
    ctx = fact + "\n\n" if fact else ""
    turns = _parse_history(history)
    out = await llm.complete("twin", sys, f"{ctx}{message}", history=turns)

    async def _regen(correction: str) -> str:
        r = await llm.complete("twin", f"{sys}\n\nIMPORTANT: {correction}",
                               f"{ctx}{message}", history=turns)
        return _clean_reply(r, had_search=False)

    return await enforcer.enforce(_clean_reply(out, had_search=False), _regen)


async def web_view(target: str) -> str:
    """The web-viewing agent: open a URL, or search a query then read the top hit,
    and summarise/answer with citations (llama3.2:1b)."""
    target = target.strip()
    is_url = target.startswith(("http://", "https://")) or (
        "." in target and " " not in target)

    if is_url:
        page = await websearch.fetch(target, 6000)
        if not page.get("ok"):
            return f"❌ Couldn't open <{target}>: {page.get('error')}"
        answer = await llm.complete(
            "web_agent", agents.SYSTEM_PROMPTS["web_agent"],
            f"Page: {page['url']}\nTitle: {page['title']}\n\n{page['text']}\n\n"
            "Summarise this page and pull out what matters.")
        return f"🌐 **{page['title'] or page['url']}**\n<{page['url']}>\n\n{answer}"

    results = await websearch.search(target, n=5)
    if not results:
        return f"🔎 No results for **{target}**."
    web_ctx = websearch.format_results(results) + "\n\n"
    page = await websearch.fetch(results[0]["url"], 4000)
    if page.get("ok") and page.get("text"):
        web_ctx += f"TOP PAGE ({page['title']}) {page['url']}:\n{page['text']}\n"
    answer = await llm.complete(
        "web_agent", agents.SYSTEM_PROMPTS["web_agent"],
        f"Question: {target}\n\n{web_ctx}\nAnswer using these results and cite the URLs.")
    return f"🔎 **{target}**\n\n{answer}"
