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
# 重要：Stream API 只会使用显式传入的 device；因此这里既设置 default.device，也会在 OutputStream 里传 device=ad.default.device。
ad.default.rb_seconds = 20

# 方式 A（推荐）：只指定输出设备（OutputStream 不需要输入设备）
# ad.default.device_out = 17
# STREAM_DEVICE = (None, int(ad.default.device_out))

# 方式 B：同时指定 (输入设备index, 输出设备index)，两者必须同 hostapi
ad.default.device = [15, 17]
STREAM_DEVICE = tuple(ad.default.device)
ad.print_default_devices()
print(STREAM_DEVICE)    

# 尽量让采样率匹配实际输出设备，避免 WASAPI 下因格式/采样率不匹配引入的爆音/杂音
try:
    out_idx = int(STREAM_DEVICE[1])
    out_dev = ad.query_devices(out_idx)
    SAMPLERATE = int(float(out_dev.get("default_samplerate", 48_000) or 48_000))
except Exception:
    SAMPLERATE = 48_000
ad.default.samplerate = SAMPLERATE

# 这套 Python<->engine 的 callback 推流需要 base64 编码；blocksize 太小更容易造成欠载/杂音
BLOCKSIZE = 8192
print(f"Output samplerate={SAMPLERATE}, blocksize={BLOCKSIZE}, device={STREAM_DEVICE}")

# # ====================== 单通道（Mono） ======================
def callback(indata, outdata, frames, time_info, status):
    fs = float(SAMPLERATE)
    t = (phase[0] + np.arange(frames, dtype=np.float32)) / fs
    phase[0] += frames
    outdata[:, 0] = VOLUME * np.sin(2 * np.pi * FREQ * t)

CHANNELS = 1

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
    samplerate=SAMPLERATE,
    blocksize=BLOCKSIZE,
    device=STREAM_DEVICE,
):
    ad.sleep(5000)
print("完成")

