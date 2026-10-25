"""Per-user Jevflow home: which folders on this computer have flows, and which
sessions have already sent a prompt.

Each project keeps its own ``.jevflow/`` (it stays its own workspace). The
registry only remembers *where* those folders are, so ``jevflow ui`` started
from any directory can list every flow on the machine. Apps such as Claude
Cowork create a fresh, randomly named folder per task, which is hard to find
by hand; hooks register each folder the first time they see it.

Home: ``$JEVFLOW_HOME``, else ``$XDG_CONFIG_HOME/jevflow``, else
``~/.config/jevflow``. Nothing here is required for a flow to work: every
function swallows OSError and returns a neutral value.
"""

import fcntl
import json
import os
import time
from typing import Dict, Iterable, List, Optional

PROJECTS_FILE = "projects.json"
SESSIONS_FILE = "sessions.json"
REFRESH_S = 3600.0          # re-stamp a known project at most once an hour
MAX_PROJECTS = 500
MAX_SESSIONS = 400
SCAN_DEPTH = 4
SCAN_MAX_DIRS = 20000
SKIP_DIRS = frozenset({"node_modules", "Library", "venv", "env", "__pycache__", "site-packages",
                       "build", "dist", "target", "Applications", "Pictures", "Music", "Movies"})
DEFAULT_SCAN = ("~/Documents", "~/Desktop", "~/Projects", "~/projects", "~/code", "~/src", "~/dev",
                "~/Developer", "~/workplace", "~/Claude", "~/repos", "~/git")


def home(env: Optional[Dict[str, str]] = None) -> str:
    env = os.environ if env is None else env
    if env.get("JEVFLOW_HOME"):
        return os.path.expanduser(env["JEVFLOW_HOME"])
    base = env.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "jevflow")


def _read(path: str) -> Dict:
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _update(name: str, fn) -> None:
    """Locked read-modify-write of one JSON file in the home dir."""
    d = home()
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name)
    with open(path + ".lock", "a+") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        data = _read(path)
        if fn(data) is False:
            return
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, path)


def _has_jevflow(root: str) -> bool:
    return os.path.isdir(os.path.join(root, ".jevflow"))


def register(root: str, now: Optional[float] = None) -> None:
    """Remember a project folder. Cheap when already known (no write)."""
    now = time.time() if now is None else now
    root = os.path.realpath(root)
    known = _read(os.path.join(home(), PROJECTS_FILE)).get("projects") or {}
    entry = known.get(root) if isinstance(known, dict) else None
    if isinstance(entry, dict) and now - float(entry.get("seen") or 0) < REFRESH_S:
        return

    def fn(data: Dict) -> None:
        ps = data.setdefault("projects", {})
        if not isinstance(ps, dict):
            ps = data["projects"] = {}
        ps[root] = {"seen": now}
        for k in [k for k in ps if not _has_jevflow(k)]:
            ps.pop(k, None)  # folder deleted, or .jevflow removed
        if len(ps) > MAX_PROJECTS:
            for k, _ in sorted(ps.items(), key=lambda kv: kv[1].get("seen", 0))[: len(ps) - MAX_PROJECTS]:
                ps.pop(k, None)
    try:
        _update(PROJECTS_FILE, fn)
    except OSError:
        pass


def registered() -> List[str]:
    """Registered folders that still have a .jevflow, most recently seen first."""
    ps = _read(os.path.join(home(), PROJECTS_FILE)).get("projects") or {}
    if not isinstance(ps, dict):
        return []
    rows = sorted(ps.items(), key=lambda kv: -float((kv[1] or {}).get("seen", 0) or 0))
    return [k for k, _ in rows if _has_jevflow(k)]


def scan(dirs: Iterable[str], depth: int = SCAN_DEPTH, max_dirs: int = SCAN_MAX_DIRS) -> List[str]:
    """Folders under ``dirs`` (to ``depth`` levels) holding a ``.jevflow``.
    Finds projects whose hooks ran somewhere the registry cannot see (for
    example a Cowork sandbox writing into a folder shared from this disk)."""
    found: List[str] = []
    budget = [max_dirs]

    def walk(d: str, left: int) -> None:
        if budget[0] <= 0:
            return
        budget[0] -= 1
        try:
            with os.scandir(d) as it:
                entries = [e for e in it if e.is_dir(follow_symlinks=False)]
        except OSError:
            return
        if any(e.name == ".jevflow" for e in entries):
            found.append(os.path.realpath(d))
        if left <= 0:
            return
        for e in entries:
            if e.name.startswith(".") or e.name in SKIP_DIRS:
                continue
            walk(e.path, left - 1)

    seen = set()
    for raw in dirs:
        d = os.path.realpath(os.path.expanduser(raw))
        if d in seen or not os.path.isdir(d):
            continue
        seen.add(d)
        walk(d, depth)
    return found


def scan_dirs(env: Optional[Dict[str, str]] = None) -> List[str]:
    """``$JEVFLOW_SCAN`` (os.pathsep list; empty string turns scanning off),
    else the usual project folders under the home directory."""
    env = os.environ if env is None else env
    if "JEVFLOW_SCAN" in env:
        return [p for p in env["JEVFLOW_SCAN"].split(os.pathsep) if p.strip()]
    return list(DEFAULT_SCAN)


def first_prompt(session_id: Optional[str], now: Optional[float] = None) -> bool:
    """True the first time a session sends a prompt (then remembered).
    Unknown session ids count as first, so the nudge errs on showing."""
    if not session_id:
        return True
    now = time.time() if now is None else now
    out = [True]

    def fn(data: Dict):
        ss = data.setdefault("sessions", {})
        if not isinstance(ss, dict):
            ss = data["sessions"] = {}
        if session_id in ss:
            out[0] = False
            return False  # nothing to write
        ss[session_id] = now
        if len(ss) > MAX_SESSIONS:
            for k, _ in sorted(ss.items(), key=lambda kv: kv[1])[: len(ss) - MAX_SESSIONS]:
                ss.pop(k, None)
    try:
        _update(SESSIONS_FILE, fn)
    except OSError:
        pass
    return out[0]
