#!/bin/sh
# Block the first Stop only (marker file), to observe stop_hook_active on the retry. Always exit 0.
m="$(dirname "$0")/../../logs/.blocked_once"
cat > /dev/null
if [ ! -f "$m" ]; then
  touch "$m"
  printf '{"decision":"block","reason":"jevflow spike: reply with the word AGAIN, then stop."}\n'
fi
exit 0
