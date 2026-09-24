"""Supervisor: ``python -m jevflow run`` (SPEC 6, 10.2, 10.5).

The restart guarantee. Launches ``claude -p`` in the project, waits, reads
``.jevflow/state.json`` and decides in code whether to stop or relaunch:

- goal done                              -> exit 0
- ``needs_human`` set (ask_human)        -> exit 4, never relaunched
- restarts, time or Jev budget used up   -> exit 2 with a budget report
- configuration error (flow, binary)     -> exit 3
- another live supervisor holds the lease -> exit 5
- StopFailure recorded a transient API error (rate_limit, overloaded,
  server_error) during the run -> relaunch after exponential backoff; this
  is NOT counted against ``max_restarts`` (capped separately)
- no transcript/state progress for ``limits.hang_minutes`` -> kill the
  child's process group and relaunch (counted as a restart)

Single-runner lease: ``.jevflow/lock`` holds pid + timestamp and an
exclusive ``flock``. The kernel drops the flock when the holder dies, so a
lock file left behind by a crashed supervisor is stale and is taken over;
one held by a live process is refused.

The Jev key is never read here. The child inherits the environment so the
hooks can find the key the same way the user configured it.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, IO, List, Optional, Sequence

from .flow import Flow, FlowError, load_flow
from .project import Paths, find_project
from .state import StateError, load_state, record

try:  # POSIX only; the plugin targets Linux and macOS
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

EXIT_DONE = 0
EXIT_LIMIT = 2
EXIT_CONFIG = 3
EXIT_HUMAN = 4
EXIT_LEASE = 5

CLAUDE_BIN_ENV = "JEVFLOW_CLAUDE_BIN"
TRANSIENT_ERRORS = ("rate_limit", "overloaded", "server_error")
BACKOFF_BASE_S = 30.0
BACKOFF_CAP_S = 600.0
MAX_API_FAILURES = 6           # consecutive transient failures before giving up
KILL_GRACE_S = 10.0
OVERTIME_GRACE_S = 60.0        # past max_total_minutes, time for the last Stop hook to finish
PROMPT_REASON_CHARS = 1500
RUNS_DIR = "runs"
REPORT_FILE = "last_run.json"

DEFAULT_PROMPT = (
    "Work toward the Jevflow goal of this project. The session context lists the "
    "goal, the phase table and the current phase. Complete the current phase, then "
    "continue phase by phase until the whole goal is done. Stop only when finished."
)


class LeaseHeld(RuntimeError):
    """Another live supervisor holds the lease for this project."""


# ---------------------------------------------------------------- lease

class Lease:
    """Exclusive single-runner lease on ``.jevflow/lock`` (flock + pid/ts)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._fh: Optional[IO[str]] = None
        self.took_over: Optional[Dict[str, Any]] = None

    def acquire(self, now: float) -> "Lease":
        if fcntl is None:  # pragma: no cover
            raise LeaseHeld("file locking is not available on this platform")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.seek(0)
            holder = fh.read().strip()
            fh.close()
            raise LeaseHeld(f"another supervisor holds {self.path}: {holder or 'unknown'}") from None
        fh.seek(0)
        prev = fh.read().strip()
        if prev:
            try:
                self.took_over = json.loads(prev)
            except ValueError:
                self.took_over = {"raw": prev[:200]}
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps({"pid": os.getpid(), "host": socket.gethostname(),
                             "ts": round(now, 3)}) + "\n")
        fh.flush()
        self._fh = fh
        return self

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.flush()
        except OSError:
            pass
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._fh.close()
        self._fh = None


# ---------------------------------------------------------------- config

@dataclass
class RunConfig:
    project: str
    plugin_dir: str
    claude_bin: str = "claude"
    prompt: str = DEFAULT_PROMPT
    max_turns: Optional[int] = None
    permission_mode: Optional[str] = "acceptEdits"
    extra_args: List[str] = field(default_factory=list)
    poll_s: float = 2.0
    hang_s: Optional[float] = None          # default: flow limits.hang_minutes
    backoff_base_s: float = BACKOFF_BASE_S
    backoff_cap_s: float = BACKOFF_CAP_S
    max_api_failures: int = MAX_API_FAILURES
    as_json: bool = False
    overtime_grace_s: float = OVERTIME_GRACE_S


