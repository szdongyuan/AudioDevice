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
CHANNELS = 2
ROTATE_S = 10  # set to 0 to disable rotation

# More stable defaults for stream demos
ad.default.samplerate = SAMPLERATE
ad.default.rb_seconds = 8


@dataclass
class WriterConfig:
    out_dir: Path
    samplerate: int
    channels: int
    rotate_s: int


def _open_wav(path: Path, samplerate: int, channels: int) -> wave.Wave_write:
    w = wave.open(str(path), "wb")
    w.setnchannels(int(channels))
    w.setsampwidth(2)  # int16
    w.setframerate(int(samplerate))
    return w


def main() -> None:
    out_dir = Path(__file__).resolve().parent
    cfg = WriterConfig(out_dir=out_dir, samplerate=SAMPLERATE, channels=CHANNELS, rotate_s=int(ROTATE_S))

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

        seg_start = time.time()
        seg_idx = 0
        next_seg_idx = 1

        current_path = base_path
        wav = _open_wav(current_path, cfg.samplerate, cfg.channels)
        try:
            while not stop_ev.is_set():
                try:
                    blk = q.get(timeout=0.2)
                except queue.Empty:
                    blk = None

                if blk is not None:
                    pcm16 = np.clip(blk, -1.0, 1.0)
                    pcm16 = (pcm16 * 32767.0).astype(np.int16, copy=False)
                    wav.writeframes(pcm16.tobytes())

                if cfg.rotate_s > 0 and (time.time() - seg_start) >= float(cfg.rotate_s):
                    wav.close()
                    # Rename completed segment to timestamp-based filename.
                    seg_dt = start_dt + timedelta(seconds=float(cfg.rotate_s) * float(seg_idx))
                    seg_ts = seg_dt.strftime("%Y%m%d_%H%M%S")
                    dst = cfg.out_dir / f"{seg_ts}{base_ext}"
                    if dst.exists():
                        dst = cfg.out_dir / f"{seg_ts}_{seg_idx:05}{base_ext}"
                    try:
                        os.replace(current_path, dst)
                    except OSError:
                        # If rename fails (e.g. file still locked), keep the temp name.
                        pass

                    seg_idx += 1
                    seg_start = time.time()
                    current_path = cfg.out_dir / f"{base_stem}_{next_seg_idx:05}{base_ext}"
                    next_seg_idx += 1
                    wav = _open_wav(current_path, cfg.samplerate, cfg.channels)
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
            q.put_nowait(indata.copy())
        except queue.Full:
            # Drop data if writer thread cannot keep up.
            pass

    wt = threading.Thread(target=writer, daemon=True)
    wt.start()

    print("Recording forever using InputStream... (Ctrl+C to stop)")
    try:
        with ad.InputStream(
            callback=callback,
            channels=CHANNELS,
            samplerate=SAMPLERATE,
            blocksize=BLOCKSIZE,
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

