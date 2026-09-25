"""Table-driven policy tests: one case per condition (SPEC 4, 10.1-10.5)."""

import unittest

from jevflow import policy as pol
from jevflow.flow import parse_flow
from jevflow.judge import CheckResult, Judgment
from jevflow.state import new_state

GOAL = "Build a CLI todo app"
LINEAR = {
    "goal": GOAL,
    "phases": [
        {"id": "scaffold", "name": "Scaffold", "done_when": "layout exists", "check": "test -f x"},
        {"id": "implement", "name": "Implement", "done_when": "commands work"},
        {"id": "test", "name": "Tests", "done_when": "suite passes", "check": "pytest -q"},
    ],
}
LOOPED = {
    "goal": GOAL,
    "phases": [
        {"id": "implement", "name": "Implement", "done_when": "commands work"},
        {"id": "test", "name": "Tests", "done_when": "suite passes", "check": "pytest -q",
         "loop": {"max_iterations": 3, "until": "pytest -q"}},
    ],
}
BRANCH = {
    "goal": GOAL,
    "phases": [
        {"id": "implement", "name": "Implement", "done_when": "commands work"},
        {"id": "test", "name": "Tests", "done_when": "suite passes", "check": "pytest -q",
         "on_fail": "debug"},
        {"id": "debug", "name": "Debug", "done_when": "root cause fixed", "depends_on": []},
    ],
}
LOOP_BRANCH = {
    "goal": GOAL,
    "phases": [
        {"id": "test", "name": "Tests", "done_when": "suite passes", "check": "pytest -q",
         "loop": {"max_iterations": 2, "until": "pytest -q"}, "on_fail": "debug"},
        {"id": "debug", "name": "Debug", "done_when": "root cause fixed", "depends_on": []},
    ],
}
DAG = {
    "goal": GOAL,
    "phases": [
        {"id": "api", "name": "API", "done_when": "api done", "depends_on": []},
        {"id": "cli", "name": "CLI", "done_when": "cli done", "depends_on": ["api"]},
        {"id": "docs", "name": "Docs", "done_when": "docs done", "depends_on": []},
        {"id": "ship", "name": "Ship", "done_when": "shipped", "depends_on": ["cli", "docs"]},
    ],
}

PASS = CheckResult(True, "ok")
FAIL = CheckResult(False, "2 failed, 3 passed\nAssertionError: expected 3")


def J(phase, conf=0.95, verify=None, **kw):
    """Confident judgment for ``phase``; verify defaults to 'not done'."""
    j = Judgment(current_phase=phase, current_phase_conf=conf, next_action="continue_phase",
                 next_action_conf=0.9, verify_phase=None if phase == "unclear" else phase,
                 verify=0.05 if verify is None else verify, stuck=0.05, off_goal=0.02,
                 claims_done=0.05, calls=2)
    for k, v in kw.items():
        setattr(j, k, v)
    return j


def S(flow, cur, done=(), **kw):
    s = new_state(flow, now=1000.0)
    for pid in s["phase_status"]:
        s["phase_status"][pid] = "pending"
    for pid in done:
        s["phase_status"][pid] = "done"
    s["phase_status"][cur] = "active"
    s["current_phase"] = cur
    s.update(kw)
    return s