@dataclass
class Report:
    exit_code: int = EXIT_CONFIG
    outcome: str = "config_error"
    detail: str = ""
    runs: int = 0
    restarts: int = 0
    api_backoffs: int = 0
    hang_kills: int = 0
    overtime_kills: int = 0
    permission_denials: int = 0
    elapsed_min: float = 0.0
    jev_calls: int = 0
    current_phase: Optional[str] = None
    phase_status: Dict[str, str] = field(default_factory=dict)
    limits: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)

    def text(self) -> str:
        lim = self.limits
        lines = [
            f"jevflow run: {self.outcome} (exit {self.exit_code})",
            f"  {self.detail}" if self.detail else "",
            f"  runs {self.runs}, restarts {self.restarts}/{lim.get('max_restarts')}, "
            f"api backoffs {self.api_backoffs}, hang kills {self.hang_kills}, "
            f"overtime kills {self.overtime_kills}",
            (f"  claude permission denials {self.permission_denials} "
             "(tool calls refused; see .jevflow/runs/ and docs on --permission-mode)")
            if self.permission_denials else "",
            f"  elapsed {self.elapsed_min:.1f}/{lim.get('max_total_minutes')} min, "
            f"jev calls {self.jev_calls}/{lim.get('max_jev_calls')}",
            f"  current phase {self.current_phase}: "
            + ", ".join(f"{k}={v}" for k, v in self.phase_status.items()),
        ]
        return "\n".join(line for line in lines if line) + "\n"


# ---------------------------------------------------------------- helpers

def build_prompt(flow: Flow, state: Dict[str, Any], base: str) -> str:
    """Relaunch prompt from the current phase and the last BLOCK reason."""
    cur = state.get("current_phase")
    parts = [base, "", f"Goal: {flow.goal}"]
    if cur in flow.ids:
        p = flow.phase(cur)
        parts.append(f"Current phase: {p.id} ({p.name}). Done when: {p.done_when}.")
        if p.check:
            parts.append(f"Its check (must exit 0): {p.check}")
    reason = state.get("last_block_reason")
    if reason:
        parts.append("Last Jevflow instruction: " + str(reason)[:PROMPT_REASON_CHARS])
    return "\n".join(parts)


def _mtime(path: Optional[str]) -> float:
    if not path:
        return 0.0
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _kill_group(proc: "subprocess.Popen[bytes]", grace: float) -> None:
    """SIGTERM the child's process group, then SIGKILL after ``grace``."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue
    proc.wait()


def _parse_result(path: str) -> Dict[str, Any]:
    """Last JSON object printed by ``claude -p --output-format json``."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return {}
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                return obj
    return {}


# ---------------------------------------------------------------- supervisor

