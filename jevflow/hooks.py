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

from . import auto, gates, live, notify, policy, regions, subgates
from .flow import Flow, FlowError, load_flow
from .jev_client import JevClient, JevError
from .judge import Judgment, judge
from .project import Paths, SUPERVISED_ENV, archive, find_project, git_changes, resolve, run_checks
from .state import StateError, _append, load_state, record, save_state

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
        if p.dynamic:
            extra.append("dynamic")
        if p.side_effect:
            extra.append("side effect")
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
        if p.dynamic:
            subs = regions.subtasks_for(state, flow, p.id)
            parts.append(
                "This phase is a dynamic region: you may split it into sub-steps by writing "
                f"`.jevflow/subtasks.json` as {{\"{p.id}\": [\"step\", {{\"title\": \"step\", "
                "\"done\": true}]}. Only this phase's sub-steps can be set; the phases "
                "themselves cannot be changed."
                + ("\nCurrent sub-steps:\n" + "\n".join(
                    f"- [{'x' if t['done'] else ' '}] {t['id']}: {t['title']}" for t in subs)
                   if subs else ""))
        if p.side_effect:
            parts.append(
                "This phase has an external side effect. Idempotency key: "
                f"`{regions.idempotency_key(flow, state, p.id)}`. A previous session may have "
                "done it before stopping, so check first and pass the key to the action if "
                "it accepts one. Do it at most once.")
    ran = [q.id for q in flow.phases
           if q.side_effect and state.get("phase_status", {}).get(q.id) == "done"]
    if ran:
        parts.append("Side effects already performed, never repeat them: "
                     + ", ".join(f"{pid} ({regions.idempotency_key(flow, state, pid)})"
                                 for pid in ran))
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
    if any(p.side_effect for p in flow.phases):
        # the append-only ledger outranks state.json (SPEC 10.2): a reset or
        # deleted state must not make a completed side effect run again
        restored = regions.restore_from_ledger(state, flow, regions.load_ledger(paths.side_effects))
        if restored:
            _append(state, {"event": "side_effect_restored",
                            "phase": state.get("current_phase"), "phases": restored})
    return flow, state


def on_session_start(payload: Mapping[str, Any], paths: Paths, *, now: float) -> Dict[str, Any]:
    source = str(payload.get("source") or "startup")
    # locked like the gate hooks: a background subagent's gate write must not
    # be lost to this read-modify-write (for example on source=compact)
    with _state_lock(paths):
        flow, state = _load(paths)
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


GATE_EVENTS = ("pre_tool", "post_tool", "subagent_stop", "task_completed")


class _state_lock:
    """Exclusive flock on ``state.json.lock`` (shared with the gate hooks)."""

    def __init__(self, paths: Paths) -> None:
        self.path = paths.state + ".lock"
        self.fh: Optional[IO[str]] = None

    def __enter__(self) -> "_state_lock":
        self.fh = open(self.path, "a")
        fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc: Any) -> None:
        assert self.fh is not None
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        finally:
            self.fh.close()


def _merge_concurrent(paths: Paths, flow: Flow, state: Dict[str, Any],
                      base_seq: int, base_calls: int) -> None:
    """Gate hooks (a background subagent's tools, SubagentStop) can write
    state while a Stop is being decided. Before the Stop saves, fold in what
    they wrote since this Stop loaded: their journal entries, their Jev
    calls and the subtask hold counters. Call with the lock held."""
    try:
        fresh = load_state(paths.state, flow)
    except StateError:
        return
    new = [h for h in fresh.get("history", [])
           if isinstance(h, dict) and int(h.get("seq", 0) or 0) > base_seq
           and h.get("event") in GATE_EVENTS]
    state["history"].extend(new)
    delta = int(fresh.get("jev_calls", 0)) - base_calls
    if delta > 0:
        state["jev_calls"] = int(state.get("jev_calls", 0)) + delta
    if isinstance(fresh.get("subtask_holds"), dict):
        state["subtask_holds"] = fresh["subtask_holds"]
    state["seq"] = max(int(state.get("seq", 0) or 0), int(fresh.get("seq", 0) or 0))


