# Jevflow MVP Spec

Jevflow is a Claude Code plugin that keeps an agent session on track toward a declared goal. A human or agent writes a flow (goal plus ordered phases). At each checkpoint, Jev (TypeSafe's System One model) judges where the work is and what should happen next. Code applies the policy and takes the corrective action. A supervisor restarts the session if it dies before the goal is met.

Core principle from the research: **Jev supplies calibrated local judgments; code owns control flow, limits, and side effects.** Jev never authorizes anything by itself.

## 1. Flow file (`.jevflow/flow.json` in the target project)

```json
{
  "goal": "Build a CLI todo app in Python with add/list/done commands and tests",
  "phases": [
    {"id": "scaffold", "name": "Project scaffold", "done_when": "package layout and entry point exist", "check": "test -f todo/cli.py"},
    {"id": "implement", "name": "Implement commands", "done_when": "add, list and done commands work", "check": null},
    {"id": "test", "name": "Tests pass", "done_when": "a test suite exists and passes", "check": "python -m pytest -q"}
  ],
  "limits": {"max_blocks_per_session": 6, "max_restarts": 5, "max_total_minutes": 90},
  "privacy": {"send_diff": false}
}
```

- `check` is an optional shell command run by code (exit 0 = passes). Deterministic checks always outrank Jev.
- `privacy.send_diff=false` (default) sends only file names and line counts to Jev, never file contents.

## 2. State file (`.jevflow/state.json`, written only by Jevflow code)

`{current_phase, phase_status{id: pending|active|done}, blocks_this_session, restarts, started_at, history[{ts, event, phase, decision, probs}], done: bool}`

## 3. Judgment step (`jevflow/judge.py`)

Input "state" sent to Jev (JSON): goal, phases (id, name, done_when), current_phase, check results, last assistant message (truncated to 4k chars), git change summary, recent history (last 5 decisions).

One parallel request with these questions:
- `current_phase` (choice over phase ids + `unclear`)
- `phase_done__<id>` (noul) for the current phase and the next one
- `next_action` (choice): `continue_phase`, `advance_phase`, `fix_regression`, `ask_human`, `goal_complete`
- `stuck` (noul): repeating itself, making no progress, or going in circles
- `off_goal` (noul): working on something not serving the goal
- `progress` (score, 5 levels) toward the whole goal

## 4. Policy (`jevflow/policy.py`, pure function, fully unit-tested)

Given judgment + check results + state + limits, return one of:
- `ALLOW_STOP`: goal complete (all phase checks pass where defined AND Jev goal_complete confidence >= threshold), or a limit hit, or ask_human.
- `BLOCK(reason)`: continue working; the reason is a concrete corrective instruction naming the phase, what is missing, and any failing check output (truncated).
- `ADVANCE(to_phase)`: mark current phase done (check must pass if defined), then BLOCK with next-phase instruction.
- Thresholds live in one constants block. Low confidence (below band) never auto-advances; it keeps the current phase.
- `stuck` or `off_goal` >= 0.7 produces a BLOCK whose reason tells Claude to stop, re-read the goal, and take a different approach.
- Hard caps (max_blocks_per_session, respect Claude's `stop_hook_active` 8-block cap) always win so the loop cannot run forever.

## 5. Claude Code plugin surface

```
.claude-plugin/plugin.json
hooks/hooks.json          SessionStart + Stop (+ optional PreToolUse risk gate, off by default)
commands/init.md          /jevflow:init   write a flow.json from a goal the user states
commands/status.md        /jevflow:status show phase table + recent decisions
skills/jevflow/SKILL.md   how and when to use Jevflow
jevflow/                  python stdlib package (3.10+), no third-party deps
```

- **SessionStart hook**: if a flow exists, inject additionalContext: goal, phase list with status, current phase and its done_when. Resets blocks_this_session.
- **Stop hook**: run checks, call judge, apply policy, emit `{"decision":"block","reason":...}` or allow. Append to history.
- **Degraded mode**: if Jev is unreachable, key missing, or it errors, fall back to checks-only policy (never crash the session, never block forever). Log the reason.

## 6. Supervisor (`jevflow run`, the restart guarantee)

`python -m jevflow run --project <dir> [--plugin-dir <jevflow>]`
- Loop: launch `claude -p "<resume prompt>" --plugin-dir <jevflow> --output-format json` (with `--resume <session_id>` after the first run), wait for exit.
- After each exit, read state.json. If `done`, stop with success. If not done and restarts < max_restarts and time < max_total_minutes, restart with a prompt built from the current phase and the last BLOCK reason. Otherwise stop and report.
- Exit codes: 0 goal complete, 2 limit reached, 3 configuration error.
- Must be testable with a fake `claude` binary (env `JEVFLOW_CLAUDE_BIN`).

## 7. Key handling

API key from `JEV_API_KEY`, else the file at `JEVFLOW_KEY_FILE`, else `~/.config/jevflow/api_key`. Never printed, logged, or written to state.

## 8. Non-goals for MVP

No UI, no multi-agent, no marketplace publish, no Amazon data. Test only on synthetic toy projects.

## 9. Definition of done (MVP by morning)

1. `python -m unittest` passes (policy, judge request-building, state, hooks I/O, supervisor with fake claude).
2. Live Jev smoke test passes on a synthetic transcript (skips cleanly with no key).
3. End-to-end demo on a toy project in `examples/todo/`: at least one real `claude -p` run where the Stop hook blocks at least once, the phase advances, and the supervisor finishes or reports cleanly. If `claude -p` cannot run unattended here, document why and demo with the fake claude instead.
4. README: install (`claude --plugin-dir`), flow format, how the guarantee works, limits, privacy note, known gaps.
