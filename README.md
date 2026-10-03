# muji

A self-hosted, single-user agent harness with a web UI. Streaming answers, a
tool loop (files, shell, web, math verification), command approvals, sandboxed
previews of generated files, and live agent progress. **Multi-instance:** every
chat runs as a detached background agent — switch chats, reload or close the
tab and the work keeps going; several chats run at the same time. Runs against
**any OpenAI-compatible model endpoint** — OpenAI, OpenRouter, Ollama, vLLM,
llama.cpp server, your HPC-hosted model, etc.

The core is deliberately small and self-contained: a FastAPI backend (`src/`)
and a vanilla-JS frontend (`static/`) — no frameworks, no build step, no
database server (SQLite on disk). Personal integrations (Gmail, Telegram, a
local to-do/calendar store) ship as **opt-in tools** you enable in `.env` —
off by default, so a fresh clone runs the bare harness out of the box.

## Quick start (Windows)

**Double-click `start.bat`.** On first run it:

1. creates a local virtual environment (`muji\.venv`) and installs the
   dependencies,
2. copies `.env.example` → `.env` (if `.env` doesn't exist yet) — **edit
   `.env` to set your model endpoint + API key, and to opt in/out of tools**,
3. starts the server and opens your browser at http://127.0.0.1:8321.

(Needs Python 3.10+ on PATH — tick "Add python.exe to PATH" at install.)

### Point it at your model

Edit `.env`:

```env
OPENAI_BASE_URL=https://api.openai.com/v1   # any OpenAI-compatible base URL
OPENAI_API_KEY=sk-...
MODEL=gpt-4o-mini                            # the exact model string your endpoint expects
```

Ollama: `OPENAI_BASE_URL=http://localhost:11434/v1`, `MODEL=llama3.1`.

### Choosing your tools (opt-in / opt-out)

`TOOLS_DISABLED` in `.env` is a comma-separated list of tool **names** to
exclude. A disabled tool is dropped from the model's schema and prompt and any
call to it is refused. Fresh clones ship with the **personal integrations
disabled** and the **core enabled**:

| Group | Tools | Needs |
|---|---|---|
| Core (on) | `list_dir` `read_file` `write_file` `edit_file` `search_files` `run_command` `web_search` `fetch_url` `browser` `verify_math` `ask_user` | nothing extra |
| Local RAG (on) | `local_search` `index_documents` | nothing (BM25); set `RAG_EMBED_MODEL` for dense/hybrid |
| Gmail (off) | `gmail_search` `gmail_read` | a Gmail OAuth setup |
| Telegram (off) | `tg_search` `tg_read` | a Telegram session |
| To-do store (off) | `tasks_list` `tasks_add` `tasks_done` | nothing |
| Calendar store (off) | `events_add` `events_list` `events_done` | nothing |

To enable a group, remove its tools from `TOOLS_DISABLED` (and add the creds
for Gmail/Telegram). To cut a core tool, add its name. Restart the server
after editing `.env`.

## Layout

```
server.py          entry point  (python server.py → http://127.0.0.1:8321)
start.bat          Windows launcher (venv + deps + .env seed + launch)
requirements.txt   Python dependencies
.env.example       settings template (copied to .env on first run)
src/
  config.py        settings, loaded from .env
  api.py           FastAPI app: SSE chat, sessions, uploads, previews, approvals
  agent.py         the tool loop + system prompt
  llm.py           OpenAI-compatible client (native tools or text protocol)
  tools.py         the tool registry (files, shell, web, math, RAG, integrations)
  db.py            SQLite layer (sessions, messages, trajectories, approvals)
  rag.py           local document index (BM25 + optional embeddings)
  tasks.py         background task manager (detached, multi-instance)
  taskstore.py     local to-do + calendar store (opt-in tools)
  gmail.py gtasks.py email.py telegram.py   personal integrations (opt-in)
  reminders.py supervisor.py sse.py         toasts, watchdog, stop signaling
static/
  index.html app.js style.css sw.js manifest.json   the web UI (vanilla JS)
tools/
  test_onit.py test_learned.py run_tests.py   test suites
```

## Tests

```
.venv\Scripts\python.exe tools\test_onit.py
.venv\Scripts\python.exe tools\test_learned.py
```

No server or model needed — they exercise the pure functions and the SQLite
layer directly against temp paths.

## Notes

- **Approvals:** read-only tools run freely; file edits, shell commands, and
  web fetches raise an approval card you can approve once or always. Destructive
  commands and server-restart commands always ask.
- **Self-restart:** the agent may restart the server itself only through
  `POST /api/restart`, and only after it has verified its own change (imports
  clean + tests green) and no other chat is mid-run. The sidebar ⟳ button
  always works.
- **Privacy:** everything runs on your machine. Your API key and any
  integration creds live in `.env` (git-ignored). The model endpoint you point
  at is the only place prompts leave the box.
