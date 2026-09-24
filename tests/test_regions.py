"""F4: dynamic region sub-steps and idempotent side-effect phases (SPEC 10.1, 10.2)."""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from jevflow import judge as jd  # noqa: E402
from jevflow import policy as pol  # noqa: E402
from jevflow import regions  # noqa: E402
from jevflow.flow import FlowError, parse_flow  # noqa: E402
from jevflow.judge import CheckResult, Judgment  # noqa: E402
from jevflow.state import load_state, new_state, save_state  # noqa: E402

from tests.test_hooks import FakeClient, HookCase, answers, fixture  # noqa: E402

PASS = CheckResult(True, "ok")
FAIL = CheckResult(False, "publish marker missing")

DYN = {
    "goal": "Toy: build then write docs",
    "phases": [
        {"id": "build", "name": "Build", "done_when": "features work", "dynamic": True},
        {"id": "docs", "name": "Docs", "done_when": "README exists"},
    ],
}
SIDE = {
    "goal": "Toy: build then publish",
    "phases": [
        {"id": "build", "name": "Build", "done_when": "built", "check": "test -f built.txt"},
        {"id": "publish", "name": "Publish", "done_when": "published", "check": "test -f pub.txt",
         "side_effect": True},
        {"id": "announce", "name": "Announce", "done_when": "announced"},
    ],
}


def J(phase, conf=0.95, verify=0.95, sub=None, sub_conf=0.0, **kw):
    j = Judgment(current_phase=phase, current_phase_conf=conf, next_action="advance_phase",
                 next_action_conf=0.9, verify_phase=phase, verify=verify, stuck=0.05,
                 off_goal=0.02, claims_done=0.1, calls=2,
                 current_subtask=sub, current_subtask_conf=sub_conf)
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


SUBS = {"build": [{"id": "s1", "title": "parser", "done": True},
                  {"id": "s2", "title": "storage", "done": False}]}


class TestParseSubtasks(unittest.TestCase):
    def setUp(self):
        self.flow = parse_flow(DYN)

    def test_strings_and_objects_get_positional_ids(self):
        out, warn = regions.parse_subtasks(
            {"build": ["parser", {"title": "  storage\n layer ", "done": True}]}, self.flow)
        self.assertEqual(out["build"], [{"id": "s1", "title": "parser", "done": False},
                                        {"id": "s2", "title": "storage layer", "done": True}])
        self.assertEqual(warn, [])

    def test_skeleton_cannot_be_edited(self):
        out, warn = regions.parse_subtasks(
            {"docs": ["x"], "ship": ["y"], "phases": [{"id": "evil"}]}, self.flow)
        self.assertEqual(out, {})
        self.assertEqual(len(warn), 3)

    def test_caps_and_bad_items(self):
        items = [""] + [f"step {i}" for i in range(20)] + [{"title": 3}]
        out, warn = regions.parse_subtasks({"build": items}, self.flow)
        self.assertEqual(len(out["build"]), regions.MAX_SUBTASKS)
        self.assertEqual(out["build"][-1]["id"], "s12")
        self.assertTrue(any("first" in w for w in warn))
        long, _ = regions.parse_subtasks({"build": ["x" * 999]}, self.flow)
        self.assertEqual(len(long["build"][0]["title"]), regions.SUBTASK_TITLE_CHARS)

    def test_done_must_be_true_literal(self):
        out, _ = regions.parse_subtasks({"build": [{"title": "a", "done": "yes"}]}, self.flow)
        self.assertFalse(out["build"][0]["done"])

    def test_not_an_object(self):
        out, warn = regions.parse_subtasks(["a"], self.flow)
        self.assertEqual(out, {})
        self.assertEqual(len(warn), 1)

    def test_subtasks_for_ignores_non_dynamic_and_forged_ids(self):
        s = S(self.flow, "build", subtasks={"build": [{"id": "../x", "title": "t"},
                                                      {"id": "s1", "title": "ok"}],
                                            "docs": [{"id": "s1", "title": "t"}]})
        self.assertEqual([t["id"] for t in regions.subtasks_for(s, self.flow, "build")], ["s1"])
        self.assertEqual(regions.subtasks_for(s, self.flow, "docs"), [])
        self.assertEqual(regions.subtasks_for(s, self.flow, "nope"), [])


