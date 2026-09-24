"""Claude Code hook entry points: SessionStart, Stop, StopFailure, and the
optional PreToolUse / PostToolUse gates (SPEC 10.4, off by default).

Contract: stdin JSON in, one JSON object out on stdout, exit 0, never raise.
Any internal failure fails OPEN (the stop is allowed) so Jevflow can never
trap a session. Claude Code's own 8-consecutive-block cap is a second
backstop.
"""

import fcntl
import json
import os
import sys
import time
import traceback
from typing import Any, Callable, Dict, IO, List, Mapping, Optional

from . import gates, policy
from .flow import Flow, FlowError, load_flow
from .jev_client import JevClient, JevError
from .judge import Judgment, judge
from .project import Paths, find_project, git_changes, run_checks
from .state import StateError, load_state, record, save_state

REASON_JOURNAL_CHARS = 600
LAST_BLOCK_CONTEXT_CHARS = 1500
ERROR_DETAIL_CHARS = 300
RESET_SOURCES = ("startup", "resume", "clear")
NO_JEV_ENV = "JEVFLOW_NO_JEV"
DEBUG_ENV = "JEVFLOW_DEBUG"


def _log(msg: str) -> None:
    """stderr only (Claude Code shows it in verbose mode). Never the key."""
    try:
        sys.stderr.write(f"jevflow: {msg}\n")
    except Exception:
        pass


def default_client_factory(remaining: int) -> Optional[Any]:
    """A JevClient capped at ``remaining`` calls, or None (checks-only)."""
    if os.environ.get(NO_JEV_ENV):
        return None
    if remaining <= 0:
        return None
    try:
        return JevClient(max_calls=remaining)
    except JevError as exc:  # JevKeyError: no key configured
        _log(f"Jev disabled: {type(exc).__name__}")
        return None


# ------------------------------------------------------------- context

def phase_table(flow: Flow, state: Mapping[str, Any]) -> str:
    status = state.get("phase_status", {})
    cur = state.get("current_phase")
    lines = []
    for p in flow.phases:
        mark = ">" if p.id == cur else " "
        extra = []
        if p.depends_on:
            extra.append("after " + ",".join(p.depends_on))
        if p.loop:
            extra.append(f"loop<= {p.loop.max_iterations} until `{p.loop.until}`")
        if p.on_fail:
            extra.append(f"on_fail->{p.on_fail}")
        if p.id in flow.branch_only:
            extra.append("branch only")
        tail = f" ({'; '.join(extra)})" if extra else ""
        lines.append(f"{mark} [{status.get(p.id, 'pending')}] {p.id}: {p.name}{tail}")
    return "\n".join(lines)


def session_context(flow: Flow, state: Mapping[str, Any], source: str) -> str:
    cur = state.get("current_phase")
    parts = [
        "Jevflow is tracking this session against a declared flow. A Stop hook checks "
        "progress before you are allowed to stop.",
        f"Goal: {flow.goal}",
        "Phases:\n" + phase_table(flow, state),
    ]
    if state.get("done"):
        parts.append("The goal is already complete.")
    elif cur in flow.ids:
        p = flow.phase(cur)
        parts.append(f"Current phase: {p.id} ({p.name}). Done when: {p.done_when}."
                     + (f" Check: `{p.check}`." if p.check else ""))
    last = state.get("last_block_reason")
    if last and source in ("resume", "compact"):
        if len(last) > LAST_BLOCK_CONTEXT_CHARS:
            last = last[:LAST_BLOCK_CONTEXT_CHARS - 3] + "..."
        parts.append("Last Jevflow instruction before this point:\n" + last)
    if state.get("needs_human"):
        parts.append("A human decision is pending (see .jevflow/NEEDS_HUMAN.md): "
                     + str(state["needs_human"]))
    return "\n\n".join(parts)


# ---------------------------------------------------------------- hooks

def _load(paths: Paths) -> tuple:
    flow = load_flow(paths.flow)
    state = load_state(paths.state, flow)
    return flow, state


def on_session_start(payload: Mapping[str, Any], paths: Paths, *, now: float) -> Dict[str, Any]:
    flow, state = _load(paths)
    source = str(payload.get("source") or "startup")
    if source in RESET_SOURCES:
        # new process: per-session budgets start over; compact is the same session
        state["blocks_this_session"] = 0
        state["consecutive_blocks"] = 0
    state["session_id"] = payload.get("session_id")
    if payload.get("transcript_path"):
        state["transcript_path"] = payload.get("transcript_path")
    record(paths.state, state, "session_start", source=source, now=now)
    return {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                   "additionalContext": session_context(flow, state, source)}}


def _write_needs_human(paths: Paths, flow: Flow, state: Mapping[str, Any],
                       question: str, now: float) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    body = (f"# Jevflow needs a human\n\n{ts}\n\n## Question\n\n{question}\n\n"
            f"## Goal\n\n{flow.goal}\n\n## Phases\n\n```\n{phase_table(flow, state)}\n```\n\n"
            "Resolve it, then clear `needs_human` (delete this file and restart the "
            "supervisor, or edit .jevflow/state.json).\n")
    tmp = paths.needs_human + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.replace(tmp, paths.needs_human)


