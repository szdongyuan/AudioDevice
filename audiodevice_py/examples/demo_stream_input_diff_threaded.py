"""
demo_stream_input_diff_threaded.py
多个"逻辑任务"使用 **不同设备、不同采样率、相同时长** 进行多线程流式录制 (InputStream)，
各自指定通道映射和采样率，并将录制结果分别保存为 WAV 文件。

核心思路：
  - InputStream 通过 ad.default.device / ad.default.samplerate 解析设备与采样率，
    不接受显式 device 参数。
  - 因此在主线程中 **顺序** 设置 default.device / default.samplerate → 创建并启动
    InputStream，等待引擎线程完成设备解析后再切换下一台设备的配置。
  - 启动后各流的回调各自在独立线程中并发采集数据，互不干扰。
  - 录制结束后各自按对应采样率保存 WAV。
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
DURATION_S = 10
DELAY_MS = 34
BLOCKSIZE = 1024
RB_SECONDS = 8

# 设备 0：设备索引 + 采样率 + (in_ch, out_ch) + 1-based mapping
DEVICE_0 = (26, 27)
SAMPLERATE_0 = 44100
DEFAULT_CHANNELS_NUM_0 = (1, 2)
MAPPING_0 = [1]
INPUT_CHANNELS_NUM_0 = len(MAPPING_0)  # sounddevice-like: callback channels == len(mapping)

# 设备 1
DEVICE_1 = (24, 30)
SAMPLERATE_1 = 48000
DEFAULT_CHANNELS_NUM_1 = (6, 2)
MAPPING_1 = [3]
INPUT_CHANNELS_NUM_1 = len(MAPPING_1)  # sounddevice-like: callback channels == len(mapping)

SAVE_DIR = os.path.join(
    os.path.dirname(__file__),
    "recordings",
    "diff_device_stream_input",
)
# ---- end constants ----


DEVICE_JOBS: list[dict[str, Any]] = [
    {
        "name": "dev0",
        "device": DEVICE_0,
        "samplerate": SAMPLERATE_0,
        "channels_num": DEFAULT_CHANNELS_NUM_0,
        "input_channels": INPUT_CHANNELS_NUM_0,
        "mapping": MAPPING_0,
    },
    {
        "name": "dev1",
        "device": DEVICE_1,
        "samplerate": SAMPLERATE_1,
        "channels_num": DEFAULT_CHANNELS_NUM_1,
        "input_channels": INPUT_CHANNELS_NUM_1,
        "mapping": MAPPING_1,
    },
]


def build_wav_path(tag: str, device_in: int, mapping: list[int]) -> str:
    os.makedirs(SAVE_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    ch_tag = "-".join(map(str, mapping))
    return os.path.join(SAVE_DIR, f"{ts}_{tag}_in{device_in}_ch{ch_tag}.wav")


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
            f"device={result['device']} | sr={result['samplerate']} | "
            f"mapping={result['mapping']} | shape={result['shape']} | "
            f"active={result['active_seconds']}s | wav={result['wav_path']}"
        )
        return
    print(
        f"[{result['name']}] FAIL | elapsed={result['elapsed_sec']}s | "
        f"device={result['device']} | sr={result.get('samplerate')} | "
        f"mapping={result['mapping']} | "
        f"{result.get('error_type')}: {result.get('error_msg')}"
    )
    if result.get("traceback"):
        print(result["traceback"])


def _make_callback(
    chunks: list[np.ndarray],
    frames_captured: list[int],
    target_frames: int,
    done_event: threading.Event,
):
    """为每个流创建独立的回调闭包，避免数据竞争。"""
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
    return callback


def run() -> list[dict[str, Any]]:
    """
    使用不同设备、不同采样率、相同时长进行多路流式并发录制。

    顺序设置 default → 创建/启动 InputStream（保证设备/采样率解析无竞争），
    然后所有流并发采集，录制结束后各自按对应采样率保存。
    """
    print("=== multi-device concurrent stream-input demo ===")
    t0_total = time.perf_counter()

    init_engine()
    ad.default.rb_seconds = RB_SECONDS

    print(f"duration_s={DURATION_S}, delay_ms={DELAY_MS}, blocksize={BLOCKSIZE}")
    for job in DEVICE_JOBS:
        print(f"  {job['name']}: device={job['device']}, samplerate={job['samplerate']}, "
              f"channels={job['channels_num']}, mapping={job['mapping']}")

    streams: list[ad.InputStream] = []
    all_chunks: list[list[np.ndarray]] = []
    all_frames_captured: list[list[int]] = []
    all_done_events: list[threading.Event] = []
    all_target_frames: list[int] = []

    # ---- 顺序启动各流（保证 default.device / default.samplerate 解析不冲突） ----
    for job in DEVICE_JOBS:
        sr = int(job["samplerate"])
        target_frames = int(round(float(sr) * float(DURATION_S)))

        chunks: list[np.ndarray] = []
        frames_captured = [0]
        done_event = threading.Event()

        ad.default.device = job["device"]
        ad.default.samplerate = sr

        cb = _make_callback(chunks, frames_captured, target_frames, done_event)
        stream = ad.InputStream(
            callback=cb,
            channels=job["input_channels"],
            samplerate=sr,
            blocksize=BLOCKSIZE,
            delay_time=int(DELAY_MS),
            mapping=job["mapping"],
        )
        stream.start()
        # 等引擎工作线程完成设备/采样率解析后，再切换 default 给下一台设备
        time.sleep(0.5)

        streams.append(stream)
        all_chunks.append(chunks)
        all_frames_captured.append(frames_captured)
        all_done_events.append(done_event)
        all_target_frames.append(target_frames)

    print(f"all {len(streams)} streams started, waiting for recording ...")

    # ---- 等待所有流采集完成 ----
    wait_timeout = float(DURATION_S) + float(DELAY_MS) / 1000.0 + 5.0
    for i, (job, done_event) in enumerate(zip(DEVICE_JOBS, all_done_events)):
        finished = done_event.wait(timeout=wait_timeout)
        if not finished:
            print(
                f"WARNING: [{job['name']}] callback did not finish within "
                f"{wait_timeout}s, frames_captured="
                f"{all_frames_captured[i][0]}/{all_target_frames[i]}"
            )

    time.sleep(0.2)

    # ---- 关闭所有流 ----
    for stream in streams:
        try:
            stream.close()
        except Exception:
            pass

    # ---- 保存结果 ----
    results: list[dict[str, Any]] = []
    for i, job in enumerate(DEVICE_JOBS):
        t0 = time.perf_counter()
        name = job["name"]
        device = job["device"]
        sr = int(job["samplerate"])
        mapping = job["mapping"]
        din = int(device[0])
        wav_path = build_wav_path(name, din, mapping)
        save_channels = len(mapping)
        chunks = all_chunks[i]
        captured = all_frames_captured[i][0]
        target_frames = all_target_frames[i]

        result: dict[str, Any] = {
            "name": name,
            "device": device,
            "samplerate": sr,
            "mapping": list(mapping),
            "wav_path": wav_path,
            "ok": False,
        }

        try:
            print(
                f"[{name}] frames_captured={captured}/{target_frames} "
                f"({captured / sr:.3f}s / {DURATION_S}s)"
            )

            if not chunks:
                result["error_type"] = "NoData"
                result["error_msg"] = "没有录到数据"
            else:
                data = np.concatenate(chunks, axis=0)
                if data.shape[0] > target_frames:
                    data = data[:target_frames]
                save_wav(wav_path, data, sr, save_channels)
                result["ok"] = True
                result["shape"] = tuple(data.shape)
                result["active_seconds"] = active_seconds(data, sr)
        except Exception as exc:
            result["error_type"] = type(exc).__name__
            result["error_msg"] = str(exc)
            result["traceback"] = traceback.format_exc()
        finally:
            result["elapsed_sec"] = round(time.perf_counter() - t0, 4)

        results.append(result)

    total_elapsed = round(time.perf_counter() - t0_total, 4)
    print(f"total_elapsed={total_elapsed}s")
    for r in results:
        print_result(r)
    return results


def run_sequential() -> list[dict[str, Any]]:
    """顺序模式：逐台设备流式录制（不存在并发，可用于对比/调试）。"""
    print("=== sequential multi-device stream-input demo ===")
    results: list[dict[str, Any]] = []

    for job in DEVICE_JOBS:
        t0 = time.perf_counter()
        name = job["name"]
        device = job["device"]
        sr = int(job["samplerate"])
        mapping = job["mapping"]
        din = int(device[0])

        init_engine()
        ad.default.samplerate = sr
        ad.default.rb_seconds = RB_SECONDS
        ad.default.device = device

        target_frames = int(round(float(sr) * float(DURATION_S)))
        wav_path = build_wav_path(name, din, mapping)
        save_channels = len(mapping)

        result: dict[str, Any] = {
            "name": name,
            "device": device,
            "samplerate": sr,
            "mapping": list(mapping),
            "wav_path": wav_path,
            "ok": False,
        }

        chunks: list[np.ndarray] = []
        frames_captured = [0]
        done_event = threading.Event()

        try:
            cb = _make_callback(chunks, frames_captured, target_frames, done_event)
            stream = ad.InputStream(
                callback=cb,
                channels=job["input_channels"],
                samplerate=sr,
                blocksize=BLOCKSIZE,
                delay_time=int(DELAY_MS),
                mapping=mapping,
            )
            stream.start()

            wait_timeout = float(DURATION_S) + float(DELAY_MS) / 1000.0 + 5.0
            finished = done_event.wait(timeout=wait_timeout)
            if not finished:
                print(
                    f"WARNING: [{name}] callback did not finish within "
                    f"{wait_timeout}s, frames_captured="
                    f"{frames_captured[0]}/{target_frames}"
                )

            time.sleep(0.2)
            try:
                stream.close()
            except Exception:
                pass

            captured = frames_captured[0]
            print(
                f"[{name}] frames_captured={captured}/{target_frames} "
                f"({captured / sr:.3f}s / {DURATION_S}s)"
            )

            if chunks:
                data = np.concatenate(chunks, axis=0)
                if data.shape[0] > target_frames:
                    data = data[:target_frames]
                save_wav(wav_path, data, sr, save_channels)
                result["ok"] = True
                result["shape"] = tuple(data.shape)
                result["active_seconds"] = active_seconds(data, sr)
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


if __name__ == "__main__":
    run()
