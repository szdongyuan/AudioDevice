from __future__ import annotations

import multiprocessing as mp
import os
import time
import traceback
import wave
from pathlib import Path
from queue import Empty
from typing import Any

import numpy as np

import audiodevice as ad


def init_engine() -> None:
    _root = Path(__file__).resolve().parent.parent
    _engine = _root / "audiodevice.exe"
    if _engine.is_file():
        ad.init(engine_exe=str(_engine), engine_cwd=str(_root), timeout=10)
    else:
        ad.init(timeout=10)


# ---- constants (keep this block aligned with demo_playrec.py 17-28) ----
SAMPLERATE = 48000
DURATION_S = 5.0
DELAY_MS = 34
DEVICE = (10, 12)  # (device_in, device_out)
DEFAULT_CHANNELS_NUM = (6, 2)  # (in_ch, out_ch)
OUTPUT_MAPPING = [1]  # 1-based
INPUT_MAPPING = [1, 3, 5]  # 1-based: keep these input channels in returned recording
WAV_PATH = os.path.join(os.path.dirname(__file__), "playrecdelay34ms.wav")

# ---- end constants block ----

def apply_defaults(*, allow_device_fallback: bool = True) -> tuple[int | None, int | None]:
    """
    Apply constants to audiodevice defaults.

    If DEVICE indices are invalid on this machine, fall back to system defaults
    so the demo can still run.
    """
    ad.default.samplerate = int(SAMPLERATE)
    ad.default.channels = DEFAULT_CHANNELS_NUM

    try:
        ad.default.device = DEVICE
    except ValueError as exc:
        if not allow_device_fallback:
            raise
        print(f"WARNING: {exc}")
        print("WARNING: Falling back to system default devices. Please update DEVICE=(in,out) to match your machine.")

    try:
        din, dout = ad.default.device
        return int(din) if din is not None else None, int(dout) if dout is not None else None
    except Exception:  # noqa: BLE001
        return None, None


INPUT_CHANNELS_NUM = int(DEFAULT_CHANNELS_NUM[0])

# Two processes record from the SAME input device, but keep DIFFERENT input channels.
PROC_1_INPUT_MAPPING = INPUT_MAPPING
PROC_2_INPUT_MAPPING = [2, 4, 6]  # 1-based, must be within INPUT_CHANNELS_NUM

SAVE_DIR = os.path.join(os.path.dirname(__file__), "recordings", "two_proc_same_device_diff_channels")


def write_wav_int16(path: str, samplerate: int, data: np.ndarray) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    x = np.asarray(data)
    if x.ndim == 1:
        x = x[:, None]
    x = np.clip(x, -1.0, 1.0)
    pcm = (x * 32767.0).astype("<i2", copy=False)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(int(pcm.shape[1]))
        wf.setsampwidth(2)  # int16
        wf.setframerate(int(samplerate))
        wf.writeframes(pcm.tobytes(order="C"))


def record_worker(proc_name: str, mapping: list[int], wav_path: str, result_q: mp.Queue) -> None:
    t0 = time.perf_counter()
    result: dict[str, Any] = {
        "proc": proc_name,
        "ok": False,
        "error_type": None,
        "error_msg": None,
        "traceback": None,
        "elapsed_sec": None,
        "shape": None,
        "dtype": None,
        "wav_path": wav_path,
        "mapping": list(mapping),
    }

    try:
        init_engine()
        din, _ = apply_defaults(allow_device_fallback=True)

        frames = int(round(float(SAMPLERATE) * float(DURATION_S)))
        x = ad.rec(
            frames,
            blocking=True,
            delay_time=int(DELAY_MS),
            channels=int(INPUT_CHANNELS_NUM),
            mapping=mapping,
            dtype=np.float32,
        )
        x = np.asarray(x, dtype=np.float32)

        # Ensure exact-length file (some backends may return slightly more/less).
        if x.ndim == 1:
            x = x[:, None]
        if x.shape[0] < frames:
            pad = np.zeros((frames - x.shape[0], x.shape[1]), dtype=x.dtype)
            x = np.concatenate([x, pad], axis=0)
        x = x[:frames]

        write_wav_int16(wav_path, int(SAMPLERATE), x)
        result["ok"] = True
        result["shape"] = tuple(x.shape)
        result["dtype"] = str(x.dtype)
        result["device_in_used"] = din
    except Exception as exc:  # noqa: BLE001
        result["error_type"] = type(exc).__name__
        result["error_msg"] = str(exc)
        result["traceback"] = traceback.format_exc()
    finally:
        result["elapsed_sec"] = round(time.perf_counter() - t0, 4)
        result_q.put(result)


