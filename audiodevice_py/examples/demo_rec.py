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

    # hostapi is read-only; it follows from default.device
    ad.default.samplerate = 48_000
    ad.default.device = (14,18)
    ad.default.channels = (6,2)

    delay_ms = 200
    wav_path = os.path.join(os.path.dirname(__file__), "rec88888.wav")
    y = ad.rec(
        48000 * 3,
        blocking=True,
        wav_path=wav_path,
        save_wav=True,
        channels=6,
        delay_time=delay_ms,
    )
    print("recorded:", y.shape, y.dtype, "min/max:", float(y.min()), float(y.max()))
    ad.play(y, blocking=True,samplerate=48000,channels=2)


if __name__ == "__main__":
    main()

