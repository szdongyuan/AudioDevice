from __future__ import annotations

import multiprocessing as mp
import time
import traceback
import wave
from pathlib import Path
from queue import Empty
from typing import Any

import audiodevice as ad
import numpy as np

# 直接修改这里的参数即可
TEST_CONFIG: dict[str, Any] = {
    # Keep the same stream defaults as demo_stream_playrecord.py.
    "device": (14, 18),  # (input_device_index, output_device_index)
    "samplerate": 48000,
    "seconds": 6.0,
    "blocksize": 1024,
    "output_mapping": [1, 2],
    # 1-based input channel mapping; align with demo_stream_playrecord.py.
    "mapping": [2, 3, 5],
    "result_timeout": 20.0,
    "save_dir": "recordings/two_proc_same_device_stream",
}


def init_engine() -> None:
    root = Path(__file__).resolve().parent / "AudioDevice-master" / "audiodevice_py"
    engine = root / "audiodevice.exe"
    if engine.is_file():
        ad.init(engine_exe=str(engine), engine_cwd=str(root), timeout=10)
    else:
        ad.init(timeout=10)


def save_wav(path: Path, data_f32: np.ndarray, samplerate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    x = np.asarray(data_f32, dtype=np.float32)
    if x.ndim == 1:
        x = x[:, None]
    x = np.clip(x, -1.0, 1.0)
    pcm = (x * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(int(pcm.shape[1]))
        wav.setsampwidth(2)
        wav.setframerate(int(samplerate))
        wav.writeframes(pcm.tobytes())


def resolve_save_dir(save_dir: str) -> Path:
    p = Path(save_dir)
    if p.is_absolute():
        return p
    # Resolve relative paths against the repo root, not the shell cwd.
    return (Path(__file__).resolve().parents[2] / p).resolve()


def channel_record_worker(
    proc_name: str,
    channel_index: int,
    cfg: dict[str, Any],
    start_event: mp.Event,
    audio_q: mp.Queue,
    result_q: mp.Queue,
    start_time: float,
) -> None:
    t0 = time.perf_counter()
    print(f"子进程{proc_name}启动时间: {time.perf_counter() - start_time}s")
    result: dict[str, Any] = {
        "proc": proc_name,
        "device": cfg["device"],
        "ok": False,
        "error_type": None,
        "error_msg": None,
        "traceback": None,
        "elapsed_sec": None,
        "shape": None,
        "wav_path": None,
    }

    try:
        samplerate = int(cfg["samplerate"])
        target_frames = int(round(float(cfg["seconds"]) * samplerate))

        chunks: list[np.ndarray] = []
        frames_captured = [0]

        # 统一等待主进程发令，尽量保证两个进程同时间开始录制。
        if not start_event.wait(timeout=10.0):
            raise TimeoutError("等待统一开始信号超时")

        is_start = False
        while True:
            item = audio_q.get()
            if item is None:
                break
            if not is_start:
                is_start = True
                print(f"子进程{proc_name}开始接收音频数据时间: {time.perf_counter() - start_time}s")
            x = np.asarray(item, dtype=np.float32)
            if x.ndim != 1:
                raise ValueError(f"收到的音频分块维度不正确: {x.shape}")
            remain = target_frames - frames_captured[0]
            if remain <= 0:
                continue
            take = int(x.shape[0]) if int(x.shape[0]) < int(remain) else int(remain)
            if take > 0:
                chunks.append(x[:take].copy())
                frames_captured[0] += int(take)
            if frames_captured[0] >= target_frames:
                # 继续读取直到 sentinel，避免主进程队列阻塞
                pass

        if not chunks:
            raise RuntimeError("没有采集到任何音频帧")

        audio = np.concatenate(chunks, axis=0)[:, None]
        if audio.shape[0] > target_frames:
            audio = audio[:target_frames]

        ts = time.strftime("%Y%m%d_%H%M%S")
        mapping = cfg.get("mapping", None)
        dev_ch = None
        if mapping is not None and 0 <= int(channel_index) < len(mapping):
            dev_ch = int(mapping[int(channel_index)])
        map_tag = f"_map{dev_ch}" if dev_ch is not None else f"_ch{int(channel_index) + 1}"
        wav_name = f"{ts}_{proc_name}_stream_dev{cfg['device']}{map_tag}.wav"
        wav_path = Path(cfg["save_dir"]) / wav_name
        save_wav(wav_path, audio, samplerate)

        result["ok"] = True
        result["shape"] = tuple(audio.shape)
        result["wav_path"] = str(wav_path.resolve())
    except Exception as exc:  # noqa: BLE001
        result["error_type"] = type(exc).__name__
        result["error_msg"] = str(exc)
        result["traceback"] = traceback.format_exc()
    finally:
        result["elapsed_sec"] = round(time.perf_counter() - t0, 4)
        result_q.put(result)


def print_report(results: list[dict[str, Any]], expected_count: int) -> None:
    print("\n=== 子进程结果 ===")
    for r in sorted(results, key=lambda x: x["proc"]):
        if r["ok"]:
            print(
                f"[{r['proc']}] OK | device={r['device']} | elapsed={r['elapsed_sec']}s | "
                f"shape={r['shape']} | wav={r['wav_path']}"
            )
        else:
            print(
                f"[{r['proc']}] FAIL | device={r['device']} | elapsed={r['elapsed_sec']}s | "
                f"{r['error_type']}: {r['error_msg']}"
            )
            if r.get("traceback"):
                print(f"[{r['proc']}] traceback:\n{r['traceback']}")

    success = sum(1 for r in results if r["ok"])
    fail = len(results) - success
    print("\n=== 判定 ===")
    print(f"success={success}, fail={fail}")
    if success == expected_count:
        print(f"{expected_count} 个子进程都成功：单次 stream_playrecord 采集并分发成功。")
    elif success == 0:
        print("全部失败：请检查设备索引、权限、采样率、HostAPI 和 mapping。")
    else:
        print("部分成功部分失败：请检查设备输入通道数是否满足 mapping。")


def run() -> None:
    cfg = {
        "device": tuple(TEST_CONFIG["device"]),
        "samplerate": int(TEST_CONFIG["samplerate"]),
        "seconds": float(TEST_CONFIG["seconds"]),
        "output_mapping": list(TEST_CONFIG.get("output_mapping", [])) if TEST_CONFIG.get("output_mapping", None) is not None else None,
        "mapping": list(TEST_CONFIG.get("mapping", [])) if TEST_CONFIG.get("mapping", None) is not None else None,
        "blocksize": int(TEST_CONFIG["blocksize"]),
        "save_dir": str(resolve_save_dir(str(TEST_CONFIG["save_dir"]))),
    }
    timeout = float(TEST_CONFIG["result_timeout"])

    print("=== 双进程同设备流式录制测试 ===")
    print(
        f"device={cfg['device']} | samplerate={cfg['samplerate']} | "
        f"seconds={cfg['seconds']} | mapping={cfg['mapping']} | "
        f"blocksize={cfg['blocksize']} | save_dir={cfg['save_dir']}"
    )

    start_event = mp.Event()
    result_q: mp.Queue = mp.Queue()

    init_engine()
    samplerate = int(cfg["samplerate"])
    mapping = cfg.get("mapping", None)
    output_mapping = cfg.get("output_mapping", None)
    if mapping is None or len(mapping) == 0:
        raise ValueError("cfg['mapping'] 必须为非空 1-based 列表")
    if any(int(v) < 1 for v in mapping):
        raise ValueError(f"mapping 必须为 1-based 且 >= 1，但得到: {mapping}")
    if output_mapping is None or len(output_mapping) == 0:
        raise ValueError("cfg['output_mapping'] 必须为非空 1-based 列表")
    if any(int(v) < 1 for v in output_mapping):
        raise ValueError(f"output_mapping 必须为 1-based 且 >= 1，但得到: {output_mapping}")

    worker_count = len(mapping)
    audio_qs: list[mp.Queue] = [mp.Queue(maxsize=64) for _ in range(worker_count)]

    start_time = time.perf_counter()
    workers: list[mp.Process] = []
    for idx in range(worker_count):
        p = mp.Process(
            target=channel_record_worker,
            args=(f"proc-{idx + 1}", idx, cfg, start_event, audio_qs[idx], result_q, start_time),
            daemon=False,
        )
        workers.append(p)

    t_all = time.perf_counter()
    for p in workers:
        p.start()
    print(f"{worker_count} 个子进程启动（共用一次 stream_playrecord 采集）")

    ad.default.device = tuple(cfg["device"])
    ad.default.samplerate = samplerate
    ad.default.rb_seconds = 8

    blocksize = int(cfg["blocksize"])
    target_frames = int(round(float(cfg["seconds"]) * samplerate))
    silent_output = np.zeros((target_frames, len(output_mapping)), dtype=np.float32)
    start_event.set()
    print("开始录音")
    captured = ad.stream_playrecord(
        silent_output,
        samplerate=samplerate,
        blocksize=blocksize,
        delay_time=0.0,
        alignment=False,
        input_mapping=mapping,
        output_mapping=output_mapping,
    )
    captured = np.asarray(captured, dtype=np.float32)
    if captured.ndim == 1:
        captured = captured[:, None]

    for offset in range(0, int(captured.shape[0]), blocksize):
        blk = captured[offset: offset + blocksize]
        for ch in range(worker_count):
            audio_qs[ch].put(blk[:, ch].copy())

    for q in audio_qs:
        q.put(None)

    results: list[dict[str, Any]] = []
    deadline = time.perf_counter() + max(timeout, cfg["seconds"] + 8.0)
    while len(results) < worker_count and time.perf_counter() < deadline:
        try:
            results.append(result_q.get(timeout=0.2))
        except Empty:
            pass

    for p in workers:
        p.join(timeout=1.0)
    for p in workers:
        if p.is_alive():
            p.terminate()

    elapsed_all = round(time.perf_counter() - t_all, 4)
    print(f"总耗时: {elapsed_all}s")
    if len(results) < worker_count:
        print(f"警告：仅收到 {len(results)} / {worker_count} 个结果，可能存在子进程卡死。")
    print_report(results, worker_count)


if __name__ == "__main__":
    mp.freeze_support()
    run()
