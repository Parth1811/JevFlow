"""D1: supervisor with a fake ``claude`` binary (SPEC 6, 10.2, 10.5)."""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from jevflow import supervisor as sv  # noqa: E402
from jevflow.flow import load_flow  # noqa: E402
from jevflow.state import load_state, new_state, save_state  # noqa: E402

PY = sys.executable

# The fake claude: one scripted action per launch. It edits state.json the
# way the real hooks would (done flag, StopFailure last_error, needs_human).
FAKE = r'''#!{py}
import json, os, subprocess, sys, time
d = os.environ["FAKE_DIR"]
actions = json.load(open(os.path.join(d, "scenario.json")))
cpath = os.path.join(d, "count")
n = int(open(cpath).read()) if os.path.exists(cpath) else 0
open(cpath, "w").write(str(n + 1))
with open(os.path.join(d, "argv.jsonl"), "a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\n")
act = actions[min(n, len(actions) - 1)]
sp = os.path.join(os.getcwd(), ".jevflow", "state.json")
def edit(fn):
    s = json.load(open(sp)); fn(s)
    tmp = sp + ".fake"; json.dump(s, open(tmp, "w")); os.replace(tmp, sp)
out = {{"type": "result", "subtype": "success", "session_id": "sess-%d" % (n + 1)}}
if act == "done":
    edit(lambda s: s.update(done=True))
elif act == "api_error":
    edit(lambda s: s.update(last_error={{"source": "claude", "error": "rate_limit", "ts": time.time()}}))
    print(json.dumps(dict(out, is_error=True))); sys.exit(1)
elif act == "needs_human":
    edit(lambda s: s.update(needs_human="which database?"))
    open(os.path.join(os.getcwd(), ".jevflow", "NEEDS_HUMAN.md"), "w").write("q")
elif act == "hang":
    child = subprocess.Popen(["sleep", "60"])
    open(os.path.join(d, "grandchild.pid"), "w").write(str(child.pid))
    time.sleep(60)
elif act == "no_output":
    sys.exit(1)
elif act == "denied":
    out["permission_denials"] = [{{"tool_name": "Write"}}, {{"tool_name": "Bash"}}]
print(json.dumps(out))
'''


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # a zombie still answers kill(0); treat it as dead
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split()[2] != "Z"
    except OSError:
        return False


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jevflow-sup-")
        self.proj = os.path.join(self.tmp, "proj")
        self.fake_dir = os.path.join(self.tmp, "fake")
        os.makedirs(os.path.join(self.proj, ".jevflow"))
        os.makedirs(self.fake_dir)
        self.bin = os.path.join(self.fake_dir, "claude")
        with open(self.bin, "w") as fh:
            fh.write(FAKE.format(py=PY))
        os.chmod(self.bin, 0o755)
        self.write_flow()
        self.sleeps = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_flow(self, **limits):
        lim = {"max_restarts": 3, "max_total_minutes": 30}
        lim.update(limits)
        doc = {"schema_version": 1, "flow_version": "1", "goal": "Toy: write a.txt",
               "phases": [{"id": "a", "name": "A", "done_when": "a.txt exists",
                           "check": "test -f a.txt"}],
               "limits": lim}
        with open(os.path.join(self.proj, ".jevflow", "flow.json"), "w") as fh:
            json.dump(doc, fh)

    def scenario(self, *actions):
        with open(os.path.join(self.fake_dir, "scenario.json"), "w") as fh:
            json.dump(list(actions), fh)

    def argvs(self):
        p = os.path.join(self.fake_dir, "argv.jsonl")
        if not os.path.exists(p):
            return []
        with open(p) as fh:
            return [json.loads(line) for line in fh]

    def state(self):
        flow = load_flow(os.path.join(self.proj, ".jevflow", "flow.json"))
        return load_state(os.path.join(self.proj, ".jevflow", "state.json"), flow)

    def seed_state(self, **over):
        flow = load_flow(os.path.join(self.proj, ".jevflow", "flow.json"))
        s = new_state(flow)
        s.update(over)
        save_state(os.path.join(self.proj, ".jevflow", "state.json"), s)

    def sup(self, **cfg):
        c = sv.RunConfig(project=self.proj, plugin_dir=ROOT, claude_bin=self.bin,
                         poll_s=0.1, backoff_base_s=30.0)
        for k, v in cfg.items():
            setattr(c, k, v)
        env = dict(os.environ, FAKE_DIR=self.fake_dir)
        return sv.Supervisor(c, sleep=self.sleeps.append, env=env)

    def events(self):
        return [h["event"] for h in self.state()["history"]]

    # ------------------------------------------------------------ core loop

    def test_stop_on_done(self):
        self.scenario("done")
        r = self.sup().run()
        self.assertEqual((r.exit_code, r.outcome, r.runs, r.restarts), (0, "goal_complete", 1, 0))
        argv = self.argvs()[0]
        self.assertEqual(argv[0], "-p")
        self.assertIn("Goal: Toy: write a.txt", argv[1])
        self.assertIn("Current phase: a (A)", argv[1])
        for flag in ("--plugin-dir", "--output-format", "--permission-mode"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--plugin-dir") + 1], ROOT)
        self.assertNotIn("--resume", argv)
        self.assertIn("supervisor_end", self.events())
        with open(os.path.join(self.proj, ".jevflow", "last_run.json")) as fh:
            self.assertEqual(json.load(fh)["outcome"], "goal_complete")

    def test_restart_after_not_done_resumes_with_block_reason(self):
        self.seed_state(last_block_reason="Phase a: a.txt is missing; create it.")
        self.scenario("not_done", "done")
        r = self.sup(max_turns=25).run()
        self.assertEqual((r.exit_code, r.runs, r.restarts), (0, 2, 1))
        second = self.argvs()[1]
        self.assertEqual(second[second.index("--resume") + 1], "sess-1")
        self.assertIn("Last Jevflow instruction: Phase a: a.txt is missing", second[1])
        self.assertEqual(second[second.index("--max-turns") + 1], "25")
        restarts = [h for h in self.state()["history"] if h["event"] == "supervisor_restart"]
        self.assertEqual([h["why"] for h in restarts], ["not_done"])

    def test_stop_at_max_restarts(self):
        self.write_flow(max_restarts=2)
        self.scenario("not_done")
        r = self.sup().run()
        self.assertEqual((r.exit_code, r.outcome, r.runs, r.restarts), (2, "budget", 3, 2))
        self.assertIn("restart budget reached (2)", r.detail)
        self.assertEqual(self.state()["restarts"], 2)
        self.assertIn("restarts 2/2", r.text())

    def test_permission_denials_reported(self):
        # E2 finding: a session whose writes are refused ends early; the
        # report must say so instead of only showing a stuck phase
        self.write_flow(max_restarts=0)
        self.scenario("denied")
        r = self.sup().run()
        self.assertEqual((r.exit_code, r.runs, r.permission_denials), (2, 1, 2))
        self.assertIn("claude permission denials 2", r.text())
        end = [h for h in self.state()["history"] if h["event"] == "supervisor_end"][-1]
        self.assertEqual(end["permission_denials"], 2)

    def test_restart_count_persists_across_supervisors(self):
        self.write_flow(max_restarts=2)
        self.seed_state(restarts=2)
        self.scenario("not_done")
        r = self.sup().run()
        self.assertEqual((r.exit_code, r.runs), (2, 1))

    def test_resume_dropped_when_resumed_run_gives_no_result(self):
        self.seed_state(session_id="stale-session")
        self.scenario("no_output", "done")
        r = self.sup().run()
        self.assertEqual(r.exit_code, 0)
        a1, a2 = self.argvs()
        self.assertEqual(a1[a1.index("--resume") + 1], "stale-session")
        self.assertNotIn("--resume", a2)

    # ------------------------------------------------------------ watchdog

    def test_hang_kills_process_group_and_restarts(self):
        self.scenario("hang", "done")
        t0 = time.monotonic()
        r = self.sup(hang_s=1.0).run()
        self.assertLess(time.monotonic() - t0, 30)
        self.assertEqual((r.exit_code, r.hang_kills, r.restarts, r.runs), (0, 1, 1, 2))
        with open(os.path.join(self.fake_dir, "grandchild.pid")) as fh:
            gpid = int(fh.read())
        deadline = time.monotonic() + 5
        while pid_alive(gpid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(pid_alive(gpid), "grandchild of the hung claude survived")
        why = [h.get("why") for h in self.state()["history"] if h["event"] == "supervisor_restart"]
        self.assertEqual(why, ["hang"])

    def test_active_transcript_is_not_a_hang(self):
        # the child keeps touching its transcript: no kill even though it runs past hang_s
        transcript = os.path.join(self.tmp, "t.jsonl")
        open(transcript, "w").close()
        self.seed_state(transcript_path=transcript)
        with open(os.path.join(self.fake_dir, "scenario.json"), "w") as fh:
            json.dump(["done"], fh)
        toucher = os.path.join(self.fake_dir, "claude")
        with open(toucher, "w") as fh:
            fh.write(f"#!{PY}\n"
                     "import json, os, time\n"
                     f"t = {transcript!r}\n"
                     "for _ in range(12):\n"
                     "    os.utime(t); time.sleep(0.25)\n"
                     "sp = os.path.join(os.getcwd(), '.jevflow', 'state.json')\n"
                     "s = json.load(open(sp)); s['done'] = True\n"
                     "json.dump(s, open(sp + '.x', 'w')); os.replace(sp + '.x', sp)\n"
                     "print(json.dumps({'session_id': 'x'}))\n")
        # state.json is only rewritten at the end, so only the transcript shows progress
        r = self.sup(hang_s=1.0).run()
        self.assertEqual((r.exit_code, r.hang_kills, r.runs), (0, 0, 1))

    def test_runaway_session_killed_at_time_budget(self):
        # busy (not hung) child past max_total_minutes + grace: killed, exit 2
        self.write_flow(max_total_minutes=1)
        self.seed_state(started_at=time.time() - 58.0)  # 2 s of budget left
        self.scenario("hang", "done")
        t0 = time.monotonic()
        r = self.sup(hang_s=600.0, overtime_grace_s=0.0).run()
        self.assertLess(time.monotonic() - t0, 30)
        self.assertEqual((r.exit_code, r.outcome, r.overtime_kills, r.runs), (2, "budget", 1, 1))
        self.assertIn("still running", r.detail)

    # ------------------------------------------------------------ lease

    def test_live_lease_refused_and_state_untouched(self):
        self.scenario("done")
        lock = os.path.join(self.proj, ".jevflow", "lock")
        holder = subprocess.Popen(
            [PY, "-c", "import fcntl, sys, time\n"
             f"fh = open({lock!r}, 'a+'); fcntl.flock(fh, fcntl.LOCK_EX)\n"
             "fh.write('{\"pid\": 1}'); fh.flush(); print('held', flush=True); time.sleep(30)"],
            stdout=subprocess.PIPE)
        try:
            self.assertEqual(holder.stdout.readline().strip(), b"held")
            r = self.sup().run()
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()
        self.assertEqual((r.exit_code, r.outcome, r.runs), (5, "lease_held", 0))
        self.assertIn('"pid": 1', r.detail)
        self.assertFalse(os.path.exists(os.path.join(self.proj, ".jevflow", "state.json")))
        self.assertEqual(self.argvs(), [])

    def test_two_leases_exclude_each_other(self):
        lock = os.path.join(self.proj, ".jevflow", "lock")
        a = sv.Lease(lock).acquire(1.0)
        try:
            with self.assertRaises(sv.LeaseHeld):
                sv.Lease(lock).acquire(2.0)
        finally:
            a.release()
        sv.Lease(lock).acquire(3.0).release()  # free again after release

    def test_stale_lease_taken_over(self):
        self.scenario("done")
        with open(os.path.join(self.proj, ".jevflow", "lock"), "w") as fh:
            fh.write(json.dumps({"pid": 999999, "host": "gone", "ts": 1.0}))
        r = self.sup().run()
        self.assertEqual(r.exit_code, 0)
        start = [h for h in self.state()["history"] if h["event"] == "supervisor_start"][0]
        self.assertTrue(start["took_over_stale_lease"])
        with open(os.path.join(self.proj, ".jevflow", "lock")) as fh:
            self.assertEqual(fh.read(), "")  # released on exit

    def test_lease_released_so_second_run_works(self):
        self.scenario("not_done", "done")
        self.write_flow(max_restarts=0)
        self.assertEqual(self.sup().run().exit_code, 2)
        # lease was released: the rerun gets its one launch (no exit 5) and finishes
        self.assertEqual(self.sup().run().exit_code, 0)

    # ------------------------------------------------------------ API failure backoff

    def test_api_error_backoff_not_counted_as_restart(self):
        self.write_flow(max_restarts=0)
        self.scenario("api_error", "api_error", "done")
        r = self.sup().run()
        self.assertEqual((r.exit_code, r.restarts, r.api_backoffs, r.runs), (0, 0, 2, 3))
        self.assertEqual(self.sleeps, [30.0, 60.0])
        third = self.argvs()[2]
        self.assertEqual(third[third.index("--resume") + 1], "sess-2")  # resume after API error

    def test_api_error_backoff_is_capped(self):
        self.scenario("api_error")
        r = self.sup(max_api_failures=3).run()
        self.assertEqual((r.exit_code, r.outcome, r.runs), (2, "api_errors", 3))
        self.assertEqual(self.sleeps, [30.0, 60.0])

    def test_old_api_error_does_not_trigger_backoff(self):
        # a last_error from before this launch is not this run's failure
        self.seed_state(last_error={"source": "claude", "error": "rate_limit", "ts": 1.0})
        self.scenario("not_done", "done")
        r = self.sup().run()
        self.assertEqual((r.exit_code, r.api_backoffs, r.restarts), (0, 0, 1))
        self.assertEqual(self.sleeps, [])

    # ------------------------------------------------------------ human + budgets

    def test_needs_human_exits_4_without_restart(self):
        self.scenario("needs_human", "done")
        r = self.sup().run()
        self.assertEqual((r.exit_code, r.outcome, r.runs, r.restarts), (4, "needs_human", 1, 0))
        self.assertIn("which database?", r.detail)

    def test_pending_human_blocks_start(self):
        self.seed_state(needs_human="q")
        open(os.path.join(self.proj, ".jevflow", "NEEDS_HUMAN.md"), "w").close()
        self.scenario("done")
        r = self.sup().run()
        self.assertEqual((r.exit_code, r.runs), (4, 0))

    def test_deleting_needs_human_file_resolves(self):
        self.seed_state(needs_human="q", escalations=2)
        self.scenario("done")
        r = self.sup().run()
        self.assertEqual((r.exit_code, r.runs), (0, 1))
        self.assertIn("human_resolved", self.events())

    def test_time_budget_stops_before_launch(self):
        self.seed_state(started_at=time.time() - 31 * 60)
        self.scenario("done")
        r = self.sup().run()
        self.assertEqual((r.exit_code, r.outcome, r.runs), (2, "budget", 0))
        self.assertIn("time budget", r.detail)

    def test_budget_exit_runs_notify(self):
        doc_path = os.path.join(self.proj, ".jevflow", "flow.json")
        with open(doc_path) as fh:
            doc = json.load(fh)
        doc["notify"] = {"command": "printf '%s' \"$JEVFLOW_EVENT:$JEVFLOW_CONDITION\" > notified.txt"}
        with open(doc_path, "w") as fh:
            json.dump(doc, fh)
        self.seed_state(started_at=time.time() - 31 * 60)
        self.scenario("done")
        r = self.sup().run()
        self.assertEqual(r.outcome, "budget")
        with open(os.path.join(self.proj, "notified.txt")) as fh:
            self.assertEqual(fh.read(), "budget:supervisor_budget")
        self.assertEqual(self.state()["history"][-1]["notify"]["exit_code"], 0)

    def test_jev_budget_stops_before_launch(self):
        self.write_flow(max_jev_calls=5)
        self.seed_state(jev_calls=5)
        self.scenario("done")
        r = self.sup().run()
        self.assertEqual((r.exit_code, r.runs), (2, 0))

    def test_already_done(self):
        self.seed_state(done=True)
        self.scenario("not_done")
        r = self.sup().run()
        self.assertEqual((r.exit_code, r.runs), (0, 0))

    # ------------------------------------------------------------ config errors

    def test_max_restarts_zero_is_valid(self):
        self.write_flow(max_restarts=0)
        self.scenario("not_done")
        r = self.sup().run()
        self.assertEqual((r.exit_code, r.runs, r.restarts), (2, 1, 0))

    def test_config_errors(self):
        self.scenario("done")
        r = self.sup(claude_bin=os.path.join(self.tmp, "nope")).run()
        self.assertEqual((r.exit_code, r.outcome), (3, "config_error"))
        self.assertIn("not found", r.detail)
        r = self.sup(plugin_dir=self.tmp).run()
        self.assertEqual(r.exit_code, 3)
        r = self.sup(project=os.path.join(self.tmp, "missing")).run()
        self.assertEqual(r.exit_code, 3)
        with open(os.path.join(self.proj, ".jevflow", "flow.json"), "w") as fh:
            fh.write("{not json")
        r = self.sup().run()
        self.assertEqual(r.exit_code, 3)

    def test_parent_flow_does_not_capture_subdir(self):
        sub = os.path.join(self.proj, "sub")
        os.makedirs(sub)
        self.scenario("done")
        r = self.sup(project=sub).run()
        self.assertEqual(r.exit_code, 3)
        self.assertEqual(self.argvs(), [])

    # ------------------------------------------------------------ CLI

    def _cli(self, *args, env_extra=None, **kw):
        env = dict(os.environ, FAKE_DIR=self.fake_dir, JEVFLOW_CLAUDE_BIN=self.bin)
        env.update(env_extra or {})
        return subprocess.run([PY, "-m", "jevflow", "run", "--project", self.proj,
                               "--poll-seconds", "0.1", *args],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=60, **kw)

    def test_parse_args_equals_form(self):
        cfg = sv.parse_args(["--project=" + self.proj, "--claude-arg=--allowedTools",
                             "--claude-arg", "Bash(x:*)", "--max-turns=7"], {})
        self.assertEqual(cfg.extra_args, ["--allowedTools", "Bash(x:*)"])
        self.assertEqual(cfg.max_turns, 7)
        with self.assertRaises(ValueError):
            sv.parse_args(["--project", self.proj, "--json=1"], {})
        with self.assertRaises(ValueError):
            sv.parse_args(["--project", self.proj, "--claude-arg"], {})

    def test_cli_report_and_exit_codes(self):
        self.scenario("done")
        p = self._cli()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("jevflow run: goal_complete (exit 0)", p.stdout)
        os.remove(os.path.join(self.fake_dir, "count"))
        self.seed_state()
        self.scenario("needs_human")
        p = self._cli("--json")
        self.assertEqual(p.returncode, 4, p.stderr)
        self.assertEqual(json.loads(p.stdout)["outcome"], "needs_human")
        p = self._cli("--max-turns", "0")
        self.assertEqual(p.returncode, 3)
        p = subprocess.run([PY, "-m", "jevflow", "run", "-h"], cwd=ROOT,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0)
        self.assertIn("Exit codes", p.stdout)
        self.scenario("done")
        p = self._cli(env_extra={"JEV_API_KEY": "sk-test-SECRET-123"})
        self.assertIn("JEV_API_KEY is set", p.stderr)
        self.assertNotIn("SECRET", p.stderr + p.stdout)
        runs = os.listdir(os.path.join(self.proj, ".jevflow", "runs"))
        self.assertEqual(len(runs), 4)  # 2 launching invocations x (out, err), none overwritten

    def test_cli_sigterm_stops_child_group(self):
        self.scenario("hang")
        env = dict(os.environ, FAKE_DIR=self.fake_dir, JEVFLOW_CLAUDE_BIN=self.bin)
        p = subprocess.Popen([PY, "-m", "jevflow", "run", "--project", self.proj,
                              "--poll-seconds", "0.1"], cwd=ROOT, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        gp = os.path.join(self.fake_dir, "grandchild.pid")
        deadline = time.monotonic() + 20
        while not os.path.exists(gp) and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.2)
        with open(gp) as fh:
            gpid = int(fh.read())
        p.send_signal(signal.SIGTERM)
        out, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 130, err)
        deadline = time.monotonic() + 5
        while pid_alive(gpid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(pid_alive(gpid))


if __name__ == "__main__":
    unittest.main()
