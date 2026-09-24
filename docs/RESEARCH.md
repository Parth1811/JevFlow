# Jevflow research spikes

## A1. Claude Code plugin loading (2026-09-24, cycle 1)

Setup: throwaway plugin at `examples/spike/plugin/` (`.claude-plugin/plugin.json`, `hooks/hooks.json`, a shell logger that appends stdin JSON to `examples/spike/logs/<event>.jsonl`, plus a block-once Stop hook). Host: claude 2.1.280.929 (ASBX build). All runs from `examples/spike/work/` with `--plugin-dir ../plugin --max-turns 25 --output-format json`.

Real runs used: 3 of the 4 overnight budget (1 left for E2).

| Run | Command | Result |
|---|---|---|
| 1 | `claude -p "Reply with exactly the word OK..."` | exit 0, `result: "OK"`, 1 turn, $0.156, 1.9s. SessionStart(startup) + Stop fired |
| 2 | same session, `--resume <id>`, block-once Stop hook added | exit 0, `result: "AGAIN"`, 2 turns. SessionStart(resume), Stop(stop_hook_active=false) blocked, Claude continued, Stop(stop_hook_active=true) allowed |
| 3 | `claude -p "/compact" --resume <id>` | exit 0, 0 turns, $0.235. PreCompact(trigger=manual) then SessionStart(source=compact) fired |

### Captured payload fields

Sanitized copies (personal paths replaced by `/home/user/...`) are the test fixtures in `tests/fixtures/hooks/`.

- Common: `session_id`, `transcript_path`, `cwd`, `hook_event_name`.
- SessionStart startup: common + `source: "startup"`.
- SessionStart resume: common + `source: "resume"`, `seconds_since_last_response`, `context_tokens`, `prompt_cache_likely_expired`, `estimated_cache_write_usd`.
- SessionStart compact: common + `prompt_id`, `source: "compact"`, `model`.
- Stop: common + `prompt_id`, `permission_mode` (`"auto"` here), `effort {level}`, `stop_hook_active` (bool), `last_assistant_message` (string), `background_tasks` [], `session_crons` [].
- PreCompact: common + `prompt_id`, `trigger: "manual"`, `custom_instructions: null`.
- StopFailure: NOT observed (it only fires on an API error, which cannot be triggered safely). Fixture `StopFailure_rate_limit.json` is taken from the official hooks reference: common + `error` (`rate_limit | overloaded | server_error | ...`), optional `error_details`, optional `last_assistant_message` (the error string). Its output and exit code are ignored by Claude Code, so it can only record state for the supervisor.

### Behaviour confirmed

- `{"decision":"block","reason":...}` on stdout from a Stop hook makes Claude continue with the reason as its next instruction; the follow-up Stop carries `stop_hook_active: true`. Multiple Stop hooks in one group all run.
- `--resume <session_id>` keeps the same `session_id` and fires SessionStart with `source=resume`, so the supervisor can resume and re-inject context.
- `/compact` works as a `-p` prompt and fires `SessionStart source=compact`: the compaction re-injection path (SPEC 10.2) is testable headlessly.
- `cwd` is reported with the resolved real path (`/local/home/...`), not the symlinked `/home/...`. Jevflow must `os.path.realpath` both sides before comparing project dirs.
- `--output-format json` gives `session_id`, `subtype`, `is_error`, `num_turns`, `total_cost_usd`, `terminal_reason`, `permission_denials` for the supervisor to parse.

### Verdict on unattended `claude -p`

Yes. `claude -p` runs unattended on this host with no prompts, no TTY, exit 0, and plugin hooks load from `--plugin-dir`. The session reports `permission_mode: "auto"` by default on this build. Not yet verified: tool calls that need write permission in the E2 demo. E2 should pass an explicit `--permission-mode acceptEdits` (or `--allowedTools`) rather than rely on the host default. Each real run costs about $0.15 to $0.25 even for a one-word reply (cache creation of about 24k tokens), so the 4-run budget is the right cap.

## A2. Jev judge prompt design (2026-09-24, cycle 1)

Script: `loop/spikes/a2_jev_spike.py` (stdlib, key from `JEVFLOW_KEY_FILE`, never printed). Endpoint `POST https://api.typesafe.ai/v1/systemone`, `model: jev-latest`, served by `jev-1.13.0`. One parallel request per case with the SPEC 3 question set (every choice includes `unclear`), plus an added `claims_done` noul, then a second compete-then-verify call: a `noul` "is the winning phase actually done" on the `current_phase` winner. Latency 270 to 520 ms per call. Raw outputs: `loop/spikes/a2_v1.json`, `a2_v2.json`, `a2_v3.json`.

