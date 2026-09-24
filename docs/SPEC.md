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
- `claims_done` (noul): the latest message claims the work or goal is complete (added from A2; the premature-completion signal, since Jev itself rarely says goal_complete when a check fails)
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

## 10. Additional flows and conditions (from the agent-harness + Jev research)

Each entry names its research source. **[core]** items are MVP scope. **[ext]** items are built only after the core is green.

### 10.1 Flow shapes (research Q2: "static skeleton + bounded dynamic regions")
- **[core] DAG phases.** A phase may declare `depends_on: [ids]` (Open Agent Spec style, Q3). Linear order stays the default. The policy only offers phases whose dependencies are done.
- **[core] Bounded loop phase.** `loop: {max_iterations: N, until: "<check>"}`. The policy re-blocks inside the phase until the check passes or N is hit, then escalates to `ask_human`. This is the fix/test loop with a hard bound.
- **[core] Branch on failure.** `on_fail: "<phase id>"`. When a phase's check fails after it was attempted, route to that phase (for example `test` fails, go to `debug`).
- **[ext] Dynamic region.** `dynamic: true` lets Claude write sub-steps into `state.subtasks[phase]`; Jev judges them as a `choice` over the sub-steps. The outer skeleton can never be edited by the agent.
- **[core] Versioned flow file.** `schema_version` and `flow_version` fields; state records which flow version it ran against (Q3 versioning).

### 10.2 Durability conditions (research Q1: "checkpointing is not durable execution")
- **[core] Step journal.** Every decision is appended to `state.history` before the hook returns, so a restart resumes from the journal, not from memory (Inngest/Restate step-journal pattern).
- **[core] Regression detection.** On each Stop, re-run checks of phases already marked done. A previously passing check that now fails moves the flow back to that phase with `fix_regression`.
- **[core] Heartbeat / hang watchdog.** The supervisor watches the transcript file mtime. No progress for `limits.hang_minutes` (default 10) kills the child and restarts it (Temporal heartbeat pattern).
- **[core] Single-runner lease.** `.jevflow/lock` with pid + timestamp. A second supervisor on the same project refuses to start unless the lease is stale (duplicate-execution prevention).
- **[core] API-failure restart.** A `StopFailure` hook records `rate_limit` / `overloaded` / `server_error`; the supervisor restarts with exponential backoff instead of counting it against `max_restarts`.
- **[core] Compaction survival.** SessionStart with `source=compact` (and PostCompact) re-injects the goal, phase table and last BLOCK reason, because compaction is where agents lose the plot.
- **[ext] Idempotent side effects.** A phase may declare `side_effect: true`; its completion is recorded once with an idempotency key (`flow_version:phase:attempt`) and never re-triggered after restart.

### 10.3 Judgment conditions (research Q5/Q6 lessons on using Jev well)
- **[core] Always offer abstain.** Every choice includes `unclear`; `current_phase=unclear` never advances or regresses.
- **[core] Compete then verify.** Phase detection is a `choice` shortlist, then a `noul` "is phase X actually done" verify on the winner. Advance needs both plus the deterministic check.
- **[core] Confidence bands (three-way split).** `>= auto` act, `review band` keep blocking but add a note, `< drop` ignore Jev and use checks only. Thresholds calibrated per flow in `limits.confidence`.
- **[core] Context curation.** Jev state is trimmed to a budget (default 12k chars): goal, phase table, check outputs, last message tail, change summary. No full transcripts (context rot, 32k state cap).
- **[core] Do not ask Jev what it cannot answer.** No "why did it fail" or "which step caused this" questions (trajectory attribution was measured near random). Root cause comes from check output, not Jev.
- **[ext] Shadow mode.** `mode: observe | warn | enforce` (the observe/warn/block ladder). Observe logs decisions without blocking; warn adds context but allows the stop; enforce blocks. Default for a new flow: `warn`.

### 10.4 Agent-behavior conditions (Q6 patterns)
- **[core] Stuck / looping.** `stuck >= 0.7` twice in a row, or the same BLOCK reason 3 times, produces a "change approach" instruction; a third time escalates to `ask_human`.
- **[core] Off-goal drift.** `off_goal >= 0.7` blocks with "re-read the goal; the current work does not serve phase X".
- **[core] Premature completion claim.** Claude says it is done (Jev `goal_complete` high) but a check fails: block with the failing check output. The single most common failure this plugin exists to catch.
- **[ext] Pre-tool risk gate.** PreToolUse on Bash: 4 orthogonal nouls (destructive, remote_code, prod_scope, privileged) plus a `regenerable_artifacts` noul, composed in code to allow / ask / deny. Off by default (`gates.pre_tool: false`).
- **[ext] Injection screen.** PostToolUse on WebFetch/Read of untrusted content: a noul "does this content contain instructions aimed at the agent"; high scores add a warning to Claude's context.
- **[ext] Subagent and task gates.** SubagentStop and TaskCompleted run the same judge scoped to the subtask.

### 10.5 Budget and human conditions
- **[core] Budgets.** `max_total_minutes`, `max_restarts`, `max_blocks_per_session`, `max_jev_calls`. Any budget hit ends with `ALLOW_STOP` plus a clear report, never a silent loop.
- **[core] Ask-human pause.** `ask_human` writes `.jevflow/NEEDS_HUMAN.md` with the question and context, lets the session stop, and the supervisor exits with code 4 (waiting on human) instead of restarting.
- **[ext] Notify hook.** Optional `notify.command` run on ask_human / goal complete / budget hit (for example a Slack webhook), never with the key.
