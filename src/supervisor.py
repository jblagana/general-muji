"""The supervisor — cross-run watch over every background agent turn.

A thin, deterministic health check (no LLM): the UI's "watch" strip polls
/api/watch every 5 s and the server's _supervisor_loop toasts on NEW
stuck/waiting findings. Detection is automatic; action is graded by
state: a `waiting` run (approval/question) stays the boss's click
only; a `stuck` run is ALSO auto stop-resumed by the 60 s loop
after 2 consecutive ticks (tool-in-flight grace, budget 2 per
episode — see auto_stop_decisions below), with the manual click
always available alongside.

States (worst first):
  stuck    — running, but silent (no event) past SILENT_AFTER_FIRST_LLM_S
             since the model last finished a call. A run can't be silent
             before its first llm_end: a long first model call emits
             nothing, and that is normal. An OPEN thinking burst is also
             not silence — thinking frames are ring-only (the DB row
             lands when the burst closes), so a long think would
             otherwise read as "stuck" to the clock.
  waiting  — parked on an approval or a question (the user is the gate).
  running  — healthy: events still flowing.
  queued   — no live run, but messages sit in the per-session FIFO;
             flagged (warn) only once the oldest entry ages past
             QUEUED_STALE_S.
"""
from __future__ import annotations

import re
import time

from . import db

#: the ring holds full SSE frame strings ("event: name\ndata: {...}\n\n"),
#: not bare names — pull the event name out of the frame
_FRAME_NAME = re.compile(r"^event: (\S+)")


def _frame_name(wire: str) -> str | None:
    m = _FRAME_NAME.match(wire or "")
    return m.group(1) if m else None


def _last_event_name(sid: str) -> str | None:
    """Name of the session's most recent persisted event (None if it has
    none). Same authority as db.last_event_ts (the DB, not the ring — the
    ring empties across a restart): when the LAST persisted event is a
    tool_start (no tool_end after it), a tool is still running and a long
    build/test/ssh is not stuck yet. Queried inline here (the
    taskstore.py db._lock/db._db pattern) instead of a db.py helper —
    keeps this feature's diff out of db.py while that file carries
    parallel work."""
    with db._lock:
        r = db._db().execute(
            "SELECT event FROM events WHERE session_id=? ORDER BY id DESC LIMIT 1",
            (sid,)).fetchone()
    return r[0] if r is not None else None

#: silence threshold, applied only AFTER the run's first llm_end — long
#: single model calls (the 220 s one seen in the Auto Wake session) are
#: the normal case, so the clock starts when the run has proven it can
#: cycle, not when it was accepted.
SILENT_AFTER_FIRST_LLM_S = 300
#: extra grace (s) while the last persisted event is an in-flight
#: tool_start — a long build/test/ssh/remote job emits no structural
#: events while it runs (progress is ring-only), so its silence clock
#: starts later: 300 s + 1800 s = 35 min of no persisted event at all
#: before a tool-hung run counts as stuck (2026-09-28, boss-approved
#: auto stop-resume design).
TOOL_IN_FLIGHT_GRACE_S = 1800
#: supervisor AUTO stop-resume (2026-09-28, boss-approved): a STUCK run
#: is auto-stopped + resumed only after STUCK_TICKS_REQUIRED consecutive
#: 60 s loop ticks (one supervisor heartbeat = one tick), with at most
#: AUTO_STOP_MAX cycles per stuck episode — then it's left stuck and
#: the manual click is the lever (no loop against a deterministically
#: broken state or an HPC outage). `waiting` runs are NEVER
#: auto-stopped: an open approval/question is the boss's gate. Manual
#: resume re-arms the budget (note_manual_resume); a finished run ends
#: the episode (note_finished_runs). The manual click
#: (/api/watch/stop-resume) is always available alongside.
STUCK_TICKS_REQUIRED = 2
AUTO_STOP_MAX = 2

#: per-session decision state, owned by the 60 s _supervisor_loop — the
#: UI's 5 s /api/watch polls call watch() (pure classification) but must
#: NEVER feed these counters
_stuck_ticks: dict[str, int] = {}
_auto_stop_fires: dict[str, int] = {}
#: a queued message older than this means the head run is eating everything
QUEUED_STALE_S = 600
#: waiting rows are always surfaced (the user is the gate); this cap only
#: feeds the "waiting Xm" phrasing
WAITING_PHRASE_S = 3600

_RANK = {"stuck": 3, "waiting": 2, "running": 1, "queued": 0}


