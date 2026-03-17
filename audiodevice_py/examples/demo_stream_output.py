"""
demo_stream_output.py - 最简 OutputStream 示例：播放 1000Hz 正弦波 5 秒
"""
from pathlib import Path

import time
import numpy as np

import audiodevice as ad

# 初始化引擎
_root = Path(__file__).resolve().parent.parent
_engine = _root / "audiodevice.exe"
if _engine.is_file():
    ad.init(engine_exe=str(_engine), engine_cwd=str(_root), timeout=10)
else:
    ad.init(timeout=10)

SAMPLERATE = 48000
BLOCKSIZE = 8192
RB_SECONDS = 20
OUTPUT_MAPPING = [1]  # 1-based: route callback columns to output channels
DEVICE_OUT_CHANNELS = 2  # many devices/drivers reject mono (1ch) output configs
CALLBACK_CHANNELS = len(OUTPUT_MAPPING)
DEVICE = (10, 12)  # (device_in, device_out)
DEFAULT_CHANNELS_NUM = (6, 2)  # (in_ch, out_ch) for engine default session

FREQ = 1000.0
VOLUME = 0.5
phase = [0.0]

# More stable defaults for stream demos
ad.default.samplerate = SAMPLERATE
ad.default.device = DEVICE
ad.default.channels = DEFAULT_CHANNELS_NUM
ad.default.rb_seconds = RB_SECONDS
ad.print_default_devices()
print(tuple(ad.default.device))

# # ====================== 单通道（Mono） ======================
def callback(indata, outdata, frames, time_info, status):
    fs = float(ad.default.samplerate)
    t = (phase[0] + np.arange(frames, dtype=np.float32)) / fs
    phase[0] += frames
    sig = VOLUME * np.sin(2 * np.pi * FREQ * t)
    outdata[:, :] = sig[:, None]

# # ====================== 双通道（Stereo） ======================
# def callback(indata, outdata, frames, time_info, status):
#     fs = float(SAMPLERATE)
#     t = (phase[0] + np.arange(frames, dtype=np.float32)) / fs
#     phase[0] += frames
#     # 左声道：1000Hz，右声道：500Hz
#     outdata[:, 0] = VOLUME * np.sin(2 * np.pi * FREQ * t)
#     outdata[:, 1] = VOLUME * np.sin(2 * np.pi * 500.0 * t)

# CHANNELS = 2

print(f"播放正弦波 5 秒 (device_out_ch={DEVICE_OUT_CHANNELS}, callback_ch={CALLBACK_CHANNELS})...")
with ad.OutputStream(
    callback=callback,
    channels=DEVICE_OUT_CHANNELS,
    output_mapping=OUTPUT_MAPPING,
    samplerate=SAMPLERATE,
    blocksize=BLOCKSIZE,
):
    # OutputStream 在后台线程里做 session_start；某些设备/后端启动会有明显延迟。
    # 为了让“听到的时长”更接近 5 秒，先等到 session 真正启动后再开始计时。
    t0 = time.time()
    while True:
        st = ad.get_status() or {}
        if bool(st.get("has_session", False)):
            break
        if (time.time() - t0) >= 5.0:
            print("警告：等待 session 启动超时，仍然开始计时 5 秒")
            break
        ad.sleep(50)
    ad.sleep(5000)
print("完成")

