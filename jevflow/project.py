"""Project discovery, deterministic checks and change summary.

Everything here runs locally. Nothing in this module talks to Jev.
"""

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .flow import Flow
from .judge import CheckResult

JEVFLOW_DIR = ".jevflow"
FLOW_FILE = "flow.json"
STATE_FILE = "state.json"
NEEDS_HUMAN_FILE = "NEEDS_HUMAN.md"
LOCK_FILE = "lock"
SUBTASKS_FILE = "subtasks.json"
SIDE_EFFECTS_FILE = "side_effects.jsonl"
# multi-flow layout: .jevflow/flows/<id>/ (active), .jevflow/done/<id>/ (archived),
# .jevflow/sessions/<session_id> (which flow a Claude session is working on)
FLOWS_DIR = "flows"
DONE_DIR = "done"
SESSIONS_DIR = "sessions"
CONFIG_FILE = "config.json"
DRAFT_FILE = "draft.json"
SUMMARY_FILE = "SUMMARY.md"
FLOW_ENV = "JEVFLOW_FLOW"            # pin a flow id (the supervisor sets it for its child)
SUPERVISED_ENV = "JEVFLOW_SUPERVISED"
AUTO_ENV = "JEVFLOW_AUTO"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")

CHECK_OUTPUT_KEEP = 4000       # chars of check output kept per check
CHECKS_TOTAL_S = 300.0         # all checks together; leaves room for git + Jev under the 600 s hook timeout
GIT_TOTAL_S = 45.0             # all git calls of one Stop together
GIT_TIMEOUT_S = 20.0
MAX_UNTRACKED = 200
# never hand the Jev key to a user-defined check command
SCRUB_ENV = ("JEV_API_KEY", "JEVFLOW_KEY_FILE")
# set by hooks/jevflow before it prepends the plugin root to PYTHONPATH
ORIG_PYTHONPATH_VAR = "JEVFLOW_ORIG_PYTHONPATH"


@dataclass(frozen=True)
class Paths:
    """Files of one flow. ``flow_id=None`` is the single legacy flow at
    ``.jevflow/flow.json``; otherwise ``.jevflow/flows/<id>/`` (or
    ``.jevflow/done/<id>/`` once archived)."""
    root: str
    flow_id: Optional[str] = None
    archived: bool = False

    @property
    def base(self) -> str:
        return os.path.join(self.root, JEVFLOW_DIR)

    @property
    def dir(self) -> str:
        if self.flow_id is None:
            return self.base
        return os.path.join(self.base, DONE_DIR if self.archived else FLOWS_DIR, self.flow_id)

    @property
    def flow(self) -> str:
        return os.path.join(self.dir, FLOW_FILE)

    @property
    def state(self) -> str:
        return os.path.join(self.dir, STATE_FILE)

    @property
    def needs_human(self) -> str:
        return os.path.join(self.dir, NEEDS_HUMAN_FILE)

    @property
    def lock(self) -> str:
        return os.path.join(self.dir, LOCK_FILE)

    @property
    def subtasks(self) -> str:
        return os.path.join(self.dir, SUBTASKS_FILE)

    @property
    def side_effects(self) -> str:
        return os.path.join(self.dir, SIDE_EFFECTS_FILE)

    @property
    def draft(self) -> str:
        return os.path.join(self.dir, DRAFT_FILE)

    @property
    def is_draft(self) -> bool:
        """An auto-created flow whose phases have not been laid out yet."""
        return self.flow_id is not None and (
            os.path.isfile(self.draft) or not os.path.isfile(self.flow))


def find_project(start: Optional[str], env: Optional[Mapping[str, str]] = None) -> Optional[Paths]:
    """Directory holding ``.jevflow/flow.json``. When Claude Code sets
    ``CLAUDE_PROJECT_DIR`` only that directory counts (a flow in some parent
    directory must not capture an unrelated project). Otherwise ``start``
    and its parents. Paths are realpath'd (Claude reports the resolved cwd).
    Returns None when there is no flow (plugin inactive)."""
    env = os.environ if env is None else env
    candidates: List[str] = []
    if env.get("CLAUDE_PROJECT_DIR"):
        candidates.append(env["CLAUDE_PROJECT_DIR"])
    elif start:
        cur = os.path.realpath(start)
        while True:
            candidates.append(cur)
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
    for c in candidates:
        root = os.path.realpath(c)
        base = os.path.join(root, JEVFLOW_DIR)
        if (os.path.isfile(os.path.join(base, FLOW_FILE))
                or os.path.isdir(os.path.join(base, FLOWS_DIR))
                or os.path.isdir(os.path.join(base, DONE_DIR))
                or os.path.isfile(os.path.join(base, CONFIG_FILE))):
            return Paths(root)
    return None


