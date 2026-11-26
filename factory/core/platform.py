"""Cross-platform helpers for child processes (Linux/macOS and Windows).

Child processes get a minimal environment so secrets never leak into sandboxes or tools. Windows needs a few
system variables (SYSTEMROOT, WINDIR, ...) for Python and git to start at all, and its PATH, because git
(and the `sh` that runs git hooks) live under the Git for Windows install directory.
"""
from __future__ import annotations

import os
import sys

IS_WINDOWS = sys.platform == "win32"
POSIX_PATH = "/usr/local/bin:/usr/bin:/bin"
_WINDOWS_KEEP = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "PROGRAMFILES", "PROGRAMDATA",
                 "LOCALAPPDATA", "APPDATA", "USERPROFILE")
# Decode child-process output as UTF-8 everywhere (Windows otherwise uses the local code page).
TEXT = {"text": True, "encoding": "utf-8", "errors": "replace"}


def child_env(extra: dict | None = None, path: str | None = None) -> dict:
    """Minimal environment for a child process, plus `extra`."""
    if IS_WINDOWS:
        env = {k: os.environ[k] for k in _WINDOWS_KEEP if k in os.environ}
        env["PATH"] = path or os.environ.get("PATH", "")
        env["PYTHONUTF8"] = "1"
    else:
        env = {"PATH": path or POSIX_PATH, "LANG": "C.UTF-8"}
    env.update(extra or {})
    return env


def rmtree(path, ignore_errors: bool = False) -> None:
    """shutil.rmtree that also removes read-only files (git objects, evidence copies) on Windows."""
    import shutil
    import stat

    def _retry(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            if not ignore_errors:
                raise
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_retry)
    else:                                   # Python 3.11 and earlier
        shutil.rmtree(path, onerror=_retry)
