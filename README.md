# laptop-agent

laptop-agent is a Discord bot by Syed Abrar Ali that turns a Linux laptop into a remote worker. You message it on Discord and it runs shell commands, reads and edits files, searches the web, reads screenshots and PDFs, drafts e-mail and produces documents, all driven by small local language models served by Ollama (1B to 3B parameters, so the whole thing fits in a few gigabytes of RAM). Long jobs go through a multi-agent pipeline (planner, reviewer, resource adder, coder, tester, debugger, report writer); casual messages go to a chatbot that never interrupts a running job. The problem it solves: getting real work done on a machine you are away from, with nothing but a phone and Discord, without paying for a hosted model API.

## Where it is used

In Abrar's setup the bot runs as a user-level systemd service on his everyday laptop. He drives it from his phone over Discord to kick off coding tasks, run shell commands with a persistent working directory, look things up, have voice notes transcribed, build slide decks or reports, and read and send mail from several accounts through a local mail MCP server. A background monitor watches CPU, memory and disk and sends a Discord heads-up when a threshold is crossed. An Apache reverse proxy exposes a tiny HTTP surface so other automations can trigger jobs, and n8n workflows handle webhook-driven side tasks (file events, token provisioning).

## How it works

### Message routing (`bot.py`)

Every incoming Discord message is checked against the owner id, then routed in this order:

1. **Attachments** are saved to `workspace/uploads/`; PDFs go through `pdftools` (pdftotext, OCR fallback), images through the vision model, audio through faster-whisper. The result is posted and folded into the chat context.
2. **Pending e-mail draft**: `yes` sends, `no` discards, anything else is treated as an edit instruction and the draft is recomposed.
3. `stop` cancels the current job (the only thing that interrupts a job).
4. `$ <cmd>` runs a shell command as the owner with a persistent cwd (`$cd dir`).
5. `!`-prefixed commands (see Usage).
6. Natural-language requests: "make me a presentation about X" is detected by regex and sent to document generation; "build/write/create a <file|script|app...>" is detected and sent to the pipeline; anything else goes to the chatbot.

Only one pipeline job runs at a time (`asyncio.Task`); progress is streamed to the channel as the agents act.

### The agent pipeline (`orchestrator.py`, `agents.py`)

`run_pipeline()` chains the roles: **planner** writes a numbered plan; **plan_reviewer** replies `APPROVED` or `REVISED` with a corrected plan; **resource_adder**, **coder**, **tester** and (only if the tester says `FAIL`) **debugger** run as autonomous loops; **report_writer** summarises everything. An autonomous loop (`run_agent_loop`) asks the model for a reply, parses action blocks with regexes, executes them and feeds the observations back until the model emits `<done>` or `MAX_AGENT_ITERATIONS` is reached.

Action grammar: `<shell>`, `<vm>`, `<search>`, `<open>`, `<read>`, `<ls>`, `<write path="">`, `<append path="">`, `<edit path="" find="">`, `<mkdir>`, `<move from="" to=""/>`, `<delete>`, `<done>`.

### Chatbot grounding

`orchestrator.chat()` decides whether a message needs the live web using keyword triggers, a WH-question regex, a "checkable claim" detector and casual/conversational exclusion lists. If so it searches DuckDuckGo (`websearch.py`, standard library only) and answers via the web-viewing role with citations. Replies then pass through:

- `enforcer.py`: every reply is checked against regex rules in `rules.json` (refusals, assistant disclaimers, corporate tone, sycophancy, fabricated experiences, empty output). Severity `block` triggers a regeneration with a corrective instruction and then a canned fallback; `warn` strips the match. `rules.json` is hot-reloaded on mtime change and violations are logged to `logs/violations.jsonl`.
- `grounding.py`: collapses repetition loops and appends a hedge when a reply states a specific fact (year, score, percentage) without having searched.
- `knowledge.py` / `userprofile.py`: inject facts from a personal metadata file and remembered profile notes (`user_profile.json`).

### Model routing (`llm.py`, `config.py`, `models/`)

Roles map to custom Ollama models built from the Modelfiles in `models/` (coder/chat on llama3.2:1b, a reasoning model on deepseek-r1:1.5b, a vision model on granite3.2-vision:2b, a persona "twin" model). Coder-family roles get a larger context and low temperature; chat roles get a short output cap. `llm.py` uses `langchain_ollama.ChatOllama` when importable and otherwise falls back to Ollama's raw HTTP API. Setting `LLM_BACKEND=openrouter` routes every role to an OpenRouter chat-completions endpoint instead (free-tier models by default).

### Safety (`safety.py`, `tools.py`)

