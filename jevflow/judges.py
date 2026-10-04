"""Pick the judge behind every Jevflow decision.

Jevflow asks typed questions (``noul`` / ``choice`` / ``score``) and expects
calibrated probabilities back, the System One contract Jev defined. Three
kinds of backend speak it:

* ``jev`` (default): TypeSafe's hosted Jev API.
* ``laya``: Laya, the Apache-2.0 open-weights System One model, served by
  ``laya-serve`` on the same ``POST /v1/systemone`` shape. Self-hosted, no key
  needed unless you set ``LAYA_API_KEY`` on the server.
* ``openrouter`` / ``openai``: any chat model behind an OpenAI-compatible
  ``/chat/completions`` endpoint (OpenRouter, Ollama, LM Studio, vLLM, ...).
  The model is asked for the same typed answers as JSON. Its probabilities
  are self-reported, not calibrated, so treat them as rougher than Jev's;
  the deterministic checks still decide what they always decide.

Selection, first match wins:
``$JEVFLOW_JUDGE`` > the plugin's ``judge`` option > ``config.json`` in the
Jevflow home (``jevflow judge set ...``) > ``jev``. ``none`` turns the judge
off (checks only). Keys come only from the environment or the plugin's
secure ``judge_api_key`` option, never from a file.
"""

import json
import os
import re
from typing import Any, Dict, Mapping, Optional

from . import registry
from .jev_client import JevClient, JevError, JevKeyError, JevResponseError

PROVIDERS = ("jev", "laya", "openrouter", "openai", "none")
CONFIG_FILE = "config.json"
OPT = "CLAUDE_PLUGIN_OPTION_"
LAYA_URL = "http://127.0.0.1:8000/v1/systemone"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "google/gemini-2.5-flash"
LAYA_STATE_CHARS = 1800      # Laya reads ~512 tokens per question (English checkpoint)
LLM_TIMEOUT_S = 45.0
KEY_ENVS = ("JEVFLOW_JUDGE_API_KEY", OPT + "JUDGE_API_KEY", "OPENROUTER_API_KEY", "LAYA_API_KEY")


def load_config() -> Dict[str, Any]:
    try:
        with open(os.path.join(registry.home(), CONFIG_FILE), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg: Mapping[str, Any]) -> str:
    d = registry.home()
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, CONFIG_FILE)
    clean = {k: v for k, v in cfg.items() if k in ("judge", "url", "model") and v}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(clean, fh, indent=1)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def _pick(env: Mapping[str, str], cfg: Mapping[str, Any], name: str, env_name: str) -> str:
    return str(env.get(env_name) or env.get(OPT + name.upper()) or cfg.get(name) or "").strip()