# (name, flow, state kwargs, judgment, checks, decide kwargs, expected kind, condition, to_phase)
CASES = [
    ("already_done", LINEAR, dict(cur="test", done=("scaffold", "implement"), done_flag=True),
     J("test"), {}, {}, pol.ALLOW_STOP, "already_done", None),
    ("budget_blocks", LINEAR, dict(cur="implement", done=("scaffold",), blocks_this_session=6),
     J("implement"), {}, {}, pol.ALLOW_STOP, "budget_blocks", None),
    ("budget_time", LINEAR, dict(cur="implement", done=("scaffold",)),
     J("implement"), {}, dict(now=1000.0 + 91 * 60), pol.ALLOW_STOP, "budget_time", None),
    ("budget_jev", LINEAR, dict(cur="implement", done=("scaffold",), jev_calls=200),
     J("implement"), {}, {}, pol.ALLOW_STOP, "budget_jev", None),
    ("hook_cap", LINEAR, dict(cur="implement", done=("scaffold",), consecutive_blocks=7,
                              blocks_this_session=0),
     J("implement"), {}, dict(stop_hook_active=True), pol.ALLOW_STOP, "hook_cap", None),
    ("hook_cap_resets_without_stop_hook_active", LINEAR,
     dict(cur="implement", done=("scaffold",), consecutive_blocks=7),
     J("implement"), {}, dict(stop_hook_active=False), pol.BLOCK, "drop_band", None),
    ("regression", LINEAR, dict(cur="implement", done=("scaffold",)),
     J("implement"), {"scaffold": FAIL}, {}, pol.BLOCK, "regression", "scaffold"),
    ("loop_continue", LOOPED, dict(cur="test", done=("implement",)),
     J("test"), {"test": FAIL}, dict(loop_checks={"test": FAIL}), pol.BLOCK, "loop_continue", None),
    ("loop_until_pass_but_check_fails", LOOPED, dict(cur="test", done=("implement",)),
     J("test"), {"test": FAIL}, dict(loop_checks={"test": PASS}), pol.BLOCK, "loop_continue", None),
    ("loop_pass", LOOPED, dict(cur="test", done=("implement",)),
     J("test"), {"test": PASS}, dict(loop_checks={"test": PASS}), pol.ALLOW_STOP, "goal_complete", None),
    ("loop_exhausted", LOOPED, dict(cur="test", done=("implement",), loop_iterations={"test": 3}),
     J("test"), {"test": FAIL}, dict(loop_checks={"test": FAIL}), pol.ALLOW_STOP, "loop_exhausted", None),
    ("loop_exhausted_on_fail", LOOP_BRANCH, dict(cur="test", loop_iterations={"test": 2}),
     J("test"), {"test": FAIL}, dict(loop_checks={"test": FAIL}), pol.ADVANCE,
     "loop_exhausted_on_fail", "debug"),
    ("degraded_check_pass", LINEAR, dict(cur="scaffold"),
     Judgment.degraded_result("JevHTTPError: 503"), {"scaffold": PASS}, {},
     pol.ADVANCE, "degraded_check_pass", "implement"),
    ("degraded_check_fail", LINEAR, dict(cur="test", done=("scaffold", "implement")),
     Judgment.degraded_result("timeout"), {"scaffold": PASS, "test": FAIL}, {},
     pol.BLOCK, "degraded_check_fail", None),
    ("degraded_no_check", LINEAR, dict(cur="implement", done=("scaffold",)),
     None, {"scaffold": PASS}, {}, pol.ALLOW_STOP, "degraded_no_check", None),
    ("ask_human", LINEAR, dict(cur="implement", done=("scaffold",)),
     J("implement", next_action="ask_human", next_action_conf=0.85), {}, {},
     pol.ALLOW_STOP, "ask_human", None),
    ("ask_human_low_conf_ignored", LINEAR, dict(cur="implement", done=("scaffold",)),
     J("implement", next_action="ask_human", next_action_conf=0.6), {}, {},
     pol.BLOCK, "drop_band", None),
    ("stuck_once", LINEAR, dict(cur="test", done=("scaffold", "implement")),
     J("test", stuck=0.95), {"test": FAIL}, {}, pol.BLOCK, "stuck", None),
    ("stuck_twice_escalates", LINEAR, dict(cur="test", done=("scaffold", "implement"), stuck_streak=1),
     J("test", stuck=0.95), {"test": FAIL}, {}, pol.BLOCK, "stuck_escalate", None),
    ("same_reason_3x_escalates", LINEAR, dict(cur="test", done=("scaffold", "implement"),
                                              same_reason_count=3),
     J("test"), {"test": FAIL}, {}, pol.BLOCK, "stuck_escalate", None),
    ("stuck_third_escalation_asks_human", LINEAR,
     dict(cur="test", done=("scaffold", "implement"), stuck_streak=1, escalations=2),
     J("test", stuck=0.95), {"test": FAIL}, {}, pol.ALLOW_STOP, "stuck_ask_human", None),
    ("off_goal", LINEAR, dict(cur="implement", done=("scaffold",)),
     J("implement", off_goal=0.9), {}, {}, pol.BLOCK, "off_goal", None),
    ("on_fail_branch", BRANCH, dict(cur="test", done=("implement",)),
     J("test", claims_done=0.93), {"test": FAIL}, {}, pol.ADVANCE, "on_fail", "debug"),
    ("premature_completion", LINEAR, dict(cur="test", done=("scaffold", "implement")),
     J("test", claims_done=0.93), {"scaffold": PASS, "test": FAIL}, {},
     pol.BLOCK, "premature_completion", None),
    ("unclear_never_advances", LINEAR, dict(cur="scaffold"),
     J("unclear", conf=0.9, verify=None), {"scaffold": PASS}, {}, pol.BLOCK, "unclear", None),
    ("advance_with_check", LINEAR, dict(cur="scaffold"),
     J("scaffold", verify=0.9), {"scaffold": PASS}, {}, pol.ADVANCE, "advance", "implement"),
    ("advance_no_check_phase", LINEAR, dict(cur="implement", done=("scaffold",)),
     J("implement", conf=0.83, verify=0.83), {"scaffold": PASS}, {}, pol.ADVANCE, "advance", "test"),
    ("check_pass_but_verify_low_keeps_phase", LINEAR, dict(cur="scaffold"),
     J("scaffold", verify=0.1), {"scaffold": PASS}, {}, pol.BLOCK, "drop_band", None),
    ("check_not_run_never_advances", LINEAR, dict(cur="scaffold"),
     J("scaffold", verify=0.95), {}, {}, pol.BLOCK, "continue", None),
    ("low_conf_never_advances", LINEAR, dict(cur="scaffold"),
     J("scaffold", conf=0.4, verify=0.95), {"scaffold": PASS}, {}, pol.BLOCK, "drop_band", None),
    ("review_band_note", LINEAR, dict(cur="implement", done=("scaffold",)),
     J("implement", conf=0.9, verify=0.65), {}, {}, pol.BLOCK, "review_band", None),
    ("phase_mismatch_keeps_current", LINEAR, dict(cur="implement", done=("scaffold",)),
     J("test", conf=0.9, verify=0.9), {}, {}, pol.BLOCK, "phase_mismatch", None),
    ("goal_complete", LINEAR, dict(cur="test", done=("scaffold", "implement")),
     J("test", verify=0.9), {"scaffold": PASS, "test": PASS}, {}, pol.ALLOW_STOP, "goal_complete", None),
    ("dag_eligibility", DAG, dict(cur="api"),
     J("api", verify=0.9), {}, {}, pol.ADVANCE, "advance", "cli"),
    ("dag_waits_for_all_deps", DAG, dict(cur="cli", done=("api",)),
     J("cli", verify=0.9), {}, {}, pol.ADVANCE, "advance", "docs"),
    ("branch_only_not_required_for_goal", BRANCH, dict(cur="test", done=("implement",)),
     J("test", verify=0.9), {"test": PASS}, {}, pol.ALLOW_STOP, "goal_complete", None),
    ("branch_returns_to_failed_phase", BRANCH, dict(cur="debug", done=("implement",)),
     J("debug", verify=0.9), {"test": FAIL}, {}, pol.ADVANCE, "advance", "test"),
    ("bad_state_asks_human", LINEAR, dict(cur="scaffold"),
     J("scaffold"), {}, {}, pol.ALLOW_STOP, "bad_state", None),
]


