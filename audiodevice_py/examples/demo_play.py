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
    ad.default.samplerate = 44100
    ad.default.channels = (1,2)
    ad.default.device = (15, 17)                                                         
    fs = ad.default.samplerate
    t = np.arange(fs*5, dtype=np.float32) / fs
    output_mapping = [1,2]
    n_out = int(len(output_mapping))
    freqs = 1000.0 + 200.0 * np.arange(n_out, dtype=np.float32)
    y = 0.1 * np.sin(2 * np.pi * t[:, None] * freqs[None, :]).astype(np.float32)
    if n_out == 1:
        y = y[:, 0]
    ad.play(y, output_mapping=output_mapping, blocking=True)

if __name__ == "__main__":
    main()