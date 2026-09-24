#!/bin/sh
# Append the hook's stdin JSON to a per-event log next to this plugin. Always exit 0.
evt="$1"
dir="$(dirname "$0")/../../logs"
mkdir -p "$dir"
{ cat; echo; } >> "$dir/$evt.jsonl" 2>/dev/null
exit 0
