import copy
import json
import os
import tempfile
import threading
import unittest

from jevflow import flow as fl
from jevflow import state as st

BASE = {
    "schema_version": 1,
    "flow_version": "3",
    "goal": "Build a CLI todo app with tests",
    "phases": [
        {"id": "scaffold", "name": "Scaffold", "done_when": "layout exists", "check": "test -f todo/cli.py"},
        {"id": "implement", "name": "Implement", "done_when": "commands work"},
        {"id": "test", "name": "Tests", "done_when": "suite passes", "check": "pytest -q",
         "loop": {"max_iterations": 4, "until": "pytest -q"}, "on_fail": "debug"},
        {"id": "debug", "name": "Debug", "done_when": "failure understood", "depends_on": ["implement"]},
    ],
    "limits": {"max_blocks_per_session": 6, "confidence": {"auto": 0.85}},
    "privacy": {"send_diff": False},
}


def flow_with(**over):
    d = copy.deepcopy(BASE)
    d.update(over)
    return d


def phases_with(i, **over):
    d = copy.deepcopy(BASE)
    d["phases"][i].update(over)
    return d


class ValidFlows(unittest.TestCase):
    def test_parse_full(self):
        f = fl.parse_flow(BASE)
        self.assertEqual(f.ids, ["scaffold", "implement", "test", "debug"])
        self.assertEqual(f.flow_version, "3")
        self.assertEqual(f.phase("implement").depends_on, ("scaffold",))  # linear default
        self.assertEqual(f.phase("debug").depends_on, ("implement",))     # explicit
        self.assertEqual(f.phase("test").loop, fl.Loop(4, "pytest -q"))
        self.assertEqual(f.phase("test").on_fail, "debug")
        self.assertEqual(f.limits["confidence"]["auto"], 0.85)
        self.assertEqual(f.limits["confidence"]["review"], 0.50)  # default kept
        self.assertEqual(f.limits["max_restarts"], 5)
        self.assertEqual(f.limits["hang_minutes"], 10)
        self.assertEqual(f.mode, "enforce")

    def test_minimal_defaults(self):
        f = fl.parse_flow({"goal": "g", "phases": [{"id": "a", "name": "A", "done_when": "x"}]})
        self.assertEqual(f.schema_version, 1)
        self.assertEqual(f.flow_version, "1")
        self.assertFalse(f.privacy["send_diff"])
        self.assertEqual(f.phase("a").depends_on, ())
        self.assertIsNone(f.phase("a").check)

    def test_integer_flow_version(self):
        self.assertEqual(fl.parse_flow(flow_with(flow_version=7)).flow_version, "7")

    def test_eligible_follows_dag(self):
        d = {"goal": "g", "phases": [
            {"id": "a", "name": "A", "done_when": "x", "depends_on": []},
            {"id": "b", "name": "B", "done_when": "x", "depends_on": []},
            {"id": "c", "name": "C", "done_when": "x", "depends_on": ["a", "b"]},
        ]}
        f = fl.parse_flow(d)
        self.assertEqual(f.eligible({}), ["a", "b"])
        self.assertEqual(f.eligible({"a": "done"}), ["b"])
        self.assertEqual(f.eligible({"a": "done", "b": "done"}), ["c"])
        self.assertEqual(f.eligible({"a": "done", "b": "done", "c": "done"}), [])

    def test_topo_order_forward_reference(self):
        d = {"goal": "g", "phases": [
            {"id": "b", "name": "B", "done_when": "x", "depends_on": ["a"]},
            {"id": "a", "name": "A", "done_when": "x", "depends_on": []},
        ]}
        self.assertEqual(fl.parse_flow(d).topo_order(), ["a", "b"])

    def test_next_phase(self):
        f = fl.parse_flow(BASE)
        self.assertEqual(f.next_phase("scaffold"), "implement")
        self.assertIsNone(f.next_phase("debug"))

    def test_load_flow_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "flow.json")
            with open(p, "w") as fh:
                json.dump(BASE, fh)
            self.assertEqual(fl.load_flow(p).goal, BASE["goal"])


