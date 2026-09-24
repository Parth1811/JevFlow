"""Flow run state and step journal (SPEC 2, 10.1, 10.2).

``.jevflow/state.json`` is written only by Jevflow code, always atomically
(temp file in the same directory, fsync, os.replace). ``record()`` appends a
journal entry and persists it before returning, so a restart resumes from
the journal rather than from memory.

The API key is never part of state. Callers must not pass it in ``extra``.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any, Dict, Mapping, Optional

from .flow import Flow

STATE_SCHEMA = 1
HISTORY_CAP = 500
PHASE_STATUSES = ("pending", "active", "done")


class StateError(ValueError):
    """The state file is unreadable or structurally invalid."""


def _now() -> float:
    return time.time()


def new_state(flow: Flow, now: Optional[float] = None) -> Dict[str, Any]:
    now = _now() if now is None else now
    first = flow.eligible({})[0]
    status = {pid: "pending" for pid in flow.ids}
    status[first] = "active"
    return {
        "state_schema": STATE_SCHEMA,
        "flow_version": flow.flow_version,
        "current_phase": first,
        "phase_status": status,
        "blocks_this_session": 0,
        "restarts": 0,
        "jev_calls": 0,
        "loop_iterations": {},
        "phase_attempts": {first: 1},
        "subtasks": {},
        "consecutive_blocks": 0,
        "stuck_streak": 0,
        "escalations": 0,
        "same_reason_count": 0,
        "needs_human": None,
        "last_failure": None,
        "review_streak": None,
        "last_block_reason": None,
        "last_error": None,
        "started_at": now,
        "updated_at": now,
        "history": [],
        "done": False,
    }


def validate_state(state: Any, flow: Flow) -> Dict[str, Any]:
    """Validate and reconcile a loaded state against the flow.

    If the flow changed (new or removed phases, new flow_version), the state
    is reconciled and a ``flow_changed`` journal entry is added. Structural
    corruption raises StateError.
    """
    if not isinstance(state, dict):
        raise StateError("state must be a JSON object")
    required = {"current_phase": str, "phase_status": dict, "history": list, "done": bool}
    for k, t in required.items():
        if not isinstance(state.get(k), t):
            raise StateError(f"state.{k} missing or wrong type")
    for k in ("blocks_this_session", "restarts", "jev_calls", "consecutive_blocks",
              "stuck_streak", "escalations", "same_reason_count"):
        v = state.setdefault(k, 0)
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise StateError(f"state.{k} must be a non-negative integer")
    for k in ("loop_iterations", "phase_attempts", "subtasks"):
        if not isinstance(state.setdefault(k, {}), dict):
            raise StateError(f"state.{k} must be an object")
    state.setdefault("last_block_reason", None)
    state.setdefault("last_error", None)
    state.setdefault("needs_human", None)
    state.setdefault("last_failure", None)
    state.setdefault("started_at", _now())
    state.setdefault("updated_at", state["started_at"])
    state.setdefault("state_schema", STATE_SCHEMA)
    for pid, st in state["phase_status"].items():
        if st not in PHASE_STATUSES:
            raise StateError(f"state.phase_status[{pid!r}] = {st!r} is invalid")

    changes = []
    ids = flow.ids
    for pid in list(state["phase_status"]):
        if pid not in ids:
            del state["phase_status"][pid]
            state["loop_iterations"].pop(pid, None)
            state["subtasks"].pop(pid, None)
            state["phase_attempts"].pop(pid, None)
            changes.append(f"removed {pid}")
    for pid in ids:
        if pid not in state["phase_status"]:
            state["phase_status"][pid] = "pending"
            changes.append(f"added {pid}")
    if state.get("flow_version") != flow.flow_version:
        changes.append(f"flow_version {state.get('flow_version')!r} -> {flow.flow_version!r}")
        state["flow_version"] = flow.flow_version
    if state["current_phase"] not in ids:
        eligible = flow.eligible(state["phase_status"])
        state["current_phase"] = eligible[0] if eligible else ids[-1]
        changes.append(f"current_phase reset to {state['current_phase']}")
    if changes:
        _append(state, {"event": "flow_changed", "detail": "; ".join(changes)})
    return state


def load_state(path: str, flow: Flow) -> Dict[str, Any]:
    """Load state, or create a fresh one if the file does not exist."""
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return new_state(flow)
    except (OSError, ValueError) as exc:
        raise StateError(f"cannot read state file {path}: {exc}") from None
    return validate_state(raw, flow)


def save_state(path: str, state: Mapping[str, Any]) -> None:
    """Atomic write: a reader sees the old file or the new one, never a torn one."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _append(state: Dict[str, Any], entry: Dict[str, Any], now: Optional[float] = None) -> None:
    now = _now() if now is None else now
    seq = int(state.get("seq", 0) or 0) + 1
    state["seq"] = seq
    entry = {"ts": round(now, 3), "seq": seq, **entry}
    hist = state["history"]
    hist.append(entry)
    if len(hist) > HISTORY_CAP:
        del hist[: len(hist) - HISTORY_CAP]
    state["updated_at"] = now


def record(
    path: str,
    state: Dict[str, Any],
    event: str,
    *,
    phase: Optional[str] = None,
    decision: Optional[str] = None,
    probs: Optional[Mapping[str, Any]] = None,
    now: Optional[float] = None,
    **extra: Any,
) -> Dict[str, Any]:
    """Append a journal entry and persist state before returning (step journal)."""
    entry: Dict[str, Any] = {"event": event, "phase": phase or state.get("current_phase")}
    if decision is not None:
        entry["decision"] = decision
    if probs is not None:
        entry["probs"] = dict(probs)
    entry.update(extra)
    _append(state, entry, now)
    save_state(path, state)
    return entry


def set_phase_status(state: Dict[str, Any], phase_id: str, status: str) -> None:
    if status not in PHASE_STATUSES:
        raise ValueError(f"bad phase status {status!r}")
    if phase_id not in state["phase_status"]:
        raise KeyError(phase_id)
    state["phase_status"][phase_id] = status
