"""``python -m jevflow statusline``: one line for Claude Code's terminal status line.

Claude Code runs the configured command with session JSON on stdin and shows
whatever it prints. This reads ``workspace.project_dir`` (or ``cwd``) from that
JSON, finds the project's ``.jevflow/``, and prints e.g.::

    jevflow ▸ test 5/7 · loop 1/1 · blocks 2/8 · jev 47/80 · last BLOCK loop_continue

It must be fast and must never break the status line: no flow, bad JSON or an
unreadable state all print nothing (or a short hint) and exit 0. It reads
state.json directly (no schema validation) to stay cheap on every refresh.

``--config`` prints the settings.json snippet to enable it. ``--with CMD`` runs
an existing status line command first and appends Jevflow's segment, so it can
sit next to a status line you already have.
"""

import json
import os
import subprocess
import sys
from typing import Any, Dict, IO, List, Optional

from .project import find_project

HERE = os.path.dirname(os.path.abspath(__file__))
WRAPPER = os.path.join(os.path.dirname(HERE), "hooks", "jevflow")
SEP = " · "
WITH_TIMEOUT_S = 2.0

USAGE = """usage: python -m jevflow statusline [--with CMD]    (reads Claude Code status JSON on stdin)
       python -m jevflow statusline --config          print the settings.json snippet
"""

C = {"dim": "\033[2m", "ok": "\033[32m", "bad": "\033[31m", "warn": "\033[33m",
     "act": "\033[35m", "bold": "\033[1m", "off": "\033[0m"}


def _paint(on: bool):
    def p(text: str, *styles: str) -> str:
        if not on or not styles:
            return text
        return "".join(C[s] for s in styles) + text + C["off"]
    return p


def _load(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def segment(session: Dict[str, Any], color: bool = True, width: Optional[int] = None,
            env: Optional[Dict[str, str]] = None) -> str:
    """The Jevflow part of the status line, or '' when there is no flow."""
    ws = session.get("workspace") if isinstance(session.get("workspace"), dict) else {}
    start = ws.get("project_dir") or ws.get("current_dir") or session.get("cwd") or os.getcwd()
    paths = find_project(start, env={} if env is None else env)
    if paths is None:
        return ""
    p = _paint(color)
    flow = _load(paths.flow)
    if flow is None or not isinstance(flow.get("phases"), list):
        return p("jevflow", "dim") + " " + p("flow.json unreadable", "bad")
    phases = [ph.get("id") for ph in flow["phases"] if isinstance(ph, dict)]
    branch = {ph.get("on_fail") for ph in flow["phases"] if isinstance(ph, dict) and ph.get("on_fail")}
    main_phases = [x for x in phases if x not in branch]
    lim = flow.get("limits") if isinstance(flow.get("limits"), dict) else {}
    state = _load(paths.state)
    head = p("jevflow", "dim")
    if state is None:
        return f"{head} {p('ready', 'act')}{SEP}{len(main_phases)} phases, not started"
    status = state.get("phase_status") if isinstance(state.get("phase_status"), dict) else {}
    cur = state.get("current_phase") or "?"
    if state.get("done"):
        parts = [f"{head} {p('✓ goal complete', 'ok', 'bold')}"]
    elif state.get("needs_human") or os.path.isfile(paths.needs_human):
        q = state.get("needs_human")
        q = q if isinstance(q, str) else "see .jevflow/NEEDS_HUMAN.md"
        parts = [f"{head} {p('⚠ needs human', 'warn', 'bold')}", q.replace("\n", " ")]
    else:
        done_n = sum(1 for x in main_phases if status.get(x) == "done")
        parts = [f"{head} {p('▸ ' + str(cur), 'act', 'bold')} {done_n}/{len(main_phases)}"]
        ph = next((x for x in flow["phases"] if isinstance(x, dict) and x.get("id") == cur), None)
        loop = ph.get("loop") if isinstance(ph, dict) and isinstance(ph.get("loop"), dict) else None
        if loop:
            it = (state.get("loop_iterations") or {}).get(cur, 0)
            parts.append(p(f"loop {it}/{loop.get('max_iterations', '?')}", "warn"))
        if cur in branch:
            parts.append(p("debug branch", "bad"))
    blocks, mb = state.get("blocks_this_session", 0), lim.get("max_blocks_per_session")
    parts.append(f"blocks {blocks}/{mb}" if mb else f"blocks {blocks}")
    jev, mj = state.get("jev_calls", 0), lim.get("max_jev_calls")
    parts.append(f"jev {jev}/{mj}" if mj else f"jev {jev}")
    if state.get("restarts"):
        parts.append(f"restarts {state['restarts']}")
    if not state.get("done"):
        last = next((h for h in reversed(state.get("history") or [])
                     if isinstance(h, dict) and h.get("event") == "stop"), None)
        if last:
            kind = str(last.get("decision", ""))
            style = {"BLOCK": "bad", "ADVANCE": "ok", "ALLOW_STOP": "act"}.get(kind, "dim")
            parts.append(p(f"last {kind} {last.get('condition', '')}".rstrip(), style))
    line = SEP.join(parts)
    if width and _visible_len(line) > width:
        # drop trailing segments until it fits, never below the head
        while len(parts) > 1 and _visible_len(SEP.join(parts)) > width:
            parts.pop()
        line = SEP.join(parts)
    return line


def _visible_len(s: str) -> int:
    out, i = 0, 0
    while i < len(s):
        if s[i] == "\033":
            j = s.find("m", i)
            i = len(s) if j < 0 else j + 1
            continue
        out += 1
        i += 1
    return out


def config_snippet() -> str:
    return json.dumps({"statusLine": {"type": "command", "command": f"{WRAPPER} statusline"}}, indent=2)


def main(argv: List[str], stdin: IO[str], stdout: IO[str], stderr: IO[str]) -> int:
    args = list(argv)
    wrap = None
    while args:
        a = args.pop(0)
        if a == "--config":
            stdout.write(config_snippet() + "\n")
            return 0
        if a == "--with" and args:
            wrap = args.pop(0)
        elif a in ("-h", "--help"):
            stdout.write(USAGE)
            return 0
        else:
            stderr.write(USAGE)
            return 0  # never fail a status line
    raw = stdin.read() if not stdin.isatty() else ""
    try:
        session = json.loads(raw) if raw.strip() else {}
        if not isinstance(session, dict):
            session = {}
    except ValueError:
        session = {}
    env = dict(os.environ)
    color = not env.get("NO_COLOR")
    try:
        width = int(env.get("COLUMNS") or 0) or None
    except ValueError:
        width = None
    lines: List[str] = []
    if wrap:
        try:
            r = subprocess.run(wrap, shell=True, input=raw, capture_output=True, text=True,
                               timeout=WITH_TIMEOUT_S)
            lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        except (OSError, subprocess.SubprocessError):
            lines = []
    try:
        seg = segment(session, color=color, width=width,
                      env={k: env[k] for k in ("CLAUDE_PROJECT_DIR",) if k in env})
    except Exception:  # a status line must never crash
        seg = ""
    if seg:
        lines.append(seg)
    if lines:
        stdout.write("\n".join(lines) + "\n")
    return 0
