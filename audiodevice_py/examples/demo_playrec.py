import numpy as np
import audiodevice as ad
import os

from pathlib import Path
import wave

current_file = Path(__file__).resolve()
engine_path = current_file.parent.parent / "audiodevice.exe"
ENGINE_EXE = str(engine_path)

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
    ad.default.auto_start = True
    if engine_path.is_file():
        ad.default.engine_exe = ENGINE_EXE
        ad.default.engine_cwd = os.path.dirname(ENGINE_EXE)

    # Note: CPAL+ASIO works well for input-only (rec) or output-only (play),
    # but full-duplex playrec may produce zero input frames on some ASIO drivers.
    # WASAPI is the most compatible hostapi for duplex on Windows.
    ad.default.hostapi = "WASAPI"
    ad.default.samplerate = 48_000
    ad.default.channels = 2

    fs = ad.default.samplerate
    frames = int(fs * 5)
    t = np.arange(frames, dtype=np.float32) / fs
    y = 0.1 * np.sin(2 * np.pi * 1000* t).astype(np.float32)
    y = np.stack([y, y], axis=1)  # (frames, channels)

    wav_path = os.path.join(os.path.dirname(__file__), "playrec.wav")
    # On some devices/drivers, the recorded WAV may contain a small tail.
    # Save an exact-length WAV by trimming/padding to the target frame count.
    x = ad.playrec(y, save_wav=False, blocking=True)
    x = np.asarray(x)
    if x.shape[0] < frames:
        pad = np.zeros((frames - x.shape[0],) + x.shape[1:], dtype=x.dtype)
        x = np.concatenate([x, pad], axis=0)
    x = x[:frames]
    write_wav_int16(wav_path, fs, x)
    print("captured:", x.shape, x.dtype, "saved:", wav_path)


if __name__ == "__main__":
    main()

