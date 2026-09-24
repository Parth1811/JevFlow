"""Project discovery, deterministic checks and change summary.

Everything here runs locally. Nothing in this module talks to Jev.
"""

import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional

from .flow import Flow
from .judge import CheckResult

JEVFLOW_DIR = ".jevflow"
FLOW_FILE = "flow.json"
STATE_FILE = "state.json"
NEEDS_HUMAN_FILE = "NEEDS_HUMAN.md"
LOCK_FILE = "lock"

CHECK_OUTPUT_KEEP = 4000       # chars of check output kept per check
CHECKS_TOTAL_S = 300.0         # all checks together; leaves room for git + Jev under the 600 s hook timeout
GIT_TOTAL_S = 45.0             # all git calls of one Stop together
GIT_TIMEOUT_S = 20.0
MAX_UNTRACKED = 200
# never hand the Jev key to a user-defined check command
SCRUB_ENV = ("JEV_API_KEY", "JEVFLOW_KEY_FILE")


@dataclass(frozen=True)
class Paths:
    root: str

    @property
    def dir(self) -> str:
        return os.path.join(self.root, JEVFLOW_DIR)

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
        if os.path.isfile(os.path.join(root, JEVFLOW_DIR, FLOW_FILE)):
            return Paths(root)
    return None


def _check_env() -> Dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in SCRUB_ENV}


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


def checks_to_run(flow: Flow, state: Mapping[str, Any]) -> List[str]:
    """Phases whose ``check`` runs on this Stop: done phases (regression
    invariants, branch-only excluded) plus the current phase."""
    status = state.get("phase_status", {})
    cur = state.get("current_phase")
    out = []
    for p in flow.phases:
        if p.check is None:
            continue
        if p.id == cur or (status.get(p.id) == "done" and p.id not in flow.branch_only):
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