def on_stop(payload: Mapping[str, Any], paths: Paths, *, now: float,
            client_factory: Callable[[int], Optional[Any]]) -> Dict[str, Any]:
    flow, state = _load(paths)
    if state.get("done"):
        return {}
    active = bool(payload.get("stop_hook_active"))
    base_seq, base_calls = int(state.get("seq", 0) or 0), int(state.get("jev_calls", 0))
    if any(p.dynamic for p in flow.phases):
        change = regions.ingest_subtasks(state, flow, paths.subtasks)
        if change is not None:
            _append(state, {"event": "subtasks", "phase": state.get("current_phase"), **change}, now)
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

    if any(p.side_effect for p in flow.phases):
        keys = regions.record_side_effects(state, flow, paths.side_effects, now)
        if keys:
            _append(state, {"event": "side_effect_recorded",
                            "phase": state.get("current_phase"), "keys": keys}, now)
    asked = bool(eff.ask_human and eff.decision.question)
    if asked:
        _write_needs_human(paths, flow, state, eff.decision.question, now)
    sent = None
    ev = notify.event_for(d.condition, asked_human=asked)
    if ev is not None:
        sent = notify.notify(flow, state, paths.root, ev, condition=d.condition,
                             message=d.reason, now=now)
    extra = {"notify": sent} if sent is not None else {}
    with _state_lock(paths):
        _merge_concurrent(paths, flow, state, base_seq, base_calls)
        record(paths.state, state, "stop", decision=d.kind, condition=d.condition,
               mode=mode, enforced=blocks, to_phase=d.to_phase,
               reason=d.reason[:REASON_JOURNAL_CHARS],
               checks={k: c.passed for k, c in checks.items() if c.passed is not None},
               probs=j.probs() if j is not None else None, now=now, **extra)

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
    err = str(payload.get("error") or "unknown")[:60]
    details = str(payload.get("error_details") or "")[:ERROR_DETAIL_CHARS]
    with _state_lock(paths):
        flow, state = _load(paths)
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


def _gate_record(paths: Paths, flow: Flow, r: Any, event: str, *, now: float,
                 mutate: Optional[Callable[[Dict[str, Any]], None]] = None,
                 **extra: Any) -> None:
    """Charge Jev calls and journal a gate result. Claude can run tool calls in
    parallel, so the read-modify-write is done under an exclusive lock on a
    fresh reload (the Jev call itself happens outside the lock)."""
    with _state_lock(paths):
        state = load_state(paths.state, flow)
        state["jev_calls"] = int(state.get("jev_calls", 0)) + int(r.calls)
        if r.degraded and r.calls:
            state["last_jev_error"] = {"error": str(r.error)[:ERROR_DETAIL_CHARS], "ts": now}
        if mutate is not None:
            mutate(state)
        record(paths.state, state, event, verdict=r.verdict, condition=r.condition,
               mode=flow.mode, probs=r.probs or None, now=now, **extra)


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
    """Live progress for every tool, then the injection screen on WebFetch (and
    Read with send_diff). Never blocks."""
    with _state_lock(paths):
        flow, state = _load(paths)
        if live.tick(payload, paths, flow, state, now):
            save_state(paths.state, state)
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


# TaskCompleted blocks only through exit code 2 + stderr. The launcher maps
# this private code to 2 and every other non-zero code to 0, so a crash can
# never turn into a block.
BLOCK_EXIT = 42
SUBTASK_HOLDS_KEEP = 50


