"""
demo_stream_rec_long_forever.py - "Forever" recording using InputStream callback.

This is a Stream-API alternative to rec_long_forever:
- Capture audio blocks in the stream callback (no disk I/O in callback).
- A writer thread converts float32 blocks to int16 PCM and writes WAV continuously.
- Optionally rotate output files every N seconds.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import os
import queue
import threading
import time
import wave

import numpy as np

import audiodevice as ad

# Initialize engine
_root = Path(__file__).resolve().parent.parent
_engine = _root / "audiodevice.exe"
if _engine.is_file():
    ad.init(engine_exe=str(_engine), engine_cwd=str(_root), timeout=10)
else:
    ad.init(timeout=10)
ad.print_default_devices()

SAMPLERATE = 48_000
BLOCKSIZE = 1024
ROTATE_S = 10.0  # set to 0 to disable rotation
INPUT_MAPPING = [1, 3, 5]  # 1-based: keep these input channels (and order) in WAV
INPUT_CHANNELS = len(INPUT_MAPPING)  # sounddevice-like: callback channels == len(mapping)
RB_SECONDS = 8
DEVICE = (10, 12)  # (device_in, device_out)

# More stable defaults for stream demos
ad.default.samplerate = SAMPLERATE
ad.default.device = DEVICE
ad.default.rb_seconds = RB_SECONDS


@dataclass
class WriterConfig:
    out_dir: Path
    samplerate: int
    channels: int
    rotate_s: float


def _open_wav(path: Path, samplerate: int, channels: int) -> wave.Wave_write:
    w = wave.open(str(path), "wb")
    w.setnchannels(int(channels))
    w.setsampwidth(2)  # int16
    w.setframerate(int(samplerate))
    return w


def main() -> None:
    out_dir = Path(__file__).resolve().parent
    cfg = WriterConfig(out_dir=out_dir, samplerate=SAMPLERATE, channels=len(INPUT_MAPPING), rotate_s=float(ROTATE_S))

    q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=256)
    stop_ev = threading.Event()

    def writer() -> None:
        # Match demo_rec_long_forever.py naming strategy:
        # - Write to temporary segment files: startTs.wav, startTs_00001.wav, ...
        # - After each segment is closed, rename it to segment timestamp: segTs.wav
        start_dt = datetime.now()
        start_ts = start_dt.strftime("%Y%m%d_%H%M%S")
        base_path = cfg.out_dir / f"{start_ts}.wav"
        base_stem = base_path.stem
        base_ext = base_path.suffix or ".wav"

        seg_idx = 0
        next_seg_idx = 1
        fs = int(cfg.samplerate)
        seg_frames_written = 0

        def _segment_target_frames(i: int) -> int:
            if cfg.rotate_s <= 0:
                return 0
            boundary = int(round(float(i + 1) * float(cfg.rotate_s) * float(fs)))
            return max(1, boundary - int(round(float(i) * float(cfg.rotate_s) * float(fs))))

        current_path = base_path
        wav = _open_wav(current_path, cfg.samplerate, cfg.channels)
        if cfg.rotate_s > 0:
            print(f"[writer] rotate every {cfg.rotate_s:g}s -> {current_path.name}")
        else:
            print(f"[writer] rotation disabled -> {current_path.name}")
        try:
            while not stop_ev.is_set():
                try:
                    blk = q.get(timeout=0.2)
                except queue.Empty:
                    blk = None

                if blk is None:
                    continue

                pcm16 = np.clip(blk, -1.0, 1.0)
                pcm16 = (pcm16 * 32767.0).astype(np.int16, copy=False)
                frames_total = int(pcm16.shape[0])
                offset = 0

                while offset < frames_total and not stop_ev.is_set():
                    if cfg.rotate_s <= 0:
                        wav.writeframes(pcm16[offset:, :].tobytes())
                        break

                    target_frames = _segment_target_frames(seg_idx)
                    remaining = int(target_frames - seg_frames_written)
                    if remaining <= 0:
                        remaining = 1

                    take = min(remaining, frames_total - offset)
                    wav.writeframes(pcm16[offset : offset + take, :].tobytes())
                    seg_frames_written += take
                    offset += take

                    if seg_frames_written < target_frames:
                        continue

                    # Close & rename completed segment (sample-accurate length).
                    wav.close()
                    seg_dt = start_dt + timedelta(seconds=float(cfg.rotate_s) * float(seg_idx))
                    seg_ts = seg_dt.strftime("%Y%m%d_%H%M%S")
                    dst = cfg.out_dir / f"{seg_ts}{base_ext}"
                    if dst.exists():
                        dst = cfg.out_dir / f"{seg_ts}_{seg_idx:05}{base_ext}"
                    try:
                        os.replace(current_path, dst)
                    except OSError:
                        pass
                    else:
                        print(f"[writer] rotated -> {dst.name}")

                    seg_idx += 1
                    seg_frames_written = 0
                    current_path = cfg.out_dir / f"{base_stem}_{next_seg_idx:05}{base_ext}"
                    next_seg_idx += 1
                    wav = _open_wav(current_path, cfg.samplerate, cfg.channels)
        except Exception as e:
            # Make writer thread failures obvious (otherwise rotation looks "unused").
            print("[writer] error:", repr(e))
            stop_ev.set()
        finally:
            try:
                wav.close()
            except Exception:
                pass

            # Best-effort rename the last segment as well.
            try:
                seg_dt = start_dt + timedelta(seconds=float(cfg.rotate_s) * float(seg_idx)) if cfg.rotate_s > 0 else start_dt
                seg_ts = seg_dt.strftime("%Y%m%d_%H%M%S")
                dst = cfg.out_dir / f"{seg_ts}{base_ext}"
                if dst.exists():
                    dst = cfg.out_dir / f"{seg_ts}_{seg_idx:05}{base_ext}"
                if current_path.exists():
                    os.replace(current_path, dst)
            except Exception:
                pass

    def callback(indata, outdata, frames, time_info, status):
        # Never do blocking work here.
        try:
            blk = np.asarray(indata)
            if blk.ndim == 1:
                blk = blk[:, None]

            # Ensure WAV channel count and order matches INPUT_MAPPING.
            # If engine already applied mapping, blk.shape[1] should equal len(INPUT_MAPPING).
            if blk.shape[1] != len(INPUT_MAPPING):
                idx = [int(m) - 1 for m in INPUT_MAPPING]
                blk = blk[:, idx]

            q.put_nowait(blk.copy())
        except queue.Full:
            # Drop data if writer thread cannot keep up.
            pass

    wt = threading.Thread(target=writer, daemon=True)
    wt.start()

    print("Recording forever using InputStream... (Ctrl+C to stop)")
    try:
        with ad.InputStream(
            callback=callback,
            channels=int(INPUT_CHANNELS),
            samplerate=SAMPLERATE,
            blocksize=BLOCKSIZE,
            mapping=INPUT_MAPPING,

        ):
            while True:
                ad.sleep(200)
    except KeyboardInterrupt:
        print("Stopping...")
    finally:
        stop_ev.set()
        wt.join(timeout=2.0)
        print("Done. Files are in:", str(out_dir))


if __name__ == "__main__":
    main()

