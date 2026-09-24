"""Dynamic regions and idempotent side-effect phases (SPEC 10.1, 10.2).

Dynamic region
    A phase with ``dynamic: true`` may be split into sub-steps by the agent.
    The agent writes ``.jevflow/subtasks.json``::

        {"<phase id>": ["step one", {"title": "step two", "done": true}]}

    Jevflow reads that file at each Stop and copies the sub-steps of dynamic
    phases into ``state.subtasks[phase]``. Entries for unknown or non-dynamic
    phases are ignored, so the outer skeleton (phases, order, status) can
    never be edited this way. Sub-steps carry no commands: nothing the agent
    writes here is ever executed. Jev judges them as a ``choice``; the only
    thing that choice can do is hold an advance (a BLOCK), never cause one.

Side-effect phase
    A phase with ``side_effect: true`` (publish, send, deploy) gets an
    idempotency key ``flow_version:phase:attempt``. When the phase is marked
    done the key is appended once to ``.jevflow/side_effects.jsonl``. That
    ledger is separate from state.json and append-only, so the phase stays
    done even if state.json is deleted or reset, and it is never routed back
    into (regression or on_fail) without a human.

Everything here is local file I/O. Nothing in this module talks to Jev.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .flow import Flow

MAX_SUBTASKS = 12
SUBTASK_TITLE_CHARS = 200
SUBTASKS_FILE_BYTES = 64 * 1024
LEDGER_FILE_BYTES = 1024 * 1024
ALL_DONE = "all_done"
SUBTASK_ID_RE = re.compile(r"^s([1-9][0-9]?)$")


# ------------------------------------------------------------- sub-steps

def _clean_title(val: Any) -> Optional[str]:
    if not isinstance(val, str):
        return None
    title = " ".join(val.split())
    if not title:
        return None
    if len(title) > SUBTASK_TITLE_CHARS:
        title = title[: SUBTASK_TITLE_CHARS - 3] + "..."
    return title


def parse_subtasks(data: Any, flow: Flow) -> Tuple[Dict[str, List[Dict[str, Any]]], List[str]]:
    """Normalise agent-written sub-steps. Returns (subtasks, warnings).

    Only dynamic phases are kept. Items are strings or {title, done}; ids
    s1..sN are assigned by position (the agent cannot pick ids)."""
    warnings: List[str] = []
    if not isinstance(data, dict):
        return {}, ["subtasks.json must be an object of phase id -> list"]
    dynamic = {p.id for p in flow.phases if p.dynamic}
    out: Dict[str, List[Dict[str, Any]]] = {}
    for pid, items in data.items():
        if pid not in dynamic:
            warnings.append(f"ignored {str(pid)[:40]!r}: not a dynamic phase")
            continue
        if not isinstance(items, list):
            warnings.append(f"ignored {pid!r}: must be a list")
            continue
        steps: List[Dict[str, Any]] = []
        for item in items:
            if len(steps) >= MAX_SUBTASKS:
                warnings.append(f"{pid!r}: only the first {MAX_SUBTASKS} sub-steps are kept")
                break
            done = False
            if isinstance(item, dict):
                title = _clean_title(item.get("title"))
                done = item.get("done") is True
            else:
                title = _clean_title(item)
            if title is None:
                warnings.append(f"{pid!r}: skipped a sub-step without a title")
                continue
            steps.append({"id": f"s{len(steps) + 1}", "title": title, "done": done})
        if steps:
            out[pid] = steps
    return out, warnings


def read_subtasks(path: str, flow: Flow) -> Tuple[Optional[Dict[str, List[Dict[str, Any]]]], List[str]]:
    """Read ``subtasks.json``. Returns (None, []) when the file is absent,
    so callers keep the sub-steps they already have."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(SUBTASKS_FILE_BYTES + 1)
    except FileNotFoundError:
        return None, []
    except OSError as exc:
        return None, [f"cannot read subtasks.json: {type(exc).__name__}"]
    if len(raw) > SUBTASKS_FILE_BYTES:
        return None, [f"subtasks.json is larger than {SUBTASKS_FILE_BYTES} bytes; ignored"]
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, ["subtasks.json is not valid JSON; ignored"]
    return parse_subtasks(data, flow)


