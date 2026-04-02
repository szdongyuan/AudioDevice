from __future__ import annotations

from queue import Queue
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Optional

import numpy as np

# ============================================
# 可修改配置区
# ============================================
THREAD_COUNT = 10

# 确保优先导入仓库内的 `audiodevice_py/audiodevice`，避免误用 site-packages 里的同名包
_PKG_ROOT = Path(__file__).resolve().parents[1]
if (_PKG_ROOT / "audiodevice").is_dir():
    sys.path.insert(0, str(_PKG_ROOT))

# 打印控制
# - True: 打印更聚焦的“关键阶段耗时”(推荐用于定位延迟)
# - False: 只打印最终 OK/FAIL
PRINT_THREAD_TIMING = True
# 是否把 chirp 生成挪到 start_event 之后，便于区分“生成音频慢”还是“初始化/启动流慢”
GENERATE_AUDIO_AFTER_START_EVENT = True
# 是否在回调里打印每次回调间隔（会刷屏，默认关）
PRINT_CALLBACK_INTERVALS = False
# audiodevice 预热：先用一个很短的静音流启动一次 session/设备，把“首次启动成本”提前支付掉
AUDIODEVICE_WARMUP = True
AUDIODEVICE_WARMUP_SEC = 0.3
# 引擎环形缓冲容量（帧）。值越大，某些后端/设备的“启动/预填充”成本可能越高。
AUDIODEVICE_RB_FRAMES = 4096
# 是否强制把 stream.start() 串行化（用于验证引擎内部是否存在全局排队/锁）
SERIALIZE_STREAM_START = False
# 单 stream 模式的每线程队列容量（block 数）
SINGLE_STREAM_QUEUE_MAX_BLOCKS = 64
# 在启动单 stream 前，尽量等每个 producer 先准备好若干 block，降低首个 callback 空转概率
SINGLE_STREAM_QUEUE_PREFILL_BLOCKS = 2
# 单 stream 模式下，多个逻辑线程若落到同一物理输出通道，按该通道重叠数自动衰减
SINGLE_STREAM_AUTO_ATTENUATE = True

# 线程 -> 扫频映射（Hz）
# 要求：
# 1) thread 必须覆盖 0..THREAD_COUNT-1
# 2) 每个 thread 只能出现一次
# 3) f0/f1 必须 > 0，且 f0 != f1
THREAD_AUDIO_MAP: list[dict[str, Any]] = [
    {"thread": 0, "f0": 261.63, "f1": 1046.52},
    {"thread": 1, "f0": 329.63, "f1": 1318.52},
    {"thread": 2, "f0": 392.00, "f1": 1568.00},
    {"thread": 3, "f0": 440.00, "f1": 1760.00},
    {"thread": 4, "f0": 493.88, "f1": 1975.52},
    {"thread": 5, "f0": 293.66, "f1": 1174.64},
    {"thread": 6, "f0": 349.23, "f1": 1396.92},
    {"thread": 7, "f0": 415.30, "f1": 1661.20},
    {"thread": 8, "f0": 466.16, "f1": 1864.64},
    {"thread": 9, "f0": 277.18, "f1": 1108.72},
]

DEVICE: tuple[int, int] = (14, 18)  # (input_device, output_device)
DEFAULT_CHANNELS_NUM = (6, 2)  # (in_ch, out_ch) for engine default session

SAMPLERATE = 48000
TONE_DURATION_SEC = 4.0
TONE_AMPLITUDE = 0.2
# 在 TONE_DURATION_SEC 总时长内重复扫频的次数
TONE_REPEAT_COUNT = 4
CHIRP_FADE_SEC = 0.01

# 轮转场景：
# - 每轮所有 10 个线程同步启动
# - 每轮固定播放/录制/播放录制 80 -> 2000 Hz 的 4s chirp
# - 共执行 10 轮；第 r 轮让 thread-r 使用 in_map=[1]，其余线程使用 in_map=[2]
ROTATING_CHIRP_F0_HZ = 80.0
ROTATING_CHIRP_F1_HZ = 2000.0
ROTATING_ROUND_DURATION_SEC = 4.0
ROTATING_ROUND_COUNT = 10
ROTATING_PRIMARY_INPUT_MAPPING = [1]
ROTATING_SECONDARY_INPUT_MAPPING = [2]

# 每个线程的输入/输出通道与 mapping
THREAD_INPUT_CHANNELS = [6, 6, 6, 6, 6, 6, 6, 6, 6, 6]
THREAD_INPUT_MAPPING: list[list[int]] = [
    [1],
    [1],
    [1],
    [1],
    [1],
    [1],
    [1],
    [1],
    [1],
    [1],
]
THREAD_OUTPUT_CHANNELS = [2, 2, 2, 2, 2, 2, 2, 2, 2, 2]
THREAD_OUTPUT_MAPPING: list[list[int]] = [
    [1],
    [1],
    [1],
    [1],
    [1],
    [1],
    [1],
    [1],
    [1],
    [1],
]

BLOCKSIZE = 1024
RESULT_TIMEOUT = 60.0
# 等待 audiodevice session 真正启动的最长时间（用于把“启动延迟”从墙钟时长里分离出来）
WAIT_SESSION_START_TIMEOUT_SEC = 5.0
# 播放完后额外等待一点尾巴，避免 close() 截断设备/引擎缓冲
PLAY_TAIL_SEC = 0.2
# 录音/回采窗口完成后，留一点时间让 callback 线程退出
CAPTURE_TAIL_SEC = 0.2
# InputStream 专用 delay_time；默认 0，避免把采集窗口再额外后移
INPUT_DELAY_MS = 0.0
# sounddevice 回调流在 Windows 子线程 + WASAPI 组合下容易启动失败，
# 这里默认自动尝试切换到同名非 WASAPI 输出设备（优先 WDM-KS）。
SOUNDDEVICE_AVOID_WASAPI_IN_THREADS = True

# audiodevice 引擎初始化（可按需改）
AUDIODEVICE_ENGINE_DIR = Path(__file__).resolve().parent / "AudioDevice-master" / "audiodevice_py"
AUDIODEVICE_ENGINE_EXE = AUDIODEVICE_ENGINE_DIR / "audiodevice.exe"

# 运行入口模式:
# - "audiodevice_play":     同设备多线程播放（每线程各自初始化同一 output 设备）
# - "audiodevice_record":   同设备多线程录制（每线程各自初始化同一 input 设备）
# - "audiodevice_playrec":  同设备多线程播放录制（每线程各自初始化同一 duplex 设备）
# - "audiodevice_single_stream": 同设备单 OutputStream + 多 producer，对照规避方案
# - "audiodevice_single_input_stream": 同设备单 InputStream + 多 consumer
# - "audiodevice_single_duplex_stream": 同设备单 duplex Stream + 多逻辑线程
# - "sounddevice_play":     sounddevice 对照路径
RUN_MODE = "audiodevice_play"


def _init_audiodevice_engine(ad_module) -> None:
    if AUDIODEVICE_ENGINE_EXE.is_file():
        ad_module.init(
            engine_exe=str(AUDIODEVICE_ENGINE_EXE),
            engine_cwd=str(AUDIODEVICE_ENGINE_DIR),
            timeout=10,
        )
    else:
        ad_module.init(timeout=10)


def _validate_mapping_list(
    mappings: list[list[int]],
    channels: list[int],
    *,
    mapping_name: str,
    channels_name: str,
) -> None:
    if len(channels) != THREAD_COUNT:
        raise ValueError(
            f"{channels_name} 长度必须等于 THREAD_COUNT，当前为 {len(channels)} vs {THREAD_COUNT}"
        )
    if len(mappings) != THREAD_COUNT:
        raise ValueError(
            f"{mapping_name} 长度必须等于 THREAD_COUNT，当前为 {len(mappings)} vs {THREAD_COUNT}"
        )
    for idx, ch in enumerate(channels):
        if int(ch) <= 0:
            raise ValueError(f"{channels_name}[{idx}] 必须 > 0")
    for idx, mapping in enumerate(mappings):
        if not isinstance(mapping, list) or len(mapping) == 0:
            raise ValueError(f"{mapping_name}[{idx}] 必须是非空列表")
        max_ch = int(channels[idx])
        for ch in mapping:
            ch_i = int(ch)
            if ch_i < 1:
                raise ValueError(f"{mapping_name}[{idx}] 的通道号必须 >= 1")
            if ch_i > max_ch:
                raise ValueError(
                    f"{mapping_name}[{idx}] 中通道号 {ch_i} 超过该线程 {channels_name}={max_ch}"
                )


def _validate_and_build_entries() -> list[dict[str, Any]]:
    if THREAD_COUNT <= 0:
        raise ValueError("THREAD_COUNT 必须 > 0")

    _validate_mapping_list(
        THREAD_INPUT_MAPPING,
        THREAD_INPUT_CHANNELS,
        mapping_name="THREAD_INPUT_MAPPING",
        channels_name="THREAD_INPUT_CHANNELS",
    )
    _validate_mapping_list(
        THREAD_OUTPUT_MAPPING,
        THREAD_OUTPUT_CHANNELS,
        mapping_name="THREAD_OUTPUT_MAPPING",
        channels_name="THREAD_OUTPUT_CHANNELS",
    )

    if len(THREAD_AUDIO_MAP) != THREAD_COUNT:
        raise ValueError(
            f"THREAD_AUDIO_MAP 数量({len(THREAD_AUDIO_MAP)})必须等于 THREAD_COUNT({THREAD_COUNT})"
        )

    seen: set[int] = set()
    entries: list[dict[str, Any]] = []
    for item in THREAD_AUDIO_MAP:
        tid = int(item["thread"])
        f0 = float(item["f0"])
        f1 = float(item["f1"])
        if f0 <= 0 or f1 <= 0:
            raise ValueError(f"THREAD_AUDIO_MAP[{tid}] 的 f0/f1 必须 > 0，当前: f0={f0}, f1={f1}")
        if f0 == f1:
            raise ValueError(f"THREAD_AUDIO_MAP[{tid}] 的 f0 与 f1 不能相同，当前: {f0}")
        if tid < 0 or tid >= THREAD_COUNT:
            raise ValueError(f"thread 索引越界: {tid}, 需在 [0, {THREAD_COUNT - 1}]")
        if tid in seen:
            raise ValueError(f"thread 重复定义: {tid}")
        seen.add(tid)
        entries.append(
            {
                "thread": tid,
                "f0_hz": f0,
                "f1_hz": f1,
                "input_channels": int(THREAD_INPUT_CHANNELS[tid]),
                "input_mapping": [int(v) for v in THREAD_INPUT_MAPPING[tid]],
                "output_channels": int(THREAD_OUTPUT_CHANNELS[tid]),
                "output_mapping": [int(v) for v in THREAD_OUTPUT_MAPPING[tid]],
            }
        )

    missing = [i for i in range(THREAD_COUNT) if i not in seen]
    if missing:
        raise ValueError(f"THREAD_AUDIO_MAP 缺少线程: {missing}")

    entries.sort(key=lambda x: int(x["thread"]))
    return entries


