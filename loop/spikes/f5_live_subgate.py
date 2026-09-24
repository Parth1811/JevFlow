"""F5 live check: subtask judge on two synthetic subagent results (2 Jev calls)."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from jevflow.jev_client import JevClient
from jevflow import subgates

B = {"auto": 0.80, "flag": 0.70}
cases = {
    "premature": ("Add input validation to parse_date() in toy/dates.py and a unit test for it",
                  "Done! I looked at parse_date and it seems fine. Validation is complete."),
    "genuine": ("Add input validation to parse_date() in toy/dates.py and a unit test for it",
                "Added a ValueError for empty and malformed strings in parse_date(), plus "
                "test_parse_date_rejects_bad_input in tests/test_dates.py. Ran the tests: 4 passed."),
}
out = {}
c = JevClient(max_calls=2)
for name, (task, msg) in cases.items():
    r = subgates.judge_subtask(c, task=task, goal="Toy date utils", last_message=msg, b=B)
    out[name] = {"verdict": r.verdict, "condition": r.condition,
                 "probs": {k: round(v, 3) for k, v in r.probs.items()}, "error": r.error}
print(json.dumps(out, indent=1))
json.dump(out, open(os.path.join(os.path.dirname(__file__), "f5_live.json"), "w"), indent=1)