- Owner gate: only `OWNER_ID` may drive the bot; unset means open mode (warned at start-up).
- Path sandbox: all file tools resolve paths under `SANDBOX_ROOT` and refuse system prefixes (`/etc`, `/usr`, `/bin`, `/boot`, `/dev`, `/proc`, `/sys`, `/var/lib`, ...).
- Command blocklist: catastrophic patterns (`rm -rf /`, `mkfs`, `dd of=/dev/...`, fork bombs) are blocked for everyone; power commands, `curl | sh`, force-push and writes to `/etc` are additionally blocked for agents but allowed when the owner types `$ ...`.
- Shell timeout (`SHELL_TIMEOUT`) and iteration cap (`MAX_AGENT_ITERATIONS`); `ALLOW_SHELL=false` makes the bot read-only.

### Components

| File | Role |
|---|---|
| `bot.py` | Discord client, command routing, background job, aiohttp HTTP surface (`/health`, `/trigger`), system-alert loop |
| `orchestrator.py` | Action parser, autonomous agent loop, pipeline, grounded chatbot, maths, document writing, deep reasoning |
| `agents.py` | System prompts and action grammar for every role |
| `llm.py` | Role to model routing; LangChain/Ollama with raw-HTTP and OpenRouter fallbacks |
| `config.py` | All settings from `.env` |
| `tools.py` / `safety.py` | Shell and file tools; sandbox, blocklist, owner check |
| `enforcer.py` + `rules.json` | Regex rule engine that gates every reply |
| `grounding.py`, `knowledge.py`, `userprofile.py` | Hedging, personal fact injection, adaptive profile memory |
| `websearch.py`, `browser.py` | DuckDuckGo search and page text; Playwright Chromium for JS pages and screenshots |
| `vision.py`, `voice.py`, `pdftools.py` | Image understanding, speech-to-text (faster-whisper) and text-to-speech (gTTS), PDF text/OCR |
| `docgen.py`, `factcheck.py` | Marp / python-pptx / Quarto / LibreOffice outputs into `workspace/outputs/`; claim checking against search results |
| `mathsolver.py`, `mathreport.py` | Program-aided maths: the model writes Python, run in an isolated subprocess |
| `emailer.py`, `mailcompose.py`, `mailbrain.py`, `mailstyle.py` | Mail through a local MCP server; natural-language drafting with purpose/formality rules; writing-style corpus builder |
| `gui.py`, `openmanus.py`, `vmtools.py`, `cloudshell.py`, `cloudwork.py` | Virtual-display mouse/keyboard, optional OpenManus bridge, KVM VM runner, remote cloud shell execution |
| `sysmon.py`, `filewatch.py`, `tokens.py` | psutil health snapshot and alerts; folder watcher posting to n8n; n8n token-provisioner client |
| `apache/`, `n8n/`, `models/`, `Dockerfile`, `docker-compose.yml` | Reverse proxy vhosts, importable workflows, Ollama Modelfiles, containers |

## Setup

Requirements: Linux host, Python 3.11 or 3.12, Ollama with the base models pulled, Discord bot token with the Message Content intent enabled. Optional: Apache or Docker, n8n, Playwright + system Chromium, LibreOffice, Marp, Quarto, tesseract, xdotool/scrot, faster-whisper.

```bash
git clone <repo> ~/laptop-agent && cd ~/laptop-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
for f in models/*.Modelfile; do ollama create "$(basename "$f" .Modelfile)" -f "$f"; done
cp .env.example .env   # then edit
```

Key `.env` variables (see `config.py` for the full list and defaults):

```
DISCORD_TOKEN=<your bot token>
OWNER_ID=000000000000000000        # your Discord user id; 0 = open mode
WORK_CHANNEL_ID=0                  # optional channel where plain messages are tasks
OLLAMA_HOST=http://127.0.0.1:11434
CODER_MODEL=abrar-coder  LIGHT_MODEL=abrar-chat  VISION_MODEL=abrar-vision  REASON_MODEL=abrar-reason
LLM_BACKEND=ollama                 # or openrouter (+ OPENROUTER_API_KEY, OPENROUTER_MODEL)
SANDBOX_ROOT=/home/USER            # file and shell sandbox
ALLOW_SHELL=true  SHELL_TIMEOUT=180  MAX_AGENT_ITERATIONS=8  AUTO_LEARN=false
BOT_HTTP_HOST=127.0.0.1  BOT_HTTP_PORT=8765
MAIL_MCP_URL=http://127.0.0.1:8973/mcp  MAIL_DEFAULT_ACCOUNT=personal  EMAIL_TO=user@example.com
MONITOR_ENABLED=true  ALERT_CPU=92  ALERT_MEM=90  ALERT_DISK=90
VM_ENABLED=true  OPENMANUS_ENABLED=false
```

