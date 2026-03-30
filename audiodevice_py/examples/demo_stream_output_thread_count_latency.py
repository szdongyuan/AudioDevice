"""
demo_stream_output_thread_count_latency.py

验证现象：随着“并发启动的 Stream 线程数”增加，回调触发间隔 / 启动响应时间出现明显延迟。

本 demo 当前配置为：**4 个线程各自打开一个 InputStream**，
并分别采集“同一个输入设备”的前 4 个通道（mapping=1..4），以验证多线程并发流式工作的延迟问题。

用法（在 examples 目录或任意目录均可）：
  python demo_stream_output_thread_count_latency.py
  python demo_stream_output_thread_count_latency.py --threads 4 --seconds 10
  python demo_stream_output_thread_count_latency.py --sweep 1,2,4,8 --seconds 8

提示：
- 默认使用 ad.default.device / ad.default.device_out 等配置；你可以用 --device-in/--device-out 覆盖。
- 多个 OutputStream 并发竞争同一设备时，部分后端可能直接失败或出现“某些流 callback 不触发”的现象；
  这也属于我们要观察的结果（会在汇总里标为 no_callback / failed）。
"""

from __future__ import annotations

import argparse
import statistics
import os
import queue
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

import numpy as np

import audiodevice as ad


# ---- constants (similar to demo_stream_output.py) ----
SAMPLERATE = 48_000
BLOCKSIZE = 1024
RB_SECONDS = 20
OUTPUT_MAPPING = [1, 2]  # 1-based: route callback columns to output channels
DEVICE_OUT_CHANNELS = 2  # many devices/drivers reject mono (1ch) output configs
DEVICE = (14, 18)  # (device_in, device_out)
DEFAULT_CHANNELS_NUM = (6, 2)  # (in_ch, out_ch) for engine default session
# ---- end constants ----

SAVE_DIR = os.path.join(
    os.path.dirname(__file__),
    "recordings",
    "demo_stream_input_thread_count_latency",
)


def init_engine() -> None:
    root = Path(__file__).resolve().parent.parent
    engine = root / "audiodevice.exe"
    if engine.is_file():
        ad.init(engine_exe=str(engine), engine_cwd=str(root), timeout=10)
    else:
        ad.init(timeout=10)


def _parse_int_list(s: str) -> list[int]:
    s2 = str(s).strip()
    if not s2:
        return []
    out: list[int] = []
    for part in s2.split(","):
        part = part.strip()
        if not part:
            continue
        out.append(int(part))
    return out


def _ms(x_s: float | None) -> float | None:
    if x_s is None:
        return None
    return float(x_s) * 1000.0


def _save_wav_mono(path: str, data_f32: np.ndarray, samplerate: int) -> None:
    x = np.asarray(data_f32, dtype=np.float32)
    if x.ndim == 2:
        x = x[:, 0]
    x = np.clip(x, -1.0, 1.0)
    pcm16 = (x * 32767.0).astype(np.int16)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as wavf:
        wavf.setnchannels(1)
        wavf.setsampwidth(2)
        wavf.setframerate(int(samplerate))
        wavf.writeframes(pcm16.tobytes())


def _f32_to_pcm16_bytes(x_f32: np.ndarray) -> bytes:
    x = np.asarray(x_f32, dtype=np.float32).reshape(-1)
    x = np.clip(x, -1.0, 1.0)
    return (x * 32767.0).astype(np.int16).tobytes()


@dataclass
class StreamThreadResult:
    thread_id: int
    ok: bool
    error: str | None
    stream_start_latency_s: float | None  # first_cb_ts - stream.start() return ts
    callback_dt_s: list[float]  # dt between callbacks (steady-state; prefill skipped)
    callback_jitter_ms: list[float]  # (dt - expected_dt) * 1000, steady-state
    callback_count: int
    status_count: int
    mapping: list[int]
    wav_path: str | None


@dataclass
class SingleStreamFanoutResult:
    ok: bool
    error: str | None
    stream_start_latency_s: float | None
    callback_jitter_ms: list[float]
    callback_count: int
    status_count: int
    mapping: list[int]
    wav_paths: list[str]
    dropped_blocks: list[int]
    written_frames: list[int]