def on_stop(payload: Mapping[str, Any], paths: Paths, *, now: float,
            client_factory: Callable[[int], Optional[Any]]) -> Dict[str, Any]:
    flow, state = _load(paths)
    if state.get("done"):
        return {}
    active = bool(payload.get("stop_hook_active"))
    checks, loop_checks = run_checks(flow, state, paths.root)

    # Deterministic first: budgets, caps, regression and loop phases never
    # need Jev. Probe with no judgment; only a degraded_* result means the
    # outcome depends on the judgment.
    d = policy.decide(flow, state, None, checks, now=now, stop_hook_active=active,
                      loop_checks=loop_checks)
    j: Optional[Judgment] = None
    if d.condition.startswith("degraded_"):
        remaining = int(flow.limits.get("max_jev_calls", 200)) - int(state.get("jev_calls", 0))
        client = client_factory(remaining)
        j = judge(client, flow, state, checks=checks,
                  last_message=str(payload.get("last_assistant_message") or ""),
                  changes=git_changes(paths.root, bool(flow.privacy.get("send_diff", False))))
        d = policy.decide(flow, state, j, checks, now=now, stop_hook_active=active,
                          loop_checks=loop_checks)

    mode = flow.mode
    # shadow modes (SPEC 10.3): policy.apply_mode owns the per-mode rules
    eff = policy.apply_mode(d, mode)
    blocks = eff.block

    policy.apply_decision(state, eff.decision)
    if j is not None:
        state["jev_calls"] = int(state.get("jev_calls", 0)) + int(j.calls)
        if j.degraded:
            # kept apart from last_error, which the supervisor reads for
            # Claude API failures (StopFailure backoff)
            state["last_jev_error"] = {"error": str(j.error)[:ERROR_DETAIL_CHARS], "ts": now}
    state["session_id"] = payload.get("session_id") or state.get("session_id")

    if eff.ask_human and eff.decision.question:
        _write_needs_human(paths, flow, state, eff.decision.question, now)
    record(paths.state, state, "stop", decision=d.kind, condition=d.condition,
           mode=mode, enforced=blocks, to_phase=d.to_phase,
           reason=d.reason[:REASON_JOURNAL_CHARS],
           checks={k: c.passed for k, c in checks.items() if c.passed is not None},
           probs=j.probs() if j is not None else None, now=now)

    if blocks:
        return {"decision": "block", "reason": "[jevflow] " + d.reason}
    if eff.message:
        return {"systemMessage": eff.message}
    if mode == "observe" and (d.blocks or d.question):
        return {}
    if d.condition == "goal_complete":
        return {"systemMessage": "[jevflow] Goal complete."}
    if d.kind == policy.ALLOW_STOP and d.condition not in ("already_done",):
        return {"systemMessage": f"[jevflow] {d.reason}"}
    return {}


def on_stop_failure(payload: Mapping[str, Any], paths: Paths, *, now: float) -> Dict[str, Any]:
    """Record the API error for the supervisor. Output is ignored by Claude."""
    flow, state = _load(paths)
    err = str(payload.get("error") or "unknown")[:60]
    details = str(payload.get("error_details") or "")[:ERROR_DETAIL_CHARS]
    state["last_error"] = {"source": "claude", "error": err, "details": details,
                           "session_id": payload.get("session_id"), "ts": now}
    record(paths.state, state, "stop_failure", error=err, now=now)
    return {}


# gate hooks run inside Claude's tool loop: keep Jev retries short so the
# journal is written well inside the hook timeout (hooks.json: 60 s)
GATE_JEV_TIMEOUT_S = 10.0
GATE_JEV_RETRIES = 1
GATE_JOURNAL_CMD_CHARS = 200


def _gate_client(flow: Flow, state: Mapping[str, Any],
                 client_factory: Callable[[int], Optional[Any]]) -> Optional[Any]:
    remaining = int(flow.limits.get("max_jev_calls", 200)) - int(state.get("jev_calls", 0))
    client = client_factory(remaining)
    if client is not None:
        for attr, val in (("timeout", GATE_JEV_TIMEOUT_S), ("max_retries", GATE_JEV_RETRIES)):
            if hasattr(client, attr):
                setattr(client, attr, min(getattr(client, attr), val))
    return client


def _gate_record(paths: Paths, flow: Flow, r: "gates.GateResult", event: str, *,
                 now: float, **extra: Any) -> None:
    """Charge Jev calls and journal a gate result. Claude can run tool calls in
    parallel, so the read-modify-write is done under an exclusive lock on a
    fresh reload (the Jev call itself happens outside the lock)."""
    with open(paths.state + ".lock", "a") as lk:
        fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
        try:
            state = load_state(paths.state, flow)
            state["jev_calls"] = int(state.get("jev_calls", 0)) + int(r.calls)
            if r.degraded and r.calls:
                state["last_jev_error"] = {"error": str(r.error)[:ERROR_DETAIL_CHARS], "ts": now}
            record(paths.state, state, event, verdict=r.verdict, condition=r.condition,
                   mode=flow.mode, probs=r.probs or None, now=now, **extra)
        finally:
            fcntl.flock(lk.fileno(), fcntl.LOCK_UN)