class TestSubtaskJudge(unittest.TestCase):
    def setUp(self):
        self.flow = parse_flow(DYN)

    def test_choice_question_only_with_subtasks(self):
        self.assertNotIn("current_subtask", jd.build_questions(self.flow, "build"))
        q = jd.build_questions(self.flow, "build", SUBS["build"])
        c = q["current_subtask"]
        self.assertEqual(c["type"], "choice")
        self.assertEqual(set(c["criteria"]), {"s1", "s2", regions.ALL_DONE, jd.UNCLEAR})

    def test_state_lists_substeps_of_current_phase_only(self):
        s = S(self.flow, "build", subtasks=SUBS)
        doc = json.loads(jd.build_state(self.flow, s, checks={}))
        self.assertEqual([t["id"] for t in doc["current_phase_substeps"]], ["s1", "s2"])
        s2 = S(self.flow, "docs", done=("build",), subtasks=SUBS)
        self.assertNotIn("current_phase_substeps", json.loads(jd.build_state(self.flow, s2, checks={})))

    def test_state_budget_still_holds(self):
        many = {"build": [{"id": f"s{i}", "title": "t" * 200, "done": False} for i in range(1, 13)]}
        s = S(self.flow, "build", subtasks=many)
        self.assertLessEqual(len(jd.build_state(self.flow, s, checks={}, budget=3000)), 3000)

    def test_parse_and_judge_end_to_end(self):
        ans = answers("build")
        ans["current_subtask"] = {"choice": "s2", "confidence": 0.9, "probabilities": {"s2": 0.9}}
        client = FakeClient(ans, {"verify": {"noul": 0.9}})
        j = jd.judge(client, self.flow, S(self.flow, "build", subtasks=SUBS), checks={})
        self.assertEqual((j.current_subtask, j.current_subtask_conf), ("s2", 0.9))
        self.assertEqual(j.probs()["current_subtask"], ["s2", 0.9])

    def test_forged_or_missing_choice_is_unclear(self):
        ans = answers("build")
        ans["current_subtask"] = {"choice": "s9", "probabilities": {"s9": 1.0}}
        j = jd.parse_answers(self.flow, ans, ["s1", "s2"])
        self.assertEqual(j.current_subtask, jd.UNCLEAR)
        j2 = jd.parse_answers(self.flow, answers("build"), ["s1"])
        self.assertEqual(j2.current_subtask, jd.UNCLEAR)
        j3 = jd.parse_answers(self.flow, answers("build"))
        self.assertIsNone(j3.current_subtask)


