# audiodevice Engine Package (Windows) — User Notes

This ZIP contains the **engine executable** used by the `audiodevice` Python package.
Most users do **not** need to run the EXE manually — the Python SDK can start it automatically.

## What’s inside

- `audiodevice.exe` (required)
- `portaudio.dll` (optional; only needed for some backends/builds)
- `README_ENGINE.md` (this file)
- `API_USAGE.md` (how to use the Python SDK)

## Recommended setup (let Python auto-start the engine)

1) Install the Python SDK wheel:

```powershell
python -m pip install C:\path\to\audiodevice-<version>-py3-none-any.whl
```

2) Put this ZIP somewhere stable, for example:

```powershell
mkdir C:\tools\audiodevice
copy C:\Downloads\audiodevice_engine_win64_*.zip C:\tools\audiodevice\
```

3) Point the SDK to the ZIP (current PowerShell session):

```powershell
$env:AUDIODEVICE_ENGINE_URL="C:\tools\audiodevice\audiodevice_engine_win64_xxx.zip"
```

Optional SHA256 verification (only if provided to you):

```powershell
$env:AUDIODEVICE_ENGINE_SHA256="<sha256>"
```

4) Quick test:

```powershell
python -c "import audiodevice as ad; ad.default.auto_start=True; print(ad.query_backends())"
```

When using `AUDIODEVICE_ENGINE_URL`, the engine is installed to the cache directory (default):

- `%LOCALAPPDATA%\audiodevice\engine\`

## Alternative setup (use an extracted EXE directly)

Extract the ZIP, then in Python:

```python
import audiodevice as ad
ad.default.auto_start = True
ad.default.engine_exe = r"C:\tools\audiodevice\audiodevice.exe"
ad.default.engine_cwd = r"C:\tools\audiodevice"
```

## Troubleshooting (common user issues)

- **Firewall prompt**: allow `audiodevice.exe` to communicate on local loopback (127.0.0.1).
- **`portaudio.dll`**:
  - If your ZIP includes `portaudio.dll`, keep it in the same folder as `audiodevice.exe` (or add that folder to PATH).
  - If you don’t have it, most WASAPI/ASIO use-cases still work (depending on your engine build).

