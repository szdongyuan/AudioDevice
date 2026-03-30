"""
demo_stream_playrecord.py - 播放同时录音（playrec），并保存录音 WAV

说明：
- 这是“流式封装 stream_playrecord”的阻塞式 demo（不是 callback 手写循环）。
- 默认播放 1kHz 正弦 3 秒，同时录下输入，保存为 playrecord.wav。
"""
from pathlib import Path
import os
import time
import matplotlib.pyplot as plt
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

SAMPLERATE = 48000
BLOCKSIZE = 1024
RB_SECONDS = 8
DEVICE = (14, 18)  # (device_in, device_out)

# More stable defaults for stream demos
ad.default.samplerate = SAMPLERATE
ad.default.device = DEVICE
ad.default.rb_seconds = RB_SECONDS

DURATION_S = 3.0
# Chirp 参数（线性扫频：f0 -> f1）
CHIRP_F0 = 200.0
CHIRP_F1 = 8000.0
VOLUME = 0.1

# 选择一种“窗口定位方式”（二选一）。
# - "delay": 你知道大致延迟 -> 用 delay_time 把返回的 3s 窗口往后移
# - "alignment": 回采里确实能看到刺激信号 -> 用 GCC-PHAT 自动对齐（忽略 delay_time）
MODE = "alignment"

DELAY_MS = 34
ALIGNMENT_CH = 3  # 1-based, used when MODE="alignment"
INPUT_MAPPING = [1,2,3]  # 1-based: keep these input channels in returned recording
# 输出通道映射（1-based）：把 y 的每一列路由到指定的设备输出通道。
# 例如 [2,1] 表示交换左右声道；[2] 表示把单通道送到右声道。
OUTPUT_MAPPING = [1,2]
OUT_CH = len(OUTPUT_MAPPING)


def main() -> None:
    fs = int(SAMPLERATE)
    n = int(round(float(DURATION_S) * fs))
    t = np.arange(n, dtype=np.float32) / fs
    # 线性 chirp：phase(t) = 2π( f0*t + 0.5*k*t^2 ),  k=(f1-f0)/T
    T = float(DURATION_S)
    k = (float(CHIRP_F1) - float(CHIRP_F0)) / T
    phase = 2 * np.pi * (float(CHIRP_F0) * t + 0.5 * k * (t**2))
    y = (VOLUME * np.sin(phase)).astype(np.float32)
    # 淡入淡出，减少点击声
    fade_n = int(round(0.01 * fs))  # 10 ms
    if fade_n > 0 and 2 * fade_n < y.shape[0]:
        w = np.ones_like(y, dtype=np.float32)
        w[:fade_n] = np.linspace(0.0, 1.0, fade_n, dtype=np.float32)
        w[-fade_n:] = np.linspace(1.0, 0.0, fade_n, dtype=np.float32)
        y *= w
    if OUT_CH > 1:
        y = np.stack([y] * int(OUT_CH), axis=1)  # (frames, channels)

    ts = time.strftime("%Y%m%d_%H%M%S")
    wav_path = Path(__file__).resolve().parent / f"playrecord_{ts}.wav"

    print(f"播放并录音 {DURATION_S:.1f}s -> {wav_path.name}")
    if MODE == "delay":
        # 手工延迟窗口：返回/保存的 3s 窗口从 delay_time 开始
        x = ad.stream_playrecord(
            y,
            samplerate=fs,
            blocksize=BLOCKSIZE,
            delay_time=float(DELAY_MS),
            alignment=False,
            input_mapping=INPUT_MAPPING,
            output_mapping=OUTPUT_MAPPING,
            save_wav=True,
            wav_path=str(wav_path),
        )
        print("captured:", np.asarray(x).shape, np.asarray(x).dtype)
        print("wav:", os.path.abspath(str(wav_path)))
        return

    if MODE == "alignment":
        # 自动对齐窗口：用 GCC-PHAT 找刺激出现的位置并裁出 3s（delay_time 会被忽略）
        x = ad.stream_playrecord(
            y,
            samplerate=fs,
            blocksize=BLOCKSIZE,
            delay_time=float(DELAY_MS),  # will be ignored in alignment mode
            alignment=True,
            alignment_channel=int(ALIGNMENT_CH),
            input_mapping=INPUT_MAPPING,
            output_mapping=OUTPUT_MAPPING,
            save_wav=True,
            wav_path=str(wav_path),
        )
        print("captured:", np.asarray(x).shape, np.asarray(x).dtype)
        print("wav:", os.path.abspath(str(wav_path)))
        return

    raise ValueError('MODE must be "delay" or "alignment"')


if __name__ == "__main__":
    main()

