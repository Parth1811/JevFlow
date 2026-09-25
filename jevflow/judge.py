"""Judgment step (SPEC 3, 10.3).

Builds a curated Jev state within a character budget, asks the v3 question
set from docs/RESEARCH.md (every choice offers ``unclear``), then runs
compete-then-verify: a ``noul`` "is phase X actually done" on the
``current_phase`` winner. The result is a ``Judgment`` dataclass.

Privacy: with ``privacy.send_diff=false`` (the default) the change summary
carries only file names and line counts, never file contents or diff text.
Check output (truncated) and the last assistant message tail are always
sent (SPEC 3); a check command that prints file contents will expose them.

Jev is never asked "why" or "which step caused this" (trajectory attribution
was measured near random). Root cause comes from check output.

Any Jev failure yields ``Judgment.degraded(...)``; callers then fall back to
checks-only policy. ``judge()`` never raises a JevError.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .flow import Flow
from .jev_client import JevError
from .regions import ALL_DONE, subtasks_for

LAST_MESSAGE_CHARS = 4000
HISTORY_ITEMS = 5
CHECK_OUTPUT_CHARS = 800
MAX_CHANGE_FILES = 40
UNCLEAR = "unclear"

NEXT_ACTIONS: Dict[str, str] = {
    "continue_phase": "Keep working on the current phase, it is not finished",
    "advance_phase": "The current phase is finished, move to the next phase",
    "fix_regression": "Something that previously worked is now broken and must be fixed",
    "ask_human": "The agent is blocked and needs a human decision",
    "goal_complete": "Every phase is finished and verified, the goal is met",
    UNCLEAR: "The state does not make the next step clear",
}

# v3 wording chosen in the A2 spike (docs/RESEARCH.md).
Q_PHASE = "Which phase of the plan is the agent currently working on"
Q_DONE = "The agent has fully completed the '{name}' phase: {done_when}"
Q_NEXT = "What should happen next in this session"
Q_STUCK = ("The recent history and latest message show the agent retrying the same "
           "failing approach without new information")
Q_OFF = "The latest message describes work unrelated to the goal and to the current phase"
Q_CLAIMS = "The latest message claims the work or the goal is complete"
Q_PROGRESS = "How far the work has progressed toward the whole goal"
PROGRESS_LEVELS = ["Not started", "Early", "About halfway", "Nearly done",
                   "Complete and verified"]
Q_UNCLEAR_PHASE = "The state does not make the current phase clear"
Q_SUBTASK = "Which sub-step of the current phase is the agent working on"
Q_SUBTASK_ALL_DONE = "Every sub-step of the current phase is finished"
Q_UNCLEAR_SUBTASK = "The state does not make the current sub-step clear"


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a deterministic check. ``passed=None`` means not run / no check."""

    passed: Optional[bool]
    output: str = ""


@dataclass
class Judgment:
    current_phase: str = UNCLEAR
    current_phase_conf: float = 0.0
    current_phase_probs: Dict[str, float] = field(default_factory=dict)
    next_action: str = UNCLEAR
    next_action_conf: float = 0.0
    phase_done: Dict[str, float] = field(default_factory=dict)
    verify_phase: Optional[str] = None
    verify: Optional[float] = None
    stuck: float = 0.0
    off_goal: float = 0.0
    claims_done: float = 0.0
    current_subtask: Optional[str] = None      # sub-step id, all_done, unclear; None if not asked
    current_subtask_conf: float = 0.0
    progress: Optional[float] = None
    progress_conf: Optional[float] = None
    calls: int = 0
    degraded: bool = False
    error: Optional[str] = None

    @classmethod
    def degraded_result(cls, reason: str, calls: int = 0) -> "Judgment":
        return cls(degraded=True, error=reason, calls=calls)

    def probs(self) -> Dict[str, Any]:
        """Compact numbers for the step journal (no free text)."""
        out: Dict[str, Any] = {
            "current_phase": [self.current_phase, round(self.current_phase_conf, 3)],
            "next_action": [self.next_action, round(self.next_action_conf, 3)],
            "stuck": round(self.stuck, 3),
            "off_goal": round(self.off_goal, 3),
            "claims_done": round(self.claims_done, 3),
        }
        for k, v in self.phase_done.items():
            out["phase_done__" + k] = round(v, 3)
        if self.current_subtask is not None:
            out["current_subtask"] = [self.current_subtask, round(self.current_subtask_conf, 3)]
        if self.verify is not None:
            out["verify"] = [self.verify_phase, round(self.verify, 3)]
        if self.progress is not None:
            out["progress"] = round(self.progress, 3)
        if self.degraded:
            out["degraded"] = self.error
        return out


# ---------------------------------------------------------------- state

def _tail(text: str, n: int) -> str:
    text = text or ""
    if len(text) <= n:
        return text
    if n <= 3:
        return text[-n:] if n > 0 else ""
    return "..." + text[-(n - 3):]


def _head(text: str, n: int) -> str:
    text = text or ""
    if len(text) <= n:
        return text
    if n <= 3:
        return text[:n]
    return text[: n - 3] + "..."


