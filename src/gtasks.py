"""Google Tasks API — add + list tasks (default task list).

Pure httpx implementation — zero new dependencies beyond what muji already has.
Reuses the same OAuth client as the old calendar integration; scope is `tasks`.

OAuth 2.0 flow:
  1. GET /api/tasks/auth → returns Google consent URL
  2. Boss authorizes in browser → Google redirects to /api/tasks/callback?code=...
  3. Server exchanges code → stores tokens in data/tasks_tokens.json
  4. Subsequent API calls auto-refresh the access_token (1-hour expiry)

Config (.env):
  GOOGLE_CLIENT_ID=xxx.apps.googleusercontent.com
  GOOGLE_CLIENT_SECRET=yyy
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse

import httpx

from .config import settings

SCOPES = "https://www.googleapis.com/auth/tasks"
API_BASE = "https://tasks.googleapis.com/tasks/v1"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/auth"
TOKEN_FILE = settings.data_dir / "tasks_tokens.json"


def _client_id() -> str:
    v = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    if not v:
        raise Exception("GOOGLE_CLIENT_ID not set in .env")
    return v


def _client_secret() -> str:
    v = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    if not v:
        raise Exception("GOOGLE_CLIENT_SECRET not set in .env")
    return v


def _redirect_uri() -> str:
    return f"http://127.0.0.1:{settings.port}/api/tasks/callback"


# ── OAuth ────────────────────────────────────────────────────────────

def get_auth_url() -> str:
    """Build the Google OAuth consent URL for the Boss to open in a browser."""
    params = {
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(code: str) -> dict:
    """Exchange an authorization code for tokens. Persists to TOKEN_FILE."""
    r = httpx.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": code,
        "client_id": _client_id(),
        "client_secret": _client_secret(),
        "redirect_uri": _redirect_uri(),
    }, timeout=15)
    if r.status_code != 200:
        raise Exception(f"token exchange failed ({r.status_code}): {r.text[:300]}")
    data = r.json()
    tokens = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
        "expiry": time.time() + data.get("expires_in", 3600),
    }
    TOKEN_FILE.write_text(json.dumps(tokens, indent=1), encoding="utf-8")
    return tokens


def _refresh() -> str:
    """Refresh the access_token using the stored refresh_token."""
    if not TOKEN_FILE.exists():
        raise Exception("tasks not connected — open /api/tasks/auth in the browser first")
    tokens = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    rt = tokens.get("refresh_token", "")
    if not rt:
        raise Exception("no refresh_token stored — re-run the auth flow")
    r = httpx.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "client_id": _client_id(),
        "client_secret": _client_secret(),
    }, timeout=15)
    if r.status_code != 200:
        raise Exception(f"token refresh failed ({r.status_code}): {r.text[:300]}")
    data = r.json()
    tokens["access_token"] = data["access_token"]
    tokens["expiry"] = time.time() + data.get("expires_in", 3600)
    TOKEN_FILE.write_text(json.dumps(tokens, indent=1), encoding="utf-8")
    return data["access_token"]


def _valid_token() -> str:
    """Get a valid access_token, auto-refreshing if within 60s of expiry."""
    if not TOKEN_FILE.exists():
        raise Exception("tasks not connected — open /api/tasks/auth in the browser first")
    tokens = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    if tokens.get("expiry", 0) > time.time() + 60:
        return tokens["access_token"]
    return _refresh()


# ── Tasks API ────────────────────────────────────────────────────────

def _request(method: str, path: str, body: dict | None = None) -> dict:
    token = _valid_token()
    r = httpx.request(method, f"{API_BASE}{path}", json=body,
                      headers={"Authorization": f"Bearer {token}"}, timeout=15)
    if r.status_code not in (200, 201):
        raise Exception(f"tasks API {r.status_code}: {r.text[:300]}")
    return r.json()


def _default_list_id() -> str:
    """Resolve the ID of the default task list (e.g. 'abc123', not '@me')."""
    data = _request("GET", "/users/@me/lists")
    for lst in data.get("items", []):
        if lst.get("id") == "@default" or lst.get("defaultList"):
            return lst["id"]
    # Fallback: first list (accounts with exactly one list is the common case)
    items = data.get("items", [])
    if items:
        return items[0]["id"]
    raise Exception("no task list found on the account")


def list_tasks(max_results: int = 50) -> list[dict]:
    """List open tasks in the default task list."""
    lid = _default_list_id()
    token = _valid_token()
    r = httpx.get(f"{API_BASE}/lists/{lid}/tasks",
                  params={"maxResults": max_results, "completed": False},
                  headers={"Authorization": f"Bearer {token}"}, timeout=15)
    if r.status_code != 200:
        raise Exception(f"tasks API {r.status_code}: {r.text[:300]}")
    data = r.json()
    out = []
    for t in data.get("items", []):
        out.append({
            "id": t["id"],
            "title": t.get("title", "(no title)"),
            "due": t.get("due", ""),
            "status": t.get("status", "needsAction"),
        })
    return out


def add_task(title: str, due: str = "") -> dict:
    """Add a task to the default task list. due: ISO 8601 (optional)."""
    lid = _default_list_id()
    body: dict = {"title": title}
    if due:
        body["due"] = due
    data = _request("POST", f"/lists/{lid}/tasks", body)
    return {
        "id": data.get("id", ""),
        "title": data.get("title", title),
        "due": data.get("due", ""),
        "status": data.get("status", "needsAction"),
    }
