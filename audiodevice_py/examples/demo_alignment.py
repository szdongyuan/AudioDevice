"""
demo_alignment.py - 对齐验证（只展示对齐细节）

固定默认配置：
- ad.default.device = (14, 18)
- ad.default.samplerate = 48_000

会播放 4 次：
- playrec raw / aligned
- stream_playrecord raw / aligned
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

import audiodevice as ad
from audiodevice.alignment_processing import AlignmentProcessing

_root = Path(__file__).resolve().parent.parent
_engine = _root / "audiodevice.exe"
if _engine.is_file():
    ad.init(engine_exe=str(_engine), engine_cwd=str(_root), timeout=10)
else:
    ad.init(timeout=10)
ad.print_default_devices()

SAMPLERATE = 48_000
IN_CH = 6
OUT_CH = 2
DURATION_S = 2.0
AMP = 0.1
BLOCKSIZE = 1024
ALIGNMENT_CH = 1  # 1-based
DEVICE = (14, 18)  # (device_in, device_out)

ad.default.device = DEVICE
ad.default.samplerate = SAMPLERATE


def to_mono(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    if x.ndim == 1:
        return x
    if x.ndim == 2:
        if x.shape[1] == 1:
            return x[:, 0]
        return np.mean(x, axis=1)
    raise ValueError("audio array must be 1D or 2D")


def make_stimulus(fs: int, duration_s: float, out_ch: int, amp: float) -> np.ndarray:
    n = int(round(float(fs) * float(duration_s)))
    t = np.arange(n, dtype=np.float32) / float(fs)
    # Chirp sweep (more robust for alignment than a pure tone).
    f0 = 5000.0
    f1 = 20000.0
    k = (float(f1) - float(f0)) / max(1e-9, float(duration_s))
    phase = 2.0 * np.pi * (float(f0) * t + 0.5 * k * t * t)
    y = np.sin(phase).astype(np.float32)
    # Gentle fade to avoid clicks at boundaries.
    fade_n = int(round(0.01 * float(fs)))  # 10 ms
    if fade_n > 1 and (2 * fade_n) < int(y.size):
        w = np.ones((int(y.size),), dtype=np.float32)
        ramp = np.hanning(int(2 * fade_n)).astype(np.float32)
        w[:fade_n] = ramp[:fade_n]
        w[-fade_n:] = ramp[-fade_n:]
        y *= w
    y = (float(amp) * y).astype(np.float32)
    if int(out_ch) > 1:
        y = np.stack([y] * int(out_ch), axis=1)
    else:
        y = y[:, None]
    return y


def gcc_delay(stimulus: np.ndarray, rec: np.ndarray) -> tuple[int, np.ndarray, int]:
    d, corr, max_shift = AlignmentProcessing.gcc_phat(to_mono(stimulus), to_mono(rec))
    return int(d), np.asarray(corr, dtype=np.float32), int(max_shift)

def _rms(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=np.float32)
    if x.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(x * x)))


def gain_to_match_stimulus(stim: np.ndarray, ref: np.ndarray, *, max_gain: float = 200.0) -> float:
    """按 RMS 计算增益：把 ref 幅值缩放到接近 stim（仅用于显示）。"""
    s = to_mono(stim)
    r = to_mono(ref)
    n = int(min(s.size, r.size))
    s = s[:n]
    r = r[:n]
    s_rms = _rms(s)
    r_rms = _rms(r)
    if s_rms <= 0.0 or r_rms <= 1e-12:
        return 1.0
    g = s_rms / r_rms 
    g = float(np.clip(g, 0.0, float(max_gain)))
    return g


def plot_full_waveform(
    *,
    fs: int,
    title: str,
    stimulus: np.ndarray,
    raw: np.ndarray,
    aligned: np.ndarray,
    zoom_first_ms: float = 500.0,
) -> None:
    import matplotlib.pyplot as plt

    stim_m = to_mono(stimulus)
    raw_m = to_mono(raw)
    aln_m = to_mono(aligned)

    # Use ONE gain for both raw and aligned for fair comparison.
    g = gain_to_match_stimulus(stimulus, raw)
    raw_m = (np.asarray(raw_m, dtype=np.float32) * float(g)).astype(np.float32)
    aln_m = (np.asarray(aln_m, dtype=np.float32) * float(g)).astype(np.float32)

    n = int(min(stim_m.size, raw_m.size, aln_m.size))
    stim_m = stim_m[:n]
    raw_m = raw_m[:n]
    aln_m = aln_m[:n]

    t_s = np.arange(n, dtype=np.float32) / float(fs)

    fig = plt.figure(figsize=(14, 5), constrained_layout=True)
    ax = fig.add_subplot(1, 1, 1)
    ax.plot(t_s, stim_m, lw=1.0, label="stimulus")
    ax.plot(t_s, raw_m, lw=1.0, label=f"rec raw (gain×{g:.3g})")
    ax.plot(t_s, aln_m, lw=1.0, label=f"rec aligned (gain×{g:.3g})")
    ax.set_title(title)
    ax.set_xlabel("time (s)")
    if zoom_first_ms is not None and float(zoom_first_ms) > 0:
        ax.set_xlim(0.0, float(zoom_first_ms) / 1000.0)
    ax.grid(True, alpha=0.25)
    ax.legend(fontsize=9)


def main() -> None:
    y_ref = make_stimulus(SAMPLERATE, DURATION_S, OUT_CH, AMP)

    x_playrec_raw = ad.playrec(y_ref, blocking=True, alignment=False, channels=IN_CH)
    x_playrec_aligned = ad.playrec(y_ref, blocking=True, alignment=True, alignment_channel=ALIGNMENT_CH, channels=IN_CH)

    x_stream_raw = ad.stream_playrecord(y_ref, blocksize=BLOCKSIZE, alignment=False, channels=IN_CH)
    x_stream_aligned = ad.stream_playrecord(y_ref, blocksize=BLOCKSIZE, alignment=True, alignment_channel=ALIGNMENT_CH, channels=IN_CH)

    d_pr_raw, _, _ = gcc_delay(y_ref, x_playrec_raw)
    d_pr_aln, _, _ = gcc_delay(y_ref, x_playrec_aligned)
    d_st_raw, _, _ = gcc_delay(y_ref, x_stream_raw)
    d_st_aln, _, _ = gcc_delay(y_ref, x_stream_aligned)
    print(f"playrec: delay_raw={d_pr_raw} samples, delay_aligned={d_pr_aln} samples")
    print(f"stream_playrecord: delay_raw={d_st_raw} samples, delay_aligned={d_st_aln} samples")

    out_dir = Path(__file__).resolve().parent
    np.save(str(out_dir / "demo_alignment_stimulus.npy"), y_ref)
    np.save(str(out_dir / "demo_alignment_playrec_raw.npy"), np.asarray(x_playrec_raw, dtype=np.float32))
    np.save(str(out_dir / "demo_alignment_playrec_aligned.npy"), np.asarray(x_playrec_aligned, dtype=np.float32))
    np.save(str(out_dir / "demo_alignment_stream_raw.npy"), np.asarray(x_stream_raw, dtype=np.float32))
    np.save(str(out_dir / "demo_alignment_stream_aligned.npy"), np.asarray(x_stream_aligned, dtype=np.float32))

    # 每个方法一张图（figure），直接弹出，不保存 png
    plot_full_waveform(
        fs=SAMPLERATE,
        title="playrec - full waveform (stimulus vs raw vs aligned)",
        stimulus=y_ref,
        raw=np.asarray(x_playrec_raw, dtype=np.float32),
        aligned=np.asarray(x_playrec_aligned, dtype=np.float32),
        zoom_first_ms=500.0,
    )
    plot_full_waveform(
        fs=SAMPLERATE,
        title="stream_playrecord - full waveform (stimulus vs raw vs aligned)",
        stimulus=y_ref,
        raw=np.asarray(x_stream_raw, dtype=np.float32),
        aligned=np.asarray(x_stream_aligned, dtype=np.float32),
        zoom_first_ms=500.0,
    )

    import matplotlib.pyplot as plt

    plt.show()


if __name__ == "__main__":
    main()
