import numpy as np
import audiodevice as ad
import os


from pathlib import Path
import wave

_root = Path(__file__).resolve().parent.parent
_engine = _root / "audiodevice.exe"
if _engine.is_file():
    ad.init(engine_exe=str(_engine), engine_cwd=str(_root), timeout=10)
else:
    ad.init(timeout=10)
ad.print_default_devices()

SAMPLERATE = 48000
DURATION_S = 5.0
DELAY_MS = 34
DEVICE = (10, 12)  # (device_in, device_out)
DEFAULT_CHANNELS_NUM = (6, 2)  # (in_ch, out_ch)
OUTPUT_MAPPING = [1]  # 1-based
INPUT_MAPPING = [1, 3, 5]  # 1-based: keep these input channels in returned recording
WAV_PATH = os.path.join(os.path.dirname(__file__), "playrecdelay34ms.wav")

ad.default.samplerate = SAMPLERATE
ad.default.device = DEVICE
ad.default.channels = DEFAULT_CHANNELS_NUM

def write_wav_int16(path: str, samplerate: int, data: np.ndarray) -> None:
    x = np.asarray(data)
    if x.ndim == 1:
        x = x[:, None]
    x = np.clip(x, -1.0, 1.0)
    pcm = (x * 32767.0).astype("<i2", copy=False)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(int(pcm.shape[1]))
        wf.setsampwidth(2)  # int16
        wf.setframerate(int(samplerate))
        wf.writeframes(pcm.tobytes(order="C"))

def main() -> None:
    fs = int(SAMPLERATE)
    frames = int(round(float(fs) * float(DURATION_S)))
    t = np.arange(frames, dtype=np.float32) / float(fs)
    n_out = int(len(OUTPUT_MAPPING))
    freqs = 1000.0 + 200.0 * np.arange(n_out, dtype=np.float32)
    y = 0.1 * np.sin(2 * np.pi * t[:, None] * freqs[None, :]).astype(np.float32)
    if n_out == 1:
        y = y[:, 0]

    # On some devices/drivers, the recorded WAV may contain a small tail.
    # Save an exact-length WAV by trimming/padding to the target frame count.
    x = ad.playrec(
        y,
        save_wav=True,
        blocking=True,
        delay_time=DELAY_MS,
        wav_path=WAV_PATH,
        input_mapping=INPUT_MAPPING,
        output_mapping=OUTPUT_MAPPING,
    )
    x = np.asarray(x)
    if x.shape[0] < frames:
        pad = np.zeros((frames - x.shape[0],) + x.shape[1:], dtype=x.dtype)
        x = np.concatenate([x, pad], axis=0)
    x = x[:frames]
    write_wav_int16(WAV_PATH, fs, x)
    print("captured:", x.shape, x.dtype, "saved:", WAV_PATH)


if __name__ == "__main__":
    main()

