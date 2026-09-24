# Jevflow morning report (2026-09-24)

Overnight autonomous build, 9 cycles of builder plus hostile critic. All 17 tasks in TASKS.md are done (A1 to G1). 280 unit tests pass on Python 3.11 (1 live test skips without a key; it passes with one). Everything is committed locally in this repo, nothing pushed.

## What works

- Plugin loads via `claude --plugin-dir` with SessionStart, Stop, StopFailure, PreToolUse, PostToolUse, SubagentStop and TaskCompleted hooks. Payload fields were captured from real runs (docs/RESEARCH.md, A1).
- Flow format with DAG phases, bounded loops, on_fail branches, dynamic sub-step regions, side-effect phases, versioning, strict validation (`jevflow validate`).
- Stop policy: pure function, 20+ table-driven conditions (premature completion, regression, stuck and looping escalation, off-goal, confidence bands, budgets, ask-human pause, degraded checks-only mode). Code owns every advance; Jev can only hold or inform.
- Shadow modes observe, warn, enforce across the Stop hook and all gates.
- Supervisor `jevflow run`: restarts with `--resume`, single-runner lease, hang watchdog, API-error backoff not charged as a restart, budget report, exit codes 0/2/3/4/5.
- Optional gates: Bash pre-tool risk gate (never emits allow), injection screen, subagent and task completion gates, notify command. All off by default and fail open.
- Live Jev measurements on synthetic data (docs/RESEARCH.md): premature "done" claim caught (claims_done 0.92, phase_done 0.02), `rm -rf src/ .git/` denied (0.99), build-dir wipe asks (destructive 0.95, regenerable 0.82), injection attack 0.99 vs benign 0.03, premature subagent claim held (complete 0.13, claims 0.89). Judge latency about 0.3 to 0.6 s per call.
- End-to-end with a scripted agent and real hooks, checks, Jev and supervisor: advance, review-band block, false "tests pass" claim blocked with the real failing assertion, goal complete, exit 0 (docs/DEMO.md run 2).

## What does not (yet)

- No successful real `claude -p` end-to-end run. The one real demo run (4 of 4 allowed) had every write refused by Claude Code. Jevflow handled it correctly (ask_human, NEEDS_HUMAN.md, exit 4), but no code got written. Unverified cause: the example project sat inside the `--plugin-dir` tree.
- The on_fail branch was not taken in any end-to-end run (unit tested only).
- Not a sandbox: the Claude process inherits the key location, and `flow.json` (checks, notify) is editable by the agent.
- Pre-tool secret redaction is pattern based.
- Linux and macOS only.

## Next steps

1. One real `claude -p` run with the toy project outside the plugin directory (for example a temp dir), `--max-turns 25`, flow in `enforce`. Goal: see a real block, an advance and exit 0.
2. Force the on_fail path in that run (seed a failing test) to exercise loop exhaustion into `debug`.
3. Decide on key isolation: a small local proxy holding the key, so the Claude process never sees it.
4. Protect `flow.json` from agent edits (hash recorded at run start, or a PreToolUse deny on writes to `.jevflow/`).
5. Calibrate `limits.confidence` on a handful of real transcripts; current bands come from 4 synthetic cases.
6. Push to a real repo and add CI once reviewed.

## Where to look

- `README.md`: install, flow format, conditions, privacy, gaps.
- `docs/SPEC.md`, `docs/RESEARCH.md`, `docs/DEMO.md`.
- `loop/CHANGELOG.md`: per-cycle log with every critic finding and fix (44 defects found and fixed across 9 cycles).
