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
RB_SECONDS = 8
DEVICE = (10, 12)  # (device_in, device_out)
DEFAULT_CHANNELS_NUM = (6, 2)  # (in_ch, out_ch)
DURATION_MS = 3000
TARGET_FRAMES = int(round(SAMPLERATE * (DURATION_MS / 1000.0)))
DELAY_MS = 0
INPUT_MAPPING = [1, 3, 5]  # 1-based: pick these input channels
CHANNELS = 6  # must be >= max(INPUT_MAPPING)
SAVE_CHANNELS = len(INPUT_MAPPING)

# More stable defaults for stream demos
ad.default.samplerate = SAMPLERATE
ad.default.device = DEVICE
ad.default.channels = DEFAULT_CHANNELS_NUM
ad.default.rb_seconds = RB_SECONDS


def save_wav(path: Path, data_f32: np.ndarray, samplerate: int, channels: int) -> None:
    pcm = np.clip(data_f32, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(int(channels))
        wav.setsampwidth(2)
        wav.setframerate(int(samplerate))
        wav.writeframes(pcm.tobytes())


chunks = []
frames_captured = [0]


def callback(indata, outdata, frames, time_info, status):
    # 复制一份保存（indata 可能在后续回调中被复用）
    remain = TARGET_FRAMES - frames_captured[0]
    if remain <= 0:
        raise ad.CallbackStop()

    # 只取需要的帧数，保证最终 WAV 时长精确等于 DURATION_MS（按采样率换算）
    take = int(frames) if int(frames) < int(remain) else int(remain)
    if take > 0:
        chunks.append(indata[:take].copy())
        frames_captured[0] += int(take)

    if frames_captured[0] >= TARGET_FRAMES:
        raise ad.CallbackStop()


out_path = Path(__file__).resolve().parent / "demo_stream_input_recording.wav"
print(f"录音 {DURATION_MS}ms (delay={DELAY_MS}ms) -> {out_path.name} ...")

stream = ad.InputStream(
    callback=callback,
    channels=CHANNELS,
    samplerate=SAMPLERATE,
    blocksize=BLOCKSIZE,
    delay_time=int(DELAY_MS),
    mapping=INPUT_MAPPING
)
stream.start()
# 注意：sleep 只是“等待”，实际精确停止由 callback 中的 TARGET_FRAMES 控制
ad.sleep(DURATION_MS + DELAY_MS + 500)
stream.close()

if chunks:
    data = np.concatenate(chunks, axis=0)
    # 双保险：截断到目标帧数，避免任何边界条件导致略长
    if data.shape[0] > TARGET_FRAMES:
        data = data[:TARGET_FRAMES]
    save_wav(out_path, data, SAMPLERATE, SAVE_CHANNELS)
    dur_s = data.shape[0] / SAMPLERATE
    print(f"完成，保存 WAV: {out_path}  (frames={data.shape[0]}, duration={dur_s:.3f}s)")
else:
    print("没有录到数据（可尝试切换 HostAPI/设备）。")

