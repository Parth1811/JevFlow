"""``python -m jevflow ui``: a read-only local web viewer for a Jevflow run.

One page (``ui_static/index.html``, no build step, no dependencies) renders
the phase DAG, counters, NEEDS_HUMAN and the decision timeline. It gets data
three ways, so the same file serves every surface:

* live:     ``jevflow ui`` serves it on 127.0.0.1 and streams state.json
            changes over Server-Sent Events. Works in any browser and in the
            Claude Code desktop app's Browser pane (see ``--launch-json``).
* snapshot: ``jevflow ui --export FILE`` writes a self-contained HTML file with
            the data embedded. Open it anywhere, attach it, or click it in the
            desktop app (the Browser pane opens project HTML files).
* offline:  opened with no data (for example from GitHub Pages), the page
            accepts a dropped state.json / flow.json.

The server is loopback-only, rejects foreign Host headers (DNS rebinding),
sends no CORS headers and has no write endpoints.
"""

import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, IO, List, Mapping, Optional

from . import registry
from .flow import FlowError, load_flow, parse_flow
from .project import Paths, default_flow, find_project, flow_paths, has_legacy, list_flows, valid_id

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "ui_static", "index.html")
DEFAULT_PORT = 7788
MAX_HISTORY = 500
MAX_FLOWS = 60                  # flows listed in the switcher (newest first)
EXPORT_FLOWS = 30               # flows embedded in a snapshot export
AGENT_ACTIVE_S = 120            # an agent seen this recently counts as active
POLL_S = 0.5
HEARTBEAT_S = 15.0
NEEDS_HUMAN_CHARS = 4000
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]", "::1")
MAX_ALL_FLOWS = 300             # rows across every project
SCAN_EVERY_S = 60.0             # re-scan the usual project folders this often
DEFAULT_CACHE_S = 3.0           # how long the "newest flow anywhere" pick is reused
UI_STATE = "ui.json"            # background server pid/port, in the Jevflow home

USAGE = """usage: python -m jevflow ui [--project DIR] [--here] [--flow ID] [--port N] [--open]
       python -m jevflow ui --background [--open] [--port N]
       python -m jevflow ui [--project DIR] [--here] --export FILE
       python -m jevflow ui [--project DIR] --launch-json [--port N]
       python -m jevflow ui --stop
  (no flag)      serve a live viewer on http://127.0.0.1:PORT (default $PORT or 7788) showing
                 every flow on this computer: the current folder's first, then every folder
                 Jevflow has seen (and .jevflow folders under ~/Documents, ~/Projects, ...;
                 set JEVFLOW_SCAN to choose the folders, or to "" to turn the scan off).
                 Works from any directory; each folder keeps its own .jevflow.
  --here         only the project at --project / the current directory
  --background   start the viewer detached (or reuse a running one) and print its URL;
                 this is what /jevflow:ui runs so the viewer can start from a chat
  --stop         stop the background viewer
  --open         also open it in the default browser
  --export FILE  write a self-contained HTML snapshot and exit
  --launch-json  add a 'jevflow' preview server to <project>/.claude/launch.json
                 so the Claude Code desktop app can open the viewer in its Browser pane
"""


def _outcome(p: Paths) -> Optional[str]:
    """Archived flows: the outcome line of SUMMARY.md ("complete", "abandoned ...")."""
    try:
        with open(os.path.join(p.dir, "SUMMARY.md"), encoding="utf-8") as fh:
            for line in fh.read(4000).splitlines():
                if line.startswith("Outcome:"):
                    return line.split(":", 1)[1].strip().strip("*").strip()
    except OSError:
        pass
    return None


def project_key(root: str) -> str:
    return hashlib.sha1(os.path.realpath(root).encode("utf-8", "surrogateescape")).hexdigest()[:10]


def flow_key(p: Paths) -> str:
    """Unique across projects: two folders can both have a flow called `docs`."""
    return f"{project_key(p.root)}:{p.flow_id or '(legacy)'}"