class TestPolicyTable(unittest.TestCase):
    def test_cases(self):
        self.assertGreaterEqual(len(CASES), 20)
        for name, fdict, skw, j, checks, dkw, kind, cond, to in CASES:
            with self.subTest(name):
                flow = parse_flow(fdict)
                skw = dict(skw)
                done_flag = skw.pop("done_flag", False)
                state = S(flow, **skw)
                state["done"] = done_flag
                if name == "bad_state_asks_human":
                    state["current_phase"] = "ghost"
                dkw = dict(dkw)
                now = dkw.pop("now", 1000.0 + 60)
                before = repr(state)
                d = pol.decide(flow, state, j, checks, now=now, **dkw)
                self.assertEqual(repr(state), before, "decide must not mutate state")
                self.assertEqual((d.kind, d.condition), (kind, cond), d.reason)
                self.assertEqual(d.to_phase, to)
                if d.blocks:
                    self.assertTrue(d.reason.strip())


class TestPolicyDetails(unittest.TestCase):
    def setUp(self):
        self.flow = parse_flow(LINEAR)

    def decide(self, state, j, checks, **kw):
        return pol.decide(self.flow, state, j, checks, now=1060.0, **kw)

    def test_block_reason_quotes_failing_check_output(self):
        s = S(self.flow, "test", done=("scaffold", "implement"))
        d = self.decide(s, J("test", claims_done=0.93), {"scaffold": PASS, "test": FAIL})
        self.assertIn("AssertionError: expected 3", d.reason)
        self.assertIn("suite passes", d.reason)

    def test_check_output_truncated(self):
        s = S(self.flow, "test", done=("scaffold", "implement"))
        d = self.decide(s, J("test", claims_done=0.93),
                        {"scaffold": PASS, "test": CheckResult(False, "x" * 10000 + "TAIL")})
        self.assertLess(len(d.reason), pol.OUTPUT_CHARS + 400)
        self.assertIn("TAIL", d.reason)

    def test_apply_advance(self):
        s = S(self.flow, "scaffold")
        d = self.decide(s, J("scaffold", verify=0.9), {"scaffold": PASS})
        pol.apply_decision(s, d)
        self.assertEqual(s["current_phase"], "implement")
        self.assertEqual(s["phase_status"]["scaffold"], "done")
        self.assertEqual(s["phase_status"]["implement"], "active")
        self.assertEqual(s["blocks_this_session"], 0)  # advances do not spend the budget
        self.assertEqual(s["consecutive_blocks"], 1)

    def test_budget_exhausted_but_finished_completes(self):
        # seen live: last phase's check passed on the stop that hit the block budget
        s = S(self.flow, "test", done=("scaffold", "implement"), blocks_this_session=99)
        d = self.decide(s, J("test"), {"scaffold": PASS, "test": PASS})
        self.assertEqual(d.condition, "goal_complete")
        d = self.decide(s, J("test"), {"scaffold": PASS, "test": FAIL})
        self.assertEqual(d.condition, "budget_blocks")

    def test_budget_out_without_checks_just_stops(self):
        # seen live: 'tests' check passed, Jev stayed in the review band until
        # the budget ran out, and 'docs' (never entered) also already passed
        flow = parse_flow(DAG)
        s = S(flow, "cli", done=("api",), blocks_this_session=6)
        checks = {"cli": PASS, "docs": PASS, "ship": FAIL}
        # DAG phases define no checks, so nothing can be settled
        d = pol.decide(flow, s, J("cli"), checks, now=1000.0)
        self.assertEqual(d.condition, "budget_blocks")
        self.assertIn("reply", d.reason)

    def test_budget_out_marks_passing_current_done_and_moves_on(self):
        doc = {"goal": GOAL, "phases": [
            {"id": "tests", "name": "Tests", "done_when": "pytest passes", "check": "pytest"},
            {"id": "docs", "name": "Docs", "done_when": "README", "check": "test -f README.md"},
            {"id": "ship", "name": "Ship", "done_when": "shipped", "check": "false"}]}
        flow = parse_flow(doc)
        s = S(flow, "tests", blocks_this_session=6)
        d = pol.decide(flow, s, J("tests", conf=0.6, verify=0.6),
                       {"tests": PASS, "docs": PASS, "ship": FAIL}, now=1000.0)
        self.assertEqual(d.condition, "budget_blocks")
        pol.apply_decision(s, d)
        self.assertEqual(s["phase_status"]["tests"], "done")
        self.assertEqual(s["phase_status"]["docs"], "done")
        self.assertEqual(s["current_phase"], "ship")
        self.assertEqual(s["phase_status"]["ship"], "active")
        # and when every remaining check passes, the goal completes
        s = S(flow, "tests", blocks_this_session=6)
        d = pol.decide(flow, s, J("tests"), {"tests": PASS, "docs": PASS, "ship": PASS}, now=1000.0)
        self.assertEqual(d.condition, "goal_complete")
        pol.apply_decision(s, d)
        self.assertTrue(s["done"])
        self.assertEqual(set(s["phase_status"].values()), {"done"})

    def test_review_streak_survives_other_holds(self):
        doc = {"goal": GOAL, "phases": [
            {"id": "tests", "name": "Tests", "done_when": "pytest passes", "check": "pytest"},
            {"id": "docs", "name": "Docs", "done_when": "README", "check": "test -f README.md"}]}
        flow = parse_flow(doc)
        s = S(flow, "tests")
        band = J("tests", conf=0.6, verify=0.6)
        pol.apply_decision(s, pol.decide(flow, s, band, {"tests": PASS}, now=1000.0))
        self.assertEqual(s["review_streak"], {"phase": "tests", "n": 1})
        d = pol.decide(flow, s, J("tests", off_goal=0.9), {"tests": PASS}, now=1000.0)
        self.assertEqual(d.condition, "off_goal")
        pol.apply_decision(s, d)
        self.assertEqual(s["review_streak"], {"phase": "tests", "n": 1})
        d = pol.decide(flow, s, band, {"tests": PASS}, now=1000.0)
        self.assertEqual(d.condition, "review_check_pass")

    def test_apply_goal_complete_sets_done(self):
        s = S(self.flow, "test", done=("scaffold", "implement"))
        d = self.decide(s, J("test", verify=0.9), {"scaffold": PASS, "test": PASS})
        pol.apply_decision(s, d)
        self.assertTrue(s["done"])
        self.assertEqual(s["consecutive_blocks"], 0)
        self.assertEqual(s["blocks_this_session"], 0)

    def test_consecutive_blocks_counted_only_while_stop_hook_active(self):
        s = S(self.flow, "implement", done=("scaffold",), consecutive_blocks=3)
        d = self.decide(s, J("implement"), {}, stop_hook_active=True)
        self.assertEqual(d.patch["consecutive_blocks"], 4)
        d = self.decide(s, J("implement"), {}, stop_hook_active=False)
        self.assertEqual(d.patch["consecutive_blocks"], 1)

    def test_same_failure_counter(self):
        s = S(self.flow, "test", done=("scaffold", "implement"))
        checks = {"scaffold": PASS, "test": FAIL}
        for expected in (1, 2, 3):
            d = self.decide(s, J("test"), checks)
            self.assertEqual(d.patch["same_reason_count"], expected)
            pol.apply_decision(s, d)
        self.assertEqual(self.decide(s, J("test"), checks).condition, "stuck_escalate")

    def test_plain_continue_is_not_looping(self):
        # critic: a long no-check phase must not escalate just because the
        # continue instruction repeats
        s = S(self.flow, "implement", done=("scaffold",), blocks_this_session=0)
        for _ in range(5):
            d = self.decide(s, J("implement"), {"scaffold": PASS})
            self.assertEqual(d.condition, "drop_band")
            pol.apply_decision(s, d)
        self.assertEqual(s["same_reason_count"], 0)

    def test_final_check_fail_blocks_instead_of_asking_human(self):
        # critic: last phase done but an earlier, not-yet-run check fails
        s = S(self.flow, "test", done=("scaffold", "implement"))
        d = self.decide(s, J("test", verify=0.9), {"test": PASS})
        self.assertEqual((d.kind, d.condition), (pol.BLOCK, "final_check_fail"))
        self.assertIn("scaffold", d.reason)

    def test_session_block_budget_above_claude_cap(self):
        # critic: max_blocks_per_session is a session budget, not Claude's 8 cap
        flow = parse_flow(dict(LINEAR, limits={"max_blocks_per_session": 12}))
        s = S(flow, "implement", done=("scaffold",), blocks_this_session=9)
        d = pol.decide(flow, s, J("implement"), {"scaffold": PASS}, now=1060.0)
        self.assertEqual(d.kind, pol.BLOCK)

    def test_branch_only_check_not_a_regression(self):
        flow = parse_flow({"goal": GOAL, "phases": [
            {"id": "implement", "name": "I", "done_when": "w", "check": "a", "on_fail": "debug"},
            {"id": "test", "name": "T", "done_when": "w"},
            {"id": "debug", "name": "D", "done_when": "w", "check": "repro", "depends_on": []}]})
        s = S(flow, "test", done=("implement", "debug"))
        d = pol.decide(flow, s, J("test"), {"implement": PASS, "debug": FAIL}, now=1060.0)
        self.assertNotEqual(d.condition, "regression")

    def test_loop_increments_iterations(self):
        flow = parse_flow(LOOPED)
        s = S(flow, "test", done=("implement",), loop_iterations={"test": 1})
        d = pol.decide(flow, s, J("test"), {"test": FAIL}, now=1060.0, loop_checks={"test": FAIL})
        pol.apply_decision(s, d)
        self.assertEqual(s["loop_iterations"]["test"], 2)
        self.assertIn("iteration 2 of 3", d.reason)

    def test_ask_human_sets_needs_human(self):
        s = S(self.flow, "implement", done=("scaffold",))
        d = self.decide(s, J("implement", next_action="ask_human", next_action_conf=0.9), {})
        pol.apply_decision(s, d)
        self.assertTrue(s["needs_human"])
        self.assertEqual(d.question, s["needs_human"])

    def test_on_fail_route_resets_failed_phase(self):
        flow = parse_flow(BRANCH)
        s = S(flow, "test", done=("implement",))
        d = pol.decide(flow, s, J("test", claims_done=0.93), {"test": FAIL}, now=1060.0)
        pol.apply_decision(s, d)
        self.assertEqual(s["current_phase"], "debug")
        self.assertEqual(s["phase_status"], {"implement": "done", "test": "pending", "debug": "active"})

    def test_budgets_win_over_everything(self):
        s = S(self.flow, "scaffold", blocks_this_session=6)
        d = self.decide(s, J("scaffold", verify=0.99), {"scaffold": PASS})
        self.assertEqual(d.condition, "budget_blocks")

    def test_branch_only_excluded_from_eligibility(self):
        flow = parse_flow(BRANCH)
        self.assertEqual(flow.branch_only, frozenset({"debug"}))
        self.assertEqual(flow.required(), ["implement", "test"])
        self.assertNotIn("debug", flow.eligible({}))


