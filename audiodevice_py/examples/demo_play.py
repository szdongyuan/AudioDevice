import numpy as np
import os
import audiodevice as ad
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
    ad.default.channels = 2
    ad.default.device = (10, 12)                                                         
    fs = ad.default.samplerate
    t = np.arange(fs*5, dtype=np.float32) / fs
    y = 0.1 * np.sin(2 * np.pi * 1000 * t).astype(np.float32)
    ad.play(y, blocking=True)

if __name__ == "__main__":
    main()