---
description: Turn Jevflow auto-planning on or off for this project (each task prompt becomes a tracked flow)
argument-hint: "[on|off]"
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/hooks/jevflow auto:*), Bash(${CLAUDE_PLUGIN_ROOT}/hooks/jevflow flows:*)
---

!`"${CLAUDE_PLUGIN_ROOT}/hooks/jevflow" auto $ARGUMENTS --project .`

!`"${CLAUDE_PLUGIN_ROOT}/hooks/jevflow" flows --project .`

Tell the user in one or two sentences whether auto-planning is on. When it is on, every prompt that reads like a task (roughly eight words or more, not a question or a slash command) starts a new flow under `.jevflow/flows/`: Claude lays out the phases first, Jevflow tracks them, and a finished flow moves to `.jevflow/done/<id>/` with a SUMMARY.md. `#nojev` in a prompt skips it; `#jev` forces it. Several flows can be active at once, one per session. Do not change any files.
