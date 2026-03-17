from __future__ import annotations

import multiprocessing as mp
import time
import traceback
import importlib
from pathlib import Path
from queue import Empty
from typing import Any

import numpy as np
import audiodevice as ad

# 直接改这里即可，无需命令行参数
# 两个进程会并发播放到不同设备
TEST_CONFIG: dict[str, Any] = {
    "proc_1": {
        "play_device": (14, 17),
        "audio_path": r"c:\Users\Administrator\Desktop\python11\20260310.wav",
        "samplerate": 48000,  # None 表示使用音频文件内采样率
    },
    "proc_2": {
        "play_device": (15, 18),
        "audio_path": r"c:\Users\Administrator\Desktop\python11\S004-1_2026-03-16_60cf8486590e_014.wav",
        "samplerate": 48000,  # None 表示使用音频文件内采样率
    },
    "start_gap_ms": 5.0,
    "result_timeout": 20.0,
}


def init_engine() -> None:
    """优先使用仓库内 engine，可回退默认初始化。"""
    root = Path(__file__).resolve().parent / "AudioDevice-master" / "audiodevice_py"
    engine = root / "audiodevice.exe"
    if engine.is_file():
        ad.init(engine_exe=str(engine), engine_cwd=str(root), timeout=10)
    else:
        ad.init(timeout=10)


def load_wav_float32(path: str) -> tuple[np.ndarray, int]:
    """使用 scipy 读取 WAV，兼容 PCM/IEEE float(format=3)。"""
    wavfile = importlib.import_module("scipy.io.wavfile")
    samplerate, data = wavfile.read(path)
    arr = np.asarray(data)

    if np.issubdtype(arr.dtype, np.floating):
        audio = arr.astype(np.float32, copy=False)
    elif arr.dtype == np.uint8:
        audio = (arr.astype(np.float32) - 128.0) / 128.0
    elif np.issubdtype(arr.dtype, np.signedinteger):
        info = np.iinfo(arr.dtype)
        audio = arr.astype(np.float32) / float(max(abs(info.min), abs(info.max)))
    else:
        raise ValueError(f"不支持的 WAV 数据类型: {arr.dtype}")

    if audio.ndim == 1:
        audio = audio[:, None]
    return np.clip(audio, -1.0, 1.0), int(samplerate)


def play_worker(proc_name: str, cfg: dict[str, Any], result_q: mp.Queue) -> None:
    """子进程播放并上报结果。"""
    t0 = time.perf_counter()
    result: dict[str, Any] = {
        "proc": proc_name,
        "device": cfg["device"],
        "ok": False,
        "error_type": None,
        "error_msg": None,
        "traceback": None,
        "elapsed_sec": None,
        "shape": None,
        "dtype": "float32",
        "audio_path": cfg["audio_path"],
        "samplerate_used": None,
    }

    try:
        init_engine()
        audio, wav_sr = load_wav_float32(str(cfg["audio_path"]))
        play_sr = int(cfg["samplerate"]) if cfg["samplerate"] is not None else int(wav_sr)
        ad.default.samplerate = play_sr

        ad.play(
            audio,
            samplerate=play_sr,
            blocking=True,
            device=cfg["device"],
        )

        result["ok"] = True
        result["shape"] = tuple(audio.shape)
        result["dtype"] = str(audio.dtype)
        result["samplerate_used"] = play_sr
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
                f"[{r['proc']}] OK | device={r['device']} | elapsed={r['elapsed_sec']}s | "
                f"shape={r['shape']} | dtype={r['dtype']} | sr={r['samplerate_used']} | "
                f"audio={r['audio_path']}"
            )
        else:
            print(
                f"[{r['proc']}] FAIL | device={r['device']} | elapsed={r['elapsed_sec']}s | "
                f"{r['error_type']}: {r['error_msg']} | audio={r['audio_path']}"
            )
            if r.get("traceback"):
                print(f"[{r['proc']}] traceback:\n{r['traceback']}")

    success = sum(1 for r in results if r["ok"])
    fail = len(results) - success
    print("\n=== 判定 ===")
    print(f"success={success}, fail={fail}")
    if success == 2:
        print("两个进程都成功：双设备并行播放正常。")
    elif success == 1:
        print("一个成功一个失败：某一设备配置、权限或占用状态异常。")
    elif success == 0:
        print("两个都失败：优先检查设备索引、输出通道数、采样率与引擎初始化。")
    else:
        print("结果数量异常，请检查测试流程。")


def run() -> None:
    cfg1 = {
        "device": TEST_CONFIG["proc_1"]["play_device"],
        "audio_path": str(TEST_CONFIG["proc_1"]["audio_path"]),
        "samplerate": TEST_CONFIG["proc_1"]["samplerate"],
    }
    cfg2 = {
        "device": TEST_CONFIG["proc_2"]["play_device"],
        "audio_path": str(TEST_CONFIG["proc_2"]["audio_path"]),
        "samplerate": TEST_CONFIG["proc_2"]["samplerate"],
    }

    start_gap_ms = float(TEST_CONFIG["start_gap_ms"])
    result_timeout = float(TEST_CONFIG["result_timeout"])

    print("=== 双进程双设备播放测试 ===")
    print(
        f"proc-1(device={cfg1['device']}, samplerate={cfg1['samplerate']}, "
        f"audio_path={cfg1['audio_path']})"
    )
    print(
        f"proc-2(device={cfg2['device']}, samplerate={cfg2['samplerate']}, "
        f"audio_path={cfg2['audio_path']})"
    )
    print(f"start_gap_ms={start_gap_ms}, result_timeout={result_timeout}")

    result_q: mp.Queue = mp.Queue()
    p1 = mp.Process(target=play_worker, args=("proc-1", cfg1, result_q), daemon=False)
    p2 = mp.Process(target=play_worker, args=("proc-2", cfg2, result_q), daemon=False)

    t_all = time.perf_counter()
    p1.start()
    time.sleep(max(0.0, start_gap_ms / 1000.0))
    p2.start()

    results: list[dict[str, Any]] = []
    deadline = time.perf_counter() + max(result_timeout, 10.0)
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
