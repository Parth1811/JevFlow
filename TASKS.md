# Jevflow MVP Tasks

Each item: acceptance check in brackets. Mark `[x]` when done, or `SKIPPED: <reason>`.

## Phase A: research spikes
- [ ] A1 Verify Claude Code plugin loading here: minimal plugin with a Stop hook that logs stdin JSON, loaded via `claude -p ... --plugin-dir`. Record exact Stop/SessionStart payload fields and whether `claude -p` runs unattended on this host. Write findings to docs/RESEARCH.md. [RESEARCH.md has captured payload + verdict on unattended -p]
- [ ] A2 Jev prompt design spike: run the judge question set (SPEC section 3) against 3 synthetic transcripts (mid-phase, phase-complete, stuck-looping) with the real key. Record probabilities and tune question wording. Append to docs/RESEARCH.md. [3 cases recorded, wording chosen]

## Phase B: core library
- [ ] B1 `jevflow/jev_client.py`: stdlib client, key resolution per SPEC 7, backoff on 429/529, timeout, typed errors. [unit tests with mocked urlopen]
- [ ] B2 `jevflow/flow.py` + `jevflow/state.py`: load/validate flow.json, atomic state read/write, history append. [unit tests incl. invalid flow]
- [ ] B3 `jevflow/judge.py`: build state payload + questions, parse answers into a Judgment dataclass, privacy handling. [unit tests on request shape]
- [ ] B4 `jevflow/policy.py`: pure decide() per SPEC 4 incl. degraded checks-only mode. [table-driven unit tests, >=12 cases]

## Phase C: plugin
- [ ] C1 `.claude-plugin/plugin.json`, `hooks/hooks.json`, `jevflow/hooks.py` entry points for SessionStart and Stop (stdin JSON in, JSON out, never raise). [unit tests feeding fixture payloads from A1]
- [ ] C2 `commands/init.md`, `commands/status.md`, `skills/jevflow/SKILL.md`, `python -m jevflow status`. [files exist; status prints table on fixture]

## Phase D: supervisor
- [ ] D1 `jevflow/supervisor.py` + `python -m jevflow run` per SPEC 6. [unit test with fake claude script: restarts after a non-done exit, stops on done, stops at max_restarts]

## Phase E: prove it
- [ ] E1 Live smoke test `tests/test_live_jev.py` (skips without key). [passes with key]
- [ ] E2 End-to-end demo on `examples/todo/` per SPEC 9.3, transcript/log saved to docs/DEMO.md. [DEMO.md shows block, advance, and outcome]
- [ ] E3 README.md, final critic pass over everything, docs/MORNING_REPORT.md with what works, what does not, next steps. [report written]
