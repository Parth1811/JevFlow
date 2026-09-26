"""E1: live Jev smoke test. Skips cleanly when no key is configured.

Run with a key: ``JEV_API_KEY=<key> python -m unittest tests.test_live_jev -v``.
Sends only the synthetic toy state below (no project files). Spends at most
2 Jev calls (judge + compete-then-verify).
"""

import math
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from jevflow import policy  # noqa: E402
from jevflow.flow import parse_flow  # noqa: E402
from jevflow.jev_client import JevClient, JevKeyError, resolve_key  # noqa: E402
from jevflow.judge import UNCLEAR, CheckResult, Judgment, judge  # noqa: E402
from jevflow.state import new_state  # noqa: E402

FLOW = {
    "schema_version": 1, "flow_version": "1",
    "goal": "Build a toy Python CLI todo app with add/list/done commands and tests",
    "phases": [
        {"id": "scaffold", "name": "Scaffold", "done_when": "todo/cli.py exists",
         "check": "test -f todo/cli.py"},
        {"id": "implement", "name": "Implement", "done_when": "add, list and done work",
         "check": None},
        {"id": "test", "name": "Tests pass", "done_when": "a test suite exists and passes",
         "check": "python -m pytest -q"},
    ],
}

# premature "done" claim: the message says everything is finished, the test check fails
LAST_MESSAGE = ("I have finished the todo app. All commands work and all tests pass. "
                "The project is complete!")
FAILING = "E   ModuleNotFoundError: No module named 'todo.store'\n1 error in 0.05s"


def _have_key():
    try:
        resolve_key()
        return True
    except JevKeyError:
        return False


@unittest.skipUnless(_have_key(), "no Jev key (set JEV_API_KEY)")
class LiveJevSmoke(unittest.TestCase):
    def test_judge_returns_well_formed_judgment(self):
        flow = parse_flow(FLOW)
        state = new_state(flow, now=0)
        state["phase_status"].update(scaffold="done", implement="done", test="active")
        state["current_phase"] = "test"
        checks = {"scaffold": CheckResult(True, ""), "implement": CheckResult(None),
                  "test": CheckResult(False, FAILING)}
        client = JevClient(max_calls=2)
        j = judge(client, flow, state, checks=checks, last_message=LAST_MESSAGE,
                  changes=[{"path": "todo/cli.py", "added": 42, "removed": 0}])

        self.assertIsInstance(j, Judgment)
        self.assertFalse(j.degraded, j.error)
        self.assertIn(j.current_phase, flow.ids + [UNCLEAR])
        self.assertIn(j.next_action, ["continue_phase", "advance_phase", "fix_regression",
                                      "ask_human", "goal_complete", UNCLEAR])
        for name in ("current_phase_conf", "next_action_conf", "stuck", "off_goal", "claims_done"):
            v = getattr(j, name)
            self.assertTrue(math.isfinite(v) and 0.0 <= v <= 1.0, (name, v))
        self.assertTrue(j.phase_done, "no phase_done__<id> answers")
        for pid, v in j.phase_done.items():
            self.assertIn(pid, flow.ids)
            self.assertTrue(0.0 <= v <= 1.0)
        if j.current_phase != UNCLEAR:
            self.assertEqual(j.verify_phase, j.current_phase)
            self.assertIsNotNone(j.verify)
            self.assertTrue(0.0 <= j.verify <= 1.0)
        self.assertTrue(1 <= j.calls <= 2, j.calls)

        # the whole point: a failing check with a "done" claim is never allowed through
        d = policy.decide(flow, state, j, checks, now=1.0)
        self.assertTrue(d.blocks, (d.kind, d.condition))
        self.assertNotEqual(d.kind, policy.ALLOW_STOP)
        sys.stderr.write(f"\n  live judgment: {j.probs()} -> {d.kind} {d.condition}\n")


if __name__ == "__main__":
    unittest.main()
