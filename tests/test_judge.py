import json
import unittest

from jevflow import judge as jd
from jevflow.flow import parse_flow
from jevflow.jev_client import JevBudgetError, JevHTTPError
from jevflow.state import new_state

FLOW = {
    "goal": "Build a CLI todo app in Python with add/list/done commands and tests",
    "phases": [
        {"id": "scaffold", "name": "Project scaffold", "done_when": "package layout exists",
         "check": "test -f todo/cli.py"},
        {"id": "implement", "name": "Implement commands", "done_when": "add, list and done work"},
        {"id": "test", "name": "Tests pass", "done_when": "a test suite exists and passes",
         "check": "pytest -q"},
    ],
}
SECRET = "def secret_function(): return 'PRIVATE_FILE_CONTENT'"


def answers(phase="implement", conf=0.9, stuck=0.1, off=0.05, claims=0.1, done=0.2):
    return {
        "current_phase": {"type": "choice", "choice": phase, "confidence": conf,
                          "probabilities": {phase: conf, "unclear": 1 - conf}},
        "next_action": {"type": "choice", "choice": "continue_phase", "confidence": 0.7,
                        "probabilities": {"continue_phase": 0.8, "advance_phase": 0.2}},
        "stuck": {"type": "noul", "noul": stuck},
        "off_goal": {"type": "noul", "noul": off},
        "claims_done": {"type": "noul", "noul": claims},
        "progress": {"type": "score", "score": 2.0, "confidence": 0.6},
        "phase_done__implement": {"type": "noul", "noul": done},
        "phase_done__test": {"type": "noul", "noul": 0.01},
    }


class FakeClient:
    def __init__(self, *script):
        self.script = list(script)
        self.calls = []
        self.calls_made = 0

    def ask(self, state, questions):
        self.calls_made += 1
        self.calls.append((state, questions))
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class TestBuildState(unittest.TestCase):
    def setUp(self):
        self.flow = parse_flow(FLOW)
        self.state = new_state(self.flow, now=0)
        self.state["current_phase"] = "implement"
        self.checks = {"scaffold": jd.CheckResult(True, "ok"),
                       "test": jd.CheckResult(False, "E   AssertionError\n2 failed, 3 passed")}

    def test_shape(self):
        doc = json.loads(jd.build_state(self.flow, self.state, checks=self.checks,
                                        last_message="added add command"))
        self.assertEqual(set(doc), {"goal", "phases", "current_phase", "check_results",
                                    "last_assistant_message", "change_summary", "recent_history"})
        self.assertEqual([p["id"] for p in doc["phases"]], ["scaffold", "implement", "test"])
        self.assertEqual(doc["check_results"]["scaffold"], "pass")
        self.assertTrue(doc["check_results"]["test"].startswith("fail: "))
        self.assertIn("2 failed", doc["check_results"]["test"])

    def test_last_message_truncated_to_4k_tail(self):
        msg = "x" * 10000 + "THE_END"
        doc = json.loads(jd.build_state(self.flow, self.state, checks={}, last_message=msg))
        self.assertLessEqual(len(doc["last_assistant_message"]), jd.LAST_MESSAGE_CHARS)
        self.assertTrue(doc["last_assistant_message"].endswith("THE_END"))

    def test_history_last_5_decisions(self):
        for i in range(9):
            self.state["history"].append({"event": "stop", "decision": "BLOCK", "phase": "implement",
                                          "reason": f"r{i}"})
        self.state["history"].append({"event": "session_start"})  # not a decision
        doc = json.loads(jd.build_state(self.flow, self.state, checks={}))
        self.assertEqual(len(doc["recent_history"]), 5)
        self.assertIn("r8", doc["recent_history"][-1])

    def test_budget_trimming(self):
        for i in range(20):
            self.state["history"].append({"decision": "BLOCK", "phase": "x", "reason": "y" * 300})
        big = {"test": jd.CheckResult(False, "z" * 5000)}
        changes = [{"path": f"f{i}.py", "added": i, "removed": 0} for i in range(200)]
        for budget in (12000, 6000, 3000, 1500):
            out = jd.build_state(self.flow, self.state, checks=big, last_message="m" * 9000,
                                 changes=changes, budget=budget)
            self.assertLessEqual(len(out), budget, budget)
            doc = json.loads(out)  # always valid JSON
            self.assertEqual(doc["goal"], FLOW["goal"])  # goal never trimmed away
            self.assertEqual(len(doc["phases"]), 3)

    def test_default_budget_from_flow_limits(self):
        f = parse_flow(dict(FLOW, limits={"state_char_budget": 2000}))
        out = jd.build_state(f, new_state(f, now=0), checks={}, last_message="m" * 9000)
        self.assertLessEqual(len(out), 2000)

    def test_privacy_no_file_contents_by_default(self):
        changes = [{"path": "todo/cli.py", "added": 40, "removed": 2, "diff": SECRET}]
        out = jd.build_state(self.flow, self.state, checks={}, changes=changes)
        self.assertNotIn("PRIVATE_FILE_CONTENT", out)
        self.assertIn("todo/cli.py +40 -2", out)

    def test_privacy_send_diff_true_includes_diff(self):
        f = parse_flow(dict(FLOW, privacy={"send_diff": True}))
        changes = [{"path": "todo/cli.py", "added": 40, "removed": 2, "diff": SECRET}]
        out = jd.build_state(f, new_state(f, now=0), checks={}, changes=changes)
        self.assertIn("PRIVATE_FILE_CONTENT", out)

    def test_change_list_capped(self):
        changes = [{"path": f"f{i}.py", "added": 1, "removed": 0} for i in range(60)]
        s = jd.summarize_changes(changes, False)
        self.assertEqual(len(s), jd.MAX_CHANGE_FILES + 1)
        self.assertIn("20 more", s[-1])


