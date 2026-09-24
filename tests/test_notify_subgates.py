"""F5: notify hook, SubagentStop and TaskCompleted gates (SPEC 10.4, 10.5)."""

import io
import json
import os
import subprocess
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from jevflow import hooks, notify, subgates  # noqa: E402
from jevflow.flow import FlowError, parse_flow  # noqa: E402
from jevflow.jev_client import JevConnectionError  # noqa: E402

from tests.test_hooks import LAUNCHER, NOW, FakeClient, HookCase, fixture, flow_doc  # noqa: E402

B = {"auto": 0.80, "flag": 0.70}


def sub_answers(complete, claims):
    return {"complete": {"noul": complete}, "claims_done": {"noul": claims}}


class TestNotifyParse(unittest.TestCase):
    def test_valid_and_defaults(self):
        f = parse_flow(flow_doc(notify={"command": "true"}))
        self.assertEqual(f.notify, {"command": "true", "on": list(notify.EVENTS), "timeout_s": 10})
        self.assertEqual(parse_flow(flow_doc()).notify, {})
        self.assertEqual(parse_flow(flow_doc(notify={})).notify, {})

    def test_invalid(self):
        for bad in ([], {"command": ""}, {"command": "x", "on": ["done"]}, {"command": "x", "on": []},
                    {"command": "x", "timeout_s": 0}, {"command": "x", "timeout_s": 31},
                    {"command": "x", "timeout_s": True}, {"command": "x", "url": "y"},
                    {"on": ["budget"]}):
            with self.subTest(bad=bad):
                with self.assertRaises(FlowError):
                    parse_flow(flow_doc(notify=bad))

    def test_event_map(self):
        rows = [("goal_complete", False, "goal_complete"), ("budget_blocks", False, "budget"),
                ("budget_time", False, "budget"), ("budget_jev", False, "budget"),
                ("hook_cap", False, "budget"), ("advance", False, None), ("continue", False, None),
                ("stuck_ask_human", True, "ask_human"), ("ask_human", True, "ask_human")]
        for cond, asked, want in rows:
            with self.subTest(cond=cond):
                self.assertEqual(notify.event_for(cond, asked_human=asked), want)


