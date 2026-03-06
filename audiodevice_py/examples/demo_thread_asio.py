import os
import threading
import time
from typing import Optional, Tuple

import numpy as np
import audiodevice as ad

from pathlib import Path

current_file = Path(__file__).resolve()
engine_path = current_file.parent.parent / "audiodevice.exe"
ENGINE_EXE = str(engine_path)


def _pick_asio_device_name(direction: str) -> str:
    devs = ad.query_devices_raw(hostapi="ASIO", direction=direction)["devices"]
    names = [d.get("name", "") for d in devs]
    if not names:
        print("[warning] 未发现 ASIO 输入设备，请检查 ASIO4ALL/声卡 是否已选择输入通道")
        return ""
    for prefer in ("UMC", "ASIO"):
        for n in names:
            if prefer.lower() in str(n).lower():
                return str(n)
    return str(names[0])


def _try_rec_asio(
    wav_path: str,
    device_in: str,
) -> Tuple[Optional[object], Optional[Exception]]:
    # Try common sr/ch pairs. ASIO devices tend to like 48k.
    # 使用整数帧数 sr*5，避免引擎把 5.0(秒) 误解析为 5 帧导致只录 5 个采样
    duration_seconds = 5
    tried = []
    last_err: Optional[Exception] = None
    for sr in (48_000, 44_100, 32_000, 16_000):
        for ch in (2, 1):
            tried.append((sr, ch))
            try:
                frames = sr * duration_seconds
                y = ad.rec(
                    frames,
                    blocking=True,
                    samplerate=sr,
                    channels=ch,
                    hostapi="ASIO",
                    device_in=device_in,
                    save_wav=True,
                    wav_path=wav_path,
                )
                return y, None
            except Exception as e:
                last_err = e
    return None, RuntimeError(f"ASIO rec failed; tried={tried!r}; last={last_err}")


def worker_record_asio() -> None:
    out_dir = os.path.dirname(__file__)
    wav_path = os.path.join(out_dir, "thread_asio_rec.wav")

    try:
        device_in = _pick_asio_device_name("input")
        print("[worker] ASIO device_in:", device_in or "<default>")
        if not device_in.strip():
            raise RuntimeError("未找到 ASIO 输入设备。请安装/启用 ASIO 驱动并在 ASIO4ALL 中勾选输入通道。")
        y, err = _try_rec_asio(wav_path, device_in=device_in)
        if err is not None:
            raise err
        assert y is not None
        print("[worker] recorded:", y.shape, y.dtype, "wav:", wav_path)
        expected_min_frames = 48000 * 4  # 至少约 4 秒（按 48k 算）
        if y.shape[0] < expected_min_frames:
            print(
                f"[worker] 警告: 仅录到 {y.shape[0]} 帧（约 {y.shape[0]/48000:.2f}s），"
                    "正常应为约 5 秒。可能是 audiodevice 引擎将 duration_s 误当帧数，请检查引擎或换用 demo_rec.py 测试。"
            )
        if y.size == 0:
            print("[worker] 警告: 录音数据为空，请检查 ASIO 输入设备与输入通道映射（如 ASIO4ALL 控制面板）")
        else:
            mx = float(np.abs(y).max())
            if mx < 1e-6:
                print("[worker] 警告: 录音几乎全为静音 (max≈0)，请检查麦克风/线路输入是否已选为 ASIO 输入")
    except Exception as e:
        print("[worker] ERROR:", e)


def main() -> None:
    # Auto start the Rust engine.
    ad.default.auto_start = True
    if engine_path.is_file():
        ad.default.engine_exe = ENGINE_EXE
        ad.default.engine_cwd = os.path.dirname(ENGINE_EXE)

    # We want to demonstrate ASIO in a background thread.
    ad.default.hostapi = "ASIO"

    # 先在主线程启动引擎并查询设备，避免子线程里首次调用时引擎未就绪
    ad.print_default_devices()
    time.sleep(0.3)

    t = threading.Thread(target=worker_record_asio, daemon=True)
    t.start()

    # Main thread can keep doing other work.
    for i in range(30):
        # Simulate other CPU/IO work.
        _ = sum(j * j for j in range(10_000))
        print("[main] tick", i)
        time.sleep(0.1)

    t.join()
    print("[main] done")


if __name__ == "__main__":
    main()