class TestQuestions(unittest.TestCase):
    def setUp(self):
        self.flow = parse_flow(FLOW)

    def test_request_shape(self):
        q = jd.build_questions(self.flow, "implement")
        self.assertEqual(set(q), {"current_phase", "next_action", "stuck", "off_goal", "claims_done",
                                  "progress", "phase_done__implement", "phase_done__test"})
        self.assertEqual(q["current_phase"]["type"], "choice")
        self.assertIn("unclear", q["current_phase"]["criteria"])
        self.assertIn("unclear", q["next_action"]["criteria"])
        self.assertEqual(set(q["next_action"]["criteria"]),
                         {"continue_phase", "advance_phase", "fix_regression", "ask_human",
                          "goal_complete", "unclear"})
        self.assertEqual(q["progress"]["criteria"], jd.PROGRESS_LEVELS)
        for name in ("stuck", "off_goal", "claims_done"):
            self.assertEqual(q[name]["type"], "noul")

    def test_last_phase_has_no_next(self):
        q = jd.build_questions(self.flow, "test")
        self.assertIn("phase_done__test", q)
        self.assertEqual([k for k in q if k.startswith("phase_done__")], ["phase_done__test"])

    def test_no_attribution_questions(self):
        text = json.dumps(jd.build_questions(self.flow, "implement")).lower()
        for banned in ("why", "which step caused", "root cause"):
            self.assertNotIn(banned, text)


class TestJudge(unittest.TestCase):
    def setUp(self):
        self.flow = parse_flow(FLOW)
        self.state = new_state(self.flow, now=0)
        self.state["current_phase"] = "implement"

    def run_judge(self, client):
        return jd.judge(client, self.flow, self.state, checks={}, last_message="hi")

    def test_compete_then_verify(self):
        c = FakeClient(answers(done=0.85), {"verify": {"type": "noul", "noul": 0.83}})
        j = self.run_judge(c)
        self.assertFalse(j.degraded)
        self.assertEqual(j.current_phase, "implement")
        self.assertAlmostEqual(j.current_phase_conf, 0.9)
        self.assertEqual(j.verify_phase, "implement")
        self.assertAlmostEqual(j.verify, 0.83)
        self.assertAlmostEqual(j.phase_done["implement"], 0.85)
        self.assertAlmostEqual(j.progress, 0.5)
        self.assertEqual(j.calls, 2)
        self.assertEqual(list(c.calls[1][1]), ["verify"])
        self.assertEqual(c.calls[0][0], c.calls[1][0])  # same state for both calls

    def test_unclear_skips_verify(self):
        c = FakeClient(answers(phase="unclear", conf=0.6))
        j = self.run_judge(c)
        self.assertEqual(j.current_phase, "unclear")
        self.assertIsNone(j.verify)
        self.assertEqual(j.calls, 1)

    def test_jev_error_degrades(self):
        j = self.run_judge(FakeClient(JevHTTPError(503, "down")))
        self.assertTrue(j.degraded)
        self.assertIn("JevHTTPError", j.error)

    def test_budget_error_degrades(self):
        self.assertTrue(self.run_judge(FakeClient(JevBudgetError("max"))).degraded)

    def test_no_client_degrades(self):
        self.assertTrue(self.run_judge(None).degraded)

    def test_malformed_answers_degrade(self):
        bad = answers()
        del bad["stuck"]
        self.assertTrue(self.run_judge(FakeClient(bad)).degraded)

    def test_verify_failure_keeps_first_call_without_verify(self):
        c = FakeClient(answers(), JevHTTPError(500, "x"))
        j = self.run_judge(c)
        self.assertFalse(j.degraded)
        self.assertIsNone(j.verify)
        self.assertIn("verify failed", j.error)

    def test_non_finite_numbers_degrade(self):
        # critic: NaN would compare False against every band and slip through
        for bad in (float("nan"), float("inf")):
            a = answers()
            a["stuck"]["noul"] = bad
            self.assertTrue(self.run_judge(FakeClient(a)).degraded)
        a = answers()
        a["progress"]["score"] = float("nan")
        self.assertTrue(self.run_judge(FakeClient(a)).degraded)

    def test_unknown_choice_falls_back_to_argmax(self):
        a = answers()
        a["current_phase"] = {"type": "choice", "choice": "bogus",
                              "probabilities": {"test": 0.7, "implement": 0.2, "bogus": 0.1}}
        c = FakeClient(a, {"verify": {"type": "noul", "noul": 0.1}})
        j = self.run_judge(c)
        self.assertEqual(j.current_phase, "test")
        self.assertAlmostEqual(j.current_phase_conf, 0.7)

    def test_probs_are_compact_numbers(self):
        c = FakeClient(answers(), {"verify": {"type": "noul", "noul": 0.5}})
        p = self.run_judge(c).probs()
        self.assertEqual(p["current_phase"], ["implement", 0.9])
        self.assertEqual(p["verify"], ["implement", 0.5])


if __name__ == "__main__":
    unittest.main()
