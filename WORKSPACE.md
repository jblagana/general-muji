# WORKSPACE.md — shared state for every session

Injected into every session's system prompt (cwd-independent). This is the
map: where your shared files live and what the agent can do. Keep it short —
it costs tokens in every session, every turn.

Edit this to point the agent at YOUR folders and conventions. Delete it if
you don't need it (the block is then omitted from the prompt).

## Shared memory (canonical paths)
- **Repo map:** `REPOS.md` — list your local clones + remotes here so the
  agent stops re-hunting paths.
- **Backlog (parked work):** `BACKLOG.md` — the shared "do it on free time"
  list; the agent parks off-agenda tasks here instead of dropping them.
- **Learned patterns:** `learned.md` (## active = in effect; ## pending =
  needs approval) — the agent appends candidate patterns after tasks; you
  approve by moving a line to ## active.
- **Tool quirks:** `tool_notes.md` (## active = verified fixes; follow them).
- **Notes / specs:** anywhere you like — the agent reads and writes files
  directly. Name the folder here so it knows where to look.

## Session registry (live cwds)
Sessions come and go — the live list is the DB (`data/muji.db` → `sessions`
table: title, cwd, processing, updated_at), not this file.

## Capabilities
- The agent can read/write files, run shell commands (approval-gated), search
  and fetch the web, verify math with sympy, and index + search local docs.
- Optional integrations (Gmail, Telegram, to-do, calendar) are off by default
  — enable them in `.env` (see README → "Choosing your tools").
