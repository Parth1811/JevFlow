"""Auto-planning and multiple flows per project: draft -> laid out -> done/."""
import io
import json
import os
import shutil
import time
import tempfile
import unittest

from jevflow import auto, hooks, project, statusline
from jevflow.__main__ import main as cli

NOW = time.time()  # state.started_at uses the real clock
TASK = "Add a --verbose flag to the CLI, cover it with tests and document it in the README"


def laid_out(goal, check="true"):
    return {"schema_version": 1, "goal": goal, "mode": "enforce",
            "phases": [{"id": "build", "name": "Build", "done_when": "it works", "check": check}]}


class AutoCase(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="jevflow-auto-"))
        self.root = project.Paths(self.dir)
        self.env = {"JEVFLOW_AUTO": "1"}

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def hook(self, event, sid="s1", now=NOW, env=None, **payload):
        payload = {"cwd": self.dir, "session_id": sid, "hook_event_name": event, **payload}
        return hooks.handle(event, payload, env=self.env if env is None else env, now=now,
                            client_factory=lambda n: None)

    def prompt(self, text=TASK, sid="s1", **kw):
        return self.hook("UserPromptSubmit", sid=sid, prompt=text, **kw)

    def only_flow(self):
        flows = project.list_flows(self.root)
        self.assertEqual(len(flows), 1, flows)
        return flows[0][0]

    def lay_out(self, p, check="true"):
        with open(p.flow, "w", encoding="utf-8") as fh:
            json.dump(laid_out(TASK, check), fh)


class TestTaskFilter(unittest.TestCase):
    def test_filter(self):
        yes = [TASK, "refactor the storage layer so that writes are atomic and add a regression test",
               "#jev fix it"]
        no = ["", "fix the typo", "/jevflow:status", "!ls -la now please show me everything here",
              "what does the supervisor do when claude exits early?",
              "How does the stop hook decide whether to block the stop?", TASK + " #nojev"]
        for t in yes:
            self.assertTrue(auto.looks_like_task(t), t)
        for t in no:
            self.assertFalse(auto.looks_like_task(t), t)


