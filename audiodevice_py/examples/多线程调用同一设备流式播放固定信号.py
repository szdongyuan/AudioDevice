from __future__ import annotations

from queue import PriorityQueue
import threading
import time
import traceback
from pathlib import Path
from typing import Any

import numpy as np

# ============================================
# 可修改配置区
# ============================================
THREAD_COUNT = 1

# 线程 -> 频率映射（Hz）
# 要求：
# 1) thread 必须覆盖 0..THREAD_COUNT-1
# 2) 每个 thread 只能出现一次
THREAD_AUDIO_MAP: list[dict[str, Any]] = [
    {"thread": 0, "Hz": 440.0},
    # {"thread": 1, "Hz": 523.25},
    # {"thread": 2, "Hz": 659.25},
    # {"thread": 3, "Hz": 783.99},
    # {"thread": 4, "Hz": 880.0},
    # {"thread": 5, "Hz": 987.77},
    # {"thread": 6, "Hz": 1046.50},
    # {"thread": 7, "Hz": 1174.66},
    # {"thread": 8, "Hz": 1318.51},
    # {"thread": 9, "Hz": 1567.98},
]

DEVICE: tuple[int, int] = (14, 18)  # (input_device, output_device)
DEFAULT_CHANNELS_NUM = (6, 2)  # (in_ch, out_ch) for engine default session

SAMPLERATE = 48000
TONE_DURATION_SEC = 4.0
TONE_AMPLITUDE = 0.2
# 在 TONE_DURATION_SEC 总时长内重复扫频的次数
TONE_REPEAT_COUNT = 4
# 每个线程的输出通道数（按 thread 索引对应一个元素）
# 例如 thread-0 用 2 通道，thread-1 用 1 通道
# THREAD_OUTPUT_CHANNELS = [2, 2, 2, 2, 2, 2, 2, 2, 2, 2]
THREAD_OUTPUT_CHANNELS = [2]
# 每个线程的输出 mapping（1-based），按 thread 索引对应一个元素
# 示例：thread-0 映射到左声道 [1]，thread-1 映射到右声道 [2]
THREAD_OUTPUT_MAPPING: list[list[int]] = [
    [1],
    # [2],
    # [1],
    # [2],
    # [1],
    # [2],
    # [1],
    # [2],
    # [1],
    # [2],
]
BLOCKSIZE = 1024
RESULT_TIMEOUT = 60.0
# 等待 audiodevice session 真正启动的最长时间（用于把“启动延迟”从听感时长里扣掉）
WAIT_SESSION_START_TIMEOUT_SEC = 5.0
# 播放完后额外等待一点尾巴，避免 close() 截断设备/引擎缓冲
PLAY_TAIL_SEC = 0.2
# sounddevice 回调流在 Windows 子线程 + WASAPI 组合下容易启动失败，
# 这里默认自动尝试切换到同名非 WASAPI 输出设备（优先 WDM-KS）。
SOUNDDEVICE_AVOID_WASAPI_IN_THREADS = True

# audiodevice 引擎初始化（可按需改）
AUDIODEVICE_ENGINE_DIR = Path(__file__).resolve().parent / "AudioDevice-master" / "audiodevice_py"
AUDIODEVICE_ENGINE_EXE = AUDIODEVICE_ENGINE_DIR / "audiodevice.exe"

# 运行入口模式: "audiodevice" / "sounddevice"
RUN_MODE = "audiodevice"
# RUN_MODE = "sounddevice"


def _init_audiodevice_engine(ad_module) -> None:
    if AUDIODEVICE_ENGINE_EXE.is_file():
        ad_module.init(
            engine_exe=str(AUDIODEVICE_ENGINE_EXE),
            engine_cwd=str(AUDIODEVICE_ENGINE_DIR),
            timeout=10,
        )
    else:
        ad_module.init(timeout=10)


