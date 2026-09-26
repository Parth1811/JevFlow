import io
from unittest import mock
import json
import os
import socket
import tempfile
import unittest
import urllib.error
from email.message import Message

from jevflow import jev_client as jc

FAKE_KEY = "sk-test-FAKEKEY-1234567890"

OK_BODY = {
    "model": "jev-1.13.0",
    "answers": {
        "stuck": {"type": "noul", "noul": 0.12},
        "current_phase": {"type": "choice", "choice": "implement", "confidence": 0.9,
                          "probabilities": {"implement": 0.95, "unclear": 0.05}},
        "progress": {"type": "score", "score": 2.1, "confidence": 0.7},
    },
}


class FakeResp:
    def __init__(self, body):
        self._b = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, body=b"", retry_after=None):
    hdrs = Message()
    if retry_after is not None:
        hdrs["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError("https://x", code, "err", hdrs, io.BytesIO(body))


class Opener:
    """Scripted urlopen: each item is a response body or an exception to raise."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append((req, timeout))
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return FakeResp(item)


def client(opener, **kw):
    sleeps = []
    c = jc.JevClient(FAKE_KEY, opener=opener, sleep=sleeps.append, **kw)
    return c, sleeps


class KeyResolution(unittest.TestCase):
    def test_plugin_option_wins(self):
        env = {"CLAUDE_PLUGIN_OPTION_JEV_API_KEY": " from-option ", "JEV_API_KEY": "from-env"}
        self.assertEqual(jc.resolve_key(env), "from-option")

    def test_env_var(self):
        self.assertEqual(jc.resolve_key({"JEV_API_KEY": " from-env "}), "from-env")

    def test_blank_option_falls_back(self):
        env = {"CLAUDE_PLUGIN_OPTION_JEV_API_KEY": "  ", "JEV_API_KEY": "from-env"}
        self.assertEqual(jc.resolve_key(env), "from-env")

    def test_missing_everywhere(self):
        with self.assertRaises(jc.JevKeyError):
            jc.resolve_key({})

    def test_key_file_is_not_read(self):
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, ".config", "jevflow"))
            p = os.path.join(home, ".config", "jevflow", "api_key")
            with open(p, "w") as f:
                f.write("from-file")
            with mock.patch.dict(os.environ, {"HOME": home}):
                with self.assertRaises(jc.JevKeyError):
                    jc.resolve_key({"JEVFLOW_KEY_FILE": p})


class Ask(unittest.TestCase):
    def test_success_request_shape(self):
        op = Opener(OK_BODY)
        c, sleeps = client(op, timeout=7)
        ans = c.ask({"goal": "g"}, {"stuck": {"type": "noul", "instructions": "x"}})
        self.assertEqual(ans["stuck"]["noul"], 0.12)
        req, timeout = op.requests[0]
        self.assertEqual(timeout, 7)
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer " + FAKE_KEY)
        body = json.loads(req.data)
        self.assertEqual(body["model"], "jev-latest")
        self.assertIsInstance(body["state"], str)  # state is sent as a JSON string
        self.assertEqual(json.loads(body["state"]), {"goal": "g"})
        self.assertEqual(c.calls_made, 1)
        self.assertEqual(sleeps, [])

    def test_retry_on_429_then_success_counts_one_call(self):
        op = Opener(http_error(429), http_error(529), OK_BODY)
        c, sleeps = client(op, backoff_base=0.5)
        c.ask("s", {"q": {"type": "noul", "instructions": "x"}})
        self.assertEqual(len(op.requests), 3)
        self.assertEqual(sleeps, [0.5, 1.0])  # exponential
        self.assertEqual(c.calls_made, 1)

    def test_retry_after_header_is_honoured_and_capped(self):
        op = Opener(http_error(429, retry_after=2), http_error(429, retry_after=999), OK_BODY)
        c, sleeps = client(op)
        c.ask("s", {"q": {"type": "noul", "instructions": "x"}})
        self.assertEqual(sleeps, [2.0, jc.MAX_BACKOFF_S])

    def test_retries_exhausted_raises_http_error(self):
        op = Opener(*[http_error(529)] * 4)
        c, sleeps = client(op, max_retries=3)
        with self.assertRaises(jc.JevHTTPError) as cm:
            c.ask("s", {"q": {"type": "noul", "instructions": "x"}})
        self.assertEqual(cm.exception.status, 529)
        self.assertEqual(len(op.requests), 4)
        self.assertEqual(len(sleeps), 3)

    def test_400_not_retried(self):
        op = Opener(http_error(400, b'{"error":"bad question"}'))
        c, sleeps = client(op)
        with self.assertRaises(jc.JevHTTPError) as cm:
            c.ask("s", {"q": {"type": "noul", "instructions": "x"}})
        self.assertEqual(cm.exception.status, 400)
        self.assertIn("bad question", str(cm.exception))
        self.assertEqual(len(op.requests), 1)
        self.assertEqual(sleeps, [])

    def test_401_error_body_echoing_key_is_redacted(self):
        op = Opener(http_error(401, ("invalid key " + FAKE_KEY).encode()))
        c, _ = client(op)
        with self.assertRaises(jc.JevHTTPError) as cm:
            c.ask("s", {"q": {"type": "noul", "instructions": "x"}})
        self.assertNotIn(FAKE_KEY, str(cm.exception))
        self.assertIn("[REDACTED]", str(cm.exception))

    def test_timeout_typed(self):
        op = Opener(socket.timeout(), urllib.error.URLError(socket.timeout()))
        c, sleeps = client(op, max_retries=1)
        with self.assertRaises(jc.JevTimeoutError):
            c.ask("s", {"q": {"type": "noul", "instructions": "x"}})
        self.assertEqual(len(sleeps), 1)

    def test_connection_error_typed(self):
        op = Opener(urllib.error.URLError("Name or service not known"))
        c, _ = client(op, max_retries=0)
        with self.assertRaises(jc.JevConnectionError):
            c.ask("s", {"q": {"type": "noul", "instructions": "x"}})

    def test_connection_reset_typed(self):
        op = Opener(ConnectionResetError(), OK_BODY)
        c, _ = client(op, max_retries=1)
        self.assertIn("stuck", c.ask("s", {"q": {"type": "noul", "instructions": "x"}}))

    def test_bad_json_and_bad_shape(self):
        for body in (b"not json", b"[]", b'{"answers": {"x": {"type": "noul"}}}',
                     b'{"answers": {"x": {"type": "choice"}}}', b'{"no": 1}'):
            c, _ = client(Opener(body))
            with self.assertRaises(jc.JevResponseError, msg=body):
                c.ask("s", {"q": {"type": "noul", "instructions": "x"}})

    def test_budget(self):
        op = Opener(OK_BODY, OK_BODY)
        c, _ = client(op, max_calls=2, calls_made=1)
        self.assertEqual(c.calls_remaining, 1)
        c.ask("s", {"q": {"type": "noul", "instructions": "x"}})
        self.assertEqual(c.calls_remaining, 0)
        with self.assertRaises(jc.JevBudgetError):
            c.ask("s", {"q": {"type": "noul", "instructions": "x"}})
        self.assertEqual(len(op.requests), 1)  # no request sent once over budget

    def test_failed_call_still_spends_budget(self):
        c, _ = client(Opener(http_error(400)), max_calls=1)
        with self.assertRaises(jc.JevHTTPError):
            c.ask("s", {"q": {"type": "noul", "instructions": "x"}})
        with self.assertRaises(jc.JevBudgetError):
            c.ask("s", {"q": {"type": "noul", "instructions": "x"}})

    def test_key_with_control_chars_rejected(self):
        for bad in ("abc\r\nX-Evil: 1", "a b"):
            with self.assertRaises(jc.JevKeyError):
                jc.JevClient(bad, opener=Opener())

    def test_empty_questions_rejected(self):
        c, _ = client(Opener())
        with self.assertRaises(ValueError):
            c.ask("s", {})

    def test_all_errors_are_jev_errors(self):
        for cls in (jc.JevKeyError, jc.JevBudgetError, jc.JevTimeoutError,
                    jc.JevConnectionError, jc.JevResponseError):
            self.assertTrue(issubclass(cls, jc.JevError))
        self.assertTrue(issubclass(jc.JevHTTPError, jc.JevError))

    def test_repr_hides_key(self):
        c, _ = client(Opener())
        self.assertNotIn(FAKE_KEY, repr(c))


if __name__ == "__main__":
    unittest.main()
