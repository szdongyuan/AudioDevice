"""
demo_stream_playrecord.py - 播放同时录音（playrec），并保存录音 WAV

说明：
- 这是“播放+录音”的阻塞式 demo（不是 callback 流 API）。
- 默认播放 1kHz 正弦 3 秒，同时录下输入，保存为 playrecord.wav。
"""
from pathlib import Path
import os

import numpy as np

import audiodevice as ad

# 初始化引擎
_root = Path(__file__).resolve().parent.parent
_engine = _root / "audiodevice.exe"
if _engine.is_file():
    ad.init(engine_exe=str(_engine), engine_cwd=str(_root), timeout=10)
else:
    ad.init(timeout=10)
ad.print_default_devices()

SAMPLERATE = 48_000
DURATION_S = 3.0
FREQ = 1000.0
VOLUME = 0.1
OUT_CH = 2
IN_CH = 6

# 更稳一些（避免调度抖动导致的缓冲问题）
ad.default.samplerate = SAMPLERATE
ad.default.rb_seconds = 8
ad.default.device = (14,18)
ad.default.channels = (6,2)
print(ad.default.device)


def main() -> None:
    fs = int(SAMPLERATE)
    n = int(round(float(DURATION_S) * fs))
    t = np.arange(n, dtype=np.float32) / fs
    y = (VOLUME * np.sin(2 * np.pi * FREQ * t)).astype(np.float32)
    if OUT_CH > 1:
        y = np.stack([y] * int(OUT_CH), axis=1)  # (frames, channels)

    wav_path = Path(__file__).resolve().parent / "playrecord.wav"
    print(f"播放并录音 {DURATION_S:.1f}s -> {wav_path.name}")
    x = ad.playrec(
        y,
        wav_path=str(wav_path),
        save_wav=True,
        blocking=True,
        samplerate=fs,
        in_channels=int(IN_CH),
    )
    print("captured:", x.shape, x.dtype)
    print("wav:", os.path.abspath(str(wav_path)))


if __name__ == "__main__":
    main()

