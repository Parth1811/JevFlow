#!/usr/bin/env bash
# Offline smoke test for auto-planning + multiple flows + done/ archive.
# Drives the real hook launcher with the JSON Claude Code would send, so it
# needs no Claude and no Jev key. Usage: examples/auto/smoke.sh [DIR]
set -euo pipefail
J="$(cd "$(dirname "$0")/../.." && pwd)/hooks/jevflow"
DIR="${1:-$(mktemp -d /tmp/jevflow-auto-XXXX)}"
mkdir -p "$DIR" && cd "$DIR"
export JEVFLOW_NO_JEV=1        # checks-only: no Jev calls
PY="${JEVFLOW_PYTHON:-$(command -v python3.11 || command -v python3)}"
hook() {  # hook EVENT SESSION [PROMPT]
  printf '{"cwd":"%s","session_id":"%s","prompt":%s}' "$DIR" "$2" \
    "$(printf '%s' "${3:-}" | "$PY" -c 'import json,sys;print(json.dumps(sys.stdin.read()))')" \
    | timeout 30 "$J" hook "$1"
  echo
}
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

step "1. turn auto-planning on"
"$J" auto on --project .

step "2. session A sends a task prompt -> a draft flow + planning instructions"
out=$(hook UserPromptSubmit A "Add a --verbose flag to the CLI, cover it with tests and document it"); echo "${out:0:400} ..."
step "   session B sends another task in parallel -> a second flow"
hook UserPromptSubmit B "Write a migration script that renames the user table and backfills ids" >/dev/null
"$J" flows --project .

step "3. A tries to stop before laying out phases -> blocked"
out=$(hook Stop A); echo "${out:0:220} ..."

step "4. 'Claude' lays out A's flow (one phase, check passes once hello.txt exists)"
FA=$(cat .jevflow/sessions/A)   # the flow session A is bound to
cat > ".jevflow/flows/$FA/flow.json" <<EOF
{"schema_version": 1, "goal": "Add a --verbose flag", "mode": "enforce",
 "phases": [{"id": "build", "name": "Build", "done_when": "hello.txt exists", "check": "test -f hello.txt"}]}
EOF
"$J" validate --project . --flow "$FA"

step "5. A stops with the work unfinished -> blocked by the phase check"
out=$(hook Stop A); echo "${out:0:220} ..."

step "6. work done, A stops again -> goal complete, flow moved to .jevflow/done/"
touch hello.txt
hook Stop A
"$J" flows --project .
echo; cat ".jevflow/done/$FA/SUMMARY.md"

step "7. status line for each session"
printf '{"workspace":{"project_dir":"%s"},"session_id":"A"}' "$DIR" | NO_COLOR=1 "$J" statusline
printf '{"workspace":{"project_dir":"%s"},"session_id":"B"}' "$DIR" | NO_COLOR=1 "$J" statusline
echo; echo "playground: $DIR"