if __name__ == "__main__":
    unittest.main()


class TestReviewStreak(unittest.TestCase):
    """C1 live finding: a passing check + Jev stuck in the review band must not stall."""

    def setUp(self):
        self.flow = parse_flow(LINEAR)

    def step(self, st, j, checks):
        d = pol.decide(self.flow, st, j, checks, now=0.0)
        pol.apply_decision(st, d)
        return d

    def test_second_review_band_with_passing_check_advances(self):
        st = S(self.flow, "scaffold")
        d1 = self.step(st, J("scaffold", conf=0.85, verify=0.63), {"scaffold": PASS})
        self.assertEqual((d1.kind, d1.condition), (pol.BLOCK, "review_band"))
        self.assertIn("its check passes", d1.reason)
        self.assertEqual(st["review_streak"], {"phase": "scaffold", "n": 1})
        d2 = self.step(st, J("scaffold", conf=0.85, verify=0.63), {"scaffold": PASS})
        self.assertEqual((d2.kind, d2.condition, d2.to_phase), (pol.ADVANCE, "review_check_pass", "implement"))
        self.assertIsNone(st["review_streak"])

    def test_streak_broken_by_other_outcome(self):
        st = S(self.flow, "scaffold")
        self.step(st, J("scaffold", conf=0.85, verify=0.63), {"scaffold": PASS})
        d = self.step(st, J("scaffold", conf=0.85, verify=0.2), {"scaffold": PASS})   # drop band
        self.assertEqual(d.condition, "drop_band")
        self.assertIsNone(st["review_streak"])
        d = self.step(st, J("scaffold", conf=0.85, verify=0.63), {"scaffold": PASS})
        self.assertEqual(d.condition, "review_band", "streak restarted at 1")

    def test_no_check_review_band_never_advances(self):
        st = S(self.flow, "implement", done=("scaffold",))
        for _ in range(4):
            d = self.step(st, J("implement", conf=0.9, verify=0.65), {"scaffold": PASS})
            self.assertEqual(d.condition, "review_band")
        self.assertIsNone(st.get("review_streak"))

    def test_failing_check_review_band_never_advances(self):
        st = S(self.flow, "scaffold")
        for _ in range(3):
            d = self.step(st, J("scaffold", conf=0.85, verify=0.63), {"scaffold": FAIL})
            self.assertNotEqual(d.kind, pol.ADVANCE)


