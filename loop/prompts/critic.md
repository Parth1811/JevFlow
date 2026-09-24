# Critic prompt (run after each build batch, fix before finalizing)

Review only what this cycle built. Be hostile. For each item check:
- Acceptance check actually met, verified by running it (not by reading code). Tests really ran with `/usr/bin/python3.11` and the count is real.
- Hooks never raise and never block forever: every path emits valid JSON or exits 0; caps enforced; degraded mode when Jev fails.
- Policy honors "code owns control flow": no side effect or phase advance driven by a probability alone when a deterministic check exists or confidence is below band.
- Key never printed/logged/written; no file contents sent to Jev when `send_diff=false`.
- Nothing written outside `/home/parthvp/Documents/jevflow`; no push; no destructive ops.
- No em-dashes in docs. No personal absolute paths inside the shipped plugin code (`jevflow/`, `hooks/`, `commands/`, `skills/`, `.claude-plugin/`); loop/ and docs/ may reference them.
Fix every defect found, rerun the checks, then note "critic: N issues found, N fixed" in the changelog entry.
