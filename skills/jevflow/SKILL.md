---
name: jevflow
description: How to work inside a Jevflow-tracked session. Use when a Jevflow context block or a "[jevflow]" Stop instruction appears, when the user asks to keep a long task on track toward a goal, or when they mention flow.json, phases, or the Jevflow supervisor.
---

# Working with Jevflow

Jevflow tracks a session against a flow in `.jevflow/flow.json`: a goal and ordered phases, each with a `done_when` sentence and often a shell `check`. When you try to stop, a Stop hook runs the checks, asks Jev (a calibrated judgment model) where the work is, and a fixed policy decides whether you may stop.

## When a `[jevflow]` instruction blocks your stop

- Treat it as the next instruction. Do the concrete thing it names for the named phase.
- If it quotes failing check output, fix the cause shown there. Run the check command yourself before claiming the phase is done.
- "You said the work is done, but it is not" means a check still fails. Do not repeat the claim; make the check pass.
- "You are looping" means your last approach is not working. Re-read the goal and change approach, not just parameters.
- "Regression" means a phase that was done now fails its check. Fix that first.
- Never edit `.jevflow/state.json` or `.jevflow/flow.json` to get past a block. Only Jevflow code writes state. Changing the flow is a user decision.

## Starting and checking

- `/jevflow:init <goal>` writes a flow (starts in `warn` mode, which reports but does not block).
- `/jevflow:status` shows the phase table, recent decisions and any pending human question.
- `.jevflow/NEEDS_HUMAN.md` exists when Jevflow decided a human must answer. Stop and surface the question; do not guess.

## What Jev sees

Goal, phase table, check results (with a tail of failing output), the tail of your last message, a change summary (file names and line counts only, unless the flow sets `privacy.send_diff`), and the last few decisions. Keep your final message of each turn a plain, honest summary of what was done and what still fails; that is what gets judged.