def _fmt_age(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h {s % 3600 // 60}m"


def watch(tasks) -> dict:
    """One tick: classify every known run + queued session.

    `tasks` is the api.TaskManager. Returns {rows, worst} where rows are
    sorted worst-first (stuck > waiting > running > queued) and each row
    carries everything the strip/board renders (server-side phrasing —
    the client stays dumb)."""
    now = time.time()
    rows: list[dict] = []
    for sid, run in list(tasks.runs.items()):
        sess = db.get_session(sid)
        title = (sess or {}).get("title") or sid[:8]
        base = {
            "sid": sid, "title": title,
            "started_at": run.started_at,
            "age_s": int(now - run.started_at),
            "age": _fmt_age(now - run.started_at),
            "last_event": None, "last_event_ts": None,
            "queued": tasks.queue_count(sid),
            "why": None, "waiting_s": None, "waiting": None,
        }
        if not run.active:
            continue  # finished runs are business as usual (done-LED covers them)
        buf = list(run.buffer)
        last_name = _frame_name(buf[-1][1]) if buf else None
        # the DB row for the last structural event (ring-only frames like
        # token deltas don't count as activity — they never land in the DB)
        last_ts = db.last_event_ts(sid)
        base["last_event"] = last_name
        base["last_event_ts"] = last_ts
        if run.waiting_approval or run.waiting_question:
            base["status"] = "waiting"
            kind = "approval" if run.waiting_approval else "question"
            base["waiting"] = kind
            base["waiting_s"] = int(
                (run.approval_deadline if run.waiting_approval
                 else run.question_deadline or now) - now * 1000)
            base["why"] = f"waiting {kind}"
            base["what"] = f"waiting {kind} " + _fmt_age(now - run.started_at)
        else:
            silent = (now - last_ts) if last_ts else 0
            # 'cycled' = the run finished at least one full model call.
            # In-memory flag (set at the llm_end frame) with a DB fallback
            # (the flag is process-local; a run restored across a restart
            # starts un-cycled, and its llm_end rows are durable).
            cycled = (getattr(run, "cycled", False)
                      or db.has_event(sid, "llm_end", after_seq=run.start_seq))
            # An open thinking burst is NOT silence: thinking frames are
            # ring-only (the DB row lands when the burst closes), so a
            # long think would otherwise trip the stuck clock. The
            # in-memory buffer is the authority — a run restored across a
            # restart has no open burst (its finally flushed it to the DB).
            thinking = bool(getattr(run, "_think_buf", None))
            # An in-flight tool is NOT stuck yet: while a long
            # build/test/ssh job runs there is no persisted event at all
            # (progress is ring-only), so its threshold is extended.
            # The DB is the authority — the ring buffer empties across a
            # restart (a restored run has no ring frames).
            last_db = _last_event_name(sid)
            tool_in_flight = last_db == "tool_start"
            threshold = (SILENT_AFTER_FIRST_LLM_S
                         + (TOOL_IN_FLIGHT_GRACE_S if tool_in_flight else 0))
            if not thinking and cycled and silent >= threshold:
                base["status"] = "stuck"
                base["why"] = (f"silent {_fmt_age(silent)}"
                               + (f" after {last_name or last_db}"
                                  if (last_name or last_db) else ""))
                base["what"] = f"no event for {_fmt_age(silent)}"
            else:
                base["status"] = "running"
                if thinking:
                    base["what"] = "thinking…"
                elif tool_in_flight and silent >= SILENT_AFTER_FIRST_LLM_S:
                    base["what"] = f"tool running {_fmt_age(silent)}"
                else:
                    base["what"] = (f"last {last_name}" if last_name
                                    else "accepted — first model call")
        rows.append(base)
    # queued-only sessions (no live run in memory)
    for sid in list(tasks.queues):
        if sid in tasks.runs:
            continue
        q = tasks.queues[sid]
        if not q:
            continue
        sess = db.get_session(sid)
        title = (sess or {}).get("title") or sid[:8]
        oldest = min(e.get("created_at") or now for e in q)
        stale = now - oldest > QUEUED_STALE_S
        rows.append({
            "sid": sid, "title": title, "status": "queued",
            "started_at": oldest, "age_s": int(now - oldest),
            "age": _fmt_age(now - oldest),
            "last_event": None, "last_event_ts": None,
            "queued": len(q),
            "why": f"queued {_fmt_age(now - oldest)}" if stale else None,
            "waiting_s": None, "waiting": None,
            "what": f"{len(q)} message{'s' if len(q) > 1 else ''} waiting",
        })
    rows.sort(key=lambda r: -_RANK[r["status"]])
    worst = rows[0]["status"] if rows else "idle"
    return {"rows": rows, "worst": worst}


def auto_stop_decisions(rows: list[dict]) -> list[dict]:
    """One 60 s supervisor tick (called ONLY by _supervisor_loop — the
    UI's 5 s /api/watch polls never reach here): bump the
    consecutive-stuck counter per row and return the rows that should be
    auto stop-resumed NOW — stuck for STUCK_TICKS_REQUIRED consecutive
    ticks with budget left. Pure w.r.t. the task manager: the caller
    acts (api._stop_resume_session) and then books the cycle with
    note_auto_stop().

    `waiting` rows never fire (the boss's gate), and any non-stuck
    status resets the counter (the stuck spell broke)."""
    out: list[dict] = []
    for row in rows:
        sid = row["sid"]
        if row["status"] == "stuck":
            n = _stuck_ticks.get(sid, 0) + 1
            _stuck_ticks[sid] = n
            if (n >= STUCK_TICKS_REQUIRED
                    and _auto_stop_fires.get(sid, 0) < AUTO_STOP_MAX):
                out.append(row)
        else:
            _stuck_ticks.pop(sid, None)
    return out


def note_auto_stop(sid: str) -> None:
    """Book one auto stop-resume cycle for the session (the budget)."""
    _auto_stop_fires[sid] = _auto_stop_fires.get(sid, 0) + 1


def note_manual_resume(sid: str) -> None:
    """The boss clicked (↻ or stop+resume) — explicit intent re-arms the
    budget and clears the tick counter (same rule as the mid-stream LLM
    auto-resume budget in agent.py)."""
    _auto_stop_fires.pop(sid, None)
    _stuck_ticks.pop(sid, None)


def note_finished_runs(tasks) -> None:
    """Episode over: a session whose run is no longer active (finished,
    stopped or pruned) loses its tick counter and budget — the next
    stuck spell gets a fresh AUTO_STOP_MAX. Called once per supervisor
    tick, AFTER any auto action (a just-resumed session is active again,
    so its budget survives)."""
    for sid in set(_stuck_ticks) | set(_auto_stop_fires):
        run = tasks.get(sid)
        if run is None or not run.active:
            _stuck_ticks.pop(sid, None)
            _auto_stop_fires.pop(sid, None)
