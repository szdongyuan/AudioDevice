import os

from pathlib import Path

import numpy as np

import audiodevice as ad

_root = Path(__file__).resolve().parent.parent
_engine = _root / "audiodevice.exe"
if _engine.is_file():
    ad.init(engine_exe=str(_engine), engine_cwd=str(_root), timeout=10)
else:
    ad.init(timeout=10)
ad.print_default_devices()

SAMPLERATE = 48_000
DURATION_S = 3.0
DELAY_MS = 200
INPUT_MAPPING = [1, 3, 5]  # 1-based
WAV_PATH = os.path.join(os.path.dirname(__file__), "rec88888.wav")
DEVICE = (10, 12)  # (device_in, device_out)

ad.default.samplerate = SAMPLERATE
ad.default.device = DEVICE

def main() -> None:
    frames = int(round(float(SAMPLERATE) * float(DURATION_S)))
    y = ad.rec(
        frames,
        blocking=True,
        wav_path=WAV_PATH,
        save_wav=True,
        delay_time=DELAY_MS,
        mapping=INPUT_MAPPING,
    )
    print("recorded:", y.shape, y.dtype, "min/max:", float(y.min()), float(y.max()))


if __name__ == "__main__":
    main()