class TestShadowModes(unittest.TestCase):
    """F1: the observe / warn / enforce ladder (SPEC 10.3), one row per mode x kind."""

    BLOCK = pol.Decision(pol.BLOCK, "premature_completion", "tests fail",
                         patch={"blocks_inc": 1, "consecutive_blocks": 3, "stuck_streak": 0})
    ADV = pol.Decision(pol.ADVANCE, "advance", "go to b", to_phase="b",
                       patch={"blocks_inc": 1, "consecutive_blocks": 1,
                              "phase_status": {"a": "done", "b": "active"}, "current_phase": "b"})
    ASK = pol._ask_human("loop_exhausted", "which db?")
    STOP = pol.Decision(pol.ALLOW_STOP, "goal_complete", "done", patch={"done": True})

    CASES = [
        # mode,      decision, block, msg_prefix,                    ask
        ("enforce", "BLOCK", True, None, False),
        ("enforce", "ADV", True, None, False),
        ("enforce", "ASK", False, None, True),
        ("enforce", "STOP", False, None, False),
        ("warn", "BLOCK", False, "[jevflow warn] would block (premature_completion)", False),
        ("warn", "ADV", False, "[jevflow warn] would block (advance); phase table moved to 'b'", False),
        ("warn", "ASK", False, "[jevflow warn] needs a human: which db?", True),
        ("warn", "STOP", False, None, False),
        ("observe", "BLOCK", False, None, False),
        ("observe", "ADV", False, None, False),
        ("observe", "ASK", False, None, False),
        ("observe", "STOP", False, None, False),
    ]

    def test_table(self):
        for mode, name, block, msg, ask in self.CASES:
            d = getattr(self, name)
            with self.subTest(mode=mode, decision=name):
                e = pol.apply_mode(d, mode)
                self.assertEqual(e.block, block)
                self.assertEqual(e.ask_human, ask)
                if msg is None:
                    self.assertIsNone(e.message)
                else:
                    self.assertTrue(e.message.startswith(msg), e.message)

    def test_shadow_modes_do_not_charge_blocks_but_keep_bookkeeping(self):
        for mode in ("warn", "observe"):
            with self.subTest(mode=mode):
                st = {"blocks_this_session": 2, "consecutive_blocks": 1,
                      "phase_status": {"a": "active", "b": "pending"}, "current_phase": "a"}
                pol.apply_decision(st, pol.apply_mode(self.ADV, mode).decision)
                self.assertEqual(st["blocks_this_session"], 2)
                self.assertEqual(st["consecutive_blocks"], 0)
                self.assertEqual((st["current_phase"], st["phase_status"]["a"]), ("b", "done"))

    def test_enforce_charges_blocks(self):
        st = {"blocks_this_session": 2}
        pol.apply_decision(st, pol.apply_mode(self.BLOCK, "enforce").decision)
        self.assertEqual((st["blocks_this_session"], st["consecutive_blocks"]), (3, 3))

    def test_observe_ask_human_is_not_persisted(self):
        e = pol.apply_mode(self.ASK, "observe")
        self.assertIsNone(e.decision.question)
        st = {}
        pol.apply_decision(st, e.decision)
        self.assertNotIn("needs_human", st)
        st = {}
        pol.apply_decision(st, pol.apply_mode(self.ASK, "warn").decision)
        self.assertEqual(st["needs_human"], "which db?")

    def test_input_not_mutated(self):
        before = (self.BLOCK.patch.copy(), self.ASK.patch.copy(), self.ASK.question)
        for mode in ("enforce", "warn", "observe"):
            pol.apply_mode(self.BLOCK, mode)
            pol.apply_mode(self.ASK, mode)
        self.assertEqual((self.BLOCK.patch, self.ASK.patch, self.ASK.question), before)


class TestLoopMessage(unittest.TestCase):
    def test_until_pass_reports_phase_check_output(self):
        # real wordstats run: the until-check passed but the phase check failed;
        # the block used to show the passing until output ("OK") as the failure
        flow = parse_flow(LOOPED)
        state = S(flow, "test", done=("implement",))
        until_ok = CheckResult(True, "Ran 3 tests\n\nOK")
        phase_bad = CheckResult(False, "no extra test file")
        d = pol.decide(flow, state, J("test"), {"implement": PASS, "test": phase_bad},
                       now=1060.0, loop_checks={"test": until_ok})
        self.assertEqual(d.condition, "loop_continue")
        self.assertIn("no extra test file", d.reason)
        self.assertNotIn("Ran 3 tests", d.reason)
        self.assertIn("phase check fails", d.reason)

