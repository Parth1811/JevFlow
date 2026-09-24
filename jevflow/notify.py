"""Notify hook (SPEC 10.5, extension).

``flow.notify``::

    {"command": "<shell command>", "on": ["ask_human", "goal_complete", "budget"],
     "timeout_s": 10}

The command runs in the project directory when one of the listed events
happens (default: all three). It receives a JSON object on stdin and these
environment variables: JEVFLOW_EVENT, JEVFLOW_CONDITION, JEVFLOW_PHASE,
JEVFLOW_MESSAGE (at most 1000 chars). The Jev key variables are removed from
its environment, like for checks. Its output is discarded; a failure or
timeout is journaled and never affects the flow. The same event (event,
condition, phase) is sent once, so a budget stop repeated on every Stop does
not spam.

The command comes from flow.json, which the agent can also edit (same trust
level as a check command). Treat flow.json as code.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
from typing import Any, Dict, Mapping, Optional

EVENTS = ("ask_human", "goal_complete", "budget")
MESSAGE_CHARS = 1000
DEFAULT_TIMEOUT_S = 10
MAX_TIMEOUT_S = 30


def event_for(condition: str, *, asked_human: bool) -> Optional[str]:
    """Map a Stop decision (or supervisor outcome) to a notify event."""
    if asked_human:
        return "ask_human"
    if condition == "goal_complete":
        return "goal_complete"
    if condition.startswith("budget") or condition == "hook_cap":
        return "budget"
    return None


def _env(extra: Mapping[str, str]) -> Dict[str, str]:
    from .project import SCRUB_ENV  # lazy: flow.py imports this module
    env = {k: v for k, v in os.environ.items() if k not in SCRUB_ENV}
    env.update(extra)
    return env


def notify(flow: Any, state: Dict[str, Any], root: str, event: str, *, condition: str,
           message: str, now: float) -> Optional[Dict[str, Any]]:
    """Run notify.command once for this event. Returns a journal payload, or
    None when nothing ran. Updates ``state['last_notify']``. Never raises."""
    cfg = flow.notify or {}
    cmd = cfg.get("command")
    if not cmd or event not in cfg.get("on", EVENTS):
        return None
    phase = str(state.get("current_phase") or "")
    key = f"{event}:{condition}:{phase}"
    if state.get("last_notify") == key:
        return None
    state["last_notify"] = key
    msg = (message or "")[:MESSAGE_CHARS]
    payload = {"event": event, "condition": condition, "phase": phase, "message": msg,
               "goal": flow.goal[:MESSAGE_CHARS], "ts": round(now, 3)}
    timeout = int(cfg.get("timeout_s", DEFAULT_TIMEOUT_S))
    try:
        # stderr to a temp file, not a pipe: a backgrounded grandchild holding
        # the pipe open would otherwise hang us past the timeout
        with tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(
                cmd, shell=True, cwd=root, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=err, start_new_session=True,
                env=_env({"JEVFLOW_EVENT": event, "JEVFLOW_CONDITION": condition,
                          "JEVFLOW_PHASE": phase, "JEVFLOW_MESSAGE": msg}))
            try:
                proc.communicate(json.dumps(payload).encode("utf-8"), timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.wait()
                return {"event": event, "condition": condition, "error": f"timeout after {timeout}s"}
            out: Dict[str, Any] = {"event": event, "condition": condition,
                                   "exit_code": proc.returncode}
            if proc.returncode != 0:
                err.seek(0)
                out["stderr"] = err.read(4096).decode("utf-8", "replace")[-300:]
            return out
    except (OSError, ValueError) as exc:
        return {"event": event, "condition": condition, "error": type(exc).__name__}


def parse_notify(raw: Any) -> Dict[str, Any]:
    """Validate ``flow.notify``. Raises ValueError with a message."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("notify must be an object")
    unknown = set(raw) - {"command", "on", "timeout_s"}
    if unknown:
        raise ValueError(f"notify: unknown key(s) {sorted(unknown)}")
    if not raw:
        return {}
    cmd = raw.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        raise ValueError("notify.command must be a non-empty string")
    on = raw.get("on", list(EVENTS))
    if not isinstance(on, list) or not on or any(e not in EVENTS for e in on):
        raise ValueError(f"notify.on must be a non-empty list from {EVENTS}")
    t = raw.get("timeout_s", DEFAULT_TIMEOUT_S)
    if isinstance(t, bool) or not isinstance(t, int) or not 1 <= t <= MAX_TIMEOUT_S:
        raise ValueError(f"notify.timeout_s must be an integer in [1, {MAX_TIMEOUT_S}]")
    return {"command": cmd.strip(), "on": sorted(set(on), key=EVENTS.index), "timeout_s": t}