# ------------------------------------------------------------ multiple flows

def has_legacy(root: Paths) -> bool:
    return os.path.isfile(os.path.join(root.base, FLOW_FILE))


def valid_id(flow_id: Any) -> bool:
    return isinstance(flow_id, str) and bool(_ID_RE.match(flow_id))


def flow_paths(root: Paths, flow_id: str) -> Optional[Paths]:
    """Active flow ``flow_id``, else the archived one, else None."""
    if not valid_id(flow_id):
        return None
    for archived in (False, True):
        p = Paths(root.root, flow_id, archived)
        if os.path.isdir(p.dir):
            return p
    return None


def _session_file(root: Paths, session_id: str) -> Optional[str]:
    sid = re.sub(r"[^A-Za-z0-9_-]", "", str(session_id or ""))[:80]
    return os.path.join(root.base, SESSIONS_DIR, sid) if sid else None


def bound_flow(root: Paths, session_id: Optional[str]) -> Optional[Paths]:
    f = _session_file(root, session_id) if session_id else None
    if not f:
        return None
    try:
        with open(f, encoding="utf-8") as fh:
            return flow_paths(root, fh.read().strip())
    except OSError:
        return None


def bind_session(root: Paths, session_id: Optional[str], flow_id: str) -> None:
    f = _session_file(root, session_id) if session_id else None
    if not f:
        return
    os.makedirs(os.path.dirname(f), exist_ok=True)
    tmp = f + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(flow_id + "\n")
    os.replace(tmp, f)


def resolve(root: Paths, session_id: Optional[str] = None,
            env: Optional[Mapping[str, str]] = None, flow_id: Optional[str] = None) -> Optional[Paths]:
    """The flow this caller works on: an explicit id, then ``$JEVFLOW_FLOW``,
    then the session's binding, then the legacy ``.jevflow/flow.json``."""
    env = os.environ if env is None else env
    for fid in (flow_id, env.get(FLOW_ENV)):
        if fid:
            return flow_paths(root, fid)
    b = bound_flow(root, session_id)
    if b is not None:
        return b
    return Paths(root.root) if has_legacy(root) else None


def list_flows(root: Paths) -> List[Tuple[Paths, float]]:
    """Every flow (active first, then archived), newest first within each."""
    out: List[Tuple[Paths, float]] = []
    for archived in (False, True):
        d = os.path.join(root.base, DONE_DIR if archived else FLOWS_DIR)
        try:
            names = os.listdir(d)
        except OSError:
            continue
        group = []
        for n in names:
            p = Paths(root.root, n, archived)
            if valid_id(n) and os.path.isdir(p.dir):
                mt = max((_mtime(x) for x in (p.state, p.flow, p.draft, p.dir)), default=0.0)
                group.append((p, mt))
        out += sorted(group, key=lambda t: -t[1])
    return out


def default_flow(root: Paths) -> Optional[Paths]:
    """For viewers with no session: legacy, else newest active, else newest archived."""
    if has_legacy(root):
        return Paths(root.root)
    flows = list_flows(root)
    return flows[0][0] if flows else None


