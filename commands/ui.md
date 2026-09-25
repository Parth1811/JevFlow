---
description: Open the Jevflow viewer (phase graph, counters, decision timeline) for this project
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/hooks/jevflow ui:*)
---

Set up the Jevflow viewer for this project:

!`"${CLAUDE_PLUGIN_ROOT}/hooks/jevflow" ui --launch-json`

If that succeeded and you are running in the Claude Code desktop app, start the `jevflow` preview server so the viewer opens in the Browser pane. Otherwise tell the user to run `"${CLAUDE_PLUGIN_ROOT}/hooks/jevflow" ui --open` in a terminal (live viewer on 127.0.0.1), or `ui --export jevflow-report.html` for a self-contained snapshot. The viewer is read-only. Do not change any other files.
