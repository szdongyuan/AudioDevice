from __future__ import annotations

import argparse
import wave
from pathlib import Path

import numpy as np
import audiodevice as ad


def init_engine() -> None:
    """优先使用仓库内 engine，可回退默认初始化。"""
    root = Path(__file__).resolve().parent / "AudioDevice-master" / "audiodevice_py"
    engine = root / "audiodevice.exe"
    if engine.is_file():
        ad.init(engine_exe=str(engine), engine_cwd=str(root), timeout=10)
    else:
        ad.init(timeout=10)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="float32 录-播循环测试")
    p.add_argument("--samplerate", type=int, default=48000, help="采样率，默认 48000")
    p.add_argument("--seconds", type=float, default=3.0, help="每次录音时长（秒），默认 1.0")
    p.add_argument("--channels", type=int, default=1, help="录音声道数，默认 1")
    p.add_argument("--loops", type=int, default=2, help="录-播循环次数，默认 2")
    p.add_argument("--save-dir", type=str, default="recordings/dtype_loops", help="每次录音 wav 保存目录")
    return p.parse_args()


def to_float32_for_wav(data: np.ndarray) -> np.ndarray:
    """将不同 dtype 的录音数据转换到 [-1, 1] float32 便于统一写 wav。"""
    arr = np.asarray(data)
    if np.issubdtype(arr.dtype, np.floating):
        return np.clip(arr.astype(np.float32), -1.0, 1.0)
    if np.issubdtype(arr.dtype, np.integer):
        info = np.iinfo(arr.dtype)
        # 对称归一化到 [-1, 1]
        scale = float(max(abs(info.min), abs(info.max)))
        if scale <= 0:
            return np.zeros(arr.shape, dtype=np.float32)
        return np.clip(arr.astype(np.float32) / scale, -1.0, 1.0)
    raise TypeError(f"不支持写 wav 的 dtype: {arr.dtype}")


def save_wav(path: Path, data: np.ndarray, samplerate: int) -> None:
    """将音频保存为 16-bit PCM wav。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    x = np.asarray(data)
    if x.ndim == 1:
        x = x[:, None]
    x = to_float32_for_wav(x)
    pcm16 = (x * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(int(pcm16.shape[1]))
        wf.setsampwidth(2)
        wf.setframerate(int(samplerate))
        wf.writeframes(pcm16.tobytes())


def run() -> None:
    args = parse_args()
    init_engine()

    ad.default.samplerate = args.samplerate
    ad.default.rb_seconds = 8
    # 不主动修改 hostapi，沿用当前默认配置

    frames = int(round(args.seconds * args.samplerate))
    dt_name = "float32"

    print("=== float32 录-播循环测试 ===")
    print(
        f"配置: samplerate={args.samplerate}, seconds={args.seconds}, "
        f"frames={frames}, channels={args.channels}, loops={args.loops}"
    )

    total_rec_ok = 0
    total_play_ok = 0
    total_play_fail = 0
    total_save_ok = 0
    total_save_fail = 0
    save_dir = Path(args.save_dir)

    print(f"\n--- dtype={dt_name} ---")

    rec_ok = 0
    play_ok = 0
    play_fail = 0

    for i in range(1, args.loops + 1):
        # 录音
        try:
            print(f"[{dt_name}][loop {i}] rec start ...")
            data = ad.rec(
                frames,
                samplerate=args.samplerate,
                channels=args.channels,
                blocking=True,
            )
            rec_ok += 1
            total_rec_ok += 1
            print(f"[{dt_name}][loop {i}] rec done: shape={data.shape}, dtype={data.dtype}")
        except Exception as e:
            print(f"[{dt_name}][loop {i}] rec failed: {type(e).__name__}: {e}")
            # 该次录音失败，无法播放，进入下一次循环
            continue

        # 保存每次录音结果
        try:
            wav_path = save_dir / f"rec_{dt_name}_loop{i:03d}.wav"
            save_wav(wav_path, data, args.samplerate)
            total_save_ok += 1
            print(f"[{dt_name}][loop {i}] wav saved: {wav_path}")
        except Exception as e:
            total_save_fail += 1
            print(f"[{dt_name}][loop {i}] wav save failed: {type(e).__name__}: {e}")

        # 播放
        try:
            print(f"[{dt_name}][loop {i}] play start ...")
            ad.play(data, samplerate=args.samplerate, blocking=True)
            play_ok += 1
            total_play_ok += 1
            print(f"[{dt_name}][loop {i}] play done")
        except Exception as e:
            play_fail += 1
            total_play_fail += 1
            print(f"[{dt_name}][loop {i}] play failed, 跳过本次: {type(e).__name__}: {e}")
            # 按需求：播放失败仅跳过本次，继续下一次录-播
            continue

    print(
        f"[{dt_name}] summary: rec_ok={rec_ok}/{args.loops}, "
        f"play_ok={play_ok}/{args.loops}, play_fail={play_fail}"
    )

    print("\n=== 总结 ===")
    print(f"total_rec_ok={total_rec_ok}")
    print(f"total_play_ok={total_play_ok}")
    print(f"total_play_fail={total_play_fail}")
    print(f"total_save_ok={total_save_ok}")
    print(f"total_save_fail={total_save_fail}")


if __name__ == "__main__":
    run()
