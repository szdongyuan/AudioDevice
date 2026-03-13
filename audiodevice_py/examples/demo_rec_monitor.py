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
    # 选择要监听的输入通道（1-based）：1=CH1, 2=CH2...
    # 多通道声卡常见场景：你可能只想听某一路输入，而不是总听 CH1。
    MONITOR_CH = 2

    # Same style as demo_rec.py. device_in / device_out accept only int. hostapi is read-only (follows from device_in/device_out).
    ad.default.samplerate = 48_000
    ad.default.channels = (6,2)
    ad.default.device = (22,30)
    IN_CH = int(ad.default.channels.input or 1)

    print("hostapi:", ad.default.hostapi)
    print("device_in:", ad.default.device_in)
    print("device_out:", ad.default.device_out)
    print("device:", ad.default.device)

    x = ad.rec_monitor(
        10.0,  # seconds
        save_wav=True,
        wav_path=wav_path,
        blocking=True,
        monitor_channel=MONITOR_CH,
        samplerate=48_000,
        channels=IN_CH,
    )
    print("captured:", x.shape, x.dtype, "wav:", wav_path)


if __name__ == "__main__":
    main()

