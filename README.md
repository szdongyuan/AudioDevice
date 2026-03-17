# audiodevice (Windows + Rust engine + Python SDK)

This repository contains a **Windows-only** audio engine:

- **Rust resident engine**: `audiodevice.exe` (TCP JSON-lines control)
- **Python SDK**: `audiodevice_py/` (audiodevice-like API; all audio goes through Rust)

For end users who received prebuilt packages, see:

- `dist/INSTALL.md`
- `dist/INSTALL_zh_CN.md`
- `dist/API_USAGE.md`

## Architecture

Python user code
→ `audiodevice` Python SDK
→ TCP JSON
→ `audiodevice.exe`
→ Backend abstraction
→ CPAL / PortAudio

## Build (Rust)

### CPAL backend (default)

```powershell
cd audio_engine
cargo build --release
```

The engine binary will be at:

- `audio_engine\target\release\audiodevice.exe`

### CPAL + ASIO backend (optional)

Your current engine reports `cpal` hostapis as `["WASAPI"]` only, which means it was built **without** CPAL's `asio` feature.  
To make ASIO show up in `ad.query_hostapis_raw()["by_backend"]["cpal"]`, rebuild the engine with:

```powershell
cd audio_engine
cargo build --release --features cpal_asio
```

Notes (Windows):

- You need **LLVM/Clang** available for `bindgen`. If build fails, install LLVM and set `LIBCLANG_PATH` (pointing to LLVM `bin`).
- If you already have an ASIO SDK locally, you can set `CPAL_ASIO_DIR` to its path; otherwise CPAL may try to download it during build (subject to Steinberg licensing/availability).
- At runtime you still need an ASIO driver installed (e.g. vendor ASIO driver / ASIO4ALL).

### PortAudio backend (optional feature)

The PortAudio backend requires **`portaudio.lib`** (MSVC import library) at link time and
**`portaudio.dll`** at runtime.

1. Put artifacts under:

- `audio_engine/third_party/portaudio/lib/portaudio.lib`
- `audio_engine/third_party/portaudio/bin/portaudio.dll`

2. Build with feature（stable 方案：先构建到 `target/release`，再复制到 Python 包的 `audiodevice/bin/`）:

```powershell
cd audio_engine
cargo build --release --features portaudio_backend
```

然后将引擎产物放到 Python 包目录（推荐直接运行 `audiodevice_py/audiodevice/build_engine.ps1` 自动完成；脚本也会尝试从 vcpkg 目录自动定位 `portaudio.lib` / `portaudio.dll`）：

- `audio_engine\target\release\audiodevice.exe` → `audiodevice_py\audiodevice\bin\`
- （可选）`audio_engine\third_party\portaudio\bin\portaudio.dll` → `audiodevice_py\audiodevice\bin\`

> Note: PortAudio can expose multiple Windows Host APIs (MME/DirectSound/WASAPI/ASIO) depending on
> how you build the DLL.

## Package engine ZIP (for AUDIODEVICE_ENGINE_URL)

This repo includes a helper script to create a distribution ZIP containing:

- `audiodevice.exe`
- (optional) `portaudio.dll`
- `README_ENGINE.md` + `API_USAGE.md`

Run:

```powershell
powershell -ExecutionPolicy Bypass -File .\dist\package_engine.ps1
```

Notes:

- The script packages binaries from `audiodevice_py\audiodevice\bin\` if present (wheel-like layout),
  otherwise it falls back to `audio_engine\target\release\`.
- Optional args:
  - `-BinDir <dir>`: explicitly specify the folder containing `audiodevice.exe` / `portaudio.dll`
  - `-OutZip <path>`: set output zip base name (timestamp is always appended)

## Run (engine)

```powershell
audio_engine\target\release\audiodevice.exe
```

Default TCP address: `127.0.0.1:18789`.

## Install (Python SDK)

```powershell
python -m pip install -e audiodevice_py
```

### Engine auto-discovery / auto-download

Recommended: call `ad.init()`, which enables auto-start and warms up device enumeration.

Resolution order for locating `audiodevice.exe`:

- Use `ad.default.engine_exe` if it’s an existing file (absolute/relative path)
- Otherwise, try to find `audiodevice.exe` on `PATH`
- Otherwise, use the engine bundled in the wheel (if present)
- Otherwise, if running from this monorepo, try `audio_engine/target/release/audiodevice.exe`
- Otherwise, auto-download / auto-install if configured:
  - Set env `AUDIODEVICE_ENGINE_URL` to a local path or HTTP(S) URL ending with `.zip` or `.exe`
  - (Optional) set env `AUDIODEVICE_ENGINE_SHA256` for integrity verification
  - Or set `ad.default.engine_download_url` / `ad.default.engine_sha256`

Engine ZIPs are unpacked to the cache directory by default:

- `%LOCALAPPDATA%\audiodevice\engine\`

## Python usage

```python
import audiodevice as ad
import time

ad.init()
print(ad.query_backends())
ad.print_default_devices()

# Host API is derived from selected devices (read-only).
# Pick a host API by selecting its default devices:
hs = ad.query_hostapis()
target = "Windows WASAPI"  # or "ASIO" / "MME" / "DirectSound"
h = next((x for x in hs if x["name"] == target), hs[0])
ad.default.device = (h["default_input_device"], h["default_output_device"])

# Or pick devices by *global index* from `ad.query_devices()` output:
# ad.default.device = (0, 1)

ad.default.samplerate = 48000
ad.default.channels = 2

y = ad.rec(3.0, blocking=True)
ad.play(y, blocking=True)
y2 = ad.playrec(y, blocking=True)

h = ad.rec_long("long.wav", rotate_s=300)
time.sleep(10)
h.stop()
```

## TCP JSON protocol (high level)

All commands are **JSON-lines** (one JSON object per line, UTF-8).

- `list_backends`
- `list_hostapis`
- `list_devices`
- `session_start`
- `session_stop`
- `capture_read` → base64 PCM16 (interleaved)
- `play_write` / `play_finish`

## Examples

See:

- `audiodevice_py/examples/demo_rec.py`
- `audiodevice_py/examples/demo_play.py`
- `audiodevice_py/examples/demo_playrec.py`

