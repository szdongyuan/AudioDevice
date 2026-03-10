"""
demo_stream_input.py - 最简 InputStream 示例：录音 1.5 秒并保存为 WAV
"""
from pathlib import Path
import wave

import numpy as np

import audiodevice as ad

# 初始化引擎
_root = Path(__file__).resolve().parent.parent
_engine = _root / "audiodevice.exe"
if _engine.is_file():
    ad.init(engine_exe=str(_engine), engine_cwd=str(_root), timeout=10)
else:
    # Fallback: use PATH / bundled wheel assets / repo build / auto-download (AUDIODEVICE_ENGINE_URL)
    ad.init(timeout=10)
ad.print_default_devices()

SAMPLERATE = 48_000
BLOCKSIZE = 1024
CHANNELS = 1
DURATION_MS = 3000

# 对 Stream demo 更稳一些（避免调度抖动导致的缓冲问题）
ad.default.samplerate = SAMPLERATE
ad.default.rb_seconds = 8


def save_wav(path: Path, data_f32: np.ndarray, samplerate: int, channels: int) -> None:
    pcm = np.clip(data_f32, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(int(channels))
        wav.setsampwidth(2)
        wav.setframerate(int(samplerate))
        wav.writeframes(pcm.tobytes())


chunks = []
n_blocks = [0]


def callback(indata, outdata, frames, time_info, status):
    n_blocks[0] += 1
    # 复制一份保存（indata 可能在后续回调中被复用）
    chunks.append(indata.copy())


out_path = Path(__file__).resolve().parent / "demo_stream_input_recording.wav"
print(f"录音 {DURATION_MS}ms -> {out_path.name} ...")

stream = ad.InputStream(
    callback=callback,
    channels=CHANNELS,
    samplerate=SAMPLERATE,
    blocksize=BLOCKSIZE,
)
stream.start()
ad.sleep(DURATION_MS)
stream.close()

if chunks:
    data = np.concatenate(chunks, axis=0)
    save_wav(out_path, data, SAMPLERATE, CHANNELS)
    print(f"完成，保存 WAV: {out_path}")
else:
    print("没有录到数据（可尝试切换 HostAPI/设备）。")

