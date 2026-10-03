"""Gmail API (readonly) — search + read mail. Retires the old IMAP pipe.

Pure httpx implementation — zero new dependencies beyond what muji already has.
Same OAuth client as the Tasks integration; scope is `gmail.readonly`.

OAuth 2.0 flow:
  1. GET /api/gmail/auth → returns Google consent URL
  2. Boss authorizes in browser → Google redirects to /api/gmail/callback?code=...
  3. Server exchanges code → stores tokens in data/gmail_tokens.json
  4. Subsequent API calls auto-refresh the access_token (1-hour expiry)

Query language (Gmail's, not IMAP's): from:, to:, subject:, is:unread,
label:, after:2024-01-01, before:2024-06-01 — plain words work too.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.parse

import httpx

from .config import settings

SCOPES = "https://www.googleapis.com/auth/gmail.readonly"
API_BASE = "https://gmail.googleapis.com/gmail/v1"
TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/auth"
TOKEN_FILE = settings.data_dir / "gmail_tokens.json"

MAX_BODY_CHARS = 20_000  # cap per-message body so results stay readable


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
    return f"http://127.0.0.1:{settings.port}/api/gmail/callback"


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
        raise Exception("gmail not connected — open /api/gmail/auth in the browser first")
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
        raise Exception("gmail not connected — open /api/gmail/auth in the browser first")
    tokens = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    if tokens.get("expiry", 0) > time.time() + 60:
        return tokens["access_token"]
    return _refresh()


def _request(method: str, path: str, params: dict | None = None) -> dict:
    token = _valid_token()
    r = httpx.request(method, f"{API_BASE}{path}", params=params,
                      headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if r.status_code != 200:
        raise Exception(f"gmail API {r.status_code}: {r.text[:300]}")
    return r.json()


# ── Gmail API ────────────────────────────────────────────────────────

def _b64url(s: str) -> str:
    """Decode a base64url-encoded header/body value (Gmail's format).

    Defensive: Gmail's list endpoint can hand back *truncated* header values
    (with a trailing '…' ellipsis), which are not valid base64 — those decode
    to '' rather than crashing the whole search.
    """
    if not s:
        return ""
    s = s.strip().rstrip("…").rstrip("...")
    if not s:
        return ""
    s = re.sub(r"[^A-Za-z0-9_\-]", "", s)
    if not s:
        return ""
    try:
        pad = "=" * (-len(s) % 4)
        return base64.urlsafe_b64decode(s + pad).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _decode_value(s: str) -> str:
    """Gmail header values come back as PLAIN TEXT — not base64 (verified
    live: raw 'jrlagana702@gmail.com', 'by 2002:ac8:...' etc.). Only body
    data is base64url. Old code base64-decoded everything → garbage.
    Fallback: if it decodes cleanly as base64url to printable text, use that;
    otherwise return the raw string as-is."""
    if not s:
        return ""
    s = s.strip().rstrip("…").rstrip("...").strip()
    if re.fullmatch(r"[A-Za-z0-9_\-]+={0,2}", s) and len(s) >= 8:
        try:
            pad = "=" * (-len(s) % 4)
            dec = base64.urlsafe_b64decode(s + pad).decode("utf-8")
            if dec and all(c.isprintable() or c in "\n\r\t" for c in dec):
                return dec
        except Exception:
            pass
    return s


def _headers(payload: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in payload.get("headers", []):
        out[h["name"].lower()] = _decode_value(h.get("value", ""))
    return out


def _strip_html(html: str) -> str:
    """Crude HTML→text: drop scripts/styles, tags, collapse whitespace."""
    html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _extract_body(payload: dict) -> tuple[str, list[dict]]:
    """Walk a (possibly multipart) payload → (body text, attachments)."""
    body: str | None = None
    html: str | None = None
    attachments: list[dict] = []

    def walk(p: dict):
        nonlocal body, html
        mime = p.get("mimeType", "")
        if p.get("filename"):
            attachments.append({
                "filename": p["filename"],
                "mimeType": mime,
                "size": int(p.get("body", {}).get("size", 0) or 0),
            })
        if mime == "text/plain" and body is None:
            body = _decode_value(p.get("body", {}).get("data", ""))
        elif mime == "text/html" and html is None:
            html = _decode_value(p.get("body", {}).get("data", ""))
        for part in p.get("parts", []):
            walk(part)

    walk(payload)
    text = body if body is not None else (
        _strip_html(html) if html is not None else "(no readable body)")
    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS] + f"\n[truncated — {len(text):,} chars total]"
    return text, attachments


def search_messages(query: str = "", max_results: int = 10) -> list[dict]:
    """Search mail. Empty query = most recent. Returns summaries (newest first).

    The list endpoint returns ids only (no headers/snippet/labels), so each
    message is fetched individually with format=full — up to `max_results`
    extra GETs, which is fine for a personal agent.
    """
    params: dict = {"maxResults": max_results}
    if query:
        params["q"] = query
    data = _request("GET", "/users/me/messages", params)
    out = []
    for m in data.get("messages", []):
        try:
            full = _request("GET", f"/users/me/messages/{m['id']}", {"format": "full"})
        except Exception:
            continue  # one bad message shouldn't blank the whole search
        h = _headers(full.get("payload", {}))
        out.append({
            "id": m["id"],
            "threadId": m.get("threadId", ""),
            "from": h.get("from", ""),
            "to": h.get("to", ""),
            "subject": h.get("subject", "(no subject)"),
            "date": h.get("date", ""),
            "snippet": full.get("snippet", "")[:200],
            "unread": "UNREAD" in full.get("labelIds", []),
        })
    return out


def read_message(id: str) -> dict:
    """Read one message in full (headers + body + attachment list)."""
    data = _request("GET", f"/users/me/messages/{id}", {"format": "full"})
    h = _headers(data.get("payload", {}))
    body, attachments = _extract_body(data.get("payload", {}))
    return {
        "id": data["id"],
        "threadId": data.get("threadId", ""),
        "from": h.get("from", ""),
        "to": h.get("to", ""),
        "cc": h.get("cc", ""),
        "subject": h.get("subject", "(no subject)"),
        "date": h.get("date", ""),
        "body": body,
        "attachments": attachments,
    }