def _subtask_gate(flow: Flow, state: Mapping[str, Any], paths: Paths, key: str, task: str,
                  last_message: str, event: str, *, now: float,
                  client_factory: Callable[[int], Optional[Any]]) -> "subgates.SubtaskResult":
    """Shared SubagentStop / TaskCompleted path: cap, judge, journal."""
    holds = state.get("subtask_holds") if isinstance(state.get("subtask_holds"), dict) else {}
    if int(holds.get(key, 0) or 0) >= subgates.SUBTASK_BLOCK_CAP:
        r = subgates.SubtaskResult(subgates.ALLOW, "", "subtask_cap")
    else:
        client = _gate_client(flow, state, client_factory)
        c = flow.limits.get("confidence") or {}
        r = subgates.judge_subtask(client, task=task, goal=flow.goal, last_message=last_message,
                                   b={"auto": c.get("auto", 0.80), "flag": c.get("flag", 0.70)})
    enforced = flow.mode == "enforce" and r.verdict == subgates.BLOCK

    def bump(st: Dict[str, Any]) -> None:
        if not enforced:
            return
        h = st.get("subtask_holds") if isinstance(st.get("subtask_holds"), dict) else {}
        h[key] = int(h.get(key, 0) or 0) + 1
        while len(h) > SUBTASK_HOLDS_KEEP:
            h.pop(next(iter(h)))
        st["subtask_holds"] = h

    _gate_record(paths, flow, r, event, now=now, mutate=bump, enforced=enforced,
                 subtask=key[:80])
    return r


def on_subagent_stop(payload: Mapping[str, Any], paths: Paths, *, now: float,
                     client_factory: Callable[[int], Optional[Any]]) -> Dict[str, Any]:
    flow, state = _load(paths)
    if not flow.gates.get("subagent_stop") or state.get("done"):
        return {}
    if not str(payload.get("agent_type") or ""):
        return {}  # Claude Code's own internal agents (prompt suggestions, /btw)
    key = "agent:" + str(payload.get("agent_id") or "?")
    task = subgates.first_prompt(payload.get("agent_transcript_path"))
    r = _subtask_gate(flow, state, paths, key, task,
                      str(payload.get("last_assistant_message") or ""), "subagent_stop",
                      now=now, client_factory=client_factory)
    if r.verdict != subgates.BLOCK or flow.mode == "observe":
        return {}
    if flow.mode == "warn":
        return {"systemMessage": f"[jevflow warn] would keep the subagent working: {r.reason}"}
    return {"decision": "block", "reason": "[jevflow] " + r.reason}


def on_task_completed(payload: Mapping[str, Any], paths: Paths, *, now: float,
                      client_factory: Callable[[int], Optional[Any]]) -> Dict[str, Any]:
    flow, state = _load(paths)
    if not flow.gates.get("task_completed") or state.get("done"):
        return {}
    subject = str(payload.get("task_subject") or "").strip()
    task = subject + ("\n" + str(payload["task_description"]).strip()
                      if payload.get("task_description") else "")
    key = "task:" + str(payload.get("task_id") or subject[:40] or "?")
    last = subgates.last_assistant_text(payload.get("transcript_path"))
    r = _subtask_gate(flow, state, paths, key, task, last, "task_completed",
                      now=now, client_factory=client_factory)
    if r.verdict != subgates.BLOCK or flow.mode == "observe":
        return {}
    if flow.mode == "warn":
        return {"systemMessage": f"[jevflow warn] would keep the task open: {r.reason}"}
    return {"_block_exit": "[jevflow] " + r.reason}


HANDLERS = ("SessionStart", "Stop", "StopFailure", "PreToolUse", "PostToolUse",
            "SubagentStop", "TaskCompleted", "UserPromptSubmit")


def _archive_if_done(paths: Paths, env: Mapping[str, str], now: float) -> Optional[Paths]:
    """Move a finished multi-flow flow to .jevflow/done/. Under a supervisor
    the supervisor does it after releasing its lease (the lease file lives in
    the flow directory)."""
    if paths.flow_id is None or paths.archived or env.get(SUPERVISED_ENV):
        return None
    try:
        with open(paths.state, encoding="utf-8") as fh:
            if not json.load(fh).get("done"):
                return None
    except (OSError, ValueError):
        return None
    return archive(paths, now)