def ingest_subtasks(state: Dict[str, Any], flow: Flow, path: str) -> Optional[Dict[str, Any]]:
    """Copy sub-steps from the file into ``state['subtasks']``. Returns a
    journal payload when something changed, else None. Never raises."""
    try:
        parsed, warnings = read_subtasks(path, flow)
    except Exception as exc:  # defensive: a hook must never raise
        parsed, warnings = None, [f"subtasks: {type(exc).__name__}"]
    # journal only on change: the same file re-read every Stop is not news
    new_warn = warnings != state.get("subtasks_warnings", [])
    state["subtasks_warnings"] = warnings
    if parsed is None:
        return {"warnings": warnings} if (warnings and new_warn) else None
    old = state.get("subtasks") if isinstance(state.get("subtasks"), dict) else {}
    if parsed == old:
        return {"warnings": warnings} if (warnings and new_warn) else None
    state["subtasks"] = parsed
    return {"phases": sorted(parsed), "counts": {k: len(v) for k, v in parsed.items()},
            "warnings": warnings}


def subtasks_for(state: Mapping[str, Any], flow: Flow, phase_id: str) -> List[Dict[str, Any]]:
    """Validated sub-steps of a dynamic phase from state (empty otherwise)."""
    if phase_id not in flow.ids or not flow.phase(phase_id).dynamic:
        return []
    raw = (state.get("subtasks") or {}).get(phase_id) if isinstance(state.get("subtasks"), dict) else None
    if not isinstance(raw, list):
        return []
    out = []
    for s in raw[:MAX_SUBTASKS]:
        if (isinstance(s, dict) and isinstance(s.get("id"), str) and SUBTASK_ID_RE.match(s["id"])
                and isinstance(s.get("title"), str)):
            out.append({"id": s["id"], "title": s["title"][:SUBTASK_TITLE_CHARS],
                        "done": s.get("done") is True})
    return out


# ------------------------------------------------------------ side effects

def attempt(state: Mapping[str, Any], phase_id: str) -> int:
    n = (state.get("phase_attempts") or {}).get(phase_id, 0)
    return n if isinstance(n, int) and not isinstance(n, bool) and n >= 1 else 1


def idempotency_key(flow: Flow, state: Mapping[str, Any], phase_id: str) -> str:
    return f"{flow.flow_version}:{phase_id}:{attempt(state, phase_id)}"


def load_ledger(path: str) -> List[Dict[str, Any]]:
    """Ledger entries. Torn or foreign lines are skipped; a missing file is empty."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(LEDGER_FILE_BYTES)
    except FileNotFoundError:
        return []
    except OSError:
        return []
    out = []
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and isinstance(e.get("phase"), str) and isinstance(e.get("key"), str):
            out.append(e)
    return out


def append_ledger(path: str, entry: Mapping[str, Any]) -> None:
    """One JSON line, O_APPEND + fsync, so a record is never half-replaced."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    line = (json.dumps(dict(entry), sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def restore_from_ledger(state: Dict[str, Any], flow: Flow,
                        ledger: List[Dict[str, Any]]) -> List[str]:
    """Mark side-effect phases done when the ledger says they already ran.

    Matches on phase id for any flow_version: a flow edit must not make a
    completed publish run again. Returns the phases restored."""
    done_phases = {e["phase"] for e in ledger}
    restored = []
    status = state.setdefault("phase_status", {})
    for p in flow.phases:
        if p.side_effect and p.id in done_phases and status.get(p.id) != "done":
            status[p.id] = "done"
            restored.append(p.id)
    if restored and status.get(state.get("current_phase")) == "done":
        eligible = flow.eligible(status)
        if eligible:
            state["current_phase"] = eligible[0]
            status[eligible[0]] = "active"
    return restored


def record_side_effects(state: Mapping[str, Any], flow: Flow, path: str, now: float) -> List[str]:
    """Append a ledger entry for every done side-effect phase not yet in the
    ledger. Returns the keys written."""
    have = {e["phase"] for e in load_ledger(path)}
    written = []
    for p in flow.phases:
        if p.side_effect and state.get("phase_status", {}).get(p.id) == "done" and p.id not in have:
            key = idempotency_key(flow, state, p.id)
            append_ledger(path, {"phase": p.id, "key": key, "ts": round(now, 3)})
            written.append(key)
    return written
