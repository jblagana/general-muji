"""muji launcher (called by start.bat).

Owns the "is the server up / which port" decision so start.bat stays a
thin shell script:

  1. If OUR server already answers /api/health on the configured port,
     open the browser and exit (a foreign program on that port is NOT
     treated as us — that was the muji2.0 bug).
  2. Otherwise start server.py as a child in the SAME console (closing
     this window stops it, as before) and wait for it to come up.
  3. server.py itself picks a free port when the configured one is taken
     and records it in server.port — we read that file for the browser
     URL, so the tab always lands on the port we actually bound.
"""
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from src.config import settings  # noqa: E402

PORT_FILE = "server.port"


def _port_ours(port: int) -> bool:
    """True if THIS checkout's server answers /api/health on `port`.

    Compares the response's boot_token against the server.boot file the
    running instance wrote at startup — a bare 200 would be ambiguous
    when another muji checkout (or any HTTP service) holds the port."""
    try:
        expected = open(os.path.join(ROOT, "server.boot"), encoding="utf-8").read().strip()
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/health", timeout=1.5
        ) as r:
            import json
            body = json.loads(r.read().decode("utf-8", "replace"))
        return bool(expected) and body.get("boot_token") == expected
    except Exception:
        return False


def _real_port() -> int:
    """Port server.py actually bound (server.port), else the configured one."""
    try:
        with open(PORT_FILE, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return settings.port


def main() -> int:
    if _port_ours(settings.port):
        print(f"Server is already running - opening the browser.")
        webbrowser.open(f"http://127.0.0.1:{settings.port}")
        time.sleep(2)
        return 0

    try:
        os.remove(PORT_FILE)  # drop any stale record from a crashed run
    except OSError:
        pass
    # Per-boot identity: server.py serves this token via /api/health and
    # records its own port in server.port; _port_ours() matches the token.
    import uuid
    os.environ["MUJI_BOOT_TOKEN"] = uuid.uuid4().hex
    with open(os.path.join(ROOT, "server.boot"), "w", encoding="utf-8") as f:
        f.write(os.environ["MUJI_BOOT_TOKEN"])

    print("Starting server...")
    child = subprocess.Popen([sys.executable, "server.py"])
    try:
        deadline = time.time() + 120
        while time.time() < deadline and child.poll() is None:
            if _port_ours(_real_port()):
                port = _real_port()
                if port != settings.port:
                    print(f"  (port {settings.port} was taken - using {port})")
                print(f"  {settings.title}  ->  http://127.0.0.1:{port}")
                webbrowser.open(f"http://127.0.0.1:{port}")
                break
            time.sleep(0.25)
        if child.poll() is not None:
            print("Server did not come up - see errors above.")
            return 1
        child.wait()  # stay in the foreground: closing this window stops the server
    except KeyboardInterrupt:
        child.terminate()
        child.wait()
    finally:
        # Belt and braces: the child unlinks these on clean exit, but a
        # hard kill (window closed) skips atexit — don't leave stale files.
        for f in (PORT_FILE, "server.boot"):
            try:
                os.remove(os.path.join(ROOT, f))
            except OSError:
                pass
    print("Server stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
