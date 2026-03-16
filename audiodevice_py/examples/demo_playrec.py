import numpy as np
import audiodevice as ad
import os
import matplotlib.pyplot as plt


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

    ad.init()

    # Note: CPAL+ASIO works well for input-only (rec) or output-only (play),
    # but full-duplex playrec may produce zero input frames on some ASIO drivers.
    # WASAPI is the most compatible hostapi for duplex on Windows. hostapi is read-only; set device to change it.
    ad.default.samplerate = 44100
    ad.default.device = (15,17)
    ad.default.channels = (1,2)
 
    fs = ad.default.samplerate
    frames = int(fs * 5)
    t = np.arange(frames, dtype=np.float32) / fs
    # 输出通道映射（1-based）：把 y 的每一列路由到指定的设备输出通道（可用于交换左右声道等）。
    output_mapping = [1,2]
    n_out = int(len(output_mapping))
    freqs = 1000.0 + 200.0 * np.arange(n_out, dtype=np.float32)
    y = 0.1 * np.sin(2 * np.pi * t[:, None] * freqs[None, :]).astype(np.float32)
    if n_out == 1:
        y = y[:, 0]

    delay_ms = 34
    wav_path = os.path.join(os.path.dirname(__file__), "playrecdelay34ms.wav")
    input_mapping = [1]  # 1-based: only keep CH1 in returned recording
    # On some devices/drivers, the recorded WAV may contain a small tail.
    # Save an exact-length WAV by trimming/padding to the target frame count.
    x = ad.playrec(
        y,
        save_wav=True,
        blocking=True,
        delay_time=delay_ms,
        wav_path=wav_path,
        input_mapping=input_mapping,
        output_mapping=output_mapping,
    )
    x = np.asarray(x)
    if x.shape[0] < frames:
        pad = np.zeros((frames - x.shape[0],) + x.shape[1:], dtype=x.dtype)
        x = np.concatenate([x, pad], axis=0)
    x = x[:frames]
    write_wav_int16(wav_path, fs, x)
    print("captured:", x.shape, x.dtype, "saved:", wav_path)


if __name__ == "__main__":
    main()

