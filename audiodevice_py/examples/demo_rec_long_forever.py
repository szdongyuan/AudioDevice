import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import audiodevice as ad


current_file = Path(__file__).resolve()
engine_path = current_file.parent.parent / "audiodevice.exe"
ENGINE_EXE = str(engine_path)


def main() -> None:
    ad.default.auto_start = True
    if engine_path.is_file():
        ad.default.engine_exe = ENGINE_EXE
        ad.default.engine_cwd = os.path.dirname(ENGINE_EXE)

    ad.default.hostapi = "ASIO"
    ad.default.samplerate = 48_000
    ad.default.channels = 2
    ad.default.device_in = "UMC ASIO Driver"

    rotate_s = 5

    out_dir = Path(__file__).resolve().parent
    start_dt = datetime.now()
    start_ts = start_dt.strftime("%Y%m%d_%H%M%S")
    wav_path = str(out_dir / f"{start_ts}.wav")

    h = None
    tried = []
    for sr in (48_000, 44_100, 32_000, 16_000):
        for ch in (1, 2):
            tried.append((sr, ch))
            try:
                h = ad.rec_long(wav_path, rotate_s=rotate_s, samplerate=sr, channels=ch)
                print(f"started: sr={sr}, ch={ch}")
                break
            except Exception as e:
                print(f"start failed: sr={sr}, ch={ch}: {e}")
        if h is not None:
            break

    if h is None:
        raise RuntimeError(f"failed to start long recording; tried={tried!r}")

    base_path = Path(wav_path)
    base_stem = base_path.stem
    base_ext = base_path.suffix or ".wav"
    next_seg_idx = 1

    print("recording forever... (Ctrl+C to stop)")
    try:
        while True:
            while True:
                src = base_path.with_name(f"{base_stem}_{next_seg_idx:05}{base_ext}")
                if not src.exists():
                    break

                seg_dt = start_dt + timedelta(seconds=float(rotate_s) * next_seg_idx)
                seg_ts = seg_dt.strftime("%Y%m%d_%H%M%S")
                dst = base_path.with_name(f"{seg_ts}{base_ext}")
                if dst.exists():
                    dst = base_path.with_name(f"{seg_ts}_{next_seg_idx:05}{base_ext}")

                try:
                    os.replace(src, dst)
                    next_seg_idx += 1
                except OSError:
                    break

            time.sleep(0.2)
    except KeyboardInterrupt:
        print("stopping...")
    finally:
        try:
            h.stop()
        except Exception:
            pass
        print("done:", wav_path)


if __name__ == "__main__":
    main()

