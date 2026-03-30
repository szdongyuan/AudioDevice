"""
demo_stream_input_two_threaded.py
两个"逻辑任务"使用 **相同设备、相同采样率、相同时长** 进行流式录制 (InputStream)，
分别需要不同的通道映射，并将结果各自保存为 WAV 文件。

核心思路：打开 **一个** InputStream（合并所有通道映射），回调中统一收集数据，
录制结束后按通道拆分、分别保存。避免两个流竞争同一环形缓冲区导致数据被瓜分。
"""
from __future__ import annotations

import os
import threading
import time
import traceback
import wave
from pathlib import Path
from typing import Any

import numpy as np

import audiodevice as ad

# ---- engine init ----

def init_engine() -> None:
    root = Path(__file__).resolve().parent.parent
    engine = root / "audiodevice.exe"
    if engine.is_file():
        ad.init(engine_exe=str(engine), engine_cwd=str(root), timeout=10)
    else:
        ad.init(timeout=10)


# ---- constants ----
SAMPLERATE = 48000
DURATION_S = 10
DELAY_MS = 34
BLOCKSIZE = 1024
RB_SECONDS = 8
DEVICE = (24, 30)
DEFAULT_CHANNELS_NUM = (6, 2)
INPUT_CHANNELS_NUM = int(DEFAULT_CHANNELS_NUM[0])

THREAD_1_MAPPING = [1]
THREAD_2_MAPPING = [3]

SAVE_DIR = os.path.join(
    os.path.dirname(__file__),
    "recordings",
    "two_thread_stream_input_same_device",
)
# ---- end constants ----


def apply_defaults() -> tuple[int | None, int | None]:
    ad.default.samplerate = int(SAMPLERATE)
    ad.default.channels = DEFAULT_CHANNELS_NUM
    ad.default.rb_seconds = RB_SECONDS

    try:
        ad.default.device = DEVICE
    except ValueError as exc:
        print(f"WARNING: {exc}")
        print("WARNING: Falling back to system default devices. "
              "Please update DEVICE=(in,out) to match your machine.")

    try:
        din, dout = ad.default.device
        return (int(din) if din is not None else None,
                int(dout) if dout is not None else None)
    except Exception:
        return None, None


