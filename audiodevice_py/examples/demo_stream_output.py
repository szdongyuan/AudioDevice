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

FREQ = 1000.0
VOLUME = 0.5
phase = [0.0]

# 对 Stream demo 更稳一些（降低 Windows 调度抖动导致的杂音风险）
# 重要：Stream API 统一使用 default.device；不再接受 Stream(..., device=...) 参数。
ad.default.rb_seconds = 20

# 方式 A（推荐）：只指定输出设备（OutputStream 不需要输入设备）
# ad.default.device_out = 17
# STREAM_DEVICE = (None, int(ad.default.device_out))

# 方式 B：同时指定 (输入设备index, 输出设备index)，两者必须同 hostapi
ad.default.samplerate = 44100
ad.default.device = (15,17)
ad.default.channels = (1,2)
ad.print_default_devices()
print(tuple(ad.default.device))

# 这套 Python<->engine 的 callback 推流需要 base64 编码；blocksize 太小更容易造成欠载/杂音
BLOCKSIZE = 8192

# 输出通道映射（1-based）：把 callback 写出的列路由到指定的设备输出通道。
# 例如 [2] 表示把单通道送到右声道；[2,1] 表示交换左右声道。
OUTPUT_MAPPING = [1]

# # ====================== 单通道（Mono） ======================
def callback(indata, outdata, frames, time_info, status):
    fs = float(ad.default.samplerate)
    t = (phase[0] + np.arange(frames, dtype=np.float32)) / fs
    phase[0] += frames
    sig = VOLUME * np.sin(2 * np.pi * FREQ * t)
    outdata[:, :] = sig[:, None]

# 注意：channels 是“设备输出通道数”，而 outdata 的列数是 len(OUTPUT_MAPPING)。
CHANNELS = 2

# # ====================== 双通道（Stereo） ======================
# def callback(indata, outdata, frames, time_info, status):
#     fs = float(SAMPLERATE)
#     t = (phase[0] + np.arange(frames, dtype=np.float32)) / fs
#     phase[0] += frames
#     # 左声道：1000Hz，右声道：500Hz
#     outdata[:, 0] = VOLUME * np.sin(2 * np.pi * FREQ * t)
#     outdata[:, 1] = VOLUME * np.sin(2 * np.pi * 500.0 * t)

# CHANNELS = 2

print(f"播放正弦波 5 秒 (channels={CHANNELS})...")
with ad.OutputStream(
    callback=callback,
    channels=CHANNELS,
    output_mapping=OUTPUT_MAPPING,
    samplerate=ad.default.samplerate,
    blocksize=BLOCKSIZE,
):
    ad.sleep(5000)
print("完成")