def _mtime(path: str) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def load_config(root: Paths) -> Dict[str, Any]:
    try:
        with open(os.path.join(root.base, CONFIG_FILE), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(root: Paths, cfg: Mapping[str, Any]) -> None:
    os.makedirs(root.base, exist_ok=True)
    path = os.path.join(root.base, CONFIG_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(dict(cfg), fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def auto_enabled(root: Paths, env: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if env is None else env
    v = env.get(AUTO_ENV)
    if v is not None and v != "":
        return v not in ("0", "false", "no", "off")
    return bool(load_config(root).get("auto"))


def slugify(text: str, words: int = 5) -> str:
    toks = re.findall(r"[a-z0-9]+", text.lower())
    stop = {"a", "an", "the", "and", "or", "to", "of", "for", "in", "on", "with", "please", "can",
            "you", "me", "my", "i", "it", "that", "this", "is", "be", "we", "our"}
    keep = [t for t in toks if t not in stop][:words] or ["flow"]
    return "-".join(keep)[:48].strip("-") or "flow"


def new_flow(root: Paths, goal: str, now: float, session_id: Optional[str] = None) -> Paths:
    """Create ``.jevflow/flows/<date>-<slug>/`` as a draft (goal only)."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now))
    base_id = f"{stamp}-{slugify(goal)}"
    fid, n = base_id, 2
    while os.path.exists(Paths(root.root, fid).dir) or os.path.exists(Paths(root.root, fid, True).dir):
        fid, n = f"{base_id}-{n}", n + 1
    p = Paths(root.root, fid)
    os.makedirs(p.dir)
    with open(p.draft, "w", encoding="utf-8") as fh:
        json.dump({"goal": goal, "created_at": now, "session_id": session_id,
                   "plan_blocks": 0}, fh, indent=1)
    return p


def archive(p: Paths, now: float, outcome: str = "complete") -> Optional[Paths]:
    """Move an active flow to ``.jevflow/done/<id>/`` and write SUMMARY.md.
    Returns the archived Paths, or None if there was nothing to move."""
    if p.flow_id is None or p.archived or not os.path.isdir(p.dir):
        return None
    dst = Paths(p.root, p.flow_id, True)
    try:
        with open(os.path.join(p.dir, SUMMARY_FILE), "w", encoding="utf-8") as fh:
            fh.write(summary_markdown(p, now, outcome))
    except OSError:
        pass
    os.makedirs(os.path.dirname(dst.dir), exist_ok=True)
    shutil.move(p.dir, dst.dir)
    return dst


def summary_markdown(p: Paths, now: float, outcome: str) -> str:
    def load(path: str) -> Dict[str, Any]:
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}
    flow, state, draft = load(p.flow), load(p.state), load(p.draft)
    goal = flow.get("goal") or draft.get("goal") or "?"
    started = state.get("started_at") or draft.get("created_at") or now
    hist = state.get("history") if isinstance(state.get("history"), list) else []
    stops = [h for h in hist if isinstance(h, dict) and h.get("event") == "stop"]
    counts: Dict[str, int] = {}
    for h in stops:
        counts[str(h.get("decision"))] = counts.get(str(h.get("decision")), 0) + 1
    ts = lambda t: time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(float(t)))  # noqa: E731
    lines = [f"# {p.flow_id}", "", f"Outcome: **{outcome}**", "",
             f"Goal: {goal}", "",
             f"Started {ts(started)}, archived {ts(now)} ({(now - float(started)) / 60:.1f} min).",
             f"Jev calls: {state.get('jev_calls', 0)}. Restarts: {state.get('restarts', 0)}. "
             f"Stops: {', '.join(f'{k} {v}' for k, v in sorted(counts.items())) or 'none'}.",
             "", "| Phase | Status | Done when |", "| --- | --- | --- |"]
    status = state.get("phase_status") if isinstance(state.get("phase_status"), dict) else {}
    for ph in flow.get("phases") or []:
        if isinstance(ph, dict):
            lines.append(f"| {ph.get('id')} | {status.get(ph.get('id'), 'pending')} | "
                         f"{str(ph.get('done_when', '')).replace('|', '/')} |")
    return "\n".join(lines) + "\n"


def _check_env() -> Dict[str, str]:
    """Environment for user check commands: the caller's env minus the Jev key,
    with PYTHONPATH restored to what the user had before the jevflow wrapper
    prepended the plugin root (otherwise the plugin's own ``tests`` package
    shadows the project's)."""
    env = {k: v for k, v in os.environ.items() if k not in SCRUB_ENV}
    if ORIG_PYTHONPATH_VAR in env:
        orig = env.pop(ORIG_PYTHONPATH_VAR)
        if orig:
            env["PYTHONPATH"] = orig
        else:
            env.pop("PYTHONPATH", None)
    return env


def run_check(cmd: str, cwd: str, timeout: float) -> CheckResult:
    """Run one shell check. Exit 0 passes. Never raises."""
    if timeout <= 0:
        return CheckResult(False, "not run: check time budget for this Stop is used up")
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, env=_check_env(),
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout)
    except subprocess.TimeoutExpired:
        return CheckResult(False, f"check timed out after {timeout:.0f}s: {cmd}")
    except OSError as exc:
        return CheckResult(False, f"check could not start: {exc}")
    out = p.stdout.decode("utf-8", errors="replace")
    if len(out) > CHECK_OUTPUT_KEEP:
        out = "..." + out[-(CHECK_OUTPUT_KEEP - 3):]
    return CheckResult(p.returncode == 0, out)


def checks_to_run(flow: Flow, state: Mapping[str, Any], now: Optional[float] = None) -> List[str]:
    """Phases whose ``check`` runs on this Stop: done phases (regression
    invariants, branch-only excluded) plus the current phase."""
    status = state.get("phase_status", {})
    cur = state.get("current_phase")
    # at a budget or cap: also run pending phases' checks so the policy can
    # record work that is already finished (policy.settle_by_checks)
    from .policy import cap_reached
    settling = cap_reached(flow, state, now=time.time() if now is None else now) is not None
    out = []
    for p in flow.phases:
        if p.check is None:
            continue
        if p.id == cur or (status.get(p.id) == "done" and p.id not in flow.branch_only):
            out.append(p.id)
        elif (settling and p.id not in flow.branch_only and p.loop is None
              and not p.side_effect and not p.dynamic):
            out.append(p.id)
    return out


def run_checks(flow: Flow, state: Mapping[str, Any], cwd: str,
               clock=time.monotonic) -> tuple:
    """Return ``(checks, loop_checks)`` for policy.decide."""
    per = float(flow.limits.get("check_timeout_s", 120))
    deadline = clock() + CHECKS_TOTAL_S
    checks: Dict[str, CheckResult] = {}
    for pid in checks_to_run(flow, state):
        checks[pid] = run_check(flow.phase(pid).check, cwd, min(per, deadline - clock()))
    for p in flow.phases:
        if p.check is None and p.id not in checks:
            checks[p.id] = CheckResult(None)
    loop_checks: Dict[str, CheckResult] = {}
    cur = state.get("current_phase")
    if cur in flow.ids and flow.phase(cur).loop is not None:
        loop_checks[cur] = run_check(flow.phase(cur).loop.until, cwd, min(per, deadline - clock()))
    return checks, loop_checks


def _git(args: List[str], cwd: str, timeout: float = GIT_TIMEOUT_S) -> Optional[str]:
    if timeout <= 0:
        return None
    try:
        p = subprocess.run(["git", *args], cwd=cwd, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=min(timeout, GIT_TIMEOUT_S), env=_check_env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    if p.returncode != 0:
        return None
    return p.stdout.decode("utf-8", errors="replace")


def _count_lines(path: str) -> int:
    try:
        with open(path, "rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def git_changes(cwd: str, send_diff: bool = False, clock=time.monotonic) -> List[Dict[str, Any]]:
    """Uncommitted changes as ``{"path", "added", "removed"[, "diff"]}``.
    Counts are computed locally; ``diff`` text is only read when
    ``send_diff`` is true. Empty list outside a git repo. Never raises."""
    deadline = clock() + GIT_TOTAL_S

    def left() -> float:
        return deadline - clock()

    out = _git(["diff", "--numstat", "--relative", "HEAD"], cwd, left())
    if out is None:  # no commits yet: only staged changes can be listed
        out = _git(["diff", "--numstat", "--relative", "--cached"], cwd, left())
        if out is None:
            return []
    entries: List[Dict[str, Any]] = []
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        a, r, path = parts
        entries.append({"path": path, "added": int(a) if a.isdigit() else 0,
                        "removed": int(r) if r.isdigit() else 0})
    untracked = _git(["ls-files", "--others", "--exclude-standard"], cwd, left()) or ""
    for path in untracked.splitlines()[:MAX_UNTRACKED]:
        if path.startswith(JEVFLOW_DIR + "/"):
            continue
        entries.append({"path": path, "added": _count_lines(os.path.join(cwd, path)), "removed": 0})
    entries = [e for e in entries if not e["path"].startswith(JEVFLOW_DIR + "/")]
    if send_diff:
        for e in entries[:40]:
            d = _git(["diff", "HEAD", "--", e["path"]], cwd, left())
            if d:
                e["diff"] = d[:4000]
    return entries
