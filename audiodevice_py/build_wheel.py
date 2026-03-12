"""
Build wheel with version and timestamp set from current date/time.
Run from project root: python build_wheel.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
PYPROJECT = PROJECT_ROOT / "pyproject.toml"


def current_version_string(*, include_time: bool = True) -> str:
    """Version: 0.1.0.postYYYYMMDD or 0.1.0.postYYYYMMDDHHMM."""
    now = datetime.now()
    if include_time:
        suffix = now.strftime("%Y%m%d%H%M")  # e.g. 202603111435
    else:
        suffix = now.strftime("%Y%m%d")  # e.g. 20260311
    return f"0.1.0.post{suffix}"


def update_pyproject_version(new_version: str) -> None:
    content = PYPROJECT.read_text(encoding="utf-8")
    content = re.sub(
        r'^version\s*=\s*["\'][^"\']+["\']',
        f'version = "{new_version}"',
        content,
        count=1,
        flags=re.MULTILINE,
    )
    PYPROJECT.write_text(content, encoding="utf-8")


def main() -> int:
    version = current_version_string(include_time=True)
    print(f"Setting version to {version}")
    update_pyproject_version(version)
    r = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--no-isolation"],
        cwd=PROJECT_ROOT,
        shell=False,
    )
    if r.returncode != 0:
        return r.returncode
    print(f"Built wheel with version {version}")
    print("Install with: python -m pip install dist\\audiodevice-*.whl --force-reinstall")
    return 0


if __name__ == "__main__":
    sys.exit(main())
