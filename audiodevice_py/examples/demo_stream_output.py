"""
demo_stream_output.py - OutputStream 示例：播放 4 秒内重复 4 次的正弦扫频
"""
from pathlib import Path

import time
from time import perf_counter
import queue
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
BLOCKSIZE = 1024
RB_SECONDS = 20
OUTPUT_MAPPING = [1, 2]  # 1-based: route callback columns to output channels
DEVICE_OUT_CHANNELS = 2  # many devices/drivers reject mono (1ch) output configs
CALLBACK_CHANNELS = len(OUTPUT_MAPPING)
DEVICE = (14, 18)  # (device_in, device_out)

VOLUME = 0.5
TOTAL_SECONDS = 4
REPEAT_TIMES = 4
SWEEP_SECONDS = TOTAL_SECONDS / REPEAT_TIMES
SWEEP_START_HZ = 80.0
SWEEP_END_HZ = 2000.0
FADE_SECONDS = 0.005  # soften boundaries between repeats
sample_index = [0]
cb_last_t = [None]  # perf_counter timestamp
cb_count = [0]
cb_dt_q: "queue.Queue[tuple[int, float, int, float]]" = queue.Queue(maxsize=10000)  # (n, dt_s, frames, expected_s)
cb_status_q: "queue.Queue[tuple[int, str]]" = queue.Queue(maxsize=10000)  # (n, status_repr)

# 注意：OutputStream 内部会先“预填充”一段输出环形缓冲，期间回调可能被紧密连续调用，dt 会远小于期望值。
# 估算预填充回调次数（与 audiodevice.api._StreamBase._run 的逻辑保持一致），用于观测稳定段是否仍抖动。
_block_dt = float(BLOCKSIZE) / float(SAMPLERATE) if float(SAMPLERATE) > 0 else 0.0
_prefill_s = min(2.0, float(RB_SECONDS) * 0.2) if _block_dt > 0 else 0.0
PREFILL_BLOCKS = max(4, int(_prefill_s / _block_dt)) if _block_dt > 0 else 0

# More stable defaults for stream demos
ad.default.samplerate = SAMPLERATE
ad.default.device = DEVICE
ad.default.rb_seconds = RB_SECONDS
ad.print_default_devices()
print(tuple(ad.default.device))

# # ====================== 单通道（Mono） ======================
def callback(indata, outdata, frames, time_info, status):
    now = perf_counter()
    cb_count[0] += 1
    n = cb_count[0]
    last = cb_last_t[0]
    cb_last_t[0] = now

    # 预填充阶段（以及刚切到 paced 的第 1 个块）不参与 dt 统计，避免把“紧密填充”误判为不稳定。
    if n <= (PREFILL_BLOCKS + 1):
        last = None

    if last is not None:
        dt = now - last
        expected = frames / float(ad.default.samplerate)
        try:
            cb_dt_q.put_nowait((n, dt, int(frames), float(expected)))
        except queue.Full:
            pass

    if status:
        try:
            cb_status_q.put_nowait((n, repr(status)))
        except queue.Full:
            pass

    fs = float(ad.default.samplerate)
    sweep_samples = int(round(SWEEP_SECONDS * fs))
    if sweep_samples <= 0:
        outdata.fill(0.0)
        return

    idx = sample_index[0] + np.arange(frames, dtype=np.int64)
    sample_index[0] += frames

    local = (idx % sweep_samples).astype(np.float32, copy=False)
    t = local / fs  # [0, SWEEP_SECONDS)
    k = (SWEEP_END_HZ - SWEEP_START_HZ) / SWEEP_SECONDS  # Hz/s, linear chirp
    phase = 2 * np.pi * (SWEEP_START_HZ * t + 0.5 * k * t * t)
    sig = np.sin(phase).astype(np.float32, copy=False)

    fade_samples = int(round(FADE_SECONDS * fs))
    if fade_samples > 0 and fade_samples * 2 < sweep_samples:
        env = np.ones(frames, dtype=np.float32)
        head = local < fade_samples
        if np.any(head):
            x = local[head] / fade_samples
            env[head] = 0.5 - 0.5 * np.cos(np.pi * x)
        tail = local >= (sweep_samples - fade_samples)
        if np.any(tail):
            x = (sweep_samples - 1.0 - local[tail]) / fade_samples
            env[tail] = 0.5 - 0.5 * np.cos(np.pi * x)
        sig *= env

    sig *= VOLUME
    if outdata.shape[1] >= 2:
        outdata[:, 0] = sig
        outdata[:, 1] = sig
    else:
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

print(f"播放正弦波 {TOTAL_SECONDS:.1f} 秒 (device_out_ch={DEVICE_OUT_CHANNELS}, callback_ch={CALLBACK_CHANNELS})...")
with ad.OutputStream(
    callback=callback,
    channels=DEVICE_OUT_CHANNELS,
    output_mapping=OUTPUT_MAPPING,
    samplerate=SAMPLERATE,
    blocksize=BLOCKSIZE,
):
    # OutputStream 在后台线程里做 session_start；某些设备/后端启动会有明显延迟。
    # 为了让“听到的时长”更接近 TOTAL_SECONDS，先等到 session 真正启动后再开始计时。
    t0 = time.time()
    while True:
        st = ad.get_status() or {}
        if bool(st.get("has_session", False)):
            break
        if (time.time() - t0) >= 5.0:
            print("警告：等待 session 启动超时，仍然开始计时 5 秒")
            break
        ad.sleep(50)
    print(
        f"播放 {TOTAL_SECONDS:.1f}s：每 {SWEEP_SECONDS:.2f}s 从 {SWEEP_START_HZ:.0f}Hz 扫到 {SWEEP_END_HZ:.0f}Hz，重复 {REPEAT_TIMES} 次..."
    )
    print(f"回调 dt 统计：跳过预填充 {PREFILL_BLOCKS} 个 block（避免把预填充 burst 当成抖动）")
    t_end = time.time() + TOTAL_SECONDS
    while time.time() < t_end:
        while True:
            try:
                n, dt, frames, expected = cb_dt_q.get_nowait()
            except queue.Empty:
                break
            jitter_ms = (dt - expected) * 1000.0
            print(
                f"callback #{n:04d}  dt={dt*1000.0:8.3f} ms"
                f"  expected={expected*1000.0:8.3f} ms"
                f"  jitter={jitter_ms:8.3f} ms"
                f"  frames={frames}"
            )

        while True:
            try:
                n, status_repr = cb_status_q.get_nowait()
            except queue.Empty:
                break
            print(f"callback #{n:04d}  STATUS: {status_repr}")
        ad.sleep(50)
print("完成")

