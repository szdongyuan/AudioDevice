from pathlib import Path
import time
import wave

import numpy as np
import audiodevice as ad


# 调用方式参考 demo_stream_input.py：同样的 init + default 配置 + InputStream 用法
CASE_ID = "IS-P1-02"
SAMPLERATE = 48_000
BLOCKSIZE = 1024
RB_SECONDS = 8
DEVICE = (10, 12)  # (device_in, device_out)
DEFAULT_CHANNELS_NUM = (6, 2)  # (in_ch, out_ch) 仅用于 default 配置
DURATION_MS = 6000
TARGET_FRAMES = int(round(SAMPLERATE * (DURATION_MS / 1000.0)))
DELAY_MS = 0
CALLBACK_DELAY_MS = 50  # 故意让 callback 变慢（默认不触发 ratio>=2 的强制异常）
INPUT_MAPPING = [1, 3, 5]  # 1-based: pick these input channels
CHANNELS = 6  # must be >= max(INPUT_MAPPING)
SAVE_CHANNELS = len(INPUT_MAPPING)
SAVE_DIR = Path("recordings/inputstream_timeout")
PRINT_EVERY_N_CALLBACKS = 50


def init_engine() -> None:
    _root = Path(__file__).resolve().parent.parent
    _engine = _root / "audiodevice.exe"
    if _engine.is_file():
        ad.init(engine_exe=str(_engine), engine_cwd=str(_root), timeout=10)
    else:
        # Fallback: use PATH / bundled wheel assets / repo build / auto-download (AUDIODEVICE_ENGINE_URL)
        ad.init(timeout=10)

    ad.print_default_devices()

    # More stable defaults for stream demos
    ad.default.samplerate = SAMPLERATE
    ad.default.device = DEVICE
    ad.default.channels = DEFAULT_CHANNELS_NUM
    ad.default.rb_seconds = RB_SECONDS


def save_wav(path: Path, data: np.ndarray, samplerate: int) -> None:
    """保存为 16-bit PCM wav。"""
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