def _summarize_ms(xs_ms: list[float]) -> str:
    if not xs_ms:
        return "n=0"
    xs = sorted(float(x) for x in xs_ms)
    n = len(xs)
    p50 = xs[int(round(0.50 * (n - 1)))]
    p90 = xs[int(round(0.90 * (n - 1)))]
    p99 = xs[int(round(0.99 * (n - 1)))]
    return (
        f"n={n} mean={statistics.fmean(xs):.3f} "
        f"p50={p50:.3f} p90={p90:.3f} p99={p99:.3f} "
        f"min={xs[0]:.3f} max={xs[-1]:.3f}"
    )


def _run_single_stream_fanout(
    *,
    threads: int,
    seconds: float,
    samplerate: int,
    blocksize: int,
    rb_seconds: float,
    device_in: int | None,
    device_out: int | None,
    callback_work_ms: float,
    start_input_channel: int,
    input_channels_num: int,
    delay_ms: int,
) -> SingleStreamFanoutResult:
    """
    单 InputStream + fanout：
    - 只打开 1 个 InputStream，mapping=[start..start+threads-1]
    - callback 中把每列数据分发到各自 writer 线程
    - 每个 writer 线程写一个 wav（单通道）
    """
    if threads <= 0:
        raise ValueError("--threads must be > 0")
    if seconds <= 0:
        raise ValueError("--seconds must be > 0")
    if samplerate <= 0:
        raise ValueError("--samplerate must be > 0")
    if blocksize <= 0:
        raise ValueError("--blocksize must be > 0")
    if input_channels_num <= 0:
        raise ValueError("--in-channels-num must be > 0")
    if start_input_channel <= 0:
        raise ValueError("--start-input-channel must be >= 1")

    ad.default.samplerate = int(samplerate)
    ad.default.rb_seconds = float(rb_seconds)
    if device_in is None and device_out is None:
        ad.default.device = tuple(DEVICE)
    else:
        din = int(device_in) if device_in is not None else int(DEVICE[0])
        dout = int(device_out) if device_out is not None else int(DEVICE[1])
        ad.default.device = (din, dout)
    ad.default.channels = (int(input_channels_num), int(DEFAULT_CHANNELS_NUM[1]))

    mapping = [int(start_input_channel) + i for i in range(int(threads))]
    if any(m > int(input_channels_num) for m in mapping):
        raise ValueError(f"mapping out of range: mapping={mapping}, in_channels_num={input_channels_num}")

    # prefill skip
    block_dt = float(blocksize) / float(samplerate)
    prefill_s = min(2.0, float(rb_seconds) * 0.2) if block_dt > 0 else 0.0
    prefill_blocks = max(4, int(prefill_s / block_dt)) if block_dt > 0 else 0

    ts = time.strftime("%Y%m%d_%H%M%S")
    wav_paths: list[str] = []
    wav_files: list[wave.Wave_write] = []
    q_list: list["queue.Queue[np.ndarray | None]"] = []
    dropped = [0 for _ in range(int(threads))]
    written_frames = [0 for _ in range(int(threads))]

    stop_ev = threading.Event()

    def writer(i: int) -> None:
        nonlocal written_frames
        q = q_list[i]
        wf = wav_files[i]
        while True:
            try:
                item = q.get(timeout=0.2)
            except queue.Empty:
                if stop_ev.is_set():
                    break
                continue
            if item is None:
                break
            try:
                wf.writeframes(_f32_to_pcm16_bytes(item))
                written_frames[i] += int(item.shape[0])
            except Exception:
                # best-effort: demo doesn't crash on write errors
                break

    # open wav writers and queues
    for i in range(int(threads)):
        path = os.path.join(
            SAVE_DIR,
            f"{ts}_single_stream_writer{i}_devin{int(ad.default.device[0])}_map{mapping[i]}_sr{int(samplerate)}.wav",
        )
        os.makedirs(os.path.dirname(path), exist_ok=True)
        wf = wave.open(path, "wb")
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(samplerate))
        wav_paths.append(path)
        wav_files.append(wf)
        q_list.append(queue.Queue(maxsize=64))

    writer_threads = [
        threading.Thread(target=writer, args=(i,), daemon=False) for i in range(int(threads))
    ]
    for t in writer_threads:
        t.start()

    err: str | None = None
    ok = False
    first_cb_ts: float | None = None
    last_cb_ts: float | None = None
    stream_start_ts: float | None = None
    cb_count = 0
    status_count = 0
    jitter_ms: list[float] = []

    def callback(indata, outdata, frames, time_info, status):  # noqa: ARG001
        nonlocal first_cb_ts, last_cb_ts, cb_count, status_count, jitter_ms
        now = perf_counter()
        cb_count += 1
        if first_cb_ts is None:
            first_cb_ts = now
        if status:
            status_count += 1
        last = last_cb_ts
        last_cb_ts = now

        if callback_work_ms > 0:
            time.sleep(float(callback_work_ms) / 1000.0)

        # fanout each column to its writer
        try:
            x = np.asarray(indata, dtype=np.float32)
            if x.ndim == 1:
                x = x[:, None]
            cols = int(x.shape[1])
            for i in range(int(threads)):
                if i >= cols:
                    break
                blk = x[:, i].copy()
                try:
                    q_list[i].put_nowait(blk)
                except queue.Full:
                    dropped[i] += 1
        except Exception:
            pass

        if last is not None and cb_count > (prefill_blocks + 1):
            dt = float(now - last)
            expected = float(frames) / float(samplerate)
            jitter_ms.append((dt - expected) * 1000.0)

    try:
        stream = ad.InputStream(
            device=tuple(ad.default.device),
            callback=callback,
            samplerate=int(samplerate),
            blocksize=int(blocksize),
            channels=int(input_channels_num),
            mapping=list(mapping),
            delay_time=int(delay_ms),
        )
        try:
            stream.start()
            stream_start_ts = perf_counter()
            time.sleep(float(seconds))
        finally:
            stream.close()
        ok = True
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
    finally:
        stop_ev.set()
        for q in q_list:
            try:
                q.put_nowait(None)
            except Exception:
                pass
        for t in writer_threads:
            t.join(timeout=5.0)
        for wf in wav_files:
            try:
                wf.close()
            except Exception:
                pass

    start_latency_s = None
    if first_cb_ts is not None and stream_start_ts is not None:
        start_latency_s = float(first_cb_ts - stream_start_ts)

    return SingleStreamFanoutResult(
        ok=bool(ok),
        error=err,
        stream_start_latency_s=start_latency_s,
        callback_jitter_ms=jitter_ms,
        callback_count=int(cb_count),
        status_count=int(status_count),
        mapping=list(mapping),
        wav_paths=list(wav_paths),
        dropped_blocks=list(dropped),
        written_frames=list(written_frames),
    )