class Supervisor:
    def __init__(self, cfg: RunConfig, *, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 env: Optional[Dict[str, str]] = None) -> None:
        self.cfg = cfg
        self.clock = clock
        self.sleep = sleep
        self.env = dict(os.environ if env is None else env)
        self.report = Report()
        self.paths: Optional[Paths] = None
        self.flow: Optional[Flow] = None

    # -- state
    def _load(self) -> Dict[str, Any]:
        assert self.paths is not None and self.flow is not None
        return load_state(self.paths.state, self.flow)

    def _journal(self, event: str, **extra: Any) -> Dict[str, Any]:
        state = self._load()
        record(self.paths.state, state, event, now=self.clock(), **extra)
        return state

    def _finish(self, code: int, outcome: str, detail: str,
                state: Optional[Dict[str, Any]] = None) -> Report:
        r = self.report
        r.exit_code, r.outcome, r.detail = code, outcome, detail
        if self.flow is not None and self.paths is not None:
            try:
                state = state if state is not None else self._load()
                r.restarts = int(state.get("restarts", 0))
                r.jev_calls = int(state.get("jev_calls", 0))
                r.current_phase = state.get("current_phase")
                r.phase_status = dict(state.get("phase_status", {}))
                r.elapsed_min = (self.clock() - float(state.get("started_at", self.clock()))) / 60.0
                r.limits = {k: self.flow.limits.get(k) for k in
                            ("max_restarts", "max_total_minutes", "max_jev_calls", "hang_minutes")}
                extra: Dict[str, Any] = {}
                if outcome in ("budget", "api_errors"):
                    from .notify import notify as _notify
                    sent = _notify(self.flow, state, self.paths.root, "budget",
                                   condition="supervisor_" + outcome, message=detail,
                                   now=self.clock())
                    if sent is not None:
                        extra["notify"] = sent
                record(self.paths.state, state, "supervisor_end", now=self.clock(), **extra,
                       outcome=outcome, exit_code=code, runs=r.runs,
                       api_backoffs=r.api_backoffs, hang_kills=r.hang_kills,
                       permission_denials=r.permission_denials)
                tmp = os.path.join(self.paths.dir, REPORT_FILE + ".tmp")
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(r.to_dict(), fh, indent=1, sort_keys=True)
                os.replace(tmp, os.path.join(self.paths.dir, REPORT_FILE))
            except (OSError, FlowError, StateError) as exc:
                r.detail += f" (report not fully written: {exc})"
        return r

    def _budget_hit(self, state: Dict[str, Any]) -> Optional[str]:
        lim = self.flow.limits
        elapsed = (self.clock() - float(state.get("started_at", self.clock()))) / 60.0
        if elapsed >= float(lim.get("max_total_minutes", 90)):
            return f"time budget reached ({lim.get('max_total_minutes')} min)"
        if int(state.get("jev_calls", 0)) >= int(lim.get("max_jev_calls", 200)):
            return f"Jev call budget reached ({lim.get('max_jev_calls')})"
        return None

    def _terminal(self, state: Dict[str, Any]) -> Optional[Report]:
        if state.get("done"):
            return self._finish(EXIT_DONE, "goal_complete", "all phases done", state)
        if state.get("needs_human"):
            return self._finish(EXIT_HUMAN, "needs_human",
                                f"waiting on a human, see {self.paths.needs_human}: "
                                f"{state['needs_human']}", state)
        return None

    # -- one child run
    def _launch(self, prompt: str, session_id: Optional[str]) -> Dict[str, Any]:
        cfg = self.cfg
        cmd = [cfg.claude_bin, "-p", prompt, "--plugin-dir", cfg.plugin_dir,
               "--output-format", "json"]
        if session_id:
            cmd += ["--resume", session_id]
        if cfg.max_turns:
            cmd += ["--max-turns", str(cfg.max_turns)]
        if cfg.permission_mode:
            cmd += ["--permission-mode", cfg.permission_mode]
        cmd += cfg.extra_args

        self.report.runs += 1
        n = self.report.runs
        started = self.clock()
        runs = os.path.join(self.paths.dir, RUNS_DIR)
        os.makedirs(runs, exist_ok=True)
        # unique per supervisor invocation, so a rerun never overwrites earlier logs
        stem = os.path.join(runs, f"{int(started)}-{os.getpid()}-{n:03d}")
        out_path, err_path = stem + ".out.json", stem + ".err.txt"
        hang_s = cfg.hang_s if cfg.hang_s is not None else \
            60.0 * float(self.flow.limits.get("hang_minutes", 10))
        state = self._journal("supervisor_launch", run=n, resume=bool(session_id))
        # an active but runaway session must not outlive the time budget
        deadline = float(state.get("started_at", started)) + \
            60.0 * float(self.flow.limits.get("max_total_minutes", 90)) + cfg.overtime_grace_s
        with open(out_path, "wb") as out, open(err_path, "wb") as err:
            proc = subprocess.Popen(cmd, cwd=self.paths.root, env=self.env,
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                    start_new_session=True)
            hung = overtime = False
            try:
                while True:
                    try:
                        proc.wait(timeout=cfg.poll_s)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    transcript = None
                    try:
                        transcript = self._load().get("transcript_path")
                    except (FlowError, StateError):
                        pass
                    last = max(started, _mtime(transcript), _mtime(self.paths.state),
                               _mtime(out_path))
                    if self.clock() - last >= hang_s:
                        hung = True
                        _kill_group(proc, KILL_GRACE_S)
                        break
                    if self.clock() >= deadline:
                        overtime = True
                        _kill_group(proc, KILL_GRACE_S)
                        break
            except BaseException:
                _kill_group(proc, KILL_GRACE_S)  # never orphan the child
                raise
        result = _parse_result(out_path)
        denials = result.get("permission_denials")
        if isinstance(denials, list) and denials:
            # a session that could not write is the usual cause of a stuck
            # or ask_human outcome; count it so the report says so
            self.report.permission_denials += len(denials)
        if overtime:
            self.report.overtime_kills += 1
        return {"returncode": proc.returncode, "hung": hung, "overtime": overtime,
                "started": started,
                "result": result}

    # -- main loop
    def run(self) -> Report:
        cfg = self.cfg
        if not os.path.isdir(cfg.project):
            return self._finish(EXIT_CONFIG, "config_error", f"no such project dir: {cfg.project}")
        self.paths = find_project(cfg.project, env={})
        if self.paths is None or os.path.realpath(self.paths.root) != os.path.realpath(cfg.project):
            self.paths = None
            return self._finish(EXIT_CONFIG, "config_error",
                                f"no .jevflow/flow.json in {cfg.project}")
        try:
            self.flow = load_flow(self.paths.flow)
            state = self._load()
        except (FlowError, StateError) as exc:
            self.flow = None
            return self._finish(EXIT_CONFIG, "config_error", str(exc))
        if not os.path.isdir(cfg.plugin_dir) or not os.path.isfile(
                os.path.join(cfg.plugin_dir, ".claude-plugin", "plugin.json")):
            return self._finish(EXIT_CONFIG, "config_error",
                                f"not a jevflow plugin dir: {cfg.plugin_dir}")

        lease = Lease(self.paths.lock)
        try:
            lease.acquire(self.clock())
        except LeaseHeld as exc:
            self.report.exit_code, self.report.outcome, self.report.detail = \
                EXIT_LEASE, "lease_held", str(exc)
            return self.report  # do not touch state owned by the live supervisor
        try:
            return self._run_locked(lease)
        except KeyboardInterrupt:
            try:
                self._journal("supervisor_interrupted", runs=self.report.runs)
            except (OSError, FlowError, StateError):
                pass
            raise
        finally:
            lease.release()

    def _run_locked(self, lease: Lease) -> Report:
        cfg = self.cfg
        state = self._journal("supervisor_start",
                              took_over_stale_lease=lease.took_over is not None)
        if state.get("needs_human") and not os.path.exists(self.paths.needs_human):
            # the documented way to resolve: delete NEEDS_HUMAN.md and rerun
            state["needs_human"] = None
            state["escalations"] = 0
            state["stuck_streak"] = 0
            record(self.paths.state, state, "human_resolved", now=self.clock())
        term = self._terminal(state)
        if term is not None:
            return term

        max_restarts = int(self.flow.limits.get("max_restarts", 5))
        session_id = state.get("session_id")
        prompt = build_prompt(self.flow, state, cfg.prompt)
        api_failures = 0
        while True:
            hit = self._budget_hit(state)
            if hit:
                return self._finish(EXIT_LIMIT, "budget", hit, state)
            try:
                run = self._launch(prompt, session_id)
            except FileNotFoundError:
                return self._finish(EXIT_CONFIG, "config_error",
                                    f"claude binary not found: {cfg.claude_bin}")
            except PermissionError:
                return self._finish(EXIT_CONFIG, "config_error",
                                    f"claude binary not executable: {cfg.claude_bin}")
            state = self._load()
            res = run["result"]
            sid = res.get("session_id") or state.get("session_id")
            term = self._terminal(state)
            if term is not None:
                return term
            if run["overtime"]:
                return self._finish(EXIT_LIMIT, "budget",
                                    "time budget reached while claude was still running "
                                    "(child stopped)", state)

            err = state.get("last_error") or {}
            api_error = (isinstance(err, dict) and err.get("error") in TRANSIENT_ERRORS
                         and float(err.get("ts") or 0) >= run["started"])
            if run["hung"]:
                self.report.hang_kills += 1
                why = "hang"
            elif api_error:
                why = "api_error"
            else:
                why = "not_done"

            if why == "api_error":
                api_failures += 1
                if api_failures >= cfg.max_api_failures:
                    return self._finish(EXIT_LIMIT, "api_errors",
                                        f"{api_failures} consecutive Claude API failures "
                                        f"(last: {err.get('error')})", state)
                delay = min(cfg.backoff_cap_s, cfg.backoff_base_s * (2 ** (api_failures - 1)))
                left = 60.0 * float(self.flow.limits.get("max_total_minutes", 90)) - \
                    (self.clock() - float(state.get("started_at", self.clock())))
                delay = max(0.0, min(delay, left))
                self.report.api_backoffs += 1
                state = self._journal("supervisor_backoff", error=err.get("error"),
                                      attempt=api_failures, delay_s=round(delay, 1))
                self.sleep(delay)
                state = self._load()
            else:
                api_failures = 0
                if int(state.get("restarts", 0)) >= max_restarts:
                    return self._finish(EXIT_LIMIT, "budget",
                                        f"restart budget reached ({max_restarts}); last exit: {why}",
                                        state)
                state["restarts"] = int(state.get("restarts", 0)) + 1
                record(self.paths.state, state, "supervisor_restart", now=self.clock(),
                       why=why, returncode=run["returncode"], restarts=state["restarts"])

            # a resumed run that produced no result (e.g. unknown session): start fresh
            if session_id and not res and why != "api_error":
                sid = None
            session_id = sid
            prompt = build_prompt(self.flow, state, cfg.prompt)


