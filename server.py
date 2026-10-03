"""Entry point:  python server.py  →  http://127.0.0.1:8321"""
import socket
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import uvicorn

from src.config import settings


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
    print(f"  {settings.title}  →  http://{settings.host}:{settings.port}")
    print(f"  model: {settings.model or '(not set — edit .env)'}")
    print(f"  root:  {settings.root_dir}")
    _wait_port_free(settings.port)
    uvicorn.run("src.api:app", host=settings.host, port=settings.port, log_level="info")
