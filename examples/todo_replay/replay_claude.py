#!/usr/bin/python3.11
"""Scripted stand-in for `claude -p` used by the E2 demo (see docs/DEMO.md).

The supervisor launches this through JEVFLOW_CLAUDE_BIN exactly as it would
launch claude. It behaves like a small, fallible agent working on
examples/todo: each turn it does the work for the phase Jevflow says is
current, then "stops" by feeding a Stop payload to the REAL plugin hook
(hooks/jevflow hook Stop), which runs the real checks and asks the real Jev.
A block sends it round again with stop_hook_active=true, like Claude Code.

Scripted mistakes, so the demo exercises the guard:
  * first stop claims the whole app is done while only a stub exists;
  * the first test file has a wrong expected string, and the agent claims
    the tests pass.
Only synthetic toy content is written, and only inside the project dir.
"""
import json
import os
import subprocess
import sys
import time
import uuid

MAX_TURNS = 20
PLUGIN = os.environ.get("REPLAY_PLUGIN_ROOT") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HOOK = os.path.join(PLUGIN, "hooks", "jevflow")
PROJ = os.getcwd()
LOG = os.path.join(PROJ, ".jevflow", "replay_log.jsonl")

STUB_CLI = '''"""todo CLI (stub)."""
import sys


def main(argv=None):
    print("not implemented yet", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
'''

CLI = '''"""todo CLI: add TEXT, list, done N. Storage: $TODO_FILE (default todos.json)."""
import json
import os
import sys


def _path():
    return os.environ.get("TODO_FILE", "todos.json")


def _load():
    try:
        with open(_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return []


def _save(items):
    with open(_path(), "w", encoding="utf-8") as fh:
        json.dump(items, fh)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: todo.cli add TEXT | list | done N", file=sys.stderr)
        return 2
    items = _load()
    cmd = args[0]
    if cmd == "add" and len(args) > 1:
        items.append({"text": " ".join(args[1:]), "done": False})
        _save(items)
        return 0
    if cmd == "list":
        for i, it in enumerate(items, 1):
            print(f"{i}. [{'x' if it['done'] else ' '}] {it['text']}")
        return 0
    if cmd == "done" and len(args) == 2 and args[1].isdigit():
        n = int(args[1])
        if 1 <= n <= len(items):
            items[n - 1]["done"] = True
            _save(items)
            return 0
    print("bad command", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
'''

README = '''# todo

Tiny CLI todo app.

    python -m todo.cli add TEXT
    python -m todo.cli list
    python -m todo.cli done N

Todos are stored as JSON in the file named by `TODO_FILE` (default `todos.json`).
'''

TEST_TEMPLATE = '''import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from todo import cli


class CliTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json", dir=".jevflow")
        os.close(fd)
        os.remove(self.path)
        os.environ["TODO_FILE"] = self.path

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def run_cli(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(list(args))
        return rc, buf.getvalue()

    def test_add_list_done(self):
        self.assertEqual(self.run_cli("add", "buy", "milk")[0], 0)
        self.assertEqual(self.run_cli("list")[1], "1. [ ] buy milk\\n")
        self.assertEqual(self.run_cli("done", "1")[0], 0)
        self.assertEqual(self.run_cli("list")[1], "{expected}")

    def test_done_out_of_range(self):
        self.assertEqual(self.run_cli("done", "3")[0], 2)
'''

BUGGY_TEST = TEST_TEMPLATE.replace("{expected}", "1. [done] buy milk\\n")
GOOD_TEST = TEST_TEMPLATE.replace("{expected}", "1. [x] buy milk\\n")


def write(rel, text):
    path = os.path.join(PROJ, rel)
    os.makedirs(os.path.dirname(path) or PROJ, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def state():
    with open(os.path.join(PROJ, ".jevflow", "state.json"), encoding="utf-8") as fh:
        return json.load(fh)


def hook(event, payload):
    env = dict(os.environ, CLAUDE_PROJECT_DIR=PROJ, CLAUDE_PLUGIN_ROOT=PLUGIN)
    p = subprocess.run([HOOK, "hook", event], input=json.dumps(payload), text=True,
                       capture_output=True, env=env, cwd=PROJ, timeout=600)
    try:
        return json.loads(p.stdout or "{}")
    except ValueError:
        return {}


def log(rec):
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def act(phase, seen):
    """Do the work for `phase`; return the assistant's closing message."""
    n = seen.get(phase, 0)
    seen[phase] = n + 1
    if phase == "scaffold":
        if n == 0:
            write("todo/__init__.py", '"""todo package."""\n')
            write("todo/cli.py", STUB_CLI)
            return "All done! The todo app is complete with add, list and done."
        return "Scaffold is in place: todo/__init__.py and todo/cli.py exist. Moving on to the commands."
    if phase == "implement":
        write("todo/cli.py", CLI)
        return "Implemented add, list and done in todo/cli.py; todos persist as JSON in TODO_FILE."
    if phase == "docs":
        write("README.md", README)
        return "Wrote README.md documenting add, list, done and TODO_FILE."
    if phase in ("test", "debug"):
        if not os.path.exists(os.path.join(PROJ, "tests", "test_cli.py")):
            write("tests/__init__.py", "")
            write("tests/test_cli.py", BUGGY_TEST)
            return "Added tests/test_cli.py covering add, list and done. All tests pass."
        write("tests/test_cli.py", GOOD_TEST)
        return ("The failing assertion expected '[done]' but list prints '[x]'. "
                "Fixed the expected string in tests/test_cli.py; the suite passes now.")
    return "Nothing left for this phase."


def main():
    sid = str(uuid.uuid4())
    transcript = os.path.join(PROJ, ".jevflow", "replay_transcript.jsonl")
    base = {"session_id": sid, "transcript_path": transcript, "cwd": PROJ,
            "permission_mode": "acceptEdits"}
    open(transcript, "a").close()
    hook("SessionStart", dict(base, hook_event_name="SessionStart", source="startup"))
    seen = {}
    active = False
    turns = 0
    for turns in range(1, MAX_TURNS + 1):
        s = state()
        phase = s.get("current_phase")
        msg = act(phase, seen)
        with open(transcript, "a") as fh:
            fh.write(json.dumps({"turn": turns, "text": msg}) + "\n")
        t0 = time.time()
        out = hook("Stop", dict(base, hook_event_name="Stop", stop_hook_active=active,
                                last_assistant_message=msg))
        s2 = state()
        last = (s2.get("history") or [{}])[-1]
        log({"turn": turns, "phase_before": phase, "message": msg,
             "hook_output": out, "decision": last.get("decision"),
             "condition": last.get("condition"), "to_phase": last.get("to_phase"),
             "checks": last.get("checks"), "probs": last.get("probs"),
             "phase_after": s2.get("current_phase"), "hook_s": round(time.time() - t0, 2)})
        if out.get("decision") != "block":
            break
        active = True
    print(json.dumps({"type": "result", "subtype": "success", "session_id": sid,
                      "num_turns": turns, "is_error": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
