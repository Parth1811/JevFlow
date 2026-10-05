"""0.1.4: machine-wide viewer, judge backends, first-prompt nudge, CLI on PATH."""
import http.client
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from jevflow import auto, cli_install, judge as judge_mod, judges, registry, ui
from jevflow.flow import parse_flow
from jevflow.jev_client import JevKeyError
from jevflow.project import Paths
from jevflow.state import new_state, save_state

FLOW = {"schema_version": 1, "goal": "Build it", "title": "T",
        "phases": [{"id": "a", "name": "A", "done_when": "a done", "check": "true"}]}


class HomeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="jevflow-014-"))
        self.env = mock.patch.dict(os.environ, {"JEVFLOW_HOME": os.path.join(self.tmp, "home"),
                                                "JEVFLOW_SCAN": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def project(self, name, flow_id="20261004-000100-docs", title="T", mtime=None):
        root = os.path.join(self.tmp, name)
        p = Paths(root, flow_id)
        os.makedirs(p.dir)
        doc = dict(FLOW, title=title)
        with open(p.flow, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        save_state(p.state, new_state(parse_flow(doc), now=time.time()))
        if mtime is not None:
            for f in (p.flow, p.state, p.dir):
                os.utime(f, (mtime, mtime))
        return Paths(root), p


class TestRegistry(HomeCase):
    def test_register_list_prune(self):
        a, _ = self.project("a")
        b, _ = self.project("b")
        registry.register(a.root, now=100.0)
        registry.register(b.root, now=200.0)
        self.assertEqual(registry.registered(), [b.root, a.root])
        shutil.rmtree(os.path.join(a.root, ".jevflow"))
        self.assertEqual(registry.registered(), [b.root])
        registry.register(b.root, now=200.0 + registry.REFRESH_S + 1)  # rewrite prunes a
        with open(os.path.join(registry.home(), registry.PROJECTS_FILE)) as fh:
            self.assertEqual(list(json.load(fh)["projects"]), [b.root])

    def test_first_prompt(self):
        self.assertTrue(registry.first_prompt("s1"))
        self.assertFalse(registry.first_prompt("s1"))
        self.assertTrue(registry.first_prompt("s2"))
        self.assertTrue(registry.first_prompt(None))

    def test_scan_finds_nested_and_skips_hidden(self):
        a, _ = self.project("work/x/proj")
        self.project(".hidden/p")
        found = registry.scan([self.tmp], depth=4)
        self.assertIn(a.root, found)
        self.assertFalse(any(".hidden" in f for f in found))

    def test_scan_dirs_env(self):
        self.assertEqual(registry.scan_dirs({"JEVFLOW_SCAN": ""}), [])
        self.assertEqual(registry.scan_dirs({"JEVFLOW_SCAN": f"/x{os.pathsep}/y"}), ["/x", "/y"])


class TestCatalog(HomeCase):
    def test_same_flow_id_in_two_folders(self):
        a, pa = self.project("a", mtime=1000)
        b, pb = self.project("b", title="B flow", mtime=2000)
        registry.register(a.root)
        registry.register(b.root)
        cat = ui.Catalog(None)
        rows = cat.flows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({r["key"] for r in rows}), 2)
        self.assertEqual(rows[0]["project"], "b")  # newest first
        self.assertEqual(cat.resolve(rows[1]["key"]).root, a.root)
        self.assertEqual(cat.default().root, b.root)  # newest active anywhere
        self.assertIsNone(cat.resolve("nope:../../etc"))

    def test_home_project_first_and_bare_ids(self):
        a, pa = self.project("a", mtime=1000)
        b, _ = self.project("b", mtime=2000)
        registry.register(b.root)
        cat = ui.Catalog(a)
        self.assertEqual(cat.default().root, a.root)  # the folder it was started in wins
        self.assertEqual(cat.resolve(pa.flow_id).root, a.root)
        self.assertEqual({r.root for r in cat.roots()}, {a.root, b.root})
        self.assertEqual([r.root for r in ui.Catalog(a, here=True).roots()], [a.root])

    def test_target_for_query_keys(self):
        a, _ = self.project("a")
        b, pb = self.project("b")
        registry.register(b.root)
        t = ui.GlobalTarget(ui.Catalog(a))
        picked = ui._target_for(t, "/api/state?flow=" + ui.flow_key(pb))
        self.assertEqual(picked.paths().root, b.root)
        self.assertIs(ui._target_for(t, "/api/state?flow=../../etc"), t)

    def test_global_export(self):
        a, pa = self.project("a")
        b, pb = self.project("b")
        registry.register(b.root)
        data = ui.export_bundle(ui.Catalog(a), pa)
        self.assertEqual(data["key"], ui.flow_key(pa))
        self.assertEqual(list(data["snapshots"]), [ui.flow_key(pb)])

    def test_server_lists_every_folder(self):
        a, _ = self.project("a")
        b, _ = self.project("b")
        registry.register(b.root)
        stop, ready = threading.Event(), threading.Event()
        th = threading.Thread(target=ui.serve, args=(ui.GlobalTarget(ui.Catalog(None)), 0, io.StringIO()),
                              kwargs={"ready": ready, "stop": stop}, daemon=True)
        registry.register(a.root)
        th.start()
        try:
            self.assertTrue(ready.wait(5))
            c = http.client.HTTPConnection("127.0.0.1", ready.port, timeout=5)
            c.request("GET", "/api/flows", headers={"Host": "localhost"})
            rows = json.loads(c.getresponse().read())["flows"]
            self.assertEqual({r["project"] for r in rows}, {"a", "b"})
            c.request("GET", "/api/state?flow=" + rows[-1]["key"], headers={"Host": "localhost"})
            snap = json.loads(c.getresponse().read())
            self.assertEqual(snap["key"], rows[-1]["key"])
            c.close()
        finally:
            stop.set()
            th.join(5)

    def test_main_works_outside_a_project(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(ui, "serve", return_value=0) as srv:
            self.assertEqual(ui.main(["--project", self.tmp], out, err), 0)
        self.assertIsInstance(srv.call_args[0][0], ui.GlobalTarget)
        self.assertEqual(ui.main(["--project", self.tmp, "--here"], out, err), 3)


class Resp:
    def __init__(self, body):
        self.b = json.dumps(body).encode()

    def read(self):
        return self.b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestJudges(HomeCase):
    def test_selection_order(self):
        self.assertEqual(judges.settings({})["judge"], "jev")
        judges.save_config({"judge": "laya", "url": "http://box:9000"})
        s = judges.settings({})
        self.assertEqual((s["judge"], s["url"]), ("laya", "http://box:9000/v1/systemone"))
        s = judges.settings({"CLAUDE_PLUGIN_OPTION_JUDGE": "openrouter"})
        self.assertEqual((s["judge"], s["model"]), ("openrouter", judges.OPENROUTER_MODEL))
        self.assertEqual(judges.settings({"JEVFLOW_JUDGE": "none", "CLAUDE_PLUGIN_OPTION_JUDGE": "jev"})["judge"], "none")
        self.assertEqual(judges.settings({"JEVFLOW_JUDGE": "bogus"})["judge"], "jev")

    def test_laya_needs_no_key_and_caps_state(self):
        seen = {}

        def opener(req, timeout):
            seen["url"], seen["auth"] = req.full_url, req.get_header("Authorization")
            return Resp({"answers": {"x": {"type": "noul", "noul": 0.7}}})
        c = judges.make_client(5, env={"JEVFLOW_JUDGE": "laya"})
        c._opener = opener
        self.assertEqual(c.ask("s", {"x": {"type": "noul", "instructions": "?"}})["x"]["noul"], 0.7)
        self.assertEqual(seen["url"], judges.LAYA_URL)
        self.assertIsNone(seen["auth"])
        self.assertEqual(c.state_budget, judges.LAYA_STATE_CHARS)

    def test_openrouter_requires_key(self):
        with self.assertRaises(JevKeyError):
            judges.make_client(5, env={"JEVFLOW_JUDGE": "openrouter"})
        with self.assertRaises(JevKeyError):
            judges.make_client(5, env={"JEVFLOW_JUDGE": "openai"})

    def test_llm_round_trip(self):
        sent = {}

        def opener(req, timeout):
            sent["body"] = json.loads(req.data)
            sent["auth"] = req.get_header("Authorization")
            content = json.dumps({"stuck": {"noul": 1.4}, "phase": {"probabilities": {"a": 3, "b": 1, "zz": 9}},
                                  "progress": {"score": 9}})
            return Resp({"choices": [{"message": {"content": "```json\n" + content + "\n```"}}]})
        c = judges.make_client(5, env={"JEVFLOW_JUDGE": "openrouter", "OPENROUTER_API_KEY": "sk-or-test"})
        c._opener = opener
        qs = {"stuck": {"type": "noul", "instructions": "stuck?"},
              "phase": {"type": "choice", "instructions": "which", "criteria": {"a": "A", "b": "B"}},
              "progress": {"type": "score", "instructions": "how far", "criteria": ["0", "1", "2"]},
              "missing": {"type": "noul", "instructions": "?"}}
        ans = c.ask({"goal": "g"}, qs)
        self.assertEqual(sent["auth"], "Bearer sk-or-test")
        self.assertEqual(sent["body"]["model"], judges.OPENROUTER_MODEL)
        self.assertEqual(ans["stuck"], {"type": "noul", "noul": 1.0})
        self.assertEqual(ans["phase"]["choice"], "a")
        self.assertAlmostEqual(sum(ans["phase"]["probabilities"].values()), 1.0)
        self.assertNotIn("zz", ans["phase"]["probabilities"])
        self.assertEqual(ans["progress"]["score"], 2.0)
        self.assertEqual(ans["missing"]["noul"], 0.5)  # unanswered = maximally unsure

    def test_judge_uses_client_state_budget(self):
        f = parse_flow(FLOW)
        seen = {}

        class C:
            calls_made = 0
            state_budget = 300

            def ask(self, state, qs):
                seen.setdefault("len", len(state))
                return {"current_phase": {"probabilities": {"a": 1.0}}, "next_action": {"probabilities": {"continue_phase": 1}},
                        "stuck": {"noul": 0}, "off_goal": {"noul": 0}, "claims_done": {"noul": 0},
                        "verify": {"noul": 0.1}}
        judge_mod.judge(C(), f, {"current_phase": "a"}, checks={}, last_message="x" * 5000)
        self.assertLessEqual(seen["len"], 400)


class TestFirstPromptNudge(unittest.TestCase):
    def test_lower_bar_on_first_prompt(self):
        self.assertIsNone(auto.prompt_nudge("add a dark mode toggle"))
        self.assertIsNotNone(auto.prompt_nudge("add a dark mode toggle", first=True))
        self.assertIsNone(auto.prompt_nudge("what is this repo?", first=True))


class TestJoinHint(HomeCase):
    TASK = "Add a --verbose flag to the CLI, cover it with tests and document it in the README"

    def setUp(self):
        super().setUp()
        from jevflow import hooks, project
        self.hooks, self.proj = hooks, project
        self.root, self.pa = self.project("work")
        project.bind_session(self.root, "lead", self.pa.flow_id)

    def hook(self, event, sid, env=None, **payload):
        payload = {"cwd": self.root.root, "session_id": sid, "hook_event_name": event, **payload}
        return self.hooks.handle(event, payload, env=env or {}, now=time.time(), client_factory=lambda n: None)

    def ctx(self, out):
        return (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""

    def test_second_session_is_told_to_join(self):
        start = self.ctx(self.hook("SessionStart", "cowork-1", source="startup"))
        self.assertIn(self.pa.flow_id, start)
        self.assertIn("join", start)
        first = self.ctx(self.hook("UserPromptSubmit", "cowork-1", prompt=self.TASK))
        self.assertIn(self.pa.flow_id, first)
        self.assertIn("start a tracked flow", first)  # unrelated work can still get its own flow

    def test_bound_session_gets_no_join_hint(self):
        self.assertNotIn("Other agents", self.ctx(self.hook("SessionStart", "lead", source="startup")))

    def test_auto_mode_does_not_duplicate(self):
        out = self.hook("UserPromptSubmit", "cowork-2", env={"JEVFLOW_AUTO": "1"}, prompt=self.TASK)
        self.assertIn(self.pa.flow_id, self.ctx(out))
        self.assertEqual(len(self.proj.list_flows(self.root)), 1)
        self.hook("UserPromptSubmit", "cowork-3", env={"JEVFLOW_AUTO": "1"}, prompt="#jev " + self.TASK)
        self.assertEqual(len(self.proj.list_flows(self.root)), 2)

    def test_finished_or_old_flows_are_not_offered(self):
        st = json.load(open(self.pa.state))
        st["done"] = True
        json.dump(st, open(self.pa.state, "w"))
        self.assertIsNone(auto.join_hint(self.root, "x", time.time()))


class TestCliSetup(unittest.TestCase):
    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp(prefix="jevflow-cli-"))
        self.p = mock.patch.dict(os.environ, {"HOME": self.home, "SHELL": "/bin/zsh"})
        self.p.start()
        self.c = mock.patch.object(cli_install, "CANDIDATES", ("~/.local/bin", "~/bin"))
        self.c.start()

    def tearDown(self):
        self.c.stop()
        self.p.stop()
        shutil.rmtree(self.home, ignore_errors=True)

    def test_uses_a_folder_already_on_path(self):
        os.makedirs(os.path.join(self.home, "bin"))
        status, link, changed = cli_install.setup(path=os.path.join(self.home, "bin") + ":/usr/bin")
        self.assertEqual((status, link, changed), ("created", os.path.join(self.home, "bin", "jevflow"), []))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".zshrc")))

    def test_adds_rc_block_once(self):
        status, link, changed = cli_install.setup(path="/usr/bin")
        self.assertEqual(status, "created")
        self.assertEqual(changed, [os.path.join(self.home, ".zshrc")])
        with open(changed[0]) as fh:
            text = fh.read()
        self.assertIn('export PATH="$HOME/.local/bin:$PATH"', text)
        self.assertEqual(cli_install.setup(path="/usr/bin")[2], [])  # fast path: link exists
        self.assertEqual(cli_install.add_to_rc("~/.local/bin"), [])   # marker already there

    def test_no_rc_env(self):
        with mock.patch.dict(os.environ, {cli_install.NO_RC_ENV: "1"}):
            self.assertEqual(cli_install.setup(path="/usr/bin")[2], [])
        self.assertFalse(os.path.exists(os.path.join(self.home, ".zshrc")))


if __name__ == "__main__":
    unittest.main()
