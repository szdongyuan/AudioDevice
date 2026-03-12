"""
demo_stream_output.py - 最简 OutputStream 示例：播放 1000Hz 正弦波 5 秒
"""
from pathlib import Path

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
FREQ = 1000.0
VOLUME = 0.5
phase = [0.0]

# 对 Stream demo 更稳一些（避免调度抖动导致的缓冲问题）
ad.default.samplerate = SAMPLERATE
ad.default.rb_seconds = 8
# ad.default.device = [10,12]
ad.default.device = [1,3]
def callback(indata, outdata, frames, time_info, status):
    t = (phase[0] + np.arange(frames, dtype=np.float32)) / SAMPLERATE
    phase[0] += frames
    outdata[:, 0] = VOLUME * np.sin(2 * np.pi * FREQ * t)
    if outdata.shape[1] > 1:
        outdata[:, 1:] = outdata[:, 0:1]

print("播放 1000Hz 正弦波 5 秒...")
with ad.OutputStream(callback=callback, channels=2, samplerate=SAMPLERATE, blocksize=1024):
    ad.sleep(5000)
print("完成")

