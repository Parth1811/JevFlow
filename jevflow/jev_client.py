"""Stdlib client for the Jev System One API (SPEC 3, 7, 10.5).

- Key resolution: the plugin's ``jev_api_key`` option (Claude Code passes it
  to hooks as CLAUDE_PLUGIN_OPTION_JEV_API_KEY), else JEV_API_KEY. Jevflow
  never reads a key from a file. The key is never printed, logged, put in
  an exception message, or shown by repr().
- Retries with exponential backoff on 429 / 529 (and 500 / 502 / 503 / 504),
  honouring a Retry-After header up to a cap.
- A per-client ``max_calls`` budget. Each ``ask()`` is one call; retries of
  the same call do not count again. A call is counted before it is sent, so
  a failed call still spends budget (it cost a request).
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, Optional

DEFAULT_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
DEFAULT_TIMEOUT_S = 15.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_S = 0.5
MAX_BACKOFF_S = 8.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504, 529})
PLUGIN_KEY_ENV = "CLAUDE_PLUGIN_OPTION_JEV_API_KEY"
_ERR_BODY_CHARS = 300


class JevError(Exception):
    """Base class for every Jev client failure. Callers degrade on this."""


class JevKeyError(JevError):
    """No API key could be resolved."""


class JevBudgetError(JevError):
    """max_calls reached; no request was sent."""


class JevTimeoutError(JevError):
    """The request timed out on every attempt."""


class JevConnectionError(JevError):
    """Network failure (DNS, refused, reset) on every attempt."""


class JevHTTPError(JevError):
    """Non-2xx response that was not retried, or retries were exhausted."""

    def __init__(self, status: int, message: str):
        super().__init__(f"Jev HTTP {status}: {message}")
        self.status = status


class JevResponseError(JevError):
    """2xx response whose body is not the expected shape."""


def resolve_key(env: Optional[Mapping[str, str]] = None) -> str:
    """Return the API key or raise JevKeyError. Never returns an empty key."""
    env = os.environ if env is None else env
    for name in (PLUGIN_KEY_ENV, "JEV_API_KEY"):
        key = (env.get(name) or "").strip()
        if key:
            return key
    raise JevKeyError(
        "no Jev API key: set the plugin's jev_api_key option (/plugin > jevflow > Configure) "
        "or JEV_API_KEY"
    )


def _redact(text: str, key: str) -> str:
    if key and key in text:
        text = text.replace(key, "[REDACTED]")
    return text


def _retry_after_s(headers: Any) -> Optional[float]:
    if headers is None:
        return None
    try:
        raw = headers.get("Retry-After")
    except Exception:
        return None
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


class JevClient:
    """Minimal Jev API client. ``opener`` and ``sleep`` are injectable for tests."""

    def __init__(
        self,
        key: Optional[str] = None,
        *,
        url: str = DEFAULT_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE_S,
        max_calls: Optional[int] = None,
        calls_made: int = 0,
        opener: Optional[Callable[..., Any]] = None,
        sleep: Callable[[float], None] = time.sleep,
        env: Optional[Mapping[str, str]] = None,
    ):
        self._key = (key.strip() if key else "") or resolve_key(env)
        if any(ch.isspace() or ord(ch) < 32 for ch in self._key):
            # would corrupt the Authorization header (header injection)
            raise JevKeyError("Jev API key contains whitespace or control characters")
        self.url = url
        self.model = model
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.backoff_base = backoff_base
        self.max_calls = max_calls
        self.calls_made = int(calls_made)
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleep

    def __repr__(self) -> str:  # never expose the key
        return (
            f"JevClient(url={self.url!r}, model={self.model!r}, "
            f"calls_made={self.calls_made}, max_calls={self.max_calls})"
        )

    @property
    def calls_remaining(self) -> Optional[int]:
        if self.max_calls is None:
            return None
        return max(0, self.max_calls - self.calls_made)

    def _backoff(self, attempt: int, retry_after: Optional[float]) -> float:
        if retry_after is not None:
            return min(retry_after, MAX_BACKOFF_S)
        return min(self.backoff_base * (2 ** attempt), MAX_BACKOFF_S)

    def ask(self, state: Any, questions: Mapping[str, Any]) -> dict:
        """Send one System One request. Returns the ``answers`` dict.

        ``state`` may be a string or any JSON-serialisable value (sent as a
        JSON string). Raises a JevError subclass on any failure.
        """
        if not questions:
            raise ValueError("questions must be non-empty")
        if self.max_calls is not None and self.calls_made >= self.max_calls:
            raise JevBudgetError(f"max_jev_calls reached ({self.max_calls})")
        state_str = state if isinstance(state, str) else json.dumps(state, sort_keys=True)
        body = json.dumps(
            {"state": state_str, "model": self.model, "questions": dict(questions)}
        ).encode("utf-8")
        self.calls_made += 1

        last_exc: Optional[JevError] = None
        for attempt in range(self.max_retries + 1):
            req = urllib.request.Request(
                self.url,
                data=body,
                method="POST",
                headers={
                    "Authorization": "Bearer " + self._key,
                    "Content-Type": "application/json",
                    "User-Agent": "jevflow",
                },
            )
            retry_after = None
            try:
                with self._opener(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                return self._parse(raw)
            except urllib.error.HTTPError as exc:
                status = exc.code
                try:
                    detail = exc.read().decode("utf-8", "replace")[:_ERR_BODY_CHARS]
                except Exception:
                    detail = ""
                detail = _redact(detail, self._key) or (exc.reason or "")
                last_exc = JevHTTPError(status, str(detail))
                if status not in RETRY_STATUSES:
                    raise last_exc from None
                retry_after = _retry_after_s(exc.headers)
            except (socket.timeout, TimeoutError) as exc:
                last_exc = JevTimeoutError(f"Jev request timed out after {self.timeout}s")
            except urllib.error.URLError as exc:
                if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                    last_exc = JevTimeoutError(f"Jev request timed out after {self.timeout}s")
                else:
                    last_exc = JevConnectionError(
                        "Jev connection failed: " + _redact(str(exc.reason), self._key)
                    )
            except (ConnectionError, OSError) as exc:
                last_exc = JevConnectionError(
                    "Jev connection failed: " + _redact(type(exc).__name__, self._key)
                )
            if attempt < self.max_retries:
                self._sleep(self._backoff(attempt, retry_after))
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _parse(raw: bytes) -> dict:
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise JevResponseError("Jev response is not JSON") from None
        if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
            raise JevResponseError("Jev response has no 'answers' object")
        answers = data["answers"]
        for name, ans in answers.items():
            if not isinstance(ans, dict) or "type" not in ans:
                raise JevResponseError(f"Jev answer {name!r} is malformed")
            kind = ans["type"]
            if kind == "noul" and not isinstance(ans.get("noul"), (int, float)):
                raise JevResponseError(f"Jev noul answer {name!r} has no value")
            if kind == "choice" and not isinstance(ans.get("probabilities"), dict):
                raise JevResponseError(f"Jev choice answer {name!r} has no probabilities")
            if kind == "score" and not isinstance(ans.get("score"), (int, float)):
                raise JevResponseError(f"Jev score answer {name!r} has no score")
        return answers
