"""同设备流式播放：两个"逻辑任务"使用 **相同设备、相同采样率、不同 WAV 文件**
进行流式播放 (OutputStream)，分别路由到不同的输出通道。

核心思路：打开 **一个** OutputStream（合并所有 output_mapping），
回调中按列写入各自的 WAV 数据，播完后自动停止。
避免两个流竞争同一设备导致其中一路回调不被调用。
"""
from __future__ import annotations

import threading
import time
import traceback
import wave
from pathlib import Path
from typing import Any

import numpy as np

import audiodevice as ad

# ── 共享设备配置 ───────────────────────────────────────────────────────────
DEVICE = (15, 17)                 # (device_in, device_out)
SAMPLERATE = 44100
BLOCKSIZE = 10240
# 引擎输出环形缓冲容量（帧）；越大越稳、延迟与启动/收尾成本通常越高。
RB_FRAMES = 4096

TASK_0_MAPPING = [1]              # WAV_PATH_0 → 左声道
TASK_1_MAPPING = [2]              # WAV_PATH_1 → 右声道

# ── 两个不同的 WAV 文件 ──────────────────────────────────────────────────
WAV_PATH_0 = str(r"E:\2026\3\audiodevice_py\examples\车载试音\张三的歌（明亮度）.wav")
WAV_PATH_1 = str(r"E:\2026\3\audiodevice_py\examples\车载试音\晚秋&送别（丰满度）.wav")


def init_engine() -> None:
    root = Path(__file__).resolve().parent.parent
    exe = root / "audiodevice.exe"
    if exe.is_file():
        ad.init(engine_exe=str(exe), engine_cwd=str(root), timeout=10)
    else:
        ad.init(timeout=10)


def wav_duration_sec(path: str) -> float:
    with wave.open(path, "rb") as wf:
        return wf.getnframes() / float(wf.getframerate())


def wav_samplerate(path: str) -> int:
    with wave.open(path, "rb") as wf:
        return int(wf.getframerate())


def load_wav_mono_float32(path: str) -> tuple[np.ndarray, int]:
    """读取 WAV 第一个通道 → float32，形状 (frames, 1)。仅支持 16-bit PCM。"""
    with wave.open(path, "rb") as wf:
        sr = int(wf.getframerate())
        nch = wf.getnchannels()
        sw = wf.getsampwidth()
        if sw != 2:
            raise ValueError(f"仅支持 16-bit PCM WAV: {path!r} (sampwidth={sw})")
        raw = wf.readframes(wf.getnframes())
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    x = x.reshape(-1, nch)
    x = x[:, :1].copy()
    return np.clip(x, -1.0, 1.0), sr


