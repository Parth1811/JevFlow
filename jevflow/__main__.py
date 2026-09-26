"""Command line: ``python -m jevflow <hook|status|validate|flows|auto|run|ui|statusline> ...``.

Exit codes: 0 ok, 3 configuration error. ``hook`` exits 0, except the
private code 42 for a deliberate TaskCompleted block, which the launcher
(hooks/jevflow) maps to Claude Code's blocking exit 2.
``run`` (the supervisor) adds 2 limit reached, 4 waiting on a human,
5 another supervisor is running.
"""

import sys
from typing import List, Optional

USAGE = """usage: python -m jevflow <command>
  hook <Event>                            Claude Code hook (stdin JSON -> stdout JSON); Event is
                                          SessionStart, Stop, StopFailure, PreToolUse, PostToolUse,
                                          SubagentStop or TaskCompleted
  status [--project DIR] [--json]         phase table, recent decisions, NEEDS_HUMAN
  validate [--project DIR] [--flow ID | FLOW_JSON]   check a flow file
  flows [--project DIR]                   list active and archived flows
  install-cli [--bin-dir DIR]             link `jevflow` into ~/.local/bin (done on first session)
  start [--name N] --goal TEXT [--project DIR]
                                          start a tracked flow now (Claude runs this itself)
  join FLOW_ID [--project DIR]            work on an existing flow from this session (another agent)
  claim PHASE [--as NAME]                 say which phase this agent is working on (viewer/status)
  auto [on|off] [--project DIR]           auto-plan a new flow from each task prompt
  run --project DIR [options]             supervisor: relaunch claude until done (run -h)
  ui [--project DIR] [--export F | --launch-json]
                                          read-only web viewer: live on 127.0.0.1, an HTML
                                          snapshot, or a Claude Code desktop preview entry (ui -h)
  statusline [--with CMD | --config]      one-line status for Claude Code's terminal status line
"""


def _pick(argv: List[str]):
    """Parse [--project DIR] [--flow ID] [FILE]; returns (project_dir, flow_id, rest)."""
    import os
    project, flow_id, rest = os.getcwd(), None, []
    args = list(argv)
    while args:
        a = args.pop(0)
        if a == "--project" and args:
            project = args.pop(0)
        elif a == "--flow" and args:
            flow_id = args.pop(0)
        else:
            rest.append(a)
    return project, flow_id, rest


def _validate(argv: List[str]) -> int:
    import os
    from .flow import FlowError, load_flow
    from .project import default_flow, find_project, flow_paths

    project, flow_id, rest = _pick(argv)
    if rest and os.path.isfile(rest[0]):
        path = rest[0]
    else:
        target = rest[0] if rest else project
        root = find_project(target, env={})
        paths = None
        if root is not None:
            paths = flow_paths(root, flow_id) if flow_id else default_flow(root)
        if paths is None:
            sys.stderr.write(f"no flow{' ' + flow_id if flow_id else ''} at or above {target}\n")
            return 3
        path = paths.flow
        if not os.path.isfile(path):
            sys.stderr.write(f"{path} does not exist yet (draft flow)\n")
            return 3
    try:
        flow = load_flow(path)
    except FlowError as exc:
        sys.stderr.write(f"invalid flow: {exc}\n")
        return 3
    order = " -> ".join(flow.topo_order())
    sys.stdout.write(f"ok: {len(flow.phases)} phases ({order}), mode {flow.mode}, "
                     f"flow_version {flow.flow_version}\n")
    return 0


def _flows(argv: List[str]) -> int:
    import json
    import os
    import time
    from .project import find_project, has_legacy, list_flows

    project, _, _ = _pick(argv)
    root = find_project(project, env={})
    if root is None:
        sys.stderr.write(f"no .jevflow at or above {os.path.realpath(project)}. Run this from inside a project, or turn Jevflow on there first: jevflow auto on --project <dir>\n")
        return 3
    rows = []
    if has_legacy(root):
        rows.append(("(legacy)", "active", ".jevflow/flow.json", ""))
    for p, mt in list_flows(root):
        try:
            with open(p.state, encoding="utf-8") as fh:
                st = json.load(fh)
            info = "done" if st.get("done") else f"at {st.get('current_phase')}"
        except (OSError, ValueError):
            info = "draft (not laid out)" if p.is_draft else "not started"
        rows.append((p.flow_id, "archived" if p.archived else "active", info,
                     time.strftime("%Y-%m-%d %H:%M", time.gmtime(mt))))
    if not rows:
        sys.stdout.write("no flows yet\n")
    for r in rows:
        sys.stdout.write(f"{r[1]:<9} {r[0]:<52} {r[2]:<24} {r[3]}\n")
    return 0


