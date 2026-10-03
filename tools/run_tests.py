"""One-command test runner: runs every tools/test_*.py in a FRESH subprocess
(each suite mutates global settings — they must not share a process), prints
a per-suite line + summary, exits non-zero if any failed.

Run:  .venv\Scripts\python.exe tools\run_tests.py [name-filter]
      .venv\Scripts\python.exe tools\run_tests.py onit   # just one suite
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = pathlib.Path(__file__).resolve().parent
flt = sys.argv[1] if len(sys.argv) > 1 else ""
suites = sorted(p for p in HERE.glob("test_*.py") if flt in p.name)
if not suites:
    print(f"no suites match {flt!r}")
    raise SystemExit(1)

results = []
for p in suites:
    print(f"\n===== {p.name} " + "=" * max(0, 50 - len(p.name)), flush=True)
    t0 = time.time()
    r = subprocess.run([sys.executable, str(p)])  # no capture: stream live
    results.append((p.name, r.returncode, time.time() - t0))

print("\n" + "=" * 60)
failed = 0
for name, code, dt in results:
    ok = code == 0
    failed += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  {name}  ({dt:.1f}s, exit {code})")
print("=" * 60)
if failed:
    print(f"{failed}/{len(results)} FAILED")
    raise SystemExit(1)
print(f"ALL GREEN ({len(results)}/{len(results)})")