def build_wav_path(name: str, mapping: list[int], device_in: int | None) -> str:
    os.makedirs(SAVE_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    dev_tag = str(device_in) if device_in is not None else "default"
    ch_tag = "-".join(map(str, mapping))
    return os.path.join(SAVE_DIR, f"{ts}_{name}_in{dev_tag}_ch{ch_tag}.wav")


def save_wav(path: str, data_f32: np.ndarray, samplerate: int, channels: int) -> None:
    pcm = np.clip(data_f32, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype(np.int16)
    with wave.open(path, "wb") as wav:
        wav.setnchannels(int(channels))
        wav.setsampwidth(2)
        wav.setframerate(int(samplerate))
        wav.writeframes(pcm.tobytes())


def active_seconds(x: np.ndarray, samplerate: int) -> float:
    y = np.asarray(x, dtype=np.float32)
    if y.ndim == 1:
        y = y[:, None]
    mask = np.any(np.abs(y) > 1e-6, axis=1)
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return 0.0
    return round(float(idx[-1] - idx[0] + 1) / float(samplerate), 3)


def print_result(result: dict[str, Any]) -> None:
    if result["ok"]:
        print(
            f"[{result['name']}] OK | elapsed={result['elapsed_sec']}s | "
            f"mapping={result['mapping']} | shape={result['shape']} | "
            f"active={result['active_seconds']}s | wav={result['wav_path']}"
        )
        return
    print(
        f"[{result['name']}] FAIL | elapsed={result['elapsed_sec']}s | "
        f"mapping={result['mapping']} | "
        f"{result.get('error_type')}: {result.get('error_msg')}"
    )
    if result.get("traceback"):
        print(result["traceback"])


def _build_combined_mapping(
    *mappings: list[int],
) -> tuple[list[int], list[list[int]]]:
    """合并多组 mapping 为一个去重有序列表，并返回每组在合并后的列索引。

    Returns:
        (combined_mapping, column_indices_per_group)
        例如 mappings=([1], [3]) -> combined=[1,3], indices=[[0], [1]]
        例如 mappings=([1,3], [3,5]) -> combined=[1,3,5], indices=[[0,1], [1,2]]
    """
    seen: dict[int, int] = {}
    combined: list[int] = []
    for m in mappings:
        for ch in m:
            if ch not in seen:
                seen[ch] = len(combined)
                combined.append(ch)
    indices = [[seen[ch] for ch in m] for m in mappings]
    return combined, indices


def run_together() -> list[dict[str, Any]]:
    """用一个 InputStream 同时录制所有通道，录完后按 mapping 拆分保存。"""
    print("=== together (single-stream, split by channel) ===")
    t0_total = time.perf_counter()

    init_engine()
    din, _ = apply_defaults()

    all_mappings = [THREAD_1_MAPPING, THREAD_2_MAPPING]
    all_names = ["thread-1", "thread-2"]
    combined_mapping, col_indices = _build_combined_mapping(*all_mappings)
    target_frames = int(round(float(SAMPLERATE) * float(DURATION_S)))

    print(f"combined_mapping={combined_mapping}, target_frames={target_frames}")
    for name, mapping, cols in zip(all_names, all_mappings, col_indices):
        print(f"  {name}: mapping={mapping} -> columns {cols} in combined stream")

    chunks: list[np.ndarray] = []
    frames_captured = [0]
    done_event = threading.Event()

    def callback(indata, outdata, frames, time_info, status):
        remain = target_frames - frames_captured[0]
        if remain <= 0:
            done_event.set()
            raise ad.CallbackStop()
        take = min(int(frames), int(remain))
        if take > 0:
            chunks.append(indata[:take].copy())
            frames_captured[0] += take
        if frames_captured[0] >= target_frames:
            done_event.set()
            raise ad.CallbackStop()

    results: list[dict[str, Any]] = []

    try:
        stream = ad.InputStream(
            callback=callback,
            channels=INPUT_CHANNELS_NUM,
            samplerate=SAMPLERATE,
            blocksize=BLOCKSIZE,
            delay_time=int(DELAY_MS),
            mapping=combined_mapping,
        )
        stream.start()

        wait_timeout = float(DURATION_S) + float(DELAY_MS) / 1000.0 + 5.0
        finished = done_event.wait(timeout=wait_timeout)

        if not finished:
            print(f"WARNING: callback did not finish within {wait_timeout}s, "
                  f"frames_captured={frames_captured[0]}/{target_frames}")

        # Wait a moment for the worker thread to exit cleanly after CallbackStop
        time.sleep(0.2)
        if stream.active:
            stream.close()
        else:
            try:
                stream.close()
            except Exception:
                pass

        actual_frames = frames_captured[0]
        print(f"frames_captured={actual_frames}/{target_frames} "
              f"({actual_frames / SAMPLERATE:.3f}s / {DURATION_S}s)")

        if not chunks:
            for name, mapping in zip(all_names, all_mappings):
                results.append({
                    "name": name, "mapping": list(mapping),
                    "wav_path": "", "ok": False,
                    "error_type": "NoData", "error_msg": "没有录到数据",
                    "elapsed_sec": round(time.perf_counter() - t0_total, 4),
                })
        else:
            full_data = np.concatenate(chunks, axis=0)
            if full_data.shape[0] > target_frames:
                full_data = full_data[:target_frames]

            for name, mapping, cols in zip(all_names, all_mappings, col_indices):
                t0 = time.perf_counter()
                wav_path = build_wav_path(name, mapping, din)
                save_channels = len(mapping)
                data = full_data[:, cols]

                result: dict[str, Any] = {
                    "name": name,
                    "mapping": list(mapping),
                    "wav_path": wav_path,
                    "ok": False,
                }
                try:
                    save_wav(wav_path, data, SAMPLERATE, save_channels)
                    result["ok"] = True
                    result["shape"] = tuple(data.shape)
                    result["active_seconds"] = active_seconds(data, SAMPLERATE)
                except Exception as exc:
                    result["error_type"] = type(exc).__name__
                    result["error_msg"] = str(exc)
                    result["traceback"] = traceback.format_exc()
                finally:
                    result["elapsed_sec"] = round(time.perf_counter() - t0, 4)
                results.append(result)

    except Exception as exc:
        for name, mapping in zip(all_names, all_mappings):
            results.append({
                "name": name, "mapping": list(mapping),
                "wav_path": "", "ok": False,
                "error_type": type(exc).__name__,
                "error_msg": str(exc),
                "traceback": traceback.format_exc(),
                "elapsed_sec": round(time.perf_counter() - t0_total, 4),
            })

    print(f"total_elapsed={round(time.perf_counter() - t0_total, 4)}s")
    for r in results:
        print_result(r)
    return results


def run_separate() -> list[dict[str, Any]]:
    """顺序执行两次流式录制（各自独占流，不存在竞争问题）。"""
    print("=== separate (sequential stream input) ===")
    results: list[dict[str, Any]] = []

    for name, mapping in [("thread-1", THREAD_1_MAPPING), ("thread-2", THREAD_2_MAPPING)]:
        t0 = time.perf_counter()
        init_engine()
        din, _ = apply_defaults()
        target_frames = int(round(float(SAMPLERATE) * float(DURATION_S)))
        wav_path = build_wav_path(name, mapping, din)
        save_channels = len(mapping)

        result: dict[str, Any] = {
            "name": name, "mapping": list(mapping),
            "wav_path": wav_path, "ok": False,
        }

        chunks: list[np.ndarray] = []
        frames_captured = [0]
        done_event = threading.Event()

        def make_callback(ch_list, fc, tf, evt):
            def callback(indata, outdata, frames, time_info, status):
                remain = tf - fc[0]
                if remain <= 0:
                    evt.set()
                    raise ad.CallbackStop()
                take = min(int(frames), int(remain))
                if take > 0:
                    ch_list.append(indata[:take].copy())
                    fc[0] += take
                if fc[0] >= tf:
                    evt.set()
                    raise ad.CallbackStop()
            return callback

        try:
            stream = ad.InputStream(
                callback=make_callback(chunks, frames_captured, target_frames, done_event),
                channels=INPUT_CHANNELS_NUM,
                samplerate=SAMPLERATE,
                blocksize=BLOCKSIZE,
                delay_time=int(DELAY_MS),
                mapping=mapping,
            )
            stream.start()

            wait_timeout = float(DURATION_S) + float(DELAY_MS) / 1000.0 + 5.0
            finished = done_event.wait(timeout=wait_timeout)
            if not finished:
                print(f"WARNING: [{name}] callback did not finish within {wait_timeout}s, "
                      f"frames_captured={frames_captured[0]}/{target_frames}")

            time.sleep(0.2)
            try:
                stream.close()
            except Exception:
                pass

            actual_frames = frames_captured[0]
            print(f"[{name}] frames_captured={actual_frames}/{target_frames} "
                  f"({actual_frames / SAMPLERATE:.3f}s / {DURATION_S}s)")

            if chunks:
                data = np.concatenate(chunks, axis=0)
                if data.shape[0] > target_frames:
                    data = data[:target_frames]
                save_wav(wav_path, data, SAMPLERATE, save_channels)
                result["ok"] = True
                result["shape"] = tuple(data.shape)
                result["active_seconds"] = active_seconds(data, SAMPLERATE)
            else:
                result["error_type"] = "NoData"
                result["error_msg"] = "没有录到数据"
        except Exception as exc:
            result["error_type"] = type(exc).__name__
            result["error_msg"] = str(exc)
            result["traceback"] = traceback.format_exc()
        finally:
            result["elapsed_sec"] = round(time.perf_counter() - t0, 4)

        results.append(result)

    for r in results:
        print_result(r)
    return results


def run() -> None:
    init_engine()
    ad.print_default_devices()
    din, dout = apply_defaults()

    print("=== two-task same-device stream-input demo ===")
    print(f"samplerate={SAMPLERATE}, duration_s={DURATION_S}, "
          f"delay_ms={DELAY_MS}, blocksize={BLOCKSIZE}")
    print(f"device_const(in,out)={DEVICE}, device_used(in,out)=({din},{dout})")
    print(f"input_channels_num={INPUT_CHANNELS_NUM}")
    print(f"thread-1 mapping={THREAD_1_MAPPING}")
    print(f"thread-2 mapping={THREAD_2_MAPPING}")
    print("Use run_separate() for sequential calls.")
    print("Use run_together() for concurrent calls.")

    run_together()


if __name__ == "__main__":
    run()
