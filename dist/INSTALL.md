# audiodevice Installation (User Guide)

This guide is for end users who received the prebuilt packages (no source checkout, no compilation).

## What you need

- **OS**: Windows 10/11 (x64)
- **Python**: Already installed (Python 3.10+ recommended)
- **You only need one file**:
  - `audiodevice-<version>-py3-none-any.whl` (Python SDK wheel, **bundles the engine**)

## 1) Install the Python SDK (wheel)

```powershell
python -m pip install C:\path\to\audiodevice-<version>-py3-none-any.whl
```

## 2) Quick test

```powershell
python -c "import audiodevice as ad; ad.init(); print(ad.query_backends()); print(ad.query_devices())"
```

If you see a backend list and a device list, installation is OK.
