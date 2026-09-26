"""Multi-flow and multi-agent: titles, start --name, join/claim, agent
tracking, the phase-transition status line, and the viewer's flow list."""

import io
import json
import os
import shutil
import tempfile
import time
import unittest
from contextlib import redirect_stdout

from jevflow import __main__ as cli
from jevflow import agents, ui
from jevflow.flow import FlowError, parse_flow
from jevflow.project import Paths, archive, new_flow
from jevflow.state import new_state, save_state

from tests.test_hooks import FakeClient, HookCase, answers, fixture

DOC = {"schema_version": 1, "title": "Toy files", "goal": "make a.txt then b.txt", "phases": [
    {"id": "a", "name": "A", "done_when": "a.txt", "check": "test -f a.txt"},
    {"id": "b", "name": "B", "done_when": "b.txt", "check": "test -f b.txt"}]}


class TestTitleAndName(unittest.TestCase):
    def test_title_parsed_and_bounded(self):
        self.assertEqual(parse_flow(DOC).title, "Toy files")
        self.assertEqual(parse_flow({k: v for k, v in DOC.items() if k != "title"}).title, "")
        with self.assertRaises(FlowError):
            parse_flow(dict(DOC, title="x" * 81))
        with self.assertRaises(FlowError):
            parse_flow(dict(DOC, title=3))

    def test_start_name_sets_the_slug(self):
        d = tempfile.mkdtemp(prefix="jevflow-multi-")
        try:
            p = new_flow(Paths(d), "Build a small Python package that converts temperatures", 0.0,
                         name="temp-converter-cli")
            self.assertTrue(p.flow_id.endswith("-temp-converter-cli"), p.flow_id)
            q = new_flow(Paths(d), "Build a small Python package that converts temperatures", 0.0)
            self.assertIn("build-small-python-package", q.flow_id)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestAgents(unittest.TestCase):
    def test_touch_claim_and_by_phase(self):
        st = {"current_phase": "a"}
        self.assertTrue(agents.touch(st, {"session_id": "s1abcdef"}, 1.0, event="tool", tool="Edit"))
        self.assertFalse(agents.touch(st, {"session_id": "s1abcdef"}, 2.0, event="tool", tool="Edit"))
        sub = {"session_id": "s1abcdef", "agent_id": "ag42", "agent_type": "docs-writer"}
        self.assertTrue(agents.touch(st, sub, 3.0, event="tool", tool="Write"))
        self.assertTrue(agents.claim_from_output(st, sub, "JEVFLOW_CLAIM phase=b name=Docs writer",
                                                 ["a", "b"], 4.0))
        self.assertFalse(agents.claim_from_output(st, sub, "JEVFLOW_CLAIM phase=zzz", ["a", "b"], 4.0))
        self.assertEqual(agents.by_phase(st), {"a": ["claude s1abcd"], "b": ["Docs writer"]})
        a = st["agents"]["agent:ag42"]
        self.assertEqual((a["kind"], a["type"], a["tools"]), ("subagent", "docs-writer", 1))
        # the claim holds while the flow moves, and ends with the agent's turn
        st["current_phase"] = "c"
        agents.touch(st, sub, 5.0, event="tool")
        self.assertEqual(st["agents"]["agent:ag42"]["phase"], "b")
        agents.touch(st, sub, 6.0, event="stop")
        agents.touch(st, sub, 7.0, event="tool")
        self.assertEqual(st["agents"]["agent:ag42"]["phase"], "c")

    def test_cap(self):
        st = {"current_phase": "a"}
        for i in range(agents.MAX_AGENTS + 5):
            agents.touch(st, {"session_id": f"s{i}"}, float(i), event="tool")
        self.assertEqual(len(st["agents"]), agents.MAX_AGENTS)
        self.assertNotIn("session:s0", st["agents"])


