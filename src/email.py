"""IMAP email access — read-only by default (list, read, search).

Accounts are configured in .env as:
  EMAIL_1_LABEL=gmail
  EMAIL_1_ADDRESS=jrlagana702@gmail.com
  EMAIL_1_PASSWORD=xxxx xxxx xxxx xxxx
  EMAIL_2_LABEL=edu
  EMAIL_2_ADDRESS=jan@up.edu.ph
  EMAIL_2_PASSWORD=xxxx xxxx xxxx xxxx
  ...

IMAP host is auto-detected from the email domain:
  gmail.com → imap.gmail.com:993
  up.edu.ph → mail.up.edu.ph:993 (or whatever works)
  yahoo.com → imap.mail.yahoo.com:993
  outlook.com / hotmail.com → outlook.office365.com:993
  default → imap.<domain>:993
"""
from __future__ import annotations

import email
import email.header
import email.utils
import imaplib
import os
import re
from dataclasses import dataclass
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path

# ── account config ───────────────────────────────────────────────────

IMAP_HOSTS = {
    "gmail.com": ("imap.gmail.com", 993),
    "googlemail.com": ("imap.gmail.com", 993),
    "yahoo.com": ("imap.mail.yahoo.com", 993),
    "outlook.com": ("outlook.office365.com", 993),
    "hotmail.com": ("outlook.office365.com", 993),
    "live.com": ("outlook.office365.com", 993),
    "up.edu.ph": ("mail.up.edu.ph", 993),
    "dlsu.edu.ph": ("mail.dlsu.edu.ph", 993),
    "adamson.edu.ph": ("mail.adamson.edu.ph", 993),
}


@dataclass
class EmailAccount:
    label: str
    address: str
    password: str
    host: str
    port: int

    @classmethod
    def load_all(cls) -> list["EmailAccount"]:
        """Load all EMAIL_N_* accounts from environment."""
        accounts = []
        i = 1
        while True:
            label = os.environ.get(f"EMAIL_{i}_LABEL", "").strip()
            address = os.environ.get(f"EMAIL_{i}_ADDRESS", "").strip()
            password = os.environ.get(f"EMAIL_{i}_PASSWORD", "").strip()
            if not (label and address and password):
                break
            domain = address.split("@")[-1].lower()
            host, port = IMAP_HOSTS.get(domain, (f"imap.{domain}", 993))
            accounts.append(cls(label, address, password, host, port))
            i += 1
        return accounts


def get_account(label: str) -> EmailAccount:
    accounts = EmailAccount.load_all()
    for a in accounts:
        if a.label.lower() == label.lower():
            return a
    available = ", ".join(a.label for a in accounts) or "(none configured)"
    raise ValueError(f"unknown account '{label}'. available: {available}")


# ── IMAP connection ──────────────────────────────────────────────────

def _connect(account: EmailAccount) -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(account.host, account.port)
    conn.login(account.address, account.password)
    return conn


def _parse_msg(msg: Message) -> dict:
    """Extract useful fields from a parsed email Message."""
    # decode_header returns a list of (bytes|str, charset) parts — join them
    raw_subject = msg.get("Subject", "")
    subject = "<no subject>"
    if raw_subject:
        decoded_parts = email.header.decode_header(raw_subject)
        subject = "".join(
            part.decode(charset or "utf-8", errors="replace") if isinstance(part, bytes) else part
            for part, charset in decoded_parts
        )

    from_raw = msg.get("From", "")
    from_decoded_parts = email.header.decode_header(from_raw)
    from_addr = "".join(
        part.decode(charset or "utf-8", errors="replace") if isinstance(part, bytes) else part
        for part, charset in from_decoded_parts
    )

    to_raw = msg.get("To", "")
    to_decoded_parts = email.header.decode_header(to_raw)
    to_addr = "".join(
        part.decode(charset or "utf-8", errors="replace") if isinstance(part, bytes) else part
        for part, charset in to_decoded_parts
    )

    date = msg.get("Date", "")
    body = _extract_body(msg)

    return {
        "subject": subject[:200],
        "from": from_addr[:200],
        "to": to_addr[:200],
        "date": date,
        "body": body,
    }


class _HTMLToText(HTMLParser):
    SKIP = {"script", "style", "noscript", "head", "svg", "template", "iframe"}
    BLOCK = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "br",
             "section", "article", "header", "footer", "table", "ul", "ol",
             "pre", "blockquote", "dd", "dt", "main", "figure"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK and not self._skip:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK and not self._skip:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data)

    @property
    def text(self) -> str:
        t = "".join(self.parts)
        t = re.sub(r"[ \t]+", " ", t)
        t = re.sub(r" ?\n ?", "\n", t)
        return t.strip()


