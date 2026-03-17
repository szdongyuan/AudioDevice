"""
demo_delay.py - 验证 delay_time(ms) 是否生效的示例

思路（抗声卡固有延迟/房间声学延迟）：
- 用同一段播放信号做两次 playrec：
  - 第一次 delay_time=0（baseline）
  - 第二次 delay_time=DELAY_MS
- 播放信号中包含一个“很明显的事件点”（短脉冲/短 burst），位于 EVENT_MS 时刻
- 由于 delay_time>0 会把录音窗口整体往后推（函数返回的是“从更晚开始”的那段录音），
  同一个事件点在返回数组中的位置会整体提前约 DELAY_MS（样本数差值约为 delay_frames）

前提：
- 需要有回放->录音 的回路：比如把声卡输出用线接到输入，或使用虚拟声卡（VB-Audio Cable）。
"""

from __future__ import annotations

from pathlib import Path
import os

import numpy as np

import audiodevice as ad

_root = Path(__file__).resolve().parent.parent
_engine = _root / "audiodevice.exe"
if _engine.is_file():
    ad.init(engine_exe=str(_engine), engine_cwd=str(_root), timeout=10)
else:
    ad.init(timeout=10)
ad.print_default_devices()

SAMPLERATE = 48_000
DURATION_S = 2.0
OUT_CH = 2
IN_CH = 1
DELAY_MS = 500
EVENT_MS = 1000
RB_SECONDS = 8

# More stable defaults for stream-ish demos
ad.default.samplerate = SAMPLERATE
ad.default.rb_seconds = RB_SECONDS


def _peak_index(x: np.ndarray) -> int:
    x = np.asarray(x)
    if x.ndim == 2:
        x = x[:, 0]
    x = np.abs(x.astype(np.float32, copy=False))
    if x.size == 0:
        return 0
    return int(np.argmax(x))


def _to_mono(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    if x.ndim == 2:
        x = x[:, 0]
    return x


def main() -> None:
    n = int(round(float(DURATION_S) * float(SAMPLERATE)))
    y = np.zeros((n,), dtype=np.float32)

    # 用一个短 burst 作为事件点（比单 sample 脉冲更抗噪）
    event_i = int(round(float(EVENT_MS) * float(SAMPLERATE) / 1000.0))
    burst_len = int(round(0.01 * float(SAMPLERATE)))  # 10ms
    t = np.arange(burst_len, dtype=np.float32) / float(SAMPLERATE)
    burst = (0.8 * np.sin(2 * np.pi * 2000.0 * t)).astype(np.float32)
    end_i = min(n, event_i + burst_len)
    y[event_i:end_i] = burst[: max(0, end_i - event_i)]

    if OUT_CH > 1:
        y_play = np.stack([y] * OUT_CH, axis=1)
    else:
        y_play = y

    print(f"FS={SAMPLERATE}, duration={DURATION_S:.3f}s, event={EVENT_MS}ms, delay={DELAY_MS}ms")

    print("第 1 次：delay_time=0（baseline）")
    x0 = ad.playrec(
        y_play,
        blocking=True,
        samplerate=SAMPLERATE,
        channels=IN_CH,
        delay_time=0,
        save_wav=False,
    )
    i0 = _peak_index(x0)
    print(f"baseline peak @ {i0} samples -> {i0 * 1000.0 / SAMPLERATE:.2f} ms")

    print(f"第 2 次：delay_time={DELAY_MS}ms")
    x1 = ad.playrec(
        y_play,
        blocking=True,
        samplerate=SAMPLERATE,
        channels=IN_CH,
        delay_time=DELAY_MS,
        save_wav=False,
    )
    i1 = _peak_index(x1)
    print(f"delayed  peak @ {i1} samples -> {i1 * 1000.0 / SAMPLERATE:.2f} ms")

    # 关键：比较两次事件点位置差，理论上约等于 DELAY_MS（单位 ms）
    shift_samples = i0 - i1
    shift_ms = shift_samples * 1000.0 / SAMPLERATE
    print(f"observed shift: {shift_samples} samples -> {shift_ms:.2f} ms (expected ~{DELAY_MS} ms)")

    # 可选：保存一下两次录音便于目视检查
    out_dir = Path(__file__).resolve().parent
    np.save(str(out_dir / "delay_baseline.npy"), np.asarray(x0))
    np.save(str(out_dir / "delay_delayed.npy"), np.asarray(x1))
    print(f"saved: {os.path.abspath(str(out_dir / 'delay_baseline.npy'))}")
    print(f"saved: {os.path.abspath(str(out_dir / 'delay_delayed.npy'))}")

    # 可选：画图对比（如果已安装 matplotlib）
    try:
        import matplotlib

        matplotlib.use("Agg")  # 无 GUI 环境也能保存图片
        import matplotlib.pyplot as plt
    except Exception as e:
        print(f"未绘图：matplotlib 不可用（{e}）。如需画图：pip install matplotlib")
        return

    x0m = _to_mono(x0)
    x1m = _to_mono(x1)
    peak0 = int(i0)
    peak1 = int(i1)
    win = int(round(0.06 * SAMPLERATE))  # 取峰值前后 60ms 便于观察
    a0 = max(0, peak0 - win)
    b0 = min(len(x0m), peak0 + win)
    a1 = max(0, peak1 - win)
    b1 = min(len(x1m), peak1 + win)

    t0 = (np.arange(a0, b0) - peak0) * 1000.0 / SAMPLERATE
    t1 = (np.arange(a1, b1) - peak1) * 1000.0 / SAMPLERATE

    fig = plt.figure(figsize=(10, 7), dpi=140)
    ax1 = fig.add_subplot(2, 1, 1)
    ax1.plot(np.arange(len(x0m)) * 1000.0 / SAMPLERATE, x0m, lw=0.8, label="baseline (delay=0)")
    ax1.plot(np.arange(len(x1m)) * 1000.0 / SAMPLERATE, x1m, lw=0.8, alpha=0.8, label=f"delayed (delay={DELAY_MS}ms)")
    ax1.axvline(peak0 * 1000.0 / SAMPLERATE, color="C0", ls="--", lw=1.0)
    ax1.axvline(peak1 * 1000.0 / SAMPLERATE, color="C1", ls="--", lw=1.0)
    ax1.set_title(f"playrec delay compare (observed shift ≈ {shift_ms:.2f} ms, expected ≈ {DELAY_MS} ms)")
    ax1.set_xlabel("time (ms)")
    ax1.set_ylabel("amp (mono ch0)")
    ax1.grid(True, alpha=0.25)
    ax1.legend(loc="upper right")

    ax2 = fig.add_subplot(2, 1, 2)
    ax2.plot(t0, x0m[a0:b0], lw=1.0, label="baseline (aligned to its peak)")
    ax2.plot(t1, x1m[a1:b1], lw=1.0, alpha=0.8, label="delayed (aligned to its peak)")
    ax2.axvline(0.0, color="k", ls=":", lw=1.0)
    ax2.set_xlabel("time relative to peak (ms)")
    ax2.set_ylabel("amp (mono ch0)")
    ax2.grid(True, alpha=0.25)
    ax2.legend(loc="upper right")

    fig.tight_layout()
    fig_path = out_dir / f"delay_compare_{int(DELAY_MS)}ms.png"
    fig.savefig(str(fig_path))
    plt.close(fig)
    print(f"plot saved: {os.path.abspath(str(fig_path))}")


if __name__ == "__main__":
    main()

