"""
demo_stream_multithread.py - Use stream together with another Python thread.

This demo shows a safe pattern:
- The audio callback (running in stream worker thread) only consumes precomputed blocks from a Queue.
- A producer thread generates audio blocks and feeds the Queue.
"""
from __future__ import annotations

from pathlib import Path
import queue
import threading
import time

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
DURATION_S = 4.0
F_START_HZ = 200.0
F_END_HZ = 3000.0

# More stable defaults for stream demos
ad.default.samplerate = SAMPLERATE
ad.default.rb_seconds = 8


def main() -> None:
    q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=64)
    stop_ev = threading.Event()

    # Shared state controlled by main thread (protected by a lock)
    lock = threading.Lock()
    params = {"volume": 0.2}
    sample_pos = 0  # absolute sample index for phase continuity

    def producer() -> None:
        nonlocal sample_pos
        dt = float(BLOCKSIZE) / float(SAMPLERATE)
        block_count = 0
        while not stop_ev.is_set():
            with lock:
                vol = float(params["volume"])

            # Log chirp (exponential sweep) f(t)=f0*(f1/f0)^(t/T)
            # Phase(t)=2π * f0 * T / ln(f1/f0) * ((f1/f0)^(t/T)-1)
            t = (sample_pos + np.arange(BLOCKSIZE, dtype=np.float32)) / float(SAMPLERATE)
            T = float(DURATION_S)
            k = float(F_END_HZ) / float(F_START_HZ)
            if k <= 1.0:
                phase = 2.0 * np.pi * float(F_START_HZ) * t
            else:
                c = (2.0 * np.pi * float(F_START_HZ) * T) / np.log(k)
                phase = c * (np.power(k, t / T) - 1.0)

            block = (vol * np.sin(phase)).astype(np.float32)
            block = np.stack([block] * CHANNELS, axis=1)  # (frames, ch)
            sample_pos += int(BLOCKSIZE)

            try:
                q.put(block, timeout=0.1)
            except queue.Full:
                # Drop the block if consumer is too slow.
                continue

            block_count += 1
            if block_count % 20 == 0:
                print(f"  [生产者线程] 已产生 {block_count} 块, sample_pos={sample_pos}")

            # Producer pacing. The callback itself is paced by the stream loop,
            # but we also pace the producer to avoid queue overflow.
            time.sleep(dt * 0.9)

    def callback(indata, outdata, frames, time_info, status):
        try:
            blk = q.get_nowait()
            if blk.shape[0] != frames:
                outdata[:] = 0
            else:
                outdata[:] = blk
        except queue.Empty:
            outdata[:] = 0

    t = threading.Thread(target=producer, daemon=True)
    t.start()

    print(f"Starting stream. Log chirp {F_START_HZ:.0f}Hz -> {F_END_HZ:.0f}Hz ...")
    started = time.time()
    with ad.OutputStream(
        callback=callback,
        channels=CHANNELS,
        samplerate=SAMPLERATE,
        blocksize=BLOCKSIZE,
    ):
        # Keep the stream running; main thread could change params["volume"] if needed.
        last_print = 0.0
        while time.time() - started < float(DURATION_S):
            with lock:
                params["volume"] = params["volume"]
            elapsed = time.time() - started
            if elapsed - last_print >= 0.5:
                print(f"[主线程] 已运行 {elapsed:.1f}s")
                last_print = elapsed
            ad.sleep(50)

    stop_ev.set()
    print("Done.")


if __name__ == "__main__":
    main()

