"""Which agents work on a flow, and on which phase.

Several Claude sessions can work on one flow (a second session runs
``jevflow join <flow>``), one session can drive several flows (one per
session binding), and a session's subagents show up with their own
``agent_id``. Every hook that sees a payload calls ``touch``; it keeps one
entry per agent in ``state["agents"]``:

    {"<key>": {"label": "claude a1b2c3", "kind": "session" | "subagent",
               "type": "<subagent type>", "session": "<session id>",
               "phase": "<phase id>", "tool": "Edit", "target": "src/x.py",
               "first_at": 0.0, "at": 0.0, "tools": 3, "stops": 1}}

The phase is the agent's claim (``jevflow claim <phase>`` printed a marker
that PostToolUse picked up), else the flow's current phase. Informational
only: the Stop policy never reads it.
"""

import re
from typing import Any, Dict, Mapping, Optional

MAX_AGENTS = 24                 # oldest entries drop off beyond this
LABEL_CHARS = 40
CLAIM_MARKER = "JEVFLOW_CLAIM"
_CLAIM_RE = re.compile(CLAIM_MARKER + r" phase=([a-z][a-z0-9_-]{0,39})(?: name=([A-Za-z0-9 ._-]{1,40}))?")


def key_of(payload: Mapping[str, Any]) -> Optional[str]:
    aid = payload.get("agent_id")
    if aid:
        return "agent:" + str(aid)[:64]
    sid = payload.get("session_id")
    return "session:" + str(sid)[:64] if sid else None


def _default_label(payload: Mapping[str, Any]) -> str:
    if payload.get("agent_id"):
        t = str(payload.get("agent_type") or "subagent")
        return f"{t} {str(payload.get('agent_id'))[:6]}"
    return f"claude {str(payload.get('session_id') or '?')[:6]}"


def touch(state: Dict[str, Any], payload: Mapping[str, Any], now: float, *,
          event: str, tool: str = "", target: str = "") -> bool:
    """Record this agent's activity. Returns True when something a viewer
    should see changed (a new agent, a new phase, a stop), so the caller
    saves even inside its write throttle."""
    k = key_of(payload)
    if k is None:
        return False
    agents = state.get("agents") if isinstance(state.get("agents"), dict) else {}
    a = agents.get(k)
    new = a is None
    if new:
        a = {"label": _default_label(payload), "kind": "subagent" if payload.get("agent_id") else "session",
             "session": str(payload.get("session_id") or "")[:64], "first_at": now, "tools": 0, "stops": 0}
        if payload.get("agent_type"):
            a["type"] = str(payload.get("agent_type"))[:LABEL_CHARS]
    before = a.get("phase")
    if not a.get("claimed"):
        a["phase"] = state.get("current_phase")
    a["at"] = now
    if event == "tool":
        a["tools"] = int(a.get("tools", 0) or 0) + 1
        a["tool"], a["target"] = tool[:LABEL_CHARS], target
    elif event == "stop":
        a["stops"] = int(a.get("stops", 0) or 0) + 1
        a.pop("claimed", None)  # a claim lasts until the agent's turn ends
    agents[k] = a
    if len(agents) > MAX_AGENTS:
        for old in sorted(agents, key=lambda x: agents[x].get("at", 0))[:len(agents) - MAX_AGENTS]:
            agents.pop(old, None)
    state["agents"] = agents
    return new or before != a.get("phase") or event == "stop"


def claim_from_output(state: Dict[str, Any], payload: Mapping[str, Any], text: str,
                      phase_ids, now: float) -> bool:
    """Apply a ``jevflow claim`` marker from a Bash tool's output to the agent
    that ran it. Returns True when applied."""
    m = _CLAIM_RE.search(text or "")
    if not m or m.group(1) not in set(phase_ids):
        return False
    touch(state, payload, now, event="claim")
    a = state["agents"][key_of(payload)]
    a["phase"], a["claimed"] = m.group(1), True
    if m.group(2):
        a["label"] = m.group(2).strip()[:LABEL_CHARS]
    return True


def by_phase(state: Mapping[str, Any]) -> Dict[str, list]:
    out: Dict[str, list] = {}
    for a in (state.get("agents") or {}).values():
        if isinstance(a, dict) and a.get("phase"):
            out.setdefault(a["phase"], []).append(a.get("label", "?"))
    return out
