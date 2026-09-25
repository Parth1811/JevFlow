"""Auto-planning: a hook lays out a new flow from the user's prompt.

With auto mode on for a project (``jevflow auto on`` or ``JEVFLOW_AUTO=1``),
``UserPromptSubmit`` looks at each prompt of a session that is not yet
working on a flow. If it reads like a task, it creates a draft flow under
``.jevflow/flows/<id>/`` (goal = the prompt), binds the session to it, and
tells Claude to lay the work out as phases in that flow.json before starting.

While the flow is a draft, the Stop hook blocks until flow.json is valid (at
most ``PLAN_BLOCK_LIMIT`` times; after that the draft is archived as
``abandoned`` so a session is never trapped). Once valid, the flow runs like
any other, and when its goal completes it is moved to ``.jevflow/done/<id>/``
with a SUMMARY.md, and the next task prompt starts a new flow.
"""

import json
import os
import re
from typing import Any, Dict, Mapping, Optional, Tuple

from .flow import FlowError, load_flow
from .project import (Paths, archive, auto_enabled, bind_session, bound_flow, has_legacy,
                      load_config, new_flow)

PLAN_BLOCK_LIMIT = 3
MIN_WORDS = 8
SKIP_TAG = "#nojev"
FORCE_TAG = "#jev"
WRAPPER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks", "jevflow")


def looks_like_task(prompt: str, min_words: int = MIN_WORDS) -> bool:
    """A cheap, deterministic filter: long imperative requests become flows;
    questions, slash commands and short follow-ups do not."""
    text = prompt.strip()
    if not text or SKIP_TAG in text.lower():
        return False
    if FORCE_TAG in text.lower().split():
        return True
    if text[0] in "/!":
        return False
    words = re.findall(r"\S+", text)
    if len(words) < min_words:
        return False
    first = words[0].lower().strip(",.:")
    question_start = first in {"what", "why", "how", "when", "where", "who", "which", "is", "are",
                               "does", "do", "did", "can", "could", "should", "would", "explain"}
    if text.rstrip().endswith("?") and (question_start or len(words) < 25):
        return False
    return not (question_start and len(words) < 25)


