"""
demo_stream_duplex.py - 最简 Stream(全双工) 示例：麦克风直通到扬声器 1.5 秒
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
BLOCKSIZE = 1024
IN_CH = 1
OUT_CH = 2
DURATION_MS = 15000

# 对 Stream demo 更稳一些（避免调度抖动导致的缓冲问题）
ad.default.samplerate = SAMPLERATE
ad.default.rb_seconds = 8


def callback(indata, outdata, frames, time_info, status):
    if outdata.shape[1] > 0 and indata.shape[1] > 0:
        n = min(outdata.shape[1], indata.shape[1])
        outdata[:, :n] = indata[:, :n]
        if outdata.shape[1] > n:
            outdata[:, n:] = 0
    elif outdata.shape[1] > 0:
        outdata[:] = 0


print("全双工直通 1.5 秒（注意可能啸叫，建议先把音量调小）...")
stream = ad.Stream(
    callback=callback,
    channels=(IN_CH, OUT_CH),
    samplerate=SAMPLERATE,
    blocksize=BLOCKSIZE,
)
stream.start()
ad.sleep(DURATION_MS)
stream.close()
print("完成")

