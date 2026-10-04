"""Entry point:  python server.py  →  http://127.0.0.1:<port from .env>

Port collision policy: if the configured port is taken by something that
isn't our own running server, we pick the next free one and record it in
server.port so start.bat (and the user) know where we actually are.
"""
import os
import socket
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import uvicorn

from src import config
from src.config import settings


def _port_ours(port: int) -> bool:
    """True if the process on `port` is THIS checkout's running server.

    Probes /api/health and compares the boot_token the server reports
    against the one this process wrote to server.boot. A bare 200 would
    be ambiguous when another muji checkout (or any HTTP service) holds
    the port; the token is per-boot random, so only our own process can
    match it."""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{settings.host}:{port}/api/health", timeout=1.5) as r:
            if r.status != 200:
                return False
            body = json.loads(r.read().decode("utf-8", "replace"))
        return bool(body.get("boot_token")) and body["boot_token"] == _boot_token()
    except Exception:
        return False


def _boot_token() -> str:
    try:
        return config.APP_ROOT.joinpath("server.boot").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _pick_port(want: int) -> int:
    """Return `want` if free, otherwise the next free port.

    If `want` is held by our own server, keep waiting for it to release
    (self-restart overlap) instead of stealing the neighbor's port."""
    host = settings.host
    if _port_ours(want):
        _wait_port_free(want)
        return want
    port = want
    while True:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((host, port))
            return port
        except OSError:
            if _port_ours(port):
                _wait_port_free(port)
                return port
            port += 1
        finally:
            s.close()


def _wait_port_free(port: int, host: str = "127.0.0.1", tries: int = 150, delay: float = 0.1) -> None:
    """Before binding, wait for any previous instance to release the port.

    A normal cold start finds the port free and returns immediately. A
    self-restart (POST /api/restart) overlaps the dying old process, so we
    poll until a connection is refused — then uvicorn can bind without
    "address already in use"."""
    deadline = time.time() + tries * delay
    while time.time() < deadline:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.4)
        try:
            s.connect((host, port))
            s.close()
            time.sleep(delay)  # still listening — the old server hasn't exited yet
        except OSError:
            s.close()
            return  # connection refused → the port is free
    raise SystemExit(f"port {port} still busy after {tries * delay:.0f}s — aborting")


if __name__ == "__main__":
    import uuid as _uuid
    os.environ["MUJI_BOOT_TOKEN"] = _uuid.uuid4().hex
    port = _pick_port(settings.port)
    if port != settings.port:
        print(f"  port {settings.port} is taken by another program — using {port} instead")
    # Record where we actually are so start.bat/launch.py (and the user)
    # can find us even when we had to move off the configured port.
    pfile = config.APP_ROOT.joinpath("server.port")
    pfile.write_text(str(port), encoding="utf-8")
    # And the per-boot identity token that "is the port ours?" probes are
    # compared against (served by /api/health via MUJI_BOOT_TOKEN).
    bfile = config.APP_ROOT.joinpath("server.boot")
    bfile.write_text(os.environ.get("MUJI_BOOT_TOKEN", ""), encoding="utf-8")
    try:
        import atexit
        atexit.register(pfile.unlink, missing_ok=True)  # don't leave stale files behind
        atexit.register(bfile.unlink, missing_ok=True)
    except Exception:
        pass
    print(f"  {settings.title}  →  http://{settings.host}:{port}")
    print(f"  model: {settings.model or '(not set — edit .env)'}")
    print(f"  root:  {settings.root_dir}")
    uvicorn.run("src.api:app", host=settings.host, port=port, log_level="info")