def settings(env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The effective judge, url and model (no key). Unknown names fall back to jev."""
    env = os.environ if env is None else env
    cfg = load_config()
    judge = (_pick(env, cfg, "judge", "JEVFLOW_JUDGE") or "jev").lower()
    if judge in ("off", "checks", "checks-only"):
        judge = "none"
    if judge not in PROVIDERS:
        judge = "jev"
    url = _pick(env, cfg, "url", "JEVFLOW_JUDGE_URL")
    model = str(env.get("JEVFLOW_JUDGE_MODEL") or env.get(OPT + "JUDGE_MODEL") or cfg.get("model") or "").strip()
    if judge == "laya":
        url = _systemone_url(url or LAYA_URL)
        model = model or "laya"
    elif judge == "openrouter":
        url = _chat_url(url or OPENROUTER_URL)
        model = model or OPENROUTER_MODEL
    elif judge == "openai":
        url = _chat_url(url) if url else ""
    return {"judge": judge, "url": url, "model": model}


def _systemone_url(url: str) -> str:
    u = url.rstrip("/")
    return u if u.endswith("/systemone") else u + ("/systemone" if u.endswith("/v1") else "/v1/systemone")


def _chat_url(url: str) -> str:
    u = url.rstrip("/")
    return u if u.endswith("/chat/completions") else u + ("/chat/completions" if u.endswith("/v1") else "/v1/chat/completions")


def _key(env: Mapping[str, str], judge: str) -> str:
    order = {"laya": ("JEVFLOW_JUDGE_API_KEY", OPT + "JUDGE_API_KEY", "LAYA_API_KEY"),
             "openrouter": ("JEVFLOW_JUDGE_API_KEY", OPT + "JUDGE_API_KEY", "OPENROUTER_API_KEY"),
             "openai": ("JEVFLOW_JUDGE_API_KEY", OPT + "JUDGE_API_KEY", "OPENAI_API_KEY")}[judge]
    for name in order:
        v = (env.get(name) or "").strip()
        if v:
            return v
    return ""


def make_client(max_calls: Optional[int] = None, env: Optional[Mapping[str, str]] = None) -> Optional[Any]:
    """A client with ``ask(state, questions) -> answers`` and ``calls_made``,
    or None when the judge is off. Raises JevError (usually JevKeyError) when
    the chosen judge is not usable; callers then run checks-only."""
    env = os.environ if env is None else env
    s = settings(env)
    j = s["judge"]
    if j == "none":
        return None
    if j == "jev":
        return JevClient(max_calls=max_calls, env=env)
    if j == "laya":
        return JevClient(_key(env, j) or None, url=s["url"], model=s["model"], max_calls=max_calls,
                         require_key=False, state_budget=LAYA_STATE_CHARS, label="Laya", env=env)
    key = _key(env, j)
    if j == "openrouter" and not key:
        raise JevKeyError("no OpenRouter key: set the plugin's judge_api_key option or OPENROUTER_API_KEY")
    if j == "openai" and (not s["url"] or not s["model"]):
        raise JevKeyError("judge 'openai' needs a url and a model: jevflow judge set openai --url URL --model NAME")
    return LLMJudgeClient(key or None, url=s["url"], model=s["model"], max_calls=max_calls,
                          openrouter=(j == "openrouter"))


def describe(env: Optional[Mapping[str, str]] = None) -> str:
    s = settings(env)
    env = os.environ if env is None else env
    if s["judge"] == "none":
        return "judge: none (checks only)"
    if s["judge"] == "jev":
        try:
            JevClient(env=env)
            ok = "key set"
        except JevError:
            ok = "NO KEY (checks only until you set jev_api_key or JEV_API_KEY)"
        return f"judge: jev (TypeSafe API), {ok}"
    key = "key set" if _key(env, s["judge"]) else "no key"
    return f"judge: {s['judge']}, url {s['url'] or '(not set)'}, model {s['model'] or '(not set)'}, {key}"


# --------------------------------------------------------- LLM-as-judge

SYSTEM = (
    "You are a strict, well-calibrated judgment model. You read STATE (JSON describing an AI "
    "coding agent's task and progress) and answer every question in QUESTIONS from the evidence "
    "in STATE alone. Reply with one JSON object and nothing else. For each question name, give:\n"
    '- type "noul": {"noul": P} where P in [0,1] is the probability the statement is true;\n'
    '- type "choice": {"probabilities": {OPTION: P, ...}} over exactly the listed option keys, '
    "summing to 1;\n"
    '- type "score": {"score": S} where S is a number from 0 (first level) to N-1 (last level).\n'
    "Use the full probability range honestly: near 0 or 1 only when STATE makes it clear, near 0.5 "
    "when it does not. Claims in the agent's own message are not evidence that work is done; "
    "failing checks are strong evidence that it is not.")


def _question_text(questions: Mapping[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name, q in questions.items():
        item: Dict[str, Any] = {"type": q.get("type"), "question": q.get("instructions", "")}
        if q.get("type") == "choice":
            item["options"] = dict(q.get("criteria") or {})
        elif q.get("type") == "score":
            item["levels"] = list(q.get("criteria") or [])
        out[name] = item
    return out


def _unit(v: Any, default: float = 0.5) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):
        return default
    return max(0.0, min(1.0, f))


def normalize(raw: Mapping[str, Any], questions: Mapping[str, Any]) -> Dict[str, Any]:
    """Coerce a model's JSON into the System One answers shape. Every question
    gets an answer; missing or garbled ones become maximally uncertain."""
    answers: Dict[str, Any] = {}
    for name, q in questions.items():
        a = raw.get(name)
        a = a if isinstance(a, Mapping) else ({"noul": a} if isinstance(a, (int, float)) else {})
        kind = q.get("type")
        if kind == "noul":
            answers[name] = {"type": "noul", "noul": _unit(a.get("noul", a.get("p", a.get("probability"))))}
        elif kind == "choice":
            opts = list((q.get("criteria") or {}).keys())
            given = a.get("probabilities") if isinstance(a.get("probabilities"), Mapping) else {}
            probs = {o: _unit(given.get(o), 0.0) for o in opts}
            if not any(probs.values()) and a.get("choice") in probs:
                probs[a["choice"]] = 1.0
            total = sum(probs.values())
            probs = {o: (p / total if total else 1.0 / max(1, len(opts))) for o, p in probs.items()}
            best = max(probs, key=probs.get) if probs else "unclear"
            answers[name] = {"type": "choice", "choice": best, "confidence": probs.get(best, 0.0),
                             "probabilities": probs}
        elif kind == "score":
            top = max(0, len(q.get("criteria") or []) - 1)
            try:
                sc = float(a.get("score"))
                sc = sc if sc == sc else top / 2.0
            except (TypeError, ValueError):
                sc = top / 2.0
            answers[name] = {"type": "score", "score": max(0.0, min(float(top), sc))}
    return answers


def _content(raw: bytes) -> Dict[str, Any]:
    try:
        data = json.loads(raw)
        text = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise JevResponseError("judge response has no message content") from None
    if isinstance(text, list):  # some providers return content parts
        text = "".join(str(p.get("text", "")) for p in text if isinstance(p, dict))
    text = str(text or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    try:
        obj = json.loads(m.group(0) if m else text)
    except ValueError:
        raise JevResponseError("judge did not return JSON") from None
    if not isinstance(obj, dict):
        raise JevResponseError("judge JSON is not an object")
    return obj


class LLMJudgeClient(JevClient):
    """OpenAI-compatible chat model asked for System One shaped answers.
    Reuses JevClient's retries, call budget, timeouts and key handling."""

    def __init__(self, key: Optional[str], *, url: str, model: str, openrouter: bool = False,
                 max_calls: Optional[int] = None, timeout: float = LLM_TIMEOUT_S, **kw: Any):
        super().__init__(key, url=url, model=model, max_calls=max_calls, timeout=timeout,
                         require_key=False, label="judge", **kw)
        self.openrouter = openrouter

    def _body(self, state_str: str, questions: Mapping[str, Any]) -> dict:
        user = ("STATE:\n" + state_str + "\n\nQUESTIONS:\n"
                + json.dumps(_question_text(questions), ensure_ascii=False))
        return {"model": self.model, "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]}

    def _parse(self, raw: bytes, questions: Optional[Mapping[str, Any]] = None) -> dict:
        return normalize(_content(raw), questions or {})
