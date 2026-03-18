"""
demo_stream_output_diff_processes.py
多个"逻辑任务"使用 **不同设备、不同采样率、不同 WAV 文件** 进行多线程流式播放，
各自指定通道映射和采样率，并通过回调逐块输出音频数据。

核心思路：
  - OutputStream 内部使用 engine 的 "play" 模式，引擎对此模式只支持单个活动 session，
    第二个 session_start(mode="play") 会替代第一个。
  - 改用 ad.Stream（duplex）代替 ad.OutputStream：duplex 使用 "playrec" 模式，
    引擎对此模式支持多个并发 session（与 demo_stream_input_diff_processes 对称）。
    回调中忽略 indata、只写 outdata 即可实现纯输出。
  - 在主线程中 **顺序** 设置 default.device / default.samplerate → 创建并启动
    Stream，等待引擎线程完成设备解析后再切换下一台设备的配置。
  - 启动后各流的回调各自在独立线程中并发播放数据，互不干扰。
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

# ---- engine init ----

def init_engine() -> None:
    root = Path(__file__).resolve().parent.parent
    engine = root / "audiodevice.exe"
    if engine.is_file():
        ad.init(engine_exe=str(engine), engine_cwd=str(root), timeout=10)
    else:
        ad.init(timeout=10)


# ---- constants ----
BLOCKSIZE = 8192
RB_SECONDS = 20

# 设备 0：设备索引 + 采样率 + (in_ch, out_ch) + 1-based output mapping
DEVICE_0 = (26, 27)
SAMPLERATE_0 = 44100
DEFAULT_CHANNELS_NUM_0 = (1, 2)
OUTPUT_CHANNELS_NUM_0 = int(DEFAULT_CHANNELS_NUM_0[1])
OUTPUT_MAPPING_0 = [1, 2]
WAV_PATH_0 = str(r"E:\2026\3\audiodevice_py\examples\车载试音\张三的歌（明亮度）.wav")

# 设备 1
DEVICE_1 = (24, 30)
SAMPLERATE_1 = 48000
DEFAULT_CHANNELS_NUM_1 = (6, 2)
OUTPUT_CHANNELS_NUM_1 = int(DEFAULT_CHANNELS_NUM_1[1])
OUTPUT_MAPPING_1 = [1, 2]
WAV_PATH_1 = str(r"E:\2026\3\audiodevice_py\examples\车载试音\晚秋&送别（丰满度）.wav")

# ---- end constants ----


DEVICE_JOBS: list[dict[str, Any]] = [
    {
        "name": "dev0",
        "device": DEVICE_0,
        "samplerate": SAMPLERATE_0,
        "channels_num": DEFAULT_CHANNELS_NUM_0,
        "output_channels": OUTPUT_CHANNELS_NUM_0,
        "output_mapping": OUTPUT_MAPPING_0,
        "wav_path": WAV_PATH_0,
    },
    {
        "name": "dev1",
        "device": DEVICE_1,
        "samplerate": SAMPLERATE_1,
        "channels_num": DEFAULT_CHANNELS_NUM_1,
        "output_channels": OUTPUT_CHANNELS_NUM_1,
        "output_mapping": OUTPUT_MAPPING_1,
        "wav_path": WAV_PATH_1,
    },
]


def wav_duration_sec(path: str) -> float:
    with wave.open(path, "rb") as wf:
        return wf.getnframes() / float(wf.getframerate())


def wav_samplerate(path: str) -> int:
    with wave.open(path, "rb") as wf:
        return int(wf.getframerate())


def load_wav_float32(path: str, max_channels: int | None = None) -> tuple[np.ndarray, int]:
    """读取 WAV → float32，形状 (frames, channels)。仅支持 16-bit PCM。
    max_channels 不为 None 时只保留前 max_channels 个通道。"""
    with wave.open(path, "rb") as wf:
        sr = int(wf.getframerate())
        nch = wf.getnchannels()
        sw = wf.getsampwidth()
        if sw != 2:
            raise ValueError(f"仅支持 16-bit PCM WAV: {path!r} (sampwidth={sw})")
        raw = wf.readframes(wf.getnframes())
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    x = x.reshape(-1, nch)
    if max_channels is not None and nch > max_channels:
        x = x[:, :max_channels]
    return np.clip(x, -1.0, 1.0).copy(), sr


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


def _load_and_resample(wav_path: str, target_sr: int,
                       max_channels: int | None = None) -> np.ndarray:
    """加载 WAV 并重采样到 target_sr，返回 (frames, channels) float32。"""
    audio, wav_sr = load_wav_float32(wav_path, max_channels=max_channels)
    if wav_sr != target_sr:
        audio = resample_audio_linear(audio, wav_sr, target_sr)
    return audio


def print_device_info() -> None:
    print(f"\n  ┌─ 设备列表 ─────────────────────────────────────")
    for job in DEVICE_JOBS:
        dev = job["device"]
        in_idx, out_idx = int(dev[0]), int(dev[1])
        print(f"  │ [{job['name']}] device=({in_idx}, {out_idx})")
        for label, idx, direction in [("input ", in_idx, "in"), ("output", out_idx, "out")]:
            try:
                info = ad.query_devices(idx)
                name = info.get("name", "?")
                hi = int(info.get("hostapi", -1))
                hostapi = ad.query_hostapis(hi).get("name", "?") if hi >= 0 else "?"
                dev_sr = info.get("default_samplerate", "?")
                max_ch = info.get(f"max_{direction}put_channels", "?")
                print(f"  │   {label}: [{idx}] {name}")
                print(f"  │            hostapi={hostapi}  default_sr={dev_sr}  max_ch={max_ch}")
            except Exception as e:
                print(f"  │   {label}: [{idx}] 查询失败: {e}")
        print(f"  │   samplerate={job['samplerate']}, channels_num={job['channels_num']}, "
              f"output_mapping={job['output_mapping']}")
        wp = job["wav_path"]
        try:
            dur = wav_duration_sec(wp)
            sr_w = wav_samplerate(wp)
            print(f"  │   wav={wp}")
            print(f"  │       wav_sr={sr_w}  duration≈{dur:.2f}s")
        except Exception as e:
            print(f"  │   wav={wp}  (读取失败: {e})")
        print(f"  │")
    print(f"  │ blocksize={BLOCKSIZE}, rb_seconds={RB_SECONDS}")
    print(f"  └────────────────────────────────────────────")


def print_result(result: dict[str, Any]) -> None:
    if result["ok"]:
        print(
            f"  [{result['name']}] OK | elapsed={result['elapsed_sec']}s | "
            f"device={result['device']} | sr={result.get('play_sr')} | "
            f"mapping={result['output_mapping']} | shape={result.get('shape')} | "
            f"frames_played={result.get('frames_played')} | "
            f"wav={result.get('wav_path', '')!r}"
        )
    else:
        print(
            f"  [{result['name']}] FAIL | elapsed={result['elapsed_sec']}s | "
            f"device={result['device']} | "
            f"mapping={result['output_mapping']} | "
            f"{result.get('error_type')}: {result.get('error_msg')}"
        )
        if result.get("traceback"):
            print(result["traceback"])


def _make_output_callback(
    audio: np.ndarray,
    total_frames: int,
    position: list[int],
    done_event: threading.Event,
    out_channels: int,
):
    """为每个输出流创建独立的回调闭包（5 参数形式，兼容 duplex Stream）。"""
    def callback(indata, outdata, frames, time_info, status):
        start = position[0]
        end = min(start + int(frames), total_frames)
        count = end - start

        if count <= 0:
            outdata[:, :] = 0.0
            done_event.set()
            raise ad.CallbackStop()

        outdata[:, :] = 0.0
        src_ch = audio.shape[1]
        fill_ch = min(src_ch, out_channels)
        outdata[:count, :fill_ch] = audio[start:end, :fill_ch]

        position[0] = end
        if end >= total_frames:
            done_event.set()
            raise ad.CallbackStop()
    return callback


def run() -> list[dict[str, Any]]:
    """
    使用不同设备、不同采样率、不同 WAV 文件进行多路并发流式播放。

    使用 ad.Stream（duplex, mode="playrec"）代替 ad.OutputStream（mode="play"），
    因为引擎对 "playrec" 模式支持多个并发 session。
    顺序设置 default → 创建/启动 Stream（保证设备/采样率解析不冲突），
    然后所有流并发播放，结束后汇总结果。
    """
    print("\n=== multi-device concurrent stream-output demo (duplex Stream) ===")
    t0_total = time.perf_counter()

    init_engine()

    for job in DEVICE_JOBS:
        wp = job["wav_path"]
        if not wp or not Path(wp).is_file():
            raise SystemExit(
                f"请设置有效 WAV 路径: [{job['name']}] wav_path={wp!r}\n"
                "要求: 16-bit PCM（单声道或多声道均可）。"
            )

    print_device_info()

    print(f"\n  blocksize={BLOCKSIZE}")
    for job in DEVICE_JOBS:
        print(f"    {job['name']}: device={job['device']}, sr={job['samplerate']}, "
              f"channels_num={job['channels_num']}, "
              f"output_mapping={job['output_mapping']}, "
              f"wav={job['wav_path']!r}")

    audios: list[np.ndarray] = []
    play_srs: list[int] = []
    for job in DEVICE_JOBS:
        sr = int(job["samplerate"])
        out_ch = len(job["output_mapping"])
        audio = _load_and_resample(job["wav_path"], sr, max_channels=out_ch)
        audios.append(audio)
        play_srs.append(sr)
        print(f"    {job['name']}: loaded shape={audio.shape}, play_sr={sr}")

    streams: list[ad.Stream] = []
    all_positions: list[list[int]] = []
    all_done_events: list[threading.Event] = []
    all_total_frames: list[int] = []

    # ---- 顺序启动各流（保证 default.device / default.samplerate 解析不冲突） ----
    for i, job in enumerate(DEVICE_JOBS):
        audio = audios[i]
        sr = play_srs[i]
        total_frames = audio.shape[0]
        out_channels = len(job["output_mapping"])

        position: list[int] = [0]
        done_event = threading.Event()

        ad.default.device = job["device"]
        ad.default.channels = job["channels_num"]
        ad.default.samplerate = sr
        ad.default.rb_seconds = RB_SECONDS

        cb = _make_output_callback(audio, total_frames, position, done_event, out_channels)
        print(
            f"    [{job['name']}] creating duplex Stream: "
            f"channels={job['channels_num']}, sr={sr}, "
            f"output_mapping={job['output_mapping']}"
        )
        stream = ad.Stream(
            callback=cb,
            channels=job["channels_num"],
            samplerate=sr,
            blocksize=BLOCKSIZE,
            output_mapping=job["output_mapping"],
        )
        stream.start()
        time.sleep(0.5)

        streams.append(stream)
        all_positions.append(position)
        all_done_events.append(done_event)
        all_total_frames.append(total_frames)

    print(f"\n  all {len(streams)} streams started, playing ...")

    # ---- 等待所有流播放完成 ----
    max_duration = max(
        float(tf) / float(sr)
        for tf, sr in zip(all_total_frames, play_srs)
    )
    wait_timeout = max_duration + 10.0
    for i, (job, done_event) in enumerate(zip(DEVICE_JOBS, all_done_events)):
        finished = done_event.wait(timeout=wait_timeout)
        if not finished:
            print(
                f"  WARNING: [{job['name']}] callback did not finish within "
                f"{wait_timeout:.0f}s, position="
                f"{all_positions[i][0]}/{all_total_frames[i]}"
            )

    time.sleep(0.2)

    # ---- 关闭所有流 ----
    for stream in streams:
        try:
            stream.close()
        except Exception:
            pass

    # ---- 汇总结果 ----
    results: list[dict[str, Any]] = []
    for i, job in enumerate(DEVICE_JOBS):
        played = all_positions[i][0]
        total = all_total_frames[i]
        sr = play_srs[i]
        audio = audios[i]
        print(
            f"  [{job['name']}] frames_played={played}/{total} "
            f"({played / sr:.3f}s / {total / sr:.3f}s)"
        )
        results.append({
            "name": job["name"],
            "device": job["device"],
            "output_mapping": list(job["output_mapping"]),
            "wav_path": job["wav_path"],
            "ok": True,
            "play_sr": sr,
            "shape": tuple(audio.shape),
            "frames_played": played,
            "elapsed_sec": round(time.perf_counter() - t0_total, 4),
        })

    total_elapsed = round(time.perf_counter() - t0_total, 4)
    print(f"\n  total_elapsed={total_elapsed}s")
    for r in results:
        print_result(r)
    return results


def run_sequential() -> list[dict[str, Any]]:
    """顺序模式：逐台设备流式播放（不存在并发，可用于对比/调试）。"""
    print("\n=== sequential multi-device stream-output demo ===")
    results: list[dict[str, Any]] = []

    for job in DEVICE_JOBS:
        t0 = time.perf_counter()
        name = job["name"]
        device = job["device"]
        mapping = job["output_mapping"]
        wav_path = job["wav_path"]

        if not wav_path or not Path(wav_path).is_file():
            results.append({
                "name": name, "device": device,
                "output_mapping": list(mapping),
                "wav_path": wav_path, "ok": False,
                "error_type": "FileNotFound",
                "error_msg": f"WAV 文件不存在: {wav_path!r}",
                "elapsed_sec": round(time.perf_counter() - t0, 4),
            })
            continue

        init_engine()
        play_sr = int(job["samplerate"])
        ad.default.samplerate = play_sr
        ad.default.rb_seconds = RB_SECONDS
        ad.default.device = device
        ad.default.channels = job["channels_num"]
        out_channels = len(mapping)
        audio = _load_and_resample(wav_path, play_sr, max_channels=out_channels)
        total_frames = audio.shape[0]

        result: dict[str, Any] = {
            "name": name, "device": device,
            "output_mapping": list(mapping),
            "wav_path": wav_path, "ok": False,
        }

        position: list[int] = [0]
        done_event = threading.Event()

        try:
            cb = _make_output_callback(audio, total_frames, position, done_event, out_channels)
            print(
                f"  [{name}] stream: device={device}, sr={play_sr}, "
                f"channels={job['channels_num']}, "
                f"mapping={mapping}, shape={audio.shape}"
            )
            stream = ad.Stream(
                callback=cb,
                channels=job["channels_num"],
                output_mapping=mapping,
                samplerate=play_sr,
                blocksize=BLOCKSIZE,
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

            played = position[0]
            print(
                f"  [{name}] frames_played={played}/{total_frames} "
                f"({played / play_sr:.3f}s / {total_frames / play_sr:.3f}s)"
            )
            result["ok"] = True
            result["play_sr"] = play_sr
            result["shape"] = tuple(audio.shape)
            result["frames_played"] = played
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


def run_all() -> None:
    """先并发再顺序，方便对比。"""
    print("=" * 60)
    print("  多设备多任务流式播放 WAV（duplex Stream + callback）")
    print("  不同设备 + 不同采样率 + 不同文件")
    print("=" * 60)
    run()
    print("\n" + "-" * 60)
    run_sequential()


if __name__ == "__main__":
    run()
