"""
Build wheel with version and timestamp set from current date/time.
Run from project root: python build_wheel.py

Before building, ensures audiodevice/bin/ contains audiodevice.exe and portaudio.dll
(by copying from audio_engine/target/release/ or extracting from engine.zip if present),
so the resulting wheel is self-contained and pip install deploys the engine automatically.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
PYPROJECT = PROJECT_ROOT / "pyproject.toml"
BIN_DIR = PROJECT_ROOT / "audiodevice" / "bin"
RELEASE_DIR = PROJECT_ROOT / ".." / "audio_engine" / "target" / "release"
ENGINE_EXE = "audiodevice.exe"
ENGINE_DLL = "portaudio.dll"


def prepare_bin_artifacts() -> None:
    """Ensure audiodevice/bin/ has audiodevice.exe and portaudio.dll before building the wheel.

    Tries in order:
    1. Use existing files in audiodevice/bin/ if both are present.
    2. Copy from audio_engine/target/release/ if missing.
    3. Extract from engine.zip in project root or in audiodevice/ if present.
    """
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    exe_dst = BIN_DIR / ENGINE_EXE
    dll_dst = BIN_DIR / ENGINE_DLL
    release = RELEASE_DIR.resolve()

    def copy_if(src: Path, dst: Path, name: str) -> bool:
        if not src.is_file():
            return False
        shutil.copy2(src, dst)
        print(f"  Copied {name} from {src.parent}")
        return True

    need_exe = not exe_dst.is_file()
    need_dll = not dll_dst.is_file()
    if not need_exe and not need_dll:
        print("  audiodevice/bin/ already has exe and dll, skipping prepare.")
        return

    # Prefer copying from audio_engine/target/release/
    if need_exe:
        copy_if(release / ENGINE_EXE, exe_dst, ENGINE_EXE)
        need_exe = not exe_dst.is_file()
    if need_dll:
        copy_if(release / ENGINE_DLL, dll_dst, ENGINE_DLL)
        need_dll = not dll_dst.is_file()

    # If still missing, try extracting from engine.zip
    for zip_path in (PROJECT_ROOT / "engine.zip", PROJECT_ROOT / "audiodevice" / "engine.zip"):
        if not zip_path.is_file():
            continue
        if not need_exe and not need_dll:
            break
        print(f"  Extracting from {zip_path.name} into audiodevice/bin/")
        with zipfile.ZipFile(zip_path, "r") as z:
            found_exe = found_dll = None
            for info in z.infolist():
                if info.is_dir():
                    continue
                fn = Path(info.filename).name
                if fn.lower() == ENGINE_EXE:
                    found_exe = info
                elif fn.lower() == ENGINE_DLL:
                    found_dll = info
            if need_exe and found_exe:
                z.extract(found_exe, BIN_DIR)
                extracted = BIN_DIR / found_exe.filename
                if extracted != exe_dst:
                    shutil.move(str(extracted), str(exe_dst))
                need_exe = False
            if need_dll and found_dll:
                z.extract(found_dll, BIN_DIR)
                extracted = BIN_DIR / found_dll.filename
                if extracted != dll_dst:
                    shutil.move(str(extracted), str(dll_dst))
                need_dll = False
        # Remove any extracted intermediate dirs
        for p in list(BIN_DIR.iterdir()):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
        if not need_exe and not need_dll:
            break

    if need_exe or need_dll:
        missing = ([ENGINE_EXE] if need_exe else []) + ([ENGINE_DLL] if need_dll else [])
        print(f"  Warning: missing in audiodevice/bin/: {missing}. Wheel may run without bundled engine.")
    else:
        print("  audiodevice/bin/ prepared; wheel will include engine exe and dll.")
    return None


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
    print("Preparing engine binaries for wheel (audiodevice/bin/)...")
    prepare_bin_artifacts()
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
