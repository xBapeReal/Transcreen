"""The one-file build: generated artifact imports every embedded module."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_onefile_builds_and_all_modules_import(tmp_path):
    out = tmp_path / "Transcreen.py"
    build = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "build_onefile.py"), "--out", str(out)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert build.returncode == 0, build.stderr
    assert out.exists()

    env = {**os.environ, "PYTHONUTF8": "1"}
    env.pop("PYTHONPATH", None)  # the file must not lean on the dev src/ path
    # Run from an unrelated cwd: the artifact must be location-independent.
    verify = subprocess.run(
        [sys.executable, str(out), "--verify-embed"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        env=env,
        cwd=str(tmp_path),
    )
    assert verify.returncode == 0, verify.stdout + verify.stderr
    assert "EMBED OK" in verify.stdout
