"""SSE helpers and per-session stop events."""
from __future__ import annotations

import asyncio
import json


def sse(event: str, data: dict | None = None) -> str:
    """Format one Server-Sent-Event frame."""
    payload = json.dumps(data or {}, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


_stoppers: dict[str, asyncio.Event] = {}


def get_stopper(session_id: str) -> asyncio.Event:
    ev = _stoppers.get(session_id)
    if ev is None:
        ev = asyncio.Event()
        _stoppers[session_id] = ev
    return ev


def request_stop(session_id: str) -> None:
    get_stopper(session_id).set()


def clear_stopper(session_id: str) -> None:
    _stoppers.pop(session_id, None)
