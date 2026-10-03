"""Local task + calendar store (option A — the alarm-clock layer).

Substitutes Google Tasks: rows live in muji.db (tables `tasks` and
`calendar`), due times are exact (ISO 8601 with offset), and the reminder
poller in api.py can fire Windows toasts at the moment — the thing Google
Tasks can't do because it stores dates, not times.

`due_iso` / `start_iso` are the source of truth (what the user typed);
`due_utc` / `start_utc` are epoch seconds for SQL comparison.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from . import db


def _parse_iso(s: str) -> float | None:
    """ISO 8601 → epoch seconds. Naive timestamps are taken as local time.
    Returns None for empty/None input (dateless task)."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(f"bad ISO 8601 date/time: {s!r} "
                         "(expected e.g. '2026-09-25T09:00:00+08:00')")
    if dt.tzinfo is None:
        dt = dt.astimezone()  # local
    return dt.timestamp()


def _fmt(r) -> dict:
    d = dict(r)
    d["done"] = bool(d.get("done"))
    d["notified"] = bool(d.get("notified"))
    return d


# ── tasks ───────────────────────────────────────────────────────────

def add_task(title: str, due: str = "", note: str = "") -> dict:
    title = (title or "").strip()
    if not title:
        raise ValueError("task needs a title")
    due_iso = (due or "").strip()
    due_utc = _parse_iso(due_iso)  # raises on bad input
    now = time.time()
    with db._lock:
        cur = db._db().execute(
            "INSERT INTO tasks(title,note,due_iso,due_utc,done,created_at) "
            "VALUES(?,?,?,?,0,?)",
            (title, (note or "").strip(), due_iso or None, due_utc, now))
        db._db().commit()
        tid = cur.lastrowid
    with db._lock:
        r = db._db().execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    return _fmt(r)


def list_tasks(include_done: bool = False, max_results: int = 50) -> list[dict]:
    sql = "SELECT * FROM tasks"
    if not include_done:
        sql += " WHERE done=0"
    sql += " ORDER BY (due_utc IS NULL), due_utc, id LIMIT ?"
    with db._lock:
        rows = db._db().execute(sql, (max_results,)).fetchall()
    return [_fmt(r) for r in rows]


def toggle_task(tid: int, done: bool = True) -> dict | None:
    with db._lock:
        db._db().execute("UPDATE tasks SET done=? WHERE id=?", (1 if done else 0, tid))
        db._db().commit()
        r = db._db().execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    return _fmt(r) if r is not None else None


def update_task(tid: int, title: str | None = None,
                note: str | None = None, due: str | None = None) -> dict | None:
    """Edit an existing task. None = leave that field untouched; a provided
    value replaces it (title must be non-empty; due = ISO 8601 or "" to clear).
    The UI edit modal (Boss: "make my tasks editable so i can modify it
    myself") and any future agent edit-tool both go through here."""
    with db._lock:
        cur = db._db().execute("SELECT * FROM tasks WHERE id=?", (tid,))
        row = cur.fetchone()
        if row is None:
            return None
        t = dict(row)
        new_title = t["title"]
        if title is not None:
            new_title = (title or "").strip()
            if not new_title:
                raise ValueError("task needs a title")
        new_note = t["note"]
        if note is not None:
            new_note = (note or "").strip()
        new_due_iso = t["due_iso"]
        new_due_utc = t["due_utc"]
        if due is not None:
            new_due_iso = (due or "").strip()
            new_due_utc = _parse_iso(new_due_iso)  # raises on bad input
        db._db().execute(
            "UPDATE tasks SET title=?, note=?, due_iso=?, due_utc=? WHERE id=?",
            (new_title, new_note, new_due_iso or None, new_due_utc, tid))
        db._db().commit()
        r = db._db().execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    return _fmt(r) if r is not None else None


def delete_task(tid: int) -> bool:
    with db._lock:
        cur = db._db().execute("DELETE FROM tasks WHERE id=?", (tid,))
        db._db().commit()
    return cur.rowcount > 0


# ── calendar (events) ───────────────────────────────────────────────

def add_event(title: str, start: str, end: str = "", note: str = "") -> dict:
    title = (title or "").strip()
    if not title:
        raise ValueError("event needs a title")
    start_iso = (start or "").strip()
    start_utc = _parse_iso(start_iso)
    if start_utc is None:
        raise ValueError("event needs a start time (ISO 8601)")
    end_iso = (end or "").strip()
    end_utc = _parse_iso(end_iso)
    if end_utc is not None and end_utc <= start_utc:
        raise ValueError("end must be after start")
    now = time.time()
    with db._lock:
        cur = db._db().execute(
            "INSERT INTO calendar(title,note,start_iso,start_utc,end_utc,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (title, (note or "").strip(), start_iso, start_utc, end_utc, now))
        db._db().commit()
        eid = cur.lastrowid
    with db._lock:
        r = db._db().execute("SELECT * FROM calendar WHERE id=?", (eid,)).fetchone()
    return _fmt(r)


def list_events(start_utc: float | None = None,
                end_utc: float | None = None,
                limit: int = 50,
                include_done: bool = False) -> list[dict]:
    """Events overlapping [start_utc, end_utc); no bounds = next `limit`.
    Done events are hidden by default (they're history, not schedule)."""
    sql = "SELECT * FROM calendar WHERE 1=1"
    args: list = []
    if not include_done:
        sql += " AND done=0"
    if start_utc is not None:
        sql += " AND (end_utc IS NULL OR end_utc >= ?)"
        args.append(start_utc)
    if end_utc is not None:
        sql += " AND start_utc < ?"
        args.append(end_utc)
    sql += " ORDER BY start_utc, id LIMIT ?"
    args.append(limit)
    with db._lock:
        rows = db._db().execute(sql, tuple(args)).fetchall()
    return [_fmt(r) for r in rows]


def toggle_event(eid: int, done: bool = True) -> dict | None:
    """Mark an event done (or reopen it). Done events stay in the store
    as history — the row keeps its title/time, the UI strikes it through."""
    with db._lock:
        db._db().execute("UPDATE calendar SET done=? WHERE id=?",
                         (1 if done else 0, eid))
        db._db().commit()
        r = db._db().execute("SELECT * FROM calendar WHERE id=?", (eid,)).fetchone()
    return _fmt(r) if r is not None else None


def delete_event(eid: int) -> bool:
    with db._lock:
        cur = db._db().execute("DELETE FROM calendar WHERE id=?", (eid,))
        db._db().commit()
    return cur.rowcount > 0


# ── reminder engine helpers (used by the api.py poller) ─────────────

def due_items(now: float | None = None) -> list[dict]:
    """Unnotified, not-done tasks + events whose moment has arrived."""
    now = now if now is not None else time.time()
    with db._lock:
        trows = db._db().execute(
            "SELECT * FROM tasks WHERE done=0 AND notified=0 "
            "AND due_utc IS NOT NULL AND due_utc<=? ORDER BY due_utc", (now,)
        ).fetchall()
        erows = db._db().execute(
            "SELECT * FROM calendar WHERE notified=0 AND done=0 "
            "AND start_utc<=? ORDER BY start_utc", (now,)
        ).fetchall()
    out = []
    for r in trows:
        d = _fmt(r)
        d["kind"] = "task"
        out.append(d)
    for r in erows:
        d = _fmt(r)
        d["kind"] = "event"
        out.append(d)
    return out


def mark_notified(kind: str, item_id: int) -> None:
    table = "tasks" if kind == "task" else "calendar"
    with db._lock:
        db._db().execute(
            f"UPDATE {table} SET notified=1 WHERE id=?", (item_id,))
        db._db().commit()