def _validate_and_build_entries() -> list[dict[str, Any]]:
    if THREAD_COUNT <= 0:
        raise ValueError("THREAD_COUNT 必须 > 0")
    if len(THREAD_OUTPUT_CHANNELS) != THREAD_COUNT:
        raise ValueError(
            "THREAD_OUTPUT_CHANNELS 长度必须等于 THREAD_COUNT，"
            f"当前为 {len(THREAD_OUTPUT_CHANNELS)} vs {THREAD_COUNT}"
        )
    for idx, ch in enumerate(THREAD_OUTPUT_CHANNELS):
        if int(ch) <= 0:
            raise ValueError(f"THREAD_OUTPUT_CHANNELS[{idx}] 必须 > 0")
    if len(THREAD_OUTPUT_MAPPING) != THREAD_COUNT:
        raise ValueError(
            "THREAD_OUTPUT_MAPPING 长度必须等于 THREAD_COUNT，"
            f"当前为 {len(THREAD_OUTPUT_MAPPING)} vs {THREAD_COUNT}"
        )
    for idx, mapping in enumerate(THREAD_OUTPUT_MAPPING):
        if not isinstance(mapping, list) or len(mapping) == 0:
            raise ValueError(f"THREAD_OUTPUT_MAPPING[{idx}] 必须是非空列表")
        out_ch = int(THREAD_OUTPUT_CHANNELS[idx])
        for ch in mapping:
            ch_i = int(ch)
            if ch_i < 1:
                raise ValueError(f"THREAD_OUTPUT_MAPPING[{idx}] 的通道号必须 >= 1")
            if ch_i > out_ch:
                raise ValueError(
                    f"THREAD_OUTPUT_MAPPING[{idx}] 中通道号 {ch_i} 超过该线程 channels={out_ch}"
                )
    if len(THREAD_AUDIO_MAP) != THREAD_COUNT:
        raise ValueError(
            f"THREAD_AUDIO_MAP 数量({len(THREAD_AUDIO_MAP)})必须等于 THREAD_COUNT({THREAD_COUNT})"
        )

    seen: set[int] = set()
    entries: list[dict[str, Any]] = []
    for item in THREAD_AUDIO_MAP:
        tid = int(item["thread"])
        if "Hz" not in item:
            raise ValueError(f"THREAD_AUDIO_MAP[{tid}] 缺少 'Hz' 字段")
        hz = float(item["Hz"])
        if hz <= 0:
            raise ValueError(f"THREAD_AUDIO_MAP[{tid}] 的 Hz 必须 > 0，当前: {hz}")
        if tid < 0 or tid >= THREAD_COUNT:
            raise ValueError(f"thread 索引越界: {tid}, 需在 [0, {THREAD_COUNT - 1}]")
        if tid in seen:
            raise ValueError(f"thread 重复定义: {tid}")
        seen.add(tid)
        entries.append(
            {
                "thread": tid,
                "hz": hz,
                "output_channels": int(THREAD_OUTPUT_CHANNELS[tid]),
                "output_mapping": [int(v) for v in THREAD_OUTPUT_MAPPING[tid]],
            }
        )

    missing = [i for i in range(THREAD_COUNT) if i not in seen]
    if missing:
        raise ValueError(f"THREAD_AUDIO_MAP 缺少线程: {missing}")

    entries.sort(key=lambda x: int(x["thread"]))
    return entries


def _generate_tone_float32(
    freq_hz: float,
    duration_sec: float,
    samplerate: int,
    repeat_count: int = 1,
) -> np.ndarray:
    """生成单声道 float32 正弦扫频，形状 (frames, 1)。"""
    frame_count = int(round(float(duration_sec) * float(samplerate)))
    if frame_count <= 0:
        raise ValueError(f"生成音频帧数必须 > 0，当前 duration={duration_sec}, sr={samplerate}")

    start_hz = float(freq_hz)
    end_hz = 80.0
    sr = float(samplerate)
    repeats = max(int(repeat_count), 1)
    t = np.arange(frame_count, dtype=np.float64) / sr
    total_duration = float(duration_sec)
    cycle_duration = total_duration / float(repeats)
    if cycle_duration <= 0:
        raise ValueError(
            f"单次扫频时长必须 > 0，当前 duration={duration_sec}, repeat_count={repeat_count}"
        )
    t_cycle = np.mod(t, cycle_duration)
    k = (end_hz - start_hz) / cycle_duration  # 单次扫频线性斜率(Hz/s)
    phase = 2.0 * np.pi * (start_hz * t_cycle + 0.5 * k * t_cycle * t_cycle)
    sweep = np.sin(phase).astype(np.float32) * np.float32(TONE_AMPLITUDE)
    return np.clip(sweep[:, None], -1.0, 1.0)


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