class TestSubtaskPolicy(unittest.TestCase):
    """The sub-step choice can hold an advance, never cause one."""

    def setUp(self):
        self.flow = parse_flow(DYN)
        self.s = S(self.flow, "build", subtasks=SUBS)

    def decide(self, j, state=None, checks=None):
        return pol.decide(self.flow, state or self.s, j, checks or {}, now=1000.0)

    def test_table(self):
        rows = [
            # (sub, conf, expected condition)
            ("s2", 0.95, "subtask_pending"),
            ("s2", 0.60, "advance"),          # below auto: no hold
            (regions.ALL_DONE, 0.99, "advance"),
            (jd.UNCLEAR, 0.99, "advance"),
            (None, 0.0, "advance"),
            ("s7", 0.99, "advance"),          # unknown id never holds
        ]
        for sub, conf, want in rows:
            with self.subTest(sub=sub, conf=conf):
                d = self.decide(J("build", sub=sub, sub_conf=conf))
                self.assertEqual(d.condition, want)
        d = self.decide(J("build", sub="s2", sub_conf=0.95))
        self.assertEqual(d.kind, pol.BLOCK)
        self.assertIn("storage", d.reason)

    def test_all_done_alone_never_advances(self):
        # verify is low: the sub-step choice is not an advance signal
        d = self.decide(J("build", verify=0.1, sub=regions.ALL_DONE, sub_conf=0.99))
        self.assertNotEqual(d.kind, pol.ADVANCE)

    def test_no_subtasks_no_hold(self):
        d = self.decide(J("build", sub="s2", sub_conf=0.99), state=S(self.flow, "build"))
        self.assertEqual(d.condition, "advance")

    def test_review_check_pass_is_held(self):
        flow = parse_flow({"goal": "g", "phases": [
            {"id": "build", "name": "Build", "done_when": "w", "check": "true", "dynamic": True},
            {"id": "docs", "name": "Docs", "done_when": "d"}]})
        s = S(flow, "build", subtasks=SUBS, review_streak={"phase": "build", "n": 1})
        d = pol.decide(flow, s, J("build", verify=0.6, sub="s2", sub_conf=0.9),
                       {"build": PASS}, now=1000.0)
        self.assertEqual(d.condition, "subtask_pending")
        d2 = pol.decide(flow, s, J("build", verify=0.6, sub=regions.ALL_DONE, sub_conf=0.9),
                        {"build": PASS}, now=1000.0)
        self.assertEqual(d2.condition, "review_check_pass")

    def test_degraded_mode_ignores_subtasks(self):
        flow = parse_flow({"goal": "g", "phases": [
            {"id": "build", "name": "Build", "done_when": "w", "check": "true", "dynamic": True},
            {"id": "docs", "name": "Docs", "done_when": "d"}]})
        d = pol.decide(flow, S(flow, "build", subtasks=SUBS), None, {"build": PASS}, now=1000.0)
        self.assertEqual(d.condition, "degraded_check_pass")


class TestSideEffectPolicy(unittest.TestCase):
    def setUp(self):
        self.flow = parse_flow(SIDE)

    def test_key_format_and_attempts(self):
        s = new_state(self.flow)
        self.assertEqual(s["phase_attempts"], {"build": 1})
        self.assertEqual(regions.idempotency_key(self.flow, s, "build"), "1:build:1")
        self.assertEqual(regions.idempotency_key(self.flow, s, "publish"), "1:publish:1")
        d = pol.decide(self.flow, s, None, {"build": PASS}, now=s["started_at"])
        self.assertEqual(d.condition, "degraded_check_pass")
        pol.apply_decision(s, d)
        self.assertEqual(s["phase_attempts"], {"build": 1, "publish": 1})
        # re-entering an active phase is not a new attempt
        pol.apply_decision(s, pol.Decision(pol.BLOCK, "x", patch={"phase_status": {"publish": "active"}}))
        self.assertEqual(s["phase_attempts"]["publish"], 1)
        pol.apply_decision(s, pol.Decision(pol.BLOCK, "x", patch={"phase_status": {"publish": "pending"}}))
        pol.apply_decision(s, pol.Decision(pol.BLOCK, "x", patch={"phase_status": {"publish": "active"}}))
        self.assertEqual(regions.idempotency_key(self.flow, s, "publish"), "1:publish:2")

    def test_regression_of_side_effect_phase_asks_human(self):
        s = S(self.flow, "announce", done=("build", "publish"))
        d = pol.decide(self.flow, s, None, {"build": PASS, "publish": FAIL}, now=1000.0)
        self.assertEqual(d.condition, "side_effect_regression")
        self.assertIsNotNone(d.question)
        self.assertNotIn("current_phase", d.patch)
        self.assertIn("1:publish:1", d.question)

    def test_regression_of_normal_phase_still_routes_back(self):
        s = S(self.flow, "announce", done=("build", "publish"))
        d = pol.decide(self.flow, s, None, {"build": FAIL, "publish": PASS}, now=1000.0)
        self.assertEqual((d.condition, d.to_phase), ("regression", "build"))

    def test_on_fail_into_done_side_effect_asks_human(self):
        flow = parse_flow({"goal": "g", "phases": [
            {"id": "publish", "name": "Publish", "done_when": "p", "side_effect": True,
             "check": "true", "depends_on": []},
            {"id": "verify", "name": "Verify", "done_when": "v", "check": "false",
             "on_fail": "publish", "depends_on": []}]})
        s = S(flow, "verify", done=("publish",))
        d = pol.decide(flow, s, J("verify", verify=0.95), {"verify": FAIL}, now=1000.0)
        self.assertEqual(d.condition, "side_effect_on_fail")
        self.assertIsNotNone(d.question)


