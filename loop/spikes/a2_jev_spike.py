"""A2 spike: run the SPEC 3 judge question set against 4 synthetic transcripts.

Stdlib only. Key from JEV_API_KEY. The key is never printed.
Usage: JEV_API_KEY=... /usr/bin/python3.11 loop/spikes/a2_jev_spike.py [wording]
"""
import json, os, sys, time, urllib.request

URL = "https://api.typesafe.ai/v1/systemone"

def key():
    k = os.environ.get("JEV_API_KEY")
    if not k:
        sys.exit("no key")
    return k

def call(state, questions):
    body = json.dumps({"state": state, "model": "jev-latest", "questions": questions}).encode()
    req = urllib.request.Request(URL, data=body, method="POST", headers={
        "Authorization": "Bearer " + key(), "Content-Type": "application/json"})
    t = time.time()
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.loads(r.read())
    out["_ms"] = int((time.time() - t) * 1000)
    return out

PHASES = [
    {"id": "scaffold", "name": "Project scaffold", "done_when": "package layout and entry point exist"},
    {"id": "implement", "name": "Implement commands", "done_when": "add, list and done commands work"},
    {"id": "test", "name": "Tests pass", "done_when": "a test suite exists and passes"},
]
GOAL = "Build a CLI todo app in Python with add/list/done commands and tests"

CASES = {
    "mid_phase": dict(current="implement", checks={"scaffold": "pass", "test": "fail: no tests collected"},
        last="I added the `add` command in todo/cli.py and it writes to todos.json. Next I will implement `list`.",
        changes="todo/cli.py +42", history=["ADVANCE scaffold->implement"]),
    "phase_complete": dict(current="implement", checks={"scaffold": "pass", "test": "fail: no tests collected"},
        last="add, list and done are all implemented. I ran `todo add milk`, `todo list` shows it, `todo done 1` marks it complete. Moving on to writing tests.",
        changes="todo/cli.py +118, todo/store.py +40", history=["ADVANCE scaffold->implement", "BLOCK implement: list missing"]),
    "stuck_looping": dict(current="test", checks={"scaffold": "pass", "test": "fail: ImportError: cannot import name 'store' from 'todo'"},
        last="The import still fails. Let me try changing the import in tests/test_cli.py back to `from todo import store` again. That did not work either, retrying the same change.",
        changes="tests/test_cli.py +1 -1", history=["BLOCK test: ImportError", "BLOCK test: ImportError", "BLOCK test: ImportError"]),
    "premature_done": dict(current="test", checks={"scaffold": "pass", "test": "fail: 2 failed, 3 passed"},
        last="All done! The todo app is complete with add, list and done commands and a full test suite. The goal is achieved.",
        changes="tests/test_cli.py +60", history=["ADVANCE implement->test"]),
}

WORDING = {
    "v1": dict(
        phase="Which phase of the plan is the agent currently working on",
        done="The agent has fully completed the '{name}' phase: {done_when}",
        next="What should happen next in this session",
        stuck="The agent is repeating itself, making no progress, or going in circles",
        off="The agent is working on something that does not serve the goal",
        progress="How far the work has progressed toward the whole goal",
        claims="The latest message claims the work or the goal is complete"),
    "v2": dict(
        phase="Based on the latest message and check results, which plan phase is the agent actively working on right now",
        done="Evidence in the state shows the '{name}' phase is finished: {done_when}. A claim by the agent alone is not evidence",
        next="Given the plan, check results and latest message, what is the correct next step",
        stuck="The recent history and latest message show the agent retrying the same failing approach without new information",
        off="The latest message describes work unrelated to the goal and to the current phase",
        progress="How far the work has progressed toward the whole goal, judged by check results",
        claims="The agent's latest message asserts that the task or the whole goal is finished"),
}

WORDING["v3"] = dict(WORDING["v1"], stuck=WORDING["v2"]["stuck"], off=WORDING["v2"]["off"])


def state_for(c):
    return json.dumps({"goal": GOAL, "phases": PHASES, "current_phase": c["current"],
        "check_results": c["checks"], "last_assistant_message": c["last"],
        "change_summary": c["changes"], "recent_history": c["history"]}, indent=1)

def questions(w, cur):
    ids = [p["id"] for p in PHASES]
    idx = ids.index(cur)
    q = {"current_phase": {"type": "choice", "instructions": w["phase"],
            "criteria": {**{p["id"]: p["name"] + ": " + p["done_when"] for p in PHASES},
                         "unclear": "The state does not make the current phase clear"}},
         "next_action": {"type": "choice", "instructions": w["next"], "criteria": {
            "continue_phase": "Keep working on the current phase, it is not finished",
            "advance_phase": "The current phase is finished, move to the next phase",
            "fix_regression": "Something that previously worked is now broken and must be fixed",
            "ask_human": "The agent is blocked and needs a human decision",
            "goal_complete": "Every phase is finished and verified, the goal is met",
            "unclear": "The state does not make the next step clear"}},
         "stuck": {"type": "noul", "instructions": w["stuck"]},
         "off_goal": {"type": "noul", "instructions": w["off"]},
         "claims_done": {"type": "noul", "instructions": w["claims"]},
         "progress": {"type": "score", "instructions": w["progress"], "criteria": [
            "Not started", "Early", "About halfway", "Nearly done", "Complete and verified"]}}
    for p in PHASES[idx: idx + 2]:
        q["phase_done__" + p["id"]] = {"type": "noul", "instructions": w["done"].format(**p)}
    return q

def summarize(a):
    s = {}
    for k, v in a.items():
        if v["type"] == "noul":
            s[k] = round(v["noul"], 3)
        elif v["type"] == "choice":
            s[k] = {"choice": v["choice"], "conf": round(v.get("confidence", 0), 3),
                    "p": {o: round(p, 3) for o, p in v["probabilities"].items() if p >= 0.01}}
        else:
            s[k] = {"score": v["score"], "conf": round(v.get("confidence", 0), 3)}
    return s

if __name__ == "__main__":
    wname = sys.argv[1] if len(sys.argv) > 1 else "v1"
    w = WORDING[wname]
    results = {}
    for name, c in CASES.items():
        out = call(state_for(c), questions(w, c["current"]))
        s = summarize(out["answers"])
        # compete then verify: noul check on the choice winner
        win = out["answers"]["current_phase"]["choice"]
        if win != "unclear":
            p = next(p for p in PHASES if p["id"] == win)
            v = call(state_for(c), {"verify": {"type": "noul", "instructions": w["done"].format(**p)}})
            s["verify_winner_done"] = round(v["answers"]["verify"]["noul"], 3)
        s["_ms"] = out["_ms"]; s["_model"] = out.get("model")
        results[name] = s
    print(json.dumps({"wording": wname, "results": results}, indent=1))