class TestAutoPlanning(AutoCase):
    def test_no_jevflow_dir_is_inactive(self):
        out = self.prompt()  # only a nudge; nothing is created without auto mode
        self.assertIn("start a tracked flow", json.dumps(out))
        self.assertFalse(os.path.exists(os.path.join(self.dir, ".jevflow")))

    def test_auto_off_does_nothing(self):
        os.makedirs(os.path.join(self.dir, ".jevflow"))
        self.assertNotIn("lay it out as phases", json.dumps(self.prompt(env={})))
        self.assertEqual(project.list_flows(self.root), [])

    def test_config_turns_it_on(self):
        out = io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli(["auto", "on", "--project", self.dir]), 0)
        self.assertIn("auto-planning on", out.getvalue())
        self.assertTrue(os.path.isfile(os.path.join(self.dir, ".jevflow", ".gitignore")))
        ctx = self.prompt(env={})["hookSpecificOutput"]["additionalContext"]
        self.assertIn("lay it out as phases", ctx)

    def test_task_prompt_creates_draft_and_binds_session(self):
        project.save_config(self.root, {})
        out = self.prompt()
        p = self.only_flow()
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn(p.flow_id, ctx)
        self.assertIn(os.path.relpath(p.flow, self.dir), ctx)
        self.assertTrue(p.is_draft)
        self.assertIn("add-verbose-flag-cli", p.flow_id)
        self.assertEqual(project.bound_flow(self.root, "s1"), p)
        # a follow-up in the same session belongs to that flow: no second flow
        self.prompt("also make the output coloured when stdout is a terminal please")
        self.assertEqual(len(project.list_flows(self.root)), 1)

    def test_questions_do_not_start_flows(self):
        project.save_config(self.root, {})
        self.assertEqual(self.prompt("what is the difference between a loop and on_fail here?"), {})
        self.assertEqual(project.list_flows(self.root), [])

    def test_draft_stop_blocks_then_gives_up(self):
        project.save_config(self.root, {})
        self.prompt()
        p = self.only_flow()
        for n in range(1, auto.PLAN_BLOCK_LIMIT + 1):
            out = self.hook("Stop")
            self.assertEqual(out.get("decision"), "block", out)
            self.assertIn(f"({n}/{auto.PLAN_BLOCK_LIMIT})", out["reason"])
        out = self.hook("Stop")
        self.assertNotIn("decision", out)
        self.assertIn("abandoned", out["systemMessage"])
        done = project.flow_paths(self.root, p.flow_id)
        self.assertTrue(done.archived)
        with open(os.path.join(done.dir, project.SUMMARY_FILE), encoding="utf-8") as fh:
            self.assertIn("abandoned", fh.read())

    def test_invalid_flow_is_reported(self):
        project.save_config(self.root, {})
        self.prompt()
        p = self.only_flow()
        with open(p.flow, "w", encoding="utf-8") as fh:
            json.dump({"goal": "x", "phases": []}, fh)
        out = self.hook("Stop")
        self.assertEqual(out["decision"], "block")
        self.assertIn("invalid", out["reason"])

    def test_full_lifecycle_to_done_folder(self):
        project.save_config(self.root, {})
        self.prompt()
        p = self.only_flow()
        self.lay_out(p)
        out = self.hook("Stop")
        self.assertIn("archived", out.get("systemMessage", ""), out)
        done = project.flow_paths(self.root, p.flow_id)
        self.assertTrue(done.archived)
        self.assertFalse(os.path.exists(p.dir))
        with open(os.path.join(done.dir, project.SUMMARY_FILE), encoding="utf-8") as fh:
            summary = fh.read()
        self.assertIn("complete", summary)
        self.assertIn("| build | done |", summary)
        # the session's next task starts a fresh flow
        self.prompt("now add a --quiet flag that suppresses everything except errors please")
        active = [x for x, _ in project.list_flows(self.root) if not x.archived]
        self.assertEqual(len(active), 1)
        self.assertNotEqual(active[0].flow_id, p.flow_id)

    def test_failing_check_keeps_flow_active(self):
        project.save_config(self.root, {})
        self.prompt()
        p = self.only_flow()
        self.lay_out(p, check="false")
        out = self.hook("Stop")
        self.assertEqual(out.get("decision"), "block", out)
        self.assertFalse(project.flow_paths(self.root, p.flow_id).archived)

    def test_two_sessions_two_flows(self):
        project.save_config(self.root, {})
        self.prompt(sid="a")
        self.prompt("write a migration script that renames the user table and backfills ids", sid="b")
        ids = {x.flow_id for x, _ in project.list_flows(self.root)}
        self.assertEqual(len(ids), 2)
        pa, pb = project.bound_flow(self.root, "a"), project.bound_flow(self.root, "b")
        self.assertNotEqual(pa, pb)
        self.lay_out(pa)
        self.hook("Stop", sid="a")
        self.assertTrue(project.flow_paths(self.root, pa.flow_id).archived)
        self.assertFalse(project.flow_paths(self.root, pb.flow_id).archived)
        self.assertTrue(pb.is_draft)

    def test_supervised_run_is_not_archived_by_the_hook(self):
        project.save_config(self.root, {})
        self.prompt()
        p = self.only_flow()
        self.lay_out(p)
        env = dict(self.env, JEVFLOW_SUPERVISED="1", JEVFLOW_FLOW=p.flow_id)
        self.hook("Stop", env=env)
        self.assertFalse(project.flow_paths(self.root, p.flow_id).archived)

    def test_legacy_flow_owns_unbound_sessions(self):
        os.makedirs(os.path.join(self.dir, ".jevflow"))
        with open(os.path.join(self.dir, ".jevflow", "flow.json"), "w", encoding="utf-8") as fh:
            json.dump(laid_out("legacy", "false"), fh)
        self.assertEqual(self.prompt(), {})
        self.assertEqual(project.list_flows(self.root), [])

    def test_statusline_follows_the_session(self):
        project.save_config(self.root, {})
        self.prompt()
        p = self.only_flow()
        sess = {"workspace": {"project_dir": self.dir}, "session_id": "s1"}
        self.assertIn("planning", statusline.segment(sess, color=False))
        self.lay_out(p)
        self.hook("Stop")
        seg = statusline.segment(sess, color=False)
        self.assertIn("done", seg)
        self.assertIn(p.flow_id, seg)
        self.assertEqual(statusline.segment(dict(sess, session_id="other"), color=False), "")


