import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import audiodevice as ad
import numpy as np
import wave


_root = Path(__file__).resolve().parent.parent
_engine = _root / "audiodevice.exe"
if _engine.is_file():
    ad.init(engine_exe=str(_engine), engine_cwd=str(_root), timeout=10)
else:
    ad.init(timeout=10)
ad.print_default_devices()

SAMPLERATE = 48_000
ROTATE_S = 5
DEVICE = (10, 12)  # (device_in, device_out)
DEFAULT_CHANNELS_NUM = (6, 2)  # (in_ch, out_ch)
INPUT_MAPPING = [1,3,5]  # 1-based


def _wav_keep_channels_atomic(path: Path, *, mapping_1based: list[int], block_frames: int = 65536) -> None:
    """
    Rewrite an int16 PCM WAV keeping only selected channels (1-based mapping).
    Uses atomic replace (write to .tmp then os.replace).
    """
    if not mapping_1based:
        return
    cols = [int(m) - 1 for m in mapping_1based]
    if any(ci < 0 for ci in cols):
        raise ValueError(f"mapping must be 1-based >=1, got: {mapping_1based!r}")

    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with wave.open(str(path), "rb") as r:
            ch = int(r.getnchannels())
            sw = int(r.getsampwidth())
            sr = int(r.getframerate())
            nframes = int(r.getnframes())
            if ch <= 0 or sw <= 0 or sr <= 0:
                return
            if sw != 2:
                raise ValueError(f"only 16-bit PCM wav supported (sampwidth={sw})")
            if any(ci >= ch for ci in cols):
                raise ValueError(f"mapping out of range: file has {ch} channels, mapping={mapping_1based!r}")

            with wave.open(str(tmp), "wb") as w:
                w.setnchannels(int(len(cols)))
                w.setsampwidth(2)
                w.setframerate(int(sr))

                frames_left = nframes
                while frames_left > 0:
                    n = int(min(int(block_frames), frames_left))
                    frames = r.readframes(n)
                    if not frames:
                        break
                    pcm = np.frombuffer(frames, dtype="<i2")
                    if pcm.size % ch != 0:
                        pcm = pcm[: (pcm.size // ch) * ch]
                    if pcm.size == 0:
                        break
                    pcm = pcm.reshape(-1, ch)
                    pcm2 = pcm[:, cols]
                    w.writeframes(pcm2.astype("<i2", copy=False).tobytes(order="C"))
                    frames_left -= int(pcm.shape[0])

        os.replace(str(tmp), str(path))
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass


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
    out_dir = Path(__file__).resolve().parent
    start_dt = datetime.now()
    start_ts = start_dt.strftime("%Y%m%d_%H%M%S")
    wav_path = str(out_dir / f"{start_ts}.wav")

    ad.default.samplerate = SAMPLERATE
    ad.default.device = DEVICE

    try:
        # NOTE:
        # - rec_long(mapping=...) post-processes segments in a background thread and expects the
        #   original rotated filenames to remain intact.
        # - This demo renames/trim segments, so we do channel mapping here (before rename) instead.
        h = ad.rec_long(wav_path, rotate_s=int(ROTATE_S), channels=int(max(INPUT_MAPPING)))
        print(
            "started:",
            "hostapi=",
            getattr(ad.default, "hostapi_name", "") or ad.default.hostapi,
            "samplerate=",
            ad.default.samplerate,
            "channels=",
            int(max(INPUT_MAPPING)),
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
    next_seg_idx = 0

    print("recording forever... (Ctrl+C to stop)")
    try:
        while True:
            while True:
                if int(next_seg_idx) <= 0:
                    src = base_path
                    next_src = base_path.with_name(f"{base_stem}_{1:05}{base_ext}")
                else:
                    src = base_path.with_name(f"{base_stem}_{next_seg_idx:05}{base_ext}")
                    next_src = base_path.with_name(f"{base_stem}_{next_seg_idx + 1:05}{base_ext}")
                # The engine typically creates the segment file immediately and keeps writing to it
                # until the next rotation. To avoid renaming/trimming a file that is still being
                # written, only process segment N once segment N+1 exists.
                if not src.exists() or not next_src.exists():
                    break

                seg_dt = start_dt + timedelta(seconds=float(ROTATE_S) * float(next_seg_idx))
                seg_ts = seg_dt.strftime("%Y%m%d_%H%M%S")
                dst = base_path.with_name(f"{seg_ts}{base_ext}")
                if dst.exists():
                    dst = base_path.with_name(f"{seg_ts}_{next_seg_idx:05}{base_ext}")

                try:
                    _wav_keep_channels_atomic(src, mapping_1based=INPUT_MAPPING)
                    if src.resolve() != dst.resolve():
                        os.replace(src, dst)
                        out_path = dst
                    else:
                        out_path = src
                    # Engine rotation happens on I/O block boundaries, so some segments may exceed rotate_s slightly.
                    # Trim to exactly rotate_s seconds (sample-accurate) after the file is closed.
                    _trim_wav_inplace(out_path, target_seconds=float(ROTATE_S))
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
        # Best-effort: map/trim the last open segment after stop.
        try:
            if int(next_seg_idx) <= 0:
                last = base_path
            else:
                last = base_path.with_name(f"{base_stem}_{next_seg_idx:05}{base_ext}")
            if last.exists():
                _wav_keep_channels_atomic(last, mapping_1based=INPUT_MAPPING)
        except Exception:
            pass
        print("done:", wav_path)


if __name__ == "__main__":
    main()

