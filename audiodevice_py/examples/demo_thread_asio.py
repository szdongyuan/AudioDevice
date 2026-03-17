import os
import threading
import time
from typing import Optional, Tuple

import numpy as np
import audiodevice as ad

from pathlib import Path

_root = Path(__file__).resolve().parent.parent
_engine = _root / "audiodevice.exe"

DURATION_S = 5
TRY_SAMPLERATES = (48_000, 44_100, 32_000, 16_000)
TRY_CHANNELS = (2, 1)
PREFER_NAME_SUBSTR = ("UMC", "ASIO")
WAV_FILENAME = "thread_asio_rec.wav"
INIT_TIMEOUT_S = 10


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
    key = "max_input_channels" if str(direction).strip().lower() == "input" else "max_output_channels"
    candidates = [
        d
        for d in all_devs
        if int(d.get("hostapi", -1)) == asio_hi and int(d.get(key, 0) or 0) > 0
    ]
    if not candidates:
        io = "输入" if str(direction).strip().lower() == "input" else "输出"
        print(f"[warning] 未发现 ASIO {io}设备，请检查 ASIO4ALL/声卡 是否已选择对应通道")
        return -1
    for prefer in PREFER_NAME_SUBSTR:
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
    tried = []
    last_err: Optional[Exception] = None
    for sr in TRY_SAMPLERATES:
        for ch in TRY_CHANNELS:
            tried.append((sr, ch))
            try:
                frames = int(sr) * int(DURATION_S)
                y = ad.rec(
                    frames,
                    blocking=True,
                    samplerate=sr,
                    channels=ch,
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
    wav_path = os.path.join(out_dir, WAV_FILENAME)

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
    # Ensure engine and device list are ready (needed for _pick_asio_device_index).
    if _engine.is_file():
        ad.init(engine_exe=str(_engine), engine_cwd=str(_root), timeout=int(INIT_TIMEOUT_S))
    else:
        ad.init(timeout=int(INIT_TIMEOUT_S))

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

