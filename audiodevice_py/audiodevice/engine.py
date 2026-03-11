from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from typing import Optional, Tuple


def _windows_local_appdata_dir() -> str:
    """Return the Windows LOCALAPPDATA directory (best-effort).

    Returns:
        str: Absolute path to LOCALAPPDATA, with a fallback for unusual setups.
    """
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return base
    # Fallback for unusual setups.
    return os.path.join(os.path.expanduser("~"), "AppData", "Local")


def engine_cache_dir() -> str:
    """Return the directory used to cache engine artifacts.

    Returns:
        str: Cache directory path.
    """
    return os.path.join(_windows_local_appdata_dir(), "audiodevice", "engine")


def _sha256_file(path: str) -> str:
    """Compute SHA-256 digest of a file.

    Args:
        path (str): Path to a local file.

    Returns:
        str: Lowercase hex SHA-256 digest.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def bundled_engine_paths() -> Tuple[Optional[str], Optional[str]]:
    """Locate engine binaries bundled with the Python package (wheel).

    Returns:
        tuple[Optional[str], Optional[str]]: `(engine_exe_path, portaudio_dll_path)` if
        present in site-packages, otherwise `(None, None)`.
    """
    try:
        from importlib import resources as importlib_resources  # py3.10+

        base = importlib_resources.files(__package__)
        exe = base.joinpath("bin", "audiodevice.exe")
        dll = base.joinpath("bin", "portaudio.dll")
        exe_path = str(exe) if exe.is_file() else None
        dll_path = str(dll) if dll.is_file() else None
        return exe_path, dll_path
    except Exception:
        return None, None


def dev_engine_path_guess() -> Optional[str]:
    """Guess engine path for editable installs from the monorepo.

    Returns:
        Optional[str]: Absolute path to a dev-built `audiodevice.exe`, or None if not found.
    """
    here = os.path.abspath(os.path.dirname(__file__))
    # audiodevice_py/audiodevice -> ../../audio_engine/target/release/audiodevice.exe
    cand = os.path.abspath(os.path.join(here, "..", "..", "audio_engine", "target", "release", "audiodevice.exe"))
    return cand if os.path.isfile(cand) else None


def ensure_engine_available(
    engine_exe: str,
    *,
    download_url: str = "",
    sha256: str = "",
) -> str:
    """Ensure a runnable engine executable exists and return its absolute path.

    Args:
        engine_exe (str): File name or path to the engine executable (typically
            `"audiodevice.exe"`). If empty, defaults to `"audiodevice.exe"`.
        download_url (str): Optional URL or local file path to a `.zip` or `.exe` engine
            artifact. If empty, falls back to env `AUDIODEVICE_ENGINE_URL`.
        sha256 (str): Optional SHA-256 checksum for the downloaded artifact. If empty,
            falls back to env `AUDIODEVICE_ENGINE_SHA256`.

    Returns:
        str: Absolute path to the resolved engine executable.

    Raises:
        FileNotFoundError: If the engine cannot be found and no download URL is provided.
        RuntimeError: If checksum validation fails or the artifact format is invalid.
    """
    if not engine_exe:
        engine_exe = "audiodevice.exe"

    # Direct path.
    if os.path.isfile(engine_exe):
        return os.path.abspath(engine_exe)

    # PATH lookup.
    which = shutil.which(engine_exe)
    if which:
        return os.path.abspath(which)

    # Bundled wheel assets.
    bundled_exe, _bundled_dll = bundled_engine_paths()
    if bundled_exe:
        return os.path.abspath(bundled_exe)

    # Dev repo guess.
    dev = dev_engine_path_guess()
    if dev:
        return os.path.abspath(dev)

    # Download path.
    url = (os.environ.get("AUDIODEVICE_ENGINE_URL") or "").strip() or download_url.strip()
    if not url:
        raise FileNotFoundError(
            "audiodevice engine not found.\n"
            "- Provide default.engine_exe (absolute path), OR\n"
            "- Put audiodevice.exe on PATH, OR\n"
            "- Set env AUDIODEVICE_ENGINE_URL to a .zip or .exe, OR\n"
            "- Set default.engine_download_url.\n"
        )

    if not sys.platform.startswith("win"):
        raise RuntimeError("audiodevice is Windows-only (engine auto-download supports Windows only).")

    os.makedirs(engine_cache_dir(), exist_ok=True)
    dst_dir = engine_cache_dir()
    dst_exe = os.path.join(dst_dir, "audiodevice.exe")
    dst_dll = os.path.join(dst_dir, "portaudio.dll")

    with tempfile.TemporaryDirectory(prefix="audiodevice_engine_") as td:
        # Allow local file paths for offline / dev usage.
        if os.path.isfile(url):
            tmp = os.path.join(td, os.path.basename(url))
            shutil.copy2(url, tmp)
        else:
            tmp = os.path.join(td, os.path.basename(url.split("?")[0]) or "engine_download")
            urllib.request.urlretrieve(url, tmp)

        want = (os.environ.get("AUDIODEVICE_ENGINE_SHA256") or "").strip() or sha256.strip()
        if want:
            got = _sha256_file(tmp)
            if got.lower() != want.lower():
                raise RuntimeError(f"engine download sha256 mismatch: want={want}, got={got}")

        if tmp.lower().endswith(".zip"):
            with zipfile.ZipFile(tmp, "r") as z:
                z.extractall(td)
            # Find exe/dll.
            found_exe = None
            found_dll = None
            for root, _dirs, files in os.walk(td):
                for fn in files:
                    if fn.lower() == "audiodevice.exe":
                        found_exe = os.path.join(root, fn)
                    if fn.lower() == "portaudio.dll":
                        found_dll = os.path.join(root, fn)
            if not found_exe:
                raise RuntimeError("engine zip does not contain audiodevice.exe")
            shutil.copy2(found_exe, dst_exe)
            if found_dll:
                shutil.copy2(found_dll, dst_dll)
        elif tmp.lower().endswith(".exe"):
            shutil.copy2(tmp, dst_exe)
        else:
            raise RuntimeError("engine download URL must end with .zip or .exe")

    return os.path.abspath(dst_exe)

