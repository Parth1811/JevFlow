"""``jevflow install-cli``: put a ``jevflow`` command on the user's PATH.

A marketplace install only gives Claude the hooks; the shell has no
``jevflow``. This links ``<bin dir>/jevflow`` to this plugin's launcher. It is
idempotent and re-points the link when the plugin moves (a plugin update
lands in a new versioned folder).

The first Claude session does it on its own (SessionStart calls
``setup()``): it links into the first writable folder already on the user's
login-shell PATH (``~/.local/bin``, ``~/bin``, ``/opt/homebrew/bin``,
``/usr/local/bin``). When none is on PATH it links into ``~/.local/bin`` and
adds one clearly marked block to the shell's rc file so new terminals find
it. ``JEVFLOW_NO_CLI=1`` turns all of this off; ``JEVFLOW_NO_RC=1`` keeps the
link but never touches rc files.
"""

import os
import subprocess
import sys
from typing import IO, List, Optional, Tuple

LAUNCHER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks", "jevflow")
DEFAULT_BIN = os.path.join("~", ".local", "bin")
CANDIDATES = (DEFAULT_BIN, os.path.join("~", "bin"), "/opt/homebrew/bin", "/usr/local/bin")
NO_CLI_ENV = "JEVFLOW_NO_CLI"
NO_RC_ENV = "JEVFLOW_NO_RC"
RC_BEGIN = "# >>> jevflow >>>"
RC_END = "# <<< jevflow <<<"


def on_path(bin_dir: str, env: Optional[dict] = None, path: Optional[str] = None) -> bool:
    if path is None:
        path = (env if env is not None else os.environ).get("PATH", "")
    want = os.path.realpath(os.path.expanduser(bin_dir))
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


def login_path(timeout: float = 4.0) -> Optional[str]:
    """PATH as the user's terminal sees it. Apps launched from a dock or
    launcher (Claude desktop, Cowork) get a much shorter PATH than a terminal,
    so the hook's own PATH is the wrong thing to check."""
    shell = os.environ.get("SHELL") or "/bin/sh"
    try:
        p = subprocess.run([shell, "-ilc", 'printf "\\n__JEVFLOW_PATH__%s\\n" "$PATH"'],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in p.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("__JEVFLOW_PATH__"):
            return line[len("__JEVFLOW_PATH__"):]
    return None


def rc_files(shell: Optional[str] = None) -> List[str]:
    name = os.path.basename(shell or os.environ.get("SHELL") or "")
    if name == "zsh":
        return [os.path.expanduser("~/.zshrc")]
    if name == "bash":
        files = [os.path.expanduser("~/.bashrc")]
        if sys.platform == "darwin":  # Terminal.app starts login shells, which read .bash_profile
            files.append(os.path.expanduser("~/.bash_profile"))
        return files
    return []


def add_to_rc(bin_dir: str, files: Optional[List[str]] = None) -> List[str]:
    """Append one marked PATH block to each rc file that lacks it. Returns the
    files changed. Never rewrites existing content."""
    home = os.path.expanduser("~")
    d = os.path.expanduser(bin_dir)
    shown = "$HOME" + d[len(home):] if d.startswith(home + os.sep) else d
    block = f'\n{RC_BEGIN}\nexport PATH="{shown}:$PATH"\n{RC_END}\n'
    changed = []
    for f in (rc_files() if files is None else files):
        try:
            with open(f, encoding="utf-8") as fh:
                if RC_BEGIN in fh.read():
                    continue
        except FileNotFoundError:
            pass
        except OSError:
            continue
        try:
            with open(f, "a", encoding="utf-8") as fh:
                fh.write(block)
            changed.append(f)
        except OSError:
            continue
    return changed


def setup(env: Optional[dict] = None, path: Optional[str] = None) -> Tuple[str, str, List[str]]:
    """Make ``jevflow`` work in new terminals. Returns (status, link, rc files
    changed). Fast when a link already exists (no shell is started)."""
    env = os.environ if env is None else env
    for c in CANDIDATES:  # already installed: just re-point it after a plugin update
        if os.path.islink(os.path.join(os.path.expanduser(c), "jevflow")):
            status, link = ensure(c)
            if status in ("ok", "updated"):
                return status, link, []
    path = path if path is not None else (login_path() or env.get("PATH", ""))
    for c in CANDIDATES:
        d = os.path.expanduser(c)
        if on_path(d, path=path) and os.path.isdir(d) and os.access(d, os.W_OK):
            status, link = ensure(d)
            if status in ("created", "ok", "updated"):
                return status, link, []
    status, link = ensure(DEFAULT_BIN)
    changed: List[str] = []
    if status in ("created", "ok", "updated") and not on_path(DEFAULT_BIN, path=path) and not env.get(NO_RC_ENV):
        changed = add_to_rc(DEFAULT_BIN)
    return status, link, changed


def main(argv: List[str], stdout: IO[str], stderr: IO[str]) -> int:
    bin_dir = None
    args = list(argv)
    while args:
        a = args.pop(0)
        if a == "--bin-dir" and args:
            bin_dir = args.pop(0)
        else:
            stderr.write("usage: python -m jevflow install-cli [--bin-dir DIR]\n"
                         "  default: the first writable folder on your PATH, else ~/.local/bin plus\n"
                         "  a PATH line in your shell's rc file (JEVFLOW_NO_RC=1 skips that)\n")
            return 3
    if bin_dir is not None:
        status, link = ensure(bin_dir)
        changed: List[str] = []
    else:
        status, link, changed = setup()
    if status == "skipped":
        stderr.write(f"{link} already exists and is not a link; left it alone\n")
        return 3
    if status == "error":
        stderr.write(f"could not create {link}\n")
        return 3
    stdout.write(f"{status}: {link} -> {os.path.realpath(LAUNCHER)}\n")
    for f in changed:
        stdout.write(f"added {os.path.dirname(link)} to PATH in {f}; open a new terminal to use `jevflow`\n")
    d = os.path.dirname(link)
    if not changed and not on_path(d, path=login_path() or None):
        stdout.write(f"{d} is not on PATH. Add it:  echo 'export PATH=\"{d}:$PATH\"' >> ~/.zshrc  "
                     "(or your shell's profile) and open a new shell.\n")
    return 0
