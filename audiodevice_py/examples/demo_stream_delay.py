"""
demo_stream_delay.py - 验证 InputStream(delay_time=ms) 是否真的延时

说明：
- 这个 demo 不依赖“回放->录音”的物理回路（比 demo_delay.py 更容易跑通）。
- 它验证的是：InputStream 的 callback 首次收到 indata 的时间是否被 delay_time 推迟。
- 由于 session_start/线程调度会引入固定开销，所以用两次实验做差值：
  - delay_time=0 的首次回调耗时：t0_ms
  - delay_time=DELAY_MS 的首次回调耗时：t1_ms
  - 观测延时 ≈ (t1_ms - t0_ms) ，应接近 DELAY_MS
"""

from __future__ import annotations

from pathlib import Path
import time
import threading

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
INPUT_MAPPING = [1]  # 1-based: callback keeps only this input channel
CHANNELS = len(INPUT_MAPPING)
BLOCKSIZE = 1024
DURATION_MS = 1500
DELAY_MS = 500
REPEATS = 5
RB_FRAMES = 4096

# More stable defaults for stream demos
ad.default.samplerate = SAMPLERATE


def run_once(*, delay_ms: int, samplerate: int, channels: int, blocksize: int, duration_ms: int):
    target_frames = int(round(samplerate * (duration_ms / 1000.0)))
    chunks = []
    frames_captured = [0]
    t_first_cb = [None]  # type: ignore[var-annotated]
    done = threading.Event()

    t_start = time.perf_counter()

    def callback(indata, outdata, frames, time_info, status):
        if t_first_cb[0] is None:
            t_first_cb[0] = time.perf_counter()

        remain = target_frames - frames_captured[0]
        if remain <= 0:
            done.set()
            raise ad.CallbackStop()

        take = int(frames) if int(frames) < int(remain) else int(remain)
        if take > 0:
            chunks.append(indata[:take].copy())
            frames_captured[0] += int(take)

        if frames_captured[0] >= target_frames:
            done.set()
            raise ad.CallbackStop()

    stream = ad.InputStream(
        callback=callback,
        channels=int(channels),
        samplerate=int(samplerate),
        blocksize=int(blocksize),
        rb_frames=RB_FRAMES,
        delay_time=int(delay_ms),
        mapping=INPUT_MAPPING,
    )
    stream.start()

    # 等待：duration + delay + 额外缓冲（避免边界条件导致没收齐就 close）
    ad.sleep(int(duration_ms + max(0, delay_ms) + 1200))
    stream.close()

    if not done.is_set():
        # callback 可能因为设备/HostAPI 原因没跑起来；仍返回当前观测值
        pass

    if chunks:
        data = np.concatenate(chunks, axis=0)
        if data.shape[0] > target_frames:
            data = data[:target_frames]
    else:
        data = np.zeros((0, int(channels)), dtype=np.float32)

    t_first_ms = None
    if t_first_cb[0] is not None:
        t_first_ms = (float(t_first_cb[0]) - float(t_start)) * 1000.0

    return {
        "delay_ms": int(delay_ms),
        "target_frames": int(target_frames),
        "captured_frames": int(frames_captured[0]),
        "first_callback_ms": t_first_ms,
        "data": data,
    }


def main() -> None:
    print("Stream delay test (InputStream callback timing)")
    print(
        f"FS={SAMPLERATE}, blocksize={BLOCKSIZE}, duration={DURATION_MS}ms, "
        f"test_delay={DELAY_MS}ms, repeats={REPEATS}"
    )

    first0 = []
    first1 = []
    last_r0 = None
    last_r1 = None
    for k in range(int(REPEATS)):
        r0 = run_once(
            delay_ms=0,
            samplerate=SAMPLERATE,
            channels=CHANNELS,
            blocksize=BLOCKSIZE,
            duration_ms=DURATION_MS,
        )
        r1 = run_once(
            delay_ms=DELAY_MS,
            samplerate=SAMPLERATE,
            channels=CHANNELS,
            blocksize=BLOCKSIZE,
            duration_ms=DURATION_MS,
        )
        last_r0, last_r1 = r0, r1
        print(
            f"[{k+1}/{REPEATS}] "
            f"t0={r0['first_callback_ms']}, t1={r1['first_callback_ms']}, "
            f"frames0={r0['captured_frames']}/{r0['target_frames']}, "
            f"frames1={r1['captured_frames']}/{r1['target_frames']}"
        )
        if r0["first_callback_ms"] is not None:
            first0.append(float(r0["first_callback_ms"]))
        if r1["first_callback_ms"] is not None:
            first1.append(float(r1["first_callback_ms"]))

    if first0 and first1 and (len(first0) == len(first1)):
        deltas = [b - a for a, b in zip(first0, first1)]
        med = float(np.median(np.asarray(deltas, dtype=np.float32)))
        p10 = float(np.percentile(np.asarray(deltas, dtype=np.float32), 10))
        p90 = float(np.percentile(np.asarray(deltas, dtype=np.float32), 90))
        print(f"observed extra delay median ≈ {med:.1f} ms (p10={p10:.1f}, p90={p90:.1f}; expected ≈ {DELAY_MS} ms)")
    else:
        print("未能稳定测到首次回调时间（可能 callback 未触发）。可尝试切换 HostAPI/设备。")

    # 保存最后一组数据（可选，便于离线排查）
    if last_r0 is not None and last_r1 is not None:
        out_dir = Path(__file__).resolve().parent
        np.save(str(out_dir / "stream_delay_0ms.npy"), np.asarray(last_r0["data"]))
        np.save(str(out_dir / f"stream_delay_{DELAY_MS}ms.npy"), np.asarray(last_r1["data"]))
        print("saved npy in examples/: stream_delay_0ms.npy and stream_delay_*ms.npy")


if __name__ == "__main__":
    main()

