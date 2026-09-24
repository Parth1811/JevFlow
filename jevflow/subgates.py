"""Subagent and task gates (SPEC 10.4, extension, off by default).

SubagentStop and TaskCompleted run a judge scoped to the subtask instead of
the whole flow: the subtask text (the task subject and description, or the
subagent's first prompt) plus the flow goal for context, and the subagent's
last message. Two nouls:

- ``complete``: the latest message shows the subtask fully done
- ``claims_done``: the latest message claims it is done

There is no deterministic check for a subtask, so the gate is deliberately
narrow: it only blocks a premature completion claim, that is ``claims_done``
at or above ``flag`` AND ``complete`` at or below ``1 - auto`` (Jev is
confident the work is not done). Anything else, including any Jev failure or
a subagent that honestly reports it could not finish, is allowed. Each
subtask can be held at most ``SUBTASK_BLOCK_CAP`` times, so a gate can never
loop a subagent forever.

Pure functions here; hooks.py does the I/O.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

from .jev_client import JevError

SUBTASK_CHARS = 2000
GOAL_CHARS = 600
MESSAGE_CHARS = 4000
SUBTASK_BLOCK_CAP = 2
TRANSCRIPT_SCAN_BYTES = 256 * 1024

Q_COMPLETE = "The latest message shows that this subtask is fully completed: {task}"
Q_CLAIMS = "The latest message claims that the subtask is complete"

ALLOW, BLOCK = "allow", "block"


@dataclass
class SubtaskResult:
    verdict: str = ALLOW
    reason: str = ""
    condition: str = ""
    probs: Dict[str, float] = field(default_factory=dict)
    calls: int = 0
    degraded: bool = False
    error: Optional[str] = None


def _head(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 3] + "..."


def _tail(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else "..." + text[-(n - 3):]


def first_prompt(transcript_path: Optional[str]) -> str:
    """First user text of a subagent transcript (its task), or "".

    Reads at most TRANSCRIPT_SCAN_BYTES of JSON lines. Never raises."""
    if not transcript_path:
        return ""
    try:
        with open(transcript_path, "rb") as fh:
            raw = fh.read(TRANSCRIPT_SCAN_BYTES)
    except OSError:
        return ""
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        msg = e.get("message") if isinstance(e.get("message"), dict) else e
        if (e.get("type") or msg.get("role")) not in ("user",):
            continue
        text = _content_text(msg.get("content"))
        if text.strip():
            return text
    return ""


def last_assistant_text(transcript_path: Optional[str]) -> str:
    """Text of the newest assistant message in the tail of a transcript, or "".

    TaskCompleted carries no message field, so the transcript is the only
    evidence. It may lag the live turn; an empty result allows. Never raises."""
    if not transcript_path:
        return ""
    try:
        with open(transcript_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - TRANSCRIPT_SCAN_BYTES))
            raw = fh.read(TRANSCRIPT_SCAN_BYTES)
    except OSError:
        return ""
    for line in reversed(raw.decode("utf-8", "replace").splitlines()):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        msg = e.get("message") if isinstance(e.get("message"), dict) else e
        if (e.get("type") or msg.get("role")) != "assistant":
            continue
        text = _content_text(msg.get("content"))
        if text.strip():
            return text
    return ""


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content
                         if isinstance(c, dict) and c.get("type") == "text" and c.get("text"))
    return ""


def compose(p: Mapping[str, float], b: Mapping[str, float]) -> SubtaskResult:
    """Pure verdict. Blocks only a confident premature completion claim."""
    complete, claims = p.get("complete", 1.0), p.get("claims_done", 0.0)
    auto, flag = b.get("auto", 0.80), b.get("flag", 0.70)
    if claims >= flag and complete <= round(1.0 - auto, 9):
        return SubtaskResult(BLOCK, "The subtask is reported done, but the result does not "
                             f"show it is (complete {complete:.2f}, claims done {claims:.2f}). "
                             "Finish it, or say plainly what is left.", "subtask_premature",
                             dict(p))
    return SubtaskResult(ALLOW, "", "subtask_ok", dict(p))


def _noul(ans: Mapping[str, Any], name: str) -> float:
    v = ans[name]["noul"]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(name)
    return max(0.0, min(1.0, float(v)))


def judge_subtask(client: Any, *, task: str, goal: str, last_message: str,
                  b: Mapping[str, float]) -> SubtaskResult:
    """Ask Jev and compose. Any failure or an empty task allows. Never raises."""
    if client is None:
        return SubtaskResult(ALLOW, "", "degraded", degraded=True, error="no Jev client")
    task = _head(task, SUBTASK_CHARS)
    if not task.strip() or not (last_message or "").strip():
        return SubtaskResult(ALLOW, "", "no_evidence")
    before = getattr(client, "calls_made", 0)
    doc = json.dumps({"flow_goal": _head(goal, GOAL_CHARS), "subtask": task,
                      "latest_message": _tail(last_message, MESSAGE_CHARS)},
                     separators=(",", ":"), ensure_ascii=False)
    questions = {"complete": {"type": "noul", "instructions": Q_COMPLETE.format(task=_head(task, 300))},
                 "claims_done": {"type": "noul", "instructions": Q_CLAIMS}}
    try:
        ans = client.ask(doc, questions)
        p = {"complete": _noul(ans, "complete"), "claims_done": _noul(ans, "claims_done")}
    except JevError as exc:
        r = SubtaskResult(ALLOW, "", "degraded", degraded=True, error=type(exc).__name__)
    except (KeyError, TypeError, ValueError, AttributeError):
        r = SubtaskResult(ALLOW, "", "degraded", degraded=True, error="malformed Jev answers")
    else:
        r = compose(p, b)
    r.calls = max(0, getattr(client, "calls_made", before) - before)
    return r
