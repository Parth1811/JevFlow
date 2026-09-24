"""Command line: ``python -m jevflow <hook|status|validate|run> ...``.

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
  validate [--project DIR | FLOW_JSON]    check a flow file
  run --project DIR [options]             supervisor: relaunch claude until done (run -h)
"""


def _validate(argv: List[str]) -> int:
    import os
    from .flow import FlowError, load_flow
    from .project import find_project

    target = os.getcwd()
    if argv[:1] == ["--project"] and len(argv) > 1:
        target = argv[1]
    elif argv:
        target = argv[0]
    if os.path.isfile(target):
        path = target
    else:
        paths = find_project(target, env={})
        if paths is None:
            sys.stderr.write(f"no .jevflow/flow.json at or above {target}\n")
            return 3
        path = paths.flow
    try:
        flow = load_flow(path)
    except FlowError as exc:
        sys.stderr.write(f"invalid flow: {exc}\n")
        return 3
    order = " -> ".join(flow.topo_order())
    sys.stdout.write(f"ok: {len(flow.phases)} phases ({order}), mode {flow.mode}, "
                     f"flow_version {flow.flow_version}\n")
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
    if cmd == "run":
        from . import supervisor
        return supervisor.main(argv, sys.stdout, sys.stderr)
    sys.stderr.write(USAGE)
    return 0 if cmd in ("-h", "--help", "help") else 3


if __name__ == "__main__":
    sys.exit(main())