def resample_audio_linear(y: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    """线性插值重采样，保持播放时长不变。"""
    if sr_in == sr_out or y.size == 0:
        return np.asarray(y, dtype=np.float32)
    y = np.asarray(y, dtype=np.float32)
    if y.ndim == 1:
        y = y[:, None]
    n_in = int(y.shape[0])
    n_out = max(1, int(round(n_in * float(sr_out) / float(sr_in))))
    x_old = np.arange(n_in, dtype=np.float64)
    x_new = np.linspace(0.0, float(n_in - 1), n_out)
    ch = int(y.shape[1])
    out = np.empty((n_out, ch), dtype=np.float32)
    for c in range(ch):
        out[:, c] = np.interp(x_new, x_old, y[:, c].astype(np.float64)).astype(np.float32)
    return np.clip(out, -1.0, 1.0)


def _build_combined_mapping(
    *mappings: list[int],
) -> tuple[list[int], list[list[int]]]:
    """合并多组 output_mapping 为一个去重有序列表，并返回每组在合并后的列索引。

    Returns:
        (combined_mapping, column_indices_per_group)
        例如 mappings=([1], [2]) -> combined=[1,2], indices=[[0], [1]]
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


def print_device_info(wav_path_0: str, wav_path_1: str) -> None:
    out_idx = int(DEVICE[1])
    in_idx = int(DEVICE[0])
    print(f"\n  ┌─ 共享设备 ─────────────────────────────────────")
    for label, idx, direction in [("input ", in_idx, "in"), ("output", out_idx, "out")]:
        try:
            info = ad.query_devices(idx)
            name = info.get("name", "?")
            hi = int(info.get("hostapi", -1))
            hostapi = ad.query_hostapis(hi).get("name", "?") if hi >= 0 else "?"
            dev_sr = info.get("default_samplerate", "?")
            max_ch = info.get(f"max_{direction}put_channels", "?")
            print(f"  │ {label}: [{idx}] {name}")
            print(f"  │          hostapi={hostapi}  default_sr={dev_sr}  max_ch={max_ch}")
        except Exception as e:
            print(f"  │ {label}: [{idx}] 查询失败: {e}")
    print("  │ OutputStream: callback 通道数由 output_mapping 自动推断")
    print(f"  │ blocksize={BLOCKSIZE}, rb_frames={RB_FRAMES}")
    print(f"  │ task-0 → output_mapping={TASK_0_MAPPING} (左声道)")
    print(f"  │ task-1 → output_mapping={TASK_1_MAPPING} (右声道)")
    for i, wp in enumerate([wav_path_0, wav_path_1]):
        try:
            dur = wav_duration_sec(wp)
            sr_w = wav_samplerate(wp)
            print(f"  │ wav_{i}={wp}")
            print(f"  │        wav_sr={sr_w}  duration≈{dur:.2f}s")
        except Exception as e:
            print(f"  │ wav_{i}={wp}  (读取失败: {e})")
    print(f"  └────────────────────────────────────────────")


def print_result(result: dict[str, Any]) -> None:
    if result["ok"]:
        print(
            f"  [{result['name']}] OK | elapsed={result['elapsed_sec']}s | "
            f"mapping={result['mapping']} | shape={result.get('shape')} | "
            f"sr={result.get('play_sr')} | frames={result.get('frames_played')} | "
            f"{result.get('wav_path', '')!r}"
        )
    else:
        print(
            f"  [{result['name']}] FAIL | elapsed={result['elapsed_sec']}s | "
            f"mapping={result['mapping']} | "
            f"{result.get('error_type')}: {result.get('error_msg')}"
        )
        if result.get("traceback"):
            print(result["traceback"])


def _load_and_resample(wav_path: str, target_sr: int) -> np.ndarray:
    """加载 WAV 并重采样到 target_sr，返回 (frames, 1) float32。"""
    audio, wav_sr = load_wav_mono_float32(wav_path)
    if wav_sr != target_sr:
        audio = resample_audio_linear(audio, wav_sr, target_sr)
    return audio


def run_together() -> list[dict[str, Any]]:
    """用一个 OutputStream 同时播放两路 WAV（合并 output_mapping，回调中按列分写）。"""
    print("\n=== together (single-stream, split by output channel) ===")
    t0_total = time.perf_counter()

    init_engine()
    ad.default.samplerate = SAMPLERATE
    ad.default.device = DEVICE

    all_mappings = [TASK_0_MAPPING, TASK_1_MAPPING]
    all_names = ["task-0", "task-1"]
    all_wavs = [WAV_PATH_0, WAV_PATH_1]
    combined_mapping, col_indices = _build_combined_mapping(*all_mappings)

    play_sr = SAMPLERATE
    print(f"  combined_mapping={combined_mapping}, play_sr={play_sr}")
    for name, mapping, cols in zip(all_names, all_mappings, col_indices):
        print(f"    {name}: mapping={mapping} -> columns {cols} in combined stream")

    audios: list[np.ndarray] = []
    for name, wav_path in zip(all_names, all_wavs):
        audio = _load_and_resample(wav_path, play_sr)
        audios.append(audio)
        print(f"    {name}: wav={wav_path!r}, shape={audio.shape}")

    max_frames = max(a.shape[0] for a in audios)
    combined_ch = len(combined_mapping)

    pos = [0]
    done_event = threading.Event()

    def callback(indata, outdata, frames, time_info, status):
        start = pos[0]
        end = min(start + int(frames), max_frames)
        count = end - start

        if count <= 0:
            outdata[:, :] = 0.0
            done_event.set()
            raise ad.CallbackStop()

        outdata[:, :] = 0.0

        for audio, cols in zip(audios, col_indices):
            audio_frames = audio.shape[0]
            audio_end = min(end, audio_frames)
            audio_count = max(0, audio_end - start)
            if audio_count > 0:
                for dst_col, src_col in enumerate(cols):
                    outdata[:audio_count, src_col] = audio[start:audio_end, min(dst_col, audio.shape[1] - 1)]

        pos[0] = end
        if end >= max_frames:
            done_event.set()
            raise ad.CallbackStop()

    results: list[dict[str, Any]] = []

    try:
        print(
            f"  stream 参数: callback_out_ch={len(combined_mapping)}, "
            f"output_mapping={combined_mapping}, "
            f"samplerate={play_sr}, blocksize={BLOCKSIZE}"
        )
        stream = ad.OutputStream(
            callback=callback,
            output_mapping=combined_mapping,
            samplerate=play_sr,
            blocksize=BLOCKSIZE,
            rb_frames=RB_FRAMES,
        )
        stream.start()

        t_wait = time.time()
        while True:
            st = ad.get_status() or {}
            if bool(st.get("has_session", False)):
                break
            if (time.time() - t_wait) >= 5.0:
                print("  警告：等待 session 启动超时")
                break
            ad.sleep(50)

        duration_sec = float(max_frames) / float(play_sr)
        wait_timeout = duration_sec + 10.0
        print(f"  播放中… 最长约 {duration_sec:.1f}s")
        done_event.wait(timeout=wait_timeout)

        if not done_event.is_set():
            print(f"  WARNING: callback 未在 {wait_timeout:.0f}s 内完成, "
                  f"pos={pos[0]}/{max_frames}")

        time.sleep(0.2)
        try:
            stream.close()
        except Exception:
            pass

        actual_pos = pos[0]
        print(f"  frames_played={actual_pos}/{max_frames} "
              f"({actual_pos / play_sr:.3f}s / {max_frames / play_sr:.3f}s)")

        for name, mapping, audio, wav_path in zip(all_names, all_mappings, audios, all_wavs):
            t0 = time.perf_counter()
            frames_for_this = min(actual_pos, audio.shape[0])
            results.append({
                "name": name,
                "mapping": list(mapping),
                "wav_path": wav_path,
                "ok": True,
                "play_sr": play_sr,
                "shape": tuple(audio.shape),
                "frames_played": frames_for_this,
                "elapsed_sec": round(time.perf_counter() - t0_total, 4),
            })

    except Exception as exc:
        for name, mapping, wav_path in zip(all_names, all_mappings, all_wavs):
            results.append({
                "name": name, "mapping": list(mapping),
                "wav_path": wav_path, "ok": False,
                "error_type": type(exc).__name__,
                "error_msg": str(exc),
                "traceback": traceback.format_exc(),
                "elapsed_sec": round(time.perf_counter() - t0_total, 4),
            })

    print(f"\n  total_elapsed={round(time.perf_counter() - t0_total, 4)}s")
    for r in results:
        print_result(r)
    return results


def run_separate() -> list[dict[str, Any]]:
    """顺序执行两次流式播放（各自独占流，不存在竞争问题）。"""
    print("\n=== separate (sequential stream output) ===")
    results: list[dict[str, Any]] = []

    for name, mapping, wav_path in [
        ("task-0", TASK_0_MAPPING, WAV_PATH_0),
        ("task-1", TASK_1_MAPPING, WAV_PATH_1),
    ]:
        t0 = time.perf_counter()
        init_engine()
        ad.default.samplerate = SAMPLERATE
        ad.default.device = DEVICE

        play_sr = SAMPLERATE
        audio = _load_and_resample(wav_path, play_sr)
        total_frames = audio.shape[0]
        callback_ch = len(mapping)

        result: dict[str, Any] = {
            "name": name, "mapping": list(mapping),
            "wav_path": wav_path, "ok": False,
        }

        pos = [0]
        done_event = threading.Event()

        def make_callback(buf, n_frames, position, done_evt, ch):
            def callback(indata, outdata, frames, time_info, status):
                start = position[0]
                end = min(start + int(frames), n_frames)
                count = end - start
                if count > 0:
                    outdata[:count, :ch] = buf[start:end, :]
                if count < int(frames):
                    outdata[count:, :] = 0.0
                position[0] = end
                if end >= n_frames:
                    done_evt.set()
                    raise ad.CallbackStop()
            return callback

        try:
            cb = make_callback(audio, total_frames, pos, done_event, callback_ch)
            print(
                f"  [{name}] stream: sr={play_sr}, callback_out_ch={len(mapping)}, "
                f"mapping={mapping}, shape={audio.shape}"
            )
            stream = ad.OutputStream(
                callback=cb,
                output_mapping=mapping,
                samplerate=play_sr,
                blocksize=BLOCKSIZE,
                rb_frames=RB_FRAMES,
            )
            stream.start()

            t_wait = time.time()
            while True:
                st = ad.get_status() or {}
                if bool(st.get("has_session", False)):
                    break
                if (time.time() - t_wait) >= 5.0:
                    print(f"  [{name}] 警告：等待 session 启动超时")
                    break
                ad.sleep(50)

            duration_sec = float(total_frames) / float(play_sr)
            done_event.wait(timeout=duration_sec + 10.0)

            time.sleep(0.2)
            try:
                stream.close()
            except Exception:
                pass

            print(f"  [{name}] frames_played={pos[0]}/{total_frames} "
                  f"({pos[0] / play_sr:.3f}s / {total_frames / play_sr:.3f}s)")
            result["ok"] = True
            result["play_sr"] = play_sr
            result["shape"] = tuple(audio.shape)
            result["frames_played"] = pos[0]
        except Exception as exc:
            result["error_type"] = type(exc).__name__
            result["error_msg"] = str(exc)
            result["traceback"] = traceback.format_exc()
        finally:
            result["elapsed_sec"] = round(time.perf_counter() - t0, 4)

        results.append(result)

    print()
    for r in results:
        print_result(r)
    return results


def run() -> None:
    for label, p in [("WAV_PATH_0", WAV_PATH_0), ("WAV_PATH_1", WAV_PATH_1)]:
        if not p or not Path(p).is_file():
            raise SystemExit(
                f"请设置有效 WAV 路径: {label}={p!r}\n"
                "要求: 16-bit PCM（单声道或多声道均可，只取第一通道）。"
            )

    init_engine()

    print("=" * 60)
    print("  同设备多任务流式播放 WAV（OutputStream + callback）")
    print("  相同设备 + 相同采样率 + 不同文件 + 不同输出通道")
    print("=" * 60)

    print_device_info(WAV_PATH_0, WAV_PATH_1)

    ad.print_default_devices()
    _, dout = ad.default.device
    print(f"  samplerate={ad.default.samplerate}, blocksize={BLOCKSIZE}")
    print(f"  device_const(in,out)={DEVICE}, device_used_out={dout}")
    print("  OutputStream callback 通道数由 output_mapping 自动推断")
    print(f"  task-0 mapping={TASK_0_MAPPING}")
    print(f"  task-1 mapping={TASK_1_MAPPING}")
    print("  Use run_together() for concurrent playback (recommended).")
    print("  Use run_separate() for sequential playback.")

    run_together()


if __name__ == "__main__":
    run()
