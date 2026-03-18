"""多线程：两台不同设备各自 rec mapping，同采样率同时长并发录音保存。

使用 threading 而非 multiprocessing，所有线程共享同一引擎进程，
通过 session_id 隔离各自的录音流和 capture ring buffer。
"""
from __future__ import annotations

import threading
import time
import traceback
from pathlib import Path
from typing import Any

import numpy as np

import audiodevice as ad

SAMPLERATE = 48000
DURATION_S = 10
DELAY_MS = 34

# 设备 0：设备 + (in_ch, out_ch) + 1-based rec mapping
DEVICE_0 = (32, 32)
DEFAULT_CHANNELS_NUM_0 = (2, 2)
REC_MAPPING_0 = [2]

# 设备 1
DEVICE_1 = (24, 30)
DEFAULT_CHANNELS_NUM_1 = (6, 2)
REC_MAPPING_1 = [3]

SAVE_DIR = Path(__file__).parent / "recordings" / "diff_processes"


def init_engine() -> None:
    root = Path(__file__).resolve().parent.parent
    exe = root / "audiodevice.exe"
    if exe.is_file():
        ad.init(engine_exe=str(exe), engine_cwd=str(root), timeout=10)
    else:
        ad.init(timeout=10)


def active_seconds(x: np.ndarray, samplerate: int) -> float:
    y = np.asarray(x, dtype=np.float32)
    if y.ndim == 1:
        y = y[:, None]
    mask = np.any(np.abs(y) > 1e-6, axis=1)
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return 0.0
    return round(float(idx[-1] - idx[0] + 1) / float(samplerate), 3)


def build_wav_path(tag: str, device_in: int, mapping: list[int]) -> str:
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    ch_tag = "-".join(map(str, mapping))
    return str(SAVE_DIR / f"{ts}_{tag}_in{device_in}_ch{ch_tag}.wav")


def record_once(
    tag: str,
    device: tuple[int, int],
    channels_num: tuple[int, int],
    mapping: list[int],
) -> dict[str, Any]:
    t0 = time.perf_counter()
    din = int(device[0])
    wav_path = build_wav_path(tag, din, mapping)

    result: dict[str, Any] = {
        "name": tag,
        "device": device,
        "mapping": list(mapping),
        "wav_path": wav_path,
        "ok": False,
    }

    try:
        frames = int(SAMPLERATE * DURATION_S)
        x = ad.rec(
            frames,
            blocking=True,
            delay_time=int(DELAY_MS),
            channels=int(channels_num[0]),
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
        result["active_seconds"] = active_seconds(x, int(SAMPLERATE))
    except Exception as exc:  # noqa: BLE001
        result["error_type"] = type(exc).__name__
        result["error_msg"] = str(exc)
        result["traceback"] = traceback.format_exc()
    finally:
        result["elapsed_sec"] = round(time.perf_counter() - t0, 4)

    return result


def print_result(result: dict[str, Any]) -> None:
    if result["ok"]:
        print(
            f"[{result['name']}] OK | elapsed={result['elapsed_sec']}s | "
            f"device={result['device']} | mapping={result['mapping']} | "
            f"shape={result['shape']} | active={result['active_seconds']}s | "
            f"wav={result['wav_path']}"
        )
        return

    print(
        f"[{result['name']}] FAIL | elapsed={result['elapsed_sec']}s | "
        f"device={result['device']} | mapping={result['mapping']} | "
        f"{result.get('error_type')}: {result.get('error_msg')}"
    )
    if result.get("traceback"):
        print(result["traceback"])


def run() -> None:
    init_engine()
    ad.default.samplerate = int(SAMPLERATE)

    jobs = [
        ("dev0", DEVICE_0, DEFAULT_CHANNELS_NUM_0, REC_MAPPING_0),
        ("dev1", DEVICE_1, DEFAULT_CHANNELS_NUM_1, REC_MAPPING_1),
    ]

    print("=== multi-device concurrent record demo ===")
    print(f"samplerate={SAMPLERATE}, duration_s={DURATION_S}, delay_ms={DELAY_MS}")
    for tag, dev, ch, mp_ in jobs:
        print(f"  {tag}: device={dev}, channels={ch}, mapping={mp_}")

    results: dict[str, dict[str, Any]] = {}

    def worker(tag: str, device: tuple[int, int], channels_num: tuple[int, int], mapping: list[int]) -> None:
        results[tag] = record_once(tag, device, channels_num, mapping)

    threads = [threading.Thread(target=worker, args=j) for j in jobs]

    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"total_elapsed={round(time.perf_counter() - t0, 4)}s")

    for tag, *_ in jobs:
        print_result(results[tag])


if __name__ == "__main__":
    run()

