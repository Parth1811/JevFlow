"""``jevflow install-cli``: put a ``jevflow`` command on the user's PATH.

A marketplace install only gives Claude the hooks; the shell has no
``jevflow``. This links ``<bin dir>/jevflow`` to this plugin's launcher. It is
idempotent and re-points the link when the plugin moves (a plugin update
lands in a new versioned folder). It never edits shell rc files; it reports
when the bin dir is not on PATH so the user (or Claude) can say how to add it.

The SessionStart hook calls ``ensure(quiet=True)`` so the first Claude
session sets it up; ``JEVFLOW_NO_CLI=1`` turns that off.
"""

import os
import sys
from typing import IO, List, Optional, Tuple

LAUNCHER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks", "jevflow")
DEFAULT_BIN = os.path.join("~", ".local", "bin")
NO_CLI_ENV = "JEVFLOW_NO_CLI"


def on_path(bin_dir: str, env: Optional[dict] = None) -> bool:
    path = (env if env is not None else os.environ).get("PATH", "")
    want = os.path.realpath(bin_dir)
    return any(os.path.realpath(os.path.expanduser(p)) == want for p in path.split(os.pathsep) if p)


def ensure(bin_dir: str = DEFAULT_BIN, env: Optional[dict] = None) -> Tuple[str, str]:
    """Create or refresh the link. Returns (status, link_path); status is one of
    created, updated, ok, skipped (a real file owns the name), error."""
    bin_dir = os.path.expanduser(bin_dir)
    link = os.path.join(bin_dir, "jevflow")
    target = os.path.realpath(LAUNCHER)
    try:
        if os.path.islink(link):
            if os.path.realpath(link) == target:
                return "ok", link
            os.remove(link)
            os.symlink(target, link)
            return "updated", link
        if os.path.exists(link):
            return "skipped", link  # never replace a file we did not create
        os.makedirs(bin_dir, exist_ok=True)
        os.symlink(target, link)
        return "created", link
    except OSError:
        return "error", link


def main(argv: List[str], stdout: IO[str], stderr: IO[str]) -> int:
    bin_dir = DEFAULT_BIN
    args = list(argv)
    while args:
        a = args.pop(0)
        if a == "--bin-dir" and args:
            bin_dir = args.pop(0)
        else:
            stderr.write("usage: python -m jevflow install-cli [--bin-dir DIR]   (default ~/.local/bin)\n")
            return 3
    status, link = ensure(bin_dir)
    if status == "skipped":
        stderr.write(f"{link} already exists and is not a link; left it alone\n")
        return 3
    if status == "error":
        stderr.write(f"could not create {link}\n")
        return 3
    stdout.write(f"{status}: {link} -> {os.path.realpath(LAUNCHER)}\n")
    d = os.path.dirname(link)
    if not on_path(d):
        shell = os.path.basename(os.environ.get("SHELL", "sh"))
        rc = {"zsh": "~/.zshrc", "bash": "~/.bashrc"}.get(shell, "your shell profile")
        stdout.write(f"{d} is not on PATH. Add it:  echo 'export PATH=\"{d}:$PATH\"' >> {rc}  "
                     "and open a new shell.\n")
    return 0