def _thread_worker(
    *,
    lib_name: str,
    stream_ctor,
    callback_stop_exc,
    stream_kwargs: dict[str, Any],
    thread_id: int,
    freq_hz: float,
    output_channels: int,
    output_mapping: list[int],
    start_event: threading.Event,
    result_list: list[dict[str, Any]],
    result_lock: threading.Lock,
) -> None:
    t0 = time.perf_counter()
    result: dict[str, Any] = {
        "thread": int(thread_id),
        "hz": float(freq_hz),
        "ok": False,
        "error_type": None,
        "error_msg": None,
        "traceback": None,
        "elapsed_sec": None,
        "frames": None,
        "shape": None,
        "library": lib_name,
        "output_channels": int(output_channels),
        "output_mapping": list(output_mapping),
    }
    try:
        audio = _generate_tone_float32(freq_hz, TONE_DURATION_SEC, SAMPLERATE, TONE_REPEAT_COUNT)
        mapping_cols = [int(ch) - 1 for ch in output_mapping]
        x = _adapt_channels(audio, len(mapping_cols))
        total_frames = int(x.shape[0])
        cursor = [0]
        fed_done_event = threading.Event()
        t1 = None
        t2 = None
        t3 = None
        t4 = None

        if lib_name == "audiodevice":
            t001 = time.perf_counter()
            last_cb_ts = [time.perf_counter()]
            def callback(indata, outdata, frames, time_info, status) -> None:  # noqa: ARG001
                tttt = time.perf_counter()
                print(tttt - last_cb_ts[0])
                last_cb_ts[0] = tttt
                remain = total_frames - cursor[0]
                if remain <= 0:
                    outdata.fill(0.0)
                    fed_done_event.set()
                    return
                take = int(frames) if int(frames) < int(remain) else int(remain)
                outdata.fill(0.0)
                if take > 0:
                    blk = x[cursor[0] : cursor[0] + take]
                    for src_col, dst_col in enumerate(mapping_cols):
                        outdata[:take, dst_col] = blk[:, src_col]
                    cursor[0] += int(take)
                if take < int(frames):
                    outdata[take:].fill(0.0)
                    fed_done_event.set()
                    return
        else:
            def callback(outdata, frames, time_info, status) -> None:  # noqa: ARG001
                remain = total_frames - cursor[0]
                if remain <= 0:
                    outdata.fill(0.0)
                    fed_done_event.set()
                    return
                take = int(frames) if int(frames) < int(remain) else int(remain)
                outdata.fill(0.0)
                if take > 0:
                    blk = x[cursor[0] : cursor[0] + take]
                    for src_col, dst_col in enumerate(mapping_cols):
                        outdata[:take, dst_col] = blk[:, src_col]
                    cursor[0] += int(take)
                if take < int(frames):
                    outdata[take:].fill(0.0)
                    fed_done_event.set()
                    return

        if not start_event.wait(timeout=10.0):
            raise TimeoutError("等待统一启动信号超时")

        stream = stream_ctor(callback=callback, **stream_kwargs)
        t1 = time.perf_counter()

        stream.start()
        # print(22222222222222222222222222)
        t4 = time.perf_counter()
        try:
            duration_sec = total_frames / float(SAMPLERATE) if float(SAMPLERATE) > 0 else 0.0

            # audiodevice 的 OutputStream 启动时会预填充环形缓冲，回调可能会“爆发式”调用。
            # 如果按“喂完就 close()”，会截断还没播完的缓冲。所以这里按墙钟时间等播放窗口结束再 close。
            play_t0 = None
            if lib_name == "audiodevice":
                import audiodevice as ad  # local import: thread-safe and avoids passing module around

                t_wait = time.time()
                while True:
                    st = ad.get_status() or {}
                    if bool(st.get("has_session", False)):
                        play_t0 = time.time()
                        break
                    if (time.time() - t_wait) >= float(WAIT_SESSION_START_TIMEOUT_SEC):
                        play_t0 = time.time()
                        break
                    ad.sleep(50)
            else:
                play_t0 = time.time()

            t_end = float(play_t0) + float(duration_sec)
            while time.time() < t_end:
                time.sleep(0.01)
            time.sleep(max(float(PLAY_TAIL_SEC), 2.0 * float(BLOCKSIZE) / float(SAMPLERATE)))
            t3 = time.perf_counter()
        finally:
            stream.close()
            t2 = time.perf_counter()
            print(f"结束时长={t2-t3}, 播放时间={t2-t4}, 启动时长={t4-t1}, 总时长={t2-t1}")
            print(f"喂入时长(cursor/sr)={cursor[0] / float(SAMPLERATE)}")
        result["ok"] = True
        result["frames"] = total_frames
        result["shape"] = tuple(x.shape)
    except Exception as exc:  # noqa: BLE001
        result["error_type"] = type(exc).__name__
        result["error_msg"] = str(exc)
        result["traceback"] = traceback.format_exc()
    finally:
        result["elapsed_sec"] = round(time.perf_counter() - t0, 4)
        with result_lock:
            result_list.append(result)


def _print_header(title: str, entries: list[dict[str, Any]]) -> None:
    print(f"\n=== {title} ===")
    print(
        f"device={DEVICE}, samplerate={SAMPLERATE}, "
        f"tone_duration={TONE_DURATION_SEC}s, tone_amplitude={TONE_AMPLITUDE}, "
        f"tone_repeat_count={TONE_REPEAT_COUNT}, "
        f"thread_output_channels={THREAD_OUTPUT_CHANNELS}, "
        f"thread_output_mapping={THREAD_OUTPUT_MAPPING}, "
        f"blocksize={BLOCKSIZE}, thread_count={THREAD_COUNT}"
    )
    for item in entries:
        print(
            f"  thread-{item['thread']} -> {item['hz']} Hz "
            f"(channels={item['output_channels']}, mapping={item['output_mapping']})"
        )


