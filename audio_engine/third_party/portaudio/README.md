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

If you keep `portaudio.dll` under `third_party/portaudio/bin`, the custom build script will attempt to
copy it next to the built `audiodevice.exe` for local runs.