def print_report(results: list[dict[str, Any]]) -> None:
    print("\n=== 子进程结果 ===")
    for r in sorted(results, key=lambda x: x["proc"]):
        if r["ok"]:
            print(
                f"[{r['proc']}] OK | elapsed={r['elapsed_sec']}s | mapping={r['mapping']} | "
                f"shape={r['shape']} | dtype={r['dtype']} | wav={r['wav_path']}"
            )
        else:
            print(
                f"[{r['proc']}] FAIL | elapsed={r['elapsed_sec']}s | mapping={r['mapping']} | "
                f"{r['error_type']}: {r['error_msg']}"
            )
            if r.get("traceback"):
                print(f"[{r['proc']}] traceback:\n{r['traceback']}")

    success = sum(1 for r in results if r["ok"])
    fail = len(results) - success
    print("\n=== 判定 ===")
    print(f"success={success}, fail={fail}")
    if success == 2:
        print("两个进程都成功：同一设备可并发录音（或后端提供共享访问）。")
    elif success == 1:
        print("一个成功一个失败：同设备并发录音可能互斥占用。")
    else:
        print("两个都失败：优先检查 device 索引、输入通道数、采样率与引擎初始化。")


def run() -> None:
    init_engine()
    ad.print_default_devices()
    din, dout = apply_defaults(allow_device_fallback=True)

    ts = time.strftime("%Y%m%d_%H%M%S")
    dev_tag = str(din) if din is not None else "default"
    wav1 = os.path.join(SAVE_DIR, f"{ts}_proc-1_in{dev_tag}_ch{'-'.join(map(str, PROC_1_INPUT_MAPPING))}.wav")
    wav2 = os.path.join(SAVE_DIR, f"{ts}_proc-2_in{dev_tag}_ch{'-'.join(map(str, PROC_2_INPUT_MAPPING))}.wav")

    start_gap_ms = 5.0
    result_timeout = max(20.0, float(DURATION_S) + 10.0)

    print("=== 双进程同设备不同通道录音 demo ===")
    print(f"samplerate={SAMPLERATE}, duration_s={DURATION_S}, delay_ms={DELAY_MS}")
    print(f"device_const(in,out)={DEVICE}, device_used(in,out)=({din},{dout}), input_channels_num={INPUT_CHANNELS_NUM}")
    print(f"proc-1 mapping={PROC_1_INPUT_MAPPING} -> {wav1}")
    print(f"proc-2 mapping={PROC_2_INPUT_MAPPING} -> {wav2}")
    print(f"start_gap_ms={start_gap_ms}, result_timeout={result_timeout}")

    result_q: mp.Queue = mp.Queue()
    p1 = mp.Process(target=record_worker, args=("proc-1", PROC_1_INPUT_MAPPING, wav1, result_q), daemon=False)
    p2 = mp.Process(target=record_worker, args=("proc-2", PROC_2_INPUT_MAPPING, wav2, result_q), daemon=False)

    t_all = time.perf_counter()
    p1.start()
    time.sleep(max(0.0, start_gap_ms / 1000.0))
    p2.start()

    results: list[dict[str, Any]] = []
    deadline = time.perf_counter() + float(result_timeout)
    while len(results) < 2 and time.perf_counter() < deadline:
        try:
            results.append(result_q.get(timeout=0.2))
        except Empty:
            pass

    p1.join(timeout=1.0)
    p2.join(timeout=1.0)
    if p1.is_alive():
        p1.terminate()
    if p2.is_alive():
        p2.terminate()

    elapsed_all = round(time.perf_counter() - t_all, 4)
    print(f"总耗时: {elapsed_all}s")
    if len(results) < 2:
        print(f"警告：仅收到 {len(results)} / 2 个结果，可能存在子进程卡死或初始化超时。")
    print_report(results)


if __name__ == "__main__":
    mp.freeze_support()
    run()