class TestNotifyRun(HookCase):
    def out_file(self):
        return os.path.join(self.dir, "notified.jsonl")

    def flow_with(self, cmd, **kw):
        return parse_flow(flow_doc(notify=dict({"command": cmd}, **kw)))

    def test_runs_once_with_payload_and_env_without_key(self):
        cmd = ('{ cat; echo; printf "%s|%s|%s|%s\\n" "$JEVFLOW_EVENT" "$JEVFLOW_PHASE" '
               '"${JEV_API_KEY:-nokey}" "${JEVFLOW_KEY_FILE:-nofile}"; } >> notified.jsonl')
        flow = self.flow_with(cmd)
        state = {"current_phase": "a"}
        os.environ["JEV_API_KEY"], os.environ["JEVFLOW_KEY_FILE"] = "sk-test-SECRET", "/x/key"
        try:
            r = notify.notify(flow, state, self.dir, "budget", condition="budget_blocks",
                              message="Stopping: block budget", now=NOW)
            again = notify.notify(flow, state, self.dir, "budget", condition="budget_blocks",
                                  message="Stopping: block budget", now=NOW + 1)
        finally:
            del os.environ["JEV_API_KEY"], os.environ["JEVFLOW_KEY_FILE"]
        self.assertEqual(r["exit_code"], 0)
        self.assertIsNone(again)
        with open(self.out_file(), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        payload = json.loads(lines[0])
        self.assertEqual((payload["event"], payload["phase"]), ("budget", "a"))
        self.assertEqual(lines[1], "budget|a|nokey|nofile")

    def test_filtered_event_does_not_run(self):
        flow = self.flow_with("touch notified.jsonl", on=["goal_complete"])
        self.assertIsNone(notify.notify(flow, {}, self.dir, "budget", condition="budget_time",
                                        message="", now=NOW))
        self.assertFalse(os.path.exists(self.out_file()))

    def test_failure_and_timeout_are_contained(self):
        r = notify.notify(self.flow_with("echo boom >&2; exit 3"), {}, self.dir, "budget",
                          condition="c", message="", now=NOW)
        self.assertEqual(r["exit_code"], 3)
        self.assertIn("boom", r["stderr"])
        t0 = time.monotonic()
        # the backgrounded grandchild keeps stderr open; must not hang past the timeout
        r = notify.notify(self.flow_with("sleep 30 & sleep 30", timeout_s=1), {}, self.dir,
                          "budget", condition="c2", message="", now=NOW)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertIn("timeout", r["error"])

    def test_stop_hook_notifies_on_goal_complete_and_budget(self):
        self.write_flow(flow_doc(notify={"command": "cat >> notified.jsonl; echo >> notified.jsonl"}))
        self.touch("a.txt")
        self.touch("b.txt")
        self.run_hook("Stop", fixture("Stop_first.json"), client=None)   # a -> b
        out = self.run_hook("Stop", fixture("Stop_first.json"), client=None)
        self.assertEqual(out, {"systemMessage": "[jevflow] Goal complete."})
        with open(self.out_file(), encoding="utf-8") as fh:
            events = [json.loads(x)["event"] for x in fh.read().split("\n") if x.strip()]
        self.assertEqual(events, ["goal_complete"])
        st = self.state()
        self.assertEqual(st["history"][-1]["notify"]["exit_code"], 0)

    def test_stop_hook_budget_notifies_once(self):
        self.write_flow(flow_doc(notify={"command": "echo x >> notified.jsonl"},
                                 limits={"max_blocks_per_session": 1}))
        st = self.state()
        st["blocks_this_session"] = 1
        from jevflow.state import save_state
        save_state(self.paths.state, st)
        for _ in range(3):
            self.run_hook("Stop", fixture("Stop_first.json"), client=None)
        with open(self.out_file(), encoding="utf-8") as fh:
            self.assertEqual(fh.read().count("x"), 1)


class TestSubgateCompose(unittest.TestCase):
    def test_table(self):
        rows = [
            (0.05, 0.95, "block"),   # claims done, clearly not done
            (0.20, 0.70, "block"),   # both exactly at the bands
            (0.21, 0.95, "allow"),   # not confident enough that it is incomplete
            (0.05, 0.69, "allow"),   # does not claim done (honest partial report)
            (0.95, 0.95, "allow"),
            (0.50, 0.50, "allow"),
        ]
        for complete, claims, want in rows:
            with self.subTest(complete=complete, claims=claims):
                self.assertEqual(subgates.compose({"complete": complete, "claims_done": claims},
                                                  B).verdict, want)

    def test_judge_request_and_failures(self):
        seen = {}

        class C(FakeClient):
            def ask(self, doc, questions):
                seen["doc"], seen["q"] = json.loads(doc), questions
                return super().ask(doc, questions)

        r = subgates.judge_subtask(C(sub_answers(0.05, 0.9)), task="Write the parser\n\n  now",
                                   goal="G", last_message="Done! all good", b=B)
        self.assertEqual((r.verdict, r.calls), ("block", 1))
        self.assertEqual(set(seen["q"]), {"complete", "claims_done"})
        self.assertEqual(seen["doc"]["subtask"], "Write the parser now")
        for client in (None, FakeClient(JevConnectionError("down")), FakeClient({"complete": {}}),
                       FakeClient(sub_answers(float("nan"), 0.9))):
            with self.subTest(client=client):
                r = subgates.judge_subtask(client, task="t", goal="G", last_message="m", b=B)
                self.assertEqual(r.verdict, "allow")
                self.assertTrue(r.degraded)
        self.assertEqual(subgates.judge_subtask(FakeClient(), task="", goal="G", last_message="m",
                                                b=B).condition, "no_evidence")

    def test_transcript_readers(self):
        d = os.path.realpath(os.path.join(ROOT, "loop", "scratch"))
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "f5_transcript.jsonl")
        lines = [
            {"type": "user", "message": {"role": "user", "content": "Find the TODOs in src/"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "first answer"}]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "content": "x"}]}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Grep"}, {"type": "text", "text": "final answer"}]}},
        ]
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("not json\n")
                for x in lines:
                    fh.write(json.dumps(x) + "\n")
                fh.write('{"torn": ')
            self.assertEqual(subgates.first_prompt(path), "Find the TODOs in src/")
            self.assertEqual(subgates.last_assistant_text(path), "final answer")
        finally:
            os.remove(path)
        self.assertEqual(subgates.first_prompt(path), "")
        self.assertEqual(subgates.last_assistant_text(None), "")