class TestLedger(HookCase):
    def test_append_load_and_torn_line(self):
        path = self.paths.side_effects
        regions.append_ledger(path, {"phase": "publish", "key": "1:publish:1", "ts": 1.0})
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"phase": "x", "key"')  # torn write
        self.assertEqual([e["key"] for e in regions.load_ledger(path)], ["1:publish:1"])
        self.assertEqual(regions.load_ledger(path + ".missing"), [])

    def test_record_once(self):
        flow = parse_flow(SIDE)
        s = S(flow, "announce", done=("build", "publish"))
        self.assertEqual(regions.record_side_effects(s, flow, self.paths.side_effects, 1.0),
                         ["1:publish:1"])
        self.assertEqual(regions.record_side_effects(s, flow, self.paths.side_effects, 2.0), [])
        self.assertEqual(len(regions.load_ledger(self.paths.side_effects)), 1)

    def test_restore_after_state_reset(self):
        flow = parse_flow(SIDE)
        s = new_state(flow)
        s["phase_status"].update(build="done", publish="active")
        s["current_phase"] = "publish"
        restored = regions.restore_from_ledger(
            s, flow, [{"phase": "publish", "key": "0:publish:1"}])  # older flow_version too
        self.assertEqual(restored, ["publish"])
        self.assertEqual(s["current_phase"], "announce")
        self.assertEqual(s["phase_status"]["announce"], "active")


