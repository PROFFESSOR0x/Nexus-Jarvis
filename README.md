# NEXUS — Multi-Agent Command System

NEXUS is a **leader + worker** multi-agent system with two interfaces: a terminal TUI and a JARVIS-style live web deck.

One **leader agent** owns every conversation. It answers simple things directly, uses tools itself for single workstreams, and — when a request splits into independent parts — appoints isolated **worker subagents** that run concurrently, talk to each other over a shared radio, coordinate on a joint plan, and report back. The leader verifies, synthesizes, and writes exactly **one final answer** per turn.

- 🖥️ **Textual TUI** — conversation, tool ledger, sessions, stats, live log tail, health status bar
- 🛰️ **Web command deck** — live system resources, agent topology graph, worker radio, activity, thinking stream, terminal, logs, memory
- 🧠 **Long-term memory** — user profile + facts in `~/.nexus/memory.json`, survives restarts
- 🛠️ **Real tools** — shell, code execution (python / javascript / c / cpp / csharp / pwsh), web search, web fetch, parallel fan-out
- 📻 **Agent teamwork** — delegation rounds with radio bus + joint plan, per-worker iteration budgets
- 📝 **Everything logged** — every tool call, model call, save action and result lands in `nexus.log`
- 💬 **Slash commands** — sessions, models, thinking effort, memory (`/help` for all)

> Arabic docs: [README.ar.md](README.ar.md)

> ⚠️ **RESEARCH WARNING — READ FIRST:** NEXUS agents operate **without restrictions**: they run real shell commands, execute arbitrary code, and browse the web with **no sandbox, no approval gates, no guardrails**. This project is **purely for research**. Run it only on a machine or VM you can afford to break, never on production systems or alongside sensitive data/credentials. **You are solely responsible for everything the agents do.**

---

## 1. Requirements

| Need | Details |
| ---- | ------- |
| Python | 3.12 or newer |
| OS | Windows, Linux or macOS |
| Model backend | **Ollama** (local or cloud models) **or** any OpenAI-compatible API |
| Disk | ~100 MB for the app (models are extra) |

---

## 2. Installation

### Step 1 — Get the code and enter it

```bash
cd nexus
```

### Step 2 — Create a virtual environment and install dependencies

Windows (PowerShell):

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Linux / macOS:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

This installs: `textual rich httpx python-dotenv ddgs html2text openai ollama fastapi "uvicorn[standard]" psutil`.

### Step 3 — Install Ollama (if using it) and pull a model