class TestSubgateHooks(HookCase):
    def transcript(self, prompt="Summarize module a", reply="All done."):
        p = os.path.join(self.dir, "agent.jsonl")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n")
            fh.write(json.dumps({"type": "assistant", "message": {
                "role": "assistant", "content": [{"type": "text", "text": reply}]}}) + "\n")
        return p

    def sub_payload(self, **kw):
        d = {"hook_event_name": "SubagentStop", "session_id": "s1", "stop_hook_active": False,
             "agent_id": "ag1", "agent_type": "Explore",
             "agent_transcript_path": self.transcript(), "last_assistant_message": "All done."}
        d.update(kw)
        return d

    def task_payload(self, **kw):
        d = {"hook_event_name": "TaskCompleted", "session_id": "s1", "task_id": "t1",
             "task_subject": "Write README", "task_description": "Document usage",
             "transcript_path": self.transcript(reply="README written, all done.")}
        d.update(kw)
        return d

    def enable(self, mode="enforce", **g):
        self.write_flow(flow_doc(mode=mode, gates=dict({"subagent_stop": True,
                                                         "task_completed": True}, **g)))

    def test_off_by_default(self):
        out = self.run_hook("SubagentStop", self.sub_payload(),
                            client=FakeClient(sub_answers(0.0, 1.0)))
        self.assertEqual(out, {})
        self.assertEqual(self.factory_calls, [])

    def test_subagent_block_then_cap(self):
        self.enable()
        for i in range(subgates.SUBTASK_BLOCK_CAP):
            out = self.run_hook("SubagentStop", self.sub_payload(stop_hook_active=i > 0),
                                client=FakeClient(sub_answers(0.05, 0.95)))
            self.assertEqual(out["decision"], "block", i)
        out = self.run_hook("SubagentStop", self.sub_payload(stop_hook_active=True),
                            client=FakeClient(sub_answers(0.05, 0.95)))
        self.assertEqual(out, {})
        st = self.state()
        ev = [h for h in st["history"] if h["event"] == "subagent_stop"]
        self.assertEqual([h["condition"] for h in ev],
                         ["subtask_premature"] * subgates.SUBTASK_BLOCK_CAP + ["subtask_cap"])
        self.assertEqual(st["jev_calls"], subgates.SUBTASK_BLOCK_CAP)
        # phase state is untouched by subtask gates
        self.assertEqual(st["current_phase"], "a")
        self.assertNotIn("decision", ev[0])

    def test_subagent_allow_and_internal_agents(self):
        self.enable()
        self.assertEqual(self.run_hook("SubagentStop", self.sub_payload(),
                                       client=FakeClient(sub_answers(0.9, 0.9))), {})
        self.assertEqual(self.run_hook("SubagentStop", self.sub_payload(agent_type=""),
                                       client=FakeClient(sub_answers(0.0, 1.0))), {})

    def test_subagent_modes(self):
        self.enable(mode="warn")
        out = self.run_hook("SubagentStop", self.sub_payload(), client=FakeClient(sub_answers(0.0, 1.0)))
        self.assertIn("[jevflow warn]", out["systemMessage"])
        self.enable(mode="observe")
        out = self.run_hook("SubagentStop", self.sub_payload(agent_id="ag2"),
                            client=FakeClient(sub_answers(0.0, 1.0)))
        self.assertEqual(out, {})
        self.assertFalse(self.state().get("subtask_holds"))

    def test_task_completed_blocks_via_exit_code(self):
        self.enable()
        stdin = io.StringIO(json.dumps(dict(self.task_payload(), cwd=self.dir)))
        stdout, stderr = io.StringIO(), io.StringIO()
        old = sys.stderr
        sys.stderr = stderr
        try:
            rc = hooks.main(["TaskCompleted"], stdin, stdout, env=self.env, now=NOW,
                            client_factory=lambda n: FakeClient(sub_answers(0.05, 0.9)))
        finally:
            sys.stderr = old
        self.assertEqual(rc, hooks.BLOCK_EXIT)
        self.assertEqual(json.loads(stdout.getvalue()), {})
        self.assertIn("[jevflow]", stderr.getvalue())
        ok = self.run_hook("TaskCompleted", self.task_payload(task_id="t2"),
                           client=FakeClient(sub_answers(0.9, 0.9)))
        self.assertEqual(ok, {})

    def test_task_completed_without_transcript_allows(self):
        self.enable()
        out = self.run_hook("TaskCompleted", self.task_payload(transcript_path="/nonexistent"),
                            client=FakeClient(sub_answers(0.0, 1.0)))
        self.assertEqual(out, {})
        self.assertEqual(self.state()["history"][-1]["condition"], "no_evidence")

    def test_stop_keeps_a_concurrent_gate_write(self):
        """A background subagent's SubagentStop lands while the main Stop is
        waiting on Jev; the Stop's save must not drop it."""
        self.enable()
        case = self
        from tests.test_hooks import answers

        class Racing(FakeClient):
            def ask(self, doc, questions):
                if self.calls_made == 0:
                    hooks.handle("SubagentStop", dict(case.sub_payload(), cwd=case.dir),
                                 env=case.env, now=NOW,
                                 client_factory=lambda n: FakeClient(sub_answers(0.05, 0.95)))
                return super().ask(doc, questions)

        self.run_hook("Stop", fixture("Stop_first.json"),
                      client=Racing(answers("a", done=0.1), {"verify": {"noul": 0.1}}))
        st = self.state()
        events = [h["event"] for h in st["history"]]
        self.assertIn("subagent_stop", events)
        self.assertEqual(events[-1], "stop")
        self.assertEqual(st["subtask_holds"], {"agent:ag1": 1})
        self.assertEqual(st["jev_calls"], 3)  # 2 for the Stop + 1 for the gate

    def test_gate_flags_validated(self):
        with self.assertRaises(FlowError):
            parse_flow(flow_doc(gates={"subagent_stop": "yes"}))


class TestLauncherExitCodes(HookCase):
    def fake_python(self, code):
        p = os.path.join(self.dir, f"fakepy{code}")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write('#!/bin/sh\nif [ "$1" = "-c" ]; then echo jevflow-ok; exit 0; fi\n'
                     f'echo "{{}}"; exit {code}\n')
        os.chmod(p, 0o755)
        return p

    def test_only_42_becomes_block(self):
        for code, want in ((42, 2), (2, 0), (1, 0), (0, 0)):
            with self.subTest(code=code):
                p = subprocess.run([LAUNCHER, "hook", "TaskCompleted"], input=b"{}",
                                   capture_output=True, timeout=30,
                                   env=dict(os.environ, JEVFLOW_PYTHON=self.fake_python(code)))
                self.assertEqual(p.returncode, want)


if __name__ == "__main__":
    unittest.main()