if __name__ == "__main__":
    unittest.main()


class TestMultiFlowTools(AutoCase):
    def setUp(self):
        super().setUp()
        project.save_config(self.root, {})
        self.prompt(sid="a")
        self.prompt("write a migration script that renames the user table and backfills ids", sid="b")
        self.pa = project.bound_flow(self.root, "a")
        self.lay_out(self.pa)

    def run_cli(self, *argv):
        import contextlib
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli(list(argv))
        return rc, out.getvalue() + err.getvalue()

    def test_flows_listing(self):
        rc, out = self.run_cli("flows", "--project", self.dir)
        self.assertEqual(rc, 0)
        self.assertIn("draft (not laid out)", out)
        self.assertIn(self.pa.flow_id, out)

    def test_validate_by_flow_id(self):
        self.assertEqual(self.run_cli("validate", "--project", self.dir, "--flow", self.pa.flow_id)[0], 0)
        pb = project.bound_flow(self.root, "b")
        rc, out = self.run_cli("validate", "--project", self.dir, "--flow", pb.flow_id)
        self.assertEqual(rc, 3)
        self.assertIn("draft", out)

    def test_supervisor_needs_flow_choice(self):
        from jevflow import supervisor
        cfg = supervisor.RunConfig(project=self.dir, plugin_dir=self.dir)
        r = supervisor.Supervisor(cfg).run()
        self.assertEqual(r.exit_code, supervisor.EXIT_CONFIG)
        self.assertIn("pick one with --flow", r.detail)
        pb = project.bound_flow(self.root, "b")
        cfg.flow_id = pb.flow_id
        r = supervisor.Supervisor(cfg).run()
        self.assertIn("is a draft", r.detail)
        self.assertEqual(supervisor.parse_args(["--project", self.dir, "--flow", "x"], {}).flow_id, "x")

    def test_viewer_follows_newest_flow(self):
        from jevflow import ui
        t = ui.Target(self.root)
        snap = ui._snap(t)
        self.assertIsNotNone(snap)
        self.lay_out(project.bound_flow(self.root, "b"))
        self.assertIsNotNone(ui._snap(ui.Target(self.root, self.pa.flow_id))["flow"])