class TestCli(HookCase):
    def run_cli(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(list(args))
        return rc, buf.getvalue()

    def test_claim_prints_marker(self):
        rc, out = self.run_cli("claim", "b", "--as", "reviewer <x>")
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "JEVFLOW_CLAIM phase=b name=reviewer x")
        self.assertEqual(self.run_cli("claim", "Bad!")[0], 3)

    def test_join_binds_second_session_and_claim_is_tracked(self):
        p = new_flow(Paths(self.dir), "second flow", time.time(), name="second")
        with open(p.flow, "w", encoding="utf-8") as fh:
            json.dump(DOC, fh)
        os.remove(p.draft)
        rc, out = self.run_cli("join", p.flow_id, "--project", self.dir)
        self.assertEqual(rc, 0)
        self.assertIn(f"JEVFLOW_STARTED flow={p.flow_id}", out)
        self.assertEqual(self.run_cli("join", "nope", "--project", self.dir)[0], 3)
        sid = "second-session-0001"
        self.run_hook("PostToolUse", {"session_id": sid, "hook_event_name": "PostToolUse",
                                      "tool_name": "Bash", "tool_input": {"command": "jevflow join"},
                                      "tool_response": {"stdout": out}})
        self.run_hook("PostToolUse", {"session_id": sid, "hook_event_name": "PostToolUse",
                                      "tool_name": "Bash", "tool_input": {"command": "jevflow claim b"},
                                      "tool_response": {"stdout": "JEVFLOW_CLAIM phase=b name=reviewer\n"}})
        with open(p.state, encoding="utf-8") as fh:
            st = json.load(fh)
        a = st["agents"]["session:" + sid]
        self.assertEqual((a["label"], a["phase"]), ("reviewer", "b"))


class TestTransitionLine(HookCase):
    def test_advance_tells_the_user(self):
        with open(self.paths.flow, encoding="utf-8") as fh:
            doc = dict(json.load(fh), title="Toy files")
        self.write_flow(doc)
        self.touch("a.txt")
        client = FakeClient(answers("a"), {"verify": {"noul": 0.9}})
        out = self.run_hook("Stop", fixture("Stop_first.json"), client=client)
        self.assertEqual(out["decision"], "block")
        self.assertEqual(out["systemMessage"],
                         "[jevflow] Toy files: ✓ a → b (1/2 done) · Phase 'a' is complete.")
        self.assertEqual(self.state()["history"][-1]["agent"],
                         "claude " + fixture("Stop_first.json")["session_id"][:6])


class TestFlowList(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="jevflow-flows-"))
        self.root = Paths(self.dir)
        self.a = self.make("20260926-000100-alpha", dict(DOC, title="Alpha"), lambda s: None)
        self.b = self.make("20260926-000200-beta", dict(DOC, title="Beta"),
                           lambda s: s["phase_status"].update(a="done", b="done") or s.update(done=True))
        archive(self.b, 5.0)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def make(self, fid, doc, setup):
        p = Paths(self.dir, fid)
        os.makedirs(p.dir)
        with open(p.flow, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        s = new_state(parse_flow(doc), now=time.time())
        s["agents"] = {"session:x": {"label": "claude x", "phase": "a", "at": time.time()}}
        setup(s)
        save_state(p.state, s)
        return p

    def test_rows(self):
        rows = ui.all_flows(self.root)
        self.assertEqual([(r["title"], r["archived"]) for r in rows], [("Alpha", False), ("Beta", True)])
        alpha, beta = rows
        self.assertEqual((alpha["phases"], alpha["phases_done"], alpha["agents_active"]), (2, 0, 1))
        self.assertTrue(beta["done"])
        self.assertEqual(beta["outcome"], "complete")

    def test_export_bundle_has_every_flow(self):
        data = ui.export_bundle(self.root, self.a)
        self.assertEqual(data["flow_id"], self.a.flow_id)
        self.assertEqual(len(data["flows"]), 2)
        self.assertEqual(list(data["snapshots"]), ["20260926-000200-beta"])
        self.assertIn("Flow: Alpha", data["text"])

    def test_target_for_query(self):
        t = ui.Target(self.root)
        picked = ui._target_for(t, "/api/state?flow=20260926-000200-beta")
        self.assertEqual(picked.paths().flow_id, "20260926-000200-beta")
        self.assertTrue(picked.paths().archived)
        self.assertIs(ui._target_for(t, "/api/state?flow=../../etc"), t)


if __name__ == "__main__":
    unittest.main()
