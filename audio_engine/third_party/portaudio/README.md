# PortAudio (Windows DLL distribution)

This project supports a PortAudio backend behind the `portaudio_backend` Cargo feature.

## Expected layout

Place your PortAudio artifacts here:

```
audio_engine/third_party/portaudio/
  include/        # PortAudio headers (optional for this Rust build)
  lib/
    portaudio.lib # Import library (MSVC) for linking
  bin/
    portaudio.dll # Runtime DLL (ship next to audiodevice.exe)
```

## Build

- Default build (CPAL only):

```powershell
cd audio_engine
cargo build --release
```

- Build with PortAudio backend:

```powershell
cd audio_engine
cargo build --release --features portaudio_backend
```

Then copy the artifacts for the Python SDK / wheel bundling:

- `audio_engine/target/release/audiodevice.exe` → `audiodevice_py/audiodevice/bin/`
- `audio_engine/third_party/portaudio/bin/portaudio.dll` → `audiodevice_py/audiodevice/bin/` (optional, runtime)

Tip: you can run `audiodevice_py/audiodevice/build_engine.ps1` to build + copy in one step.