class TestLiveProgress(AutoCase):
    def setUp(self):
        super().setUp()
        project.save_config(self.root, {})
        self.prompt()
        self.p = self.only_flow()

    def state(self):
        with open(self.p.state, encoding="utf-8") as fh:
            return json.load(fh)

    def test_laid_out_flow_activates_on_any_tool_event(self):
        self.lay_out(self.p, check="test -f done.txt")
        self.hook("PostToolUse", tool_name="Write", tool_input={"file_path": self.dir + "/x.py"})
        self.assertFalse(self.p.is_draft)
        st = self.state()
        self.assertEqual(st["current_phase"], "build")
        self.assertIn("flow_laid_out", [h["event"] for h in st["history"]])
        self.assertEqual(st["live"]["tool"], "Write")
        self.assertEqual(st["live"]["target"], "x.py")
        self.assertEqual(st["live"]["checks"], {"build": False})

    def test_probe_sees_check_pass_before_stop(self):
        from jevflow import live
        self.lay_out(self.p, check="test -f done.txt")
        self.hook("PostToolUse", tool_name="Bash", tool_input={"command": "ls"})
        open(os.path.join(self.dir, "done.txt"), "w").close()
        self.hook("PostToolUse", tool_name="Bash", tool_input={"command": "touch done.txt"},
                  now=NOW + live.PROBE_EVERY_S + 1)
        st = self.state()
        self.assertEqual(st["live"]["checks"], {"build": True})
        self.assertEqual(st["phase_status"]["build"], "active")  # only Stop advances
        sess = {"workspace": {"project_dir": self.dir}, "session_id": "s1"}
        self.assertIn("1 check passing", statusline.segment(sess, color=False))

    def test_writes_are_throttled(self):
        self.lay_out(self.p)
        self.hook("PostToolUse", tool_name="Read", tool_input={"file_path": "a"})
        m1 = os.stat(self.p.state).st_mtime_ns
        self.hook("PostToolUse", tool_name="Read", tool_input={"file_path": "b"}, now=NOW + 0.5)
        self.assertEqual(os.stat(self.p.state).st_mtime_ns, m1)


class TestClaudeStartsFlow(AutoCase):
    """No `auto on`: Claude decides, runs `jevflow start`, the hook binds the session."""

    def test_hint_when_no_jevflow(self):
        out = self.hook("SessionStart", env={}, source="startup")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("start --name", ctx)
        self.assertEqual(self.hook("SessionStart", env={"JEVFLOW_NO_HINT": "1"}, source="startup"), {})

    def test_start_then_bind_then_track(self):
        import contextlib
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli(["start", "--project", self.dir, "--goal", TASK]), 0)
        text = out.getvalue()
        self.assertIn(auto.START_MARKER, text)
        self.assertIn("lay it out as phases", text)
        p = self.only_flow()
        self.assertIsNone(project.bound_flow(self.root, "s1"))
        # Claude Code sends the Bash result to PostToolUse; that binds this session
        self.hook("PostToolUse", env={}, tool_name="Bash",
                  tool_input={"command": "jevflow start ..."}, tool_response={"stdout": text})
        self.assertEqual(project.bound_flow(self.root, "s1"), p)
        self.assertEqual(self.hook("Stop", env={})["decision"], "block")  # must lay it out
        self.lay_out(p)
        out2 = self.hook("Stop", env={})
        self.assertIn("archived", out2.get("systemMessage", ""))
        # another session in the same project is not captured by this flow
        self.assertIsNone(project.bound_flow(self.root, "s2"))

    def test_unrelated_bash_output_does_not_bind(self):
        os.makedirs(os.path.join(self.dir, ".jevflow"))
        project.save_config(self.root, {})
        self.hook("PostToolUse", env={}, tool_name="Bash", tool_response={"stdout": "hello"})
        self.assertIsNone(project.bound_flow(self.root, "s1"))


class TestPromptNudge(AutoCase):
    def test_task_prompt_gets_nudge_without_jevflow(self):
        out = self.hook("UserPromptSubmit", env={}, prompt=TASK)
        self.assertIn("start a tracked flow", out["hookSpecificOutput"]["additionalContext"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, ".jevflow")))  # nothing created

    def test_question_gets_nothing(self):
        self.assertEqual(self.hook("UserPromptSubmit", env={}, prompt="what does this repo do?"), {})

    def test_no_nudge_while_session_has_a_flow(self):
        os.makedirs(os.path.join(self.dir, ".jevflow"))
        p = project.new_flow(self.root, TASK, NOW)
        project.bind_session(self.root, "s1", p.flow_id)
        out = json.dumps(self.hook("UserPromptSubmit", env={}, prompt=TASK))
        self.assertNotIn("start a tracked flow", out)  # a reminder about its own draft instead

    def test_opt_out(self):
        self.assertEqual(self.hook("UserPromptSubmit", env={"JEVFLOW_NO_HINT": "1"}, prompt=TASK), {})
