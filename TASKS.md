# Jevflow MVP Tasks

Each item: acceptance check in brackets. Mark `[x]` when done, or `SKIPPED: <reason>`. SPEC section refs in parentheses.

## Phase A: research spikes
- [x] A1 Verify Claude Code plugin loading here: minimal plugin with Stop, SessionStart (incl. source=compact) and StopFailure hooks that log stdin JSON, loaded via `claude -p ... --plugin-dir`. Record exact payload fields and whether `claude -p` runs unattended on this host. Write findings to docs/RESEARCH.md. [RESEARCH.md has captured payloads + verdict on unattended -p]
- [x] A2 Jev prompt design spike: run the judge question set (SPEC 3, 10.3 abstain + compete-then-verify) against 4 synthetic transcripts: mid-phase, phase-complete, stuck-looping, premature "done" claim. Record probabilities, tune wording, propose default confidence bands. Append to docs/RESEARCH.md. [4 cases recorded, wording + bands chosen]

## Phase B: core library
- [x] B1 `jevflow/jev_client.py`: stdlib client, key resolution (SPEC 7), backoff on 429/529, timeout, typed errors, `max_jev_calls` counter. [unit tests with mocked urlopen]
- [x] B2 `jevflow/flow.py` + `jevflow/state.py`: load/validate flow.json incl. schema_version/flow_version, depends_on DAG (cycle detection), loop, on_fail (10.1); atomic state write; step journal (10.2). [unit tests incl. invalid flows and a DAG cycle]
- [x] B3 `jevflow/judge.py`: build curated state within the char budget, questions with `unclear`, compete-then-verify, privacy handling (10.3). Parse into a Judgment dataclass. [unit tests on request shape + budget trimming]
- [x] B4 `jevflow/policy.py`: pure decide() covering SPEC 4 and 10.1-10.5 core: confidence bands, DAG eligibility, bounded loop, on_fail branch, regression detection, stuck/looping escalation, off-goal, premature completion, budgets, ask_human, degraded checks-only mode. [table-driven unit tests, >=20 cases, one per condition]

## Phase C: plugin
- [x] C1 `.claude-plugin/plugin.json`, `hooks/hooks.json`, `jevflow/hooks.py`: SessionStart (startup/resume/compact re-injection), Stop, StopFailure (records error for supervisor). Stdin JSON in, JSON out, never raise. [unit tests feeding fixture payloads from A1]
- [x] C2 `commands/init.md`, `commands/status.md`, `skills/jevflow/SKILL.md`, `python -m jevflow status` (phase table, recent decisions, NEEDS_HUMAN if present). [files exist; status prints table on fixture]

## Phase D: supervisor
- [ ] D1 `jevflow/supervisor.py` + `python -m jevflow run` (SPEC 6 + 10.2 + 10.5): restart loop, single-runner lease, hang watchdog on transcript mtime, StopFailure backoff not counted as restart, ask_human exit code 4, budget report. [unit tests with a fake claude script covering: restart after non-done exit, stop on done, stop at max_restarts, hang kill+restart, stale vs live lease, backoff path, exit 4]

## Phase E: prove the core
- [ ] E1 Live smoke test `tests/test_live_jev.py` (skips without key): judge on a synthetic transcript returns a well-formed Judgment. [passes with key]
- [ ] E2 End-to-end demo on `examples/todo/` (SPEC 9.3) using a flow with a DAG, a bounded test loop and an on_fail branch. Save the log to docs/DEMO.md. [DEMO.md shows at least one block, one advance, and the outcome]

## Phase F: extensions (only after A-E are green; SKIP any that do not fit before 11:30 UTC)
- [ ] F1 Shadow mode `observe | warn | enforce` (10.3). [policy tests for each mode]
- [ ] F2 Pre-tool risk gate on Bash with the regenerable_artifacts refinement, off by default (10.4). [tests with mocked judge; one live call recorded]
- [ ] F3 Injection screen on PostToolUse for WebFetch/Read (10.4). [tests with mocked judge]
- [ ] F4 Dynamic region sub-steps + idempotent side-effect phases (10.1, 10.2). [unit tests]
- [ ] F5 Notify hook + SubagentStop/TaskCompleted gates (10.4, 10.5). [unit tests]

## Phase G: ship
- [ ] G1 README.md (install via `claude --plugin-dir`, flow format with every field, how the guarantee works, conditions table, limits, privacy note, known gaps), final critic pass over the whole repo, docs/MORNING_REPORT.md (works / does not / next steps). [report written, full test suite green]
