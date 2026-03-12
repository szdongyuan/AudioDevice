# audiodevice Installation (User Guide)

This guide is for end users who received the prebuilt packages (no source checkout, no compilation).

## What you need

- **OS**: Windows 10/11 (x64)
- **Python**: Already installed (Python 3.10+ recommended)
- **Recommended**: one file
  - `audiodevice-<version>-py3-none-any.whl` (Python SDK wheel, usually **bundles the engine**)
- **Optional** (only if your wheel does **not** bundle the engine):
  - `audiodevice_engine_win64_<timestamp>.zip` (engine ZIP: `audiodevice.exe` + optional `portaudio.dll` + docs)

## 1) Install the Python SDK (wheel)

```powershell
python -m pip install C:\path\to\audiodevice-<version>-py3-none-any.whl
```

## 2) Engine setup (usually NOT needed)

If your wheel bundles the engine, you can skip this step and go to the quick test.

### How to tell if the wheel bundles the engine

After installing the wheel, run:

```powershell
python -c "import audiodevice as ad; import os; from importlib import resources as r; p=r.files('audiodevice').joinpath('bin','audiodevice.exe'); print('bundled_exe=', os.fspath(p), 'exists=', p.is_file())"
```

If `exists=True`, the engine is already included and no ZIP / PATH setup is required.

### If the wheel does NOT bundle the engine

Pick one option below (**Option A is recommended**).

#### Option A (recommended): point to the ZIP (auto-install to cache)

1) Put the ZIP in a stable location:

```powershell
mkdir C:\tools\audiodevice
copy C:\Downloads\audiodevice_engine_win64_*.zip C:\tools\audiodevice\
```

2) Set env var (for the current PowerShell session):

```powershell
$env:AUDIODEVICE_ENGINE_URL="C:\tools\audiodevice\audiodevice_engine_win64_xxx.zip"
```

Optional integrity check (if you were given a SHA256):

```powershell
$env:AUDIODEVICE_ENGINE_SHA256="<sha256>"
```

The engine will be installed to the cache directory (default):

- `%LOCALAPPDATA%\audiodevice\engine\`

#### Option B: put `audiodevice.exe` on PATH

Extract the ZIP, then add the folder containing `audiodevice.exe` to your PATH.

#### Option C: set the engine path in your code

Call `ad.init()` with the engine path:

```python
import audiodevice as ad
ad.init(
    engine_exe=r"C:\tools\audiodevice\audiodevice.exe",
    engine_cwd=r"C:\tools\audiodevice",
)
```

## 3) Quick test

```powershell
python -c "import audiodevice as ad; ad.init(); print(ad.query_backends()); print(ad.query_devices())"
```

If you see a backend list and a device list, installation is OK.

## Troubleshooting

- **Engine not found / cannot start**:
  - If your wheel does not bundle the engine, use Option A (`AUDIODEVICE_ENGINE_URL`) first
  - Or make sure `audiodevice.exe` is on PATH
- **First run is slow**: expected (engine start + device enumeration). Next calls are faster.
- **Windows Firewall prompt**: allow local loopback (127.0.0.1) communication for `audiodevice.exe`.
- **About `portaudio.dll`**:
  - Most users only need WASAPI/ASIO (CPAL) → `portaudio.dll` is typically **not** needed
  - If you do have `portaudio.dll`, keep it next to `audiodevice.exe` (or on PATH)

