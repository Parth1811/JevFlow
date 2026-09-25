"""jevflow statusline: segment content, width trimming, never-fail, --with."""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

from jevflow import statusline as sl

FLOW = {"schema_version": 1, "goal": "g", "mode": "enforce",
        "limits": {"max_blocks_per_session": 8, "max_jev_calls": 80},
        "phases": [{"id": "a", "name": "A", "done_when": "x"},
                   {"id": "b", "name": "B", "depends_on": ["a"], "done_when": "x", "on_fail": "fix",
                    "loop": {"max_iterations": 2, "until": "true"}},
                   {"id": "fix", "name": "F", "depends_on": ["a"], "done_when": "x"}]}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="jevflow-sl-"))
        os.makedirs(os.path.join(self.dir, ".jevflow"))
        self.write("flow.json", FLOW)
        self.session = {"workspace": {"project_dir": self.dir}}

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, obj):
        with open(os.path.join(self.dir, ".jevflow", name), "w", encoding="utf-8") as fh:
            fh.write(obj if isinstance(obj, str) else json.dumps(obj))

    def seg(self, **kw):
        return sl.segment(self.session, color=False, **kw)


class TestSegment(Base):
    def test_no_flow_is_silent(self):
        self.assertEqual(sl.segment({"cwd": tempfile.gettempdir()}, color=False), "")

    def test_not_started(self):
        self.assertIn("ready", self.seg())
        self.assertIn("2 phases", self.seg())  # the on_fail branch is not counted

    def test_active_loop_and_last_decision(self):
        self.write("state.json", {"current_phase": "b", "phase_status": {"a": "done", "b": "active"},
                                  "loop_iterations": {"b": 1}, "blocks_this_session": 2, "jev_calls": 47,
                                  "history": [{"event": "stop", "decision": "BLOCK", "condition": "loop_continue"}]})
        s = self.seg()
        for part in ("▸ b 1/2", "loop 1/2", "blocks 2/8", "jev 47/80", "last BLOCK loop_continue"):
            self.assertIn(part, s)

    def test_branch_flagged(self):
        self.write("state.json", {"current_phase": "fix", "phase_status": {"a": "done"}})
        self.assertIn("debug branch", self.seg())

    def test_done(self):
        self.write("state.json", {"done": True, "current_phase": "b", "restarts": 1})
        s = self.seg()
        self.assertIn("goal complete", s)
        self.assertIn("restarts 1", s)
        self.assertNotIn("last", s)

    def test_needs_human(self):
        self.write("state.json", {"current_phase": "b", "needs_human": "Pick one\nplease"})
        self.assertIn("needs human · Pick one please", self.seg())

    def test_corrupt_state_still_prints(self):
        self.write("state.json", "{not json")
        self.assertIn("ready", self.seg())

    def test_corrupt_flow(self):
        self.write("flow.json", "[]")
        self.assertIn("unreadable", self.seg())

    def test_width_trims_tail(self):
        self.write("state.json", {"current_phase": "b", "jev_calls": 3,
                                  "history": [{"event": "stop", "decision": "ADVANCE", "condition": "advance"}]})
        s = self.seg(width=30)
        self.assertLessEqual(len(s), 30)
        self.assertTrue(s.startswith("jevflow ▸ b"))

    def test_color_length_ignores_escapes(self):
        self.write("state.json", {"current_phase": "b"})
        colored = sl.segment(self.session, color=True)
        self.assertIn("\033[", colored)
        self.assertEqual(sl._visible_len(colored), len(self.seg()))


class TestMain(Base):
    def run_main(self, argv, stdin):
        out, err = io.StringIO(), io.StringIO()
        rc = sl.main(argv, io.StringIO(stdin), out, err)
        return rc, out.getvalue()

    def test_bad_stdin_never_fails(self):
        rc, out = self.run_main([], "not json")
        self.assertEqual(rc, 0)

    def test_with_existing_command(self):
        cmd = f"{sys.executable} -c \"print('mine')\""
        rc, out = self.run_main(["--with", cmd], json.dumps(self.session))
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        self.assertEqual(lines[0], "mine")
        self.assertIn("jevflow", lines[1])

    def test_config(self):
        rc, out = self.run_main(["--config"], "")
        cfg = json.loads(out)["statusLine"]
        self.assertEqual(cfg["type"], "command")
        self.assertTrue(cfg["command"].endswith("hooks/jevflow statusline"))


if __name__ == "__main__":
    unittest.main()
