import os

import audiodevice as ad

from pathlib import Path
from typing import Optional

current_file = Path(__file__).resolve()
engine_path = current_file.parent.parent / "audiodevice.exe"
ENGINE_EXE = str(engine_path)


def _pick_device(devs: list[dict], prefer: list[str]) -> str:
    for p in prefer:
        p_lc = p.lower()
        for d in devs:
            name = str(d.get("name", ""))
            if p_lc in name.lower():
                return name
    return ""


def main() -> None:
    ad.default.auto_start = True
    if engine_path.is_file():
        ad.default.engine_exe = ENGINE_EXE
        ad.default.engine_cwd = os.path.dirname(ENGINE_EXE)

    wav_path = os.path.join(os.path.dirname(__file__), "rec_monitor.wav")

    # Same style as demo_rec.py
    ad.default.hostapi = "ASIO"
    ad.default.samplerate = 48_000
    ad.default.channels = 2
    # ad.default.device_in = "ASIO4ALL"
    ad.default.device_in = "UMC ASIO Driver"
    # For ASIO, monitor/duplex typically expects the same device.
    ad.default.device_out = ad.default.device_in

    print("hostapi:", ad.default.hostapi)
    print("device_in:", ad.default.device_in or "<default>")
    print("device_out:", ad.default.device_out or "<default>")

    x = ad.rec_monitor(
        10.0,  # seconds
        save_wav=True,
        wav_path=wav_path,
        blocking=True,
        samplerate=48_000,
        channels=2,
    )
    print("captured:", x.shape, x.dtype, "wav:", wav_path)


if __name__ == "__main__":
    main()

