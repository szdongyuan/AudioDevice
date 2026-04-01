"""
demo_output_overflow.py - 稳定触发 status.output_overflow 的 demo

原理（best-effort）：
- OutputStream 的 worker 在 play_write 时，如果引擎输出缓冲已满，会出现
  accepted_frames == 0（或部分接受）。我们把它映射为 status.output_overflow，
  并在下一次 callback 里展示出来（符合 sounddevice 的“status 描述上一段 I/O”习惯）。
- 通过 pacing=False 关闭实时节拍，让回调循环尽可能快地写入，从而稳定把缓冲打满。
"""

from __future__ import annotations

from pathlib import Path
import time

import numpy as np
import audiodevice as ad


SAMPLERATE = 48_000
BLOCKSIZE = 1024
OUTPUT_MAPPING = [1]  # 回调列数由 output_mapping 推断；设备打开通道数由 OutputStream 内部自动兼容
RB_SECONDS = 1  # 缓冲更小，更容易打满
# 可选：如果你想强制指定输出设备，把它改成一个有效的“输出设备 index”（来自 ad.query_devices()）
# 例如 DEVICE_OUT = 12
# 注意：这里不要用 default.device=(in,out) 的方式，因为它会校验输入端是否是有效输入设备。
DEVICE_OUT = None  # type: ignore[assignment]
PRINT_EVERY = 50
MAX_SECONDS = 3.0

def init_engine() -> None:
    root = Path(__file__).resolve().parent.parent
    engine = root / "audiodevice.exe"
    if engine.is_file():
        ad.init(engine_exe=str(engine), engine_cwd=str(root), timeout=10)
    else:
        ad.init(timeout=10)

    ad.print_default_devices()
    ad.default.samplerate = SAMPLERATE
    ad.default.rb_seconds = int(RB_SECONDS)
    if DEVICE_OUT is not None:
        ad.default.device_out = int(DEVICE_OUT)


def main() -> None:
    init_engine()

    t0 = time.perf_counter()
    cb_count = 0
    seen = 0

    phase = 0.0
    freq = 440.0
    amp = 0.05

    def cb(outdata, frames, time_info, status):
        nonlocal cb_count, seen, phase
        cb_count += 1

        if status and (cb_count % int(PRINT_EVERY) == 0 or status.output_overflow):
            print("Status:", status)

        if bool(getattr(status, "output_overflow", False)):
            seen += 1
            if seen >= 3:
                raise ad.CallbackStop()

        # 输出一个简单正弦波，避免全零导致某些后端优化
        t = (np.arange(int(frames), dtype=np.float32) + phase) / float(SAMPLERATE)
        x = (amp * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)
        phase += float(frames)
        if outdata.ndim == 1:
            outdata[:] = x
        else:
            outdata[:, :] = x[:, None]

        # 超时保护
        if (time.perf_counter() - t0) > float(MAX_SECONDS):
            raise ad.CallbackStop()

    print(
        f"Running output_overflow demo... "
        f"(callback_out_ch={len(OUTPUT_MAPPING)}, output_mapping={OUTPUT_MAPPING})"
    )
    with ad.OutputStream(
        samplerate=SAMPLERATE,
        blocksize=BLOCKSIZE,
        output_mapping=OUTPUT_MAPPING,
        callback=cb,  # sounddevice-like: (outdata, frames, time, status)
        pacing=False,  # 关键：关闭节拍，快速写满输出缓冲
    ):
        ad.sleep(int(MAX_SECONDS * 1000) + 500)

    print(f"done. callbacks={cb_count}, output_overflow_seen={seen}")


if __name__ == "__main__":
    main()

