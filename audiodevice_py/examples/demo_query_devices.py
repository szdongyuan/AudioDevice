import json

import audiodevice as ad
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
_engine = _root / "audiodevice.exe"
if _engine.is_file():
    ad.init(engine_exe=str(_engine), engine_cwd=str(_root), timeout=10)
else:
    ad.init(timeout=10)

PREFER_HOSTAPI = "ASIO"


def _pretty(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def main() -> None:
    # hostapi is read-only; set device to an ASIO device to use ASIO as default.
    idx = ad.device_index_for_hostapi(PREFER_HOSTAPI, "input")
    if idx is not None:
        ad.default.device = (idx, idx)

    hostapis = ad.query_hostapis_raw()["hostapis"]
    print("hostapis:", hostapis)

    for hostapi in hostapis:
        try:
            dev_in = ad.query_devices_raw(hostapi=hostapi, direction="input")
        except Exception as e:
            dev_in = {"error": str(e)}

        try:
            dev_out = ad.query_devices_raw(hostapi=hostapi, direction="output")
        except Exception as e:
            dev_out = {"error": str(e)}

        print(f"\n[{hostapi}] input devices:\n{_pretty(dev_in)}")
        print(f"\n[{hostapi}] output devices:\n{_pretty(dev_out)}")


if __name__ == "__main__":
    main()