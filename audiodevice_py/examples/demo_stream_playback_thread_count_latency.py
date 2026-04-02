"""
demo_stream_playback_thread_count_latency.py

验证 issue：多线程并发调用流式播放（OutputStream）时，随着线程数增加，
可能出现明显的启动延迟（start_latency）与不稳定（jitter）。

本 demo 按如下原则提供对照：
- 不同设备：每个设备一个 OutputStream（可并发）
- 同一设备不同通道：尽量合并为 **1 个 OutputStream**（把通道合并到一个 mapping），
  在同一个 callback 里分发/混音，避免“同设备多流”带来的 start_latency 排队与递增。

用法：
  # 1) 默认：同一 device_out 上创建 N 个 stream（用于复现问题），并与“按设备合并”对比
  python demo_stream_playback_thread_count_latency.py --threads 4 --seconds 8
  python demo_stream_playback_thread_count_latency.py --sweep 1,2,4,8 --seconds 8

  # 2) 显式指定“每个任务”的 device_out 与 mapping
  #    示例：同时调用输出设备 18 的 2 个输出通道 + 16 的 2 个输出通道
  python demo_stream_playback_thread_count_latency.py --device-outs 18,16 --mappings 1,2;1,2 --seconds 8

  # 3) 演示“同设备分通道任务”合并的收益（raw=4 streams, grouped=2 streams）
  python demo_stream_playback_thread_count_latency.py --device-outs 18,18,16,16 --mappings 1;2;1;2 --seconds 8

  # 4) 放大现象（模拟 callback 重活）
  python demo_stream_playback_thread_count_latency.py --threads 8 --callback-work-ms 1

提示：
- 该 demo 主要观察“启动到首个 callback 的延迟”以及“稳定段 callback 间隔抖动”；
  同设备多流时常见现象是后启动的线程排队更久。
"""

from __future__ import annotations

import argparse
import math
import queue
import statistics
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

import numpy as np

import audiodevice as ad


# ---- constants (similar to demo_stream_output.py) ----
SAMPLERATE = 48_000
BLOCKSIZE = 1024
RB_FRAMES = 4096
OUTPUT_MAPPING = [1, 2]  # 1-based: route callback columns to output channels
DEVICE = (14, 18)  # (device_in, device_out)
DEFAULT_CHANNELS_NUM = (6, 2)  # (in_ch, out_ch) for engine default session
# ---- end constants ----


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


@dataclass
class MultiStreamThreadResult:
    thread_id: int
    ok: bool
    error: str | None
    mapping: list[int]
    stream_start_latency_s: float | None
    callback_jitter_ms: list[float]
    callback_count: int
    status_count: int


@dataclass
class TaskSpec:
    task_id: int
    device_out: int
    mapping: list[int]
    freq_hz: float


def _estimate_prefill_blocks(*, samplerate: int, blocksize: int, rb_frames: int) -> int:
    block_dt = float(blocksize) / float(samplerate) if samplerate > 0 else 0.0
    rb_seconds = float(rb_frames) / float(samplerate) if samplerate > 0 else 0.0
    prefill_s = min(2.0, float(rb_seconds) * 0.2) if block_dt > 0 else 0.0
    return max(4, int(prefill_s / block_dt)) if block_dt > 0 else 0


def _parse_mapping_list(s: str, expected_len: int) -> list[list[int]]:
    """Parse semicolon-separated mappings. Example: '1,2;1;2' -> [[1,2],[1],[2]]."""
    s2 = str(s).strip()
    if not s2:
        return []
    parts = [p.strip() for p in s2.split(";") if p.strip()]
    out: list[list[int]] = []
    for p in parts:
        m = _parse_int_list(p)
        if not m:
            raise ValueError(f"empty mapping in --mappings: {s!r}")
        out.append(m)
    if expected_len > 0 and len(out) != int(expected_len):
        raise ValueError(f"--mappings count must match --device-outs: {len(out)} vs {expected_len}")
    return out


