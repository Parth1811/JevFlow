# Builder prompt (one cycle)

1. Read `/home/parthvp/Documents/jevflow/loop/state.json`, `docs/SPEC.md`, `TASKS.md`, and the tail of `loop/CHANGELOG.md`.
2. Take the next `batch_size` unchecked items in phase order. If an item depends on an unfinished earlier item, do the earlier one first.
3. Build each item to its acceptance check. Author fresh from SPEC; never fabricate results. Record real measured outputs (Jev probabilities, hook payloads, test counts).
4. Python: `/usr/bin/python3.11`, stdlib only for the package. NEVER use bare `python3` on this host (broken shim that exits 0 silently). Run tests with `/usr/bin/python3.11 -m unittest discover -s tests -v`.
5. Jev key: prefix commands with `JEVFLOW_KEY_FILE=/home/parthvp/jev.txt`. Never print, log, or commit the key. Send only synthetic toy data to Jev.
6. Real `claude -p` runs: cap each at `--max-turns 25`, at most 4 real runs overnight, only inside `/home/parthvp/Documents/jevflow/examples/`. Use `--plugin-dir /home/parthvp/Documents/jevflow`.
7. Stay inside the fence `/home/parthvp/Documents/jevflow`. Never write elsewhere. No destructive commands, no force, no push.
8. If blocked, write `SKIPPED: <reason>` or a BLOCKER line in the changelog and move to the next unblocked item.
