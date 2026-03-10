import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import audiodevice as ad
import wave


current_file = Path(__file__).resolve()
engine_path = current_file.parent.parent / "audiodevice.exe"
ENGINE_EXE = str(engine_path)


def _trim_wav_inplace(path: Path, *, target_seconds: float, timeout_s: float = 5.0) -> None:
    """
    Make the wav exactly target_seconds long (sample-accurate) by trimming extra frames.
    If the file is shorter than target, leave it unchanged.
    """
    deadline = time.time() + float(timeout_s)
    last_err: Exception | None = None

    while time.time() < deadline:
        try:
            with wave.open(str(path), "rb") as r:
                ch = r.getnchannels()
                sw = r.getsampwidth()
                sr = r.getframerate()
                n = r.getnframes()
                if sr <= 0:
                    return
                target_frames = int(round(float(sr) * float(target_seconds)))
                if target_frames <= 0 or n <= target_frames:
                    return
                frames = r.readframes(target_frames)

            tmp = path.with_suffix(".tmp.wav")
            with wave.open(str(tmp), "wb") as w:
                w.setnchannels(int(ch))
                w.setsampwidth(int(sw))
                w.setframerate(int(sr))
                w.writeframes(frames)
            os.replace(str(tmp), str(path))
            return
        except Exception as e:
            last_err = e
            time.sleep(0.05)

    _ = last_err


def main() -> None:
    ad.default.auto_start = True
    if engine_path.is_file():
        ad.default.engine_exe = ENGINE_EXE
        ad.default.engine_cwd = os.path.dirname(ENGINE_EXE)

    rotate_s = 5

    out_dir = Path(__file__).resolve().parent
    start_dt = datetime.now()
    start_ts = start_dt.strftime("%Y%m%d_%H%M%S")
    wav_path = str(out_dir / f"{start_ts}.wav")

    try:
        h = ad.rec_long(wav_path, rotate_s=rotate_s)
        print(
            "started:",
            "hostapi=",
            getattr(ad.default, "hostapi_name", "") or ad.default.hostapi,
            "samplerate=",
            ad.default.samplerate,
            "channels=",
            ad.default.channels,
            "device=",
            ad.default.device,
        )
    except Exception as e:
        raise RuntimeError(
            f"failed to start long recording with defaults: "
            f"hostapi={getattr(ad.default, 'hostapi_name', '')!r}/{ad.default.hostapi!r}, "
            f"device={ad.default.device!r}, samplerate={ad.default.samplerate!r}, channels={ad.default.channels!r}, error={e!r}"
        ) from e

    base_path = Path(wav_path)
    base_stem = base_path.stem
    base_ext = base_path.suffix or ".wav"
    next_seg_idx = 1

    print("recording forever... (Ctrl+C to stop)")
    try:
        while True:
            while True:
                src = base_path.with_name(f"{base_stem}_{next_seg_idx:05}{base_ext}")
                # The engine typically creates the segment file immediately and keeps writing to it
                # until the next rotation. To avoid renaming/trimming a file that is still being
                # written, only process segment N once segment N+1 exists.
                next_src = base_path.with_name(f"{base_stem}_{next_seg_idx + 1:05}{base_ext}")
                if not src.exists() or not next_src.exists():
                    break

                seg_dt = start_dt + timedelta(seconds=float(rotate_s) * next_seg_idx)
                seg_ts = seg_dt.strftime("%Y%m%d_%H%M%S")
                dst = base_path.with_name(f"{seg_ts}{base_ext}")
                if dst.exists():
                    dst = base_path.with_name(f"{seg_ts}_{next_seg_idx:05}{base_ext}")

                try:
                    os.replace(src, dst)
                    # Engine rotation happens on I/O block boundaries, so some segments may exceed rotate_s slightly.
                    # Trim to exactly rotate_s seconds (sample-accurate) after the file is closed.
                    _trim_wav_inplace(dst, target_seconds=float(rotate_s))
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