def flow_summary(p: Paths, mtime: float, now: Optional[float] = None) -> Dict[str, Any]:
    """One row of the flow switcher. Never raises."""
    now = time.time() if now is None else now
    row: Dict[str, Any] = {"id": p.flow_id or "", "key": flow_key(p), "project": os.path.basename(p.root.rstrip(os.sep)),
                           "project_root": p.root, "legacy": p.flow_id is None, "archived": p.archived,
                           "updated_at": mtime, "title": "", "goal": "", "draft": p.is_draft,
                           "done": False, "current_phase": None, "phases": 0, "phases_done": 0,
                           "agents": 0, "agents_active": 0, "needs_human": False, "outcome": None}
    try:
        with open(p.flow, encoding="utf-8") as fh:
            raw = json.load(fh)
        f = parse_flow(raw)
        row.update(title=f.title, goal=f.goal, phases=len(f.required()))
    except (OSError, ValueError, FlowError):
        try:
            with open(p.draft, encoding="utf-8") as fh:
                row["goal"] = str(json.load(fh).get("goal") or "")
        except (OSError, ValueError, AttributeError):
            pass
    try:
        with open(p.state, encoding="utf-8") as fh:
            st = json.load(fh)
        ps = st.get("phase_status") or {}
        ag = [a for a in (st.get("agents") or {}).values() if isinstance(a, dict)]
        row.update(done=bool(st.get("done")), current_phase=st.get("current_phase"),
                   phases_done=sum(1 for v in ps.values() if v == "done"),
                   needs_human=bool(st.get("needs_human")), agents=len(ag),
                   agents_active=sum(1 for a in ag if now - float(a.get("at", 0) or 0) < AGENT_ACTIVE_S),
                   started_at=st.get("started_at"))
    except (OSError, ValueError, AttributeError):
        pass
    if p.archived:
        row["outcome"] = _outcome(p)
    return row


def all_flows(root: Paths) -> List[Dict[str, Any]]:
    rows = []
    if has_legacy(root):
        rows.append(flow_summary(Paths(root.root), os.path.getmtime(Paths(root.root).flow)))
    for p, mt in list_flows(root)[:MAX_FLOWS]:
        rows.append(flow_summary(p, mt))
    return rows


def status_text(flow_path: str, state: Optional[Dict[str, Any]], paths: Paths) -> str:
    """The same text `jevflow status` prints, for the viewer's text view."""
    try:
        from . import status
        f = load_flow(flow_path)
        return status.render(f, state or {"phase_status": {}}, paths, recent=12)
    except Exception as exc:  # the text view must never break the page
        return f"(text view unavailable: {exc})"


def snapshot(paths: Paths) -> Dict[str, Any]:
    """Everything the page needs, as one JSON-able dict. Never raises."""
    name = os.path.basename(paths.root.rstrip(os.sep))
    out: Dict[str, Any] = {"project": name, "project_root": paths.root, "key": flow_key(paths),
                           "flow_id": paths.flow_id, "archived": paths.archived,
                           "generated_at": time.time(), "flow": None, "state": None,
                           "needs_human": None, "error": None, "text": ""}
    try:
        load_flow(paths.flow)  # validate; the page renders the raw JSON
        with open(paths.flow, encoding="utf-8") as fh:
            out["flow"] = json.load(fh)
    except (OSError, ValueError, FlowError) as exc:
        out["error"] = f"flow.json: {exc}"
        return out
    try:
        with open(paths.state, encoding="utf-8") as fh:
            state = json.load(fh)
        hist = state.get("history")
        if isinstance(hist, list) and len(hist) > MAX_HISTORY:
            state["history"] = hist[-MAX_HISTORY:]
            state["history_truncated"] = len(hist) - MAX_HISTORY
        out["state"] = state
    except FileNotFoundError:
        out["state"] = None  # the run has not started yet
    except (OSError, ValueError) as exc:
        out["error"] = f"state.json: {exc}"
    try:
        with open(paths.needs_human, encoding="utf-8") as fh:
            out["needs_human"] = fh.read(NEEDS_HUMAN_CHARS)
    except OSError:
        pass
    out["text"] = status_text(paths.flow, out["state"], paths)
    return out