# ---------------------------------------------------------------- CLI

RUN_USAGE = """usage: python -m jevflow run --project DIR [options]
  --plugin-dir DIR        jevflow plugin root (default: this package's parent)
  --prompt TEXT           first prompt (default: work toward the flow goal)
  --max-turns N           passed to claude -p
  --permission-mode MODE  passed to claude -p (default acceptEdits; 'none' to omit)
  --claude-arg ARG        extra argument for claude (repeatable)
  --poll-seconds S        watchdog poll interval (default 2)
  --json                  print the budget report as JSON
Exit codes: 0 goal complete, 2 limit reached, 3 configuration error,
4 waiting on a human (.jevflow/NEEDS_HUMAN.md), 5 another supervisor is running.
Claude binary: $JEVFLOW_CLAUDE_BIN, else 'claude' on PATH.
"""


def _default_plugin_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def parse_args(argv: Sequence[str], env: Dict[str, str]) -> RunConfig:
    args = list(argv)
    opts: Dict[str, Any] = {"extra": []}
    as_json = False
    while args:
        a = args.pop(0)
        if a.startswith("--") and "=" in a:
            # --opt=value form; needed for values that start with '-', e.g.
            # --claude-arg=--allowedTools
            a, v0 = a.split("=", 1)
            args.insert(0, v0)
            if a == "--json":
                raise ValueError("--json takes no value")
        if a == "--json":
            as_json = True
            continue
        if a in ("-h", "--help"):
            raise SystemExit(0)
        if not args:
            raise ValueError(f"{a} needs a value")
        v = args.pop(0)
        if a == "--project":
            opts["project"] = v
        elif a == "--plugin-dir":
            opts["plugin_dir"] = v
        elif a == "--prompt":
            opts["prompt"] = v
        elif a == "--max-turns":
            opts["max_turns"] = int(v)
            if opts["max_turns"] <= 0:
                raise ValueError("--max-turns must be positive")
        elif a == "--permission-mode":
            opts["permission_mode"] = None if v == "none" else v
        elif a == "--claude-arg":
            opts["extra"].append(v)
        elif a == "--poll-seconds":
            opts["poll_s"] = float(v)
            if not opts["poll_s"] > 0:
                raise ValueError("--poll-seconds must be positive")
        else:
            raise ValueError(f"unknown option {a}")
    if "project" not in opts:
        raise ValueError("--project is required")
    cfg = RunConfig(project=os.path.realpath(opts["project"]),
                    plugin_dir=os.path.realpath(opts.get("plugin_dir") or _default_plugin_dir()),
                    claude_bin=env.get(CLAUDE_BIN_ENV) or "claude",
                    extra_args=opts["extra"])
    for k in ("prompt", "max_turns", "poll_s"):
        if k in opts:
            setattr(cfg, k, opts[k])
    if "permission_mode" in opts:
        cfg.permission_mode = opts["permission_mode"]
    cfg.as_json = as_json
    return cfg


def main(argv: Sequence[str], stdout: IO[str] = sys.stdout, stderr: IO[str] = sys.stderr) -> int:
    try:
        cfg = parse_args(argv, dict(os.environ))
    except SystemExit:
        stdout.write(RUN_USAGE)
        return 0
    except ValueError as exc:
        stderr.write(f"jevflow run: {exc}\n{RUN_USAGE}")
        return EXIT_CONFIG
    if os.environ.get("JEV_API_KEY"):
        # never echo it; the child (and so the agent's Bash tool) inherits the environment
        stderr.write("jevflow run: warning: JEV_API_KEY is set in the environment and will be "
                     "visible to the claude session; prefer JEVFLOW_KEY_FILE\n")
    sup = Supervisor(cfg)

    def _on_term(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt  # unwinds through _launch, which kills the child group

    old = signal.signal(signal.SIGTERM, _on_term)
    try:
        report = sup.run()
    except KeyboardInterrupt:
        stderr.write("jevflow run: interrupted, child stopped\n")
        return 130
    finally:
        signal.signal(signal.SIGTERM, old)
    if cfg.as_json:
        stdout.write(json.dumps(report.to_dict(), sort_keys=True) + "\n")
    else:
        stdout.write(report.text())
    return report.exit_code