def _read_draft(p: Paths) -> Dict[str, Any]:
    try:
        with open(p.draft, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_draft(p: Paths, d: Mapping[str, Any]) -> None:
    tmp = p.draft + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(dict(d), fh, indent=1)
    os.replace(tmp, p.draft)


def try_activate(p: Paths) -> Tuple[bool, str]:
    """Promote a draft once its flow.json is valid. Returns (active, error)."""
    if not os.path.isfile(p.flow):
        return False, f"{_rel(p, p.flow)} does not exist yet"
    try:
        load_flow(p.flow)
    except FlowError as exc:
        return False, f"{_rel(p, p.flow)} is invalid: {exc}"
    try:
        os.remove(p.draft)
    except FileNotFoundError:
        pass
    return True, ""


def _rel(p: Paths, path: str) -> str:
    return os.path.relpath(path, p.root)


def plan_instructions(p: Paths, goal: str) -> str:
    rel = _rel(p, p.flow)
    example = {
        "schema_version": 1, "goal": goal[:200] + ("..." if len(goal) > 200 else ""), "mode": "enforce",
        "phases": [
            {"id": "implement", "name": "Implement", "done_when": "the feature works end to end",
             "check": "python -m pytest -q tests/test_feature.py", "depends_on": []},
            {"id": "docs", "name": "Document", "done_when": "README explains the new behaviour",
             "check": "grep -q 'new flag' README.md", "depends_on": ["implement"]},
        ],
    }
    return (
        f"Jevflow auto-planning: this request starts a tracked flow `{p.flow_id}`.\n"
        f"Before doing the work, lay it out as phases by writing `{rel}`:\n"
        "- keep `goal` exactly as the user asked (the full request, not the shortened example);\n"
        "- 2 to 8 phases in execution order; ids are short lowercase words; use `depends_on` for order "
        "and to let independent phases run in parallel;\n"
        "- each phase has `name`, a concrete `done_when`, and wherever possible a `check`: a shell "
        "command run from the project root that exits 0 only when the phase is really done "
        "(tests, a file exists, grep for content). Optional: `loop` "
        "{\"max_iterations\": N, \"until\": \"cmd\"}, `on_fail`: \"<phase id>\", `side_effect`: true "
        "for one-shot actions like tagging or deploying.\n"
        f"Shape (example values, replace them):\n```json\n{json.dumps(example, indent=1)}\n```\n"
        f"Check it with `{WRAPPER} validate --project . --flow {p.flow_id}`, then start on the first "
        "phase. Jevflow will not let you stop until the flow is laid out, then it tracks each phase. "
        "Do not edit other files under `.jevflow/`."
    )


def on_user_prompt(payload: Mapping[str, Any], root: Paths, *, env: Mapping[str, str],
                   now: float) -> Dict[str, Any]:
    if env.get("JEVFLOW_FLOW"):
        return {}  # pinned by a supervisor or the user
    sid = payload.get("session_id")
    cur = bound_flow(root, sid)
    if cur is not None and not cur.archived:
        if cur.is_draft:
            ok, err = try_activate(cur)
            if not ok:
                return _ctx(f"Reminder: flow `{cur.flow_id}` still needs its phases ({err}).\n\n"
                            + plan_instructions(cur, _read_draft(cur).get("goal", "")))
        return {}  # this session is working on a flow; follow-ups belong to it
    if cur is None and has_legacy(root):
        return {}  # the project's single .jevflow/flow.json owns unbound sessions
    if not auto_enabled(root, env):
        return {}
    prompt = str(payload.get("prompt") or "")
    cfg = load_config(root)
    if not looks_like_task(prompt, int(cfg.get("min_words", MIN_WORDS) or MIN_WORDS)):
        return {}
    goal = re.sub(r"(?i)(^|\s)#jev(\s|$)", " ", prompt).strip()
    p = new_flow(root, goal, now, sid)
    bind_session(root, sid, p.flow_id)
    return _ctx(plan_instructions(p, goal))


START_MARKER = "JEVFLOW_STARTED"
_START_RE = re.compile(START_MARKER + r" flow=([a-z0-9][a-z0-9._-]{0,80})")


def start_flow(root: Paths, goal: str, now: float) -> Tuple[Paths, str]:
    """``jevflow start``: Claude decided this task deserves a tracked flow.
    Creates the draft and returns the text Claude reads: a marker line the
    PostToolUse hook uses to bind the session, then the planning instructions."""
    os.makedirs(root.base, exist_ok=True)
    gi = os.path.join(root.base, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w", encoding="utf-8") as fh:
            fh.write(GITIGNORE)
    p = new_flow(root, goal.strip(), now)
    return p, f"{START_MARKER} flow={p.flow_id}\n\n" + plan_instructions(p, goal.strip())


def bind_from_tool_output(payload: Mapping[str, Any], root: Paths, text: str) -> Optional[str]:
    """Bind the session to a flow that ``jevflow start`` just printed."""
    m = _START_RE.search(text or "")
    if not m:
        return None
    fid = m.group(1)
    bind_session(root, payload.get("session_id"), fid)
    return fid


GITIGNORE = "sessions/\n**/state.json.lock\n**/lock\n**/runs/\n**/last_run.json\n*.tmp\n"


def start_hint() -> str:
    """One-time SessionStart note for a project with no .jevflow yet."""
    return (
        "Jevflow is installed. For a task that will take several steps and should be finished "
        "and verified (not a question or a one-line change), you may start a tracked flow before "
        f"working: run `{WRAPPER} start --goal \"<the user's request>\"` from the project root, "
        "then follow what it prints. Jevflow then checks each phase before you stop. Skip it for "
        "small or conversational requests.")


def needs_nudge(payload: Mapping[str, Any], root: Paths, env: Mapping[str, str]) -> bool:
    """A project with .jevflow but auto mode off, no pinned flow, no legacy
    flow, and no active flow for this session: nudge instead of auto-creating."""
    if env.get("JEVFLOW_FLOW") or auto_enabled(root, env) or has_legacy(root):
        return False
    cur = bound_flow(root, payload.get("session_id"))
    return cur is None or cur.archived


def prompt_nudge(prompt: str) -> Optional[str]:
    """Per-prompt nudge (no auto mode): when the prompt reads like a multi-step
    task, tell Claude to start a flow before working. Claude still decides."""
    if not looks_like_task(prompt):
        return None
    return (
        "[jevflow] This request looks like a multi-step task with deliverables. Before "
        f"writing any code, start a tracked flow: `{WRAPPER} start --goal \"<the user's request, "
        "verbatim>\"` from the project root, then lay out the phases it asks for. Jevflow then "
        "verifies each phase with its check before you stop. Only skip this if the task is "
        "really a single small edit or a question.")


def _ctx(text: str) -> Dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}


def on_draft_session_start(p: Paths) -> Dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                   "additionalContext": plan_instructions(p, _read_draft(p).get("goal", ""))}}


def on_draft_stop(p: Paths, *, now: float) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Stop while the flow is still a draft. Returns (output, activated). When
    ``activated`` is True the caller runs the normal Stop policy instead."""
    ok, err = try_activate(p)
    if ok:
        return None, True
    d = _read_draft(p)
    n = int(d.get("plan_blocks", 0) or 0) + 1
    if n > PLAN_BLOCK_LIMIT:
        archive(p, now, outcome="abandoned (no flow laid out)")
        return {"systemMessage": f"[jevflow] flow {p.flow_id} was never laid out; archived as "
                                 "abandoned. Jevflow is not tracking this task."}, False
    d["plan_blocks"] = n
    _write_draft(p, d)
    return {"decision": "block",
            "reason": f"[jevflow] Lay out the flow before stopping ({n}/{PLAN_BLOCK_LIMIT}): {err}.\n\n"
                      + plan_instructions(p, d.get("goal", ""))}, False