def _run_tasks_raw_streams(
    *,
    tasks: list[TaskSpec],
    seconds: float,
    samplerate: int,
    blocksize: int,
    rb_frames: int,
    device_in: int | None,
    amp: float,
    callback_work_ms: float,
) -> list[MultiStreamThreadResult]:
    if not tasks:
        return []

    prefill_blocks = _estimate_prefill_blocks(samplerate=samplerate, blocksize=blocksize, rb_frames=rb_frames)

    start_ev = threading.Event()
    results: list[MultiStreamThreadResult] = []
    lock = threading.Lock()

    din = int(device_in) if device_in is not None else int(DEVICE[0])

    def worker(task: TaskSpec) -> None:
        err: str | None = None
        ok = False
        cb_count = 0
        status_count = 0
        first_cb_ts: float | None = None
        last_cb_ts: float | None = None
        stream_start_ts: float | None = None
        jitter_ms: list[float] = []

        mapping = list(task.mapping)
        dout = int(task.device_out)
        freq = float(task.freq_hz)
        phase = 0.0

        def callback(indata, outdata, frames, time_info, status):  # noqa: ARG001
            nonlocal cb_count, status_count, first_cb_ts, last_cb_ts, phase
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

            # sine (mono), write into specified output channels
            t = (np.arange(int(frames), dtype=np.float32) + np.float32(phase)) / np.float32(samplerate)
            x = (np.float32(amp) * np.sin(np.float32(2.0 * math.pi) * np.float32(freq) * t)).astype(
                np.float32
            )
            phase += float(frames)

            outdata[:, :] = 0.0
            for ch in mapping:
                idx = int(ch) - 1
                if 0 <= idx < int(outdata.shape[1]):
                    outdata[:, idx] = x

            if last is not None and cb_count > (prefill_blocks + 1):
                dt = float(now - last)
                expected = float(frames) / float(samplerate)
                jitter_ms.append((dt - expected) * 1000.0)

        try:
            if not start_ev.wait(timeout=10.0):
                raise TimeoutError("wait start signal timeout")
            stream = ad.OutputStream(
                device=(din, dout),
                samplerate=int(samplerate),
                blocksize=int(blocksize),
                rb_frames=int(rb_frames),
                output_mapping=list(mapping),
                callback=callback,
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
            start_latency_s = None
            if first_cb_ts is not None and stream_start_ts is not None:
                start_latency_s = float(first_cb_ts - stream_start_ts)
            with lock:
                results.append(
                    MultiStreamThreadResult(
                        thread_id=int(task.task_id),
                        ok=bool(ok),
                        error=err,
                        mapping=list(mapping),
                        stream_start_latency_s=start_latency_s,
                        callback_jitter_ms=jitter_ms,
                        callback_count=int(cb_count),
                        status_count=int(status_count),
                    )
                )

    ts = [threading.Thread(target=worker, args=(t,), daemon=False) for t in tasks]
    for t in ts:
        t.start()
    time.sleep(0.2)
    start_ev.set()
    for t in ts:
        t.join(timeout=float(seconds) + 15.0)
    return sorted(results, key=lambda r: int(r.thread_id))


@dataclass
class GroupedDeviceResult:
    device_out: int
    ok: bool
    error: str | None
    mapping: list[int]  # combined mapping for this device stream
    stream_start_latency_s: float | None
    callback_jitter_ms: list[float]
    callback_count: int
    status_count: int
    producer_dropped: list[int]
    producer_blocks: list[int]


def _run_grouped_by_device_streams(
    *,
    tasks: list[TaskSpec],
    seconds: float,
    samplerate: int,
    blocksize: int,
    rb_frames: int,
    device_in: int | None,
    amp: float,
    callback_work_ms: float,
) -> list[GroupedDeviceResult]:
    if not tasks:
        return []

    prefill_blocks = _estimate_prefill_blocks(samplerate=samplerate, blocksize=blocksize, rb_frames=rb_frames)

    din = int(device_in) if device_in is not None else int(DEVICE[0])

    # group tasks by device_out
    groups: dict[int, list[TaskSpec]] = {}
    for t in tasks:
        groups.setdefault(int(t.device_out), []).append(t)

    out_results: list[GroupedDeviceResult] = []

    def run_one_device_stream(dout: int, dev_tasks: list[TaskSpec]) -> None:
        # combined mapping for this device stream
        combined: list[int] = []
        seen: set[int] = set()
        for ts in dev_tasks:
            for ch in ts.mapping:
                chi = int(ch)
                if chi not in seen:
                    seen.add(chi)
                    combined.append(chi)
        combined.sort()

        # one producer queue per task (mono)
        q_list: list["queue.Queue[np.ndarray]"] = [queue.Queue(maxsize=64) for _ in dev_tasks]
        dropped = [0 for _ in dev_tasks]
        produced = [0 for _ in dev_tasks]
        stop_ev = threading.Event()

        def producer(i: int, task: TaskSpec) -> None:
            freq = float(task.freq_hz)
            phase = 0.0
            dt = float(blocksize) / float(samplerate)
            while not stop_ev.is_set():
                t = (np.arange(int(blocksize), dtype=np.float32) + np.float32(phase)) / np.float32(samplerate)
                blk = (np.float32(amp) * np.sin(np.float32(2.0 * math.pi) * np.float32(freq) * t)).astype(
                    np.float32
                )
                phase += float(blocksize)
                try:
                    q_list[i].put(blk, timeout=0.05)
                    produced[i] += 1
                except queue.Full:
                    dropped[i] += 1
                time.sleep(dt * 0.5)

        prod_threads = [
            threading.Thread(target=producer, args=(i, task), daemon=True)
            for i, task in enumerate(dev_tasks)
        ]
        for t in prod_threads:
            t.start()

        err: str | None = None
        ok = False
        cb_count = 0
        status_count = 0
        first_cb_ts: float | None = None
        last_cb_ts: float | None = None
        stream_start_ts: float | None = None
        jitter_ms: list[float] = []

        def callback(indata, outdata, frames, time_info, status):  # noqa: ARG001
            nonlocal cb_count, status_count, first_cb_ts, last_cb_ts, jitter_ms
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

            outdata[:, :] = 0.0
            for i, task in enumerate(dev_tasks):
                try:
                    blk = q_list[i].get_nowait()
                except queue.Empty:
                    continue
                if int(blk.shape[0]) != int(frames):
                    continue
                # write this task's mono block into its mapping channels
                for ch in task.mapping:
                    idx = int(ch) - 1
                    if 0 <= idx < int(outdata.shape[1]):
                        outdata[:, idx] += blk

            # best-effort anti-clipping when multiple tasks overlap on same channel
            if len(dev_tasks) > 1:
                outdata[:, :] *= np.float32(1.0 / float(len(dev_tasks)))

            if last is not None and cb_count > (prefill_blocks + 1):
                dt = float(now - last)
                expected = float(frames) / float(samplerate)
                jitter_ms.append((dt - expected) * 1000.0)

        try:
            stream = ad.OutputStream(
                device=(din, int(dout)),
                samplerate=int(samplerate),
                blocksize=int(blocksize),
                rb_frames=int(rb_frames),
                output_mapping=list(combined) if combined else None,  # type: ignore[arg-type]
                callback=callback,
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

        start_latency_s = None
        if first_cb_ts is not None and stream_start_ts is not None:
            start_latency_s = float(first_cb_ts - stream_start_ts)

        out_results.append(
            GroupedDeviceResult(
                device_out=int(dout),
                ok=bool(ok),
                error=err,
                mapping=list(combined),
                stream_start_latency_s=start_latency_s,
                callback_jitter_ms=jitter_ms,
                callback_count=int(cb_count),
                status_count=int(status_count),
                producer_dropped=list(dropped),
                producer_blocks=list(produced),
            )
        )

    # run per-device stream in parallel (different devices can be concurrent)
    threads_list = [
        threading.Thread(target=run_one_device_stream, args=(dout, dev_tasks), daemon=False)
        for dout, dev_tasks in groups.items()
    ]
    for t in threads_list:
        t.start()
    for t in threads_list:
        t.join(timeout=float(seconds) + 20.0)

    return sorted(out_results, key=lambda r: int(r.device_out))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--sweep", type=str, default="")
    p.add_argument("--seconds", type=float, default=8.0)
    p.add_argument("--samplerate", type=int, default=int(SAMPLERATE))
    p.add_argument("--blocksize", type=int, default=int(BLOCKSIZE))
    p.add_argument("--rb-frames", type=int, default=int(RB_FRAMES))
    p.add_argument("--device-in", type=int, default=None)
    p.add_argument("--device-out", type=int, default=None)
    p.add_argument("--out-mapping", type=str, default=",".join(str(x) for x in OUTPUT_MAPPING))
    p.add_argument(
        "--device-outs",
        type=str,
        default="",
        help="comma list, one per task. Example: 18,16 or 18,18,16,16",
    )
    p.add_argument(
        "--mappings",
        type=str,
        default="",
        help="semicolon-separated mappings, aligned with --device-outs. Example: '1,2;1,2' or '1;2;1;2'",
    )
    p.add_argument("--amp", type=float, default=0.05)
    p.add_argument("--base-hz", type=float, default=440.0)
    p.add_argument("--callback-work-ms", type=float, default=0.0)
    p.add_argument("--no-compare", action="store_true", help="only run raw multi-stream tasks (no grouping compare)")
    args = p.parse_args()

    sweep = _parse_int_list(args.sweep)
    if not sweep:
        sweep = [int(args.threads)]

    out_mapping = _parse_int_list(args.out_mapping)
    if not out_mapping:
        out_mapping = list(OUTPUT_MAPPING)

    # init ONLY ONCE
    init_engine()
    ad.default.device = tuple(DEVICE)
    ad.default.samplerate = int(SAMPLERATE)
    ad.default.rb_frames = int(args.rb_frames)

    print("=== defaults ===")
    ad.print_default_devices()
    device_outs = _parse_int_list(args.device_outs)
    mappings = _parse_mapping_list(args.mappings, expected_len=len(device_outs)) if device_outs else []

    print(
        "config:",
        {
            "sweep": sweep,
            "seconds": float(args.seconds),
            "samplerate": int(args.samplerate),
            "blocksize": int(args.blocksize),
            "rb_frames": int(args.rb_frames),
            "device_in": args.device_in,
            "device_out": args.device_out,
            "out_mapping_default": list(out_mapping),
            "callback_out_ch_default": len(out_mapping),
            "device_outs": device_outs,
            "mappings": mappings,
            "amp": float(args.amp),
            "base_hz": float(args.base_hz),
            "callback_work_ms": float(args.callback_work_ms),
        },
    )

    # If explicit tasks are provided, run once (no sweep).
    if device_outs:
        tasks = [
            TaskSpec(
                task_id=i,
                device_out=int(device_outs[i]),
                mapping=list(mappings[i]),
                freq_hz=float(args.base_hz) + 17.0 * float(i),
            )
            for i in range(len(device_outs))
        ]
        print(f"\n=== run: tasks={len(tasks)} (raw streams) ===")
        t0 = perf_counter()
        raw = _run_tasks_raw_streams(
            tasks=tasks,
            seconds=float(args.seconds),
            samplerate=int(args.samplerate),
            blocksize=int(args.blocksize),
            rb_frames=int(args.rb_frames),
            device_in=args.device_in,
            amp=float(args.amp),
            callback_work_ms=float(args.callback_work_ms),
        )
        elapsed_raw = perf_counter() - t0
        ok_n = sum(1 for r in raw if r.ok)
        fail_n = len(raw) - ok_n
        start_lat_ms = [float(_ms(r.stream_start_latency_s)) for r in raw if r.stream_start_latency_s is not None]
        all_jitter: list[float] = []
        for r in raw:
            all_jitter.extend(r.callback_jitter_ms)
        print(f"summary(raw): ok={ok_n}/{len(raw)} fail={fail_n} elapsed={elapsed_raw:.3f}s")
        print(f"start_latency_ms: {_summarize_ms(start_lat_ms)}")
        print(f"callback_jitter_ms(all): {_summarize_ms(all_jitter)}")
        for r in raw:
            if not r.ok:
                print(f"  task-{r.thread_id}: FAIL {r.error}")
                continue
            sl = _ms(r.stream_start_latency_s)
            sl_s = f"{sl:.3f}ms" if sl is not None else "no_callback"
            print(
                f"  task-{r.thread_id}: OK device_out={tasks[r.thread_id].device_out} mapping={r.mapping} "
                f"start_latency={sl_s} cb_count={r.callback_count} status={r.status_count} "
                f"jitter_ms={_summarize_ms(r.callback_jitter_ms)}"
            )

        if not bool(args.no_compare):
            print("\n=== compare: grouped by device_out (one stream per device) ===")
            t1 = perf_counter()
            grouped = _run_grouped_by_device_streams(
                tasks=tasks,
                seconds=float(args.seconds),
                samplerate=int(args.samplerate),
                blocksize=int(args.blocksize),
                rb_frames=int(args.rb_frames),
                device_in=args.device_in,
                amp=float(args.amp),
                callback_work_ms=float(args.callback_work_ms),
            )
            elapsed_grouped = perf_counter() - t1
            ok_g = sum(1 for g in grouped if g.ok)
            fail_g = len(grouped) - ok_g
            start_g_ms = [float(_ms(g.stream_start_latency_s)) for g in grouped if g.stream_start_latency_s is not None]
            jitter_g: list[float] = []
            for g in grouped:
                jitter_g.extend(g.callback_jitter_ms)
            print(
                f"summary(grouped): ok={ok_g}/{len(grouped)} fail={fail_g} elapsed={elapsed_grouped:.3f}s"
            )
            print(f"start_latency_ms: {_summarize_ms(start_g_ms)}")
            print(f"callback_jitter_ms(all): {_summarize_ms(jitter_g)}")
            for g in grouped:
                if not g.ok:
                    print(f"  device_out={g.device_out}: FAIL {g.error}")
                    continue
                sl = _ms(g.stream_start_latency_s)
                sl_s = f"{sl:.3f}ms" if sl is not None else "no_callback"
                print(
                    f"  device_out={g.device_out}: OK mapping={g.mapping} start_latency={sl_s} "
                    f"cb_count={g.callback_count} status={g.status_count} "
                    f"jitter_ms={_summarize_ms(g.callback_jitter_ms)} "
                    f"producer_dropped={sum(g.producer_dropped)}"
                )
        return

    # Otherwise: use sweep and build N tasks on the same device_out (for reproducing the issue).
    dout_default = int(args.device_out) if args.device_out is not None else int(DEVICE[1])
    for n in sweep:
        tasks = [
            TaskSpec(
                task_id=i,
                device_out=int(dout_default),
                mapping=list(out_mapping),
                freq_hz=float(args.base_hz) + 17.0 * float(i),
            )
            for i in range(int(n))
        ]
        print(f"\n=== run: tasks={len(tasks)} (raw streams, same device_out={dout_default}) ===")
        t0 = perf_counter()
        raw = _run_tasks_raw_streams(
            tasks=tasks,
            seconds=float(args.seconds),
            samplerate=int(args.samplerate),
            blocksize=int(args.blocksize),
            rb_frames=int(args.rb_frames),
            device_in=args.device_in,
            amp=float(args.amp),
            callback_work_ms=float(args.callback_work_ms),
        )
        elapsed_raw = perf_counter() - t0
        ok_n = sum(1 for r in raw if r.ok)
        fail_n = len(raw) - ok_n
        start_lat_ms = [float(_ms(r.stream_start_latency_s)) for r in raw if r.stream_start_latency_s is not None]
        all_jitter: list[float] = []
        for r in raw:
            all_jitter.extend(r.callback_jitter_ms)
        print(f"summary(raw): ok={ok_n}/{len(raw)} fail={fail_n} elapsed={elapsed_raw:.3f}s")
        print(f"start_latency_ms: {_summarize_ms(start_lat_ms)}")
        print(f"callback_jitter_ms(all): {_summarize_ms(all_jitter)}")

        if bool(args.no_compare):
            continue

        print(f"=== compare: grouped by device_out (threads={n}) ===")
        t1 = perf_counter()
        grouped = _run_grouped_by_device_streams(
            tasks=tasks,
            seconds=float(args.seconds),
            samplerate=int(args.samplerate),
            blocksize=int(args.blocksize),
            rb_frames=int(args.rb_frames),
            device_in=args.device_in,
            amp=float(args.amp),
            callback_work_ms=float(args.callback_work_ms),
        )
        elapsed_grouped = perf_counter() - t1
        ok_g = sum(1 for g in grouped if g.ok)
        fail_g = len(grouped) - ok_g
        start_g_ms = [float(_ms(g.stream_start_latency_s)) for g in grouped if g.stream_start_latency_s is not None]
        jitter_g: list[float] = []
        for g in grouped:
            jitter_g.extend(g.callback_jitter_ms)
        print(f"summary(grouped): ok={ok_g}/{len(grouped)} fail={fail_g} elapsed={elapsed_grouped:.3f}s")
        print(f"start_latency_ms: {_summarize_ms(start_g_ms)}")
        print(f"callback_jitter_ms(all): {_summarize_ms(jitter_g)}")


if __name__ == "__main__":
    main()

