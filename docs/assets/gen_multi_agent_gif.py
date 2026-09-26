#!/usr/bin/env python3
"""Render docs/assets/multi-agent.gif: three agents working one flow, shown in
the real viewer (jevflow/ui_static/index.html).

The run is scripted (a sequence of state snapshots), not a recording: each
frame is the viewer exported with that state and screenshotted by headless
Chromium. Needs Pillow and a Chromium binary (dev only):
    CHROME=/path/to/chrome python3 docs/assets/gen_multi_agent_gif.py   # THEME=dark for the dark GIF
"""
import copy
import glob
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from PIL import Image  # noqa: E402

from jevflow.ui import render_export  # noqa: E402

W, H = 1280, 1060           # browser viewport
OUT_W = 960                 # GIF width
THEME = os.environ.get("THEME", "light")   # light | dark
SHOTS = 2                   # screenshots per state (spinner moves between them)
HOLD_MS = 700               # per screenshot

FLOW = {
    "schema_version": 1, "title": "Wordstats library and CLI", "mode": "enforce",
    "goal": "Build a word-count library with a CLI, docs and a benchmark, test it, and tag v0.1",
    "phases": [
        {"id": "plan", "name": "Plan", "done_when": "subtasks written", "check": "test -f PLAN.md"},
        {"id": "core", "name": "Core library", "done_when": "acceptance tests pass",
         "check": "pytest -q tests/test_core.py", "depends_on": ["plan"]},
        {"id": "cli", "name": "CLI", "done_when": "wordstats --top 5 works", "check": "pytest -q tests/test_cli.py",
         "depends_on": ["core"]},
        {"id": "docs", "name": "Docs", "done_when": "README usage", "check": "grep -q Usage README.md",
         "depends_on": ["core"]},
        {"id": "bench", "name": "Benchmark", "done_when": "bench.md with numbers", "check": "test -s bench.md",
         "depends_on": ["core"]},
        {"id": "test", "name": "Full suite", "done_when": "everything green", "check": "pytest -q",
         "depends_on": ["cli", "docs", "bench"], "loop": {"max_iterations": 3, "until": "pytest -q"},
         "on_fail": "debug"},
        {"id": "debug", "name": "Debug", "done_when": "root cause fixed", "depends_on": []},
        {"id": "release", "name": "Release", "done_when": "v0.1 tag", "check": "git tag -l v0.1 | grep -q .",
         "depends_on": ["test"], "side_effect": True},
    ],
}
LEAD, DOCS, PERF = "lead", "docs-writer", "perf"


def agent(label, phase, tool, target, ago, tools, stops, kind="session", claimed=False, typ=None):
    a = {"label": label, "kind": kind, "phase": phase, "tool": tool, "target": target,
         "at_ago": ago, "tools": tools, "stops": stops}
    if claimed:
        a["claimed"] = True
    if typ:
        a["type"] = typ
    return a


# (status updates, current, agents, loop_iterations, live checks, new history, done)
STEPS = [
    (dict(plan="active"), "plan", [agent(LEAD, "plan", "Write", "PLAN.md", 2, 3, 0)], {}, {},
     [("sys", "flow_laid_out", None, None, "")], False),
    (dict(plan="done", core="active"), "core", [agent(LEAD, "core", "Edit", "wordstats/core.py", 1, 14, 1)], {}, {},
     [("ADVANCE", "advance", "plan", "core", "Phase 'plan' is complete. Now work on phase 'core'.")], False),
    (dict(core="done", cli="active"), "cli",
     [agent(LEAD, "cli", "Edit", "wordstats/cli.py", 1, 22, 2),
      agent(DOCS, "docs", "Write", "README.md", 3, 2, 0, kind="subagent", claimed=True, typ="general-purpose"),
      agent(PERF, "bench", "Bash", "python bench.py", 2, 3, 0, claimed=True)], {}, {},
     [("ADVANCE", "advance", "core", "cli", "Phase 'core' is complete. Now work on phase 'cli'.")], False),
    (dict(), "cli",
     [agent(LEAD, "cli", "Bash", "pytest -q tests/test_cli.py", 1, 31, 2),
      agent(DOCS, "docs", "Edit", "README.md", 2, 7, 0, kind="subagent", claimed=True, typ="general-purpose"),
      agent(PERF, "bench", "Write", "bench.md", 1, 9, 0, claimed=True)], {}, {"docs": True, "bench": True},
     [("BLOCK", "review_band", "cli", None, "Continue phase 'cli'. Not done yet: wordstats --top 5 works.")], False),
    (dict(), "cli",
     [agent(LEAD, "cli", "Edit", "wordstats/cli.py", 1, 38, 3),
      agent(DOCS, "docs", "Read", "README.md", 150, 9, 1, kind="subagent", claimed=True, typ="general-purpose"),
      agent(PERF, "bench", "Read", "bench.md", 140, 11, 1, claimed=True)], {}, {"docs": True, "bench": True},
     [], False),
    (dict(cli="done", docs="done", bench="done", test="active"), "test",
     [agent(LEAD, "test", "Bash", "pytest -q", 1, 44, 4),
      agent(PERF, "test", "Read", "tests/test_core.py", 2, 14, 1, claimed=True)], {"test": 1}, {},
     [("ADVANCE", "advance", "cli", "docs", "Phase 'cli' is complete. Now work on phase 'docs'."),
      ("ADVANCE", "check_and_phase_done", "docs", "bench", "Phase 'docs' is complete. Now work on phase 'bench'."),
      ("ADVANCE", "check_and_phase_done", "bench", "test", "Phase 'bench' is complete. Now work on phase 'test'."),
      ("BLOCK", "loop_continue", "test", None, "Phase 'test', run 1 of 3 failed: 2 failed, 41 passed.")], False),
    (dict(test="done", release="active"), "release",
     [agent(LEAD, "release", "Bash", "git tag v0.1", 1, 51, 5)], {"test": 2}, {},
     [("ADVANCE", "loop_pass", "test", "release", "Phase 'test' is complete. Now work on phase 'release'.")], False),
    (dict(release="done"), "release", [agent(LEAD, "release", "", "", 3, 53, 6)], {"test": 2}, {},
     [("ALLOW_STOP", "goal_complete", "release", None, "Goal complete: every phase is done and every check passes.")],
     True),
]

