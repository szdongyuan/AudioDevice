# audiodevice Engine Package (Windows) — User Notes

This ZIP contains the **engine executable** used by the `audiodevice` Python package.
Most users do **not** need to run the EXE manually — the Python SDK can start it automatically.

## What’s inside

- `audiodevice.exe` (required)
- `portaudio.dll` (optional; only needed for some backends/builds)
- `README_ENGINE.md` (this file)
- `API_USAGE.md` (how to use the Python SDK)

## Setup: use environment variable to point to this ZIP

The Python SDK finds the engine by reading the `AUDIODEVICE_ENGINE_URL` environment variable. Follow the steps below.

### Step 1 — Install the Python SDK wheel

```powershell
python -m pip install C:\path\to\audiodevice-<version>-py3-none-any.whl
```

### Step 2 — Put this ZIP in a fixed folder

1. Choose a folder you won’t delete (e.g. `C:\tools\audiodevice`).
2. In **PowerShell**, create the folder and copy the ZIP (replace paths with your actual download location and ZIP name):

```powershell
mkdir C:\tools\audiodevice -Force
copy "C:\YourDownloads\audiodevice_engine_win64_xxx.zip" "C:\tools\audiodevice\"
```

3. Note the **full path** to the ZIP (e.g. `C:\tools\audiodevice\audiodevice_engine_win64_20260305.zip`). You’ll need it for the next step.

### Step 3 — Add the environment variable `AUDIODEVICE_ENGINE_URL` (permanent, set once)

The variable tells the SDK where the engine ZIP is. Set it **permanently** using one of the methods below so you don’t need to configure it again.

- **Using the GUI**  
  1. Press `Win + R`, type `sysdm.cpl`, press Enter to open System Properties.  
  2. Open the **Advanced** tab → click **Environment Variables**.  
  3. Under **User variables** (or System variables), click **New**.  
  4. Variable name: `AUDIODEVICE_ENGINE_URL`  
  5. Variable value: the full path to the ZIP (e.g. `C:\tools\audiodevice\audiodevice_engine_win64_20260305.zip`).  
  6. OK to save. **New** PowerShell or Command Prompt windows will see the variable; close and reopen any already-open terminals.

- **Using PowerShell (permanent user variable)**  
  In PowerShell (replace with your actual ZIP path):

```powershell
[Environment]::SetEnvironmentVariable("AUDIODEVICE_ENGINE_URL", "C:\tools\audiodevice\audiodevice_engine_win64_xxx.zip", "User")
```

Then open a **new** PowerShell window before running Python.

### Step 4 — Optional: SHA256 verification

If you were given a SHA256 for the engine ZIP, you can set it permanently so the SDK verifies the archive before use:

- **GUI**: New variable `AUDIODEVICE_ENGINE_SHA256` with the given value.
- **PowerShell**: `[Environment]::SetEnvironmentVariable("AUDIODEVICE_ENGINE_SHA256", "<sha256>", "User")`

You can skip this if no SHA256 was provided.

### Step 5 — Where the engine is used

After `AUDIODEVICE_ENGINE_URL` is set, the SDK will unpack the ZIP into the cache directory when needed (default):

- `%LOCALAPPDATA%\audiodevice\engine\`  
  (e.g. `C:\Users\YourName\AppData\Local\audiodevice\engine\`)

No need to extract the ZIP there yourself.

### Step 6 — Quick test

```powershell
python -c "import audiodevice as ad; ad.init(); print(ad.query_backends())"
```

If you see a list of backends, the engine is working.

---

## Troubleshooting (common user issues)

- **Engine not found / cannot start**: Make sure `AUDIODEVICE_ENGINE_URL` is set to the **full path** of the engine ZIP. If you just set it permanently, **close and reopen** PowerShell or your IDE before running Python again.
- **Firewall prompt**: allow `audiodevice.exe` to communicate on local loopback (127.0.0.1).
- **`portaudio.dll`**:
  - If your ZIP includes `portaudio.dll`, keep it in the same folder as `audiodevice.exe` (or add that folder to PATH).
  - If you don’t have it, most WASAPI/ASIO use-cases still work (depending on your engine build).