def run_is_p1_02() -> None:
    init_engine()

    callback_count = 0
    err = None
    chunks = []
    flag_counts = {
        "input_overflow": 0,
        "input_underflow": 0,
        "output_overflow": 0,
        "output_underflow": 0,
    }
    frames_captured = 0
    last_status_snapshot = {}

    def cb(indata, outdata, frames, time_info, status):
        nonlocal callback_count, frames_captured, last_status_snapshot
        callback_count += 1
        if PRINT_EVERY_N_CALLBACKS > 0 and (callback_count % int(PRINT_EVERY_N_CALLBACKS) == 0):
            print(
                f"cb#{callback_count} status.callback_overrun={bool(getattr(status, 'callback_overrun', False))} "
                f"ratio={float(getattr(status, 'callback_overrun_ratio', 0.0)):.2f} "
                f"consecutive={int(getattr(status, 'callback_overrun_consecutive', 0))} "
                f"total={int(getattr(status, 'callback_overrun_total', 0))}"
            )

        for k in flag_counts:
            if bool(getattr(status, k, False)):
                flag_counts[k] += 1

        last_status_snapshot = {
            "callback_seconds": float(getattr(status, "callback_seconds", 0.0)),
            "block_seconds": float(getattr(status, "block_seconds", 0.0)),
            "callback_overrun": bool(getattr(status, "callback_overrun", False)),
            "callback_overrun_ratio": float(getattr(status, "callback_overrun_ratio", 0.0)),
            "callback_overrun_consecutive": int(getattr(status, "callback_overrun_consecutive", 0)),
            "callback_overrun_total": int(getattr(status, "callback_overrun_total", 0)),
        }

        # 复制一份保存（indata 可能在后续回调中被复用）
        remain = int(TARGET_FRAMES) - int(frames_captured)
        if remain <= 0:
            raise ad.CallbackStop()
        take = int(frames) if int(frames) < int(remain) else int(remain)
        if take > 0:
            chunks.append(indata[:take].copy())
            frames_captured += int(take)
        if int(frames_captured) >= int(TARGET_FRAMES):
            raise ad.CallbackStop()

        # 故障注入：让 callback 慢于实时节拍
        if int(CALLBACK_DELAY_MS) > 0:
            time.sleep(float(CALLBACK_DELAY_MS) / 1000.0)

    print(f"=== {CASE_ID} InputStream 回调超时模拟 ===")
    print(
        f"配置: blocksize={BLOCKSIZE}, sr={SAMPLERATE}, channels={CHANNELS}, "
        f"duration={DURATION_MS}ms, callback_delay={CALLBACK_DELAY_MS}ms, rb_seconds={RB_SECONDS}s"
    )

    block_dt = BLOCKSIZE / SAMPLERATE
    delay_ratio = ((CALLBACK_DELAY_MS / 1000.0) / block_dt) if block_dt > 0 else 0.0
    print(f"理论每块时长={block_dt:.6f}s（delay/block_dt={delay_ratio:.2f}x）")
    # 慢回调会降低录制“吞吐率”。若仍按 DURATION_MS 固定等待就 close，可能会提前关流导致录到的时长 < 目标时长。
    max_wait_ms = int((DURATION_MS + DELAY_MS + 500) * max(3.0, delay_ratio * 3.0))
    print(f"等待策略: 目标帧数={TARGET_FRAMES}；最多等待 {max_wait_ms}ms（录够就提前结束）")

    start = time.perf_counter()
    try:
        stream = ad.InputStream(
            callback=cb,
            channels=CHANNELS,
            samplerate=SAMPLERATE,
            blocksize=BLOCKSIZE,
            delay_time=int(DELAY_MS),
            mapping=INPUT_MAPPING,
        )
        stream.start()
        # 注意：sleep 只是“等待”。这里按“录够 TARGET_FRAMES 或超时”来控制关闭，避免慢回调导致录不满。
        deadline = time.perf_counter() + (max_wait_ms / 1000.0)
        while int(frames_captured) < int(TARGET_FRAMES) and time.perf_counter() < deadline:
            ad.sleep(50)
        stream.close()
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    elapsed = time.perf_counter() - start

    expected = int(round(TARGET_FRAMES / BLOCKSIZE))
    ratio = callback_count / max(expected, 1)
    captured_frames = int(sum(c.shape[0] for c in chunks)) if chunks else 0
    captured_seconds = captured_frames / float(SAMPLERATE)

    print(f"elapsed={elapsed:.3f}s")
    print(f"callback_count={callback_count}, expected≈{expected}, ratio={ratio:.3f}")
    print(f"captured_frames={captured_frames}, captured_seconds≈{captured_seconds:.3f}s")
    print(f"flag_counts={flag_counts}")
    print(f"last_status={last_status_snapshot}")
    if err is not None:
        print(f"error={err}")
    if chunks:
        audio = np.concatenate(chunks, axis=0)
        if audio.shape[0] > TARGET_FRAMES:
            audio = audio[:TARGET_FRAMES]
        ts = time.strftime("%Y%m%d_%H%M%S")
        wav_path = SAVE_DIR / f"{CASE_ID}_{ts}.wav"
        try:
            save_wav(wav_path, audio, SAMPLERATE)
            print(f"saved_wav={wav_path}")
        except Exception as e:
            print(f"save_wav_error={type(e).__name__}: {e}")
    else:
        print("saved_wav=SKIPPED(no audio captured)")

    observed_overflow = flag_counts["input_overflow"] > 0
    print(f"input_overflow_observed={'YES' if observed_overflow else 'NO'}")
    if observed_overflow:
        print("结论: 已可观测 input_overflow（符合预期）。")
    else:
        print("结论: 暂未观测到 input_overflow；可增大 delay 或减小 blocksize 重试。")


if __name__ == "__main__":
    run_is_p1_02()