OTHER_FLOWS = [
    {"id": "20260926-003000-temp-converter-cli", "title": "Temperature converter CLI", "archived": False,
     "phases": 4, "phases_done": 2, "current_phase": "tests", "agents": 1, "agents_active": 1, "done": False},
    {"id": "20260925-161314-todo-app", "title": "Todo app", "archived": True, "outcome": "complete",
     "phases": 3, "phases_done": 3, "done": True, "agents": 1},
]


def snapshots():
    t0 = time.time()
    status = {p["id"]: "pending" for p in FLOW["phases"]}
    hist = []
    for i, (upd, cur, ags, loops, live, new, done) in enumerate(STEPS):
        status.update(upd)
        for kind, cond, ph, to, reason in new:
            ev = {"event": "stop", "decision": kind, "condition": cond, "phase": ph, "to_phase": to,
                  "reason": reason, "agent": LEAD, "seq": len(hist)}
            if kind == "sys":
                ev = {"event": cond, "phase": ph, "reason": reason, "seq": len(hist)}
            hist.append(ev)
        yield {"status": dict(status), "cur": cur, "agents": copy.deepcopy(ags), "loops": dict(loops),
               "live": live, "hist": copy.deepcopy(hist), "done": done, "t0": t0 - 600 + i * 70}


def page(s, now):
    for n, h in enumerate(s["hist"]):
        h["ts"] = now - (len(s["hist"]) - n) * 25
    agents = {}
    for a in s["agents"]:
        a = dict(a)
        a["at"] = now - a.pop("at_ago")
        agents[("agent:" if a["kind"] == "subagent" else "session:") + a["label"]] = a
    state = {"phase_status": s["status"], "current_phase": s["cur"], "agents": agents,
             "loop_iterations": s["loops"], "history": s["hist"], "done": s["done"],
             "started_at": now - 420, "updated_at": now, "blocks_this_session": 1, "jev_calls": 9 + 2 * len(s["hist"]),
             "live": {"tool": s["agents"][0]["tool"] or "Stop", "target": s["agents"][0]["target"],
                      "tool_at": now - 1, "tools": s["agents"][0]["tools"], "checks": s["live"]}}
    fid = "20260926-004500-wordstats-lib-cli"
    main_row = {"id": fid, "title": FLOW["title"], "archived": False, "phases": 7,
                "phases_done": sum(1 for k, v in s["status"].items() if v == "done" and k != "debug"),
                "current_phase": s["cur"], "agents": len(agents),
                "agents_active": 0 if s["done"] else len(agents), "done": s["done"], "updated_at": now}
    flows = [main_row] + [dict(r, updated_at=now - 300 * (i + 1)) for i, r in enumerate(OTHER_FLOWS)]
    data = {"project": "wordstats", "flow_id": fid, "archived": False, "generated_at": now,
            "flow": FLOW, "state": state, "needs_human": None, "error": None, "text": "", "flows": flows,
            "snapshots": {}}
    html = render_export(data)
    # a replay, not a live snapshot: hide the snapshot timestamp pill
    return html.replace('<span id="conn" class="pill">loading</span>', '<span id="conn" class="pill" hidden></span>')


def shoot(chrome, html, png):
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as fh:
        fh.write(html.replace('<html lang="en">', f'<html lang="en" data-theme="{THEME}">'))
        path = fh.name
    try:
        subprocess.run([chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                        f"--window-size={W},{H}", "--virtual-time-budget=400", f"--screenshot={png}",
                        "file://" + path], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        os.remove(path)


def main():
    chrome = os.environ.get("CHROME") or next(iter(sorted(glob.glob(os.path.expanduser(
        "~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")))), None)
    if not chrome:
        sys.exit("set CHROME to a Chromium binary")
    out = os.path.join(HERE, "multi-agent.gif" if THEME == "light" else f"multi-agent-{THEME}.gif")
    frames, durations = [], []
    with tempfile.TemporaryDirectory() as tmp:
        for i, s in enumerate(snapshots()):
            shots = SHOTS + (2 if s["done"] else 0)
            for k in range(shots):
                png = os.path.join(tmp, f"{i:02d}-{k}.png")
                shoot(chrome, page(s, time.time()), png)
                im = Image.open(png).convert("RGB")
                im = im.resize((OUT_W, round(im.height * OUT_W / im.width)), Image.LANCZOS)
                frames.append(im)
                durations.append(HOLD_MS)
    pal = [f.quantize(colors=256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE) for f in frames]
    pal[0].save(out, save_all=True, append_images=pal[1:], duration=durations, loop=0, optimize=True)
    print(f"wrote {out}: {len(frames)} frames, {sum(durations) / 1000:.1f} s, "
          f"{os.path.getsize(out) / 1e6:.2f} MB")


if __name__ == "__main__":
    main()