class InvalidFlows(unittest.TestCase):
    def bad(self, data, fragment):
        with self.assertRaises(fl.FlowError) as cm:
            fl.parse_flow(data)
        self.assertIn(fragment, str(cm.exception))

    def test_not_object(self):
        self.bad([], "JSON object")

    def test_missing_goal(self):
        d = flow_with(); del d["goal"]
        self.bad(d, "goal")

    def test_empty_phases(self):
        self.bad(flow_with(phases=[]), "phases")

    def test_unknown_top_key(self):
        self.bad(flow_with(gaol="typo"), "unknown top-level")

    def test_unknown_phase_key(self):
        self.bad(phases_with(0, chek="typo"), "unknown keys")

    def test_duplicate_ids(self):
        self.bad(phases_with(1, id="scaffold"), "duplicate")

    def test_bad_id(self):
        self.bad(phases_with(0, id="Bad Id"), "must match")

    def test_reserved_id(self):
        self.bad(phases_with(0, id="unclear"), "reserved")

    def test_unknown_dependency(self):
        self.bad(phases_with(3, depends_on=["nope"]), "unknown phase")

    def test_self_dependency(self):
        self.bad(phases_with(3, depends_on=["debug"]), "depends_on itself")

    def test_dag_cycle(self):
        d = {"goal": "g", "phases": [
            {"id": "a", "name": "A", "done_when": "x", "depends_on": ["c"]},
            {"id": "b", "name": "B", "done_when": "x", "depends_on": ["a"]},
            {"id": "c", "name": "C", "done_when": "x", "depends_on": ["b"]},
        ]}
        with self.assertRaises(fl.FlowError) as cm:
            fl.parse_flow(d)
        msg = str(cm.exception)
        self.assertIn("cycle", msg)
        for pid in "abc":
            self.assertIn(pid, msg)

    def test_dependencies_not_list(self):
        self.bad(phases_with(3, depends_on="implement"), "list")

    def test_loop_bad_iterations(self):
        self.bad(phases_with(2, loop={"max_iterations": 0, "until": "x"}), "max_iterations")
        self.bad(phases_with(2, loop={"max_iterations": True, "until": "x"}), "max_iterations")

    def test_loop_missing_until(self):
        self.bad(phases_with(2, loop={"max_iterations": 3}), "until")

    def test_on_fail_unknown_and_self(self):
        self.bad(phases_with(2, on_fail="nope"), "on_fail unknown")
        self.bad(phases_with(2, on_fail="test"), "itself")

    def test_schema_version(self):
        self.bad(flow_with(schema_version=2), "schema_version")
        self.bad(flow_with(schema_version=True), "schema_version")

    def test_bad_limits(self):
        self.bad(flow_with(limits={"max_restarts": -1}), "max_restarts")
        self.bad(flow_with(limits={"max_restrats": 3}), "unknown keys")
        self.bad(flow_with(limits={"confidence": {"auto": 1.5}}), "[0, 1]")
        self.bad(flow_with(limits={"confidence": {"auto": 0.4, "review": 0.6}}), "review must be")

    def test_bad_privacy_and_mode(self):
        self.bad(flow_with(privacy={"send_diff": "no"}), "boolean")
        self.bad(flow_with(mode="block"), "mode")

    def test_missing_and_corrupt_file(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(fl.FlowError):
                fl.load_flow(os.path.join(d, "missing.json"))
            p = os.path.join(d, "flow.json")
            with open(p, "w") as fh:
                fh.write("{not json")
            with self.assertRaises(fl.FlowError):
                fl.load_flow(p)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.flow = fl.parse_flow(BASE)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, ".jevflow", "state.json")

    def test_new_state(self):
        s = st.new_state(self.flow, now=100.0)
        self.assertEqual(s["current_phase"], "scaffold")
        self.assertEqual(s["phase_status"]["scaffold"], "active")
        self.assertEqual(s["phase_status"]["debug"], "pending")
        self.assertFalse(s["done"])
        self.assertEqual(s["flow_version"], "3")
        self.assertEqual(s["started_at"], 100.0)

    def test_load_missing_creates_fresh(self):
        s = st.load_state(self.path, self.flow)
        self.assertEqual(s["current_phase"], "scaffold")
        self.assertFalse(os.path.exists(self.path))  # load does not write

    def test_roundtrip_and_journal_persisted(self):
        s = st.new_state(self.flow)
        st.record(self.path, s, "stop", decision="BLOCK", probs={"stuck": 0.1}, reason="x")
        on_disk = st.load_state(self.path, self.flow)
        self.assertEqual(len(on_disk["history"]), 1)
        e = on_disk["history"][0]
        self.assertEqual((e["event"], e["decision"], e["phase"]), ("stop", "BLOCK", "scaffold"))
        self.assertEqual(e["probs"], {"stuck": 0.1})
        self.assertEqual(e["reason"], "x")

    def test_atomic_write_leaves_no_temp_files(self):
        s = st.new_state(self.flow)
        for i in range(5):
            st.record(self.path, s, "e%d" % i)
        files = os.listdir(os.path.dirname(self.path))
        self.assertEqual(files, ["state.json"])

    def test_failed_write_keeps_old_file(self):
        s = st.new_state(self.flow)
        st.save_state(self.path, s)
        before = open(self.path).read()
        s2 = dict(s, bad=object())  # not JSON serialisable
        with self.assertRaises(TypeError):
            st.save_state(self.path, s2)
        self.assertEqual(open(self.path).read(), before)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["state.json"])

    def test_concurrent_writers_never_tear(self):
        s = st.new_state(self.flow)
        st.save_state(self.path, s)
        errors = []

        def writer(n):
            local = copy.deepcopy(s)
            for i in range(20):
                local["blocks_this_session"] = n * 100 + i
                st.save_state(self.path, local)

        def reader():
            for _ in range(200):
                try:
                    json.load(open(self.path))
                except ValueError as exc:
                    errors.append(exc)

        ts = [threading.Thread(target=writer, args=(n,)) for n in range(3)] + [threading.Thread(target=reader)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errors, [])

    def test_history_cap(self):
        s = st.new_state(self.flow)
        for i in range(st.HISTORY_CAP + 25):
            st._append(s, {"event": "e", "i": i})
        self.assertEqual(len(s["history"]), st.HISTORY_CAP)
        self.assertEqual(s["history"][-1]["i"], st.HISTORY_CAP + 24)

    def test_corrupt_state_raises(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as fh:
            fh.write("{torn")
        with self.assertRaises(st.StateError):
            st.load_state(self.path, self.flow)

    def test_structurally_invalid_state(self):
        for bad in ([], {"current_phase": "x"},
                    dict(st.new_state(self.flow), restarts=-1),
                    dict(st.new_state(self.flow), loop_iterations=[]),
                    dict(st.new_state(self.flow), phase_status={"scaffold": "weird"})):
            with self.assertRaises(st.StateError):
                st.validate_state(copy.deepcopy(bad), self.flow)

    def test_flow_change_reconciles_and_journals(self):
        s = st.new_state(self.flow)
        s["phase_status"]["scaffold"] = "done"
        s["current_phase"] = "gone"
        s["phase_status"]["gone"] = "active"
        s["flow_version"] = "2"
        out = st.validate_state(s, self.flow)
        self.assertNotIn("gone", out["phase_status"])
        self.assertEqual(out["current_phase"], "implement")
        self.assertEqual(out["flow_version"], "3")
        self.assertEqual(out["history"][-1]["event"], "flow_changed")
        self.assertIn("removed gone", out["history"][-1]["detail"])

    def test_unchanged_flow_adds_no_journal_entry(self):
        s = st.new_state(self.flow)
        self.assertEqual(st.validate_state(s, self.flow)["history"], [])

    def test_set_phase_status(self):
        s = st.new_state(self.flow)
        st.set_phase_status(s, "scaffold", "done")
        self.assertEqual(s["phase_status"]["scaffold"], "done")
        with self.assertRaises(ValueError):
            st.set_phase_status(s, "scaffold", "finished")
        with self.assertRaises(KeyError):
            st.set_phase_status(s, "nope", "done")


if __name__ == "__main__":
    unittest.main()
