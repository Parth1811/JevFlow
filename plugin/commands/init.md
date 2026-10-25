---
description: Write a Jevflow flow (.jevflow/flow.json) for a goal you state
argument-hint: <goal in one or two sentences>
allowed-tools: Read, Write(./.jevflow/**), Glob, Bash(${CLAUDE_PLUGIN_ROOT}/hooks/jevflow validate:*)
---

Create a Jevflow flow for this project. The user's goal:

$ARGUMENTS

If the goal above is empty, ask the user for it in one question and stop.

Steps:

1. Look at the project briefly (layout, test runner, build tool) so phase checks use commands that exist here.
2. Write `.jevflow/flow.json`. If one already exists, show it and ask before replacing it.
3. Validate it by running `"${CLAUDE_PLUGIN_ROOT}/hooks/jevflow" validate` and fix any error it reports.
4. Show the user the phase list and tell them the flow starts in `warn` mode: Jevflow reports what it would block but does not block. They switch to `"mode": "enforce"` when they trust it.

Flow format (unknown keys are rejected):

```json
{
  "schema_version": 1,
  "flow_version": "1",
  "goal": "one sentence, concrete and checkable",
  "mode": "warn",
  "phases": [
    {"id": "scaffold", "name": "Project scaffold", "done_when": "package layout and entry point exist",
     "check": "test -f app/cli.py"},
    {"id": "implement", "name": "Implement commands", "done_when": "add, list and done commands work",
     "check": null},
    {"id": "test", "name": "Tests pass", "done_when": "a test suite exists and passes",
     "check": "python -m pytest -q", "loop": {"max_iterations": 4, "until": "python -m pytest -q"},
     "on_fail": "debug"},
    {"id": "debug", "name": "Debug failures", "done_when": "the cause of the failing tests is fixed",
     "check": null}
  ],
  "limits": {"max_blocks_per_session": 6, "max_restarts": 5, "max_total_minutes": 90},
  "privacy": {"send_diff": false}
}
```

Rules for a good flow:

- 3 to 7 phases. `id` is lowercase letters, digits, `-` or `_`, and never `unclear`.
- `done_when` is one observable sentence. Jev judges against it.
- Give a `check` (shell command, exit 0 = pass) wherever one exists. Deterministic checks always outrank Jev, and a phase with a check cannot be marked done while it fails.
- Phases run in declaration order unless a phase sets `depends_on: ["id", ...]`.
- Use `loop` only for a fix-and-retest phase; it is bounded by `max_iterations`.
- `on_fail` names a phase to route to when this phase's check fails after an attempt. A phase that is only an `on_fail` target is a side branch and is not required for the goal.
- Keep `privacy.send_diff` false unless the user asks otherwise: then Jev sees file names and line counts, never file contents. Check output and the last assistant message are sent.
