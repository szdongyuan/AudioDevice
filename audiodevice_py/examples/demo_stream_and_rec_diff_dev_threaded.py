"""
test.py

多线程启动：每个线程可以指定不同设备。

- 对 `ad.rec()`：直接在每个线程里传 `device=(in_idx, out_idx)` 即可（每次调用独立 session）。
- 对 `ad.InputStream()`：本项目已支持在构造时传 `device=`，并在 `start()` 时对设备选择做快照，
  避免多路并发启动时对 `ad.default.device` 的竞争。

按需修改下方的 DEVICE_* 常量为你机器上的设备索引。
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

# 参考 demo_stream_input_diff_threaded / demo_rec_diff_threaded：按你的机器改
DEVICE_0 = (15, 16)
SAMPLERATE_0 = 44100
CHANNELS_NUM_0 = (1, 2)  # (in_ch, out_ch)
MAPPING_0 = [1]  # 1-based
RB_FRAMES_0 = 4096

DEVICE_1 = (14, 18)
SAMPLERATE_1 = 48000
CHANNELS_NUM_1 = (6, 2)
MAPPING_1 = [3]  # 1-based
RB_FRAMES_1 = 4096

SAVE_DIR = os.path.join(os.path.dirname(__file__), "recordings", "test_multi_thread_multi_device")
# ---- end constants ----


def _build_wav_path(tag: str, device_in: int | None, mapping: list[int], sr: int) -> str:
    os.makedirs(SAVE_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    dev_tag = str(device_in) if device_in is not None else "default"
    ch_tag = "-".join(map(str, mapping))
    return os.path.join(SAVE_DIR, f"{ts}_{tag}_in{dev_tag}_sr{int(sr)}_ch{ch_tag}.wav")


def _save_wav(path: str, data_f32: np.ndarray, samplerate: int, channels: int) -> None:
    pcm = np.clip(np.asarray(data_f32, dtype=np.float32), -1.0, 1.0)
    pcm16 = (pcm * 32767.0).astype(np.int16)
    with wave.open(path, "wb") as wav:
        wav.setnchannels(int(channels))
        wav.setsampwidth(2)
        wav.setframerate(int(samplerate))
        wav.writeframes(pcm16.tobytes())


def _active_seconds(x: np.ndarray, samplerate: int) -> float:
    y = np.asarray(x, dtype=np.float32)
    if y.ndim == 1:
        y = y[:, None]
    mask = np.any(np.abs(y) > 1e-6, axis=1)
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return 0.0
    return round(float(idx[-1] - idx[0] + 1) / float(samplerate), 3)


def _print_result(r: dict[str, Any]) -> None:
    if r.get("ok"):
        print(
            f"[{r.get('name')}] OK | elapsed={r.get('elapsed_sec')}s | "
            f"device={r.get('device')} | sr={r.get('samplerate')} | mapping={r.get('mapping')} | "
            f"shape={r.get('shape')} | active={r.get('active_seconds')}s | wav={r.get('wav_path')}"
        )
        return
    print(
        f"[{r.get('name')}] FAIL | elapsed={r.get('elapsed_sec')}s | "
        f"device={r.get('device')} | sr={r.get('samplerate')} | mapping={r.get('mapping')} | "
        f"{r.get('error_type')}: {r.get('error_msg')}"
    )
    if r.get("traceback"):
        print(r["traceback"])


def _rec_worker(tag: str, device: tuple[int, int], sr: int, in_channels: int, mapping: list[int]) -> dict[str, Any]:
    t0 = time.perf_counter()
    din = int(device[0])
    wav_path = _build_wav_path(f"rec_{tag}", din, mapping, sr)
    frames = int(round(float(sr) * float(DURATION_S)))

    result: dict[str, Any] = {
        "name": f"rec_{tag}",
        "device": device,
        "samplerate": int(sr),
        "mapping": list(mapping),
        "wav_path": wav_path,
        "ok": False,
    }

    try:
        x = ad.rec(
            frames,
            blocking=True,
            delay_time=int(DELAY_MS),
            samplerate=int(sr),
            mapping=mapping,
            device=device,
            dtype=np.float32,
            save_wav=True,
            wav_path=wav_path,
        )
        x = np.asarray(x, dtype=np.float32)
        if x.ndim == 1:
            x = x[:, None]
        result["ok"] = True
        result["shape"] = tuple(x.shape)
        result["active_seconds"] = _active_seconds(x, int(sr))
    except Exception as exc:  # noqa: BLE001
        result["error_type"] = type(exc).__name__
        result["error_msg"] = str(exc)
        result["traceback"] = traceback.format_exc()
    finally:
        result["elapsed_sec"] = round(time.perf_counter() - t0, 4)

    return result


def _stream_worker(tag: str, device: tuple[int, int], sr: int, in_channels: int, mapping: list[int]) -> dict[str, Any]:
    t0 = time.perf_counter()
    din = int(device[0])
    wav_path = _build_wav_path(f"stream_{tag}", din, mapping, sr)
    target_frames = int(round(float(sr) * float(DURATION_S)))

    chunks: list[np.ndarray] = []
    captured = [0]
    done = threading.Event()

    def callback(indata, outdata, frames, time_info, status):
        remain = target_frames - captured[0]
        if remain <= 0:
            done.set()
            raise ad.CallbackStop()
        take = min(int(frames), int(remain))
        if take > 0:
            chunks.append(np.asarray(indata[:take]).copy())
            captured[0] += take
        if captured[0] >= target_frames:
            done.set()
            raise ad.CallbackStop()

    result: dict[str, Any] = {
        "name": f"stream_{tag}",
        "device": device,
        "samplerate": int(sr),
        "mapping": list(mapping),
        "wav_path": wav_path,
        "ok": False,
    }

    stream: ad.InputStream | None = None
    try:
        stream = ad.InputStream(
            device=device,
            samplerate=int(sr),
            channels=int(len(mapping)),
            mapping=mapping,
            blocksize=int(BLOCKSIZE),
            rb_frames=RB_FRAMES_0 if int(sr) == int(SAMPLERATE_0) else RB_FRAMES_1,
            delay_time=int(DELAY_MS),
            callback=callback,
        )
        stream.start()

        wait_timeout = float(DURATION_S) + float(DELAY_MS) / 1000.0 + 6.0
        finished = done.wait(timeout=wait_timeout)
        if not finished:
            raise TimeoutError(f"stream callback timeout: captured={captured[0]}/{target_frames}")

        time.sleep(0.2)
        try:
            stream.close()
        except Exception:
            pass

        if not chunks:
            raise RuntimeError("NoData: 没有录到数据")
        data = np.concatenate(chunks, axis=0)
        if data.shape[0] > target_frames:
            data = data[:target_frames]
        _save_wav(wav_path, data, int(sr), len(mapping))
        result["ok"] = True
        result["shape"] = tuple(data.shape)
        result["active_seconds"] = _active_seconds(data, int(sr))
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

    return result


def run() -> None:
    init_engine()

    print("=== test: multi-thread multi-device ===")
    ad.print_default_devices()

    jobs = [
        ("dev0", DEVICE_0, SAMPLERATE_0, CHANNELS_NUM_0, MAPPING_0),
        ("dev1", DEVICE_1, SAMPLERATE_1, CHANNELS_NUM_1, MAPPING_1),
    ]

    print("--- concurrent rec() (each thread specifies device=...) ---")
    rec_results: dict[str, dict[str, Any]] = {}

    def _rec_thread(job) -> None:
        tag, device, sr, ch_num, mapping = job
        rec_results[tag] = _rec_worker(tag, device, int(sr), int(ch_num[0]), list(mapping))

    threads = [threading.Thread(target=_rec_thread, args=(j,)) for j in jobs]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"rec total_elapsed={round(time.perf_counter() - t0, 4)}s")
    for tag, *_ in jobs:
        _print_result(rec_results[tag])

    print("--- concurrent InputStream (each thread specifies device=...) ---")
    stream_results: dict[str, dict[str, Any]] = {}

    def _stream_thread(job) -> None:
        tag, device, sr, ch_num, mapping = job
        stream_results[tag] = _stream_worker(tag, device, int(sr), int(ch_num[0]), list(mapping))

    threads2 = [threading.Thread(target=_stream_thread, args=(j,)) for j in jobs]
    t1 = time.perf_counter()
    for t in threads2:
        t.start()
    for t in threads2:
        t.join()
    print(f"stream total_elapsed={round(time.perf_counter() - t1, 4)}s")
    for tag, *_ in jobs:
        _print_result(stream_results[tag])


if __name__ == "__main__":
    run()

