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

    # Same style as demo_rec.py. device_in / device_out accept only int. hostapi is read-only (follows from device_in/device_out).
    ad.default.samplerate = 48_000
    ad.default.channels = 2
    # Set by index, e.g. from ad.query_devices() or ad.print_default_devices().
    # ad.default.device_in = 0   # ASIO4ALL
    ad.default.device_in = 0     # UMC ASIO Driver (use your actual index)
    ad.default.device_out = ad.default.device_in

    print("hostapi:", ad.default.hostapi)
    print("device_in:", ad.default.device_in if ad.default.device_in >= 0 else "<default>")
    print("device_out:", ad.default.device_out if ad.default.device_out >= 0 else "<default>")

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