def summarize_changes(entries: Sequence[Mapping[str, Any]], send_diff: bool,
                      max_files: int = MAX_CHANGE_FILES) -> List[Any]:
    """Change summary for Jev.

    ``entries`` items: ``{"path", "added", "removed", optional "diff"}``.
    Without ``send_diff`` only ``"path +A -R"`` strings are produced; any
    ``diff`` text is dropped. With ``send_diff`` a truncated diff is kept.
    """
    out: List[Any] = []
    ordered = sorted(entries, key=lambda e: -(int(e.get("added") or 0) + int(e.get("removed") or 0)))
    for e in ordered[:max_files]:
        line = f"{e.get('path', '?')} +{int(e.get('added') or 0)} -{int(e.get('removed') or 0)}"
        if send_diff and e.get("diff"):
            out.append({"file": line, "diff": _head(str(e["diff"]), 1500)})
        else:
            out.append(line)
    if len(ordered) > max_files:
        out.append(f"... and {len(ordered) - max_files} more files")
    return out


def _history_view(state: Mapping[str, Any], n: int) -> List[str]:
    items = []
    for h in state.get("history", []):
        if h.get("decision"):
            items.append(f"{h.get('decision')} {h.get('phase', '')}".strip()
                         + (f": {_head(str(h['reason']), 160)}" if h.get("reason") else ""))
    return items[-n:] if n > 0 else []


def build_state(
    flow: Flow,
    state: Mapping[str, Any],
    *,
    checks: Mapping[str, CheckResult],
    last_message: str = "",
    changes: Sequence[Mapping[str, Any]] = (),
    budget: Optional[int] = None,
) -> str:
    """Return the JSON state string sent to Jev, at most ``budget`` chars when
    achievable. Trimming order: history, check outputs, change list, last
    message tail, done_when text. Goal and phase ids are never dropped."""
    budget = int(budget or flow.limits.get("state_char_budget", 12000))
    send_diff = bool(flow.privacy.get("send_diff", False))
    status = state.get("phase_status", {})

    # (history items, check chars, change files, last msg chars, done_when chars)
    levels = [
        (HISTORY_ITEMS, CHECK_OUTPUT_CHARS, MAX_CHANGE_FILES, LAST_MESSAGE_CHARS, 400),
        (3, 400, 20, 3000, 400),
        (1, 200, 10, 2000, 200),
        (0, 120, 5, 1000, 120),
        (0, 60, 0, 400, 60),
        (0, 0, 0, 150, 40),
    ]
    subs = subtasks_for(state, flow, str(state.get("current_phase") or ""))
    out = ""
    for hist_n, chk_n, files_n, msg_n, dw_n in levels:
        doc = {
            "goal": _head(flow.goal, 1000),
            "phases": [
                {"id": p.id, "name": _head(p.name, 80), "done_when": _head(p.done_when, dw_n),
                 "status": status.get(p.id, "pending")}
                for p in flow.phases
            ],
            "current_phase": state.get("current_phase"),
            "check_results": {
                pid: ("no check" if c.passed is None else
                      ("pass" if c.passed else "fail")
                      + (": " + _tail(c.output.strip(), chk_n) if chk_n and c.output.strip() and not c.passed else ""))
                for pid, c in checks.items()
            },
            "last_assistant_message": _tail(last_message, msg_n),
            "change_summary": summarize_changes(changes, send_diff, files_n) if files_n else
                              ([f"{len(changes)} files changed"] if changes else []),
            "recent_history": _history_view(state, hist_n),
        }
        if subs:
            doc["current_phase_substeps"] = [
                {"id": t["id"], "title": _head(t["title"], max(dw_n, 40)),
                 "agent_marked_done": t["done"]} for t in subs]
        out = json.dumps(doc, indent=None, separators=(",", ":"), ensure_ascii=False)
        if len(out) <= budget:
            return out
    return out  # best effort at the tightest level


# ------------------------------------------------------------ questions

def build_questions(flow: Flow, current_phase: str,
                    subtasks: Sequence[Mapping[str, Any]] = ()) -> Dict[str, Any]:
    criteria = {p.id: f"{p.name}: {p.done_when}" for p in flow.phases}
    criteria[UNCLEAR] = Q_UNCLEAR_PHASE
    q: Dict[str, Any] = {
        "current_phase": {"type": "choice", "instructions": Q_PHASE, "criteria": criteria},
        "next_action": {"type": "choice", "instructions": Q_NEXT, "criteria": dict(NEXT_ACTIONS)},
        "stuck": {"type": "noul", "instructions": Q_STUCK},
        "off_goal": {"type": "noul", "instructions": Q_OFF},
        "claims_done": {"type": "noul", "instructions": Q_CLAIMS},
        "progress": {"type": "score", "instructions": Q_PROGRESS, "criteria": list(PROGRESS_LEVELS)},
    }
    ids = flow.ids
    targets = []
    if current_phase in ids:
        targets.append(current_phase)
        nxt = flow.next_phase(current_phase)
        if nxt:
            targets.append(nxt)
    for pid in targets:
        p = flow.phase(pid)
        q["phase_done__" + pid] = {"type": "noul",
                                   "instructions": Q_DONE.format(name=p.name, done_when=p.done_when)}
    if subtasks:
        # dynamic region (SPEC 10.1): a choice over the agent's sub-steps
        sc = {str(t["id"]): str(t["title"]) for t in subtasks}
        sc[ALL_DONE] = Q_SUBTASK_ALL_DONE
        sc[UNCLEAR] = Q_UNCLEAR_SUBTASK
        q["current_subtask"] = {"type": "choice", "instructions": Q_SUBTASK, "criteria": sc}
    return q


