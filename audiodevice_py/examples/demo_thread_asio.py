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


def _pick_asio_device_index(direction: str) -> int:
    """Return global device index for preferred ASIO input device. -1 if none.
    Uses query_devices() so index is the global index required by default.device_in / rec(device_in=...).
    """
    hostapis = ad.query_hostapis()
    asio_hi = None
    for i, h in enumerate(hostapis):
        if (str(h.get("name") or "").strip().upper() == "ASIO"):
            asio_hi = i
            break
    if asio_hi is None:
        return -1
    all_devs = ad.query_devices()
    candidates = [d for d in all_devs if int(d.get("hostapi", -1)) == asio_hi and (int(d.get("max_input_channels", 0) or 0) > 0]
    if not candidates:
        print("[warning] 未发现 ASIO 输入设备，请检查 ASIO4ALL/声卡 是否已选择输入通道")
        return -1
    for prefer in ("UMC", "ASIO"):
        for d in candidates:
            name = str(d.get("name", "") or "")
            if prefer.lower() in name.lower():
                return int(d.get("index", -1))
    return int(candidates[0].get("index", -1))


def _try_rec_asio(
    wav_path: str,
    device_in: int,
) -> Tuple[Optional[object], Optional[Exception]]:
    # Try common sr/ch pairs. ASIO devices tend to like 48k.
    # device_in is device index (int only); device names are not supported.
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
        device_in = _pick_asio_device_index("input")
        print("[worker] ASIO device_in (index):", device_in if device_in >= 0 else "<default>")
        if device_in < 0:
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

    # Ensure engine and device list are ready (needed for _pick_asio_device_index).
    ad.init()

    # We want to demonstrate ASIO in a background thread. hostapi is read-only; set device to an ASIO device.
    idx = _pick_asio_device_index("input")
    if idx >= 0:
        ad.default.device = (idx, idx)

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