def export_bundle(root: Any, selected: Paths) -> Dict[str, Any]:
    """A snapshot plus the flow list and the other flows' snapshots, so the
    switcher works in an offline HTML file too. ``root`` is one project's
    Paths, or a Catalog for every project on the machine."""
    data = snapshot(selected)
    flows = root.flows() if isinstance(root, Catalog) else all_flows(root)
    data["flows"] = flows
    data["snapshots"] = {}
    want = flow_key(selected)
    for row in flows[:EXPORT_FLOWS]:
        if row["key"] == want:
            continue
        p = row_paths(row)
        if p is not None:
            data["snapshots"][row["key"]] = _snap(p)
    return data


def row_paths(row: Mapping[str, Any]) -> Optional[Paths]:
    root = Paths(row["project_root"])
    if row["legacy"]:
        return Paths(root.root)
    return flow_paths(root, row["id"]) if valid_id(row["id"]) else None


class Catalog:
    """Every project folder the viewer knows about: the one it was started in,
    the ones Jevflow's hooks registered, and any .jevflow found by scanning
    the usual project folders (refreshed in the background)."""

    def __init__(self, home: Optional[Paths], scan: Optional[List[str]] = None, here: bool = False):
        self.home, self.here = home, here
        self.scan_dirs = [] if here else (registry.scan_dirs() if scan is None else list(scan))
        self._scanned: List[str] = []
        self._scan_at = 0.0
        self._scanning = False
        self._lock = threading.Lock()
        self._default: Optional[tuple] = None

    def _scan(self) -> None:
        try:
            found = registry.scan(self.scan_dirs)
        except Exception:
            found = []
        with self._lock:
            self._scanned, self._scanning = found, False

    def refresh(self, wait: bool = False) -> None:
        if not self.scan_dirs:
            return
        with self._lock:
            due = not self._scan_at or time.monotonic() - self._scan_at > SCAN_EVERY_S
            if not due or self._scanning:
                return
            self._scan_at, self._scanning = time.monotonic(), True
        if wait:
            self._scan()
        else:
            threading.Thread(target=self._scan, daemon=True).start()

    def roots(self) -> List[Paths]:
        if self.here:
            return [self.home] if self.home else []
        self.refresh()
        with self._lock:
            scanned = list(self._scanned)
        out, seen = [], set()
        for r in ([self.home.root] if self.home else []) + registry.registered() + scanned:
            rr = os.path.realpath(r)
            if rr in seen or not os.path.isdir(os.path.join(rr, ".jevflow")):
                continue
            seen.add(rr)
            out.append(Paths(rr))
        return out

    def find(self, pk: str) -> Optional[Paths]:
        for r in self.roots():
            if project_key(r.root) == pk:
                return r
        return None

    def flows(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for r in self.roots():
            rows += all_flows(r)
        rows.sort(key=lambda row: -float(row.get("updated_at") or 0))
        return rows[:MAX_ALL_FLOWS]

    def resolve(self, key: str) -> Optional[Paths]:
        """``<project key>:<flow id>``; a bare id means the home project."""
        if ":" in key:
            pk, fid = key.split(":", 1)
            root = self.find(pk)
        else:
            root, fid = self.home or (self.roots() or [None])[0], key
        if root is None:
            return None
        if fid == "(legacy)":
            return Paths(root.root) if has_legacy(root) else None
        return flow_paths(root, fid) if valid_id(fid) else None

    def default(self) -> Optional[Paths]:
        """The home project's flow, else the newest active flow anywhere."""
        if self.home is not None:
            p = default_flow(self.home)
            if p is not None:
                return p
        now = time.monotonic()
        if self._default and now - self._default[0] < DEFAULT_CACHE_S:
            return self._default[1]
        rows = self.flows()
        pick = next((r for r in rows if not r["archived"]), rows[0] if rows else None)
        p = row_paths(pick) if pick else None
        self._default = (now, p)
        return p


class GlobalTarget:
    """Target for the machine-wide viewer: a fixed flow key, or the default."""

    def __init__(self, catalog: Catalog, key: Optional[str] = None) -> None:
        self.catalog, self.key = catalog, key
        self.root = catalog.home  # for messages

    def paths(self) -> Optional[Paths]:
        return self.catalog.resolve(self.key) if self.key else self.catalog.default()


def _mtimes(paths: Paths) -> tuple:
    res = []
    for p in (paths.flow, paths.state, paths.needs_human):
        try:
            st = os.stat(p)
            res.append((st.st_mtime_ns, st.st_size))
        except OSError:
            res.append(None)
    return tuple(res)


def _json_for_script(data: Any) -> str:
    # safe inside <script>: no '</script>' or HTML comment openers can survive
    return (json.dumps(data, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def render_export(data: Dict[str, Any]) -> str:
    with open(INDEX, encoding="utf-8") as fh:
        page = fh.read()
    tag = f"<script>window.JEVFLOW_DATA={_json_for_script(data)};</script>"
    marker = "<!--JEVFLOW_DATA-->"
    if marker not in page:
        raise RuntimeError("index.html is missing the data marker")
    return page.replace(marker, tag, 1)


def host_allowed(host: Optional[str]) -> bool:
    """Only loopback names reach the handler (DNS-rebinding guard)."""
    if not host:
        return False
    h = host.strip().lower()
    if h.startswith("["):
        name = h[: h.find("]") + 1] if "]" in h else h
    else:
        name = h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    return name in LOCAL_HOSTS or name.endswith(".localhost")


class Target:
    """Which flow the viewer shows. With no --flow it follows the newest flow,
    so a flow created (or archived) while the page is open shows up."""

    def __init__(self, root: Paths, flow_id: Optional[str] = None) -> None:
        self.root, self.flow_id = root, flow_id

    def paths(self) -> Optional[Paths]:
        return flow_paths(self.root, self.flow_id) if self.flow_id else default_flow(self.root)


def _snap(target: Any) -> Dict[str, Any]:
    p = target.paths() if isinstance(target, (Target, GlobalTarget)) else target
    if p is None:
        root = getattr(target, "root", None)
        name = os.path.basename(root.root) if root is not None else "this computer"
        return {"project": name, "flow": None, "state": None,
                "needs_human": None, "error": "no flow yet", "generated_at": time.time()}
    if p.is_draft and not p.archived and not os.path.isfile(p.flow):
        return {"project": os.path.basename(p.root), "flow_id": p.flow_id, "flow": None, "state": None,
                "needs_human": None, "error": "draft: the phases are being laid out",
                "generated_at": time.time()}
    return snapshot(p)


def _watch(target: Any) -> tuple:
    p = target.paths() if isinstance(target, (Target, GlobalTarget)) else target
    if p is None:
        return (None,)
    return (p.dir,) + _mtimes(p) + (_mtime_or_none(p.draft),)


def _mtime_or_none(path: str):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _query(path: str) -> Dict[str, str]:
    from urllib.parse import parse_qs
    q = path.split("?", 1)[1] if "?" in path else ""
    return {k: v[0] for k, v in parse_qs(q).items() if v}


def _target_for(paths: Any, path: str) -> Any:
    """``?flow=ID`` picks a flow (active or archived) for this request only."""
    fid = _query(path).get("flow")
    if isinstance(paths, GlobalTarget) and fid is not None:
        if ":" in fid or fid == "(legacy)" or valid_id(fid):
            return GlobalTarget(paths.catalog, fid)
        return paths
    if isinstance(paths, Target) and fid is not None:
        if fid == "(legacy)":
            return Paths(paths.root.root)
        if valid_id(fid):
            return Target(paths.root, fid)
    return paths


def make_handler(paths: Any, stop: threading.Event):
    class Handler(BaseHTTPRequestHandler):
        server_version = "jevflow-ui"

        def log_message(self, fmt, *args):  # quiet by default
            if os.environ.get("JEVFLOW_UI_LOG"):
                sys.stderr.write("jevflow ui: " + fmt % args + "\n")

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 (http.server API)
            if not host_allowed(self.headers.get("Host")):
                return self._send(403, b"forbidden host\n", "text/plain; charset=utf-8")
            route = self.path.split("?", 1)[0]
            if route in ("/", "/index.html"):
                with open(INDEX, "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8")
            if route == "/api/state":
                body = json.dumps(_snap(_target_for(paths, self.path))).encode()
                return self._send(200, body, "application/json")
            if route == "/api/flows":
                if isinstance(paths, GlobalTarget):
                    rows = paths.catalog.flows()
                else:
                    rows = all_flows(paths.root if isinstance(paths, Target) else Paths(paths.root))
                return self._send(200, json.dumps({"flows": rows}).encode(), "application/json")
            if route == "/events":
                return self._events(_target_for(paths, self.path))
            if route == "/healthz":
                return self._send(200, b"ok\n", "text/plain; charset=utf-8")
            return self._send(404, b"not found\n", "text/plain; charset=utf-8")

        def _events(self, paths: Any) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            last, beat = None, time.monotonic()
            try:
                while not stop.is_set():
                    cur = _watch(paths)
                    if cur != last:
                        last = cur
                        msg = "event: state\ndata: " + json.dumps(_snap(paths)) + "\n\n"
                        self.wfile.write(msg.encode())
                        self.wfile.flush()
                        beat = time.monotonic()
                    elif time.monotonic() - beat > HEARTBEAT_S:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        beat = time.monotonic()
                    stop.wait(POLL_S)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def do_POST(self):  # noqa: N802
            self._send(405, b"read-only\n", "text/plain; charset=utf-8")

        do_PUT = do_DELETE = do_PATCH = do_POST

    return Handler


def serve(paths: Any, port: int, out: IO[str], open_browser: bool = False,
          ready: Optional[threading.Event] = None, stop: Optional[threading.Event] = None) -> int:
    stop = stop or threading.Event()
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(paths, stop))
    except OSError as exc:
        out.write(f"jevflow ui: cannot listen on 127.0.0.1:{port}: {exc}\n")
        return 3
    httpd.daemon_threads = True
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    if isinstance(paths, GlobalTarget):
        n = len(paths.catalog.roots())
        where = (f"project {paths.catalog.home.root}" if paths.catalog.here and paths.catalog.home
                 else "every flow on this computer" + (f", {n} folder{'s' if n != 1 else ''} found so far" if n else ""))
    else:
        where = f"project {paths.root.root if isinstance(paths, Target) else paths.root}"
    out.write(f"jevflow ui: {url}  ({where}, read-only, Ctrl+C to stop)\n")
    out.flush()
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    if ready is not None:
        ready.port = httpd.server_address[1]  # type: ignore[attr-defined]
        ready.set()
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    t.start()
    try:
        while not stop.is_set():
            stop.wait(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.shutdown()
        httpd.server_close()
    return 0


def write_launch_json(paths: Paths, port: int, out: IO[str]) -> int:
    """Add a 'jevflow' configuration to <project>/.claude/launch.json."""
    wrapper = os.path.join(os.path.dirname(HERE), "hooks", "jevflow")
    entry = {"name": "jevflow", "runtimeExecutable": wrapper,
             "runtimeArgs": ["ui", "--project", "${workspaceFolder}"],
             "port": port, "autoPort": True}
    path = os.path.join(paths.root, ".claude", "launch.json")
    data: Dict[str, Any] = {"version": "0.0.1", "configurations": []}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except ValueError:
            out.write(f"{path} has comments or is not plain JSON; add this entry by hand:\n")
            out.write(json.dumps(entry, indent=2) + "\n")
            return 3
    confs: List[Dict[str, Any]] = [c for c in data.get("configurations", []) if c.get("name") != "jevflow"]
    confs.append(entry)
    data["configurations"] = confs
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    out.write(f"wrote {path}: in the Claude Code desktop app pick 'jevflow' from the preview "
              "server dropdown, or ask Claude to open the jevflow preview.\n")
    return 0


def _ui_state() -> str:
    return os.path.join(registry.home(), UI_STATE)


def _alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/healthz", timeout=1.5) as r:
            return r.read() == b"ok\n"
    except (OSError, ValueError):
        return False


def _running() -> Optional[Dict[str, Any]]:
    try:
        with open(_ui_state(), encoding="utf-8") as fh:
            st = json.load(fh)
        return st if isinstance(st, dict) and _alive(int(st.get("port") or 0)) else None
    except (OSError, ValueError, TypeError):
        return None


def _free_port(start: int) -> int:
    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
            try:
                sk.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return 0


def background(port: int, open_browser: bool, out: IO[str], err: IO[str]) -> int:
    """Start (or reuse) a detached machine-wide viewer and print its URL, so a
    chat (Claude Code, the desktop app, Cowork) can start it without a terminal."""
    st = _running()
    if st is None:
        port = _free_port(port)
        if not port:
            err.write("jevflow ui: no free port near 7788\n")
            return 3
        os.makedirs(registry.home(), exist_ok=True)
        env = dict(os.environ)
        pkg_parent = os.path.dirname(HERE)
        env["PYTHONPATH"] = pkg_parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        with open(os.path.join(registry.home(), "ui.log"), "ab") as log:
            proc = subprocess.Popen([sys.executable, "-m", "jevflow", "ui", "--port", str(port)],
                                    cwd=os.path.expanduser("~"), stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=log, start_new_session=True, env=env)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not _alive(port):
            if proc.poll() is not None:
                err.write(f"jevflow ui: the viewer exited; see {os.path.join(registry.home(), 'ui.log')}\n")
                return 3
            time.sleep(0.2)
        st = {"pid": proc.pid, "port": port, "started_at": time.time()}
        tmp = _ui_state() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(st, fh)
        os.replace(tmp, _ui_state())
        state = "started"
    else:
        state = "already running"
    url = f"http://127.0.0.1:{st['port']}/"
    out.write(f"jevflow ui {state}: {url}  (every flow on this computer; `jevflow ui --stop` stops it)\n")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    return 0


def stop_background(out: IO[str]) -> int:
    st = _running()
    if st is None:
        out.write("jevflow ui: no background viewer running\n")
        return 0
    try:
        import signal
        os.kill(int(st["pid"]), signal.SIGTERM)
    except (OSError, ValueError, KeyError):
        pass
    try:
        os.remove(_ui_state())
    except OSError:
        pass
    out.write(f"jevflow ui: stopped the viewer on port {st['port']}\n")
    return 0


def main(argv: List[str], stdout: IO[str], stderr: IO[str]) -> int:
    project, export, launch, open_browser, flow_id = os.getcwd(), None, False, False, None
    here = bg = stop = False
    try:
        port = int(os.environ.get("PORT") or DEFAULT_PORT)
    except ValueError:
        port = DEFAULT_PORT
    args = list(argv)
    while args:
        a = args.pop(0)
        if a == "--project" and args:
            project = args.pop(0)
        elif a == "--port" and args:
            try:
                port = int(args.pop(0))
            except ValueError:
                stderr.write(USAGE)
                return 3
        elif a == "--export" and args:
            export = args.pop(0)
        elif a == "--flow" and args:
            flow_id = args.pop(0)
        elif a == "--launch-json":
            launch = True
        elif a == "--open":
            open_browser = True
        elif a == "--here":
            here = True
        elif a == "--background":
            bg = True
        elif a == "--stop":
            stop = True
        elif a in ("-h", "--help"):
            stdout.write(USAGE)
            return 0
        else:
            stderr.write(USAGE)
            return 3
    if stop:
        return stop_background(stdout)
    root = find_project(project, env={})
    if root is not None:
        registry.register(root.root)
    if launch or here:
        if root is None:
            stderr.write(f"no .jevflow at or above {os.path.realpath(project)}. Run this from inside a project, "
                         "or drop --here to see every flow on this computer\n")
            return 3
        if launch:
            return write_launch_json(root, port, stdout)
    if bg:
        return background(port, open_browser, stdout, stderr)
    catalog = Catalog(root, here=here)
    if export:
        catalog.refresh(wait=True)
        paths = catalog.resolve(flow_id) if flow_id else catalog.default()
        if paths is None:
            stderr.write(f"no flow{' ' + flow_id if flow_id else ''} to export\n")
            return 3
        html = render_export(export_bundle(catalog, paths))
        with open(export, "w", encoding="utf-8") as fh:
            fh.write(html)
        stdout.write(f"wrote {export} ({len(html) // 1024} KB, self-contained)\n")
        return 0
    return serve(GlobalTarget(catalog, flow_id), port, stdout, open_browser=open_browser)
