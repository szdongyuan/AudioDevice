"""双设备并发播放：两路各自播放立体声 WAV（16-bit PCM），可同时出声。



两文件时长可以不同；每文件内左右声道长度一致（标准立体声 WAV）。

"""

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



_examples = Path(__file__).resolve().parent



# ── 设备 0：立体声 WAV 路径（16-bit PCM）──────────────────────────────────

DEVICE_0 = (26, 27)  # (device_in, device_out)

DEFAULT_CHANNELS_NUM_0 = (1, 2)

OUTPUT_MAPPING_0 = [1, 2]

WAV_PATH_0 = str(r"E:\2026\3\audiodevice_py\examples\车载试音\张三的歌（明亮度）.wav")  # 改成你的文件路径



# ── 设备 1：立体声 WAV 路径（可与设备 0 时长不同）────────────────────────

DEVICE_1 = (24, 30)

DEFAULT_CHANNELS_NUM_1 = (6, 2)

OUTPUT_MAPPING_1 = [1, 2]

WAV_PATH_1 = str(r"E:\2026\3\audiodevice_py\examples\车载试音\晚秋&送别（丰满度）.wav")  # 改成你的文件路径





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





def load_wav_stereo_float32(path: str) -> tuple[np.ndarray, int]:

    """读取 WAV → float32，形状 (frames, 2)。仅支持 16-bit PCM。"""

    with wave.open(path, "rb") as wf:

        sr = int(wf.getframerate())

        nch = wf.getnchannels()

        sw = wf.getsampwidth()

        if sw != 2:

            raise ValueError(f"仅支持 16-bit PCM WAV: {path!r} (sampwidth={sw})")

        raw = wf.readframes(wf.getnframes())

    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    x = x.reshape(-1, nch)

    if nch == 1:

        x = np.repeat(x, 2, axis=1)

    elif nch > 2:

        x = x[:, :2].copy()

    return np.clip(x, -1.0, 1.0), sr




def output_device_preferred_sr(out_device_index: int) -> int:

    """输出设备在 WASAPI 上常用 default_sr（多为 48000）；与 WAV 44100 不一致时会 open 失败。"""

    try:

        info = ad.query_devices(int(out_device_index))

        ds = info.get("default_samplerate")

        if ds is not None and float(ds) > 0:

            return int(round(float(ds)))

    except Exception:

        pass

    return 48000




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





def align_audio_to_mapping(

    audio: np.ndarray, output_mapping: list[int]

) -> np.ndarray:

    """play() 要求列数 == len(output_mapping)。"""

    if not output_mapping:

        raise ValueError("output_mapping 不能为空")

    y = np.asarray(audio, dtype=np.float32)

    if y.ndim == 1:

        y = y[:, None]

    n, c = len(output_mapping), int(y.shape[1])

    if c == n:

        return y

    if c == 1:

        return np.repeat(y, n, axis=1)

    if c > n:

        if n == 1:

            return np.mean(y, axis=1, keepdims=True).astype(np.float32)

        return y[:, :n].copy()

    pad = np.zeros((y.shape[0], n - c), dtype=np.float32)

    return np.concatenate([y, pad], axis=1)





def print_device_info(

    tag: str,

    device: tuple[int, int],

    channels_num: tuple[int, int],

    output_mapping: list[int],

    wav_path: str,

) -> None:

    out_idx = int(device[1])

    in_idx = int(device[0])

    print(f"\n  ┌─ {tag} ─────────────────────────────────────")

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

    try:

        dur = wav_duration_sec(wav_path)

        sr_w = wav_samplerate(wav_path)

        print(f"  │ wav={wav_path}")

        print(f"  │ wav_sr={sr_w}  duration≈{dur:.2f}s")

    except Exception as e:

        print(f"  │ wav={wav_path}  (读取失败: {e})")

    print(f"  │ channels_num={channels_num}  output_mapping={output_mapping}")

    print(f"  └────────────────────────────────────────────")





def _play_worker(

    proc_name: str,

    device: tuple[int, int],

    channels_num: tuple[int, int],

    output_mapping: list[int],

    wav_path: str,

    result_q: mp.Queue,

) -> None:

    t0 = time.perf_counter()

    result: dict[str, Any] = {

        "proc": proc_name,

        "device": device,

        "output_mapping": list(output_mapping),

        "wav_path": wav_path,

        "play_sr": 0,

        "ok": False,

    }



    try:

        init_engine()

        audio_in, wav_sr = load_wav_stereo_float32(wav_path)

        out_idx = int(device[1])

        default_sr = output_device_preferred_sr(out_idx)

        # 不同设备对 WASAPI 采样率要求不同：耳机常能 44100，部分扬声器只认 48000。

        # 顺序：先 WAV 原采样率，再设备默认，再 48000/44100 补全。

        seen_sr: set[int] = set()

        trials: list[tuple[int, np.ndarray]] = []



        def _trial(sr: int, y: np.ndarray) -> None:

            if sr in seen_sr:

                return

            seen_sr.add(sr)

            trials.append((sr, np.asarray(y, dtype=np.float32)))



        _trial(wav_sr, audio_in)

        if default_sr != wav_sr:

            _trial(default_sr, resample_audio_linear(audio_in, wav_sr, default_sr))

        for sr in (48000, 44100):

            if sr not in seen_sr:

                _trial(sr, resample_audio_linear(audio_in, wav_sr, sr))



        last_sr_err: RuntimeError | None = None

        for play_sr, audio_raw in trials:

            audio = align_audio_to_mapping(audio_raw, output_mapping)

            print(

                f"  [{proc_name}] play 尝试: sr={play_sr}, device={device}, "

                f"ch={channels_num[1]}, mapping={output_mapping}, shape={audio.shape}, "

                f"wav={wav_path!r}"

            )

            try:

                ad.play(

                    audio,

                    samplerate=play_sr,

                    output_mapping=output_mapping,

                    blocking=True,

                    device=device,

                    channels=int(channels_num[1]),

                )

                result["ok"] = True

                result["play_sr"] = int(play_sr)

                result["shape"] = tuple(audio.shape)

                print(f"  [{proc_name}] play 成功 (实际 sr={play_sr})")

                break

            except RuntimeError as e:

                msg = str(e).lower()

                if (

                    "no supported output config" in msg

                    or "sr/ch" in msg

                ):

                    last_sr_err = e

                    print(f"  [{proc_name}] sr={play_sr} 不支持，换下一档采样率…")

                    continue

                raise



        if not result["ok"]:

            if last_sr_err is not None:

                raise last_sr_err

            raise RuntimeError("play: 无可用采样率")

    except Exception as exc:  # noqa: BLE001

        result["error_type"] = type(exc).__name__

        result["error_msg"] = str(exc)

        result["traceback"] = traceback.format_exc()

        print(f"  [{proc_name}] play 失败: {exc}")

    finally:

        result["elapsed_sec"] = round(time.perf_counter() - t0, 4)

        result_q.put(result)





