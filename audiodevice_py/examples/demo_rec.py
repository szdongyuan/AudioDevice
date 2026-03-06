import audiodevice as ad
import numpy as np
import os

from pathlib import Path

current_file = Path(__file__).resolve()
engine_path = current_file.parent.parent / "audiodevice.exe"
ENGINE_EXE = str(engine_path)

def main() -> None:
    ad.default.auto_start = True
    if engine_path.is_file():
        ad.default.engine_exe = ENGINE_EXE
        ad.default.engine_cwd = os.path.dirname(ENGINE_EXE)

    ad.default.hostapi = "ASIO"
    ad.default.samplerate = 48_000
    ad.default.channels = 2
    # ad.default.device_in = "ASIO4ALL"
    ad.default.device_in = "UMC ASIO Driver"

    wav_path = os.path.join(os.path.dirname(__file__), "rec999.wav")
    y = ad.rec(48_000 * 5, wav_path=wav_path, save_wav=True, blocking=True)
    print("recorded:", y.shape, y.dtype, "min/max:", float(y.min()), float(y.max()))


if __name__ == "__main__":
    main()