1. Install from [ollama.com](https://ollama.com) and make sure it serves on `http://localhost:11434` (`ollama serve` / `ollama list` to check).
2. Pull at least one model, e.g.:

```bash
ollama pull qwen:14b
```

Cloud models (e.g. `gpt-oss:120b-cloud`) work too once you are signed in to Ollama.

*Using OpenAI instead? Skip Ollama and set `LLM_PROVIDER=openai` + `OPENAI_API_KEY` in `.env`.*

### Step 4 — Configure `.env`

Copy the shipped `.env` and edit values (all keys explained below). Minimal working example for Ollama:

```ini
LLM_PROVIDER=ollama
OLLAMA_MODEL=qwen:14b
OLLAMA_HOST=http://localhost:11434
```

---

## 3. Configuration (`.env` reference)

| Key | Default | What it does |
| --- | ------- | ------------ |
| `LLM_PROVIDER` | `openai` | `ollama` or `openai` |
| `LLM_MODEL` | `gpt-4o` | Model name when provider is `openai` |
| `OPENAI_API_KEY` | — | API key (openai mode) |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Any OpenAI-compatible endpoint |
| `OLLAMA_MODEL` | `llama3.1` | Model name when provider is `ollama` |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL (`0.0.0.0` is auto-fixed to localhost) |
| `LLM_TEMPERATURE` | `0.7` | Sampling temperature |
| `LLM_MAX_TOKENS` | `8192` | Max reply tokens |
| `THINK_EFFORT` | `medium` | Reasoning depth: `low` / `medium` / `high` (Ollama native) |
| `MAX_ITERATIONS` | `100` | Leader steps per turn |
| `MAX_SUBAGENTS` | `100` | Max workers per delegation round |
| `SUBAGENT_MAX_ITERATIONS` | `100` | Steps per worker |
| `DELEGATE_MAX_ROUNDS` | `100` | Delegation rounds per turn |
| `WORKSPACE_ROOT` | `~/.nexus/sessions` | Where sessions live |
| `WEB_HOST` / `WEB_PORT` | `127.0.0.1` / `8777` | Web deck bind address |

> Tip: `/model` and `/thinking` change the model/effort **live** (and save back to `.env`) — no restart needed.

---

## 4. Running

| Mode | Command | Notes |
| ---- | ------- | ----- |
| TUI | `python main.py` | Full terminal interface. `Ctrl+N` new session, `Ctrl+S` save, `Ctrl+L` clear, `Ctrl+Q` quit |
| Web deck | `python main.py --web` | Open http://127.0.0.1:8777 · `--host` / `--port` to change bind, `--session <id>` to resume |
| CLI | `python main.py --cli` | One-shot/headless. Add `--prompt "..."` to skip the input line |

The deck refuses to start if the port is already taken (split-brain guard) — kill the old process instead of stacking servers.

---

## 5. Using NEXUS

**Just talk.** Greetings and knowledge questions are answered directly. Anything needing tools or research is executed; multi-part jobs are delegated to workers and synthesized.

**Slash commands** (web terminal shows an autocomplete menu on `/`; TUI renders them as amber cards):

| Command | Effect |
| ------- | ------ |
| `/new` | Start a new session |
| `/sessions` | List old conversations (with previews) |
| `/open <id…>` | Switch back to an old conversation (history restored) |
| `/delete <id…>` | Delete a conversation (never the active one) |
| `/models` | List available models (live from Ollama) |
| `/model <name>` | Switch model immediately + persist |
| `/thinking [low\|medium\|high]` | Show/set reasoning effort |
| `/memory` | Show what NEXUS remembers about you |
| `/clear` | Clear the screen (local) |
| `/help` | This list |

**Memory:** NEXUS asks your name + what you work on when memory is empty, then remembers durably. Ask *"what do you remember about me?"* anytime; say *"forget X"* to erase.

**Delegation, briefly:** the leader must fan out at 2+ independent workstreams (ONE `agent.delegate` call), using the ROLE/GOAL/CONTEXT/METHOD/OUTPUT/TALK brief framework. Workers share a radio (`agent.say`) and a joint plan (`agent.plan`), can never delegate further, and their completions are logged as `[SUBAGENT]` — exactly one `[AGENT] final` per turn.

---

## 6. Web deck panels

| Panel | Shows |
| ----- | ----- |
| SYSTEM / NETWORK | Live CPU, memory, disk, per-core bars, top processes, up/down sparkline |
| ENTITY | Backend topology graph: LEADER hub + worker entities, MODEL/MEM chips, signal pulses, per-agent tool dots; **draggable cards** (double-click resets) |
| TOOL I/O | Fixed feed of every tool call with native rendering (terminal windows, tables, highlighted JSON) |
| AGENTS | Leader + rounds + worker cards with briefs, timings, radio counters |
| MEMORY | Profile + recent facts (auto-refreshes) |
| COMMS | Worker radio traffic + plan operations |
| ACTIVITY | Tool ledger with status + timings |
| THINKING | Live reasoning traces (leader + workers) |
| TERMINAL | Chat (markdown + tables + LaTeX math + RTL/Arabic rendering) |
| LOGS | `nexus.log` tail, live |

---

## 7. Project structure

```text
nexus/
├── main.py            # everything backend: tools, agents, TUI, sessions, memory, logger
├── webui.py           # FastAPI backend: REST + WebSocket bus + system stats
├── web/               # frontend: index.html, app.js, styles.css (no build step, no CDN)
│   ├── index.html
│   ├── app.js
│   └── styles.css
├── system_prompt.txt  # leader instructions (mandate, brief framework, lessons)
├── nexus.log          # every tool/model/save/result (rotated 2 MB × 3, git-ignored)
├── requirements.txt
├── .env.example       # template with safe defaults (commit this instead)
├── .gitignore
├── LICENSE            # MIT
├── README.md          # this file (English)
└── README.ar.md       # Arabic docs
```

Runtime data lives outside the repo: sessions in `~/.nexus/sessions/{id}/` (`session.json`, `context.json`, `workspace/`), memory in `~/.nexus/memory.json`.

---

## 8. Troubleshooting

| Symptom | Cause / fix |
| ------- | ----------- |
| Deck won't start, "port busy" | Another backend owns the port — stop it, don't stack servers |
| Header shows LLM OFFLINE | Ollama isn't running (`ollama serve`) or `OLLAMA_HOST` is wrong |
| `[Ollama error] … 500 (ref: …)` | Ollama cloud flaked — NEXUS auto-retries twice, then says so politely; resend, or `/model` a local model |
| `session.json` corrupt/empty | Saves are atomic; loader reports instead of crashing — worst case, `/new` |
| Workers silent / budget dead | Briefs now mandate radio discipline + 100-step budgets; check COMMS feed |
| Frontend looks stale | Hard-refresh (Ctrl+F5); assets are version-pinned (`?v=`) |

---

## 9. Notes

- Workers can read memory but only the leader writes it.
- Tool results are truncated in history (20–50 KB caps) to protect context.
- Single-instance web backend by design; TUI and web are alternative frontends, not synced live.