def _run_once(
    *,
    threads: int,
    seconds: float,
    samplerate: int,
    blocksize: int,
    rb_seconds: float,
    device_in: int | None,
    device_out: int | None,
    callback_work_ms: float,
    start_input_channel: int,
    input_channels_num: int,
    delay_ms: int,
) -> list[StreamThreadResult]:
    if threads <= 0:
        raise ValueError("--threads must be > 0")
    if seconds <= 0:
        raise ValueError("--seconds must be > 0")
    if samplerate <= 0:
        raise ValueError("--samplerate must be > 0")
    if blocksize <= 0:
        raise ValueError("--blocksize must be > 0")
    if input_channels_num <= 0:
        raise ValueError("--in-channels-num must be > 0")
    if start_input_channel <= 0:
        raise ValueError("--start-input-channel must be >= 1")

    # IMPORTANT: init_engine() is done once in main(); do NOT init per run.
    ad.default.samplerate = int(samplerate)
    ad.default.rb_seconds = float(rb_seconds)
    # Use demo-like defaults, but allow CLI override
    if device_in is None and device_out is None:
        ad.default.device = tuple(DEVICE)
    else:
        din = int(device_in) if device_in is not None else int(DEVICE[0])
        dout = int(device_out) if device_out is not None else int(DEVICE[1])
        ad.default.device = (din, dout)

    ad.default.channels = (int(input_channels_num), int(DEFAULT_CHANNELS_NUM[1]))

    # 估算预填充回调次数（与 demo_stream_output.py 保持一致思路）
    block_dt = float(blocksize) / float(samplerate)
    prefill_s = min(2.0, float(rb_seconds) * 0.2) if block_dt > 0 else 0.0
    prefill_blocks = max(4, int(prefill_s / block_dt)) if block_dt > 0 else 0

    start_ev = threading.Event()
    results: list[StreamThreadResult] = []
    results_lock = threading.Lock()

    def worker(thread_id: int) -> None:
        err: str | None = None
        ok = False
        cb_dt: list[float] = []
        cb_jitter_ms: list[float] = []
        cb_count = 0
        status_count = 0
        first_cb_ts: float | None = None
        last_cb_ts: float | None = None
        stream_start_ts: float | None = None
        wav_path: str | None = None
        chunks: list[np.ndarray] = []

        mapping = [int(start_input_channel) + int(thread_id)]
        # mapping 不能超过 channels
        if mapping[0] > int(input_channels_num):
            raise ValueError(
                f"thread-{thread_id} mapping={mapping} exceeds in_channels_num={input_channels_num}"
            )

        def callback(indata, outdata, frames, time_info, status):  # noqa: ARG001
            nonlocal cb_count, status_count, first_cb_ts, last_cb_ts
            now = perf_counter()
            cb_count += 1
            if first_cb_ts is None:
                first_cb_ts = now

            if status:
                status_count += 1

            last = last_cb_ts
            last_cb_ts = now

            if callback_work_ms > 0:
                # 故意模拟回调负载（可选），方便放大“线程越多越延迟”的现象
                time.sleep(float(callback_work_ms) / 1000.0)

            # 保存采到的音频（单通道）。indata 形状通常为 (frames, len(mapping))。
            try:
                x = np.asarray(indata, dtype=np.float32)
                if x.ndim == 1:
                    x = x[:, None]
                if x.size > 0:
                    chunks.append(x.copy())
            except Exception:
                pass

            # 跳过预填充 burst 段，观察稳定段抖动/延迟
            if last is not None and cb_count > (prefill_blocks + 1):
                dt = float(now - last)
                expected = float(frames) / float(samplerate)
                cb_dt.append(dt)
                cb_jitter_ms.append((dt - expected) * 1000.0)

        try:
            if not start_ev.wait(timeout=10.0):
                raise TimeoutError("wait start signal timeout")

            stream = ad.InputStream(
                device=tuple(ad.default.device),
                callback=callback,
                samplerate=int(samplerate),
                blocksize=int(blocksize),
                channels=int(input_channels_num),
                mapping=list(mapping),
                delay_time=int(delay_ms),
            )
            try:
                stream.start()
                stream_start_ts = perf_counter()
                time.sleep(float(seconds))
            finally:
                stream.close()

            # close 后落盘 WAV
            if chunks:
                ts = time.strftime("%Y%m%d_%H%M%S")
                wav_path = os.path.join(
                    SAVE_DIR,
                    f"{ts}_thread{int(thread_id)}_devin{int(ad.default.device[0])}_map{mapping[0]}_sr{int(samplerate)}.wav",
                )
                data = np.concatenate(chunks, axis=0)
                _save_wav_mono(wav_path, data, int(samplerate))

            ok = True
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
        finally:
            start_latency_s = None
            if first_cb_ts is not None and stream_start_ts is not None:
                start_latency_s = float(first_cb_ts - stream_start_ts)
            with results_lock:
                results.append(
                    StreamThreadResult(
                        thread_id=int(thread_id),
                        ok=bool(ok),
                        error=err,
                        stream_start_latency_s=start_latency_s,
                        callback_dt_s=cb_dt,
                        callback_jitter_ms=cb_jitter_ms,
                        callback_count=int(cb_count),
                        status_count=int(status_count),
                        mapping=list(mapping),
                        wav_path=wav_path,
                    )
                )

    threads_list = [
        threading.Thread(target=worker, args=(i,), daemon=False)
        for i in range(int(threads))
    ]
    for t in threads_list:
        t.start()

    time.sleep(0.2)
    start_ev.set()

    for t in threads_list:
        t.join(timeout=float(seconds) + 15.0)

    return sorted(results, key=lambda r: int(r.thread_id))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--threads", type=int, default=4, help="number of InputStreams to start in parallel")
    p.add_argument("--sweep", type=str, default="")
    p.add_argument("--seconds", type=float, default=8.0)
    p.add_argument("--samplerate", type=int, default=int(SAMPLERATE))
    p.add_argument("--blocksize", type=int, default=int(BLOCKSIZE))
    p.add_argument("--rb-seconds", type=float, default=float(RB_SECONDS))
    p.add_argument("--device-in", type=int, default=None)
    p.add_argument("--device-out", type=int, default=None)
    p.add_argument("--start-input-channel", type=int, default=1, help="thread-0 uses this channel, thread-1 uses +1...")
    p.add_argument("--in-channels-num", type=int, default=int(DEFAULT_CHANNELS_NUM[0]), help="total input channels for session")
    p.add_argument("--delay-ms", type=int, default=0)
    p.add_argument("--callback-work-ms", type=float, default=0.0)
    p.add_argument("--no-compare", action="store_true", help="only run multi-stream mode")
    args = p.parse_args()

    sweep = _parse_int_list(args.sweep)
    if not sweep:
        sweep = [int(args.threads)]

    # init ONLY ONCE
    init_engine()
    ad.default.device = tuple(DEVICE)
    ad.default.samplerate = int(SAMPLERATE)
    ad.default.channels = tuple(DEFAULT_CHANNELS_NUM)
    ad.default.rb_seconds = float(RB_SECONDS)
    print("=== defaults ===")
    ad.print_default_devices()
    print(
        "config:",
        {
            "sweep": sweep,
            "seconds": float(args.seconds),
            "samplerate": int(args.samplerate),
            "blocksize": int(args.blocksize),
            "rb_seconds": float(args.rb_seconds),
            "device_in": args.device_in,
            "device_out": args.device_out,
            "start_input_channel": int(args.start_input_channel),
            "in_channels_num": int(args.in_channels_num),
            "delay_ms": int(args.delay_ms),
            "callback_work_ms": float(args.callback_work_ms),
        },
    )

    for n in sweep:
        print(f"\n=== run: threads={n} ===")
        t0 = perf_counter()
        results = _run_once(
            threads=int(n),
            seconds=float(args.seconds),
            samplerate=int(args.samplerate),
            blocksize=int(args.blocksize),
            rb_seconds=float(args.rb_seconds),
            device_in=args.device_in,
            device_out=args.device_out,
            callback_work_ms=float(args.callback_work_ms),
            start_input_channel=int(args.start_input_channel),
            input_channels_num=int(args.in_channels_num),
            delay_ms=int(args.delay_ms),
        )
        elapsed_multi = perf_counter() - t0

        single: SingleStreamFanoutResult | None = None
        elapsed_single: float | None = None
        if not bool(args.no_compare):
            t1 = perf_counter()
            single = _run_single_stream_fanout(
                threads=int(n),
                seconds=float(args.seconds),
                samplerate=int(args.samplerate),
                blocksize=int(args.blocksize),
                rb_seconds=float(args.rb_seconds),
                device_in=args.device_in,
                device_out=args.device_out,
                callback_work_ms=float(args.callback_work_ms),
                start_input_channel=int(args.start_input_channel),
                input_channels_num=int(args.in_channels_num),
                delay_ms=int(args.delay_ms),
            )
            elapsed_single = perf_counter() - t1

        ok_n = sum(1 for r in results if r.ok)
        no_cb_n = sum(1 for r in results if r.ok and r.stream_start_latency_s is None)
        fail_n = len(results) - ok_n

        start_lat_ms = [
            float(_ms(r.stream_start_latency_s))
            for r in results
            if r.stream_start_latency_s is not None
        ]
        all_jitter_ms: list[float] = []
        for r in results:
            all_jitter_ms.extend(r.callback_jitter_ms)

        print(
            f"summary: ok={ok_n}/{len(results)} fail={fail_n} "
            f"no_callback={no_cb_n} elapsed={elapsed_multi:.3f}s"
        )
        print(f"start_latency_ms: {_summarize_ms(start_lat_ms)}")
        print(f"callback_jitter_ms (all threads): {_summarize_ms(all_jitter_ms)}")

        if single is not None:
            sl = _ms(single.stream_start_latency_s)
            sl_s = f"{sl:.3f}ms" if sl is not None else "no_callback"
            print(
                "compare(single_stream): "
                f"ok={single.ok} elapsed={float(elapsed_single or 0.0):.3f}s "
                f"start_latency={sl_s} "
                f"jitter_ms={_summarize_ms(single.callback_jitter_ms)}"
            )

        for r in results:
            tag = f"thread-{r.thread_id}"
            if not r.ok:
                print(f"  {tag}: FAIL {r.error}")
                continue
            sl = _ms(r.stream_start_latency_s)
            sl_s = f"{sl:.3f}ms" if sl is not None else "no_callback"
            print(
                f"  {tag}: OK mapping={r.mapping} start_latency={sl_s} "
                f"cb_count={r.callback_count} status={r.status_count} "
                f"jitter_ms={_summarize_ms(r.callback_jitter_ms)} "
                f"wav={r.wav_path!r}"
            )

        if single is not None:
            if not single.ok:
                print(f"  [single_stream] FAIL {single.error}")
            else:
                for i, wp in enumerate(single.wav_paths):
                    print(
                        f"  [single_stream][writer-{i}] mapping={single.mapping[i:i+1]} "
                        f"written_frames={single.written_frames[i]} dropped_blocks={single.dropped_blocks[i]} "
                        f"wav={wp!r}"
                    )


if __name__ == "__main__":
    main()

