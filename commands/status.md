---
description: Show Jevflow progress (phase table, recent decisions, pending human questions)
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/hooks/jevflow status:*)
---

Jevflow status for this project:

!`"${CLAUDE_PLUGIN_ROOT}/hooks/jevflow" status`

Show the status above to the user as-is in a code block. Then, in at most three sentences, say which phase is current, what the last decision asked for, and whether a human decision (NEEDS_HUMAN) is pending. If the output says there is no flow, suggest `/jevflow:init <goal>`. Do not change any files.
