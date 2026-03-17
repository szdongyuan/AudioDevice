from __future__ import annotations

import multiprocessing as mp
import time
import traceback
import wave
from pathlib import Path
from queue import Empty
from typing import Any

import numpy as np
import audiodevice as ad

TEST_CONFIG: dict[str, Any] = {
    "samplerate": 48000,
    "seconds": 8.0,
    "device": (10,12),
    "start_gap_ms": 5.0,
    "result_timeout": 20.0,
    "save_dir": "recordings/two_proc_same_device",
}


def init_engine() -> None:
    """优先使用仓库内 engine，可回退默认初始化。"""
    root = Path(__file__).resolve().parent / "AudioDevice-master" / "audiodevice_py"
    engine = root / "audiodevice.exe"
    if engine.is_file():
        ad.init(engine_exe=str(engine), engine_cwd=str(root), timeout=10)
    else:
        ad.init(timeout=10)


def save_wav(path: Path, data: np.ndarray, samplerate: int) -> None:
    """将 float32 音频保存为 16-bit PCM wav。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    x = np.asarray(data, dtype=np.float32)
    if x.ndim == 1:
        x = x[:, None]
    x = np.clip(x, -1.0, 1.0)
    pcm16 = (x * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(int(pcm16.shape[1]))
        wf.setsampwidth(2)
        wf.setframerate(int(samplerate))
        wf.writeframes(pcm16.tobytes())


def record_worker(proc_name: str, cfg: dict[str, Any], result_q: mp.Queue) -> None:
    """子进程录音并上报结果。"""
    t0 = time.perf_counter()
    result: dict[str, Any] = {
        "proc": proc_name,
        "ok": False,
        "error_type": None,
        "error_msg": None,
        "traceback": None,
        "elapsed_sec": None,
        "shape": None,
        "dtype": "float32",
        "wav_path": None,
    }

    try:
        init_engine()
        ad.default.samplerate = int(cfg["samplerate"])
        frames = int(round(float(cfg["seconds"]) * int(cfg["samplerate"])))

        data = ad.rec(
            frames,
            samplerate=int(cfg["samplerate"]),
            channels=6,
            dtype=np.float32,
            blocking=True,
            device=cfg["device"],
        )
        audio = np.asarray(data, dtype=np.float32)

        ts = time.strftime("%Y%m%d_%H%M%S")
        wav_name = f"{ts}_{proc_name}_dev{cfg['device']}.wav"
        wav_path = Path(cfg["save_dir"]) / wav_name
        save_wav(wav_path, audio, int(cfg["samplerate"]))

        result["ok"] = True
        result["shape"] = tuple(audio.shape)
        result["dtype"] = str(audio.dtype)
        result["wav_path"] = str(wav_path)
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
                f"[{r['proc']}] OK | elapsed={r['elapsed_sec']}s | "
                f"shape={r['shape']} | dtype={r['dtype']} | wav={r['wav_path']}"
            )
        else:
            print(
                f"[{r['proc']}] FAIL | elapsed={r['elapsed_sec']}s | "
                f"{r['error_type']}: {r['error_msg']}"
            )
            if r.get("traceback"):
                print(f"[{r['proc']}] traceback:\n{r['traceback']}")

    success = sum(1 for r in results if r["ok"])
    fail = len(results) - success
    print("\n=== 判定 ===")
    print(f"success={success}, fail={fail}")
    if success == 2:
        print("两个进程都成功：设备可能支持并发录音，或驱动/后端提供了共享访问。")
    elif success == 1:
        print("一个成功一个失败：出现同设备并发占用冲突（互斥）迹象。")
    elif success == 0:
        print("两个都失败：优先检查设备索引、权限、采样率与引擎初始化。")
    else:
        print("结果数量异常，请检查测试流程。")


def run() -> None:
    cfg = {
        "samplerate": int(TEST_CONFIG["samplerate"]),
        "seconds": float(TEST_CONFIG["seconds"]),
        "device": TEST_CONFIG["device"],
        "save_dir": str(TEST_CONFIG["save_dir"]),
    }
    start_gap_ms = float(TEST_CONFIG["start_gap_ms"])
    result_timeout = float(TEST_CONFIG["result_timeout"])

    print("=== 双进程同设备录音测试 ===")
    print(
        f"samplerate={cfg['samplerate']}, seconds={cfg['seconds']}, channels=1, "
        f"device={cfg['device']}, dtype=float32, start_gap_ms={start_gap_ms}, "
        f"save_dir={cfg['save_dir']}"
    )

    result_q: mp.Queue = mp.Queue()
    p1 = mp.Process(target=record_worker, args=("proc-1", cfg, result_q), daemon=False)
    p2 = mp.Process(target=record_worker, args=("proc-2", cfg, result_q), daemon=False)

    t_all = time.perf_counter()
    p1.start()
    time.sleep(max(0.0, start_gap_ms / 1000.0))
    p2.start()

    results: list[dict[str, Any]] = []
    deadline = time.perf_counter() + max(result_timeout, cfg["seconds"] + 5.0)
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