def handle(event: str, payload: Mapping[str, Any], *, env: Optional[Mapping[str, str]] = None,
           now: Optional[float] = None,
           client_factory: Callable[[int], Optional[Any]] = default_client_factory) -> Dict[str, Any]:
    """Dispatch one hook event. Never raises; returns {} on any failure."""
    now = time.time() if now is None else now
    try:
        if event not in HANDLERS:
            return {}
        env_map = os.environ if env is None else env
        root = find_project(payload.get("cwd"), env)
        if event == "UserPromptSubmit" and not env_map.get("JEVFLOW_NO_HINT") and (
                root is None or auto.needs_nudge(payload, root, env_map)):
            text = auto.prompt_nudge(str(payload.get("prompt") or ""))
            return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                           "additionalContext": text}} if text else {}
        if root is None:
            if event == "SessionStart" and not env_map.get("JEVFLOW_NO_HINT"):
                # no flow here yet: tell Claude it may start one when a task warrants it
                return {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                               "additionalContext": auto.start_hint()}}
            return {}  # no .jevflow here: plugin inactive
        if event == "PostToolUse" and payload.get("tool_name") == "Bash":
            auto.bind_from_tool_output(payload, root, gates.extract_text(payload.get("tool_response")))
        if event == "UserPromptSubmit":
            return auto.on_user_prompt(payload, root, env=env_map, now=now)
        paths = resolve(root, payload.get("session_id"), env_map)
        if paths is None or paths.archived:
            if event == "SessionStart" and not env_map.get("JEVFLOW_NO_HINT"):
                return {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                               "additionalContext": auto.start_hint()}}
            return {}  # this session is not working on an active flow
        if paths.is_draft and auto.try_activate(paths)[0]:
            # laid out mid-turn: start tracking now so the viewer and status line see it
            with _state_lock(paths):
                flow, state = _load(paths)
                record(paths.state, state, "flow_laid_out", phases=len(flow.phases), now=now)
        if paths.is_draft:
            if event == "SessionStart":
                return auto.on_draft_session_start(paths)
            if event != "Stop":
                return {}
            out, activated = auto.on_draft_stop(paths, now=now)
            if not activated:
                return out or {}
        if event == "SessionStart":
            return on_session_start(payload, paths, now=now)
        if event == "Stop":
            out = on_stop(payload, paths, now=now, client_factory=client_factory)
            done = _archive_if_done(paths, env_map, now)
            if done is not None and "decision" not in out:
                rel = os.path.relpath(done.dir, done.root)
                out = {"systemMessage": f"[jevflow] Goal complete. Flow archived to {rel}/ "
                                        "(SUMMARY.md inside)."}
            return out
        if event == "PreToolUse":
            return on_pre_tool_use(payload, paths, now=now, client_factory=client_factory)
        if event == "PostToolUse":
            return on_post_tool_use(payload, paths, now=now, client_factory=client_factory)
        if event == "SubagentStop":
            return on_subagent_stop(payload, paths, now=now, client_factory=client_factory)
        if event == "TaskCompleted":
            return on_task_completed(payload, paths, now=now, client_factory=client_factory)
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
    """``python -m jevflow hook <Event>``. Returns 0, or BLOCK_EXIT when a
    TaskCompleted gate blocks (the reason goes to stderr)."""
    out: Dict[str, Any] = {}
    code = 0
    try:
        try:
            payload = json.loads(stdin.read() or "{}")
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        event = argv[0] if argv else str(payload.get("hook_event_name") or "")
        out = handle(event, payload, **kw)
        block = out.pop("_block_exit", None) if isinstance(out, dict) else None
        if block:
            try:
                sys.stderr.write(str(block)[:2000] + "\n")
                sys.stderr.flush()
                code = BLOCK_EXIT
            except Exception:
                code = 0
    except BaseException as exc:  # includes KeyboardInterrupt/SystemExit from deep code
        _log(f"hook wrapper: {type(exc).__name__}")
        out, code = {}, 0
    try:
        stdout.write(json.dumps(out))
        stdout.write("\n")
        stdout.flush()
    except Exception:
        pass
    return code
