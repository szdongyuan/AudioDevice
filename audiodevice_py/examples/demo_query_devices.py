import json
import os

import audiodevice as ad
from pathlib import Path

current_file = Path(__file__).resolve()
engine_path = current_file.parent.parent / "audiodevice.exe"
ENGINE_EXE = str(engine_path)


def _pretty(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def main() -> None:
    # Auto start the Rust engine (optional).
    ad.default.auto_start = True
    if engine_path.is_file():
        ad.default.engine_exe = ENGINE_EXE
        ad.default.engine_cwd = os.path.dirname(ENGINE_EXE)

    ad.init()

    # hostapi is read-only; set device to an ASIO device to use ASIO as default.
    idx = ad.device_index_for_hostapi("ASIO", "input")
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