Apache gateway (optional): `sudo a2enmod proxy proxy_http proxy_wstunnel`, copy `apache/laptop-agent.conf` to `sites-available`, enable and reload. It proxies `/bot/` to the bot and `/n8n/` to n8n. Import `n8n/email-workflow.json` and `n8n/token-provisioner.json` into n8n if you use those webhooks.

Docker alternative: `docker compose up -d --build` starts `ollama`, `n8n`, `apache`, a `webtop` desktop and the `bot` (the host home directory is bind-mounted so file edits land on the real disk; shell commands then run inside the container).

## Usage

Run on the host: `./run.sh` (or install `laptop-agent.service` as a user unit). Then in Discord:

| You send | Result |
|---|---|
| `!coder <task>` / `!task <task>` | full pipeline, progress streamed, report at the end |
| `make me a presentation about rivers` | detected as a document request, returns a `.pptx` |
| `$ ls -la` / `$cd Projects` | run a shell command yourself; cwd persists |
| `!think <problem>` | reasoning model |
| `!search <query>` / `!web <url or query>` | web search / open and summarise |
| `!math <question>` | program-aided maths |
| paste an image, PDF or voice note | vision / PDF text / transcription |
| `!stats`, `!ls <path>`, `!cat <path>` | laptop health, directory listing, file content |
| `!remember <fact>` / `!profile` / `!forget` | long-term profile notes |
| `!email <plain English>` then `yes` | draft a mail, confirm to send |
| `!inbox [account] [n]`, `!unread`, `!read <uid>`, `!mailsearch <q>`, `!send <account> <to> \| <subject> \| <body>` | mailbox access through the mail MCP |
| `!gui click X Y`, `!screen` | drive the virtual display and look at it |
| `!vm <cmd>` | run inside the sandbox VM |
| `!watch <dir>` | start a folder watcher that posts file events to n8n |
| `!status` / `stop` / `!help` | job status, cancel, command list |

HTTP trigger through Apache:

```bash
curl -s http://127.0.0.1/bot/health
curl -s -X POST http://127.0.0.1/bot/trigger -H 'Content-Type: application/json' \
     -d '{"task":"list the 10 biggest files in ~/Downloads and write them to report.txt"}'
```

Offline tests (no model needed): `./.venv/bin/python tests/test_mailbrain.py` and `tests/test_mail.py`.

## Project structure

```
laptop-agent/
├── bot.py  orchestrator.py  agents.py  llm.py  config.py
├── tools.py  safety.py  enforcer.py  rules.json  grounding.py
├── websearch.py  browser.py  vision.py  voice.py  pdftools.py
├── docgen.py  factcheck.py  mathsolver.py  mathreport.py
├── emailer.py  mailcompose.py  mailbrain.py  mailstyle.py
├── knowledge.py  userprofile.py  user_profile.json
├── gui.py  openmanus.py  vmtools.py  cloudshell.py  cloudwork.py
├── sysmon.py  filewatch.py  tokens.py
├── models/            Ollama Modelfiles (coder, chat, reason, vision, twin)
├── apache/            reverse-proxy vhosts (host + docker variants)
├── n8n/               email-workflow.json, token-provisioner.json
├── tests/             offline tests (mail, maths, twin)
├── Dockerfile  docker-compose.yml  run.sh  laptop-agent.service
├── requirements.txt  .env.example  KNOWN_ISSUES.md
└── workspace/         uploads/, outputs/, mail_style/ (runtime data, not published)
```

## Limitations and notes

- The models are tiny by design. `KNOWN_ISSUES.md` records the failure modes this causes: spurious refusals, persona breaks, agreeing with false claims. The enforcer, grounding and web-search routing exist to counter them, not to eliminate them.
- Safety rules are seatbelts against an autonomous small model making mistakes, not a security boundary against a hostile user. Never leave `OWNER_ID` unset outside a private DM context, and keep the Discord token in `.env` only.
- System monitoring and real host shell access require running on the host; inside Docker the bot sees the container.
- The mail commands need a separate local mail MCP server running on `MAIL_MCP_URL`; without it they fail gracefully.
- Vision, OCR, speech and document generation depend on external tools (Chromium, tesseract, LibreOffice, Marp, Quarto) being installed; each module reports clearly when its tool is missing.
- RAM pressure is the main operational constraint: Ollama is expected to hold one model at a time, and heavy modules (OpenManus, Playwright) are off or lazy by default.
