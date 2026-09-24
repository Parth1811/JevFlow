# Jevflow

A Claude Code plugin that keeps a session working until a declared goal is actually met. You write a flow (a goal plus phases, each with a `done_when` sentence and usually a shell `check`). Every time Claude tries to stop, Jevflow runs the checks, asks Jev (TypeSafe's calibrated judgment model) where the work stands, and a fixed, unit-tested policy decides whether Claude may stop, must keep going, or moves to the next phase. A supervisor relaunches the session if it dies first.

Design rule: Jev supplies calibrated judgments; code owns control flow, limits and side effects. A probability alone never advances a phase, never marks a goal complete, and never allows a tool call.

Status: MVP, version 0.1.0. Python 3.10+ standard library only, no third-party dependencies. Tested on synthetic toy projects only.

## Install

Requirements: Claude Code with plugin support, Python 3.10 or newer, a Jev API key.

```sh
git clone <this repo> ~/jevflow
claude --plugin-dir ~/jevflow            # interactive
```

The hook launcher (`hooks/jevflow`) searches for a working Python >= 3.10 (`$JEVFLOW_PYTHON`, then `python3.13` ... `python3`, `python`) and verifies each candidate actually runs, because some hosts ship a `python3` shim that prints nothing and exits 0.

Key, in order of precedence:

1. `JEV_API_KEY`
2. the file named by `JEVFLOW_KEY_FILE`
3. `~/.config/jevflow/api_key`

Prefer a key file with mode 600. The key is never printed, logged or written to state. Without a key Jevflow runs in checks-only (degraded) mode.

## Quick start

```sh
cd my-project
claude --plugin-dir ~/jevflow
> /jevflow:init Build a CLI todo app in Python with add/list/done commands and tests
```

`/jevflow:init` writes `.jevflow/flow.json` in `warn` mode (reports what it would block, blocks nothing). Validate and inspect with:

```sh
~/jevflow/hooks/jevflow validate --project .
~/jevflow/hooks/jevflow status --project .      # or /jevflow:status inside Claude
```

Switch to `"mode": "enforce"` once the decisions look right. For an unattended run with restarts:

```sh
~/jevflow/hooks/jevflow run --project . --max-turns 40
```

## Flow format

`.jevflow/flow.json`. Unknown keys anywhere are rejected, so a typo fails at load time.

```json
{
  "schema_version": 1,
  "flow_version": "1",
  "goal": "Build a CLI todo app in Python with add/list/done commands and tests",
  "mode": "warn",
  "phases": [
    {"id": "scaffold", "name": "Project scaffold", "done_when": "package layout and entry point exist",
     "check": "test -f todo/cli.py"},
    {"id": "implement", "name": "Implement commands", "done_when": "add, list and done work",
     "dynamic": true},
    {"id": "docs", "name": "Usage docs", "done_when": "README documents every command",
     "depends_on": ["scaffold"], "check": "grep -q 'todo add' README.md"},
    {"id": "test", "name": "Tests pass", "done_when": "a test suite exists and passes",
     "depends_on": ["implement", "docs"], "check": "python -m pytest -q",
     "loop": {"max_iterations": 3, "until": "python -m pytest -q"}, "on_fail": "debug"},
    {"id": "debug", "name": "Debug failures", "done_when": "the failing tests' cause is fixed"},
    {"id": "publish", "name": "Tag release", "done_when": "v0.1 tag exists",
     "depends_on": ["test"], "check": "git tag -l v0.1 | grep -q v0.1", "side_effect": true}
  ],
  "limits": {
    "max_blocks_per_session": 6, "max_restarts": 5, "max_total_minutes": 90,
    "hang_minutes": 10, "max_jev_calls": 200, "check_timeout_s": 120,
    "state_char_budget": 12000,
    "confidence": {"auto": 0.80, "review": 0.50, "flag": 0.70}
  },
  "privacy": {"send_diff": false},
  "gates": {
    "pre_tool": false, "injection_screen": false, "injection_tools": ["WebFetch"],
    "subagent_stop": false, "task_completed": false,
    "bands": {"deny": 0.80, "ask": 0.50, "regenerable": 0.80, "injection": 0.70}
  },
  "notify": {"command": "./notify.sh", "on": ["ask_human", "goal_complete", "budget"], "timeout_s": 10}
}
```

Top level:

| Field | Default | Meaning |
|---|---|---|
| `schema_version` | `1` | Only 1 is supported. |
| `flow_version` | `"1"` | Recorded in state; part of side-effect idempotency keys. Bump it when you change the plan. |
| `goal` | required | One concrete, checkable sentence. |
| `mode` | `"enforce"` (init writes `"warn"`) | `observe`: journal only. `warn`: never blocks, shows what it would do. `enforce`: blocks and pauses for humans. |
| `phases` | required | Non-empty list, see below. |
| `limits` | see example | All integers >= 1, except `max_restarts` may be 0 (run once). |
| `privacy.send_diff` | `false` | When false, Jev sees only file names and line counts, never file contents. |
| `gates` | all off | Optional tool gates, see Conditions. |
| `notify` | none | Optional command run on human, goal and budget events. |

Phase fields:

| Field | Meaning |
|---|---|
| `id` | `^[a-z][a-z0-9_-]{0,39}$`; `unclear` is reserved. |
| `name`, `done_when` | Shown to Claude and to Jev. `done_when` is what Jev verifies. |
| `check` | Optional shell command, exit 0 = passes. Runs in the project with the Jev key removed from its environment. Always outranks Jev. |
| `depends_on` | Omitted: depends on the previous phase (linear). `[]`: a root. A list: explicit DAG edges. Cycles are rejected. |
| `loop` | `{max_iterations, until}`: re-block inside the phase until the `until` command passes, then continue; after N failures route to `on_fail` or ask a human. |
| `on_fail` | Phase to route to when this phase's check fails after an attempt. A target nothing depends on is branch-only: it runs only when routed to and is not required for goal completion. |
| `dynamic` | Claude may split this phase into sub-steps via `.jevflow/subtasks.json`. Sub-steps can only hold an advance, never cause one. |
| `side_effect` | An external action (publish, deploy, send). Requires a `check`. Completion is written once to `.jevflow/side_effects.jsonl` with key `flow_version:phase:attempt`; it is never re-entered, even after a state reset. |

## How the guarantee works

1. SessionStart (startup, resume, compact) injects the goal, the phase table, the current phase and, after resume or compaction, the last Jevflow instruction. Compaction is where agents usually lose the plot.
2. Stop runs the checks (current phase, loop condition, and every already-done phase for regression), builds a curated state within `state_char_budget` (goal, phase table, check results with failing output tail, last message tail, git change summary, last 5 decisions), and asks Jev one parallel request: current phase (with `unclear`), phase-done verify nouls, next action, stuck, off-goal, claims-done, progress.
3. The pure `policy.decide()` returns `ALLOW_STOP`, `BLOCK(reason)` or `ADVANCE(to_phase)`. Every decision is journaled to `.jevflow/state.json` before the hook returns, so a restart resumes from the journal.
4. The supervisor (`jevflow run`) launches `claude -p ... --plugin-dir ...`, and after each exit reads state: done ends the run, otherwise it relaunches with `--resume` and a prompt built from the current phase and last block reason, within the restart and time budgets. It holds a single-runner lease, kills a hung session, and backs off on API errors without charging a restart.

Advance needs three things together: Jev's `current_phase` winner is this phase at `auto` confidence, the `phase_done` verify noul is at `auto`, and the phase check passes when one is defined. Below the `review` band Jev is ignored and only checks count.

Supervisor exit codes: 0 goal complete, 2 limit reached, 3 configuration error, 4 waiting on a human (`.jevflow/NEEDS_HUMAN.md`), 5 another supervisor is running, 130 interrupted.

## Conditions

Evaluated in fixed priority order on each Stop; the first match wins. The `condition` tag is in every journal entry.

| Condition | Trigger | Result |
|---|---|---|
| `already_done` | flow already complete | allow stop |
| `budget_time`, `budget_blocks`, `budget_jev`, `hook_cap` | a limit hit, or 7 consecutive blocks (Claude Code's own cap is 8) | allow stop with report, notify `budget` |
| `regression` | a done phase's check now fails | back to that phase with the failing output |
| `side_effect_regression`, `side_effect_on_fail` | a done side-effect phase fails, or on_fail routes into one | ask human, never re-run |
| `loop_continue`, `loop_pass`, `loop_exhausted`, `loop_exhausted_on_fail` | bounded loop phase | re-block, advance, route to on_fail, or ask human |
| `degraded_check_pass/fail`, `degraded_no_check` | Jev unreachable, no key, error or bad answer | checks only; never blocks without evidence |
| `premature_completion`, `final_check_fail` | Claude claims done while a check fails | block with the failing check output |
| `stuck`, `stuck_escalate`, `stuck_ask_human` | stuck >= flag twice in a row, or the same block reason 3 times | "change approach", then ask human |
| `off_goal` | off_goal >= flag | re-read the goal |
| `ask_human` | Jev next_action ask_human at `auto` | write NEEDS_HUMAN.md, stop, supervisor exits 4 |
| `advance`, `subtask_pending` | advance rule above; a dynamic phase's named sub-step still open | advance, or hold |
| `review_band`, `review_check_pass` | confidence between `review` and `auto` | keep blocking with a note; 2 review stops with the check passing advance |
| `drop_band`, `phase_mismatch`, `continue` | low confidence or disagreement | keep the current phase |
| `dag_deadlock`, `bad_state` | no eligible phase, or unreadable state | ask human |
| `goal_complete` | every required phase done, all checks pass, Jev agrees | allow stop, notify `goal_complete` |

Optional gates (`gates.*`, all off by default, all fail open to Claude Code's own permission system when Jev is unavailable):

| Gate | Hook | What it does |
|---|---|---|
| `pre_tool` | PreToolUse on Bash | Nouls destructive, remote_code, prod_scope, privileged, regenerable_artifacts, composed in code to ask or deny. Never emits allow, so it can only tighten permissions. Plain read-only commands skip Jev. |
| `injection_screen` | PostToolUse on WebFetch (and Read if listed and `send_diff` is true) | Adds a "treat as data" warning when content looks like instructions aimed at the agent. Never blocks. |
| `subagent_stop`, `task_completed` | SubagentStop, TaskCompleted | Holds only a confident premature "done" claim on unfinished work, at most twice per subtask. |

All three modes apply to gates too: observe journals, warn shows a system message, enforce acts.

## Privacy

- With `send_diff: false` (default), Jev receives: goal, phase names and `done_when`, check exit codes and output tails, the tail (4k chars) of Claude's last message, changed file names with line counts, and recent decisions. No file contents.
- Check output and Claude's messages can still contain code or data; write checks that print little.
- The pre-tool gate sends Bash command lines. Credential-looking values (password, token, api_key, Bearer, AWS key ids) are redacted by pattern before sending and journaling, but redaction is best effort.
- Jev is an external service. Do not use Jevflow on projects whose data may not leave your environment.

## Limits and known gaps

- Not a sandbox. The hooks need the key, so the Claude process inherits `JEVFLOW_KEY_FILE` or `JEV_API_KEY`; the agent runs as the same user and could read it.
- `flow.json` is trusted code. `check` and `notify.command` are shell commands, and the agent can edit the file. The skill tells Claude not to, but nothing enforces it. Review flow changes like code.
- Do not enable `pre_tool` where secrets appear on command lines (redaction is pattern based).
- Sub-step titles in `subtasks.json` are agent-written text sent to Jev. They can only hold an advance, never cause one.
- TaskCompleted reads the transcript tail, which can lag the live turn; then the gate allows.
- The only real `claude -p` end-to-end run on the dev host was refused all file writes (the example project sat inside the plugin directory, most likely tripping Claude Code's edit safety check). The full flow was demonstrated with a scripted agent against the real hooks, checks, Jev and supervisor instead. See `docs/DEMO.md`.
- The `on_fail` branch is covered by unit tests but was not taken in the demo.
- Jev is asked only what it measured well on: phase detection, done verification, stuck, off-goal, claims-done. It is never asked why something failed; root cause comes from check output.
- Linux and macOS only (uses `fcntl` and process groups).

## Layout

```
.claude-plugin/plugin.json   plugin manifest
hooks/hooks.json, jevflow    hook registration and the Python launcher
commands/                    /jevflow:init, /jevflow:status
skills/jevflow/SKILL.md      how Claude should behave in a tracked session
jevflow/                     the package: flow, state, judge, policy, hooks, gates,
                             subgates, regions, notify, supervisor, status, jev_client
tests/                       unittest suite (python -m unittest discover -s tests)
docs/                        SPEC, RESEARCH (spikes, measured Jev probabilities), DEMO
examples/                    toy projects used by the demo
```

## Tests

```sh
python3.11 -m unittest discover -s tests
```

The live Jev smoke test (`tests/test_live_jev.py`) skips cleanly without a key.

## License

Apache-2.0.
