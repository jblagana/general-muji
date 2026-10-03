"""Telegram bridge (Telethon, user mode): rides the Boss's own account.

Two directions:
  IN  — a message to the Boss's number → routed to a muji session → the
        agent runs → the final answer is sent back to the same chat.
        Approvals and questions arrive as inline-button messages; tapping
        answers them through the same /api endpoints the web UI uses.
  OUT — the agent's own tools (tg_search / tg_read, registered in tools.py)
        let the model read the Boss's Telegram conversations.

The session file (data/telegram.session) is a full key to the account —
it lives in data/ (gitignored), never in the repo, never sent anywhere.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os

import httpx

from . import db, tasks
from .config import settings

try:
    from telethon import TelegramClient, events
    _HAVE_TELETHON = True
except Exception:  # noqa: BLE001 — telethon optional at import time
    TelegramClient = None  # type: ignore
    events = None  # type: ignore
    _HAVE_TELETHON = False

API_ID = int(os.environ.get("TG_API_ID") or 0)
API_HASH = os.environ.get("TG_API_HASH") or ""
PHONE = os.environ.get("TG_PHONE") or ""
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN") or ""
SESSION_PATH = str(settings.data_dir / "telegram")  # + ".session" (Telethon)
BOT_SESSION_PATH = str(settings.data_dir / "telegram_bot")  # + ".session"
MAP_PATH = settings.data_dir / "tg_session_map.json"

TG_MAX = 4096          # Telegram's hard per-message length
MEDIA_CAP = 20 * 1024 * 1024  # 20 MB per downloaded media file

client: "TelegramClient | None" = None
bot_client: "TelegramClient | None" = None
#: The loop each client is bound to, captured at connect time. NEVER use the
#: `client.loop` property from a tool worker thread: Telethon 1.45 resolves
#: it to the CALLING thread's running loop (helpers.get_running_loop), and a
#: worker thread without a loop gets a fresh loop that never runs — so a
#: coroutine scheduled on it hangs until timeout, every single time. That
#: property is why tg_read timed out 18/18 (2026-09-27).
_user_loop: "asyncio.AbstractEventLoop | None" = None
_bot_loop: "asyncio.AbstractEventLoop | None" = None
_owner_id: int | None = None
_bot_chat_id: int | None = None  # bot's own id — the user bridge must skip it
_tasks: list[asyncio.Task] = []

# IN routing (2026-09-27): the user-mode bridge is DISABLED by default —
# the BotFather bot is the only inbound channel (boss: "I only want
# messages from the muji bot"). The user client still lives for the
# tg_read/tg_search tools (a bot can only read chats it's a member of).
# Re-enable for specific chats: TG_USER_BRIDGE_CHATS=123,456 (chat ids).
# Empty (default) = the user bridge fires on nothing.
_user_bridge_chats: set[int] = {
    int(x) for x in (os.environ.get("TG_USER_BRIDGE_CHATS") or "").replace(",", " ").split()
    if x.isdigit()
}


def log(level: str, msg: str) -> None:
    try:
        from .api import log as api_log
        api_log(level, f"telegram: {msg}")
    except Exception:  # noqa: BLE001 — api not imported yet (boot order)
        print(f"[telegram] {msg}", flush=True)


# ── session map: tg chat id → muji session id ───────────────────────

def _load_map() -> dict:
    try:
        return json.loads(MAP_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_map(m: dict) -> None:
    MAP_PATH.write_text(json.dumps(m, indent=1), encoding="utf-8")


def _session_for(tg_chat_id: int) -> str:
    """Resolve (or create) the muji session behind a Telegram chat."""
    m = _load_map()
    sid = m.get(str(tg_chat_id))
    if sid and db.get_session(sid):
        return sid
    sess = db.create_session()
    m[str(tg_chat_id)] = sess["id"]
    _save_map(m)
    return sess["id"]


def _chat_title(tg_chat_id: int) -> str:
    return f"telegram-{tg_chat_id}"


# ── client lifecycle ────────────────────────────────────────────────

def _new_client() -> "TelegramClient":
    return TelegramClient(SESSION_PATH, API_ID, API_HASH,
                          system_version="muji 1.0", device_model="muji")


async def tg_login() -> dict:
    """One-time interactive login: phone → code (→ 2FA password).
    After success the session file persists; later boots are silent."""
    global client, _owner_id, _user_loop
    if not _HAVE_TELETHON or not API_ID or not API_HASH:
        return {"ok": False, "error": "telethon not installed or TG_API_ID/TG_API_HASH missing"}
    if client is not None and client.is_connected():
        return {"ok": True, "already": True}
    c = _new_client()
    await c.connect()
    if not await c.is_user_authorized():
        await c.send_code_request(PHONE or None)
        code = input("Telegram code: ").strip()
        await c.sign_in(code=code)
        # 2FA (if the account has one) — Telethon raises SessionPasswordNeeded
        try:
            from telethon.errors import SessionPasswordNeededError
        except Exception:  # noqa: BLE001
            SessionPasswordNeededError = None  # type: ignore
        if SessionPasswordNeededError is not None:
            try:
                await c.sign_in(code=code)
            except SessionPasswordNeededError:
                from telethon.tl.functions.auth import CheckPasswordRequest
                pw = input("2FA password: ").strip()
                await c(function=CheckPasswordRequest(password=pw))
    me = await c.get_me()
    _owner_id = me.id
    client = c
    _user_loop = asyncio.get_running_loop()
    log("info", f"logged in as {me.id} ({getattr(me, 'first_name', '?')})")
    return {"ok": True, "id": me.id,
            "name": f"{getattr(me, 'first_name', '')} {getattr(me, 'last_name', '')}".strip()}


async def start() -> None:
    """Boot-time connect. Silent when a session file exists; if none does,
    the boss runs `python tools/tg_login.py` once (interactive)."""
    global client, _owner_id, _user_loop
    if not _HAVE_TELETHON or not API_ID or not API_HASH:
        log("warn", "disabled — TG_API_ID/TG_API_HASH not set (or telethon missing)")
        return
    c = _new_client()
    await c.connect()
    if not await c.is_user_authorized():
        await c.disconnect()
        log("warn", "no saved session — run `python tools/tg_login.py` once to log in")
        return
    me = await c.get_me()
    _owner_id = me.id
    client = c
    _user_loop = asyncio.get_running_loop()
    # Telethon 1.45: add_event_handler takes an Event object, not kwargs
    c.add_event_handler(_on_message, events.NewMessage(from_users=_owner_id))
    c.add_event_handler(_on_callback, events.CallbackQuery())  # owner check inside
    if _user_bridge_chats:
        log("info", f"connected as {me.id} — user bridge on for chats {sorted(_user_bridge_chats)}")
    else:
        log("info", f"connected as {me.id} — user bridge OFF (bot-only inbound; tools only)")
    try:
        await _start_bot()
    except Exception as e:  # noqa: BLE001 — bot channel is optional
        log("warn", f"bot channel failed to start: {type(e).__name__}: {e}")


async def _start_bot() -> None:
    """Second channel: the BotFather bot. Runs alongside the user-mode
    bridge with its own session file; answers the boss only."""
    global bot_client, _bot_chat_id, _bot_loop
    if not _HAVE_TELETHON:
        return
    if not BOT_TOKEN:
        log("warn", "bot disabled — TG_BOT_TOKEN not set")
        return
    if not API_ID or not API_HASH:
        log("warn", "bot disabled — TG_API_ID/TG_API_HASH not set")
        return
    # Telethon 1.45 bot mode: api_id/api_hash in the constructor,
    # token via sign_in(bot_token=...) — no phone involved
    b = TelegramClient(BOT_SESSION_PATH, API_ID, API_HASH,
                       system_version="muji 1.0", device_model="muji")
    await b.connect()
    if not await b.is_user_authorized():
        await b.sign_in(bot_token=BOT_TOKEN)
    me = await b.get_me()
    bot_client = b
    _bot_loop = asyncio.get_running_loop()
    _bot_chat_id = me.id  # so the user bridge can skip the bot's own chat
    b.add_event_handler(_bot_on_message, events.NewMessage(incoming=True))
    b.add_event_handler(_bot_on_callback, events.CallbackQuery())
    log("info", f"bot connected as @{getattr(me, 'username', '?')} (id {me.id}) — owner-only")


async def stop() -> None:
    global client, bot_client, _user_loop, _bot_loop
    for t in list(_tasks):
        t.cancel()
    for c in (client, bot_client):
        if c is not None:
            try:
                await c.disconnect()
            except Exception:  # noqa: BLE001
                pass
    client = None
    bot_client = None
    _user_loop = None
    _bot_loop = None


def status() -> dict:
    return {"ok": _HAVE_TELETHON and bool(API_ID and API_HASH),
            "connected": bool(client is not None and client.is_connected()),
            "bot_connected": bool(bot_client is not None and bot_client.is_connected()),
            "owner_id": _owner_id}


# ── IN: telegram → muji ─────────────────────────────────────────────

async def _on_message(event) -> None:
    """Incoming message from the boss (his own account only)."""
    try:
        chat = await event.get_chat()
        chat_id = chat.id
        # The bot's own chat belongs to the bot channel — without this the
        # user bridge ALSO fires on the boss's outgoing messages to the bot
        # (Telethon's from_users filter doesn't exclude them), so both
        # channels POST the same text to two muji sessions.
        if chat_id == _bot_chat_id:
            return
        # Inbound routing (2026-09-27, boss: "only from the muji bot"): the
        # user bridge is DISABLED by default — it previously fired on EVERY
        # chat of the boss's account (classmates, groups, DMs), because the
        # from_users=owner filter matches the owner's own outgoing messages
        # in any conversation. Opt back in per chat via TG_USER_BRIDGE_CHATS.
        if not _user_bridge_chats or chat_id not in _user_bridge_chats:
            return
        text = (event.raw_text or "").strip()
        # /stop kills the current turn; /new starts a fresh session;
        # /list shows the mapped chats; /status shows the bridge state
        if text in ("/stop", "stop"):
            sid = _load_map().get(str(chat_id))
            if sid:
                tasks.cancel(sid)
                tasks.queues.pop(sid, None)
                try:
                    db.queue_clear(sid)
                except Exception:  # noqa: BLE001
                    pass
                await event.reply("⏹ stopped.")
            else:
                await event.reply("No muji chat mapped to this conversation.")
            return
        if text in ("/new", "new"):
            sid = _session_for(chat_id)
            db.rename_session(sid, _chat_title(chat_id))
            await event.reply("🆕 Fresh muji chat started.")
            return
        if text in ("/list", "list"):
            m = _load_map()
            lines = []
            for tg_id, sid in m.items():
                s = db.get_session(sid)
                if s:
                    lines.append(f"• {s.get('title') or '(untitled)'}  (tg:{tg_id})")
            await event.reply("Mapped muji chats:\n" + ("\n".join(lines) or "(none)"))
            return
        if text in ("/status", "status"):
            st = status()
            await event.reply(f"bridge: {'ok' if st['ok'] else 'off'} | "
                              f"connected: {st['connected']} | owner: {st['owner_id']}")
            return

        # media → uploads dir (same shape the web composer ships)
        files: list[dict] = []
        if event.message.media is not None and _HAVE_TELETHON:
            files = await _download_media(event.message, text)
        if not text and not files:
            return
        t = asyncio.create_task(_run_turn(chat_id, text, files))
        _tasks.append(t)
        t.add_done_callback(_tasks.discard)
    except Exception as e:  # noqa: BLE001 — never kill the handler
        log("error", f"on_message: {type(e).__name__}: {e}")


async def _download_media(msg, caption: str, c=None) -> list[dict]:
    """Download the message's media into the uploads dir; returns the
    `files` list the agent expects ([{name, original}])."""
    c = c or client
    out: list[dict] = []
    if c is None:
        return out
    try:
        # file= dir is load-bearing (2026-09-27): with file=None Telethon
        # downloads into the process CWD — and the server's CWD is not the
        # repo root, so the file landed in the wrong dir and the agent's
        # attachment loop (uploads_dir / name) silently dropped every image
        # sent to the bot. Always pin the destination to uploads/.
        dst = await c.download_media(msg, file=str(settings.uploads_dir))
        if not dst:
            return out
        name = os.path.basename(str(dst))  # Telethon returns the full path
        p = settings.uploads_dir / name
        if not p.is_file() or p.stat().st_size > MEDIA_CAP:
            return out
        base = msg.file.name if getattr(msg, "file", None) and getattr(msg.file, "name", None) else name
        out.append({"name": name, "original": base or caption[:80] or name})
    except Exception as e:  # noqa: BLE001
        log("warn", f"media download failed: {type(e).__name__}: {e}")
    return out


async def _run_turn(tg_chat_id: int, text: str, files: list[dict], c=None) -> None:
    """One agent turn, triggered from Telegram: enqueue, follow the SSE
    stream, answer the boss with the final text (chunked to 4096)."""
    sid = _session_for(tg_chat_id)
    sess = db.get_session(sid) or {}
    if not (sess.get("title") or "").strip():
        db.rename_session(sid, _chat_title(tg_chat_id))
    user_text = text or "(media)"
    try:
        await _post("/api/chat", {
            "session_id": sid,
            "message": user_text,
            "files": files,
            "mode": sess.get("mode") or "act",
            "roast": sess.get("roast") or "chill",
        })
    except Exception as e:  # noqa: BLE001
        await _reply(tg_chat_id, f"⚠️ couldn't start the run: {e}", c)
        return
    await _follow_stream(tg_chat_id, sid, c)


async def _post(path: str, body: dict) -> dict:
    async with httpx.AsyncClient(base_url=f"http://{settings.host}:{settings.port}",
                                 timeout=30) as hx:
        r = await hx.post(path, json=body)
        r.raise_for_status()
        return r.json()


async def _follow_stream(tg_chat_id: int, sid: str, c=None) -> None:
    """Consume the session's SSE stream until the run ends. Sends the
    final answer back; approvals/questions become button messages."""
    base = f"http://{settings.host}:{settings.port}"
    # last durable seq: the stream replays from there, so nothing is missed
    try:
        async with httpx.AsyncClient(base_url=base, timeout=30) as hx:
            r = await hx.get(f"/api/sessions/{sid}/events_log",
                             params={"after": 0, "limit": 1})
            r.raise_for_status()
            rows = r.json().get("events") or []
            after = rows[-1]["seq"] if rows else 0
    except Exception:  # noqa: BLE001 — start from 0, replay is harmless
        after = 0
    async with httpx.AsyncClient(base_url=base, timeout=None) as hx:
        async with hx.stream("GET", f"/api/sessions/{sid}/events",
                             params={"after": after}) as r:
            event, data = None, None
            async for line in r.aiter_lines():
                if line.startswith("event: "):
                    event = line[7:].strip()
                elif line.startswith("data: "):
                    data = line[6:]
                elif line == "":
                    if event and data:
                        try:
                            payload = json.loads(data)
                        except Exception:  # noqa: BLE001
                            payload = {}
                        if event in ("done", "stopped", "error"):
                            await _finish(tg_chat_id, event, payload, c)
                            return
                        if event == "approval":
                            await _send_approval(tg_chat_id, payload, c)
                        elif event == "question":
                            await _send_question(tg_chat_id, payload, c)
                    event, data = None, None


async def _finish(tg_chat_id: int, event: str, payload: dict, c=None) -> None:
    if event == "done":
        content = (payload.get("content") or "").strip() or "(done — no text)"
        for chunk in _chunk(content):
            await _reply(tg_chat_id, chunk, c)
    elif event == "stopped":
        await _reply(tg_chat_id, f"⏹ {payload.get('reason', 'stopped')}", c)
    else:
        await _reply(tg_chat_id, f"⚠️ {payload.get('message', 'error')}", c)


def _chunk(text: str, size: int = TG_MAX) -> list[str]:
    """Split at 4096, preferring line boundaries (Telegram hard limit)."""
    out = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        out.append(text)
    return out


async def _reply(tg_chat_id: int, text: str, c=None) -> None:
    c = c or client
    if c is None:
        return
    try:
        await c.send_message(tg_chat_id, text)
    except Exception as e:  # noqa: BLE001
        log("warn", f"reply failed: {type(e).__name__}: {e}")


async def _send_approval(tg_chat_id: int, p: dict, c=None) -> None:
    from telethon import Button
    c = c or client
    if c is None:
        return
    aid = p.get("approval_id", "")
    title = p.get("title") or "Approval needed"
    cmd = (p.get("command") or "").strip()
    reason = (p.get("reason") or "").strip()
    body = f"⚠️ **{title}**\n"
    if cmd:
        body += f"`{cmd[:500]}`\n"
    if reason:
        body += f"_{reason[:300]}_"
    await c.send_message(
        tg_chat_id, body,
        buttons=[[Button("✅ Approve", data=f"ap:{aid}"),
                  Button("❌ Deny", data=f"an:{aid}")]],
    )


async def _send_question(tg_chat_id: int, p: dict, c=None) -> None:
    from telethon import Button
    c = c or client
    if c is None:
        return
    qid = p.get("question_id", "")
    q = p.get("question") or ""
    options = p.get("options") or []
    rec = p.get("recommended", 0)
    body = f"❓ {q}\n"
    buttons = []
    for i, opt in enumerate(options):
        label = ("⭐ " if i == rec else "") + opt[:60]
        buttons.append([Button(label, data=f"q:{qid}:{i}")])
    await c.send_message(tg_chat_id, body, buttons=buttons)


async def _on_callback(event) -> None:
    """Inline-button taps: approve/deny (ap:an:) and question choices (q:)."""
    # CallbackQuery has no from_users filter in Telethon 1.45 — check here
    if _owner_id is not None and getattr(event, "user_id", None) != _owner_id:
        return
    try:
        data = str(event.data or "")
        if data.startswith("ap:") or data.startswith("an:"):
            kind, aid = data[:2], data[3:]
            decision = "approved" if kind == "ap" else "denied"
            await _post(f"/api/approvals/{aid}", {"decision": decision})
            await event.message.edit(f"{'✅ Approved' if decision == 'approved' else '❌ Denied'}")
        elif data.startswith("q:"):
            _, qid, idx = data.split(":", 2)
            await _post(f"/api/questions/{qid}", {"choice": int(idx)})
            await event.message.edit(f"✅ Answered: option {int(idx) + 1}")
        else:
            await event.message.edit("(expired or unknown)")
    except Exception as e:  # noqa: BLE001
        try:
            await event.message.edit(f"⚠️ {type(e).__name__}: {e}")
        except Exception:  # noqa: BLE001
            pass


# ── BOT channel: the BotFather bot (owner-only) ─────────────────────

def _bot_owner(sender_id) -> bool:
    """The bot answers the boss only — everyone else gets the cold shoulder."""
    return _owner_id is not None and sender_id == _owner_id


async def _bot_on_message(event) -> None:
    if not _bot_owner(event.sender_id):
        return
    try:
        chat_id = event.chat_id
        text = (event.raw_text or "").strip()
        if text in ("/stop", "stop"):
            sid = _load_map().get(str(chat_id))
            if sid:
                tasks.cancel(sid)
                tasks.queues.pop(sid, None)
                try:
                    db.queue_clear(sid)
                except Exception:  # noqa: BLE001
                    pass
                await event.reply("⏹ stopped.")
            else:
                await event.reply("No muji chat mapped to this conversation.")
            return
        if text in ("/new", "new"):
            sid = _session_for(chat_id)
            db.rename_session(sid, _chat_title(chat_id))
            await event.reply("🆕 Fresh muji chat started.")
            return
        if text in ("/list", "list"):
            m = _load_map()
            lines = []
            for tg_id, s_id in m.items():
                s = db.get_session(s_id)
                if s:
                    lines.append(f"• {s.get('title') or '(untitled)'}  (tg:{tg_id})")
            await event.reply("Mapped muji chats:\n" + ("\n".join(lines) or "(none)"))
            return
        if text in ("/status", "status"):
            st = status()
            await event.reply(f"bridge: {'ok' if st['ok'] else 'off'} | "
                              f"connected: {st['connected']} | "
                              f"bot: {st['bot_connected']} | owner: {st['owner_id']}")
            return

        files: list[dict] = []
        if event.message.media is not None and _HAVE_TELETHON:
            files = await _download_media(event.message, text, c=bot_client)
        if not text and not files:
            return
        t = asyncio.create_task(_run_turn(chat_id, text, files, c=bot_client))
        _tasks.append(t)
        t.add_done_callback(_tasks.discard)
    except Exception as e:  # noqa: BLE001 — never kill the handler
        log("error", f"bot_on_message: {type(e).__name__}: {e}")


async def _bot_on_callback(event) -> None:
    if not _bot_owner(getattr(event, "user_id", None)):
        return
    try:
        data = str(event.data or "")
        if data.startswith("ap:") or data.startswith("an:"):
            kind, aid = data[:2], data[3:]
            decision = "approved" if kind == "ap" else "denied"
            await _post(f"/api/approvals/{aid}", {"decision": decision})
            await event.message.edit(f"{'✅ Approved' if decision == 'approved' else '❌ Denied'}")
        elif data.startswith("q:"):
            _, qid, idx = data.split(":", 2)
            await _post(f"/api/questions/{qid}", {"choice": int(idx)})
            await event.message.edit(f"✅ Answered: option {int(idx) + 1}")
        else:
            await event.message.edit("(expired or unknown)")
    except Exception as e:  # noqa: BLE001
        try:
            await event.message.edit(f"⚠️ {type(e).__name__}: {e}")
        except Exception:  # noqa: BLE001
            pass


# ── OUT: muji → telegram (the model's own tools) ────────────────────
# Read-only by design — the boss asked to READ his convos. The agent's
# tool loop runs these in a worker thread (tools.dispatch), but the
# Telethon client is bound to the server's event loop, so every call
# hops back onto that loop with run_coroutine_threadsafe.

def _is_conn_error(exc: BaseException) -> bool:
    """True when an exception looks like a stale/dead MTProto socket —
    the lid-close suspension class of failure: is_connected() still says
    True (it checks internal state, not liveness), but the next real RPC
    hangs until timeout."""
    if isinstance(exc, (concurrent.futures.TimeoutError, asyncio.TimeoutError,
                        TimeoutError)):
        return True
    name = type(exc).__name__.lower()
    return "connection" in name or "socket" in name or "timeout" in name


async def _reconnect_client(kind: str) -> bool:
    """Tear down and rebuild ONE Telethon client after a suspected dead
    socket. Event handlers live on the client object, not the session
    file, so they must be re-registered on the fresh client."""
    global client, bot_client, _user_loop, _bot_loop
    old = client if kind == "user" else bot_client
    if not _HAVE_TELETHON or old is None:
        return False
    try:
        await old.disconnect()
    except Exception:  # noqa: BLE001
        pass
    if kind == "user":
        c = _new_client()
        await c.connect()
        if not await c.is_user_authorized():
            log("warn", f"reconnect failed — {kind} session not authorized")
            return False
        c.add_event_handler(_on_message, events.NewMessage(from_users=_owner_id))
        c.add_event_handler(_on_callback, events.CallbackQuery())
        client = c
        _user_loop = asyncio.get_running_loop()
    else:
        c = TelegramClient(BOT_SESSION_PATH, API_ID, API_HASH,
                           system_version="muji 1.0", device_model="muji")
        await c.connect()
        if not await c.is_user_authorized():
            await c.sign_in(bot_token=BOT_TOKEN)
        c.add_event_handler(_bot_on_message, events.NewMessage(incoming=True))
        c.add_event_handler(_bot_on_callback, events.CallbackQuery())
        bot_client = c
        _bot_loop = asyncio.get_running_loop()
    log("info", f"{kind} client reconnected after stale socket (disconnect + connect)")
    return True


async def _force_reconnect() -> bool:
    """Rebuild EVERY live client after a suspected dead socket. The
    2026-09-25 guard only rebuilt the user-mode client — the bot channel
    (the boss's main inbound path) kept its event handlers on the dead
    socket, so messages to the bot were silently dropped after a
    lid-close suspension (2026-09-27: boss's TG message never arrived).
    Both sockets share the same MTProto fate, so a stale-socket finding
    on either one invalidates both."""
    ok = True
    for kind in ("user", "bot"):
        live = client if kind == "user" else bot_client
        if live is not None:
            ok = await _reconnect_client(kind) and ok
    return ok


def _client_loop_for(c):
    """The loop the client is bound to — captured at connect time.

    Telethon 1.45's `client.loop` is a property that resolves the CALLING
    thread's running loop (helpers.get_running_loop); from a tool worker
    thread (asyncio.to_thread) that is a fresh loop that never runs, so a
    coroutine scheduled on it hangs until timeout — every single time.
    The loop is captured where the client is connected instead. The
    `c._loop` attribute (set by Telethon's connect()) is the fallback for
    clients this module did not create (tests)."""
    if c is client:
        cap = _user_loop
    elif c is bot_client:
        cap = _bot_loop
    else:
        cap = None
    if cap is None:
        cap = getattr(c, "_loop", None)
    if cap is None or cap.is_closed():
        raise RuntimeError(
            "Telegram client loop unknown or closed — "
            "restart the server (sidebar ⟳)")
    return cap


def _run_on_loop(factory, timeout: float = 60.0, _retry: bool = True):
    """Run a coroutine on the Telethon client's loop from a worker thread.
    `factory` is a zero-arg callable returning a fresh coroutine — needed
    because a timed-out coroutine can't be re-run. On a suspected dead
    socket (the lid-close stale-socket class), force a reconnect and retry
    ONCE — is_connected() lies after suspension, so the first attempt
    hangs until timeout; a fresh client on the same session file reads in
    ~0.3s (measured 2026-09-25).

    Bounded (2026-09-27): the old version re-entered itself from its own
    timeout handler, and the reconnect it scheduled ran on the very loop
    that had gone quiet — when that loop never ran again (the dead-worker-
    loop class, see _client_loop_for) the recursion spun forever and the
    tool call, with the whole agent turn on it, hung until a restart.
    Now: attempt -> force reconnect (single 30s shot) -> retry -> a clear
    RuntimeError. A dead tg_read costs at most ~210s and comes back as a
    tool error the model can react to."""
    loop = _client_loop_for(_require_client())
    fut = asyncio.run_coroutine_threadsafe(factory(), loop)
    try:
        return fut.result(timeout=timeout)
    except (concurrent.futures.TimeoutError, asyncio.TimeoutError,
            TimeoutError) as e:
        try:
            fut.cancel()
        except Exception:  # noqa: BLE001
            pass
        if not _retry:
            raise RuntimeError(
                f"Telegram call still timed out after the forced reconnect "
                f"({type(e).__name__}) — the socket is dead; "
                "restart the server (sidebar ⟳)") from e
        log("warn", f"tool call hung ({type(e).__name__}) — "
                    "forcing reconnect + one retry")
        if not _run_on_loop(_force_reconnect, timeout=30, _retry=False):
            raise RuntimeError(
                "Telegram socket was stale and the reconnect failed — "
                "restart the server (sidebar ⟳)") from e
        loop = _client_loop_for(_require_client())
        fut = asyncio.run_coroutine_threadsafe(factory(), loop)
        try:
            return fut.result(timeout=timeout)
        except (concurrent.futures.TimeoutError, asyncio.TimeoutError,
                TimeoutError) as e2:
            try:
                fut.cancel()
            except Exception:  # noqa: BLE001
                pass
            log("warn", f"tool call hung again after reconnect "
                        f"({type(e2).__name__}) — giving up")
            raise RuntimeError(
                "Telegram socket is dead and the forced reconnect did not "
                "help — restart the server (sidebar ⟳)") from e2


def _require_client():
    if client is None or not client.is_connected():
        raise RuntimeError("Telegram is not connected (no session — run tools/tg_login.py)")
    return client


async def _a_resolve_chat(query: str):
    """Find a dialog by chat id or (case-insensitive) name substring."""
    c = _require_client()
    q = query.strip()
    if q.isdigit():
        try:
            from telethon.tl.functions.contacts import SearchRequest
            res = await c(SearchRequest(q, limit=1))
            if res.users:
                return res.users[0]
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"no chat found for id {q}")
    found = []
    async for d in c.iter_dialogs(limit=200):
        name = d.name or ""
        if q.lower() in name.lower():
            found.append(d)
        if len(found) >= 5:
            break
    if not found:
        raise RuntimeError(f"no chat matching '{q}' (checked recent dialogs)")
    if len(found) > 1:
        names = "; ".join((d.name or "?")[:40] for d in found[:5])
        raise RuntimeError(f"ambiguous '{q}' — matches: {names}")
    return found[0]


async def _a_tg_search(query: str, chat: str, limit: int) -> list[dict]:
    c = _require_client()
    limit = max(1, min(int(limit or 20), 100))
    if chat.strip():
        dialogs = [await _a_resolve_chat(chat)]
    else:
        dialogs = []
        async for d in c.iter_dialogs(limit=100):
            dialogs.append(d)
    out: list[dict] = []
    for d in dialogs:
        if len(out) >= limit:
            break
        try:
            async for m in c.iter_messages(d, limit=limit, search=query or None):
                if len(out) >= limit:
                    break
                text = (m.text or m.message or "").strip().replace("\n", " ")
                if not text:
                    continue
                out.append({
                    "chat": d.name or str(d.id),
                    "from": (getattr(m, "sender_name", None)
                             or ("me" if getattr(m, "out", False) else "them")),
                    "date": m.date.strftime("%Y-%m-%d %H:%M") if m.date else "",
                    "text": text[:300],
                })
        except Exception:  # noqa: BLE001 — a dead dialog must not kill the search
            continue
    return out


async def _a_tg_read(chat: str, limit: int) -> list[dict]:
    c = _require_client()
    limit = max(1, min(int(limit or 20), 100))
    d = await _a_resolve_chat(chat)
    out: list[dict] = []
    async for m in c.iter_messages(d, limit=limit):
        text = (m.text or m.message or "").strip().replace("\n", " ")
        kind = "media" if m.media is not None else ""
        out.append({
            "from": (getattr(m, "sender_name", None)
                     or ("me" if getattr(m, "out", False) else "them")),
            "date": m.date.strftime("%Y-%m-%d %H:%M") if m.date else "",
            "text": text[:500],
            **({"media": kind} if kind else {}),
        })
    return out


def t_tg_search(ctx, query: str = "", chat: str = "", limit: int = 20) -> str:
    """Search Telegram conversations (the boss's own account)."""
    out = _run_on_loop(lambda: _a_tg_search(query, chat, limit), timeout=90)
    return json.dumps(out, ensure_ascii=False, indent=1) if out else "(no messages found)"


def t_tg_read(ctx, chat: str, limit: int = 20) -> str:
    """Read recent messages from ONE Telegram conversation."""
    if not chat.strip():
        raise RuntimeError("chat is required")
    out = _run_on_loop(lambda: _a_tg_read(chat, limit), timeout=90)
    return json.dumps(out, ensure_ascii=False, indent=1) if out else "(empty chat)"
