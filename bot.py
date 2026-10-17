"""Discord entrypoint.

Behaviour:
  • Only OWNER_ID can drive it. Everyone else is ignored.
  • `!task <desc>`  → runs the 7-agent pipeline in the BACKGROUND and streams
                      chatty progress. Casual messages during a job go to the
                      chatbot and do NOT interrupt the job.
  • `stop`/`!stop` → cancels the running job (the only thing that interrupts).
  • `!status`      → what's running.
  • `!email <txt>` → send yourself an email (local mail MCP: Proton via hydroxide + mailbridge).
  • `!inbox`, `!read`, `!mailsearch`, `!send`, `!mail` → read/search/send on any mail account.
  • anything else  → friendly chatbot (llama3.2:1b).

Also runs a tiny aiohttp server on 127.0.0.1:BOT_HTTP_PORT that Apache proxies
(`/bot/…`) so tasks can be triggered by HTTP too (e.g. POST /trigger).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import deque
from pathlib import Path

import discord
from aiohttp import web

import browser
import config
import docgen
import emailer
import mailbrain
import mailcompose
import mailstyle
import filewatch
import gui
import llm
import openmanus
import voice
import mathreport
import orchestrator
import pdftools
import userprofile as profile
import safety
import sysmon
import tokens
import tools
import vision
import vmtools

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(config.LOG_DIR / "bot.log"), logging.StreamHandler()],
)
log = logging.getLogger("laptop-agent")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# single-job model: one pipeline at a time is plenty for a laptop
_current_job: asyncio.Task | None = None
_job_desc: str = ""
_chat_history: deque[str] = deque(maxlen=8)
_last_channel: discord.abc.Messageable | None = None
_shell_cwd: str = str(config.SANDBOX_ROOT)   # persistent cwd for `$` commands
_since_learn: int = 0                        # counts chats until the next profile update

_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
_AUDIO_EXT = (".ogg", ".oga", ".opus", ".mp3", ".wav", ".m4a", ".webm", ".flac")

# Natural-language "do something" detection → route to the 7-agent pipeline
# instead of the chatbot. Needs a build/create verb near a deliverable noun.
_TASK_VERB = (r"(make|create|generate|build|write|code|develop|design|set ?up|"
              r"automate|convert|compile|scrape|extract|download|refactor|"
              r"back ?up|zip|archive|screenshot|plot|draw|do|research|find|"
              r"gather|organize|or ?ganize|summarize|prepare|put)")
_TASK_NOUN = (r"(file|pdf|script|program|app|application|website|web ?page|html|"
              r"code|folder|backup|transcript|document|docx?|report|csv|json|"
              r"spreadsheet|log|chart|graph|server|api|function|class|readme|"
              r"zip|archive|image|png|video|cron ?job|service|config|dashboard)")
_TASK_RE = re.compile(rf"\b{_TASK_VERB}\b.{{0,40}}?\b{_TASK_NOUN}\b", re.I | re.S)


def _looks_like_task(text: str) -> bool:
    t = text.strip()
    return len(t) >= 8 and bool(_TASK_RE.search(t))


# ── natural-language document requests ─────────────────────────────────────
# "make me a presentation about X" should just produce a .pptx — no `!` prefix,
# and NOT the slow 7-agent coding pipeline (which is the wrong tool for this).
_DOC_KINDS = [
    ("pptx",   r"\b(powerpoint|power point|pptx|ppt)\b"),
    ("slides", r"\b(slide ?deck|slides|deck|presentation|present)\b"),
    ("report", r"\b(report|document|write[- ]?up|essay|summary doc|paper|article)\b"),
    ("pdf",    r"\bpdf\b"),
]
_DOC_VERB = re.compile(
    r"\b(make|create|generate|build|write|draft|prepare|put together|give me|do|research|"
    r"organize|or ?ganize|can you (?:make|create|write|build|do|research)|i need|i want)\b", re.I)
# \b matters: without it, "on" matches inside "presentati(on)"
_DOC_TOPIC = re.compile(
    r"\b(?:about|on|for|covering|regarding|explaining|of)\b\s+(.{2,200})$",
    re.I | re.S)


def _looks_like_docgen(text: str) -> tuple[str, str] | None:
    """Return (kind, topic) if this is a 'make me a <document>' request."""
    t = text.strip()
    if len(t) < 10 or t.startswith(("!", "$")):
        return None
    if not _DOC_VERB.search(t):
        return None
    kind = next((k for k, pat in _DOC_KINDS if re.search(pat, t, re.I)), None)
    if not kind:
        return None
    m = _DOC_TOPIC.search(t)
    topic = (m.group(1) if m else t).strip(" .?!")
    return kind, topic


def _log_twin_sample(text: str) -> None:
    """Append Abrar's real message (with the prior bot line as context) as twin
    training data. This grows the dataset every time he chats."""
    try:
        ctx = ""
        for h in reversed(_chat_history):
            if h.startswith("Me:"):
                ctx = h[3:].strip()
                break
        with config.TWIN_DATA.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"context": ctx, "abrar": text}, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _progress_for(channel: discord.abc.Messageable):
    async def progress(msg: str) -> None:
        try:
            await channel.send(msg[:1900])
        except Exception as e:  # noqa: BLE001
            log.warning("progress send failed: %s", e)
    return progress


async def _run_job(task: str, channel: discord.abc.Messageable) -> None:
    global _current_job, _job_desc
    progress = _progress_for(channel)
    try:
        await channel.send(f"🚀 On it — starting a job: **{task}**\n"
                           f"I'll keep working while you're away. Say `stop` to cancel.")
        result = await orchestrator.run_pipeline(task, progress)

        report = result.get("report", "(no report)")
        await channel.send("🎉 **Done.** Here's the rundown:")
        for chunk in _chunks(report, 1900):
            await channel.send(chunk)

        if config.EMAIL_ON_COMPLETE:
            verdict = "✅ passed" if result.get("passed") else "⚠️ needs a look"
            r = await emailer.send_email(
                subject=f"[laptop-agent] {task[:60]} — {verdict}",
                body=report,
            )
            await channel.send("📧 Emailed you the report."
                               if r.get("ok") else f"📧 Email failed: {r.get('error')}")
    except asyncio.CancelledError:
        await channel.send("🛑 Job cancelled.")
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("job failed")
        await channel.send(f"💥 Job crashed: `{e}`")
    finally:
        _current_job = None
        _job_desc = ""


@client.event
async def on_ready() -> None:
    log.info("logged in as %s | LLM backend: %s", client.user, llm.backend_name())
    log.info("owner=%s  work_channel=%s", config.OWNER_ID, config.WORK_CHANNEL_ID)


_MAIL_NAMES: set[str] = set()
_DRAFTS: dict[int, dict] = {}   # channel id -> pending email draft awaiting yes/no


def _mail_account_names() -> set[str]:
    global _MAIL_NAMES
    if not _MAIL_NAMES:
        try:
            import json as _j
            from pathlib import Path as _P
            _MAIL_NAMES = {a["name"].lower() for a in _j.loads(
                (_P.home() / ".config/proton-mail-mcp/accounts.json").read_text())["accounts"]}
        except Exception:  # noqa: BLE001
            pass
    return _MAIL_NAMES


@client.event
async def on_message(message: discord.Message) -> None:
    global _current_job, _job_desc, _last_channel
    if message.author.bot:
        return
    # DIAGNOSTIC: log every human message that reaches the handler, so we can
    # tell "DM never arrived" (nothing logged) from "arrived but was filtered".
    log.info("MSG from %s (id=%s) in %s: %r",
             message.author, message.author.id,
             type(message.channel).__name__, (message.content or "")[:80])
    if config.DM_ONLY and message.guild is not None:
        return  # only act on DMs, never server channels
    if not safety.is_owner(message.author.id):
        return  # silently ignore everyone but you (no-op in open mode)

    content = message.content.strip()
    _last_channel = message.channel
    low = content.lower()

    # collect his natural messages (not commands/shell) as twin training data
    if content and not content.startswith(("!", "$")):
        _log_twin_sample(content)

    # ── EARS: a voice note / audio file becomes a spoken conversation ────────
    _audio = [a for a in message.attachments
              if (a.content_type or "").startswith("audio")
              or a.filename.lower().endswith(_AUDIO_EXT)] if message.attachments else []
    if _audio and not content.startswith(("!", "$")):
        att = _audio[0]
        try:
            data = await att.read()
            apath = config.UPLOADS_DIR / att.filename
            apath.write_bytes(data)
        except Exception as e:  # noqa: BLE001
            await message.channel.send(f"🎙️ couldn't read the audio: {e}")
            return
        await message.channel.send("🎙️ listening…")
        async with message.channel.typing():
            tr = await voice.transcribe(str(apath))
        if not tr.get("ok") or not tr.get("text"):
            await message.channel.send(f"🎙️ couldn't make out the audio. {tr.get('error') or ''}")
            return
        heard = tr["text"]
        await message.channel.send(f"🎙️ **heard:** *{heard}*")
        async with message.channel.typing():
            reply = await orchestrator.chat(heard, "\n".join(_chat_history))
        for chunk in _chunks(reply, 1900):
            await message.channel.send(chunk)
        # MOUTH: speak the reply back (voice in → voice out)
        say = await voice.speak(reply, "reply")
        if say.get("ok"):
            try:
                await message.channel.send(file=discord.File(say["path"]))
            except Exception:  # noqa: BLE001
                pass
        _chat_history.append(f"User: [voice] {heard}")
        _chat_history.append(f"Me: {reply}")
        return

    # ── attachments: screenshots (vision) + uploaded files ──────────────────
    if message.attachments:
        images, files = [], []
        for att in message.attachments:
            try:
                data = await att.read()
            except Exception as e:  # noqa: BLE001
                await message.channel.send(f"⚠️ couldn't read `{att.filename}`: {e}")
                continue
            try:
                (config.UPLOADS_DIR / att.filename).write_bytes(data)
            except Exception:  # noqa: BLE001
                pass
            ctype = att.content_type or ""
            if ctype.startswith("image") or att.filename.lower().endswith(_IMAGE_EXT):
                images.append((att.filename, data))
            else:
                files.append((att.filename, data))

        ctx_parts = []

        # files → save, then actually READ them (PDFs get text-layer or OCR)
        for fname, data in files:
            saved_path = config.UPLOADS_DIR / fname
            await message.channel.send(f"📎 Saved `{fname}` to `workspace/uploads/`.")

            if fname.lower().endswith(".pdf"):
                await message.channel.send("📄 Reading the PDF…")
                async with message.channel.typing():
                    r = await pdftools.read(saved_path, ocr_pages=5)
                if r.get("ok"):
                    note = r.get("note") or f"read via {r.get('method')}"
                    ctx_parts.append(f"[PDF {fname} — {note}]\n{r['text']}")
                    await message.channel.send(
                        f"✅ Got the text ({note}). Ask me anything about it.")
                else:
                    ctx_parts.append(f"[PDF {fname}] (unreadable: {r.get('error')})")
                    await message.channel.send(f"⚠️ Couldn't read it: {r.get('error')}")
                continue

            try:
                preview = data.decode("utf-8")[:2500]
            except Exception:  # noqa: BLE001
                preview = "(binary file — saved to workspace/uploads for tools)"
            ctx_parts.append(f"[File {fname}]\n{preview}")

        # images → run vision and ALWAYS post the real result (never the chatbot)
        if images:
            await message.channel.send(
                "🔍 Reading your image with the vision model… (runs on CPU, ~1–2 min)")
        for fname, data in images:
            async with message.channel.typing():
                desc = await vision.analyze(data)
            if not desc or desc.startswith("(couldn't"):
                await message.channel.send(
                    f"🖼️ `{fname}` — I couldn't read it. {desc or '(empty result)'}\n"
                    "It may have timed out on the first try; send it again and it'll be faster "
                    "(the model stays warm).")
                continue
            await message.channel.send(f"🖼️ **{fname}** — here's what I actually see:")
            for chunk in _chunks(desc, 1900):
                await message.channel.send(chunk)
            ctx_parts.append(f"[Screenshot {fname}]\n{desc}")

        upload_ctx = "\n\n".join(ctx_parts)

        # command sent with the attachment → fold context in and fall through
        if content.startswith(("!", "$")):
            content = f"{content}\n\n[Attached context]\n{upload_ctx}"
            low = content.lower()
        else:
            # only invoke the chatbot for a *specific* question, grounded in what we saw
            q = content.strip().lower()
            describe_only = (not q) or (len(q) < 45 and any(
                w in q for w in ("what is this", "what's this", "what is in", "what's in",
                                 "describe", "read this", "read the", "analyze", "analyse",
                                 "explain this", "what do you see", "see this", "screenshot")))
            if not describe_only and upload_ctx:
                async with message.channel.typing():
                    reply = await orchestrator.chat(content, "\n".join(_chat_history),
                                                    extra_context=upload_ctx)
                for chunk in _chunks(reply, 1900):
                    await message.channel.send(chunk)
                _chat_history.append(f"User: [attachment] {content}")
                _chat_history.append(f"Me: {reply}")
            return

    # ── interrupts (the only things that break into a running job) ──────────
    # ---- pending email draft: yes / no / edit ------------------------------------------
    draft = _DRAFTS.get(message.channel.id)
    if draft is not None:
        if low in {"yes", "y", "send", "send it", "confirm", "yes send"}:
            _DRAFTS.pop(message.channel.id, None)
            if not draft["to"]:
                await message.channel.send("❌ No recipient — tell me who it's for (e.g. `!email to user@example.com ...`)."); return
            r = await emailer.send_email(subject=draft["subject"], body=draft["body"], to=draft["to"], account=draft["account"])
            if r.get("ok"):
                try:
                    mailcompose.remember_sent(draft)   # learn the recipient for next time
                except Exception:  # noqa: BLE001
                    pass
            await message.channel.send(f"📧 {r['data']}" if r.get("ok") else f"📧 Failed: {r.get('error')}")
            return
        if low in {"no", "n", "cancel", "discard", "nah", "drop it"}:
            _DRAFTS.pop(message.channel.id, None)
            await message.channel.send("🗑️ Draft discarded."); return
        if low in {"stop", "!stop", "abort"}:
            _DRAFTS.pop(message.channel.id, None)   # fall through so a running job is also stopped
        if not low.startswith("!") and low not in {"stop", "!stop", "abort"} and len(content) < 600:
            # treat as an edit instruction: recompose with the change applied
            new = await mailcompose.compose(
                f"from {draft['account']} to {draft['to'] or ''} subject {draft['subject']} "
                f"saying: {draft['body']}\n\nChange requested: {content}", emailer.DEFAULT_ACCOUNT)
            new["to"] = new["to"] or draft["to"]
            _DRAFTS[message.channel.id] = new
            await message.channel.send(mailcompose.preview(new)); return

    if low in {"stop", "!stop", "cancel", "abort"}:
        if _current_job and not _current_job.done():
            _current_job.cancel()
            await message.channel.send("🛑 Cancelling the current job…")
        else:
            await message.channel.send("Nothing's running right now.")
        return

    # ── `$` = run a shell command on the laptop yourself (persistent cwd) ────
    if content.startswith("$"):
        global _shell_cwd
        cmd = content[1:].strip()
        if not cmd:
            await message.channel.send(f"📂 cwd: `{_shell_cwd}`")
            return
        if cmd == "cd" or cmd.startswith("cd "):
            target = cmd[2:].strip() or str(config.SANDBOX_ROOT)
            target = os.path.expanduser(target)
            newp = Path(target) if os.path.isabs(target) else Path(_shell_cwd) / target
            try:
                resolved = safety.resolve_in_sandbox(str(newp))
                if resolved.is_dir():
                    _shell_cwd = str(resolved)
                    await message.channel.send(f"📂 `{_shell_cwd}`")
                else:
                    await message.channel.send(f"not a directory: `{resolved}`")
            except ValueError as e:
                await message.channel.send(f"⛔ {e}")
            return
        async with message.channel.typing():
            r = await tools.run_shell(cmd, cwd=_shell_cwd, source="user")
        out = r["stdout"] or r["stderr"] or "(no output)"
        await message.channel.send(f"`$ {cmd}` (rc={r['returncode']})\n```\n{out[:1850]}\n```")
        return

    if low in {"!status", "status?"}:
        if _current_job and not _current_job.done():
            await message.channel.send(f"🟢 Working on: **{_job_desc}**")
        else:
            await message.channel.send("💤 Idle — give me a `!task ...`.")
        return

    if low in {"!ping", "!help"}:
        await message.channel.send(
            "**7-agent job:** `!coder <task>` (or `!task`) — plan→review→code→test→debug→report\n"
            "**Deep reasoning:** `!think <hard problem>` (deepseek-r1)\n"
            "**Your twin:** `!twin <message>` — a model that talks as you\n"
            "**Screenshots/files:** just paste an image or upload a file — I'll read it\n"
            "**Voice:** send a voice note — I'll transcribe it & talk back; `!say <text>` to hear me\n"
            "**Run shell yourself:** `$ <command>` on the laptop (e.g. `$ls -la`, `$cd Workspace`)\n"
            "**Web:** `!search <query>`, `!web <url or query>`  (or just ask me to search)\n"
            "**Memory:** `!remember <fact>`, `!profile`, `!forget` — I adapt to you over time\n"
            "**Laptop:** `!stats`, `!ls <path>`, `!cat <path>`\n"
            "**Computer use:** `!gui click X Y|type <text>|launch <cmd>`, `!screen` (see the invisible screen)\n"
            "**Autonomous agent:** `!manus <task>` — local OpenManus, no API key (heavy; needs RAM free)\n"
            "**VM:** `!vm <cmd>` (signed-in sandbox)\n"
            "**Mail:** `!email <who + what, plain English>` (I draft it properly, you confirm), "
            "`!inbox [account] [n]`, `!unread [account]`, `!read <uid> [account]`, "
            "`!mailsearch <query> [account]`, `!send <account> <to> | <subject> | <body>`, `!mail` (accounts), "
            "`!contacts` (who I remember), `!mailtrain` (learn your style)\n"
            "**Job control:** `!status`, `stop`  •  **Other:** `!email <who/what in plain English>` → drafts, you confirm; `!token`\n"
            f"LLM backend: `{llm.backend_name()}`.")
        return

    if low in {"!stats", "!monitor", "!sys"}:
        await message.channel.send(sysmon.format_report(sysmon.snapshot()))
        return

    if low.startswith("!ls"):
        r = tools.list_dir(content[3:].strip() or ".")
        body = "\n".join(r.get("entries", [r.get("error", "?")]))
        await message.channel.send(f"```\n{body[:1800]}\n```")
        return

    if low.startswith("!cat"):
        r = tools.read_file(content[4:].strip())
        body = r.get("content", r.get("error", "?"))
        await message.channel.send(f"```\n{body[:1800]}\n```")
        return

    if low.startswith("!twin"):
        msg = content[len("!twin"):].strip()
        if not msg:
            await message.channel.send("Talk to your twin: `!twin <message>` — it replies as you.")
            return
        async with message.channel.typing():
            reply = await orchestrator.twin_chat(msg, "\n".join(_chat_history))
        for chunk in _chunks(f"👤 **(you, as your twin)** {reply}", 1900):
            await message.channel.send(chunk)
        return

    # ── document / slide generation ──────────────────────────────────────
    if low.startswith(("!slides", "!deck", "!pptx", "!report", "!doc")):
        kind = low.split()[0].lstrip("!")
        body = content.split(None, 1)[1].strip() if len(content.split(None, 1)) > 1 else ""
        if not body:
            await message.channel.send(
                "Give me markdown to build from, e.g.\n"
                "`!slides # Title\n- point one\n- point two`\n"
                "Kinds: `!slides` (pdf) · `!pptx` (PowerPoint) · `!report` (document)")
            return
        await message.channel.send(f"🛠️ Building your {kind}…")
        async with message.channel.typing():
            if kind == "pptx":
                r = docgen.pptx_from_markdown(body, name="deck")
            elif kind in ("report", "doc"):
                r = await docgen.quarto_render(body, name="report")
            else:
                r = await docgen.marp_slides(body, name="slides", fmt="pdf")
        if r.get("ok"):
            note = f" ({r.get('tool')})" + (f" — {r['note']}" if r.get("note") else "")
            await message.channel.send(f"✅ Done{note}")
            try:
                await message.channel.send(file=discord.File(r["path"]))
            except Exception as e:  # noqa: BLE001
                await message.channel.send(f"saved to `{r['path']}` (upload failed: {e})")
        else:
            await message.channel.send(f"⚠️ Couldn't build it: {r.get('error')}")
        return

    # ── real browser ─────────────────────────────────────────────────────
    if low.startswith(("!browse", "!shot", "!render")):
        parts = content.split(None, 1)
        url = parts[1].strip() if len(parts) > 1 else ""
        if not url:
            await message.channel.send("Usage: `!browse <url>` or `!shot <url>`")
            return
        shot = low.startswith("!shot")
        await message.channel.send(f"🌐 {'Screenshotting' if shot else 'Opening'} `{url}`…")
        async with message.channel.typing():
            r = await (browser.screenshot(url, name="page") if shot
                       else browser.read(url))
        if not r.get("ok"):
            await message.channel.send(f"⚠️ {r.get('error')}")
            return
        if shot:
            await message.channel.send(file=discord.File(r["path"]))
        else:
            head = f"🌐 **{r.get('title') or url}**\n"
            for chunk in _chunks(head + r["text"], 1900):
                await message.channel.send(chunk)
        return

    # ── file watcher -> n8n ──────────────────────────────────────────────
    if low.startswith("!watch"):
        arg = content[len("!watch"):].strip().lower()
        if arg.startswith("stop"):
            filewatch.stop()
            await message.channel.send("🛑 File watcher stopped.")
        elif arg.startswith("recent") or arg.startswith("events"):
            evs = filewatch.recent(12)
            body = "\n".join(f"  `{e['event']}` {e['name']}" for e in evs) or "  (none yet)"
            await message.channel.send(f"👁️ **Recent file events**\n{body}")
        elif arg.startswith("start") or arg == "":
            r = await filewatch.start_async()
            if r.get("ok"):
                w = "\n".join(f"  • `{p}`" for p in r.get("watching", []))
                await message.channel.send(
                    f"👁️ Watching for file changes → n8n\n{w}\n"
                    f"webhook: `{r.get('webhook')}`")
            else:
                await message.channel.send(f"⚠️ {r.get('error')}")
        else:
            st = filewatch.status()
            await message.channel.send(f"👁️ watcher: `{st}`")
        return

    if low.startswith("!math"):
        rest = content[len("!math"):].strip()
        if rest in ("", "scores", "summary"):
            await message.channel.send(mathreport.summary())
            return
        wk = None
        model = None
        only_wrong = "wrong" in rest.lower()
        for tok in rest.split():
            if tok.lower().startswith("week"):
                digits = "".join(c for c in tok if c.isdigit())
                if digits:
                    wk = int(digits)
            elif tok.isdigit():
                wk = int(tok)
            elif tok.lower() not in ("wrong", "failed"):
                model = tok
        for chunk in mathreport.report(model=model, week=wk, only_wrong=only_wrong):
            await message.channel.send(chunk[:1900])
        return

    if low.startswith("!think") or low.startswith("!reason"):
        q = content.split(None, 1)
        q = q[1].strip() if len(q) > 1 else ""
        if not q:
            await message.channel.send("Usage: `!think <a hard question or problem>`")
            return
        async with message.channel.typing():
            ans = await orchestrator.deep_reason(q, "\n".join(_chat_history))
        for chunk in _chunks(f"🧩 {ans}", 1900):
            await message.channel.send(chunk)
        return

    if low.startswith("!remember") or low.startswith("!profile remember"):
        body = content.split("remember", 1)[1] if "remember" in low else ""
        body = re.sub(r"^\s*(these things|this)\s*[:\-]?\s*", "", body, flags=re.I)
        facts = []
        for ln in body.splitlines():
            raw = ln.strip()
            if not raw or raw.startswith("#"):
                continue
            if re.match(r"(?i)^(evidence|date|imported from)\b", raw):
                continue
            clean = re.sub(r"^[\s\-\*•\d\.\)]+", "", raw).strip()
            if len(clean) > 4:
                facts.append(clean)
        if not facts and body.strip():
            facts = [body.strip()]
        added = [f for f in facts if profile.remember(f)]
        await message.channel.send(
            f"🧠 Remembered {len(added)} new thing(s) about you." if added
            else "Nothing new to remember (already knew it, or nothing given).")
        return

    if low in {"!profile", "!memory", "!aboutme"}:
        notes = profile.all_notes()
        body = "\n".join(f"• {n}" for n in notes) or "(I haven't learned anything yet)"
        await message.channel.send(f"🧠 **What I've picked up about you:**\n{body[:1800]}")
        return

    if low in {"!forget", "!forgetme"}:
        profile.clear()
        await message.channel.send("🧹 Wiped what I'd learned about you.")
        return

    if low.startswith("!search") or low.startswith("!web"):
        q = content.split(None, 1)
        target = q[1].strip() if len(q) > 1 else ""
        if not target:
            await message.channel.send("Usage: `!search <query>` or `!web <url|query>`")
            return
        async with message.channel.typing():
            out = await orchestrator.web_view(target)
        for chunk in _chunks(out, 1900):
            await message.channel.send(chunk)
        return

    if low in {"!screen", "!gui screen", "!see"}:
        async with message.channel.typing():
            shot = await gui.screenshot("screen")
            if not shot.get("ok"):
                await message.channel.send(f"🖥️ couldn't grab the virtual screen: {shot.get('error')}")
                return
            try:
                desc = await vision.analyze(open(shot["path"], "rb").read())
            except Exception as e:  # noqa: BLE001
                desc = f"(vision failed: {e})"
        for ch in _chunks(f"🖥️ **virtual screen:**\n{desc}", 1900):
            await message.channel.send(ch)
        return

    if low.startswith("!gui"):
        parts = content.split(None, 2)          # !gui <action> <rest>
        act = parts[1].lower() if len(parts) > 1 else ""
        rest = parts[2] if len(parts) > 2 else ""
        try:
            if act in ("click", "move") and len(rest.split()) >= 2:
                x, y = int(rest.split()[0]), int(rest.split()[1])
                r = await (gui.click(x, y) if act == "click" else gui.move(x, y))
            elif act == "type":
                r = await gui.type_text(rest)
            elif act == "key":
                r = await gui.press(rest.strip())
            elif act == "scroll":
                r = await gui.scroll(int(rest or "-3"))
            elif act == "launch":
                r = await gui.launch(rest)
            elif act in ("screen", "shot", "see"):
                r = await gui.screenshot("screen")
            else:
                r = {"help": "!gui click X Y | move X Y | type <text> | key <Key> | "
                             "scroll N | launch <cmd>  •  !screen = see it via vision"}
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "error": str(e)}
        await message.channel.send(f"🖱️ {r}")
        return

    if low.startswith("!say"):
        text = content[4:].strip()
        if not text:
            await message.channel.send("🔊 **!say <text>** — I'll speak it (Google TTS, no API key).")
            return
        async with message.channel.typing():
            r = await voice.speak(text, "say")
        if r.get("ok"):
            await message.channel.send(file=discord.File(r["path"]))
        else:
            await message.channel.send(f"🔊 couldn't speak: {r.get('error')}")
        return

    if low.startswith("!manus"):
        task = content[6:].strip()
        if not task:
            st = openmanus.available()
            await message.channel.send(
                "🦿 **!manus <task>** — full autonomous agent (OpenManus) on 100% "
                "local models, no API key. Browses/clicks/reads on an invisible "
                f"screen. Heavy: needs ~2.6GB free RAM.\nstatus: `{st}`")
            return
        await message.channel.send("🦿 OpenManus is working… (local models are slow; up to ~5 min)")
        async with message.channel.typing():
            r = await openmanus.run(task, timeout=300)
        if not r.get("ok"):
            await message.channel.send(f"🦿 couldn't finish: {r.get('error')}")
            return
        for ch in _chunks(f"🦿 **done:**\n{r.get('output') or '(no output)'}", 1900):
            await message.channel.send(ch)
        return

    if low.startswith("!vm"):
        cmd = content[3:].strip()
        sub = cmd.split()[0].lower() if cmd else ""

        if not cmd or sub == "status":
            st = await vmtools.status()
            if not st.get("built"):
                await message.channel.send(
                    "🖥️ VM not built yet.\nBuild it: `bash vm/setup_vm.sh`")
            elif not st.get("running"):
                await message.channel.send(
                    "🖥️ VM is **stopped**.\nStart: `!vm start` "
                    "(or `bash vm/vmctl.sh start --free-ram`)")
            else:
                cs = "\n".join(f"  • {c}" for c in st.get("containers", [])) or "  (none)"
                await message.channel.send(
                    f"🖥️ VM **running** — containers inside it:\n{cs}")
            return

        if sub == "start":
            await message.channel.send("🚀 Booting the VM (frees RAM first; "
                                       "first boot installs docker, ~2-4 min)…")
            r = await vmtools.start(free_ram=True)
            out = (r.get("stdout") or "") + (r.get("stderr") or "") or r.get("error", "")
            await message.channel.send(f"```\n{out[-1500:] or 'done'}\n```")
            return

        if sub == "stop":
            r = await vmtools.stop()
            await message.channel.send("🛑 " + ((r.get("stdout") or "").strip()
                                                or r.get("error", "stopped")))
            return

        if sub == "docker":
            r = await vmtools.run_in_vm_host(cmd)          # docker admin in the VM
        else:
            r = await vmtools.run_in_vm(cmd)               # inside the container
        out = r.get("stdout") or r.get("stderr") or r.get("error") or "(no output)"
        await message.channel.send(f"```\n{out[:1800]}\n```")
        return

    if low.startswith("!email") or low.startswith("!compose"):
        req = content.split(None, 1)[1].strip() if len(content.split(None, 1)) > 1 else ""
        if not req:
            await message.channel.send("usage: `!email from <account> to <addr> subject <s> saying <what to say>` "
                                       "(only the 'to' part is required)"); return
        async with message.channel.typing():
            draft = await mailcompose.compose(req, emailer.DEFAULT_ACCOUNT)
        if not draft["to"]:
            draft["to"] = config.EMAIL_TO
            draft["warnings"].append(f"no recipient given — addressed to yourself ({config.EMAIL_TO})")
        _DRAFTS[message.channel.id] = draft
        await message.channel.send(mailcompose.preview(draft))
        return

    if low in {"!mailtrain", "!mailstyle"}:
        if low == "!mailtrain":
            async with message.channel.typing():
                prof = await asyncio.to_thread(mailstyle.build)
                learned = await asyncio.to_thread(mailbrain.learn_from_corpus)
            prof["recipients_learned"] = learned
        else:
            try:
                prof = json.loads((mailstyle.OUT / "profile.json").read_text())
            except Exception:
                await message.channel.send("No style profile yet — run `!mailtrain`."); return
        await message.channel.send(
            f"🧠 **Mail style profile** — {prof.get('email_count',0)} emails, {prof.get('voice_count',0)} voice samples"
            + (f", {prof['recipients_learned']} recipients" if prof.get('recipients_learned') else "")
            + f" (sources: {dict(prof.get('sources', {}))})\n{mailstyle.style_summary(prof) or '_nothing learned yet_'}\n"
            "_Drop ChatGPT `conversations.json` / Gemini `MyActivity.json` / `.mbox` files into "
            "`laptop-agent/workspace/mail_style/sources/` and run `!mailtrain` again._")
        return

    if low.startswith("!contact"):
        # !contacts                          -> list remembered recipients
        # !contact <email> friendly|professional|formal [Name]  -> set how to write to them
        parts = content.split()
        recips = mailbrain._load_recipients()
        if len(parts) <= 1:
            if not recips:
                await message.channel.send("No remembered contacts yet. Send some mail or run `!mailtrain`."); return
            reg = {3: "formal", 2: "professional", 1: "friendly"}
            rows = sorted(recips.items(), key=lambda kv: -kv[1].get("count", 0))[:25]
            lines = ["**Remembered contacts:**"] + [
                f"• {v.get('name') or '?'} <{k}> — {reg.get(v.get('formality'), 'auto')}, {v.get('count',0)} mail(s)"
                for k, v in rows]
            for chunk in _chunks("\n".join(lines), 1900):
                await message.channel.send(chunk)
            return
        email_arg = next((p for p in parts[1:] if "@" in p), None)
        level = next(({"formal": 3, "professional": 2, "friendly": 1}[p.lower()]
                      for p in parts[1:] if p.lower() in ("formal", "professional", "friendly")), None)
        name = next((p for p in parts[1:] if "@" not in p and p.lower() not in
                     ("formal", "professional", "friendly")), "")
        if not email_arg:
            await message.channel.send("usage: `!contact <email> friendly|professional|formal [Name]`"); return
        mailbrain.remember(email_arg, name=name, formality=level)
        await message.channel.send(f"✅ Noted {email_arg}"
                                   + (f" as {name}" if name else "")
                                   + (f", {['','friendly','professional','formal'][level]} tone" if level else ""))
        return

    if low in {"!mail", "!accounts"}:
        r = await emailer.accounts()
        if not r.get("ok"):
            await message.channel.send(f"📧 Mail MCP unavailable: {r.get('error')}"); return
        await message.channel.send("**Mail accounts:**\n" + "\n".join(
            f"• `{a['name']}` — {a['email']} ({a.get('provider')})" for a in r["data"]))
        return

    if low.startswith("!inbox") or low.startswith("!unread"):
        parts = content.split()
        unread = low.startswith("!unread")
        acct = next((x for x in parts[1:] if not x.isdigit()), None)
        n = next((int(x) for x in parts[1:] if x.isdigit()), 10)
        r = await emailer.list_messages(acct, limit=min(n, 25), unread_only=unread)
        if not r.get("ok"):
            await message.channel.send(f"📧 Failed: {r.get('error')}"); return
        for chunk in _chunks(emailer.fmt_list(r["data"], acct or emailer.DEFAULT_ACCOUNT), 1900):
            await message.channel.send(chunk)
        return

    if low.startswith("!read "):
        parts = content.split()
        if len(parts) < 2:
            await message.channel.send("usage: `!read <uid> [account]`"); return
        r = await emailer.read_message(parts[1], parts[2] if len(parts) > 2 else None)
        if not r.get("ok"):
            await message.channel.send(f"📧 Failed: {r.get('error')}"); return
        for chunk in _chunks(emailer.fmt_message(r["data"]), 1900):
            await message.channel.send(chunk)
        return

    if low.startswith("!mailsearch"):
        rest = content[len("!mailsearch"):].strip()
        if not rest:
            await message.channel.send("usage: `!mailsearch <query> [account]`"); return
        words = rest.split()
        acct = None
        if len(words) > 1 and "@" not in words[-1] and words[-1].lower() in _mail_account_names():
            acct, words = words[-1], words[:-1]
        r = await emailer.search(" ".join(words), acct)
        if not r.get("ok"):
            await message.channel.send(f"📧 Failed: {r.get('error')}"); return
        for chunk in _chunks(emailer.fmt_list(r["data"], acct or emailer.DEFAULT_ACCOUNT), 1900):
            await message.channel.send(chunk)
        return

    if low.startswith("!send "):
        # !send <account> <to> | <subject> | <body>
        try:
            head, subject, body = [x.strip() for x in content[len("!send"):].split("|", 2)]
            acct, to = head.split(None, 1)
        except ValueError:
            await message.channel.send("usage: `!send <account> <to> | <subject> | <body>`"); return
        r = await emailer.send_email(subject=subject, body=body, to=to, account=acct)
        await message.channel.send(f"📧 {r['data']}" if r.get("ok") else f"📧 Failed: {r.get('error')}")
        return

    if low.startswith("!token"):
        r = await tokens.generate_token()
        await message.channel.send(f"🔑 `{r.get('token')}`" if r.get("ok")
                                   else f"Token provisioner failed: {r.get('error')}")
        return

    # ── start a job ─────────────────────────────────────────────────────────
    task = None
    if low.startswith("!coder"):
        task = content[len("!coder"):].strip()
    elif low.startswith("!task"):
        task = content[len("!task"):].strip()
    elif config.WORK_CHANNEL_ID and message.channel.id == config.WORK_CHANNEL_ID \
            and not low.startswith("!"):
        task = content  # plain message in the dedicated work channel = a task
    # natural language: "make me a presentation about X" -> build the file
    doc = None if content.startswith(("!", "$")) else _looks_like_docgen(content)
    if doc:
        kind, topic = doc
        await message.channel.send(
            f"🛠️ Writing a {kind} about **{topic[:80]}**…")
        async with message.channel.typing():
            md = await orchestrator.write_document(kind, topic)
            if kind == "pptx":
                r = docgen.pptx_from_markdown(md, name="deck")
            elif kind in ("report", "pdf"):
                r = await docgen.quarto_render(md, name="report", to="html")
            else:
                r = await docgen.marp_slides(md, name="slides", fmt="pdf")
        if r.get("ok"):
            await message.channel.send(f"✅ Here you go ({r.get('tool')})")
            try:
                await message.channel.send(file=discord.File(r["path"]))
            except Exception as e:  # noqa: BLE001
                await message.channel.send(f"saved to `{r['path']}` (upload failed: {e})")
        else:
            await message.channel.send(f"⚠️ Couldn't build it: {r.get('error')}")
        return

    elif not content.startswith(("!", "$")) and _looks_like_task(content):
        task = content  # natural-language "make/build/write a <thing>" → pipeline

    if task:
        if _current_job and not _current_job.done():
            await message.channel.send(
                "I'm already on a job. Say `stop` first, or let me finish. "
                "(You can still chat with me — it won't interrupt the work.)")
            return
        _job_desc = task
        _current_job = asyncio.create_task(_run_job(task, message.channel))
        return

    # ── otherwise: just chat (never interrupts a running job) ───────────────
    global _since_learn
    async with message.channel.typing():
        reply = await orchestrator.chat(content, "\n".join(_chat_history))
    _chat_history.append(f"User: {content}")
    _chat_history.append(f"Me: {reply}")
    for chunk in _chunks(reply, 1900):
        await message.channel.send(chunk)

    # adapt to the user's behaviour every few exchanges (off by default — the 1B
    # extractor is too noisy; use !remember for explicit facts). AUTO_LEARN=true to enable.
    if config.AUTO_LEARN:
        _since_learn += 1
        if _since_learn >= 4:
            _since_learn = 0
            asyncio.create_task(orchestrator.learn("\n".join(_chat_history)))


# ── HTTP surface behind Apache (/bot/…) ────────────────────────────────────
async def _http_health(_req: web.Request) -> web.Response:
    running = bool(_current_job and not _current_job.done())
    return web.json_response({"ok": True, "busy": running, "job": _job_desc,
                              "backend": llm.backend_name()})


async def _http_trigger(req: web.Request) -> web.Response:
    """POST /trigger {"task": "..."} — start a job from an HTTP call."""
    global _current_job, _job_desc
    data = await req.json()
    task = (data.get("task") or "").strip()
    if not task:
        return web.json_response({"ok": False, "error": "no task"}, status=400)
    if _current_job and not _current_job.done():
        return web.json_response({"ok": False, "error": "busy"}, status=409)
    channel = _last_channel or (client.get_channel(config.WORK_CHANNEL_ID)
                                if config.WORK_CHANNEL_ID else None)
    if channel is None:
        return web.json_response({"ok": False, "error": "no channel known yet — "
                                  "message the bot once on Discord first"}, status=503)
    _job_desc = task
    _current_job = asyncio.create_task(_run_job(task, channel))
    return web.json_response({"ok": True, "started": task})


async def _start_http() -> None:
    app = web.Application()
    app.router.add_get("/health", _http_health)
    app.router.add_get("/", _http_health)
    app.router.add_post("/trigger", _http_trigger)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.BOT_HTTP_HOST, config.BOT_HTTP_PORT)
    await site.start()
    log.info("HTTP surface on http://%s:%s (proxied by Apache at /bot/)",
             config.BOT_HTTP_HOST, config.BOT_HTTP_PORT)


_alerted: set[str] = set()


async def _monitor_loop() -> None:
    """Periodically check host health; DM new threshold breaches once each."""
    await client.wait_until_ready()
    if not sysmon.available():
        log.warning("psutil unavailable — monitoring disabled (pip install psutil)")
        return
    while not client.is_closed():
        try:
            if config.MONITOR_ENABLED and config.ALERTS_ENABLED:
                s = sysmon.snapshot()
                alerts = sysmon.check_thresholds(s, config.ALERT_CPU, config.ALERT_MEM,
                                                 config.ALERT_DISK)
                keys = {a.split(" at ")[0] for a in alerts}
                fresh = [a for a in alerts if a.split(" at ")[0] not in _alerted]
                ch = _last_channel or (client.get_channel(config.WORK_CHANNEL_ID)
                                       if config.WORK_CHANNEL_ID else None)
                if fresh and ch:
                    await ch.send("⚠️ **Heads up on the laptop:**\n" + "\n".join(fresh))
                _alerted.clear()
                _alerted.update(keys)
        except Exception as e:  # noqa: BLE001
            log.warning("monitor loop error: %s", e)
        await asyncio.sleep(config.MONITOR_INTERVAL)


async def main() -> None:
    if not config.DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is empty — paste your bot token into .env")
    if config.OWNER_ID == 0:
        log.warning("OWNER_ID unset → OPEN MODE: anyone who can message the bot "
                    "can run shell commands. Keep this bot in a private server.")
    async with client:
        await _start_http()
        if config.MONITOR_ENABLED:
            asyncio.create_task(_monitor_loop())
        await client.start(config.DISCORD_TOKEN)


def _chunks(text: str, n: int):
    for i in range(0, len(text), n):
        yield text[i:i + n]


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
