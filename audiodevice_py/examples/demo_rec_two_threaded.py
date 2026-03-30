from __future__ import annotations

import os
import threading
import time
import traceback
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


# ---- constants (keep this block aligned with demo_playrec.py 17-28) ----
SAMPLERATE = 48000
DURATION_S = 10
DELAY_MS = 34
DEVICE = (24, 30)  # (device_in, device_out)Cannot be used with ASIO.
INPUT_MAPPING = [1]  # 1-based: keep these input channels in returned recording

# ---- end constants block ----


def apply_defaults() -> tuple[int | None, int | None]:
    ad.default.samplerate = int(SAMPLERATE)

    try:
        ad.default.device = DEVICE
    except ValueError as exc:
        print(f"WARNING: {exc}")
        print("WARNING: Falling back to system default devices. Please update DEVICE=(in,out) to match your machine.")

    try:
        din, dout = ad.default.device
        return int(din) if din is not None else None, int(dout) if dout is not None else None
    except Exception:  # noqa: BLE001
        return None, None


THREAD_1_MAPPING = INPUT_MAPPING
THREAD_2_MAPPING = [2]  # 1-based
SAVE_DIR = os.path.join(os.path.dirname(__file__), "recordings", "two_thread_same_device_diff_channels")


def build_wav_path(name: str, mapping: list[int], device_in: int | None) -> str:
    os.makedirs(SAVE_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    dev_tag = str(device_in) if device_in is not None else "default"
    ch_tag = "-".join(map(str, mapping))
    return os.path.join(SAVE_DIR, f"{ts}_{name}_in{dev_tag}_ch{ch_tag}.wav")


def active_seconds(x: np.ndarray, samplerate: int) -> float:
    y = np.asarray(x, dtype=np.float32)
    if y.ndim == 1:
        y = y[:, None]
    mask = np.any(np.abs(y) > 1e-6, axis=1)
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return 0.0
    return round(float(idx[-1] - idx[0] + 1) / float(samplerate), 3)


def record_once(name: str, mapping: list[int]) -> dict[str, Any]:
    t0 = time.perf_counter()
    init_engine()
    din, _ = apply_defaults()
    frames = int(round(float(SAMPLERATE) * float(DURATION_S)))
    wav_path = build_wav_path(name, mapping, din)

    result: dict[str, Any] = {
        "name": name,
        "mapping": list(mapping),
        "wav_path": wav_path,
        "ok": False,
    }

    try:
        x = ad.rec(
            frames,
            blocking=True,
            delay_time=int(DELAY_MS),
            mapping=mapping,
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
            f"mapping={result['mapping']} | shape={result['shape']} | "
            f"active={result['active_seconds']}s | wav={result['wav_path']}"
        )
        return

    print(
        f"[{result['name']}] FAIL | elapsed={result['elapsed_sec']}s | "
        f"mapping={result['mapping']} | {result.get('error_type')}: {result.get('error_msg')}"
    )
    if result.get("traceback"):
        print(result["traceback"])
def run_separate() -> list[dict[str, Any]]:
    print("=== separate ===")
    results = [
        record_once("thread-1", THREAD_1_MAPPING),
        record_once("thread-2", THREAD_2_MAPPING),
    ]
    for result in results:
        print_result(result)
    return results


def run_together() -> list[dict[str, Any]]:
    print("=== together ===")
    results: dict[str, dict[str, Any]] = {}

    def worker(name: str, mapping: list[int]) -> None:
        results[name] = record_once(name, mapping)

    t1 = threading.Thread(target=worker, args=("thread-1", THREAD_1_MAPPING))
    t2 = threading.Thread(target=worker, args=("thread-2", THREAD_2_MAPPING))

    t0 = time.perf_counter()
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    print(f"total_elapsed={round(time.perf_counter() - t0, 4)}s")

    ordered = [results["thread-1"], results["thread-2"]]
    for result in ordered:
        print_result(result)
    return ordered


def run() -> None:
    init_engine()
    ad.print_default_devices()
    din, dout = apply_defaults()

    print("=== two-thread same-device record demo ===")
    print(f"samplerate={SAMPLERATE}, duration_s={DURATION_S}, delay_ms={DELAY_MS}")
    print(f"device_const(in,out)={DEVICE}, device_used(in,out)=({din},{dout})")
    print(f"input_channels_num={INPUT_CHANNELS_NUM}")
    print(f"thread-1 mapping={THREAD_1_MAPPING}")
    print(f"thread-2 mapping={THREAD_2_MAPPING}")
    print("Use run_separate() for sequential calls.")
    print("Use run_together() for concurrent calls.")

    run_together()


if __name__ == "__main__":
    run()