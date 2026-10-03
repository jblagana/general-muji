"""Unit checks for the learned.md human-gate helpers (agent.learned_status /
apply_learned_decisions / _rewrite_learned). No server, no model — points
settings.learned_path at a temp file. Run: python tools/test_learned.py"""
import re, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import agent, config

_tmp = Path(tempfile.mkdtemp(prefix="muji_learned_"))
config.settings.learned_path = _tmp / "learned.md"

ok = 0
def check(name, cond):
    global ok
    ok += 1
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        raise SystemExit("FAILED: " + name)

sample = """# learned.md — muji's learned patterns (human-gated)

muji appends candidate patterns to `## pending` after tasks. They have NO
effect until you move a line to `## active` (veto = delete it). Only
`## active` is injected into the system prompt.

## pending

- pending one
- pending two
- pending three
- pending four

## active

- active one
"""
config.settings.learned_path.write_text(sample, encoding="utf-8")

st = agent.learned_status()
check("parse pending", st["pending"] == ["pending one", "pending two", "pending three", "pending four"])
check("parse active", st["active"] == ["active one"])

r = agent.apply_learned_decisions([
    {"text": "pending one", "action": "activate"},
    {"text": "pending two", "action": "veto"},
    {"text": "pending three", "action": "keep"},
    {"text": "pending four", "action": "activate"},
    {"text": "active one", "action": "deactivate"},
])
check("activated count", r["activated"] == 2)
check("vetoed count", r["vetoed"] == 1)
check("deactivated count", r["deactivated"] == 1)
check("pending after", r["pending"] == ["pending three"])
check("active after (newest first)", r["active"] == ["pending four", "pending one"])

st2 = agent.learned_status()
check("disk pending", st2["pending"] == ["pending three"])
check("disk active (newest first)", st2["active"] == ["pending four", "pending one"])
on_disk = config.settings.learned_path.read_text(encoding="utf-8")
check("header preserved", "human-gated" in on_disk and "## active" in on_disk)
check("no double-active", on_disk.count("- pending one\n") == 1)

r2 = agent.apply_learned_decisions([{"text": "pending one", "action": "activate"}])
check("idempotent activate", r2["activated"] == 0 and r2["active"] == ["pending four", "pending one"])

before = config.settings.learned_path.read_text(encoding="utf-8")
r3 = agent.apply_learned_decisions([{"text": "pending three", "action": "keep"}])
after = config.settings.learned_path.read_text(encoding="utf-8")
check("keep-only no-op (file unchanged)",
      before == after and r3["activated"] == 0 and r3["vetoed"] == 0)

# Newest-on-top (Boss rule 2026-09-25): a fresh pending entry lands as the
# FIRST bullet, so the latest lesson is what he sees first when reviewing.
agent._learned_append_pending("newest on top one")
agent._learned_append_pending("newest on top two")
st3 = agent.learned_status()
check("pending newest on top",
      st3["pending"] == ["newest on top two", "newest on top one", "pending three"])
on_disk3 = config.settings.learned_path.read_text(encoding="utf-8")
check("pending file order (newest first)",
      on_disk3.index("newest on top two") < on_disk3.index("newest on top one"))
check("pending section well-formed",
      "## pending\n- newest on top two\n- newest on top one\n" in on_disk3)

# Fix-phrase dedup: same bolded fix, new incident narrative = same lesson
# (the 2026-09-29 flood: 70+ re-narrations of one active rule).
agent._learned_append_pending("I claimed result R1 without running it → **Verify before asserting.** run it first")
agent._learned_append_pending("I claimed result R2 without checking it → **Verify before asserting.** check it first")
st4 = agent.learned_status()
check("fix-phrase dedup skips the re-narration",
      sum(1 for t in st4["pending"] if "Verify before asserting" in t) == 1)

# Word-safe truncation: a long entry cuts at a word boundary, not mid-word.
agent._learned_append_pending("I did " + "something wrong " * 30 + "at the end")
trunc = [t for t in agent.learned_status()["pending"] if t.startswith("I did something wrong")]
check("long entry truncates word-safe",
      len(trunc) == 1 and len(trunc[0]) <= 301 and trunc[0].endswith("…"))

# Pending cap: the queue is bounded; oldest overflow sinks to `## held`
# (never injected, invisible to the panel — which parses pending/active only).
for i in range(20):
    agent._learned_append_pending("failure case number %d → **Unique fix %d.** detail" % (i, i))
st5 = agent.learned_status()
check("pending capped at 15", len(st5["pending"]) == 15)
check("newest survives the cap", st5["pending"][0].startswith("failure case number 19"))
on_disk5 = config.settings.learned_path.read_text(encoding="utf-8")
check("## held created after ## active",
      on_disk5.index("## active") < on_disk5.index("## held"))
mh = re.search(r"^##\s*held\s*$(.*)$", on_disk5, re.M | re.S)
held_bullets = [ln for ln in mh.group(1).splitlines() if ln.lstrip().startswith("-")]
check("held carries exactly the overflow (10)", len(held_bullets) == 10)
check("held not exposed in pending/active lists",
      all(("failure case number %d → **Unique fix %d.** detail" % (i, i))
          not in st5["pending"] + st5["active"] for i in range(5)))

# Gate decisions with a held section: the rewrite keeps the overflow intact.
r4 = agent.apply_learned_decisions([{"text": st5["pending"][-1], "action": "veto"}])
on_disk6 = config.settings.learned_path.read_text(encoding="utf-8")
check("veto works with held present", r4["vetoed"] == 1)
check("held survives a gate rewrite",
      "## held" in on_disk6 and "failure case number 4" in on_disk6)

config.settings.learned_path.unlink()
check("missing file -> empty", agent.learned_status() == {"pending": [], "active": []})

print("\n%d checks green" % ok)