def _generate_chirp_segment_float32(
    *,
    f0_hz: float,
    f1_hz: float,
    frame_count: int,
    samplerate: int,
) -> np.ndarray:
    if frame_count <= 0:
        return np.zeros((0, 1), dtype=np.float32)
    sr = float(samplerate)
    t = np.arange(frame_count, dtype=np.float64) / sr
    duration_sec = max(float(frame_count) / sr, 1.0 / sr)
    k = (float(f1_hz) - float(f0_hz)) / duration_sec
    phase = 2.0 * np.pi * (float(f0_hz) * t + 0.5 * k * np.square(t))
    y = np.sin(phase).astype(np.float32)

    fade_n = int(round(float(CHIRP_FADE_SEC) * sr))
    fade_n = min(fade_n, frame_count // 2)
    if fade_n > 0:
        w = np.ones(frame_count, dtype=np.float32)
        w[:fade_n] = np.linspace(0.0, 1.0, fade_n, dtype=np.float32)
        w[-fade_n:] = np.linspace(1.0, 0.0, fade_n, dtype=np.float32)
        y *= w

    y *= np.float32(TONE_AMPLITUDE)
    return np.clip(y[:, None], -1.0, 1.0)


def _generate_repeated_chirp_float32(
    *,
    f0_hz: float,
    f1_hz: float,
    duration_sec: float,
    samplerate: int,
    repeat_count: int = 1,
) -> np.ndarray:
    """生成单声道 float32 重复线性扫频，形状 (frames, 1)。"""
    frame_count = int(round(float(duration_sec) * float(samplerate)))
    if frame_count <= 0:
        raise ValueError(f"生成音频帧数必须 > 0，当前 duration={duration_sec}, sr={samplerate}")
    if int(repeat_count) <= 0:
        raise ValueError(f"repeat_count 必须 > 0，当前: {repeat_count}")

    repeat_n = int(repeat_count)
    base = frame_count // repeat_n
    extra = frame_count % repeat_n
    chunks: list[np.ndarray] = []
    for idx in range(repeat_n):
        seg_frames = int(base + (1 if idx < extra else 0))
        chunks.append(
            _generate_chirp_segment_float32(
                f0_hz=float(f0_hz),
                f1_hz=float(f1_hz),
                frame_count=seg_frames,
                samplerate=int(samplerate),
            )
        )
    if not chunks:
        return np.zeros((0, 1), dtype=np.float32)
    return np.concatenate(chunks, axis=0).astype(np.float32, copy=False)


def _adapt_channels(audio: np.ndarray, out_channels: int) -> np.ndarray:
    x = np.asarray(audio, dtype=np.float32)
    if x.ndim == 1:
        x = x[:, None]
    if x.shape[1] == out_channels:
        return x
    if x.shape[1] > out_channels:
        return x[:, :out_channels]
    pad = np.zeros((x.shape[0], out_channels - x.shape[1]), dtype=np.float32)
    return np.concatenate([x, pad], axis=1)


def _active_seconds(x: np.ndarray, samplerate: int) -> float:
    y = np.asarray(x, dtype=np.float32)
    if y.ndim == 1:
        y = y[:, None]
    mask = np.any(np.abs(y) > 1e-6, axis=1)
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return 0.0
    return round(float(idx[-1] - idx[0] + 1) / float(samplerate), 4)


def _estimate_prefill_blocks(*, samplerate: int, blocksize: int, rb_frames: int) -> int:
    block_dt = float(blocksize) / float(samplerate) if int(samplerate) > 0 else 0.0
    if block_dt <= 0.0:
        return 0
    rb_s = float(rb_frames) / float(samplerate) if int(samplerate) > 0 else 0.0
    prefill_s = min(2.0, float(rb_s) * 0.2)
    return max(4, int(prefill_s / block_dt))


def _summarize_ms(values_ms: list[float]) -> str:
    if not values_ms:
        return "n=0"
    xs = sorted(float(v) for v in values_ms)
    n = len(xs)
    p50 = xs[int(round(0.50 * (n - 1)))]
    p90 = xs[int(round(0.90 * (n - 1)))]
    return (
        f"n={n} mean={sum(xs) / float(n):.3f} "
        f"p50={p50:.3f} p90={p90:.3f} min={xs[0]:.3f} max={xs[-1]:.3f}"
    )


def _wait_for_audiodevice_session(ad_module) -> float:
    t0 = time.time()
    while True:
        st = ad_module.get_status() or {}
        if bool(st.get("has_session", False)):
            break
        if (time.time() - t0) >= float(WAIT_SESSION_START_TIMEOUT_SEC):
            break
        ad_module.sleep(50)
    return round(time.time() - t0, 6)


def _warmup_audiodevice_stream(ad_module, *, in_channels: int = 0, out_channels: int = 0) -> None:
    if not AUDIODEVICE_WARMUP:
        return

    mode = "output"
    if int(in_channels) > 0 and int(out_channels) > 0:
        mode = "duplex"
    elif int(in_channels) > 0:
        mode = "input"

    if mode == "output":
        def _warm_cb(indata, outdata, frames, time_info, status) -> None:  # noqa: ARG001
            outdata.fill(0.0)

        warm = ad_module.OutputStream(
            samplerate=int(SAMPLERATE),
            blocksize=int(BLOCKSIZE),
            channels=int(out_channels),
            callback=_warm_cb,
        )
    elif mode == "input":
        def _warm_cb(indata, outdata, frames, time_info, status) -> None:  # noqa: ARG001
            _ = indata

        warm = ad_module.InputStream(
            samplerate=int(SAMPLERATE),
            blocksize=int(BLOCKSIZE),
            channels=int(in_channels),
            callback=_warm_cb,
            delay_time=float(INPUT_DELAY_MS),
        )
    else:
        def _warm_cb(indata, outdata, frames, time_info, status) -> None:  # noqa: ARG001
            _ = indata
            outdata.fill(0.0)

        warm = ad_module.Stream(
            samplerate=int(SAMPLERATE),
            blocksize=int(BLOCKSIZE),
            channels=(int(in_channels), int(out_channels)),
            callback=_warm_cb,
        )

    t_w0 = time.perf_counter()
    warm.start()
    time.sleep(max(float(AUDIODEVICE_WARMUP_SEC), 0.05))
    warm.close()
    t_w1 = time.perf_counter()
    print(
        f"[main][audiodevice] warmup({mode})={round(t_w1 - t_w0, 6)}s "
        f"(sec={AUDIODEVICE_WARMUP_SEC})"
    )


def _new_result(lib_name: str, entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "thread": int(entry["thread"]),
        "f0_hz": float(entry["f0_hz"]),
        "f1_hz": float(entry["f1_hz"]),
        "ok": False,
        "error_type": None,
        "error_msg": None,
        "traceback": None,
        "elapsed_sec": None,
        "frames": None,
        "shape": None,
        "record_shape": None,
        "library": lib_name,
        "input_channels": int(entry["input_channels"]),
        "input_mapping": list(entry["input_mapping"]),
        "output_channels": int(entry["output_channels"]),
        "output_mapping": list(entry["output_mapping"]),
        "active_seconds": None,
        "t_prep_audio_sec": None,
        "t_wait_event_sec": None,
        "t_stream_ctor_sec": None,
        "t_stream_start_sec": None,
        "t_session_wait_sec": None,
        "t_first_callback_delay_sec": None,
    }


def _print_thread_timing(result: dict[str, Any], *, t0: float, t_end: float) -> None:
    if not PRINT_THREAD_TIMING:
        return
    print(
        f"[{result['library']}][thread-{result['thread']}] "
        f"wait={result['t_wait_event_sec']}s, "
        f"prep={result['t_prep_audio_sec']}s, "
        f"ctor={result['t_stream_ctor_sec']}s, "
        f"start={result['t_stream_start_sec']}s, "
        f"sess={result['t_session_wait_sec']}s, "
        f"first_cb={result['t_first_callback_delay_sec']}s, "
        f"total={round(t_end - t0, 6)}s"
    )


def _prepare_output_audio(entry: dict[str, Any]) -> tuple[np.ndarray, int]:
    audio = _generate_repeated_chirp_float32(
        f0_hz=float(entry["f0_hz"]),
        f1_hz=float(entry["f1_hz"]),
        duration_sec=float(TONE_DURATION_SEC),
        samplerate=int(SAMPLERATE),
        repeat_count=int(TONE_REPEAT_COUNT),
    )
    x = _adapt_channels(audio, max(1, int(len(entry["output_mapping"]))))
    return x, int(x.shape[0])


def _slice_input(indata: np.ndarray, mapping_cols: list[int], take: int) -> np.ndarray:
    if take <= 0:
        return np.zeros((0, len(mapping_cols)), dtype=np.float32)
    blk = np.asarray(indata[:take], dtype=np.float32)
    if blk.ndim == 1:
        blk = blk[:, None]
    return blk[:, mapping_cols].copy()


def _build_combined_mapping_indices(
    entries: list[dict[str, Any]],
    *,
    mapping_key: str,
) -> tuple[list[int], dict[int, list[int]]]:
    seen: dict[int, int] = {}
    combined: list[int] = []
    cols_by_tid: dict[int, list[int]] = {}
    for entry in entries:
        tid = int(entry["thread"])
        cols: list[int] = []
        for ch in entry[mapping_key]:
            ch_i = int(ch)
            if ch_i not in seen:
                seen[ch_i] = len(combined)
                combined.append(ch_i)
            cols.append(int(seen[ch_i]))
        cols_by_tid[tid] = cols
    return combined, cols_by_tid


def _apply_shared_stream_metrics(results: list[dict[str, Any]], metrics: dict[str, Any]) -> None:
    for r in results:
        if r.get("t_stream_start_sec") is None and metrics.get("t_stream_start_sec") is not None:
            r["t_stream_start_sec"] = float(metrics["t_stream_start_sec"])
        if r.get("t_session_wait_sec") is None and metrics.get("t_session_wait_sec") is not None:
            r["t_session_wait_sec"] = float(metrics["t_session_wait_sec"])
        if r.get("t_first_callback_delay_sec") is None and metrics.get("t_first_callback_delay_sec") is not None:
            r["t_first_callback_delay_sec"] = float(metrics["t_first_callback_delay_sec"])


def _thread_output_worker(
    *,
    lib_name: str,
    stream_ctor,
    stream_kwargs: dict[str, Any],
    entry: dict[str, Any],
    start_event: threading.Event,
    result_list: list[dict[str, Any]],
    result_lock: threading.Lock,
    stream_start_lock: Optional[threading.Lock] = None,
    audiodevice_style_callback: bool = True,
    audiodevice_module=None,
) -> None:
    t0 = time.perf_counter()
    result = _new_result(lib_name, entry)
    try:
        mapping_cols = [int(ch) - 1 for ch in entry["output_mapping"]]
        x: Optional[np.ndarray] = None
        total_frames = 0
        cursor = [0]
        first_cb_ts = [None]
        last_cb_ts = [None]

        t_wait0 = time.perf_counter()
        if not start_event.wait(timeout=10.0):
            raise TimeoutError("等待统一启动信号超时")
        t_wait1 = time.perf_counter()
        result["t_wait_event_sec"] = round(t_wait1 - t_wait0, 6)

        if GENERATE_AUDIO_AFTER_START_EVENT:
            t_prep0 = time.perf_counter()
            x, total_frames = _prepare_output_audio(entry)
            t_prep1 = time.perf_counter()
            result["t_prep_audio_sec"] = round(t_prep1 - t_prep0, 6)
        else:
            t_prep0 = time.perf_counter()
            x, total_frames = _prepare_output_audio(entry)
            t_prep1 = time.perf_counter()
            result["t_prep_audio_sec"] = round(t_prep1 - t_prep0, 6)

        def _copy_to_out(outdata: np.ndarray, frames: int) -> None:
            now = time.perf_counter()
            if first_cb_ts[0] is None:
                first_cb_ts[0] = now
            if PRINT_CALLBACK_INTERVALS:
                if last_cb_ts[0] is not None:
                    print(f"[{lib_name}][thread-{entry['thread']}] cb_dt={now - last_cb_ts[0]:.6f}s")
                last_cb_ts[0] = now

            remain = int(total_frames) - cursor[0]
            take = int(min(int(frames), max(remain, 0)))
            outdata.fill(0.0)
            if take > 0 and x is not None:
                blk = x[cursor[0] : cursor[0] + take]
                for src_col, dst_col in enumerate(mapping_cols):
                    if 0 <= int(dst_col) < int(outdata.shape[1]) and src_col < int(blk.shape[1]):
                        outdata[:take, int(dst_col)] = blk[:, int(src_col)]
                cursor[0] += int(take)

        if audiodevice_style_callback:
            def callback(indata, outdata, frames, time_info, status) -> None:  # noqa: ARG001
                _copy_to_out(outdata, int(frames))
        else:
            def callback(outdata, frames, time_info, status) -> None:  # noqa: ARG001
                _copy_to_out(outdata, int(frames))

        t_ctor0 = time.perf_counter()
        stream = stream_ctor(callback=callback, **stream_kwargs)
        t_ctor1 = time.perf_counter()
        result["t_stream_ctor_sec"] = round(t_ctor1 - t_ctor0, 6)

        t_start0 = time.perf_counter()
        if stream_start_lock is None:
            stream.start()
        else:
            with stream_start_lock:
                stream.start()
        t_start1 = time.perf_counter()
        result["t_stream_start_sec"] = round(t_start1 - t_start0, 6)

        try:
            if audiodevice_module is not None:
                result["t_session_wait_sec"] = _wait_for_audiodevice_session(audiodevice_module)
            duration_sec = float(total_frames) / float(SAMPLERATE) if int(SAMPLERATE) > 0 else 0.0
            t_end_wall = time.time() + duration_sec
            while time.time() < t_end_wall:
                time.sleep(0.01)
            time.sleep(max(float(PLAY_TAIL_SEC), 2.0 * float(BLOCKSIZE) / float(SAMPLERATE)))
        finally:
            stream.close()
            t_done = time.perf_counter()
            if first_cb_ts[0] is not None:
                result["t_first_callback_delay_sec"] = round(float(first_cb_ts[0]) - float(t_start1), 6)
            _print_thread_timing(result, t0=t0, t_end=t_done)

        result["ok"] = True
        result["frames"] = int(total_frames)
        result["shape"] = tuple(x.shape) if x is not None else None
    except Exception as exc:  # noqa: BLE001
        result["error_type"] = type(exc).__name__
        result["error_msg"] = str(exc)
        result["traceback"] = traceback.format_exc()
    finally:
        result["elapsed_sec"] = round(time.perf_counter() - t0, 4)
        with result_lock:
            result_list.append(result)


def _thread_input_worker(
    *,
    lib_name: str,
    ad_module,
    entry: dict[str, Any],
    start_event: threading.Event,
    result_list: list[dict[str, Any]],
    result_lock: threading.Lock,
    stream_start_lock: Optional[threading.Lock] = None,
) -> None:
    t0 = time.perf_counter()
    result = _new_result(lib_name, entry)
    stream = None
    try:
        mapping_cols = [int(ch) - 1 for ch in entry["input_mapping"]]
        target_frames = int(round(float(TONE_DURATION_SEC) * float(SAMPLERATE)))
        captured = [0]
        chunks: list[np.ndarray] = []
        first_cb_ts = [None]
        last_cb_ts = [None]
        done_event = threading.Event()

        t_wait0 = time.perf_counter()
        if not start_event.wait(timeout=10.0):
            raise TimeoutError("等待统一启动信号超时")
        t_wait1 = time.perf_counter()
        result["t_wait_event_sec"] = round(t_wait1 - t_wait0, 6)
        result["t_prep_audio_sec"] = 0.0

        def callback(indata, outdata, frames, time_info, status) -> None:  # noqa: ARG001
            now = time.perf_counter()
            if first_cb_ts[0] is None:
                first_cb_ts[0] = now
            if PRINT_CALLBACK_INTERVALS:
                if last_cb_ts[0] is not None:
                    print(f"[{lib_name}][thread-{entry['thread']}] cb_dt={now - last_cb_ts[0]:.6f}s")
                last_cb_ts[0] = now

            remain = int(target_frames) - int(captured[0])
            if remain <= 0:
                done_event.set()
                raise ad_module.CallbackStop()
            take = int(min(int(frames), remain))
            if take > 0:
                chunks.append(_slice_input(indata, mapping_cols, take))
                captured[0] += int(take)
            if captured[0] >= target_frames:
                done_event.set()
                raise ad_module.CallbackStop()

        t_ctor0 = time.perf_counter()
        stream = ad_module.InputStream(
            samplerate=int(SAMPLERATE),
            blocksize=int(BLOCKSIZE),
            channels=int(entry["input_channels"]),
            callback=callback,
            delay_time=float(INPUT_DELAY_MS),
        )
        t_ctor1 = time.perf_counter()
        result["t_stream_ctor_sec"] = round(t_ctor1 - t_ctor0, 6)

        t_start0 = time.perf_counter()
        if stream_start_lock is None:
            stream.start()
        else:
            with stream_start_lock:
                stream.start()
        t_start1 = time.perf_counter()
        result["t_stream_start_sec"] = round(t_start1 - t_start0, 6)
        result["t_session_wait_sec"] = _wait_for_audiodevice_session(ad_module)

        wait_timeout = float(TONE_DURATION_SEC) + float(INPUT_DELAY_MS) / 1000.0 + float(WAIT_SESSION_START_TIMEOUT_SEC) + 2.0
        done_event.wait(timeout=wait_timeout)
        time.sleep(float(CAPTURE_TAIL_SEC))
        try:
            stream.close()
        finally:
            t_done = time.perf_counter()
            if first_cb_ts[0] is not None:
                result["t_first_callback_delay_sec"] = round(float(first_cb_ts[0]) - float(t_start1), 6)
            _print_thread_timing(result, t0=t0, t_end=t_done)

        if not chunks:
            raise RuntimeError("NoData: 没有录到数据")

        data = np.concatenate(chunks, axis=0)
        if data.shape[0] > target_frames:
            data = data[:target_frames]
        result["ok"] = True
        result["frames"] = int(data.shape[0])
        result["shape"] = tuple(data.shape)
        result["active_seconds"] = _active_seconds(data, int(SAMPLERATE))
    except Exception as exc:  # noqa: BLE001
        result["error_type"] = type(exc).__name__
        result["error_msg"] = str(exc)
        result["traceback"] = traceback.format_exc()
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass
    finally:
        result["elapsed_sec"] = round(time.perf_counter() - t0, 4)
        with result_lock:
            result_list.append(result)


def _thread_duplex_worker(
    *,
    lib_name: str,
    ad_module,
    entry: dict[str, Any],
    start_event: threading.Event,
    result_list: list[dict[str, Any]],
    result_lock: threading.Lock,
    stream_start_lock: Optional[threading.Lock] = None,
) -> None:
    t0 = time.perf_counter()
    result = _new_result(lib_name, entry)
    stream = None
    try:
        input_mapping_cols = [int(ch) - 1 for ch in entry["input_mapping"]]
        output_mapping_cols = [int(ch) - 1 for ch in entry["output_mapping"]]
        x: Optional[np.ndarray] = None
        total_frames = 0
        out_cursor = [0]
        captured = [0]
        chunks: list[np.ndarray] = []
        first_cb_ts = [None]
        last_cb_ts = [None]
        done_event = threading.Event()

        t_wait0 = time.perf_counter()
        if not start_event.wait(timeout=10.0):
            raise TimeoutError("等待统一启动信号超时")
        t_wait1 = time.perf_counter()
        result["t_wait_event_sec"] = round(t_wait1 - t_wait0, 6)

        t_prep0 = time.perf_counter()
        x, total_frames = _prepare_output_audio(entry)
        t_prep1 = time.perf_counter()
        result["t_prep_audio_sec"] = round(t_prep1 - t_prep0, 6)

        def callback(indata, outdata, frames, time_info, status) -> None:  # noqa: ARG001
            now = time.perf_counter()
            if first_cb_ts[0] is None:
                first_cb_ts[0] = now
            if PRINT_CALLBACK_INTERVALS:
                if last_cb_ts[0] is not None:
                    print(f"[{lib_name}][thread-{entry['thread']}] cb_dt={now - last_cb_ts[0]:.6f}s")
                last_cb_ts[0] = now

            remain_out = int(total_frames) - int(out_cursor[0])
            take_out = int(min(int(frames), max(remain_out, 0)))
            outdata.fill(0.0)
            if take_out > 0 and x is not None:
                blk = x[out_cursor[0] : out_cursor[0] + take_out]
                for src_col, dst_col in enumerate(output_mapping_cols):
                    if 0 <= int(dst_col) < int(outdata.shape[1]) and src_col < int(blk.shape[1]):
                        outdata[:take_out, int(dst_col)] = blk[:, int(src_col)]
                out_cursor[0] += int(take_out)

            remain_in = int(total_frames) - int(captured[0])
            take_in = int(min(int(frames), max(remain_in, 0)))
            if take_in > 0:
                chunks.append(_slice_input(indata, input_mapping_cols, take_in))
                captured[0] += int(take_in)

            if out_cursor[0] >= total_frames and captured[0] >= total_frames:
                done_event.set()
                raise ad_module.CallbackStop()

        t_ctor0 = time.perf_counter()
        stream = ad_module.Stream(
            samplerate=int(SAMPLERATE),
            blocksize=int(BLOCKSIZE),
            channels=(int(entry["input_channels"]), int(entry["output_channels"])),
            callback=callback,
        )
        t_ctor1 = time.perf_counter()
        result["t_stream_ctor_sec"] = round(t_ctor1 - t_ctor0, 6)

        t_start0 = time.perf_counter()
        if stream_start_lock is None:
            stream.start()
        else:
            with stream_start_lock:
                stream.start()
        t_start1 = time.perf_counter()
        result["t_stream_start_sec"] = round(t_start1 - t_start0, 6)
        result["t_session_wait_sec"] = _wait_for_audiodevice_session(ad_module)

        wait_timeout = float(TONE_DURATION_SEC) + float(WAIT_SESSION_START_TIMEOUT_SEC) + 2.0
        done_event.wait(timeout=wait_timeout)
        time.sleep(max(float(PLAY_TAIL_SEC), float(CAPTURE_TAIL_SEC)))
        try:
            stream.close()
        finally:
            t_done = time.perf_counter()
            if first_cb_ts[0] is not None:
                result["t_first_callback_delay_sec"] = round(float(first_cb_ts[0]) - float(t_start1), 6)
            _print_thread_timing(result, t0=t0, t_end=t_done)

        if not chunks:
            raise RuntimeError("NoData: 没有录到回采数据")

        data = np.concatenate(chunks, axis=0)
        if data.shape[0] > total_frames:
            data = data[:total_frames]
        result["ok"] = True
        result["frames"] = int(total_frames)
        result["shape"] = tuple(x.shape) if x is not None else None
        result["record_shape"] = tuple(data.shape)
        result["active_seconds"] = _active_seconds(data, int(SAMPLERATE))
    except Exception as exc:  # noqa: BLE001
        result["error_type"] = type(exc).__name__
        result["error_msg"] = str(exc)
        result["traceback"] = traceback.format_exc()
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass
    finally:
        result["elapsed_sec"] = round(time.perf_counter() - t0, 4)
        with result_lock:
            result_list.append(result)


def _print_header(title: str, entries: list[dict[str, Any]]) -> None:
    print(f"\n=== {title} ===")
    print(
        f"device={DEVICE}, samplerate={SAMPLERATE}, "
        f"chirp_duration={TONE_DURATION_SEC}s, chirp_amplitude={TONE_AMPLITUDE}, "
        f"chirp_repeat_count={TONE_REPEAT_COUNT}, "
        f"thread_input_channels={THREAD_INPUT_CHANNELS}, "
        f"thread_input_mapping={THREAD_INPUT_MAPPING}, "
        f"thread_output_channels={THREAD_OUTPUT_CHANNELS}, "
        f"thread_output_mapping={THREAD_OUTPUT_MAPPING}, "
        f"blocksize={BLOCKSIZE}, thread_count={THREAD_COUNT}"
    )
    for item in entries:
        print(
            f"  thread-{item['thread']} -> "
            f"chirp {item['f0_hz']} -> {item['f1_hz']} Hz "
            f"(in_ch={item['input_channels']}, in_map={item['input_mapping']}, "
            f"out_ch={item['output_channels']}, out_map={item['output_mapping']})"
        )


def _print_report(results: list[dict[str, Any]]) -> None:
    print("\n=== 线程结果 ===")
    for r in sorted(results, key=lambda x: int(x["thread"])):
        common_parts = [
            f"elapsed={r['elapsed_sec']}s",
            f"f0={r['f0_hz']}",
            f"f1={r['f1_hz']}",
            f"in_ch={r['input_channels']}",
            f"in_map={r['input_mapping']}",
            f"out_ch={r['output_channels']}",
            f"out_map={r['output_mapping']}",
        ]
        if r.get("shape") is not None:
            common_parts.append(f"shape={r['shape']}")
        if r.get("record_shape") is not None:
            common_parts.append(f"record_shape={r['record_shape']}")
        if r.get("frames") is not None:
            common_parts.append(f"frames={r['frames']}")
        if r.get("active_seconds") is not None:
            common_parts.append(f"active={r['active_seconds']}s")
        if r.get("queue_put_retry_count") is not None:
            common_parts.append(f"q_put_retry={r['queue_put_retry_count']}")
        if r.get("queue_empty_count") is not None:
            common_parts.append(f"q_empty={r['queue_empty_count']}")
        if r.get("device_channel_overlap") is not None:
            common_parts.append(f"overlap={r['device_channel_overlap']}")

        if r["ok"]:
            print(f"[{r['library']}][thread-{r['thread']}] OK | " + " | ".join(common_parts))
        else:
            print(
                f"[{r['library']}][thread-{r['thread']}] FAIL | "
                + " | ".join(common_parts)
                + f" | {r['error_type']}: {r['error_msg']}"
            )
            if r.get("traceback"):
                print(r["traceback"])

    success = sum(1 for r in results if r["ok"])
    fail = len(results) - success
    print("\n=== 汇总 ===")
    print(f"success={success}, fail={fail}")


def _pick_sounddevice_output_device(sd, requested_output_device: int) -> tuple[int, str]:
    """为子线程回调流挑选更稳定的输出设备。"""
    dev_list = sd.query_devices()
    hostapi_list = sd.query_hostapis()
    req_idx = int(requested_output_device)
    req_dev = dev_list[req_idx]
    req_hostapi_name = str(hostapi_list[int(req_dev["hostapi"])]["name"])
    req_name = str(req_dev["name"])

    if not SOUNDDEVICE_AVOID_WASAPI_IN_THREADS:
        return req_idx, f"使用配置输出设备 index={req_idx} ({req_name}, {req_hostapi_name})"
    if "WASAPI" not in req_hostapi_name.upper():
        return req_idx, f"使用非 WASAPI 输出设备 index={req_idx} ({req_name}, {req_hostapi_name})"

    req_name_norm = req_name.lower()
    candidates: list[tuple[int, int, str, str]] = []
    for idx, dev in enumerate(dev_list):
        out_ch = int(dev["max_output_channels"])
        if out_ch <= 0:
            continue
        name = str(dev["name"])
        hostapi_name = str(hostapi_list[int(dev["hostapi"])]["name"])
        host_upper = hostapi_name.upper()
        if "WASAPI" in host_upper:
            continue
        name_norm = name.lower()
        similar = (req_name_norm in name_norm) or (name_norm in req_name_norm)
        if not similar:
            continue
        if "WDM-KS" in host_upper:
            priority = 0
        elif "DIRECTSOUND" in host_upper:
            priority = 1
        elif "MME" in host_upper:
            priority = 2
        else:
            priority = 3
        candidates.append((priority, int(idx), name, hostapi_name))

    if not candidates:
        return (
            req_idx,
            f"WASAPI 设备 index={req_idx} ({req_name}) 未找到同名回退设备，继续使用原配置",
        )

    candidates.sort(key=lambda x: (x[0], x[1]))
    _, chosen_idx, chosen_name, chosen_hostapi = candidates[0]
    return (
        chosen_idx,
        f"检测到 WASAPI 输出设备 index={req_idx} ({req_name})，"
        f"已切换为 index={chosen_idx} ({chosen_name}, {chosen_hostapi})",
    )


def run_audiodevice_play_threads() -> list[dict[str, Any]]:
    import audiodevice as ad

    entries = _validate_and_build_entries()
    _print_header("audiodevice 同设备多线程播放测试", entries)

    _init_audiodevice_engine(ad)
    ad.default.device = tuple(DEVICE)
    ad.default.samplerate = int(SAMPLERATE)
    ad.default.rb_frames = int(AUDIODEVICE_RB_FRAMES)
    max_out_ch = max(int(ch) for ch in THREAD_OUTPUT_CHANNELS)
    _warmup_audiodevice_stream(ad, out_channels=int(max_out_ch))

    results: list[dict[str, Any]] = []
    lock = threading.Lock()
    start_event = threading.Event()
    threads: list[threading.Thread] = []
    start_lock = threading.Lock() if SERIALIZE_STREAM_START else None

    for entry in entries:
        t = threading.Thread(
            target=_thread_output_worker,
            kwargs={
                "lib_name": "audiodevice_play",
                "stream_ctor": ad.OutputStream,
                "stream_kwargs": {
                    "samplerate": int(SAMPLERATE),
                    "blocksize": int(BLOCKSIZE),
                    "channels": int(entry["output_channels"]),
                },
                "entry": entry,
                "start_event": start_event,
                "result_list": results,
                "result_lock": lock,
                "stream_start_lock": start_lock,
                "audiodevice_style_callback": True,
                "audiodevice_module": ad,
            },
            daemon=False,
        )
        threads.append(t)
        t.start()

    print("[main][audiodevice_play] 所有线程已 start()，0.2s 后统一放行 start_event")
    time.sleep(0.2)
    start_event.set()

    for t in threads:
        t.join(timeout=RESULT_TIMEOUT)

    _print_report(results)
    return sorted(results, key=lambda x: int(x["thread"]))


def run_audiodevice_record_threads() -> list[dict[str, Any]]:
    import audiodevice as ad

    entries = _validate_and_build_entries()
    _print_header("audiodevice 同设备多线程录制测试", entries)

    _init_audiodevice_engine(ad)
    ad.default.device = tuple(DEVICE)
    ad.default.samplerate = int(SAMPLERATE)
    ad.default.rb_frames = int(AUDIODEVICE_RB_FRAMES)
    max_in_ch = max(int(ch) for ch in THREAD_INPUT_CHANNELS)
    _warmup_audiodevice_stream(ad, in_channels=int(max_in_ch))

    results: list[dict[str, Any]] = []
    lock = threading.Lock()
    start_event = threading.Event()
    threads: list[threading.Thread] = []
    start_lock = threading.Lock() if SERIALIZE_STREAM_START else None

    for entry in entries:
        t = threading.Thread(
            target=_thread_input_worker,
            kwargs={
                "lib_name": "audiodevice_record",
                "ad_module": ad,
                "entry": entry,
                "start_event": start_event,
                "result_list": results,
                "result_lock": lock,
                "stream_start_lock": start_lock,
            },
            daemon=False,
        )
        threads.append(t)
        t.start()

    print("[main][audiodevice_record] 所有线程已 start()，0.2s 后统一放行 start_event")
    time.sleep(0.2)
    start_event.set()

    for t in threads:
        t.join(timeout=RESULT_TIMEOUT)

    _print_report(results)
    return sorted(results, key=lambda x: int(x["thread"]))


def run_audiodevice_playrec_threads() -> list[dict[str, Any]]:
    import audiodevice as ad

    entries = _validate_and_build_entries()
    _print_header("audiodevice 同设备多线程播放录制测试", entries)

    _init_audiodevice_engine(ad)
    ad.default.device = tuple(DEVICE)
    ad.default.samplerate = int(SAMPLERATE)
    ad.default.rb_frames = int(AUDIODEVICE_RB_FRAMES)
    max_in_ch = max(int(ch) for ch in THREAD_INPUT_CHANNELS)
    max_out_ch = max(int(ch) for ch in THREAD_OUTPUT_CHANNELS)
    _warmup_audiodevice_stream(ad, in_channels=int(max_in_ch), out_channels=int(max_out_ch))

    results: list[dict[str, Any]] = []
    lock = threading.Lock()
    start_event = threading.Event()
    threads: list[threading.Thread] = []
    start_lock = threading.Lock() if SERIALIZE_STREAM_START else None

    for entry in entries:
        t = threading.Thread(
            target=_thread_duplex_worker,
            kwargs={
                "lib_name": "audiodevice_playrec",
                "ad_module": ad,
                "entry": entry,
                "start_event": start_event,
                "result_list": results,
                "result_lock": lock,
                "stream_start_lock": start_lock,
            },
            daemon=False,
        )
        threads.append(t)
        t.start()

    print("[main][audiodevice_playrec] 所有线程已 start()，0.2s 后统一放行 start_event")
    time.sleep(0.2)
    start_event.set()

    for t in threads:
        t.join(timeout=RESULT_TIMEOUT)

    _print_report(results)
    return sorted(results, key=lambda x: int(x["thread"]))


def run_audiodevice_single_stream() -> list[dict[str, Any]]:
    """同设备单 stream + 多 producer：避免每线程各自 start() 排队开设备。"""
    import audiodevice as ad

    entries = _validate_and_build_entries()
    _print_header("audiodevice 单 stream + 多 producer 路由/混音测试", entries)

    _init_audiodevice_engine(ad)
    ad.default.device = tuple(DEVICE)
    ad.default.samplerate = int(SAMPLERATE)
    ad.default.rb_frames = int(AUDIODEVICE_RB_FRAMES)

    max_out_ch_cfg = max(int(ch) for ch in THREAD_OUTPUT_CHANNELS)
    max_map_ch = max(int(max(item["output_mapping"])) for item in entries) if entries else 2
    out_ch_device = int(max(max_out_ch_cfg, max_map_ch, 2))
    _warmup_audiodevice_stream(ad, out_channels=int(out_ch_device))
    prefill_blocks = _estimate_prefill_blocks(
        samplerate=int(SAMPLERATE),
        blocksize=int(BLOCKSIZE),
        rb_frames=int(AUDIODEVICE_RB_FRAMES),
    )

    q_by_tid: dict[int, Queue[np.ndarray]] = {
        int(item["thread"]): Queue(maxsize=int(SINGLE_STREAM_QUEUE_MAX_BLOCKS)) for item in entries
    }
    done_by_tid: dict[int, threading.Event] = {int(item["thread"]): threading.Event() for item in entries}
    queue_empty_count_by_tid: dict[int, int] = {int(item["thread"]): 0 for item in entries}
    channel_overlap_by_tid: dict[int, int] = {}
    for item in entries:
        overlap = 0
        for ch in item["output_mapping"]:
            overlap += sum(1 for other in entries if int(ch) in [int(v) for v in other["output_mapping"]])
        channel_overlap_by_tid[int(item["thread"])] = int(max(overlap, 1))

    results: list[dict[str, Any]] = []
    lock = threading.Lock()
    start_event = threading.Event()
    stop_event = threading.Event()
    first_cb_event = threading.Event()
    cb_first_ts = [None]
    cb_count = [0]
    status_count = [0]
    last_cb_ts = [None]
    jitter_ms: list[float] = []
    shared_metrics: dict[str, Any] = {
        "t_stream_start_sec": None,
        "t_session_wait_sec": None,
        "t_first_callback_delay_sec": None,
    }

    def _producer(*, entry: dict[str, Any]) -> None:
        t0 = time.perf_counter()
        r = _new_result("audiodevice_single_stream", entry)
        r["output_channels"] = int(out_ch_device)
        r["queue_put_retry_count"] = 0
        r["queue_empty_count"] = None
        r["device_channel_overlap"] = channel_overlap_by_tid.get(int(entry["thread"]))
        try:
            t_wait0 = time.perf_counter()
            if not start_event.wait(timeout=10.0):
                raise TimeoutError("等待统一启动信号超时")
            t_wait1 = time.perf_counter()
            r["t_wait_event_sec"] = round(t_wait1 - t_wait0, 6)

            t_prep0 = time.perf_counter()
            audio, total_frames = _prepare_output_audio(entry)
            mapping_cols = [int(ch) - 1 for ch in entry["output_mapping"]]
            x = _adapt_channels(audio, max(1, int(len(mapping_cols))))
            r["frames"] = int(total_frames)
            r["shape"] = tuple(x.shape)
            t_prep1 = time.perf_counter()
            r["t_prep_audio_sec"] = round(t_prep1 - t_prep0, 6)

            bs = int(BLOCKSIZE)
            pos = 0
            q = q_by_tid[int(entry["thread"])]
            while pos < total_frames and (not stop_event.is_set()):
                take = int(min(bs, total_frames - pos))
                blk = x[pos : pos + take].astype(np.float32, copy=False)
                if take < bs:
                    pad = np.zeros((bs - take, int(blk.shape[1])), dtype=np.float32)
                    blk = np.concatenate([blk, pad], axis=0)
                while True:
                    try:
                        q.put(blk, timeout=0.2)
                        break
                    except Exception:
                        r["queue_put_retry_count"] = int(r["queue_put_retry_count"]) + 1
                        if stop_event.is_set():
                            break
                pos += int(take)
        except Exception as exc:  # noqa: BLE001
            r["error_type"] = type(exc).__name__
            r["error_msg"] = str(exc)
            r["traceback"] = traceback.format_exc()
        finally:
            done_by_tid[int(entry["thread"])].set()
            r["ok"] = r["error_type"] is None
            r["elapsed_sec"] = round(time.perf_counter() - t0, 4)
            with lock:
                results.append(r)

    threads: list[threading.Thread] = []
    mapping_cols_by_tid: dict[int, list[int]] = {}
    for entry in entries:
        tid = int(entry["thread"])
        mapping_cols_by_tid[tid] = [int(ch) - 1 for ch in entry["output_mapping"]]
        t = threading.Thread(target=_producer, kwargs={"entry": entry}, daemon=False)
        threads.append(t)
        t.start()

    def callback(indata, outdata, frames, time_info, status) -> None:  # noqa: ARG001
        now = time.perf_counter()
        cb_count[0] += 1
        if cb_first_ts[0] is None:
            cb_first_ts[0] = now
            first_cb_event.set()
        if status:
            status_count[0] += 1
        last = last_cb_ts[0]
        last_cb_ts[0] = now
        outdata.fill(0.0)

        f = int(frames)
        channel_contrib = [0 for _ in range(int(outdata.shape[1]))]
        for tid, q in q_by_tid.items():
            try:
                blk = q.get_nowait()
            except Exception:
                queue_empty_count_by_tid[int(tid)] = int(queue_empty_count_by_tid[int(tid)]) + 1
                continue
            blk2 = blk[:f]
            mcols = mapping_cols_by_tid.get(int(tid), [])
            for src_col, dst_col in enumerate(mcols):
                if 0 <= int(dst_col) < int(outdata.shape[1]) and src_col < int(blk2.shape[1]):
                    outdata[:f, int(dst_col)] += blk2[:, int(src_col)]
                    channel_contrib[int(dst_col)] += 1

        if SINGLE_STREAM_AUTO_ATTENUATE:
            for dst_col, overlap in enumerate(channel_contrib):
                if int(overlap) > 1:
                    outdata[:, int(dst_col)] *= np.float32(1.0 / float(overlap))

        if last is not None and cb_count[0] > (int(prefill_blocks) + 1):
            expected = float(frames) / float(SAMPLERATE) if int(SAMPLERATE) > 0 else 0.0
            jitter_ms.append((float(now - last) - expected) * 1000.0)

    stream = ad.OutputStream(
        samplerate=int(SAMPLERATE),
        blocksize=int(BLOCKSIZE),
        channels=int(out_ch_device),
        callback=callback,
    )

    print(f"[main][audiodevice_single_stream] producers={len(threads)}, out_ch_device={out_ch_device}")
    print(
        "[main][audiodevice_single_stream] "
        f"queue_max_blocks={SINGLE_STREAM_QUEUE_MAX_BLOCKS}, "
        f"queue_prefill_blocks={SINGLE_STREAM_QUEUE_PREFILL_BLOCKS}, "
        f"prefill_blocks≈{prefill_blocks}"
    )
    print("[main][audiodevice_single_stream] 所有 producer 已 start()，0.2s 后统一放行 start_event")
    time.sleep(0.2)
    start_event.set()

    t_start0 = time.perf_counter()
    for _ in range(200):
        if all(
            q_by_tid[int(item["thread"])].qsize() >= int(SINGLE_STREAM_QUEUE_PREFILL_BLOCKS)
            or done_by_tid[int(item["thread"])].is_set()
            for item in entries
        ):
            break
        time.sleep(0.01)
    try:
        stream.start()
        t_start1 = time.perf_counter()
        shared_metrics["t_stream_start_sec"] = round(t_start1 - t_start0, 6)
        shared_metrics["t_session_wait_sec"] = _wait_for_audiodevice_session(ad)
        first_cb_event.wait(timeout=float(WAIT_SESSION_START_TIMEOUT_SEC))
        t_wait_first = time.perf_counter()
        print(f"[main][audiodevice_single_stream] stream.start={round(t_start1 - t_start0, 6)}s")
        if cb_first_ts[0] is not None:
            shared_metrics["t_first_callback_delay_sec"] = round(float(cb_first_ts[0]) - float(t_start1), 6)
            print(
                "[main][audiodevice_single_stream] "
                f"first_callback_delay={round(float(cb_first_ts[0]) - float(t_start1), 6)}s"
            )
        else:
            print(
                "[main][audiodevice_single_stream] "
                f"first_callback_timeout={round(t_wait_first - t_start1, 6)}s"
            )

        duration_sec = float(TONE_DURATION_SEC)
        time.sleep(max(0.0, duration_sec))
        time.sleep(max(float(PLAY_TAIL_SEC), 2.0 * float(BLOCKSIZE) / float(SAMPLERATE)))
    finally:
        stop_event.set()
        stream.close()

    for t in threads:
        t.join(timeout=RESULT_TIMEOUT)

    _apply_shared_stream_metrics(results, shared_metrics)
    for r in results:
        tid = int(r["thread"])
        r["queue_empty_count"] = int(queue_empty_count_by_tid.get(tid, 0))

    print("\n=== 单 stream 驱动统计 ===")
    print(
        "[audiodevice_single_stream][driver] "
        f"callbacks={cb_count[0]} | status_nonzero={status_count[0]} | "
        f"jitter_ms={_summarize_ms(jitter_ms)}"
    )

    _print_report(results)
    return sorted(results, key=lambda x: int(x["thread"]))


def run_audiodevice_single_output_stream() -> list[dict[str, Any]]:
    return run_audiodevice_single_stream()


def run_audiodevice_single_input_stream() -> list[dict[str, Any]]:
    """同设备单 InputStream + 多 consumer：设备层只开 1 个输入流，采集后按 in_map 分发。"""
    import audiodevice as ad

    entries = _validate_and_build_entries()
    _print_header("audiodevice 单 InputStream + 多 consumer 路由测试", entries)

    _init_audiodevice_engine(ad)
    ad.default.device = tuple(DEVICE)
    ad.default.samplerate = int(SAMPLERATE)
    ad.default.rb_frames = int(AUDIODEVICE_RB_FRAMES)

    combined_input_mapping = sorted({int(ch) for item in entries for ch in item["input_mapping"]})
    input_cols_by_tid = {
        int(item["thread"]): [int(ch) - 1 for ch in item["input_mapping"]]
        for item in entries
    }
    in_ch_device = int(max(int(ch) for ch in THREAD_INPUT_CHANNELS))
    _warmup_audiodevice_stream(ad, in_channels=int(in_ch_device))

    target_frames = int(round(float(TONE_DURATION_SEC) * float(SAMPLERATE)))
    q_by_tid: dict[int, Queue[np.ndarray | None]] = {int(item["thread"]): Queue() for item in entries}
    results: list[dict[str, Any]] = []
    lock = threading.Lock()
    start_event = threading.Event()
    metrics_ready_event = threading.Event()
    first_cb_event = threading.Event()
    stream_done_event = threading.Event()
    cb_first_ts = [None]
    cb_count = [0]
    status_count = [0]
    captured_frames = [0]
    last_cb_ts = [None]
    jitter_ms: list[float] = []
    shared_metrics: dict[str, Any] = {
        "t_stream_start_sec": None,
        "t_session_wait_sec": None,
        "t_first_callback_delay_sec": None,
    }

    def _consumer(*, entry: dict[str, Any]) -> None:
        t0 = time.perf_counter()
        r = _new_result("audiodevice_single_input_stream", entry)
        r["input_channels"] = int(in_ch_device)
        r["t_prep_audio_sec"] = 0.0
        chunks: list[np.ndarray] = []
        got_frames = 0
        try:
            t_wait0 = time.perf_counter()
            if not start_event.wait(timeout=10.0):
                raise TimeoutError("等待统一启动信号超时")
            t_wait1 = time.perf_counter()
            r["t_wait_event_sec"] = round(t_wait1 - t_wait0, 6)

            q = q_by_tid[int(entry["thread"])]
            while got_frames < target_frames:
                blk = q.get(timeout=max(0.5, float(WAIT_SESSION_START_TIMEOUT_SEC)))
                if blk is None:
                    break
                chunks.append(np.asarray(blk, dtype=np.float32))
                got_frames += int(blk.shape[0])
                if stream_done_event.is_set() and got_frames >= target_frames:
                    break

            if not chunks:
                raise RuntimeError("NoData: 没有录到数据")

            data = np.concatenate(chunks, axis=0)
            if data.shape[0] > target_frames:
                data = data[:target_frames]
            r["ok"] = True
            r["frames"] = int(data.shape[0])
            r["shape"] = tuple(data.shape)
            r["active_seconds"] = _active_seconds(data, int(SAMPLERATE))
        except Exception as exc:  # noqa: BLE001
            r["error_type"] = type(exc).__name__
            r["error_msg"] = str(exc)
            r["traceback"] = traceback.format_exc()
        finally:
            metrics_ready_event.wait(timeout=1.0)
            _apply_shared_stream_metrics([r], shared_metrics)
            r["elapsed_sec"] = round(time.perf_counter() - t0, 4)
            with lock:
                results.append(r)

    threads: list[threading.Thread] = []
    for entry in entries:
        t = threading.Thread(target=_consumer, kwargs={"entry": entry}, daemon=False)
        threads.append(t)
        t.start()

    def callback(indata, outdata, frames, time_info, status) -> None:  # noqa: ARG001
        now = time.perf_counter()
        cb_count[0] += 1
        if cb_first_ts[0] is None:
            cb_first_ts[0] = now
            first_cb_event.set()
        if status:
            status_count[0] += 1
        last = last_cb_ts[0]
        last_cb_ts[0] = now
        remain = int(target_frames) - int(captured_frames[0])
        if remain <= 0:
            stream_done_event.set()
            raise ad.CallbackStop()
        take = int(min(int(frames), remain))
        if take > 0:
            blk = np.asarray(indata[:take], dtype=np.float32)
            for tid, cols in input_cols_by_tid.items():
                q_by_tid[int(tid)].put(blk[:, cols].copy())
            captured_frames[0] += int(take)
        if last is not None:
            expected = float(frames) / float(SAMPLERATE) if int(SAMPLERATE) > 0 else 0.0
            jitter_ms.append((float(now - last) - expected) * 1000.0)
        if captured_frames[0] >= target_frames:
            stream_done_event.set()
            raise ad.CallbackStop()

    stream = ad.InputStream(
        samplerate=int(SAMPLERATE),
        blocksize=int(BLOCKSIZE),
        channels=int(in_ch_device),
        callback=callback,
        delay_time=float(INPUT_DELAY_MS),
    )

    print(
        f"[main][audiodevice_single_input_stream] consumers={len(threads)}, "
        f"in_ch_device={in_ch_device}, combined_input_mapping={combined_input_mapping}"
    )
    print("[main][audiodevice_single_input_stream] 所有 consumer 已 start()，0.2s 后统一放行 start_event")
    time.sleep(0.2)
    start_event.set()

    try:
        t_start0 = time.perf_counter()
        stream.start()
        t_start1 = time.perf_counter()
        shared_metrics["t_stream_start_sec"] = round(t_start1 - t_start0, 6)
        shared_metrics["t_session_wait_sec"] = _wait_for_audiodevice_session(ad)
        first_cb_event.wait(timeout=float(WAIT_SESSION_START_TIMEOUT_SEC))
        print(f"[main][audiodevice_single_input_stream] stream.start={shared_metrics['t_stream_start_sec']}s")
        if cb_first_ts[0] is not None:
            shared_metrics["t_first_callback_delay_sec"] = round(float(cb_first_ts[0]) - float(t_start1), 6)
            print(
                "[main][audiodevice_single_input_stream] "
                f"first_callback_delay={shared_metrics['t_first_callback_delay_sec']}s"
            )
        stream_done_event.wait(
            timeout=float(TONE_DURATION_SEC) + float(INPUT_DELAY_MS) / 1000.0 + float(WAIT_SESSION_START_TIMEOUT_SEC) + 2.0
        )
        time.sleep(float(CAPTURE_TAIL_SEC))
    finally:
        try:
            stream.close()
        finally:
            stream_done_event.set()
            for q in q_by_tid.values():
                q.put(None)
            metrics_ready_event.set()

    for t in threads:
        t.join(timeout=RESULT_TIMEOUT)

    _apply_shared_stream_metrics(results, shared_metrics)
    print("\n=== 单 InputStream 驱动统计 ===")
    print(
        "[audiodevice_single_input_stream][driver] "
        f"callbacks={cb_count[0]} | status_nonzero={status_count[0]} | "
        f"captured_frames={captured_frames[0]} | jitter_ms={_summarize_ms(jitter_ms)}"
    )
    _print_report(results)
    return sorted(results, key=lambda x: int(x["thread"]))


def run_audiodevice_single_duplex_stream() -> list[dict[str, Any]]:
    """同设备单 duplex Stream + 多逻辑线程：统一 output/input，再按 mapping 路由。"""
    import audiodevice as ad

    entries = _validate_and_build_entries()
    _print_header("audiodevice 单 duplex Stream + 多逻辑线程测试", entries)

    _init_audiodevice_engine(ad)
    ad.default.device = tuple(DEVICE)
    ad.default.samplerate = int(SAMPLERATE)
    ad.default.rb_frames = int(AUDIODEVICE_RB_FRAMES)

    combined_input_mapping = sorted({int(ch) for item in entries for ch in item["input_mapping"]})
    combined_output_mapping = sorted({int(ch) for item in entries for ch in item["output_mapping"]})
    input_cols_by_tid = {
        int(item["thread"]): [int(ch) - 1 for ch in item["input_mapping"]]
        for item in entries
    }
    output_cols_by_tid = {
        int(item["thread"]): [int(ch) - 1 for ch in item["output_mapping"]]
        for item in entries
    }
    in_ch_device = int(max(int(ch) for ch in THREAD_INPUT_CHANNELS))
    out_ch_device = int(max(int(ch) for ch in THREAD_OUTPUT_CHANNELS))
    _warmup_audiodevice_stream(
        ad,
        in_channels=int(in_ch_device),
        out_channels=int(out_ch_device),
    )
    prefill_blocks = _estimate_prefill_blocks(
        samplerate=int(SAMPLERATE),
        blocksize=int(BLOCKSIZE),
        rb_frames=int(AUDIODEVICE_RB_FRAMES),
    )
    target_frames = int(round(float(TONE_DURATION_SEC) * float(SAMPLERATE)))

    q_out_by_tid: dict[int, Queue[np.ndarray | None]] = {int(item["thread"]): Queue() for item in entries}
    q_in_by_tid: dict[int, Queue[np.ndarray | None]] = {int(item["thread"]): Queue() for item in entries}
    producer_ready_by_tid: dict[int, threading.Event] = {int(item["thread"]): threading.Event() for item in entries}
    queue_empty_count_by_tid: dict[int, int] = {int(item["thread"]): 0 for item in entries}

    results: list[dict[str, Any]] = []
    lock = threading.Lock()
    start_event = threading.Event()
    stop_event = threading.Event()
    stream_done_event = threading.Event()
    metrics_ready_event = threading.Event()
    first_cb_event = threading.Event()
    cb_first_ts = [None]
    cb_count = [0]
    status_count = [0]
    out_sent_frames = [0]
    last_cb_ts = [None]
    jitter_ms: list[float] = []
    shared_metrics: dict[str, Any] = {
        "t_stream_start_sec": None,
        "t_session_wait_sec": None,
        "t_first_callback_delay_sec": None,
    }

    channel_overlap_by_tid: dict[int, int] = {}
    for item in entries:
        overlap = 0
        for ch in item["output_mapping"]:
            overlap += sum(1 for other in entries if int(ch) in [int(v) for v in other["output_mapping"]])
        channel_overlap_by_tid[int(item["thread"])] = int(max(overlap, 1))

    def _worker(*, entry: dict[str, Any]) -> None:
        t0 = time.perf_counter()
        r = _new_result("audiodevice_single_duplex_stream", entry)
        r["input_channels"] = int(in_ch_device)
        r["output_channels"] = int(out_ch_device)
        r["queue_put_retry_count"] = 0
        r["queue_empty_count"] = None
        r["device_channel_overlap"] = channel_overlap_by_tid.get(int(entry["thread"]))
        try:
            t_wait0 = time.perf_counter()
            if not start_event.wait(timeout=10.0):
                raise TimeoutError("等待统一启动信号超时")
            t_wait1 = time.perf_counter()
            r["t_wait_event_sec"] = round(t_wait1 - t_wait0, 6)

            t_prep0 = time.perf_counter()
            audio, total_frames = _prepare_output_audio(entry)
            mapping_cols = output_cols_by_tid.get(int(entry["thread"]), [])
            x = _adapt_channels(audio, max(1, int(len(mapping_cols))))
            r["frames"] = int(total_frames)
            r["shape"] = tuple(x.shape)
            t_prep1 = time.perf_counter()
            r["t_prep_audio_sec"] = round(t_prep1 - t_prep0, 6)

            bs = int(BLOCKSIZE)
            pos = 0
            q_out = q_out_by_tid[int(entry["thread"])]
            while pos < total_frames and (not stop_event.is_set()):
                take = int(min(bs, total_frames - pos))
                blk = x[pos : pos + take].astype(np.float32, copy=False)
                if take < bs:
                    pad = np.zeros((bs - take, int(blk.shape[1])), dtype=np.float32)
                    blk = np.concatenate([blk, pad], axis=0)
                q_out.put(blk)
                pos += int(take)
            producer_ready_by_tid[int(entry["thread"])].set()

            chunks: list[np.ndarray] = []
            got_frames = 0
            q_in = q_in_by_tid[int(entry["thread"])]
            while got_frames < total_frames:
                blk_in = q_in.get(timeout=max(0.5, float(WAIT_SESSION_START_TIMEOUT_SEC)))
                if blk_in is None:
                    break
                chunks.append(np.asarray(blk_in, dtype=np.float32))
                got_frames += int(blk_in.shape[0])
                if stream_done_event.is_set() and got_frames >= total_frames:
                    break

            if not chunks:
                raise RuntimeError("NoData: 没有录到回采数据")
            data = np.concatenate(chunks, axis=0)
            if data.shape[0] > total_frames:
                data = data[:total_frames]
            r["ok"] = True
            r["record_shape"] = tuple(data.shape)
            r["active_seconds"] = _active_seconds(data, int(SAMPLERATE))
        except Exception as exc:  # noqa: BLE001
            r["error_type"] = type(exc).__name__
            r["error_msg"] = str(exc)
            r["traceback"] = traceback.format_exc()
        finally:
            producer_ready_by_tid[int(entry["thread"])].set()
            metrics_ready_event.wait(timeout=1.0)
            _apply_shared_stream_metrics([r], shared_metrics)
            r["queue_empty_count"] = int(queue_empty_count_by_tid.get(int(entry["thread"]), 0))
            r["elapsed_sec"] = round(time.perf_counter() - t0, 4)
            with lock:
                results.append(r)

    threads: list[threading.Thread] = []
    for entry in entries:
        t = threading.Thread(target=_worker, kwargs={"entry": entry}, daemon=False)
        threads.append(t)
        t.start()

    def callback(indata, outdata, frames, time_info, status) -> None:  # noqa: ARG001
        now = time.perf_counter()
        cb_count[0] += 1
        if cb_first_ts[0] is None:
            cb_first_ts[0] = now
            first_cb_event.set()
        if status:
            status_count[0] += 1
        last = last_cb_ts[0]
        last_cb_ts[0] = now

        f = int(frames)
        remain = int(target_frames) - int(out_sent_frames[0])
        take_valid = int(min(f, max(remain, 0)))
        outdata.fill(0.0)
        channel_contrib = [0 for _ in range(int(outdata.shape[1]))]
        for tid, q in q_out_by_tid.items():
            try:
                blk = q.get_nowait()
            except Exception:
                queue_empty_count_by_tid[int(tid)] = int(queue_empty_count_by_tid[int(tid)]) + 1
                continue
            blk2 = blk[:f]
            mcols = output_cols_by_tid.get(int(tid), [])
            for src_col, dst_col in enumerate(mcols):
                if 0 <= int(dst_col) < int(outdata.shape[1]) and src_col < int(blk2.shape[1]):
                    outdata[:f, int(dst_col)] += blk2[:, int(src_col)]
                    channel_contrib[int(dst_col)] += 1

        if SINGLE_STREAM_AUTO_ATTENUATE:
            for dst_col, overlap in enumerate(channel_contrib):
                if int(overlap) > 1:
                    outdata[:, int(dst_col)] *= np.float32(1.0 / float(overlap))

        if take_valid > 0:
            blk_in = np.asarray(indata[:take_valid], dtype=np.float32)
            for tid, cols in input_cols_by_tid.items():
                q_in_by_tid[int(tid)].put(blk_in[:, cols].copy())
            out_sent_frames[0] += int(take_valid)

        if last is not None and cb_count[0] > (int(prefill_blocks) + 1):
            expected = float(frames) / float(SAMPLERATE) if int(SAMPLERATE) > 0 else 0.0
            jitter_ms.append((float(now - last) - expected) * 1000.0)

        if out_sent_frames[0] >= target_frames:
            stream_done_event.set()
            raise ad.CallbackStop()

    stream = ad.Stream(
        samplerate=int(SAMPLERATE),
        blocksize=int(BLOCKSIZE),
        channels=(int(in_ch_device), int(out_ch_device)),
        callback=callback,
    )

    print(
        f"[main][audiodevice_single_duplex_stream] workers={len(threads)}, "
        f"in_ch_device={in_ch_device}, out_ch_device={out_ch_device}, "
        f"combined_input_mapping={combined_input_mapping}, "
        f"combined_output_mapping={combined_output_mapping}"
    )
    print(
        "[main][audiodevice_single_duplex_stream] "
        f"queue_prefill_blocks={SINGLE_STREAM_QUEUE_PREFILL_BLOCKS}, prefill_blocks≈{prefill_blocks}"
    )
    print("[main][audiodevice_single_duplex_stream] 所有 worker 已 start()，0.2s 后统一放行 start_event")
    time.sleep(0.2)
    start_event.set()

    for _ in range(200):
        if all(
            q_out_by_tid[int(item["thread"])].qsize() >= int(SINGLE_STREAM_QUEUE_PREFILL_BLOCKS)
            or producer_ready_by_tid[int(item["thread"])].is_set()
            for item in entries
        ):
            break
        time.sleep(0.01)

    try:
        t_start0 = time.perf_counter()
        stream.start()
        t_start1 = time.perf_counter()
        shared_metrics["t_stream_start_sec"] = round(t_start1 - t_start0, 6)
        shared_metrics["t_session_wait_sec"] = _wait_for_audiodevice_session(ad)
        first_cb_event.wait(timeout=float(WAIT_SESSION_START_TIMEOUT_SEC))
        print(f"[main][audiodevice_single_duplex_stream] stream.start={shared_metrics['t_stream_start_sec']}s")
        if cb_first_ts[0] is not None:
            shared_metrics["t_first_callback_delay_sec"] = round(float(cb_first_ts[0]) - float(t_start1), 6)
            print(
                "[main][audiodevice_single_duplex_stream] "
                f"first_callback_delay={shared_metrics['t_first_callback_delay_sec']}s"
            )
        stream_done_event.wait(timeout=float(TONE_DURATION_SEC) + float(WAIT_SESSION_START_TIMEOUT_SEC) + 2.0)
        time.sleep(max(float(PLAY_TAIL_SEC), float(CAPTURE_TAIL_SEC)))
    finally:
        stop_event.set()
        try:
            stream.close()
        finally:
            stream_done_event.set()
            for q in q_in_by_tid.values():
                q.put(None)
            metrics_ready_event.set()

    for t in threads:
        t.join(timeout=RESULT_TIMEOUT)

    _apply_shared_stream_metrics(results, shared_metrics)
    print("\n=== 单 duplex Stream 驱动统计 ===")
    print(
        "[audiodevice_single_duplex_stream][driver] "
        f"callbacks={cb_count[0]} | status_nonzero={status_count[0]} | "
        f"out_sent_frames={out_sent_frames[0]} | jitter_ms={_summarize_ms(jitter_ms)}"
    )
    _print_report(results)
    return sorted(results, key=lambda x: int(x["thread"]))


def run_sounddevice_play_threads() -> list[dict[str, Any]]:
    try:
        import sounddevice as sd
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("未安装 sounddevice，请先安装后再运行该入口。") from exc

    entries = _validate_and_build_entries()
    _print_header("sounddevice 同设备多线程播放测试", entries)
    selected_output_device, select_msg = _pick_sounddevice_output_device(sd, DEVICE[1])
    print(f"[sounddevice] {select_msg}")

    results: list[dict[str, Any]] = []
    lock = threading.Lock()
    start_event = threading.Event()
    threads: list[threading.Thread] = []

    for entry in entries:
        t = threading.Thread(
            target=_thread_output_worker,
            kwargs={
                "lib_name": "sounddevice_play",
                "stream_ctor": sd.OutputStream,
                "stream_kwargs": {
                    "device": int(selected_output_device),
                    "samplerate": int(SAMPLERATE),
                    "blocksize": int(BLOCKSIZE),
                    "channels": int(entry["output_channels"]),
                    "dtype": "float32",
                },
                "entry": entry,
                "start_event": start_event,
                "result_list": results,
                "result_lock": lock,
                "audiodevice_style_callback": False,
                "audiodevice_module": None,
            },
            daemon=False,
        )
        threads.append(t)
        t.start()

    print("[main][sounddevice_play] 所有线程已 start()，立即统一放行 start_event")
    start_event.set()

    for t in threads:
        t.join(timeout=RESULT_TIMEOUT)

    _print_report(results)
    return sorted(results, key=lambda x: int(x["thread"]))


def _copy_thread_audio_map(src: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(item) for item in src]


def _copy_nested_int_lists(src: list[list[int]]) -> list[list[int]]:
    return [[int(v) for v in row] for row in src]


def _build_rotating_thread_audio_map() -> list[dict[str, Any]]:
    return [
        {
            "thread": int(tid),
            "f0": float(ROTATING_CHIRP_F0_HZ),
            "f1": float(ROTATING_CHIRP_F1_HZ),
        }
        for tid in range(int(THREAD_COUNT))
    ]


def _build_rotating_input_mappings(primary_tid: int) -> list[list[int]]:
    return [
        list(ROTATING_PRIMARY_INPUT_MAPPING) if int(tid) == int(primary_tid) else list(ROTATING_SECONDARY_INPUT_MAPPING)
        for tid in range(int(THREAD_COUNT))
    ]


def _get_compare_round_count() -> int:
    if len(sys.argv) >= 3:
        try:
            return max(1, int(sys.argv[2]))
        except Exception:
            pass
    return int(ROTATING_ROUND_COUNT)


def _invoke_mode_runner(mode: str) -> list[dict[str, Any]]:
    mode_v = str(mode).strip().lower()
    if mode_v in ("audiodevice", "audiodevice_play", "play"):
        return run_audiodevice_play_threads()
    if mode_v in ("audiodevice_record", "record", "rec"):
        return run_audiodevice_record_threads()
    if mode_v in ("audiodevice_playrec", "playrec", "duplex"):
        return run_audiodevice_playrec_threads()
    if mode_v in (
        "audiodevice_single_stream",
        "audiodevice_single_output_stream",
        "audiodevice_single",
        "single_stream",
        "single_output_stream",
        "single_output",
    ):
        return run_audiodevice_single_output_stream()
    if mode_v in ("audiodevice_single_input_stream", "single_input_stream", "single_input"):
        return run_audiodevice_single_input_stream()
    if mode_v in ("audiodevice_single_duplex_stream", "single_duplex_stream", "single_duplex"):
        return run_audiodevice_single_duplex_stream()
    if mode_v in ("sounddevice", "sounddevice_play"):
        return run_sounddevice_play_threads()
    if mode_v in ("audiodevice_compare_rotating", "compare_rotating", "compare"):
        return run_audiodevice_compare_rotating()
    if mode_v in ("audiodevice_compare_rotating_all", "compare_rotating_all", "compare_all"):
        return run_audiodevice_compare_rotating_all()
    raise ValueError(
        "mode 必须是 "
        "'audiodevice_play'、'audiodevice_record'、'audiodevice_playrec'、"
        "'audiodevice_single_stream'、'audiodevice_single_input_stream'、"
        "'audiodevice_single_duplex_stream'、'sounddevice_play'、"
        "'audiodevice_compare_rotating' 或 'audiodevice_compare_rotating_all'，"
        f"当前: {mode!r}"
    )


def _run_mode_with_overrides(
    mode: str,
    *,
    thread_audio_map: Optional[list[dict[str, Any]]] = None,
    thread_input_mapping: Optional[list[list[int]]] = None,
    tone_duration_sec: Optional[float] = None,
    tone_repeat_count: Optional[int] = None,
) -> list[dict[str, Any]]:
    global THREAD_AUDIO_MAP, THREAD_INPUT_MAPPING, TONE_DURATION_SEC, TONE_REPEAT_COUNT

    old_thread_audio_map = _copy_thread_audio_map(THREAD_AUDIO_MAP)
    old_thread_input_mapping = _copy_nested_int_lists(THREAD_INPUT_MAPPING)
    old_tone_duration_sec = float(TONE_DURATION_SEC)
    old_tone_repeat_count = int(TONE_REPEAT_COUNT)

    try:
        if thread_audio_map is not None:
            THREAD_AUDIO_MAP = _copy_thread_audio_map(thread_audio_map)
        if thread_input_mapping is not None:
            THREAD_INPUT_MAPPING = _copy_nested_int_lists(thread_input_mapping)
        if tone_duration_sec is not None:
            TONE_DURATION_SEC = float(tone_duration_sec)
        if tone_repeat_count is not None:
            TONE_REPEAT_COUNT = int(tone_repeat_count)
        return _invoke_mode_runner(mode)
    finally:
        THREAD_AUDIO_MAP = old_thread_audio_map
        THREAD_INPUT_MAPPING = old_thread_input_mapping
        TONE_DURATION_SEC = old_tone_duration_sec
        TONE_REPEAT_COUNT = old_tone_repeat_count


def _mean_or_none(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return float(sum(values) / float(len(values)))


def _min_or_none(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return float(min(values))


def _max_or_none(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return float(max(values))


def _spread_or_none(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return float(max(values) - min(values))


def _fmt_opt(value: Optional[float], *, digits: int = 3) -> str:
    if value is None:
        return "-"
    return f"{float(value):.{digits}f}"


def _summarize_mode_results(
    *,
    mode_name: str,
    mode_label: str,
    results: list[dict[str, Any]],
    mode_elapsed_sec: float,
) -> dict[str, Any]:
    ok_results = [r for r in results if bool(r.get("ok"))]
    start_values = [float(r["t_stream_start_sec"]) for r in ok_results if r.get("t_stream_start_sec") is not None]
    first_cb_values = [float(r["t_first_callback_delay_sec"]) for r in ok_results if r.get("t_first_callback_delay_sec") is not None]
    session_values = [float(r["t_session_wait_sec"]) for r in ok_results if r.get("t_session_wait_sec") is not None]
    elapsed_values = [float(r["elapsed_sec"]) for r in ok_results if r.get("elapsed_sec") is not None]

    success = len(ok_results)
    fail = len(results) - success
    round_ids = {int(r.get("round_idx", 0)) for r in results if r.get("round_idx") is not None}

    start_spread = _spread_or_none(start_values)
    first_cb_spread = _spread_or_none(first_cb_values)
    issue_detected = (
        fail > 0
        or (start_spread is not None and start_spread >= 1.0)
        or (first_cb_spread is not None and first_cb_spread >= 1.0)
    )

    return {
        "mode_name": mode_name,
        "mode_label": mode_label,
        "rounds": len(round_ids),
        "success": success,
        "fail": fail,
        "start_mean_sec": _mean_or_none(start_values),
        "start_min_sec": _min_or_none(start_values),
        "start_max_sec": _max_or_none(start_values),
        "start_spread_sec": start_spread,
        "first_cb_mean_sec": _mean_or_none(first_cb_values),
        "first_cb_min_sec": _min_or_none(first_cb_values),
        "first_cb_max_sec": _max_or_none(first_cb_values),
        "first_cb_spread_sec": first_cb_spread,
        "session_mean_sec": _mean_or_none(session_values),
        "elapsed_mean_sec": _mean_or_none(elapsed_values),
        "mode_elapsed_sec": float(mode_elapsed_sec),
        "issue_detected": bool(issue_detected),
    }


def _print_compare_table(summaries: list[dict[str, Any]]) -> None:
    print("\n=== 模式关键统计对比表 ===")
    header = (
        f"{'mode':<18} {'rounds':>6} {'ok/fail':>10} "
        f"{'start_mean':>11} {'start_min~max':>21} {'start_spread':>13} "
        f"{'firstcb_mean':>13} {'firstcb_min~max':>23} {'mode_elapsed':>13} {'issue':>8}"
    )
    print(header)
    print("-" * len(header))
    for s in summaries:
        start_range = f"{_fmt_opt(s['start_min_sec'])}~{_fmt_opt(s['start_max_sec'])}"
        first_cb_range = f"{_fmt_opt(s['first_cb_min_sec'])}~{_fmt_opt(s['first_cb_max_sec'])}"
        issue_text = "YES" if bool(s["issue_detected"]) else "NO"
        ok_fail_text = f"{int(s['success'])}/{int(s['fail'])}"
        print(
            f"{str(s['mode_label']):<18} "
            f"{int(s['rounds']):>6} "
            f"{ok_fail_text:>10} "
            f"{_fmt_opt(s['start_mean_sec']):>11} "
            f"{start_range:>21} "
            f"{_fmt_opt(s['start_spread_sec']):>13} "
            f"{_fmt_opt(s['first_cb_mean_sec']):>13} "
            f"{first_cb_range:>23} "
            f"{_fmt_opt(s['mode_elapsed_sec']):>13} "
            f"{issue_text:>8}"
        )


def _print_compare_conclusion(summaries: list[dict[str, Any]]) -> None:
    issue_modes = [s for s in summaries if bool(s["issue_detected"])]
    print("\n=== 结论 ===")
    if not issue_modes:
        print("未观察到明显的线程初始化排队问题。")
        return

    worst = max(
        issue_modes,
        key=lambda s: (
            float(s["start_spread_sec"] or 0.0),
            float(s["start_max_sec"] or 0.0),
        ),
    )
    print("存在这个问题。")
    print(
        f"最明显的是 {worst['mode_label']}："
        f"start_spread={_fmt_opt(worst['start_spread_sec'])}s, "
        f"start_max={_fmt_opt(worst['start_max_sec'])}s。"
    )
    for s in summaries:
        verdict = "存在明显排队/延迟" if bool(s["issue_detected"]) else "未见明显问题"
        print(
            f"{s['mode_label']}: {verdict} | "
            f"start_mean={_fmt_opt(s['start_mean_sec'])}s | "
            f"start_spread={_fmt_opt(s['start_spread_sec'])}s | "
            f"firstcb_mean={_fmt_opt(s['first_cb_mean_sec'])}s"
        )


def run_audiodevice_compare_rotating() -> list[dict[str, Any]]:
    """10 轮轮转场景：每轮 10 线程同步启动，轮流让一个线程用 in_map=[1]，其余用 in_map=[2]。"""
    mode_specs = [
        ("audiodevice_play", "play"),
        ("audiodevice_record", "record"),
        ("audiodevice_playrec", "playrec"),
    ]
    scenario_audio_map = _build_rotating_thread_audio_map()
    round_count = _get_compare_round_count()

    print("\n=== 轮转同步 chirp 对比测试 ===")
    print(
        f"thread_count={THREAD_COUNT}, rounds={round_count}, "
        f"chirp={ROTATING_CHIRP_F0_HZ}->{ROTATING_CHIRP_F1_HZ} Hz, "
        f"duration_per_round={ROTATING_ROUND_DURATION_SEC}s, "
        f"primary_in_map={ROTATING_PRIMARY_INPUT_MAPPING}, "
        f"secondary_in_map={ROTATING_SECONDARY_INPUT_MAPPING}"
    )

    summaries: list[dict[str, Any]] = []
    for mode_name, mode_label in mode_specs:
        mode_results: list[dict[str, Any]] = []
        t_mode0 = time.perf_counter()
        for round_idx in range(int(round_count)):
            primary_tid = int(round_idx % int(THREAD_COUNT))
            thread_input_mapping = _build_rotating_input_mappings(primary_tid)
            print(
                f"\n--- mode={mode_label} round={round_idx + 1}/{round_count} | "
                f"thread-{primary_tid} -> in_map={ROTATING_PRIMARY_INPUT_MAPPING}, "
                f"others -> {ROTATING_SECONDARY_INPUT_MAPPING} ---"
            )
            round_results = _run_mode_with_overrides(
                mode_name,
                thread_audio_map=scenario_audio_map,
                thread_input_mapping=thread_input_mapping,
                tone_duration_sec=float(ROTATING_ROUND_DURATION_SEC),
                tone_repeat_count=1,
            )
            for item in round_results:
                item["round_idx"] = int(round_idx + 1)
                item["primary_input_thread"] = int(primary_tid)
            mode_results.extend(round_results)

        summaries.append(
            _summarize_mode_results(
                mode_name=mode_name,
                mode_label=mode_label,
                results=mode_results,
                mode_elapsed_sec=time.perf_counter() - t_mode0,
            )
        )

    _print_compare_table(summaries)
    _print_compare_conclusion(summaries)
    return summaries


def run_audiodevice_compare_rotating_all() -> list[dict[str, Any]]:
    """同一测试口径下，同时比较“多线程各开流”与“单 session 共享流”两套方案。"""
    mode_specs = [
        ("audiodevice_play", "play"),
        ("audiodevice_record", "record"),
        ("audiodevice_playrec", "playrec"),
        ("audiodevice_single_output_stream", "single_out"),
        ("audiodevice_single_input_stream", "single_in"),
        ("audiodevice_single_duplex_stream", "single_duplex"),
    ]
    scenario_audio_map = _build_rotating_thread_audio_map()
    round_count = _get_compare_round_count()

    print("\n=== 轮转同步 chirp 全量对比测试 ===")
    print(
        f"thread_count={THREAD_COUNT}, rounds={round_count}, "
        f"chirp={ROTATING_CHIRP_F0_HZ}->{ROTATING_CHIRP_F1_HZ} Hz, "
        f"duration_per_round={ROTATING_ROUND_DURATION_SEC}s, "
        f"primary_in_map={ROTATING_PRIMARY_INPUT_MAPPING}, "
        f"secondary_in_map={ROTATING_SECONDARY_INPUT_MAPPING}"
    )

    summaries: list[dict[str, Any]] = []
    for mode_name, mode_label in mode_specs:
        mode_results: list[dict[str, Any]] = []
        t_mode0 = time.perf_counter()
        for round_idx in range(int(round_count)):
            primary_tid = int(round_idx % int(THREAD_COUNT))
            thread_input_mapping = _build_rotating_input_mappings(primary_tid)
            print(
                f"\n--- mode={mode_label} round={round_idx + 1}/{round_count} | "
                f"thread-{primary_tid} -> in_map={ROTATING_PRIMARY_INPUT_MAPPING}, "
                f"others -> {ROTATING_SECONDARY_INPUT_MAPPING} ---"
            )
            round_results = _run_mode_with_overrides(
                mode_name,
                thread_audio_map=scenario_audio_map,
                thread_input_mapping=thread_input_mapping,
                tone_duration_sec=float(ROTATING_ROUND_DURATION_SEC),
                tone_repeat_count=1,
            )
            for item in round_results:
                item["round_idx"] = int(round_idx + 1)
                item["primary_input_thread"] = int(primary_tid)
            mode_results.extend(round_results)

        summaries.append(
            _summarize_mode_results(
                mode_name=mode_name,
                mode_label=mode_label,
                results=mode_results,
                mode_elapsed_sec=time.perf_counter() - t_mode0,
            )
        )

    _print_compare_table(summaries)
    _print_compare_conclusion(summaries)
    return summaries


def run(mode: str = RUN_MODE) -> list[dict[str, Any]]:
    return _invoke_mode_runner(mode)


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else RUN_MODE)
