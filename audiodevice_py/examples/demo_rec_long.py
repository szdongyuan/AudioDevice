import os
import time

import audiodevice as ad
from pathlib import Path
from datetime import datetime

current_file = Path(__file__).resolve()
engine_path = current_file.parent.parent / "audiodevice.exe"
ENGINE_EXE = str(engine_path)

def main() -> None:
    ad.default.auto_start = True
    if engine_path.is_file():
        ad.default.engine_exe = ENGINE_EXE
        ad.default.engine_cwd = os.path.dirname(ENGINE_EXE)

    ad.default.hostapi = "ASIO"  # "ASIO" also works if available
    # We'll try a few common sr/ch pairs to match your device.
    ad.default.samplerate = 48_000
    ad.default.channels = 2
    ad.default.device_in = "UMC ASIO Driver"  # empty -> default input device

    out_dir = os.path.dirname(__file__)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    wav_path = os.path.join(out_dir, f"{ts}.wav")

    h = None
    tried = []
    for sr in (48_000, 44_100, 32_000, 16_000):
        for ch in (1, 2):
            tried.append((sr, ch))
            try:
                h = ad.rec_long(wav_path, rotate_s=5, samplerate=sr, channels=ch)
                print(f"started: sr={sr}, ch={ch}")
                break
            except Exception as e:
                print(f"start failed: sr={sr}, ch={ch}: {e}")
        if h is not None:
            break

    if h is None:
        raise RuntimeError(f"failed to start long recording; tried={tried!r}")

    print("recording... (10s)")
    time.sleep(10)
    h.stop()
    print("done:", wav_path)


if __name__ == "__main__":
    main()

