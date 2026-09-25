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
        sys.stderr.write(f"no .jevflow at or above {project}\n")
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
            with open(gi, "w", encoding="utf-8") as fh:
                fh.write("sessions/\n**/state.json.lock\n**/lock\n**/runs/\n**/last_run.json\n*.tmp\n")
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
