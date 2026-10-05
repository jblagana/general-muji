"""Preview server: serves the clone's static/ + read-only /api/ stubs backed
by the clone's own muji.db, so the REAL app.js boots and renders a chat.
NOT the muji server — no agent, no SSE, no writes.
Usage: python tools/preview_server.py 8324"""
import json
import os
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "static")
DB = os.path.join(ROOT, "data", "muji.db")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8324


def j(o):
    return json.dumps(o).encode()


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        if p == "/api/config":
            return self._send(200, j({"title": "muji", "brand": "muji",
                                      "model": "preview", "root_dir": ROOT,
                                      "fact_check": False, "auth_enabled": False,
                                      "tz_offset": 8,
                                      "compact_trigger": 220000,
                                      "compact_enabled": True}))
        if p == "/api/setup/status":
            return self._send(200, j({"configured": True}))
        if p == "/api/sessions":
            try:
                con = db()
                cols = ("id, workspace_id, title, created_at, updated_at, "
                        "processing, mode, cwd, roast, last_done_at, "
                        "last_viewed_at, summary, pinned, tldr_flags")
                try:
                    rows = con.execute(
                        f"SELECT {cols}, ctx_tokens FROM sessions "
                        "ORDER BY updated_at DESC").fetchall()
                except sqlite3.OperationalError:  # pre-meter DB: no column
                    rows = con.execute(
                        f"SELECT {cols} FROM sessions "
                        "ORDER BY updated_at DESC").fetchall()
                con.close()
                out = []
                for r in rows:
                    d = dict(r)
                    d.setdefault("ctx_tokens", None)
                    d.update({"status": "running" if d.get("processing") else "idle",
                              "waiting_approval": False, "approval_deadline": None,
                              "approval_total": 0, "waiting_question": False,
                              "question_deadline": None, "question_total": 0,
                              "needs_continue": False, "queued": 0})
                    out.append(d)
                return self._send(200, j({"sessions": out}))
            except Exception as e:
                return self._send(200, j({"sessions": [], "error": str(e)}))
        if p == "/api/history":
            sid = u.query.split("session_id=")[1].split("&")[0] if "session_id=" in u.query else ""
            try:
                con = db()
                rows = con.execute(
                    "SELECT id, role, content, parts, thinking, files, created_at, mode, answer_ts "
                    "FROM messages WHERE session_id=? ORDER BY created_at", (sid,)).fetchall()
                con.close()
                out = []
                for r in rows:
                    d = dict(r)
                    for k in ("parts", "files"):
                        try:
                            d[k] = json.loads(d[k]) if d[k] else []
                        except Exception:
                            d[k] = []
                    try:
                        d["thinking"] = json.loads(d["thinking"]) if d["thinking"] else None
                    except Exception:
                        d["thinking"] = None
                    out.append(d)
                return self._send(200, j({"messages": out, "hidden_count": 0}))
            except Exception as e:
                return self._send(200, j({"messages": [], "hidden_count": 0,
                                          "error": str(e)}))
        if p.startswith("/api/sessions/") and p.endswith("/tool_log"):
            sid = p.split("/")[3]
            # the clone db has no tool_log table — rebuild from message parts
            try:
                con = db()
                rows = con.execute(
                    "SELECT parts FROM messages WHERE session_id=? "
                    "AND parts IS NOT NULL AND parts != '' ORDER BY created_at",
                    (sid,)).fetchall()
                con.close()
                evs = []
                n = 0
                for r in rows:
                    try:
                        parts = json.loads(r["parts"]) or []
                    except Exception:
                        continue
                    for pt in parts:
                        if pt.get("t") == "tool":
                            n += 1
                            evs.append({"id": n, "data": {
                                "tool": pt.get("tool"), "ok": pt.get("ok", True),
                                "ms": pt.get("ms"), "args": pt.get("args") or "",
                                "output": pt.get("detail") or "",
                            }})
                return self._send(200, j({"events": evs}))
            except Exception as e:
                return self._send(200, j({"events": [], "error": str(e)}))
        if p == "/api/tasks":
            try:
                con = db()
                rows = con.execute(
                    "SELECT id, title, note, due_iso, done FROM tasks "
                    "ORDER BY done, due_iso").fetchall()
                con.close()
                return self._send(200, j({"tasks": [dict(r) for r in rows]}))
            except Exception as e:
                return self._send(200, j({"tasks": [], "error": str(e)}))
        if p == "/api/events":
            try:
                con = db()
                rows = con.execute(
                    "SELECT id, title, note, start_iso, done FROM calendar "
                    "ORDER BY start_iso").fetchall()
                con.close()
                return self._send(200, j({"events": [dict(r) for r in rows]}))
            except Exception as e:
                return self._send(200, j({"events": [], "error": str(e)}))
        if p == "/api/workspaces":
            return self._send(200, j({"workspaces": [], "selected": None}))
        if p == "/api/auto_approve":
            return self._send(200, j({"categories": {"destructive": True, "auto_resume": False}}))
        if p in ("/api/learned", "/api/tool_notes"):
            return self._send(200, j({"active": [], "pending": []}))
        if p.startswith("/api/"):
            return self._send(200, j({}))
        # static — /static/foo → STATIC/foo (the /static prefix is the mount)
        if p == "/":
            p = "/index.html"
        if p.startswith("/static/"):
            p = p[len("/static"):]
        fp = os.path.normpath(os.path.join(STATIC, p.lstrip("/")))
        if fp.startswith(STATIC) and os.path.isfile(fp):
            ctype = "text/html" if fp.endswith(".html") else (
                "text/css" if fp.endswith(".css") else (
                    "application/javascript" if fp.endswith(".js") else
                    "image/png" if fp.endswith(".png") else
                    "image/x-icon" if fp.endswith(".ico") else
                    "application/json" if fp.endswith(".json") else
                    "application/octet-stream"))
            return self._send(200, open(fp, "rb").read(), ctype)
        return self._send(404, j({"error": "not found"}))

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        if n:
            self.rfile.read(n)
        return self._send(200, j({"ok": True}))


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
