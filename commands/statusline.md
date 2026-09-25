---
description: Show Jevflow progress in Claude Code's terminal status line
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/hooks/jevflow statusline:*), Read, Edit
---

Settings snippet for the Jevflow status line:

!`"${CLAUDE_PLUGIN_ROOT}/hooks/jevflow" statusline --config`

Read `~/.claude/settings.json`. If it has no `statusLine`, show the user the snippet above and offer to add it. If it already has a `statusLine` command, do not replace it: offer to wrap it instead, setting the command to `<jevflow wrapper> statusline --with '<their existing command>'` so their line stays and Jevflow's segment is added below it. Only edit the file after the user agrees. The status line is a terminal feature; in the Claude Code desktop app use `/jevflow:ui` instead.