class TestHooksF4(HookCase):
    def test_stop_ingests_subtasks_and_holds(self):
        doc = dict(DYN)
        self.write_flow(doc)
        with open(self.paths.subtasks, "w", encoding="utf-8") as fh:
            json.dump({"build": ["parser", "storage"], "docs": ["sneaky"]}, fh)
        ans = answers("build", done=0.95)
        ans["current_subtask"] = {"choice": "s2", "confidence": 0.93, "probabilities": {"s2": 0.93}}
        out = self.run_hook("Stop", fixture("Stop_first.json"), client=FakeClient(ans, {"verify": {"noul": 0.95}}))
        self.assertEqual(out["decision"], "block")
        self.assertIn("storage", out["reason"])
        st = self.state()
        self.assertEqual(set(st["subtasks"]), {"build"})
        self.assertEqual(st["current_phase"], "build")
        ev = [h for h in st["history"] if h["event"] == "subtasks"]
        self.assertEqual(ev[0]["counts"], {"build": 2})
        self.assertTrue(ev[0]["warnings"])
        # unchanged file: no new subtasks journal entry
        ans2 = answers("build", done=0.95)
        ans2["current_subtask"] = {"choice": "all_done", "confidence": 0.9,
                                   "probabilities": {"all_done": 0.9}}
        out = self.run_hook("Stop", fixture("Stop_active.json"),
                            client=FakeClient(ans2, {"verify": {"noul": 0.95}}))
        self.assertEqual(out["decision"], "block")  # ADVANCE to docs
        st = self.state()
        self.assertEqual(st["current_phase"], "docs")
        self.assertEqual(len([h for h in st["history"] if h["event"] == "subtasks"]), 1)

    def test_bad_subtasks_file_never_breaks_stop(self):
        self.write_flow(dict(DYN))
        with open(self.paths.subtasks, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        out = self.run_hook("Stop", fixture("Stop_first.json"), client=None)
        self.assertIn("systemMessage", out)  # degraded, no check: allowed stop
        with open(self.paths.subtasks, "wb") as fh:
            fh.write(b"[" + b"1," * 40000 + b"1]")
        out = self.run_hook("Stop", fixture("Stop_first.json"), client=None)
        self.assertIsInstance(out, dict)

    def test_session_context_for_dynamic_and_side_effect(self):
        self.write_flow(dict(DYN))
        st = self.state()
        st["subtasks"] = SUBS
        save_state(self.paths.state, st)
        ctx = self.run_hook("SessionStart", fixture("SessionStart_startup.json"))[
            "hookSpecificOutput"]["additionalContext"]
        self.assertIn(".jevflow/subtasks.json", ctx)
        self.assertIn("- [ ] s2: storage", ctx)
        self.assertIn("dynamic", ctx)

    def _side_effect_run(self):
        self.write_flow(dict(SIDE))
        self.touch("built.txt")
        self.run_hook("Stop", fixture("Stop_first.json"), client=None)            # build -> publish
        st = self.state()
        self.assertEqual(st["current_phase"], "publish")
        ctx = self.run_hook("SessionStart", fixture("SessionStart_resume.json"))[
            "hookSpecificOutput"]["additionalContext"]
        self.assertIn("Idempotency key: `1:publish:1`", ctx)
        self.touch("pub.txt")
        self.run_hook("Stop", fixture("Stop_first.json"), client=None)            # publish -> announce

    def test_side_effect_recorded_once_and_survives_state_reset(self):
        self._side_effect_run()
        ledger = regions.load_ledger(self.paths.side_effects)
        self.assertEqual([e["key"] for e in ledger], ["1:publish:1"])
        st = self.state()
        self.assertEqual(st["current_phase"], "announce")
        self.assertTrue(any(h["event"] == "side_effect_recorded" for h in st["history"]))
        self.run_hook("Stop", fixture("Stop_first.json"), client=None)
        self.assertEqual(len(regions.load_ledger(self.paths.side_effects)), 1)
        # simulate a crash that lost state.json: publish must not be offered again
        os.remove(self.paths.state)
        ctx = self.run_hook("SessionStart", fixture("SessionStart_startup.json"))[
            "hookSpecificOutput"]["additionalContext"]
        st = self.state()
        self.assertEqual(st["phase_status"]["publish"], "done")
        self.assertIn("never repeat them: publish (1:publish:1)", ctx)
        self.assertTrue(any(h["event"] == "side_effect_restored" for h in st["history"]))

    def test_side_effect_regression_writes_needs_human(self):
        self._side_effect_run()
        os.remove(os.path.join(self.dir, "pub.txt"))
        out = self.run_hook("Stop", fixture("Stop_first.json"), client=None)
        self.assertNotIn("decision", out)
        self.assertTrue(os.path.exists(self.paths.needs_human))
        st = self.state()
        self.assertEqual(st["current_phase"], "announce")
        self.assertEqual(st["phase_status"]["publish"], "done")


class TestFlowFlags(unittest.TestCase):
    def test_side_effect_needs_check(self):
        with self.assertRaises(FlowError):
            parse_flow({"goal": "g", "phases": [
                {"id": "a", "name": "A", "done_when": "w", "side_effect": True}]})

    def test_flags_must_be_bool(self):
        for flag in ("dynamic", "side_effect"):
            with self.subTest(flag=flag):
                with self.assertRaises(FlowError):
                    parse_flow({"goal": "g", "phases": [
                        {"id": "a", "name": "A", "done_when": "w", flag: "yes"}]})


if __name__ == "__main__":
    unittest.main()
