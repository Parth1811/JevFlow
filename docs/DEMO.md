# E2 demo: examples/todo

Goal of the toy project: a Python CLI todo app (`add`, `list`, `done N`, storage in `$TODO_FILE`), a README and a passing unittest suite. Flow: `examples/todo/.jevflow/flow.json`, mode `enforce`, `max_restarts: 0` (one launch only, to stay inside the real-run budget).

Flow shape exercised:

| Phase | Depends on | Check | Special |
|---|---|---|---|
| scaffold | (root) | `todo/__init__.py` and `todo/cli.py` exist | |
| implement | scaffold | add, list, done smoke run against `TODO_FILE` | |
| docs | scaffold | README mentions `TODO_FILE` and `done` | DAG sibling of implement |
| test | implement | unittest discover passes | `loop` max 3, `on_fail: debug` |
| debug | implement | unittest discover passes | branch-only on_fail target |

Two runs are recorded. Run 1 is the only real `claude -p` run the budget allowed (4 of 4 used overnight). Its sessions could not write files, so SPEC 9.3's fallback applies: run 2 replays the same flow with a scripted agent in place of Claude, while the real plugin hooks, real checks, real Jev and the real supervisor do the work.

## Run 1: real `claude -p` (2026-09-24 07:53 UTC)

Command (from the repo root):

```
JEVFLOW_KEY_FILE=... python3.11 -m jevflow run --project examples/todo \
  --plugin-dir . --max-turns 25 \
  --claude-arg=--allowedTools '--claude-arg=Bash(/usr/bin/python3.11:*)' --json
```

What happened:

- Claude Code 2.1.280.929, `--permission-mode acceptEdits`, 11 turns, 91 s, $0.59.
- All 7 tool calls that would have written anything were refused: 4 `Write` calls (`todo/__init__.py`, `todo/cli.py`, `README.md`) were rejected as edits to "a sensitive file", and 3 `Bash` calls (`mkdir`, a `printf` redirect, reading `.claude/settings*.json`) fell outside the allow list. Claude ended its turn saying it was blocked and asking a human to approve the writes.
- Stop hook: the scaffold check failed. Jev judged `current_phase` scaffold 1.00, `next_action` ask_human 0.98, `phase_done__scaffold` 0.02, `claims_done` 0.16, `stuck` 0.32 (2 Jev calls).
- Policy: `ask_human` (auto band) -> ALLOW_STOP, wrote `.jevflow/NEEDS_HUMAN.md`, set `needs_human`.
- Supervisor: exit 4 `needs_human` after 1.6 min, no relaunch. That is the right outcome: a session that cannot write should not be relaunched in a loop.

Why the writes were refused is not verified. The ASBX build's file-edit safety check covers "settings, hooks, .git, shell profiles and other sensitive files". The likely cause is that the project sat inside the directory passed as `--plugin-dir`, so the whole project looked like plugin (hook) configuration. The A1 spike used a sibling directory and never tried a write. Confirming this needs one more real run, with the project outside the plugin tree and possibly the host default permission mode. The README records this as a known gap: keep the project outside the plugin directory.

Code changes made because of this run:

- The supervisor now counts `permission_denials` from the `claude -p` JSON result and reports them ("claude permission denials N"), both in the report and in the `supervisor_end` journal entry. Without this, the report showed only a stuck phase.
- `jevflow run` accepts `--opt=value`, which is needed to pass Claude flags that start with `-` through `--claude-arg`.
- The git change summary uses `--relative`, so a project nested in a larger repo reports only its own files, with project-relative paths.

## Run 2: scripted agent, real hooks + real Jev (2026-09-24 08:02 UTC)

The driver is `examples/todo_replay/replay_claude.py`, given to the supervisor as `JEVFLOW_CLAUDE_BIN`. It stands in for Claude: each turn it does the work for the phase that `state.json` names as current, then sends a Stop payload to the real `hooks/jevflow hook Stop` and keeps going while it gets a block (with `stop_hook_active: true`, as Claude Code does). It makes two scripted mistakes: its first stop claims the whole app is done when only a stub exists, and its first test file has a wrong expected string while it claims the tests pass.

```
JEVFLOW_CLAUDE_BIN=examples/todo_replay/replay_claude.py JEVFLOW_KEY_FILE=... \
  python3.11 -m jevflow run --project examples/todo --plugin-dir . --max-turns 25
```

| Turn | Phase | Agent's closing message | Checks | Jev (winner, verify, claims_done) | Decision | Next phase |
|---|---|---|---|---|---|---|
| 1 | scaffold | "All done! The todo app is complete with add, list and done." (only a stub exists) | scaffold pass | scaffold 0.90, verify 0.92, claims_done 0.92 | ADVANCE `advance` (stop blocked) | implement |
| 2 | implement | "Implemented add, list and done in todo/cli.py; ..." | scaffold, implement pass | implement 0.99, verify 0.92 | ADVANCE `advance` | docs |
| 3 | docs | "Wrote README.md documenting add, list, done and TODO_FILE." | all 3 pass | docs 0.76 (review band), verify 0.93 | BLOCK `review_band` | docs |
| 4 | docs | same message | all 3 pass | docs 0.86, verify 0.68 (review band again) | ADVANCE `review_check_pass` | test |
| 5 | test | "Added tests/test_cli.py ... All tests pass." (false) | test FAIL | not asked (loop phase is deterministic) | BLOCK `loop_continue`, iteration 1 of 3, with the failing assertion | test |
| 6 | test | "The failing assertion expected '[done]' but list prints '[x]'. Fixed ..." | all pass | not asked | ALLOW_STOP `goal_complete` | (done) |

Block text the agent received on turn 5 (trimmed):

```
[jevflow] Phase 'test' (Tests pass), iteration 1 of 3: keep going until
'/usr/bin/python3.11 -m unittest discover -s tests -t . -q' passes. Check for 'test' fails:
FAIL: test_add_list_done (tests.test_cli.CliTest.test_add_list_done)
AssertionError: '1. [x] buy milk\n' != '1. [done] buy milk\n'
```

Outcome: `jevflow run: goal_complete (exit 0)`, 1 run, 0 restarts, 8 Jev calls out of 40, 4 s. Journal: supervisor_start, supervisor_launch, session_start, stop x6 (advance, advance, review_band, review_check_pass, loop_continue, goal_complete), supervisor_end. The generated suite passes when run by hand.

What this shows:

- Blocks: 5 of 6 stops were blocked, so the agent could not end early. That includes both false claims: the first was redirected to the next phase, the second got the real failing test output.
- Advances: 4, each gated by a passing deterministic check. Turn 4 advanced from the review band only after the check passed on 2 stops in a row (the C1 rule). Jev never advanced a phase by itself.
- DAG: after scaffold, both implement and docs were eligible; test waited on implement.
- Bounded loop: the test phase re-blocked once, and passed on iteration 2 of 3. The `on_fail: debug` branch was not taken, because the loop passed before it ran out. That branch and the loop running out are covered by unit tests, not by this demo.
- Turn 1: Jev rated the "all done" claim 0.92, but scaffold really was done, so the policy advanced one phase and blocked instead of flagging `premature_completion`. The stop was still refused, which is what matters. `premature_completion` fires when the current phase's check fails, which the live E1 and C1 checks showed.

Not shown by either run: a real Claude session that writes code under Jevflow's control. That needs one more real run with the project outside the plugin directory.