def _extract_body(msg: Message, max_chars: int = 20_000) -> str:
    """Get the text body (prefer plain text, fallback to HTML→text)."""
    text_body = ""
    html_body = ""

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in disposition:
                continue
            if ctype == "text/plain" and not text_body:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    text_body = payload.decode(charset, errors="replace")
            elif ctype == "text/html" and not html_body:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    html_body = payload.decode(charset, errors="replace")
    else:
        ctype = msg.get_content_type()
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            content = payload.decode(charset, errors="replace")
            if ctype == "text/html":
                html_body = content
            else:
                text_body = content

    if text_body:
        return text_body[:max_chars]
    if html_body:
        parser = _HTMLToText()
        try:
            parser.feed(html_body)
            return parser.text[:max_chars]
        except Exception:
            return html_body[:max_chars]
    return "(no body)"


# ── public API (called by tool) ─────────────────────────────────────

def list_emails(account_label: str, folder: str = "INBOX", limit: int = 10) -> list[dict]:
    """List recent emails (newest first). Returns id, subject, from, date."""
    account = get_account(account_label)
    conn = _connect(account)
    try:
        conn.select(folder, readonly=True)
        # Search for all (UNSEEN first for convenience, but list all)
        status, data = conn.search(None, "ALL")
        if status != "OK":
            raise RuntimeError(f"IMAP search failed: {status}")
        ids = data[0].split()
        if not ids:
            return []
        # Take the last N (newest)
        ids = ids[-limit:][::-1]  # reverse so newest first

        results = []
        for msg_id in ids:
            status, msg_data = conn.fetch(msg_id, "(RFC822.HEADER)")
            if status != "OK" or not msg_data or msg_data[0] is None:
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            results.append({
                "id": msg_id.decode(),
                "subject": _safe_header(msg.get("Subject", "")),
                "from": _safe_header(msg.get("From", "")),
                "date": msg.get("Date", ""),
            })
        return results
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def read_email(account_label: str, msg_id: str, folder: str = "INBOX") -> dict:
    """Read full email by ID."""
    account = get_account(account_label)
    conn = _connect(account)
    try:
        conn.select(folder, readonly=True)
        status, msg_data = conn.fetch(msg_id, "(RFC822)")
        if status != "OK" or not msg_data or msg_data[0] is None:
            raise RuntimeError(f"could not fetch message {msg_id}")
        msg = email.message_from_bytes(msg_data[0][1])
        return {"id": msg_id, **_parse_msg(msg)}
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def search_emails(account_label: str, query: str, folder: str = "INBOX", limit: int = 10) -> list[dict]:
    """Search emails by IMAP query (e.g. 'FROM \"boss\"', 'SUBJECT \"report\"')."""
    account = get_account(account_label)
    conn = _connect(account)
    try:
        conn.select(folder, readonly=True)
        status, data = conn.search(None, query)
        if status != "OK":
            raise RuntimeError(f"IMAP search failed: {status}")
        ids = data[0].split()
        if not ids:
            return []
        ids = ids[-limit:][::-1]

        results = []
        for msg_id in ids:
            status, msg_data = conn.fetch(msg_id, "(RFC822.HEADER)")
            if status != "OK" or not msg_data or msg_data[0] is None:
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            results.append({
                "id": msg_id.decode(),
                "subject": _safe_header(msg.get("Subject", "")),
                "from": _safe_header(msg.get("From", "")),
                "date": msg.get("Date", ""),
            })
        return results
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def list_folders(account_label: str) -> list[str]:
    """List available mail folders."""
    account = get_account(account_label)
    conn = _connect(account)
    try:
        status, data = conn.list()
        if status != "OK":
            return []
        folders = []
        for line in data:
            if isinstance(line, bytes):
                line = line.decode()
            # Format: (\HasNoChildren) "/" "INBOX"
            m = re.search(r'"([^"]*)"\s*"?([^"]+)"?$', line)
            if m:
                folders.append(m.group(2))
            else:
                # Fallback: last quoted string
                parts = line.rsplit('"', 3)
                if len(parts) >= 2:
                    folders.append(parts[1])
        return folders
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _safe_header(raw: str) -> str:
    """Decode a potentially encoded email header."""
    if not raw:
        return ""
    try:
        decoded_parts = email.header.decode_header(raw)
        return "".join(
            part.decode(charset or "utf-8", errors="replace") if isinstance(part, bytes) else part
            for part, charset in decoded_parts
        )
    except Exception:
        return raw
