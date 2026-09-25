"""jevflow ui: snapshot, export escaping, Host guard, routes, SSE, launch.json."""
import http.client
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest

from jevflow import ui
from jevflow.project import Paths

FLOW = {
    "schema_version": 1, "goal": "Build a thing", "mode": "enforce",
    "phases": [
        {"id": "a", "name": "A", "done_when": "a done", "check": "true"},
        {"id": "b", "name": "B", "depends_on": ["a"], "done_when": "b done", "on_fail": "fix",
         "loop": {"max_iterations": 2, "until": "true"}},
        {"id": "fix", "name": "Fix", "depends_on": ["a"], "done_when": "fixed"},
    ],
}


class UICase(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="jevflow-ui-"))
        os.makedirs(os.path.join(self.dir, ".jevflow"))
        self.write(".jevflow/flow.json", FLOW)
        self.paths = Paths(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, rel, obj):
        with open(os.path.join(self.dir, rel), "w", encoding="utf-8") as fh:
            json.dump(obj, fh)

    def state(self, **kw):
        s = {"phase_status": {"a": "done", "b": "active", "fix": "pending"}, "current_phase": "b",
             "history": [{"event": "stop", "decision": "BLOCK", "condition": "loop_continue",
                          "phase": "b", "ts": 1.0, "reason": "</script><img src=x onerror=alert(1)>"}],
             "started_at": 0.0, "done": False}
        s.update(kw)
        self.write(".jevflow/state.json", s)


class TestSnapshot(UICase):
    def test_not_started(self):
        d = ui.snapshot(self.paths)
        self.assertEqual(d["flow"]["goal"], "Build a thing")
        self.assertIsNone(d["state"])
        self.assertIsNone(d["error"])

    def test_history_truncated(self):
        self.state(history=[{"event": "pre_tool", "ts": i} for i in range(ui.MAX_HISTORY + 7)])
        d = ui.snapshot(self.paths)
        self.assertEqual(len(d["state"]["history"]), ui.MAX_HISTORY)
        self.assertEqual(d["state"]["history_truncated"], 7)

    def test_bad_flow_reports_error(self):
        self.write(".jevflow/flow.json", {"phases": "nope"})
        d = ui.snapshot(self.paths)
        self.assertIn("flow.json", d["error"])
        self.assertIsNone(d["flow"])

    def test_export_cannot_break_out_of_script(self):
        self.state()
        html = ui.render_export(ui.snapshot(self.paths))
        self.assertNotIn("<!--JEVFLOW_DATA-->", html)
        start = html.index("window.JEVFLOW_DATA=")
        end = html.index("</script>", start)
        blob = html[start + len("window.JEVFLOW_DATA="):end].rstrip(";")
        self.assertNotIn("<", blob)
        self.assertIn("onerror", json.loads(blob)["state"]["history"][0]["reason"])


class TestHost(unittest.TestCase):
    def test_hosts(self):
        for ok in ("localhost", "localhost:7788", "127.0.0.1:1", "[::1]:7788", "app.localhost:3000"):
            self.assertTrue(ui.host_allowed(ok), ok)
        for bad in (None, "", "evil.com", "evil.com:7788", "127.0.0.1.evil.com", "localhost.evil.com:1"):
            self.assertFalse(ui.host_allowed(bad), bad)


class TestServer(UICase):
    def setUp(self):
        super().setUp()
        self.state()
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.t = threading.Thread(target=ui.serve, args=(self.paths, 0, io.StringIO()),
                                  kwargs={"ready": self.ready, "stop": self.stop}, daemon=True)
        self.t.start()
        self.assertTrue(self.ready.wait(5))
        self.port = self.ready.port

    def tearDown(self):
        self.stop.set()
        self.t.join(5)
        super().tearDown()

    def get(self, path, host=None, method="GET"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, headers={"Host": host or f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, r.getheader("Content-Type"), body

    def test_index_and_state(self):
        code, ctype, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", ctype)
        self.assertIn(b"Jevflow", body)
        code, _, body = self.get("/api/state")
        self.assertEqual(json.loads(body)["state"]["current_phase"], "b")

    def test_foreign_host_rejected(self):
        self.assertEqual(self.get("/api/state", host="attacker.example")[0], 403)

    def test_read_only(self):
        self.assertEqual(self.get("/api/state", method="POST")[0], 405)
        self.assertEqual(self.get("/nope")[0], 404)

    def test_no_cors(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/api/state", headers={"Host": "localhost", "Origin": "https://evil.example"})
        r = c.getresponse()
        r.read()
        self.assertIsNone(r.getheader("Access-Control-Allow-Origin"))
        c.close()

    def test_sse_pushes_on_change(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/events", headers={"Host": "localhost"})
        r = c.getresponse()
        self.assertEqual(r.getheader("Content-Type"), "text/event-stream")

        def read_event():
            buf = b""
            while b"\n\n" not in buf:
                buf += r.fp.readline()
            return json.loads(buf.split(b"data: ", 1)[1].split(b"\n", 1)[0])

        self.assertEqual(read_event()["state"]["current_phase"], "b")
        time.sleep(0.05)
        self.state(current_phase="a", done=True)
        self.assertTrue(read_event()["state"]["done"])
        c.close()


class TestLaunchJson(UICase):
    def test_writes_and_replaces_entry(self):
        os.makedirs(os.path.join(self.dir, ".claude"))
        self.write(".claude/launch.json", {"version": "0.0.1", "configurations": [
            {"name": "web", "runtimeExecutable": "npm", "port": 3000},
            {"name": "jevflow", "port": 1}]})
        self.assertEqual(ui.write_launch_json(self.paths, 7788, io.StringIO()), 0)
        with open(os.path.join(self.dir, ".claude", "launch.json"), encoding="utf-8") as fh:
            confs = json.load(fh)["configurations"]
        self.assertEqual([c["name"] for c in confs], ["web", "jevflow"])
        j = confs[1]
        self.assertTrue(j["runtimeExecutable"].endswith(os.path.join("hooks", "jevflow")))
        self.assertEqual(j["runtimeArgs"], ["ui", "--project", "${workspaceFolder}"])
        self.assertEqual((j["port"], j["autoPort"]), (7788, True))

    def test_commented_file_left_alone(self):
        os.makedirs(os.path.join(self.dir, ".claude"))
        path = os.path.join(self.dir, ".claude", "launch.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{ // mine\n "configurations": [] }\n')
        out = io.StringIO()
        self.assertEqual(ui.write_launch_json(self.paths, 7788, out), 3)
        self.assertIn("by hand", out.getvalue())
        with open(path, encoding="utf-8") as fh:
            self.assertIn("// mine", fh.read())


if __name__ == "__main__":
    unittest.main()