def build_verify_question(flow: Flow, phase_id: str) -> Dict[str, Any]:
    p = flow.phase(phase_id)
    return {"verify": {"type": "noul",
                       "instructions": Q_DONE.format(name=p.name, done_when=p.done_when)}}


# -------------------------------------------------------------- parsing

def _num(v: Any) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError("not a finite number")
    return max(0.0, min(1.0, float(v)))


def _choice(ans: Mapping[str, Any], allowed: Sequence[str]) -> tuple:
    probs = {k: _num(v) for k, v in (ans.get("probabilities") or {}).items() if k in allowed}
    choice = ans.get("choice")
    if choice not in allowed:
        choice = max(probs, key=probs.get) if probs else UNCLEAR
    conf = ans.get("confidence")
    conf = _num(conf) if conf is not None else probs.get(choice, 0.0)
    return choice, conf, probs


def parse_answers(flow: Flow, answers: Mapping[str, Any],
                  subtask_ids: Sequence[str] = ()) -> Judgment:
    """Parse a first-call ``answers`` dict. Raises ValueError on a bad shape."""
    j = Judgment()
    phase_opts = flow.ids + [UNCLEAR]
    j.current_phase, j.current_phase_conf, j.current_phase_probs = _choice(
        answers["current_phase"], phase_opts)
    j.next_action, j.next_action_conf, _ = _choice(answers["next_action"], list(NEXT_ACTIONS))
    for name in ("stuck", "off_goal", "claims_done"):
        setattr(j, name, _num(answers[name]["noul"]))
    prog = answers.get("progress")
    if prog:
        # assumed 0..(levels-1) (A2 saw 1.64 / 2.52); normalised to [0, 1].
        # Logged only; the policy never keys on progress.
        score = float(prog["score"])
        if not math.isfinite(score):
            raise ValueError("progress score not finite")
        j.progress = max(0.0, min(1.0, score / (len(PROGRESS_LEVELS) - 1)))
        if prog.get("confidence") is not None:
            j.progress_conf = _num(prog["confidence"])
    if subtask_ids:
        sub = answers.get("current_subtask")
        if isinstance(sub, Mapping):
            j.current_subtask, j.current_subtask_conf, _ = _choice(
                sub, list(subtask_ids) + [ALL_DONE, UNCLEAR])
        else:
            j.current_subtask, j.current_subtask_conf = UNCLEAR, 0.0
    for k, v in answers.items():
        if k.startswith("phase_done__") and k[len("phase_done__"):] in flow.ids:
            j.phase_done[k[len("phase_done__"):]] = _num(v["noul"])
    return j


def judge(
    client: Any,
    flow: Flow,
    state: Mapping[str, Any],
    *,
    checks: Mapping[str, CheckResult],
    last_message: str = "",
    changes: Sequence[Mapping[str, Any]] = (),
) -> Judgment:
    """Run judge + verify. Never raises JevError; returns a degraded Judgment."""
    if client is None:
        return Judgment.degraded_result("no Jev client: key missing (put it in ~/.config/jevflow/api_key, or set JEV_API_KEY / JEVFLOW_KEY_FILE) or JEVFLOW_NO_JEV is set")
    before = getattr(client, "calls_made", 0)

    def spent() -> int:
        return max(0, getattr(client, "calls_made", before) - before)

    current = str(state.get("current_phase") or "")
    doc = build_state(flow, state, checks=checks, last_message=last_message, changes=changes)
    try:
        subs = subtasks_for(state, flow, current)
        answers = client.ask(doc, build_questions(flow, current, subs))
        j = parse_answers(flow, answers, [t["id"] for t in subs])
    except JevError as exc:
        return Judgment.degraded_result(f"{type(exc).__name__}: {exc}", spent())
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        return Judgment.degraded_result(f"malformed Jev answers ({type(exc).__name__})", spent())

    if j.current_phase != UNCLEAR:
        j.verify_phase = j.current_phase
        try:
            v = client.ask(doc, build_verify_question(flow, j.current_phase))
            j.verify = _num(v["verify"]["noul"])
        except JevError as exc:
            # first call succeeded: keep it, but without verify nothing can advance
            j.error = f"verify failed: {type(exc).__name__}"
            j.verify = None
        except (KeyError, TypeError, ValueError, AttributeError):
            j.error = "verify failed: malformed answer"
            j.verify = None
    j.calls = spent()
    return j
