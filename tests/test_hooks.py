"""C1/C2: hooks I/O on the A1 fixture payloads, project helpers, status, launcher."""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from jevflow import hooks, policy, project, status  # noqa: E402
from jevflow import __main__ as cli  # noqa: E402
from jevflow.flow import load_flow, parse_flow  # noqa: E402
from jevflow.jev_client import JevConnectionError  # noqa: E402
from jevflow.judge import CheckResult  # noqa: E402
from jevflow.state import load_state, save_state  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures", "hooks")
PY = sys.executable
LAUNCHER = os.path.join(ROOT, "hooks", "jevflow")
NOW = 1_000_000.0


def fixture(name, **over):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        d = json.load(fh)
    d.update(over)
    return d


def flow_doc(**over):
    d = {
        "schema_version": 1, "flow_version": "1",
        "goal": "Toy: create a.txt then b.txt",
        "phases": [
            {"id": "a", "name": "Make a", "done_when": "a.txt exists", "check": "test -f a.txt"},
            {"id": "b", "name": "Make b", "done_when": "b.txt exists", "check": "test -f b.txt"},
        ],
    }
    d.update(over)
    return d


class FakeClient:
    """Scripted Jev: each ask() pops the next answers dict (or raises it)."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls_made = 0

    def ask(self, doc, questions):
        self.calls_made += 1
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def answers(phase, conf=0.95, done=0.9, stuck=0.05, off=0.02, claims=0.1, nxt="advance_phase"):
    return {
        "current_phase": {"choice": phase, "confidence": conf, "probabilities": {phase: conf}},
        "next_action": {"choice": nxt, "confidence": 0.8, "probabilities": {nxt: 0.8}},
        "stuck": {"noul": stuck}, "off_goal": {"noul": off}, "claims_done": {"noul": claims},
        "progress": {"score": 2.0, "confidence": 0.7},
        "phase_done__" + phase: {"noul": done},
    }


class HookCase(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="jevflow-hooks-"))
        os.makedirs(os.path.join(self.dir, ".jevflow"))
        self.write_flow(flow_doc())
        self.env = {}

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_flow(self, doc):
        with open(os.path.join(self.dir, ".jevflow", "flow.json"), "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

    @property
    def paths(self):
        return project.Paths(self.dir)

    def state(self):
        return load_state(self.paths.state, load_flow(self.paths.flow))

    def touch(self, name):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as fh:
            fh.write("x\n")

    def run_hook(self, event, payload, client=None, now=NOW, factory=None):
        payload = dict(payload, cwd=self.dir)
        stdin = io.StringIO(json.dumps(payload))
        stdout = io.StringIO()
        calls = []

        def fac(remaining):
            calls.append(remaining)
            return client

        rc = hooks.main([event], stdin, stdout, env=self.env, now=now,
                        client_factory=factory or fac)
        self.assertEqual(rc, 0)
        text = stdout.getvalue()
        self.assertEqual(text.count("\n"), 1, "exactly one JSON line")
        self.factory_calls = calls
        return json.loads(text)


class TestSessionStart(HookCase):
    def test_startup_injects_context_and_creates_state(self):
        out = self.run_hook("SessionStart", fixture("SessionStart_startup.json"))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("Goal: Toy: create a.txt then b.txt", ctx)
        self.assertIn("> [active] a: Make a", ctx)
        self.assertIn("Current phase: a (Make a). Done when: a.txt exists. Check: `test -f a.txt`.", ctx)
        st = self.state()
        self.assertEqual(st["history"][-1]["event"], "session_start")
        self.assertEqual(st["history"][-1]["source"], "startup")
        self.assertEqual(st["session_id"], fixture("SessionStart_startup.json")["session_id"])

    def test_resume_resets_session_budget_and_reinjects_last_block(self):
        st = self.state()
        st.update(blocks_this_session=5, consecutive_blocks=3, last_block_reason="Continue phase 'a' please")
        save_state(self.paths.state, st)
        out = self.run_hook("SessionStart", fixture("SessionStart_resume.json"))
        self.assertIn("Continue phase 'a' please", out["hookSpecificOutput"]["additionalContext"])
        st = self.state()
        self.assertEqual((st["blocks_this_session"], st["consecutive_blocks"]), (0, 0))

    def test_compact_reinjects_but_keeps_session_budget(self):
        st = self.state()
        st.update(blocks_this_session=4, last_block_reason="X" * 5000)
        save_state(self.paths.state, st)
        out = self.run_hook("SessionStart", fixture("SessionStart_compact.json"))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Last Jevflow instruction", ctx)
        self.assertLess(len(ctx), 2500, "last block reason is truncated")
        self.assertEqual(self.state()["blocks_this_session"], 4)

    def test_startup_does_not_reinject_old_block(self):
        st = self.state()
        st["last_block_reason"] = "old instruction"
        save_state(self.paths.state, st)
        out = self.run_hook("SessionStart", fixture("SessionStart_startup.json"))
        self.assertNotIn("old instruction", out["hookSpecificOutput"]["additionalContext"])

    def test_needs_human_is_surfaced(self):
        st = self.state()
        st["needs_human"] = "Which database?"
        save_state(self.paths.state, st)
        out = self.run_hook("SessionStart", fixture("SessionStart_startup.json"))
        self.assertIn("Which database?", out["hookSpecificOutput"]["additionalContext"])


class TestStop(HookCase):
    def test_no_flow_is_inactive(self):
        os.remove(self.paths.flow)
        out = self.run_hook("Stop", fixture("Stop_first.json"))
        self.assertEqual(out, {})
        self.assertFalse(os.path.exists(self.paths.state))

    def test_failing_check_no_jev_blocks_with_output(self):
        out = self.run_hook("Stop", fixture("Stop_first.json"), client=None)
        self.assertEqual(out["decision"], "block")
        self.assertIn("[jevflow] Phase 'a'", out["reason"])
        self.assertIn("checks-only", out["reason"])
        st = self.state()
        h = st["history"][-1]
        self.assertEqual((h["event"], h["decision"], h["condition"]), ("stop", "BLOCK", "degraded_check_fail"))
        self.assertEqual(st["blocks_this_session"], 1)
        self.assertEqual(h["checks"], {"a": False})

    def test_advance_needs_jev_verify_and_check(self):
        self.touch("a.txt")
        client = FakeClient(answers("a"), {"verify": {"noul": 0.9}})
        out = self.run_hook("Stop", fixture("Stop_first.json"), client=client)
        self.assertEqual(out["decision"], "block")
        self.assertIn("'b'", out["reason"])
        st = self.state()
        self.assertEqual(st["current_phase"], "b")
        self.assertEqual(st["phase_status"]["a"], "done")
        self.assertEqual(st["jev_calls"], 2)
        self.assertEqual(st["history"][-1]["condition"], "advance")
        self.assertEqual(st["history"][-1]["probs"]["verify"], ["a", 0.9])

    def test_premature_completion_blocked(self):
        client = FakeClient(answers("a", done=0.03, claims=0.93, nxt="goal_complete"),
                            {"verify": {"noul": 0.03}})
        out = self.run_hook("Stop", fixture("Stop_first.json", last_assistant_message="All done!"),
                            client=client)
        self.assertEqual(out["decision"], "block")
        self.assertIn("You said the work is done, but it is not", out["reason"])
        self.assertEqual(self.state()["history"][-1]["condition"], "premature_completion")

    def test_goal_complete_allows_stop_and_sets_done(self):
        self.touch("a.txt")
        self.touch("b.txt")
        st = self.state()
        st["phase_status"].update(a="done", b="active")
        st["current_phase"] = "b"
        save_state(self.paths.state, st)
        client = FakeClient(answers("b"), {"verify": {"noul": 0.92}})
        out = self.run_hook("Stop", fixture("Stop_first.json"), client=client)
        self.assertEqual(out, {"systemMessage": "[jevflow] Goal complete."})
        self.assertTrue(self.state()["done"])
        # a later Stop on a done flow is a no-op and calls nothing
        out = self.run_hook("Stop", fixture("Stop_first.json"), client=FakeClient())
        self.assertEqual(out, {})
        self.assertEqual(self.factory_calls, [])

    def test_regression_is_deterministic_and_skips_jev(self):
        st = self.state()
        st["phase_status"].update(a="done", b="active")
        st["current_phase"] = "b"
        save_state(self.paths.state, st)   # a.txt does not exist: a regressed
        out = self.run_hook("Stop", fixture("Stop_first.json"), client=FakeClient())
        self.assertEqual(out["decision"], "block")
        self.assertIn("Regression", out["reason"])
        self.assertEqual(self.factory_calls, [], "no Jev client built for a deterministic outcome")
        self.assertEqual(self.state()["current_phase"], "a")

    def test_block_budget_stops_without_jev(self):
        st = self.state()
        st["blocks_this_session"] = 6
        save_state(self.paths.state, st)
        out = self.run_hook("Stop", fixture("Stop_first.json"), client=FakeClient())
        self.assertIn("block budget reached", out["systemMessage"])
        self.assertEqual(self.factory_calls, [])

    def test_hook_cap_via_stop_hook_active(self):
        st = self.state()
        st["consecutive_blocks"] = policy.CLAUDE_BLOCK_CAP - 1
        st["blocks_this_session"] = 0
        self.write_flow(flow_doc(limits={"max_blocks_per_session": 50}))
        save_state(self.paths.state, st)
        out = self.run_hook("Stop", fixture("Stop_active.json"), client=FakeClient())
        self.assertNotIn("decision", out)
        self.assertEqual(self.state()["history"][-1]["condition"], "hook_cap")

    def test_consecutive_blocks_counted_across_hook_calls(self):
        self.run_hook("Stop", fixture("Stop_first.json"))
        self.run_hook("Stop", fixture("Stop_active.json"))
        self.assertEqual(self.state()["consecutive_blocks"], 2)
        self.run_hook("Stop", fixture("Stop_first.json"))
        self.assertEqual(self.state()["consecutive_blocks"], 1)

    def test_jev_budget_passed_to_factory(self):
        self.write_flow(flow_doc(limits={"max_jev_calls": 10}))
        st = self.state()
        st["jev_calls"] = 7
        save_state(self.paths.state, st)
        self.run_hook("Stop", fixture("Stop_first.json"), client=None)
        self.assertEqual(self.factory_calls, [3])

    def test_jev_error_degrades_and_records(self):
        client = FakeClient(JevConnectionError("down"))
        out = self.run_hook("Stop", fixture("Stop_first.json"), client=client)
        self.assertEqual(out["decision"], "block")
        st = self.state()
        self.assertEqual(st["last_jev_error"]["error"], "JevConnectionError: down")
        self.assertIsNone(st["last_error"], "Claude API error slot untouched")
        self.assertEqual(st["jev_calls"], 1, "a failed call still counts")
        self.assertEqual(st["history"][-1]["condition"], "degraded_check_fail")

    def test_no_check_no_jev_allows_stop(self):
        self.write_flow(flow_doc(phases=[{"id": "a", "name": "A", "done_when": "x"}]))
        out = self.run_hook("Stop", fixture("Stop_first.json"), client=None)
        self.assertNotIn("decision", out)
        self.assertIn("no evidence to block on", out["systemMessage"])

    def test_ask_human_writes_needs_human(self):
        self.write_flow(flow_doc(phases=[
            {"id": "a", "name": "A", "done_when": "a passes", "check": "false",
             "loop": {"max_iterations": 1, "until": "false"}},
            {"id": "b", "name": "B", "done_when": "b"}]))
        st = self.state()
        st["loop_iterations"] = {"a": 1}
        save_state(self.paths.state, st)
        out = self.run_hook("Stop", fixture("Stop_first.json"), client=FakeClient())
        self.assertNotIn("decision", out)
        self.assertTrue(os.path.isfile(self.paths.needs_human))
        with open(self.paths.needs_human, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("loop ran 1 of 1", body)
        self.assertTrue(self.state()["needs_human"])

    def test_warn_mode_never_blocks(self):
        self.write_flow(flow_doc(mode="warn"))
        out = self.run_hook("Stop", fixture("Stop_first.json"), client=None)
        self.assertNotIn("decision", out)
        self.assertIn("[jevflow warn] would block", out["systemMessage"])
        st = self.state()
        self.assertEqual(st["blocks_this_session"], 0)
        self.assertFalse(st["history"][-1]["enforced"])

    def test_observe_mode_is_silent(self):
        self.write_flow(flow_doc(mode="observe"))
        out = self.run_hook("Stop", fixture("Stop_first.json"), client=None)
        self.assertEqual(out, {})
        self.assertEqual(self.state()["history"][-1]["decision"], "BLOCK")

    def _exhausted_loop_flow(self, mode):
        self.write_flow(flow_doc(mode=mode, phases=[
            {"id": "a", "name": "A", "done_when": "a passes", "check": "false",
             "loop": {"max_iterations": 1, "until": "false"}},
            {"id": "b", "name": "B", "done_when": "b"}]))
        st = self.state()
        st["loop_iterations"] = {"a": 1}
        save_state(self.paths.state, st)

    def test_observe_ask_human_does_not_pause(self):
        self._exhausted_loop_flow("observe")
        out = self.run_hook("Stop", fixture("Stop_first.json"), client=FakeClient())
        self.assertEqual(out, {})
        self.assertFalse(os.path.exists(self.paths.needs_human))
        st = self.state()
        self.assertFalse(st.get("needs_human"))
        self.assertEqual(st["history"][-1]["condition"], "loop_exhausted")

    def test_warn_ask_human_pauses_with_message(self):
        self._exhausted_loop_flow("warn")
        out = self.run_hook("Stop", fixture("Stop_first.json"), client=FakeClient())
        self.assertNotIn("decision", out)
        self.assertIn("[jevflow warn] needs a human", out["systemMessage"])
        self.assertTrue(os.path.isfile(self.paths.needs_human))
        self.assertTrue(self.state()["needs_human"])

    def test_corrupt_state_fails_open(self):
        with open(self.paths.state, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        out = self.run_hook("Stop", fixture("Stop_first.json"))
        self.assertNotIn("decision", out)
        self.assertIn("disabled for this stop", out["systemMessage"])

    def test_invalid_flow_fails_open(self):
        self.write_flow({"goal": "x", "phases": [], "bogus": 1})
        out = self.run_hook("Stop", fixture("Stop_first.json"))
        self.assertNotIn("decision", out)

    def test_unwritable_state_fails_open(self):
        with mock.patch("jevflow.state.save_state", side_effect=OSError("disk full")):
            out = self.run_hook("Stop", fixture("Stop_first.json"))
        self.assertEqual(out, {}, "if the journal cannot persist, never block")

    def test_internal_error_fails_open(self):
        def boom(_):
            raise RuntimeError("bug")
        out = self.run_hook("Stop", fixture("Stop_first.json"), factory=boom)
        self.assertEqual(out, {})

    def test_garbage_stdin(self):
        for raw in ("", "not json", "[1,2]", "null"):
            stdout = io.StringIO()
            rc = hooks.main(["Stop"], io.StringIO(raw), stdout, env={})
            self.assertEqual((rc, json.loads(stdout.getvalue())), (0, {}))

    def test_unknown_event(self):
        self.assertEqual(self.run_hook("PreCompact", fixture("PreCompact_manual.json")), {})

    def test_event_from_payload_when_no_argv(self):
        stdout = io.StringIO()
        hooks.main([], io.StringIO(json.dumps(fixture("SessionStart_startup.json", cwd=self.dir))),
                   stdout, env={}, now=NOW)
        self.assertIn("hookSpecificOutput", json.loads(stdout.getvalue()))

    def test_claude_project_dir_is_authoritative(self):
        other = os.path.realpath(tempfile.mkdtemp())
        try:
            sub = os.path.join(self.dir, "pkg")
            os.makedirs(sub)
            payload = json.dumps(fixture("SessionStart_startup.json", cwd=sub))
            stdout = io.StringIO()
            hooks.main(["SessionStart"], io.StringIO(payload), stdout,
                       env={"CLAUDE_PROJECT_DIR": other}, now=NOW)
            self.assertEqual(json.loads(stdout.getvalue()), {}, "parent flow must not capture another project")
            stdout = io.StringIO()
            hooks.main(["SessionStart"], io.StringIO(payload), stdout,
                       env={"CLAUDE_PROJECT_DIR": self.dir}, now=NOW)
            self.assertIn("hookSpecificOutput", json.loads(stdout.getvalue()))
        finally:
            shutil.rmtree(other)

    def test_finds_flow_from_subdirectory(self):
        sub = os.path.join(self.dir, "pkg", "deep")
        os.makedirs(sub)
        stdout = io.StringIO()
        hooks.main(["SessionStart"], io.StringIO(json.dumps(fixture("SessionStart_startup.json", cwd=sub))),
                   stdout, env={}, now=NOW)
        self.assertIn("hookSpecificOutput", json.loads(stdout.getvalue()))


class TestStopFailure(HookCase):
    def test_records_error_for_supervisor(self):
        out = self.run_hook("StopFailure", fixture("StopFailure_rate_limit.json"))
        self.assertEqual(out, {})
        st = self.state()
        self.assertEqual(st["last_error"]["error"], "rate_limit")
        self.assertEqual(st["last_error"]["source"], "claude")
        self.assertEqual(st["history"][-1]["event"], "stop_failure")


class TestLockedWriters(HookCase):
    """SessionStart and StopFailure take the same state lock as the gate hooks,
    so a concurrent gate write is never overwritten."""

    def _blocked_while_locked(self, event, fixture_name):
        import fcntl
        import threading
        self.run_hook("SessionStart", fixture("SessionStart_startup.json"))
        before = len(self.state()["history"])
        lock = open(self.paths.state + ".lock", "a")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        done = threading.Event()
        t = threading.Thread(target=lambda: (self.run_hook(event, fixture(fixture_name)), done.set()))
        t.start()
        try:
            self.assertFalse(done.wait(0.5), f"{event} wrote state while the lock was held")
            # a gate write lands while the hook waits; it must survive
            st = self.state()
            st["jev_calls"] = 7
            save_state(self.paths.state, st)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()
        t.join(10)
        self.assertTrue(done.is_set())
        st = self.state()
        self.assertEqual(st["jev_calls"], 7)
        self.assertEqual(len(st["history"]), before + 1)

    def test_session_start_waits_for_lock(self):
        self._blocked_while_locked("SessionStart", "SessionStart_compact.json")

    def test_stop_failure_waits_for_lock(self):
        self._blocked_while_locked("StopFailure", "StopFailure_rate_limit.json")


class TestProject(HookCase):
    def test_check_env_scrubs_key(self):
        with mock.patch.dict(os.environ, {"JEV_API_KEY": "sekrit-test-value", "JEVFLOW_KEY_FILE": "/x"}):
            r = project.run_check('echo "k=${JEV_API_KEY:-none} f=${JEVFLOW_KEY_FILE:-none}"', self.dir, 10)
        self.assertTrue(r.passed)
        self.assertEqual(r.output.strip(), "k=none f=none")

    def test_check_env_restores_user_pythonpath(self):
        # the wrapper prepends the plugin root; checks must see the user's value
        with mock.patch.dict(os.environ, {"PYTHONPATH": "/plugin:/user/lib",
                                          "JEVFLOW_ORIG_PYTHONPATH": "/user/lib"}):
            r = project.run_check('echo "p=${PYTHONPATH-unset} o=${JEVFLOW_ORIG_PYTHONPATH-unset}"',
                                  self.dir, 10)
        self.assertEqual(r.output.strip(), "p=/user/lib o=unset")
        with mock.patch.dict(os.environ, {"PYTHONPATH": "/plugin", "JEVFLOW_ORIG_PYTHONPATH": ""}):
            r = project.run_check('echo "p=${PYTHONPATH-unset}"', self.dir, 10)
        self.assertEqual(r.output.strip(), "p=unset")

    def test_check_timeout(self):
        r = project.run_check("sleep 5", self.dir, 0.3)
        self.assertFalse(r.passed)
        self.assertIn("timed out", r.output)

    def test_check_output_capped(self):
        r = project.run_check("yes x | head -c 20000; false", self.dir, 10)
        self.assertFalse(r.passed)
        self.assertLessEqual(len(r.output), project.CHECK_OUTPUT_KEEP)

    def test_checks_budget_exhausted(self):
        flow = load_flow(self.paths.flow)
        t = iter([0.0, 1000.0, 1000.0, 1000.0])
        checks, _ = project.run_checks(flow, self.state(), self.dir, clock=lambda: next(t))
        self.assertIn("time budget", checks["a"].output)

    def test_which_checks_run(self):
        flow = parse_flow(flow_doc(phases=[
            {"id": "a", "name": "A", "done_when": "a", "check": "true"},
            {"id": "b", "name": "B", "done_when": "b", "check": "true", "on_fail": "dbg"},
            {"id": "c", "name": "C", "done_when": "c", "check": "true"},
            {"id": "dbg", "name": "D", "done_when": "d", "check": "true"}]))
        st = {"current_phase": "b", "phase_status": {"a": "done", "b": "active", "c": "pending", "dbg": "done"}}
        self.assertEqual(project.checks_to_run(flow, st), ["a", "b"])

    def test_git_changes_counts_only_without_send_diff(self):
        if shutil.which("git") is None:
            self.skipTest("git missing")
        g = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]
        subprocess.run(g + ["init", "-q"], cwd=self.dir, check=True)
        self.touch("a.txt")
        subprocess.run(g + ["add", "a.txt"], cwd=self.dir, check=True)
        subprocess.run(g + ["commit", "-qm", "init"], cwd=self.dir, check=True)
        with open(os.path.join(self.dir, "a.txt"), "a", encoding="utf-8") as fh:
            fh.write("SECRET-CONTENT\n")
        self.touch("new.txt")
        ch = project.git_changes(self.dir, send_diff=False)
        by = {e["path"]: e for e in ch}
        self.assertEqual((by["a.txt"]["added"], by["new.txt"]["added"]), (1, 1))
        self.assertNotIn("diff", by["a.txt"])
        self.assertFalse(any(p.startswith(".jevflow/") for p in by))
        self.assertNotIn("SECRET-CONTENT", json.dumps(ch))
        with_diff = project.git_changes(self.dir, send_diff=True)
        self.assertIn("SECRET-CONTENT", {e["path"]: e for e in with_diff}["a.txt"]["diff"])

    def test_git_changes_scoped_to_project_subdir(self):
        # E2: a project nested inside a larger repo must not report the
        # parent repo's changes, and paths are project-relative
        if shutil.which("git") is None:
            self.skipTest("git missing")
        g = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]
        subprocess.run(g + ["init", "-q"], cwd=self.dir, check=True)
        sub = os.path.join(self.dir, "examples", "proj")
        os.makedirs(sub)
        for rel in ("outside.txt", "examples/proj/inside.txt"):
            self.touch(rel)
        subprocess.run(g + ["add", "-A"], cwd=self.dir, check=True)
        subprocess.run(g + ["commit", "-qm", "init"], cwd=self.dir, check=True)
        for rel in ("outside.txt", "examples/proj/inside.txt"):
            with open(os.path.join(self.dir, rel), "a", encoding="utf-8") as fh:
                fh.write("more\n")
        self.touch("stray.txt")
        self.touch("examples/proj/new.txt")
        paths = sorted(e["path"] for e in project.git_changes(sub))
        self.assertEqual(paths, ["inside.txt", "new.txt"])

    def test_git_budget_exhausted(self):
        t = iter([0.0] + [1e9] * 10)   # deadline set at 0, then already past
        with mock.patch("jevflow.project.subprocess.run") as run:
            self.assertEqual(project.git_changes(self.dir, clock=lambda: next(t)), [])
        run.assert_not_called()

    def test_git_changes_outside_repo(self):
        self.assertEqual(project.git_changes(self.dir), [])


class TestStatus(HookCase):
    def test_status_table_on_fixture(self):
        self.run_hook("SessionStart", fixture("SessionStart_startup.json"))
        self.run_hook("Stop", fixture("Stop_first.json"), client=None)
        out, err = io.StringIO(), io.StringIO()
        rc = status.main(["--project", self.dir], out, err)
        self.assertEqual(rc, 0, err.getvalue())
        text = out.getvalue()
        self.assertRegex(text, r"PHASE\s+STATUS\s+CHECK\s+NOTES")
        self.assertRegex(text, r">\s+a\s+active\s+yes")
        self.assertRegex(text, r"\n\s+b\s+pending\s+yes")
        self.assertIn("BLOCK/degraded_check_fail", text)
        self.assertIn("Blocks this session: 1/6", text)
        self.assertNotIn("NEEDS_HUMAN", text)

    def test_status_shows_needs_human(self):
        with open(self.paths.needs_human, "w", encoding="utf-8") as fh:
            fh.write("# Jevflow needs a human\n\nPick one.\n")
        out = io.StringIO()
        self.assertEqual(status.main(["--project", self.dir], out, io.StringIO()), 0)
        self.assertIn("NEEDS_HUMAN:", out.getvalue())
        self.assertIn("Pick one.", out.getvalue())

    def test_status_no_flow(self):
        err = io.StringIO()
        empty = tempfile.mkdtemp()
        try:
            self.assertEqual(status.main(["--project", empty], io.StringIO(), err), 3)
        finally:
            shutil.rmtree(empty)
        self.assertIn("no flow", err.getvalue())

    def test_status_json(self):
        out = io.StringIO()
        self.assertEqual(status.main(["--project", self.dir, "--json"], out, io.StringIO()), 0)
        self.assertEqual(json.loads(out.getvalue())["current_phase"], "a")


class TestPluginFiles(unittest.TestCase):
    def test_manifest_and_hooks_json(self):
        with open(os.path.join(ROOT, ".claude-plugin", "plugin.json"), encoding="utf-8") as fh:
            man = json.load(fh)
        self.assertEqual(man["name"], "jevflow")
        with open(os.path.join(ROOT, "hooks", "hooks.json"), encoding="utf-8") as fh:
            hk = json.load(fh)["hooks"]
        self.assertEqual(set(hk), {"SessionStart", "Stop", "StopFailure", "PreToolUse", "PostToolUse",
                                   "SubagentStop", "TaskCompleted", "UserPromptSubmit"})
        for ev, groups in hk.items():
            cmd = groups[0]["hooks"][0]["command"]
            self.assertIn("${CLAUDE_PLUGIN_ROOT}/hooks/jevflow", cmd)
            self.assertTrue(cmd.endswith("hook " + ev))
        self.assertTrue(os.access(LAUNCHER, os.X_OK))

    def test_command_and_skill_files(self):
        for rel in ("commands/init.md", "commands/status.md", "commands/ui.md", "commands/statusline.md",
                    "commands/auto.md", "skills/jevflow/SKILL.md"):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                text = fh.read()
            self.assertTrue(text.startswith("---\n"), rel)
            self.assertIn("description:", text.split("---")[1], rel)
            self.assertNotIn("\u2014", text, f"em-dash in {rel}")

    def test_init_example_flow_is_valid(self):
        with open(os.path.join(ROOT, "commands", "init.md"), encoding="utf-8") as fh:
            m = re.search(r"```json\n(.*?)```", fh.read(), re.S)
        flow = parse_flow(json.loads(m.group(1)))
        self.assertEqual(flow.mode, "warn")
        self.assertIn("debug", flow.branch_only)

    def test_no_personal_paths_in_shipped_code(self):
        bad = re.compile(r"/(local/)?home/parthvp|parthvp")
        for d in ("jevflow", "hooks", "commands", "skills", ".claude-plugin"):
            for base, _, files in os.walk(os.path.join(ROOT, d)):
                for f in files:
                    if f.endswith(".pyc"):
                        continue
                    with open(os.path.join(base, f), encoding="utf-8", errors="replace") as fh:
                        self.assertIsNone(bad.search(fh.read()), os.path.join(base, f))


class TestLauncher(HookCase):
    """The real shell launcher and process boundary, Jev disabled."""

    def launch(self, *args, stdin="", env=None):
        e = {k: v for k, v in os.environ.items() if k not in ("JEV_API_KEY", "JEVFLOW_KEY_FILE")}
        e.update({"JEVFLOW_PYTHON": PY, "JEVFLOW_NO_JEV": "1", "CLAUDE_PLUGIN_ROOT": ROOT})
        e.update(env or {})
        return subprocess.run([LAUNCHER, *args], input=stdin.encode(), capture_output=True,
                              env=e, timeout=60)

    def test_stop_through_launcher(self):
        p = self.launch("hook", "Stop", stdin=json.dumps(fixture("Stop_first.json", cwd=self.dir)))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["decision"], "block")

    def test_status_through_launcher(self):
        p = self.launch("status", "--project", self.dir)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(b"PHASE", p.stdout)

    def test_validate_through_launcher(self):
        p = self.launch("validate", "--project", self.dir)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(b"ok: 2 phases (a -> b)", p.stdout)
        self.write_flow({"goal": "x"})
        self.assertEqual(self.launch("validate", "--project", self.dir).returncode, 3)

    def test_silent_shim_python_is_skipped(self):
        shim = os.path.join(self.dir, "fakepy")
        with open(shim, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(shim, 0o755)
        # PATH holds only the shim under every candidate name, so nothing works
        bindir = os.path.join(self.dir, "bin")
        os.makedirs(bindir)
        for n in ("python3.13", "python3.12", "python3.11", "python3.10", "python3", "python"):
            os.symlink(shim, os.path.join(bindir, n))
        path = bindir + ":/usr/bin:/bin"
        e = {"JEVFLOW_PYTHON": shim, "PATH": path}
        p = self.launch("hook", "Stop", stdin="{}", env=e)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(json.loads(p.stdout), {})
        self.assertIn(b"no working Python", p.stderr)
        p = self.launch("status", env=e)
        self.assertEqual(p.returncode, 3)

    def test_python_crash_still_exits_0(self):
        bad = os.path.join(self.dir, "crashpy")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write(f'#!/bin/sh\nif [ "$1" = "-c" ]; then exec "{PY}" "$@"; fi\nexit 2\n')
        os.chmod(bad, 0o755)
        p = self.launch("hook", "Stop", stdin="{}", env={"JEVFLOW_PYTHON": bad})
        self.assertEqual(p.returncode, 0)
        self.assertEqual(json.loads(p.stdout), {})


class TestCli(unittest.TestCase):
    def test_usage_exit_codes(self):
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(cli.main([]), 3)
            self.assertEqual(cli.main(["--help"]), 0)


if __name__ == "__main__":
    unittest.main()