Synthetic cases (toy todo-app flow, 3 phases scaffold / implement / test):
- mid_phase: in implement, `add` done, `list` next.
- phase_complete: implement finished per the message, test check still failing (no tests yet).
- stuck_looping: in test, same ImportError fix retried 3 times.
- premature_done: message says "All done", test check shows 2 failed.

### Results with the chosen wording (v3)

| Case | current_phase (conf) | next_action (conf) | phase_done cur | verify winner | stuck | off_goal | claims_done |
|---|---|---|---|---|---|---|---|
| mid_phase | implement (1.00) | continue_phase (1.00) | 0.04 | 0.04 | 0.08 | 0.02 | 0.05 |
| phase_complete | implement (0.83) | advance_phase (0.58) | 0.83 | 0.83 | 0.13 | 0.02 | 0.23 |
| stuck_looping | test (1.00) | continue_phase (0.66) | 0.02 | 0.02 | 0.95 | 0.03 | 0.02 |
| premature_done | test (1.00) | continue_phase (0.94) | 0.03 | 0.03 | 0.23 | 0.06 | 0.93 |

All four cases were judged correctly under the bands below.

### Wording iterations

- v1 (plain SPEC 3 wording): correct on all 4, but two false-positive-ish nouls: `off_goal` 0.31 on stuck_looping and `stuck` 0.43 on premature_done.
- v2 (stricter wording everywhere, including "a claim by the agent alone is not evidence" on phase_done): fixed the two nouls (0.03, 0.24) but broke phase_complete: `phase_done__implement` fell to 0.41 and `current_phase` flipped to `test` at 0.46. Too strict: it discounted a detailed, concrete message.
- v3 (chosen): v1 wording for current_phase, phase_done, next_action, progress, claims_done; v2 wording for stuck ("retrying the same failing approach without new information") and off_goal ("work unrelated to the goal and to the current phase").

Chosen v3 question wording (goes into `jevflow/judge.py`):
- current_phase (choice): "Which phase of the plan is the agent currently working on", criteria `<phase name>: <done_when>` per phase plus `unclear`.
- phase_done__<id> (noul): "The agent has fully completed the '<name>' phase: <done_when>".
- next_action (choice): "What should happen next in this session", options continue_phase, advance_phase, fix_regression, ask_human, goal_complete, unclear.
- stuck (noul): "The recent history and latest message show the agent retrying the same failing approach without new information".
- off_goal (noul): "The latest message describes work unrelated to the goal and to the current phase".
- claims_done (noul, new): "The latest message claims the work or the goal is complete".
- progress (score, 5 levels): Not started / Early / About halfway / Nearly done / Complete and verified.

### Proposed default confidence bands (`limits.confidence`)

- Choice `confidence` (the API field, which is lower than the top probability) and noul values share one scale.
- `auto >= 0.80`: act on the judgment (subject to deterministic checks).
- `review 0.50 to 0.80`: keep the current phase, keep blocking, add a note to the BLOCK reason.
- `drop < 0.50`: ignore Jev for this decision and use checks only.
- `stuck` and `off_goal` trigger at `>= 0.70` (SPEC 4). The observed gap (0.95 true vs <= 0.23 false for stuck with v3) leaves margin.
- Advance requires all of: `current_phase` winner conf >= auto, verify noul >= auto, and the phase `check` passing if defined. phase_complete passes this at 0.83 / 0.83, which is close to the line: the default `auto` should not be raised above 0.80 without per-flow calibration.
- Premature completion = `claims_done >= 0.70` AND some check failing. Note Jev itself did NOT say goal_complete in premature_done (it saw the failing check in the state), so the policy must key on `claims_done` plus check output, not on `next_action == goal_complete`.

### Findings that change the design

1. Add `claims_done` to the SPEC 3 question set; it is the signal for SPEC 10.4 premature completion.
2. `next_action` is the weakest question (conf 0.58 to 0.66 on the two hard cases). Policy should not key control flow on it; use it only as a tiebreaker and log it.
3. Because the check results are in the Jev state, Jev's phase_done tends to agree with the checks. That is fine: the check still outranks Jev in code.
4. Each case needs 2 calls (judge + verify). Budget `max_jev_calls` should count calls, not Stop events.
