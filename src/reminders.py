"""Reminder engine — the alarm clock Google Tasks never had.

The api.py poller calls `fire_due()` every 30 s: anything in the local
store whose due moment has arrived (and hasn't been notified) gets a
Windows toast. Toast delivery:

1. raw WinRT (Windows.UI.Notifications) — real Action Center toast
2. fallback: a `wscript` popup window (always works, no dependencies)

Delivery is fire-and-forget (subprocess, detached) so a hung PowerShell
can never stall the poller.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from . import taskstore

# Toast app-logo (the slot where Windows shows the little 📌 pin). 30×30 crop
# of icon-512, shipped in static/. Injected as a file:/// URI via the
# appLogoOverride toast property (Win10 1809+; this box is 25H2).
_ICON_PATH = Path(__file__).resolve().parent.parent / "static" / "toast_icon.png"

# Title/message are interpolated in (single-quoted, ''-escaped) — args after
# `-Command` do NOT bind to a param() block, so inlining is the reliable way.
# NOTE: use .replace() tokens, NOT str.format — the PS script contains literal
# `{` braces and .format() chokes on them (ValueError on every tick).
#
# WinRT via CreateToastNotifier — NOT the static [ToastNotificationManager]::Show,
# which does not exist in the PS 5.1 type-load surface (verified 2026-09-25:
# 'does not contain a method named Show'). BurntToast was tried first but its
# nupkg ships no .NET assemblies, so New-BurntToastNotification fails with
# 'Unable to find type [Microsoft.Toolkit.Uwp.Notifications.AdaptiveSubgroup]'
# on PS 5.1 — and $ErrorActionPreference='SilentlyContinue' + `exit 0` made the
# poller report a successful toast that never rendered (the 8:34/8:51 PM
# no-shows). This path was verified live: PATH2-OK.
_TOAST_PS = """
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$ErrorActionPreference = 'Stop'
$esc = { param($s) $s.Replace('&','&amp;').Replace('<','&lt;').Replace('>','&gt;').Replace('"','&quot;') }
$xml = '<toast><visual><binding template="ToastText02"><text id="1">' + (& $esc '__TITLE__') + '</text><text id="2">' + (& $esc '__MESSAGE__') + '</text><image placement="appLogo" src="__ICON_URI__"/></binding></visual></toast>'
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
$t = New-Object Windows.UI.Notifications.ToastNotification $doc
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('muji').Show($t)
exit 0
"""

_POPUP_VBS_TEMPLATE = (
    "MsgBox {msg}, vbInformation, {title}"
)


def _ps_escape(s: str) -> str:
    return s.replace("'", "''")


def _vbs_escape(s: str) -> str:
    # wscript reads .vbs as ANSI (CP1252 on this box) — UTF-8 emoji bytes
    # get mangled into garbage ("📌" → "dY\"O"). Strip non-ASCII for the
    # popup path; BurntToast (the real toast) keeps full UTF-8.
    s = s.encode("ascii", "ignore").decode("ascii")
    return s.replace('"', '""')


def os_name_nt() -> bool:
    import os
    return os.name == "nt"


def send_toast(title: str, message: str) -> str:
    """Show one notification. Returns 'toast' or 'popup' (what was used)."""
    if os_name_nt():
        # try BurntToast first
        try:
            icon_uri = _ICON_PATH.as_uri() if _ICON_PATH.exists() else ""
            ps = (_TOAST_PS
                  .replace("__TITLE__", _ps_escape(title))
                  .replace("__MESSAGE__", _ps_escape(message))
                  .replace("__ICON_URI__", icon_uri))
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-Command", ps],
                capture_output=True, timeout=25)
            if proc.returncode == 0:
                return "toast"
        except (subprocess.SubprocessError, OSError):
            pass
        # fallback: wscript popup (always available on Windows)
        _popup_fallback(title, message)
        return "popup"
    # non-Windows: just log (no toast concept); still counts as notified
    print(f"[reminders] {title} — {message}", flush=True)
    return "logged"


def _popup_fallback(title: str, message: str) -> None:
    import tempfile
    import pathlib
    vbs = _POPUP_VBS_TEMPLATE.format(
        msg="\"" + _vbs_escape(message) + "\"",
        title="\"" + _vbs_escape(title) + "\"")
    p = pathlib.Path(tempfile.gettempdir()) / "muji_remind.vbs"
    try:
        p.write_text(vbs, encoding="utf-8")
        # NO //B flag: wscript.exe with //B exits 0 IMMEDIATELY without
        # showing the MsgBox (verified 2026-09-25 — cscript without //B
        # blocks on the dialog, wscript //B returns in <1 s, no window).
        # The popup silently never appeared, so every fallback reminder
        # was a no-op. Plain wscript shows the box and stays alive until OK.
        subprocess.Popen(["wscript", str(p)],
                         creationflags=0x00000008)  # DETACHED_PROCESS
    except OSError as e:
        print(f"[reminders] popup failed: {e}", flush=True)


def fire_due(now: float | None = None) -> list[dict]:
    """One poller tick: notify everything due. Returns what fired."""
    fired = []
    for item in taskstore.due_items(now):
        kind = item["kind"]
        when = item.get("due_iso") or item.get("start_iso") or ""
        if kind == "task":
            title = "📌 Task due"
            msg = f"{item['title']}" + (f"  (due {when})" if when else "")
        else:
            title = "📅 Event now"
            msg = f"{item['title']}  ({when})"
        used = send_toast(title, msg)
        taskstore.mark_notified(kind, item["id"])
        fired.append({"id": item["id"], "kind": kind, "title": item["title"],
                      "via": used})
    return fired
