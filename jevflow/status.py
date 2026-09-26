"""``python -m jevflow status``: phase table, recent decisions, NEEDS_HUMAN."""

import json
import os
import time
from typing import Any, IO, List, Mapping, Optional

from . import agents
from .flow import Flow, FlowError, load_flow
from .project import Paths, default_flow, find_project, flow_paths
from .state import StateError, load_state

RECENT = 8
NEEDS_HUMAN_CHARS = 2000


def _ts(v: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(float(v)))
    except (TypeError, ValueError, OverflowError):
        return "?"


def _row(cells: List[str], widths: List[int]) -> str:
    return "  ".join(c.ljust(w) for c, w in zip(cells, widths)).rstrip()


def render(flow: Flow, state: Mapping[str, Any], paths: Optional[Paths] = None,
           recent: int = RECENT) -> str:
    status = state.get("phase_status", {})
    cur = state.get("current_phase")
    loops = state.get("loop_iterations", {}) or {}
    who = agents.by_phase(state)
    head = ["", "PHASE", "STATUS", "CHECK", "AGENTS", "NOTES"]
    rows = []
    for p in flow.phases:
        notes = []
        if p.depends_on:
            notes.append("after " + ",".join(p.depends_on))
        if p.loop:
            notes.append(f"loop {loops.get(p.id, 0)}/{p.loop.max_iterations} runs")
        if p.on_fail:
            notes.append(f"on_fail->{p.on_fail}")
        if p.id in flow.branch_only:
            notes.append("branch only")
        rows.append([">" if p.id == cur else "", p.id, status.get(p.id, "pending"),
                     "yes" if p.check else "-", ", ".join(who.get(p.id, [])) or "-", "; ".join(notes)])
    widths = [max(len(r[i]) for r in rows + [head]) for i in range(len(head))]
    lim = flow.limits
    out = [
        *([f"Flow: {flow.title}"] if flow.title else []),
        f"Goal: {flow.goal}",
        f"Flow version {flow.flow_version}, mode {flow.mode}. "
        f"Done: {'yes' if state.get('done') else 'no'}.",
        "",
        _row(head, widths),
        *[_row(r, widths) for r in rows],
        "",
        f"Blocks this session: {state.get('blocks_this_session', 0)}/{lim.get('max_blocks_per_session')}  "
        f"Restarts: {state.get('restarts', 0)}/{lim.get('max_restarts')}  "
        f"Jev calls: {state.get('jev_calls', 0)}/{lim.get('max_jev_calls')}  "
        f"Started: {_ts(state.get('started_at'))}",
    ]
    err = state.get("last_error")
    if isinstance(err, dict):
        out.append(f"Last Claude API error: {err.get('error', '?')} at {_ts(err.get('ts'))}")
    jerr = state.get("last_jev_error")
    if isinstance(jerr, dict):
        out.append(f"Last Jev error (checks-only fallback): {jerr.get('error', '?')} at {_ts(jerr.get('ts'))}")
    decisions = [h for h in state.get("history", []) if h.get("event") == "stop"][-recent:]
    out += ["", "Recent decisions:"]
    if not decisions:
        out.append("  (none yet)")
    for h in decisions:
        reason = str(h.get("reason") or "").replace("\n", " ")
        if len(reason) > 110:
            reason = reason[:107] + "..."
        flag = "" if h.get("enforced", True) or h.get("decision") == "ALLOW_STOP" else " (not enforced)"
        out.append(f"  {_ts(h.get('ts'))}  {h.get('decision')}/{h.get('condition')}{flag}  "
                   f"[{h.get('phase')}] {reason}")
    if state.get("needs_human") or (paths and os.path.isfile(paths.needs_human)):
        out += ["", "NEEDS_HUMAN:"]
        body = None
        if paths and os.path.isfile(paths.needs_human):
            try:
                with open(paths.needs_human, encoding="utf-8") as fh:
                    body = fh.read(NEEDS_HUMAN_CHARS)
            except OSError:
                body = None
        out.append(body.rstrip() if body else "  " + str(state.get("needs_human")))
    return "\n".join(out)


def main(argv: List[str], stdout: IO[str], stderr: IO[str]) -> int:
    project = os.getcwd()
    as_json = False
    flow_id = None
    args = list(argv)
    while args:
        a = args.pop(0)
        if a == "--project" and args:
            project = args.pop(0)
        elif a == "--json":
            as_json = True
        elif a == "--flow" and args:
            flow_id = args.pop(0)
        else:
            stderr.write("usage: python -m jevflow status [--project DIR] [--flow ID] [--json]\n")
            return 3
    root = find_project(project, env={})
    paths = None
    if root is not None:
        paths = flow_paths(root, flow_id) if flow_id else default_flow(root)
    if paths is None:
        stderr.write(f"no flow{' ' + flow_id if flow_id else ''} at or above {project} "
                     "(see: jevflow flows)\n")
        return 3
    if paths.is_draft:
        stdout.write(f"flow {paths.flow_id}: draft, phases not laid out yet\n")
        return 0
    if paths.flow_id and not as_json:
        stdout.write(f"Flow {paths.flow_id}{' (archived)' if paths.archived else ''}\n")
    try:
        flow = load_flow(paths.flow)
        state = load_state(paths.state, flow)
    except (FlowError, StateError) as exc:
        stderr.write(f"{exc}\n")
        return 3
    if as_json:
        stdout.write(json.dumps(state, indent=1, sort_keys=True) + "\n")
    else:
        stdout.write(render(flow, state, paths) + "\n")
    return 0