def _print_report(results: list[dict[str, Any]]) -> None:
    print("\n=== 线程结果 ===")
    for r in sorted(results, key=lambda x: int(x["thread"])):
        if r["ok"]:
            print(
                f"[{r['library']}][thread-{r['thread']}] OK | elapsed={r['elapsed_sec']}s | "
                f"shape={r['shape']} | frames={r['frames']} | "
                f"channels={r['output_channels']} | mapping={r['output_mapping']} | Hz={r['hz']} | "
            )
        else:
            print(
                f"[{r['library']}][thread-{r['thread']}] FAIL | elapsed={r['elapsed_sec']}s | "
                f"{r['error_type']}: {r['error_msg']} | "
                f"channels={r['output_channels']} | mapping={r['output_mapping']} | Hz={r['hz']}"
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
        # 优先挑同名（或高相似度）设备，避免切到错误声卡
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


def run_audiodevice_threads() -> list[dict[str, Any]]:
    import audiodevice as ad

    entries = _validate_and_build_entries()
    _print_header("audiodevice 多线程流式播放测试", entries)

    _init_audiodevice_engine(ad)
    ad.default.device = tuple(DEVICE)
    ad.default.samplerate = int(SAMPLERATE)
    max_out_ch = max(int(ch) for ch in THREAD_OUTPUT_CHANNELS)
    ad.default.channels = (1, int(max_out_ch))
    ad.default.rb_seconds = 20

    results: list[dict[str, Any]] = []
    lock = threading.Lock()
    start_event = threading.Event()
    threads: list[threading.Thread] = []

    for item in entries:
        t = threading.Thread(
            target=_thread_worker,
            kwargs={
                "lib_name": "audiodevice",
                "stream_ctor": ad.OutputStream,
                "callback_stop_exc": ad.CallbackStop,
                "stream_kwargs": {
                    "samplerate": int(SAMPLERATE),
                    "blocksize": int(BLOCKSIZE),
                    "channels": int(item["output_channels"]),
                },
                "thread_id": int(item["thread"]),
                "freq_hz": float(item["hz"]),
                "output_channels": int(item["output_channels"]),
                "output_mapping": list(item["output_mapping"]),
                "start_event": start_event,
                "result_list": results,
                "result_lock": lock,
            },
            daemon=False,
        )
        threads.append(t)
        t.start()
    print(111111111)
    time.sleep(0.2)
    start_event.set()

    for t in threads:
        t.join(timeout=RESULT_TIMEOUT)

    _print_report(results)
    return sorted(results, key=lambda x: int(x["thread"]))


def run_sounddevice_threads() -> list[dict[str, Any]]:
    try:
        import sounddevice as sd
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("未安装 sounddevice，请先安装后再运行该入口。") from exc

    entries = _validate_and_build_entries()
    _print_header("sounddevice 多线程流式播放测试", entries)
    selected_output_device, select_msg = _pick_sounddevice_output_device(sd, DEVICE[1])
    print(f"[sounddevice] {select_msg}")

    results: list[dict[str, Any]] = []
    lock = threading.Lock()
    start_event = threading.Event()
    threads: list[threading.Thread] = []

    for item in entries:
        t = threading.Thread(
            target=_thread_worker,
            kwargs={
                "lib_name": "sounddevice",
                "stream_ctor": sd.OutputStream,
                "callback_stop_exc": sd.CallbackStop,
                "stream_kwargs": {
                    "device": int(selected_output_device),
                    "samplerate": int(SAMPLERATE),
                    "blocksize": int(BLOCKSIZE),
                    "channels": int(item["output_channels"]),
                    "dtype": "float32",
                },
                "thread_id": int(item["thread"]),
                "freq_hz": float(item["hz"]),
                "output_channels": int(item["output_channels"]),
                "output_mapping": list(item["output_mapping"]),
                "start_event": start_event,
                "result_list": results,
                "result_lock": lock,
            },
            daemon=False,
        )
        threads.append(t)
        t.start()
    print(111111111)
    start_event.set()

    for t in threads:
        t.join(timeout=RESULT_TIMEOUT)

    _print_report(results)
    return sorted(results, key=lambda x: int(x["thread"]))


def run(mode: str = RUN_MODE) -> list[dict[str, Any]]:
    mode_v = str(mode).strip().lower()
    if mode_v == "audiodevice":
        return run_audiodevice_threads()
    if mode_v == "sounddevice":
        return run_sounddevice_threads()
    raise ValueError(f"mode 必须是 'audiodevice' 或 'sounddevice'，当前: {mode!r}")


if __name__ == "__main__":
    run(RUN_MODE)
