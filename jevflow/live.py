"""Live progress between Stops.

Jevflow decides only at Stop, and Claude can work through several phases in
one long turn, so without this the viewer and status line sit still until the
turn ends. PostToolUse (every tool) calls ``tick``: it stores the latest tool
activity in ``state["live"]`` (not in the history, so decisions are never
pushed out), and at most every ``PROBE_EVERY_S`` it runs the checks of phases
that are not done yet, with a small time budget, and records which pass.

This is informational only: phase status and advancing stay with the Stop
policy. It never blocks and never calls Jev.
"""

import os
import time
from typing import Any, Dict, Mapping, Optional

from .project import Paths, run_check

WRITE_EVERY_S = 2.0        # at most one state write per this many seconds
PROBE_EVERY_S = 20.0       # re-run pending phase checks at most this often
PROBE_BUDGET_S = 8.0       # all probe checks of one tick together
PROBE_CHECK_S = 5.0        # one probe check
TARGET_CHARS = 90


def _target(payload: Mapping[str, Any]) -> str:
    ti = payload.get("tool_input")
    if not isinstance(ti, dict):
        return ""
    for key in ("file_path", "path", "command", "pattern", "url", "description"):
        v = ti.get(key)
        if isinstance(v, str) and v.strip():
            v = " ".join(v.split())
            root = str(payload.get("cwd") or "")
            if root and v.startswith(root + os.sep):
                v = v[len(root) + 1:]
            return v if len(v) <= TARGET_CHARS else v[:TARGET_CHARS - 3] + "..."
    return ""


def tick(payload: Mapping[str, Any], paths: Paths, flow: Any, state: Dict[str, Any],
         now: float, clock=time.monotonic) -> bool:
    """Update ``state['live']`` in place. Returns True when the caller should save."""
    if state.get("done"):
        return False
    live = state.get("live") if isinstance(state.get("live"), dict) else {}
    live["tools"] = int(live.get("tools", 0) or 0) + 1
    live["tool"] = str(payload.get("tool_name") or "?")
    live["target"] = _target(payload)
    live["tool_at"] = now
    last_write = float(live.get("written_at", 0) or 0)
    probe_due = now - float(live.get("checked_at", 0) or 0) >= PROBE_EVERY_S
    state["live"] = live
    if not probe_due and now - last_write < WRITE_EVERY_S:
        return False
    if probe_due:
        status = state.get("phase_status", {})
        branch = getattr(flow, "branch_only", set())
        pending = [p for p in flow.phases
                   if p.check and status.get(p.id) != "done" and p.id not in branch]
        # current phase first, then the rest in flow order
        cur = state.get("current_phase")
        pending.sort(key=lambda p: p.id != cur)
        deadline = clock() + PROBE_BUDGET_S
        checks: Dict[str, Optional[bool]] = {}
        for p in pending:
            left = deadline - clock()
            if left <= 0.2:
                break
            checks[p.id] = run_check(p.check, paths.root, min(PROBE_CHECK_S, left)).passed
        live["checks"] = checks
        live["checked_at"] = now
    live["written_at"] = now
    return True