def _collect_results(result_q: mp.Queue, expected: int, timeout: float) -> list[dict[str, Any]]:

    results: list[dict[str, Any]] = []

    deadline = time.perf_counter() + timeout

    while len(results) < expected and time.perf_counter() < deadline:

        try:

            results.append(result_q.get(timeout=0.3))

        except Empty:

            pass

    return results





def _print_results(results: list[dict[str, Any]]) -> None:

    for r in sorted(results, key=lambda x: x["proc"]):

        if r["ok"]:

            print(

                f"  [{r['proc']}] OK | elapsed={r['elapsed_sec']}s | "

                f"device={r['device']} | mapping={r['output_mapping']} | "

                f"shape={r.get('shape')} | sr={r.get('play_sr')} | "

                f"{r.get('wav_path', '')!r}"

            )

        else:

            print(

                f"  [{r['proc']}] FAIL | elapsed={r['elapsed_sec']}s | "

                f"device={r['device']} | {r.get('error_type')}: {r.get('error_msg')}"

            )

            if r.get("traceback"):

                print(r["traceback"])





def phase2_concurrent(max_play_sec: float) -> None:

    print("\n" + "=" * 60)

    print("  Phase 2: 双进程并发播放 WAV（各自独立引擎）")

    print("=" * 60)

    print(f"  较长一路约 {max_play_sec:.1f}s（用于 join 超时估算）")

    print("  若两台设备同时响 → 并发成功")



    result_q: mp.Queue = mp.Queue()



    p0 = mp.Process(

        target=_play_worker,

        args=("dev0", DEVICE_0, DEFAULT_CHANNELS_NUM_0, OUTPUT_MAPPING_0, WAV_PATH_0, result_q),

        daemon=False,

    )

    p1 = mp.Process(

        target=_play_worker,

        args=("dev1", DEVICE_1, DEFAULT_CHANNELS_NUM_1, OUTPUT_MAPPING_1, WAV_PATH_1, result_q),

        daemon=False,

    )



    t0 = time.perf_counter()

    p0.start()

    time.sleep(0.05)

    p1.start()



    join_timeout = max(120.0, float(max_play_sec) * 4.0 + 45.0)

    p0.join(timeout=join_timeout)

    p1.join(timeout=join_timeout)



    if p0.is_alive():

        p0.terminate()

        p0.join(timeout=2.0)

    if p1.is_alive():

        p1.terminate()

        p1.join(timeout=2.0)



    results = _collect_results(result_q, 2, timeout=15.0)

    elapsed = round(time.perf_counter() - t0, 2)



    print(f"\n  --- 并发结果 (wall={elapsed}s) ---")

    _print_results(results)



    ok_count = sum(1 for r in results if r.get("ok"))

    if ok_count == 2:

        if elapsed < float(max_play_sec) * 1.8:

            print(f"\n  结论: 两路播放均成功；墙钟 {elapsed}s 较短，倾向于真正并发")

        else:

            print(

                f"\n  结论: 两路播放均成功（墙钟 {elapsed}s；"

                f"长 WAV 时墙钟会大于单文件时长，属正常）"

            )

    elif ok_count == 1:

        print("\n  结论: 仅一路成功，见 FAIL 行")

    elif len(results) < 2:

        print("\n  结论: 未收齐子进程结果，检查 Queue / join_timeout")

    else:

        print("\n  结论: 两路均失败，见上方 FAIL")





def run() -> None:

    for label, p in [("WAV_PATH_0", WAV_PATH_0), ("WAV_PATH_1", WAV_PATH_1)]:

        if not p or not Path(p).is_file():

            raise SystemExit(

                f"请设置有效 WAV 路径: {label}={p!r}\n"

                "要求: 16-bit PCM 立体声（或单声道会自动复制为双声道）。"

            )



    d0 = wav_duration_sec(WAV_PATH_0)

    d1 = wav_duration_sec(WAV_PATH_1)

    max_play_sec = max(d0, d1)



    init_engine()



    print("=" * 60)

    print("  双设备并发播放 WAV（各自独立引擎）")

    print("=" * 60)



    print_device_info("dev0", DEVICE_0, DEFAULT_CHANNELS_NUM_0, OUTPUT_MAPPING_0, WAV_PATH_0)

    print_device_info("dev1", DEVICE_1, DEFAULT_CHANNELS_NUM_1, OUTPUT_MAPPING_1, WAV_PATH_1)



    phase2_concurrent(max_play_sec=max_play_sec)





if __name__ == "__main__":

    mp.freeze_support()

    run()