def on_pre_tool_use(payload: Mapping[str, Any], paths: Paths, *, now: float,
                    client_factory: Callable[[int], Optional[Any]]) -> Dict[str, Any]:
    """Pre-tool risk gate on Bash. Emits ask/deny or nothing, never allow."""
    flow, state = _load(paths)
    if not flow.gates.get("pre_tool") or payload.get("tool_name") != "Bash":
        return {}
    tool_input = payload.get("tool_input") or {}
    command = str(tool_input.get("command") or "") if isinstance(tool_input, dict) else ""
    if not command.strip() or gates.is_read_only(command, os.environ.get("CLAUDE_PLUGIN_ROOT")):
        return {}
    client = _gate_client(flow, state, client_factory)
    r = gates.pre_tool_risk(client, command, project_name=os.path.basename(paths.root),
                            b=gates.bands(flow.gates))
    mode = flow.mode
    enforced = mode == "enforce" and r.verdict in (gates.ASK, gates.DENY)
    _gate_record(paths, flow, r, "pre_tool", now=now, enforced=enforced,
                 command=gates.redact(command)[:GATE_JOURNAL_CMD_CHARS])
    if r.verdict == gates.NONE or mode == "observe":
        return {}
    if mode == "warn":
        return {"systemMessage": f"[jevflow] pre-tool gate would {r.verdict} this command "
                                 f"(warn mode): {r.reason}"}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": r.verdict,
                                   "permissionDecisionReason": "[jevflow] " + r.reason}}


def on_post_tool_use(payload: Mapping[str, Any], paths: Paths, *, now: float,
                     client_factory: Callable[[int], Optional[Any]]) -> Dict[str, Any]:
    """Injection screen on WebFetch (and Read with send_diff). Never blocks."""
    flow, state = _load(paths)
    tool = str(payload.get("tool_name") or "")
    g = flow.gates
    if not g.get("injection_screen") or tool not in g.get("injection_tools", ["WebFetch"]):
        return {}
    if tool == "Read" and not flow.privacy.get("send_diff", False):
        return {}  # file contents never go to Jev without send_diff
    content = gates.extract_text(payload.get("tool_response"))
    if not content.strip():
        return {}
    client = _gate_client(flow, state, client_factory)
    r = gates.injection_screen(client, tool, content, b=gates.bands(g))
    mode = flow.mode
    _gate_record(paths, flow, r, "post_tool", now=now, tool=tool)
    if r.verdict != "warn" or mode == "observe":
        return {}
    text = gates.INJECTION_WARNING.format(reason=r.reason)
    if mode == "warn":
        return {"systemMessage": text + " (warn mode: not shown to Claude)"}
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}


HANDLERS = ("SessionStart", "Stop", "StopFailure", "PreToolUse", "PostToolUse")


def handle(event: str, payload: Mapping[str, Any], *, env: Optional[Mapping[str, str]] = None,
           now: Optional[float] = None,
           client_factory: Callable[[int], Optional[Any]] = default_client_factory) -> Dict[str, Any]:
    """Dispatch one hook event. Never raises; returns {} on any failure."""
    now = time.time() if now is None else now
    try:
        if event not in HANDLERS:
            return {}
        paths = find_project(payload.get("cwd"), env)
        if paths is None:
            return {}  # no flow here: plugin inactive
        if event == "SessionStart":
            return on_session_start(payload, paths, now=now)
        if event == "Stop":
            return on_stop(payload, paths, now=now, client_factory=client_factory)
        if event == "PreToolUse":
            return on_pre_tool_use(payload, paths, now=now, client_factory=client_factory)
        if event == "PostToolUse":
            return on_post_tool_use(payload, paths, now=now, client_factory=client_factory)
        return on_stop_failure(payload, paths, now=now)
    except (FlowError, StateError) as exc:
        _log(f"{event}: {type(exc).__name__}: {exc}")
        if event == "Stop":
            return {"systemMessage": f"[jevflow] disabled for this stop: {exc}"}
        return {}
    except Exception as exc:  # fail open: never trap the session
        _log(f"{event}: internal error {type(exc).__name__}: {exc}")
        if (env if env is not None else os.environ).get(DEBUG_ENV):
            traceback.print_exc(file=sys.stderr)
        return {}


def main(argv: List[str], stdin: IO[str] = sys.stdin, stdout: IO[str] = sys.stdout, **kw: Any) -> int:
    """``python -m jevflow hook <Event>``. Always returns 0."""
    out: Dict[str, Any] = {}
    try:
        try:
            payload = json.loads(stdin.read() or "{}")
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        event = argv[0] if argv else str(payload.get("hook_event_name") or "")
        out = handle(event, payload, **kw)
    except BaseException as exc:  # includes KeyboardInterrupt/SystemExit from deep code
        _log(f"hook wrapper: {type(exc).__name__}")
        out = {}
    try:
        stdout.write(json.dumps(out))
        stdout.write("\n")
        stdout.flush()
    except Exception:
        pass
    return 0