def _start(argv: List[str]) -> int:
    import os
    import time
    from . import auto
    from .project import Paths, find_project

    project, _, rest = _pick(argv)
    name = None
    if "--name" in rest:
        i = rest.index("--name")
        if i + 1 < len(rest):
            name = rest[i + 1]
            del rest[i:i + 2]
    goal = ""
    if rest[:1] == ["--goal"] and len(rest) > 1:
        goal = " ".join(rest[1:])
    elif rest:
        goal = " ".join(rest)
    if not goal.strip():
        sys.stderr.write('usage: python -m jevflow start [--name short-name] --goal "what the user asked for" '
                         '[--project DIR]\n')
        return 3
    root = find_project(project, env={}) or Paths(os.path.realpath(project))
    _, text = auto.start_flow(root, goal, time.time(), name=name)
    sys.stdout.write(text + "\n")
    return 0


def _join(argv: List[str]) -> int:
    """``jevflow join FLOW``: this Claude session (or subagent) works on an
    existing active flow. The printed marker is read by PostToolUse, which
    binds the calling session, the same way ``start`` does."""
    import os
    from . import auto
    from .project import find_project, flow_paths

    project, flow_id, rest = _pick(argv)
    flow_id = flow_id or (rest[0] if rest else None)
    root = find_project(project, env={})
    p = flow_paths(root, flow_id) if (root is not None and flow_id) else None
    if p is None or p.archived:
        sys.stderr.write(f"no active flow {flow_id!r} at or above {os.path.realpath(project)} "
                         "(list them with `jevflow flows`)\n")
        return 3
    sys.stdout.write(f"{auto.START_MARKER} flow={p.flow_id}\n"
                     f"Joined flow {p.flow_id}. Other agents may be working on it too: before you "
                     "start, run `jevflow status --flow " + p.flow_id + "` to see the phases and who "
                     "is on which, then `jevflow claim <phase> --as <your role>` for the phase you "
                     "take. Only work on phases whose dependencies are done.\n")
    return 0


def _claim(argv: List[str]) -> int:
    """``jevflow claim PHASE [--as NAME]``: record which phase this agent
    works on (shown in the viewer and status). Informational only."""
    import re
    from .agents import CLAIM_MARKER

    _, _, rest = _pick(argv)
    name = None
    if "--as" in rest:
        i = rest.index("--as")
        if i + 1 < len(rest):
            name = rest[i + 1]
            del rest[i:i + 2]
    if not rest or not re.match(r"^[a-z][a-z0-9_-]{0,39}$", rest[0]):
        sys.stderr.write("usage: python -m jevflow claim PHASE [--as NAME]\n")
        return 3
    clean = re.sub(r"[^A-Za-z0-9 ._-]", "", name or "")[:40].strip()
    sys.stdout.write(f"{CLAIM_MARKER} phase={rest[0]}" + (f" name={clean}" if clean else "") + "\n")
    return 0


def _auto(argv: List[str]) -> int:
    import os
    from .project import JEVFLOW_DIR, Paths, find_project, load_config, save_config

    project, _, rest = _pick(argv)
    root = find_project(project, env={}) or Paths(os.path.realpath(project))
    cfg = load_config(root)
    if rest[:1] in (["on"], ["off"]):
        cfg["auto"] = rest[0] == "on"
        save_config(root, cfg)
        gi = os.path.join(root.base, ".gitignore")
        if cfg["auto"] and not os.path.exists(gi):
            # keep per-machine runtime files out of git; flows/ and done/ stay committable
            from .auto import GITIGNORE
            with open(gi, "w", encoding="utf-8") as fh:
                fh.write(GITIGNORE)
    elif rest:
        sys.stderr.write("usage: python -m jevflow auto [on|off] [--project DIR]\n")
        return 3
    state = "on" if cfg.get("auto") else "off"
    sys.stdout.write(f"auto-planning {state} for {root.root} "
                     f"({os.path.join(JEVFLOW_DIR, 'config.json')})\n")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv.pop(0) if argv else ""
    if cmd == "hook":
        try:
            from . import hooks
        except Exception as exc:  # a broken install must still never trap a session
            sys.stderr.write(f"jevflow: cannot import hooks: {type(exc).__name__}\n")
            sys.stdout.write("{}\n")
            return 0
        return hooks.main(argv)
    if cmd == "status":
        from . import status
        return status.main(argv, sys.stdout, sys.stderr)
    if cmd == "validate":
        return _validate(argv)
    if cmd == "flows":
        return _flows(argv)
    if cmd == "auto":
        return _auto(argv)
    if cmd == "install-cli":
        from . import cli_install
        return cli_install.main(argv, sys.stdout, sys.stderr)
    if cmd == "start":
        return _start(argv)
    if cmd == "join":
        return _join(argv)
    if cmd == "claim":
        return _claim(argv)
    if cmd == "statusline":
        from . import statusline
        return statusline.main(argv, sys.stdin, sys.stdout, sys.stderr)
    if cmd == "ui":
        from . import ui
        return ui.main(argv, sys.stdout, sys.stderr)
    if cmd == "run":
        from . import supervisor
        return supervisor.main(argv, sys.stdout, sys.stderr)
    sys.stderr.write(USAGE)
    return 0 if cmd in ("-h", "--help", "help") else 3


if __name__ == "__main__":
    sys.exit(main())
