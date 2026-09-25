"""Stop-hook policy (SPEC 4, 10.1 to 10.5). Pure: no I/O, no clock reads.

``decide()`` takes the flow, the current state (read-only), a Judgment (or a
degraded one), deterministic check results and the time, and returns a
``Decision``. ``apply_decision()`` applies the decision's patch to a state
dict; the hook then journals and saves it.

Code owns control flow: Jev never advances a phase on its own. Advance needs
the ``current_phase`` winner AND the verify noul at or above ``auto`` AND the
phase check passing when one is defined. Deterministic checks always outrank
Jev. ``next_action`` is only trusted for ``ask_human`` (a safe stop), and
only at ``auto`` confidence.

Conditions are evaluated in a fixed priority order; the first match wins.
Each Decision carries a ``condition`` tag naming the rule that fired.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from .flow import Flow
from .judge import UNCLEAR, CheckResult, Judgment
from .regions import ALL_DONE, idempotency_key, subtasks_for

ALLOW_STOP = "ALLOW_STOP"
BLOCK = "BLOCK"
ADVANCE = "ADVANCE"

CLAUDE_BLOCK_CAP = 8           # Claude Code's consecutive Stop-block cap
ESCALATE_AFTER = 3             # "change approach" count that escalates to ask_human
SAME_REASON_LIMIT = 3          # identical BLOCK reason this many times = looping
STUCK_STREAK = 2               # stuck >= flag this many times in a row = looping
STREAK_NEUTRAL = frozenset({"off_goal", "unclear", "phase_mismatch"})
REVIEW_PASS_LIMIT = 2          # consecutive review-band stops with the phase check passing -> advance
OUTPUT_CHARS = 1200            # failing check output quoted in a BLOCK reason


@dataclass
class Decision:
    kind: str
    condition: str
    reason: str = ""
    to_phase: Optional[str] = None
    notes: List[str] = field(default_factory=list)
    patch: Dict[str, Any] = field(default_factory=dict)
    question: Optional[str] = None   # set when ask_human

    @property
    def blocks(self) -> bool:
        return self.kind in (BLOCK, ADVANCE)


def _tail(text: str, n: int = OUTPUT_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else "..." + text[-(n - 3):]


def _bands(flow: Flow) -> Dict[str, float]:
    c = dict(flow.limits.get("confidence") or {})
    return {"auto": c.get("auto", 0.80), "review": c.get("review", 0.50), "flag": c.get("flag", 0.70)}


def _check_fail_text(pid: str, c: Optional[CheckResult]) -> str:
    if c is None or c.passed is None:
        return ""
    out = _tail(c.output)
    return f" Check for '{pid}' fails" + (f":\n{out}" if out else ".")


def _stop(condition: str, reason: str, **patch: Any) -> Decision:
    return Decision(ALLOW_STOP, condition, reason, patch=dict(patch))


def _ask_human(condition: str, question: str) -> Decision:
    return Decision(ALLOW_STOP, condition, question, question=question,
                    patch={"needs_human": question})


def _block(state: Mapping[str, Any], condition: str, reason: str, *,
           to_phase: Optional[str] = None, kind: str = BLOCK,
           notes: Optional[List[str]] = None, failure: str = "", **patch: Any) -> Decision:
    """``failure`` is the failing-check text in ``reason``. Only an identical
    failure repeated counts toward SAME_REASON_LIMIT; a plain "continue"
    repeated over a long phase is normal work, not looping."""
    notes = list(notes or [])
    full = reason + failure + ("".join("\nNote: " + n for n in notes))
    if failure and state.get("last_failure") == failure:
        same = int(state.get("same_reason_count", 0)) + 1
    else:
        same = 1 if failure else 0
    # an advance is progress, not a hold: it must not spend the block budget,
    # or a flow with N phases runs out of budget after N-1 advances
    p = {"blocks_inc": 0 if kind == ADVANCE else 1, "last_block_reason": full, "same_reason_count": same,
         "last_failure": failure or None}
    p.update(patch)
    return Decision(kind, condition, full, to_phase=to_phase, notes=notes, patch=p)


def _failing_required(flow: Flow, checks: Mapping[str, CheckResult]) -> List[str]:
    """Required phases whose defined check did not pass (or was not run)."""
    out = []
    for pid in flow.required():
        p = flow.phase(pid)
        if p.check is not None:
            c = checks.get(pid)
            if c is None or c.passed is not True:
                out.append(pid)
    return out


def settle_by_checks(flow: Flow, status: Mapping[str, Any],
                     checks: Mapping[str, CheckResult]) -> tuple:
    """Walk the DAG marking phases done whose own check passes. Only phases
    with a defined check that ran and passed; never loop, side-effect,
    dynamic or branch-only phases (those need their own machinery).
    Returns ``(settled_ids, new_status)``."""
    st = dict(status)
    settled: List[str] = []
    progress = True
    while progress:
        progress = False
        for pid in flow.eligible(st):
            p = flow.phase(pid)
            c = checks.get(pid)
            if (p.check is None or c is None or c.passed is not True or p.loop is not None
                    or p.side_effect or p.dynamic):
                continue
            st[pid] = "done"
            settled.append(pid)
            progress = True
    return settled, st


def _advance_target(flow: Flow, state: Mapping[str, Any], done_phase: str) -> Optional[str]:
    status = dict(state.get("phase_status", {}))
    status[done_phase] = "done"
    eligible = flow.eligible(status)
    return eligible[0] if eligible else None


def _advance_or_complete(flow: Flow, state: Mapping[str, Any], checks: Mapping[str, CheckResult],
                         cur: str, condition: str, notes: Optional[List[str]] = None) -> Decision:
    target = _advance_target(flow, state, cur)
    status_patch = {cur: "done"}
    if target is None:
        st_now = state.get("phase_status", {})
        remaining = [pid for pid in flow.required() if pid != cur and st_now.get(pid) != "done"]
        if not remaining:
            failing = _failing_required(flow, checks)
            if not failing:
                return _stop("goal_complete", "Goal complete: every phase is done and every check passes.",
                             phase_status=status_patch, done=True)
            return _block(state, "final_check_fail",
                          f"Every phase looks done, but these checks do not pass: {failing}. "
                          "Make them pass before stopping.",
                          failure="".join(_check_fail_text(pid, checks.get(pid)) for pid in failing))
        # nothing eligible but phases remain: dependency deadlock, ask a human
        return _ask_human("dag_deadlock",
                          f"Phase '{cur}' is done but no remaining phase is eligible "
                          f"(remaining: {remaining}). Check depends_on in flow.json.")
    p = flow.phase(target)
    status_patch[target] = "active"
    return _block(state, condition,
                  f"Phase '{cur}' is complete. Now work on phase '{target}' ({p.name}): {p.done_when}.",
                  to_phase=target, kind=ADVANCE, notes=notes,
                  phase_status=status_patch, current_phase=target,
                  stuck_streak=0, escalations=0)


def _subtask_hold(flow: Flow, state: Mapping[str, Any], j: Judgment, cur: str,
                  auto: float) -> Optional[Decision]:
    """Dynamic region (SPEC 10.1): Jev's sub-step choice can only HOLD an
    advance, never cause one. It holds when Jev names one specific sub-step
    at ``auto`` confidence, since that means work in the phase remains."""
    subs = subtasks_for(state, flow, cur)
    if not subs or j.current_subtask in (None, ALL_DONE, UNCLEAR):
        return None
    if j.current_subtask_conf < auto:
        return None
    step = next((t for t in subs if t["id"] == j.current_subtask), None)
    if step is None:
        return None
    p = flow.phase(cur)
    return _block(state, "subtask_pending",
                  f"Phase '{cur}' ({p.name}) is not finished: sub-step {step['id']} "
                  f"(\"{step['title']}\") still looks in progress "
                  f"({j.current_subtask_conf:.2f}). Finish it, or update "
                  ".jevflow/subtasks.json if the plan changed.", stuck_streak=0)


def decide(
    flow: Flow,
    state: Mapping[str, Any],
    judgment: Optional[Judgment],
    checks: Mapping[str, CheckResult],
    *,
    now: float,
    stop_hook_active: bool = False,
    loop_checks: Optional[Mapping[str, CheckResult]] = None,
) -> Decision:
    """Pure policy. ``checks``: phase id -> CheckResult of its ``check``.
    ``loop_checks``: phase id -> CheckResult of its ``loop.until``.

    ``stop_hook_active`` is Claude's flag that this Stop follows one of our
    blocks; when False the consecutive-block run starts over.
    """
    d = _decide(flow, state, judgment, checks, now=now,
                stop_hook_active=stop_hook_active, loop_checks=loop_checks)
    run = int(state.get("consecutive_blocks", 0)) if stop_hook_active else 0
    d.patch["consecutive_blocks"] = run + 1 if d.blocks else 0
    if not (d.kind == BLOCK and d.condition in STREAK_NEUTRAL):
        # Most outcomes end the streak (a drop band means Jev now says "not
        # done"). Holds that say nothing about whether the phase is finished
        # do not, or passing-check review stops split by an off_goal hold
        # never add up and the phase stalls until the budget runs out.
        d.patch.setdefault("review_streak", None)
    return d


def _decide(
    flow: Flow,
    state: Mapping[str, Any],
    judgment: Optional[Judgment],
    checks: Mapping[str, CheckResult],
    *,
    now: float,
    stop_hook_active: bool = False,
    loop_checks: Optional[Mapping[str, CheckResult]] = None,
) -> Decision:
    limits = flow.limits
    bands = _bands(flow)
    loop_checks = loop_checks or {}
    j = judgment if judgment is not None else Judgment.degraded_result("no judgment")
    cur = str(state.get("current_phase"))
    status = state.get("phase_status", {})
    phase = flow.phase(cur) if cur in flow.ids else None

    # 1. already done
    if state.get("done"):
        return _stop("already_done", "Goal already complete.")

    # 2. hard caps and budgets (always win)
    cap = int(limits.get("max_blocks_per_session", 6))
    over_budget = int(state.get("blocks_this_session", 0)) >= cap
    if over_budget or int(state.get("jev_calls", 0)) >= int(limits.get("max_jev_calls", 200)):
        # Out of budget. Let the deterministic checks settle what they can, so
        # finished work is recorded instead of stranded behind a Jev hold.
        settled, st = settle_by_checks(flow, status, checks)
        remaining = [pid for pid in flow.required() if st.get(pid) != "done"]
        if phase is not None and not remaining and not _failing_required(flow, checks):
            return _stop("goal_complete", "Goal complete: every phase is done and every check passes.",
                         phase_status={pid: "done" for pid in settled} or {cur: "done"}, done=True)
        if over_budget:
            patch: Dict[str, Any] = {}
            msg = (f"Stopping: Jevflow has already kept Claude going {cap} times this session "
                   f"(max_blocks_per_session) and will not hold it again until you reply.")
            if settled:
                nxt = next(iter(flow.eligible(st)), None)
                ps: Dict[str, str] = {pid: "done" for pid in settled}
                if nxt is not None:
                    ps[nxt] = "active"
                    patch["current_phase"] = nxt
                patch["phase_status"] = ps
                msg += f" Checks pass for {', '.join(settled)}: marked done."
            msg += (f" Still open: {', '.join(remaining)}. Send any message (for example"
                    " 'continue') to resume with a fresh budget.")
            return _stop("budget_blocks", msg, **patch)
    # Claude Code ends the loop itself after 8 consecutive blocks; stop one short
    # so the final word (and the journal entry) is ours.
    if stop_hook_active and int(state.get("consecutive_blocks", 0)) >= CLAUDE_BLOCK_CAP - 1:
        return _stop("hook_cap", "Stopping: Claude Code consecutive Stop-block cap reached.")
    elapsed_min = (now - float(state.get("started_at", now))) / 60.0
    if elapsed_min >= float(limits.get("max_total_minutes", 90)):
        return _stop("budget_time", f"Stopping: time budget reached ({limits.get('max_total_minutes')} min).")
    if int(state.get("jev_calls", 0)) >= int(limits.get("max_jev_calls", 200)):
        return _stop("budget_jev", f"Stopping: Jev call budget reached ({limits.get('max_jev_calls')}).")
    if phase is None:
        return _ask_human("bad_state", f"current_phase {cur!r} is not in the flow.")

    # 3. regression: a phase already done whose check now fails (deterministic)
    for p in flow.phases:
        if p.id in flow.branch_only:
            continue  # a debug branch's check is not a standing invariant
        if status.get(p.id) == "done" and p.check is not None:
            c = checks.get(p.id)
            if c is not None and c.passed is False:
                if p.side_effect:
                    # re-entering would repeat an external action (SPEC 10.2)
                    return _ask_human(
                        "side_effect_regression",
                        f"Phase '{p.id}' ({p.name}) has a side effect that already ran "
                        f"(key {idempotency_key(flow, state, p.id)}) but its check now fails. "
                        "Jevflow will not re-run it; decide whether to redo it by hand."
                        + _check_fail_text(p.id, c))
                patch_status = {p.id: "active"}
                if cur != p.id:
                    patch_status[cur] = "pending"
                return _block(state, "regression",
                              f"Regression: phase '{p.id}' ({p.name}) was done but its check now fails. "
                              "Fix it before continuing.", failure=_check_fail_text(p.id, c),
                              to_phase=p.id, phase_status=patch_status, current_phase=p.id)

    cur_check = checks.get(cur)
    check_defined = phase.check is not None
    check_pass = (cur_check is not None and cur_check.passed is True) if check_defined else None
    check_fail = check_defined and cur_check is not None and cur_check.passed is False

    # 4. bounded loop phase (deterministic until-check)
    if phase.loop is not None:
        lc = loop_checks.get(cur)
        iters = int(state.get("loop_iterations", {}).get(cur, 0))
        if lc is not None and lc.passed is True and check_pass is not False:
            return _advance_or_complete(flow, state, checks, cur, "loop_pass")
        # the until-check can pass while the phase check still fails; report whichever failed
        until_ok = lc is not None and lc.passed is True
        failing = cur_check if until_ok else lc
        what = (f"the phase check still fails ({phase.done_when})" if until_ok
                else f"'{phase.loop.until}' still fails")
        if iters >= phase.loop.max_iterations:
            if phase.on_fail:
                return _route_on_fail(flow, state, cur, "loop_exhausted_on_fail",
                                      f"Loop in phase '{cur}' hit {iters} iterations.", failing)
            return _ask_human("loop_exhausted",
                              f"Phase '{cur}' loop ran {iters} of {phase.loop.max_iterations} iterations "
                              f"and {what}.{_check_fail_text(cur, failing)}")
        goal = (f"'{phase.loop.until}' passes but the phase check fails; fix it: {phase.done_when}"
                if until_ok
                else f"keep going until '{phase.loop.until}' passes")
        return _block(state, "loop_continue",
                      f"Phase '{cur}' ({phase.name}), iteration {iters + 1} of "
                      f"{phase.loop.max_iterations}: {goal}.",
                      failure=_check_fail_text(cur, failing),
                      loop_iterations={cur: iters + 1})

    # 5. degraded mode: checks only, never block without evidence
    if j.degraded:
        note = f"Jev unavailable ({j.error}); checks-only mode."
        if check_pass:
            return _advance_or_complete(flow, state, checks, cur, "degraded_check_pass", [note])
        if check_fail:
            return _block(state, "degraded_check_fail",
                          f"Phase '{cur}' ({phase.name}) is not done: {phase.done_when}.",
                          failure=_check_fail_text(cur, cur_check), notes=[note])
        return _stop("degraded_no_check",
                     f"{note} Phase '{cur}' has no check, so there is no evidence to block on.")

    auto, review, flag = bands["auto"], bands["review"], bands["flag"]

    # 6. ask_human (a safe stop, only at auto confidence)
    if j.next_action == "ask_human" and j.next_action_conf >= auto:
        return _ask_human("ask_human", f"The agent appears blocked on phase '{cur}' ({phase.name}) "
                                       "and needs a human decision.")

    # 7. stuck / looping escalation
    same_reason = int(state.get("same_reason_count", 0))
    stuck_hit = j.stuck >= flag
    streak = int(state.get("stuck_streak", 0)) + 1 if stuck_hit else 0
    if (stuck_hit and streak >= STUCK_STREAK) or same_reason >= SAME_REASON_LIMIT:
        esc = int(state.get("escalations", 0)) + 1
        if esc >= ESCALATE_AFTER:
            return _ask_human("stuck_ask_human",
                              f"The agent has looped on phase '{cur}' ({phase.name}) after "
                              f"{esc - 1} change-approach instructions.{_check_fail_text(cur, cur_check)}")
        return _block(state, "stuck_escalate",
                      f"You are looping on phase '{cur}'. Stop, re-read the goal, and take a "
                      f"fundamentally different approach to: {phase.done_when}.",
                      failure=_check_fail_text(cur, cur_check),
                      stuck_streak=streak, escalations=esc, same_reason_count=0)
    if stuck_hit:
        return _block(state, "stuck",
                      f"You seem to be repeating a failing approach on phase '{cur}'. Stop, re-read "
                      f"the goal, and try a different approach to: {phase.done_when}.",
                      failure=_check_fail_text(cur, cur_check),
                      stuck_streak=streak)

    # 8. off-goal drift
    if j.off_goal >= flag:
        return _block(state, "off_goal",
                      f"Re-read the goal: \"{flow.goal}\". The current work does not serve phase "
                      f"'{cur}' ({phase.name}): {phase.done_when}.", stuck_streak=0)

    # 9. check failed after the agent thinks it is done: on_fail branch or premature claim
    thinks_done = (j.claims_done >= flag or (j.verify is not None and j.verify >= auto)
                   or j.phase_done.get(cur, 0.0) >= auto)
    if check_fail and thinks_done:
        if phase.on_fail:
            return _route_on_fail(flow, state, cur, "on_fail",
                                  f"Phase '{cur}' was attempted but its check fails.", cur_check)
        if j.claims_done >= flag:
            return _block(state, "premature_completion",
                          f"You said the work is done, but it is not: phase '{cur}' ({phase.name}) "
                          f"requires: {phase.done_when}.", failure=_check_fail_text(cur, cur_check),
                          stuck_streak=0)

    # 10. abstain: never advance or regress on 'unclear'
    if j.current_phase == UNCLEAR:
        return _block(state, "unclear",
                      f"Continue phase '{cur}' ({phase.name}): {phase.done_when}.",
                      failure=_check_fail_text(cur, cur_check) if check_fail else "",
                      notes=["Jev could not tell which phase this is; keeping the current phase."],
                      stuck_streak=0)

    # 11. compete then verify, plus the deterministic check
    conf = j.current_phase_conf
    verify = j.verify if j.verify_phase == cur else None
    # check_pass is None when no check is defined; a defined check must be True
    if (j.current_phase == cur and conf >= auto and verify is not None and verify >= auto
            and (check_pass is True or not check_defined)):
        hold = _subtask_hold(flow, state, j, cur, auto)
        if hold is not None:
            return hold
        return _advance_or_complete(flow, state, checks, cur, "advance")

    # 12. review band or drop band: keep the current phase
    notes: List[str] = []
    condition = "continue"
    top = min(conf, verify) if (j.current_phase == cur and verify is not None) else conf
    if j.current_phase != cur and conf >= review:
        notes.append(f"Jev thinks the work is in phase '{j.current_phase}' ({conf:.2f}); "
                     f"phases advance only through '{cur}'.")
        condition = "phase_mismatch"
    elif review <= top < auto:
        condition = "review_band"
        if check_pass is True:
            # The deterministic check passes and Jev only half agrees. Blocking
            # forever would stall the flow (seen live in C1: verify 0.63 after
            # a prior premature-completion block), so after REVIEW_PASS_LIMIT
            # consecutive such stops the check decides. Jev alone never advances.
            prev = state.get("review_streak") or {}
            streak = int(prev.get("n", 0)) + 1 if prev.get("phase") == cur else 1
            if streak >= REVIEW_PASS_LIMIT:
                hold = _subtask_hold(flow, state, j, cur, auto)
                if hold is not None:
                    return hold
                return _advance_or_complete(
                    flow, state, checks, cur, "review_check_pass",
                    [f"Check for '{cur}' passes and Jev was in the review band "
                     f"{streak} times in a row ({top:.2f}); the check decides."])
            notes.append(f"Jev is not yet confident phase '{cur}' is done ({top:.2f}); its check "
                         f"passes. Confirm every part of: {phase.done_when}.")
            return _block(state, condition,
                          f"Continue phase '{cur}' ({phase.name}). Not done yet: {phase.done_when}.",
                          notes=notes, stuck_streak=0, review_streak={"phase": cur, "n": streak})
        notes.append(f"Jev is not yet confident phase '{cur}' is done ({top:.2f}).")
    elif top < review:
        condition = "drop_band"
    return _block(state, condition,
                  f"Continue phase '{cur}' ({phase.name}). Not done yet: {phase.done_when}.",
                  failure=_check_fail_text(cur, cur_check) if check_fail else "",
                  notes=notes, stuck_streak=0)


def _route_on_fail(flow: Flow, state: Mapping[str, Any], cur: str, condition: str,
                   why: str, c: Optional[CheckResult]) -> Decision:
    phase = flow.phase(cur)
    target = flow.phase(phase.on_fail)  # validated to exist by flow.py
    if target.side_effect and state.get("phase_status", {}).get(target.id) == "done":
        return _ask_human("side_effect_on_fail",
                          f"{why} Its on_fail target '{target.id}' has a side effect that "
                          f"already ran (key {idempotency_key(flow, state, target.id)}); "
                          "Jevflow will not re-run it." + _check_fail_text(cur, c))
    return _block(state, condition,
                  f"{why} Switch to phase '{target.id}' ({target.name}): {target.done_when}. "
                  f"Then return to '{cur}'.", failure=_check_fail_text(cur, c),
                  to_phase=target.id, kind=ADVANCE,
                  phase_status={cur: "pending", target.id: "active"},
                  current_phase=target.id, loop_iterations={cur: 0},
                  stuck_streak=0)


@dataclass
class ModeEffect:
    """What the hook does with a Decision under a flow ``mode`` (SPEC 10.3).

    ``decision`` is a copy of the input with its patch adjusted for the mode;
    apply that one, never the original. ``block`` says whether Claude is
    actually stopped from ending its turn; ``message`` is a systemMessage for
    the user (None for none); ``ask_human`` says whether NEEDS_HUMAN is
    written and the supervisor pauses.
    """
    decision: Decision
    block: bool
    message: Optional[str]
    ask_human: bool


def apply_mode(d: Decision, mode: str) -> ModeEffect:
    """The observe / warn / enforce ladder. Pure.

    enforce: BLOCK and ADVANCE block; ask_human pauses the run.
    warn:    nothing blocks; a would-be block becomes a systemMessage; phase
             bookkeeping (advance, regression, loop count) still applies so
             the phase table tracks the work; ask_human still pauses, because
             it is a stop, not a block.
    observe: nothing blocks and nothing is shown; bookkeeping still applies;
             ask_human is journaled but does not pause or write NEEDS_HUMAN.
    In both shadow modes the block counters are not charged, since nothing
    was blocked. Unknown modes are treated as enforce (flow.py rejects them).
    """
    out = copy.deepcopy(d)
    if mode not in ("observe", "warn"):
        return ModeEffect(out, d.blocks, None, d.question is not None)
    if d.blocks:
        out.patch.pop("blocks_inc", None)
        out.patch["consecutive_blocks"] = 0
    ask = False
    if d.question is not None:
        if mode == "warn":
            ask = True
        else:
            out.patch.pop("needs_human", None)
            out.question = None
    msg: Optional[str] = None
    if mode == "warn":
        if d.blocks:
            msg = f"[jevflow warn] would block ({d.condition}): {d.reason}"
            if d.kind == ADVANCE and d.to_phase:
                # the phase table did move; say so, the stop itself is allowed
                msg = (f"[jevflow warn] would block ({d.condition}); phase table moved "
                       f"to '{d.to_phase}': {d.reason}")
        elif d.question is not None:
            msg = f"[jevflow warn] needs a human: {d.question}"
    return ModeEffect(out, False, msg, ask)


def apply_decision(state: Dict[str, Any], d: Decision) -> Dict[str, Any]:
    """Apply ``d.patch`` to ``state`` in place and return it."""
    p = copy.deepcopy(d.patch)
    if p.pop("blocks_inc", 0):
        state["blocks_this_session"] = int(state.get("blocks_this_session", 0)) + 1
    for pid, st in (p.pop("phase_status", None) or {}).items():
        prev = state.setdefault("phase_status", {}).get(pid)
        if st == "active" and prev != "active":
            # each entry into a phase is a new attempt (idempotency key, SPEC 10.2)
            att = state.setdefault("phase_attempts", {})
            att[pid] = int(att.get(pid, 0) or 0) + 1
        state["phase_status"][pid] = st
    for pid, n in (p.pop("loop_iterations", None) or {}).items():
        state.setdefault("loop_iterations", {})[pid] = n
    for k, v in p.items():
        state[k] = v
    return state
