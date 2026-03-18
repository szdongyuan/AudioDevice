import numpy as np
from pathlib import Path

import audiodevice as ad

_root = Path(__file__).resolve().parent.parent
_engine = _root / "audiodevice.exe"
if _engine.is_file():
    ad.init(engine_exe=str(_engine), engine_cwd=str(_root), timeout=10)
else:
    ad.init(timeout=10)
ad.print_default_devices()

SAMPLERATE = 44100
DURATION_S = 5.0
DEVICE = (26, 27)  # (device_in, device_out)
DEFAULT_CHANNELS_NUM = (1, 2)  # (in_ch, out_ch)
OUTPUT_MAPPING = [1,2]  # 1-based

ad.default.samplerate = SAMPLERATE
ad.default.device = DEVICE
ad.default.channels = DEFAULT_CHANNELS_NUM

def main() -> None:
    fs = int(SAMPLERATE)
    t = np.arange(int(fs * DURATION_S), dtype=np.float32) / float(fs)
    n_out = int(len(OUTPUT_MAPPING))
    freqs = 1000.0 + 200.0 * np.arange(n_out, dtype=np.float32)
    y = 0.1 * np.sin(2 * np.pi * t[:, None] * freqs[None, :]).astype(np.float32)
    if n_out == 1:
        y = y[:, 0]
    ad.play(y, output_mapping=OUTPUT_MAPPING, blocking=True)

if __name__ == "__main__":
    main()