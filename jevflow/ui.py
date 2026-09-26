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

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, IO, List, Optional

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

USAGE = """usage: python -m jevflow ui [--project DIR] [--flow ID] [--port N] [--open]
       python -m jevflow ui [--project DIR] --export FILE
       python -m jevflow ui [--project DIR] --launch-json [--port N]
  (no flag)      serve a live viewer on http://127.0.0.1:PORT (default $PORT or 7788)
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


def flow_summary(p: Paths, mtime: float, now: Optional[float] = None) -> Dict[str, Any]:
    """One row of the flow switcher. Never raises."""
    now = time.time() if now is None else now
    row: Dict[str, Any] = {"id": p.flow_id or "", "legacy": p.flow_id is None, "archived": p.archived,
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
    out: Dict[str, Any] = {"project": name, "flow_id": paths.flow_id, "archived": paths.archived,
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


def export_bundle(root: Paths, selected: Paths) -> Dict[str, Any]:
    """A snapshot plus the flow list and the other flows' snapshots, so the
    switcher works in an offline HTML file too."""
    data = snapshot(selected)
    flows = all_flows(root)
    data["flows"] = flows
    data["snapshots"] = {}
    for row in flows[:EXPORT_FLOWS]:
        p = Paths(root.root) if row["legacy"] else flow_paths(root, row["id"])
        if p is not None and not (row["legacy"] and selected.flow_id is None) and p.flow_id != selected.flow_id:
            data["snapshots"][row["id"] or "(legacy)"] = _snap(p)
    return data


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
    p = target.paths() if isinstance(target, Target) else target
    if p is None:
        return {"project": os.path.basename(target.root.root), "flow": None, "state": None,
                "needs_human": None, "error": "no flow yet", "generated_at": time.time()}
    if p.is_draft and not p.archived and not os.path.isfile(p.flow):
        return {"project": os.path.basename(p.root), "flow_id": p.flow_id, "flow": None, "state": None,
                "needs_human": None, "error": "draft: the phases are being laid out",
                "generated_at": time.time()}
    return snapshot(p)


def _watch(target: Any) -> tuple:
    p = target.paths() if isinstance(target, Target) else target
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
                root = paths.root if isinstance(paths, Target) else Paths(paths.root)
                return self._send(200, json.dumps({"flows": all_flows(root)}).encode(), "application/json")
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
    root = paths.root.root if isinstance(paths, Target) else paths.root
    out.write(f"jevflow ui: {url}  (project {root}, read-only, Ctrl+C to stop)\n")
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


def main(argv: List[str], stdout: IO[str], stderr: IO[str]) -> int:
    project, export, launch, open_browser, flow_id = os.getcwd(), None, False, False, None
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
        elif a in ("-h", "--help"):
            stdout.write(USAGE)
            return 0
        else:
            stderr.write(USAGE)
            return 3
    root = find_project(project, env={})
    paths = None
    if root is not None:
        paths = flow_paths(root, flow_id) if flow_id else default_flow(root)
    if launch:
        if root is None:
            stderr.write(f"no .jevflow at or above {os.path.realpath(project)}. Run this from inside a project, or turn Jevflow on there first: jevflow auto on --project <dir>\n")
            return 3
        return write_launch_json(root, port, stdout)
    if root is None:
        stderr.write(f"no .jevflow at or above {os.path.realpath(project)}. Run this from inside a project, or turn Jevflow on there first: jevflow auto on --project <dir>\n")
        return 3
    if export:
        if paths is None:
            stderr.write(f"no flow{' ' + flow_id if flow_id else ''} to export\n")
            return 3
        html = render_export(export_bundle(root, paths))
        with open(export, "w", encoding="utf-8") as fh:
            fh.write(html)
        stdout.write(f"wrote {export} ({len(html) // 1024} KB, self-contained)\n")
        return 0
    return serve(Target(root, flow_id), port, stdout, open_browser=open_browser)
