"""`python -m factory.cli doctor`: check this machine can run the factory, and show the real error if not."""
from __future__ import annotations

import importlib.util
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .core.platform import IS_WINDOWS, TEXT, rmtree


def _line(ok: bool, name: str, detail: str) -> bool:
    print(f"  [{'ok' if ok else 'FAIL'}] {name:<22} {detail}")
    return ok


def main() -> int:
    print(f"factory doctor — {platform.system()} {platform.release()}, Python {sys.version.split()[0]}")
    print(f"  interpreter: {sys.executable}")
    ok = True
    ok &= _line(sys.version_info >= (3, 11), "python >= 3.11", sys.version.split()[0])
    if IS_WINDOWS and "WindowsApps" in sys.executable:
        ok &= _line(False, "python build", "Microsoft Store Python restricts child processes; install from python.org")
    for mod in ("yaml", "jsonschema", "cryptography", "starlette", "uvicorn", "pytest"):
        found = importlib.util.find_spec(mod) is not None
        ok &= _line(found, f"module {mod}", "installed" if found else f"missing: pip install {'pyyaml' if mod == 'yaml' else mod}")
    git = shutil.which("git")
    ok &= _line(bool(git), "git on PATH", git or ("missing: install Git for Windows" if IS_WINDOWS else "missing"))
    if git:
        v = subprocess.run([git, "--version"], capture_output=True, **TEXT)
        _line(v.returncode == 0, "git version", v.stdout.strip() or v.stderr.strip())
    if IS_WINDOWS:
        sh = shutil.which("sh")
        _line(True, "sh for git hooks", sh or "not on PATH (Git for Windows still finds its own sh)")

    # smoke: run one test inside the dev sandbox exactly as delivery does
    from .core.db import DB
    from .core.evidence import Ledger, ObjectStore
    from .delivery.sandbox import SandboxService
    tmp = Path(tempfile.mkdtemp(prefix="factory-doctor-"))
    try:
        led = Ledger(DB(tmp / "d.db"), ObjectStore(tmp / "o"))
        sb = SandboxService(tmp / "sbx", led).create("doctor", "smoke", 0)
        (sb.work / "tests" / "unit").mkdir(parents=True)
        (sb.work / "tests" / "unit" / "test_smoke.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n",
                                                                  encoding="utf-8")
        r = sb.pytest(["tests/unit"])
        last = (r.stdout.strip().splitlines() or [""])[-1]
        passed = _line(r.code == 0, "sandbox pytest smoke", last or f"exit code {r.code}")
        if not passed:
            print("\n  --- sandbox stderr (last 20 lines) ---")
            for ln in r.stderr.strip().splitlines()[-20:]:
                print("  " + ln)
            print("  --- sandbox stdout (last 10 lines) ---")
            for ln in r.stdout.strip().splitlines()[-10:]:
                print("  " + ln)
        ok &= passed
    except Exception as e:                       # report, don't hide
        ok &= _line(False, "sandbox pytest smoke", f"{type(e).__name__}: {e}")
    finally:
        rmtree(tmp, ignore_errors=True)
    print("\nall checks passed" if ok else "\nfix the FAIL lines above, then run the demo again")
    return 0 if ok else 1
