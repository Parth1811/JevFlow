---
description: Open the Jevflow viewer, a live page with every flow on this computer (phases, agents, decisions)
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/hooks/jevflow ui:*)
---

Start the live viewer (it keeps running in the background and lists the flows of every folder Jevflow has seen, so the user does not have to find the folder):

!`"${CLAUDE_PLUGIN_ROOT}/hooks/jevflow" ui --background --open 2>&1`

Also write a self-contained snapshot of this folder's flows:

!`"${CLAUDE_PLUGIN_ROOT}/hooks/jevflow" ui --here --export .jevflow/view.html 2>&1 || true`

Then tell the user, in one or two lines:

- If the viewer started, give them its `http://127.0.0.1:...` URL (it may already have opened in their browser). It stays up until `jevflow ui --stop`.
- If you are running in a sandbox the user's browser cannot reach (Claude Cowork, a remote or container session), the 127.0.0.1 URL will not work for them. Instead open or share `.jevflow/view.html` with the user (it is a single HTML file that works offline), and mention that running `jevflow ui` in a terminal on their computer shows every flow live.
- If both commands failed because there is no flow yet, say so and offer to start one.

The viewer is read-only. Do not change any other files.
