"""
demo_stream_moniter.py - 最简 Stream(全双工) 示例：麦克风直通到扬声器（注意可能啸叫）
"""
from pathlib import Path
import wave

import numpy as np

import audiodevice as ad
import time

# 初始化引擎
_root = Path(__file__).resolve().parent.parent
_engine = _root / "audiodevice.exe"
if _engine.is_file():
    ad.init(engine_exe=str(_engine), engine_cwd=str(_root), timeout=10)
else:
    ad.init(timeout=10)
ad.print_default_devices()

SAMPLERATE = 44100
BLOCKSIZE = 1024
RB_FRAMES = 4096
DEVICE = (10, 12)   # (device_in, device_out)
DURATION_MS = 10000
TARGET_FRAMES = int(round(SAMPLERATE * (DURATION_MS / 1000.0)))
# 选择要监听的输入通道（1-based）：1=CH1, 2=CH2...
MONITOR_CH = 1
INPUT_MAPPING = [int(MONITOR_CH)]  # 1-based: callback/input WAV keep only this input channel
# 按 callback 输出列控制播放到哪些设备声道：1=left, 2=right。
# 例如 [1] 只播左声道，[2] 只播右声道，[1, 2] 左右都播。
OUTPUT_MAPPING = [2]
INPUT_CHANNELS = len(INPUT_MAPPING)
OUTPUT_CHANNELS = len(OUTPUT_MAPPING)
SAVE_CH = len(INPUT_MAPPING)

# More stable defaults for stream demos
ad.default.samplerate = SAMPLERATE
ad.default.device = DEVICE


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
    remain = TARGET_FRAMES - frames_captured[0]
    if remain <= 0:
        raise ad.CallbackStop()

    take = int(frames) if int(frames) < int(remain) else int(remain)

    # 录音：保存 mapping 后的输入列（通常就是监听的那一路）
    if take > 0 and indata.shape[1] > 0:
        ch = int(min(int(SAVE_CH), int(indata.shape[1])))
        if ch > 0:
            chunks.append(indata[:take, :ch].copy())

    if outdata.shape[1] > 0:
        outdata.fill(0.0)
        if take > 0 and indata.shape[1] > 0:
            mono = indata[:take, 0]
            outdata[:take, :] = mono[:, None]

    frames_captured[0] += int(take)
    if frames_captured[0] >= TARGET_FRAMES:
        raise ad.CallbackStop()


out_path = Path(__file__).resolve().parent / "demo_stream_moniter_recording.wav"
print(f"全双工直通并录音 {DURATION_MS}ms -> {out_path.name} （注意可能啸叫，建议先把音量调小）...")
stream = ad.Stream(
    callback=callback,
    channels=(INPUT_CHANNELS, OUTPUT_CHANNELS),
    samplerate=SAMPLERATE,
    blocksize=BLOCKSIZE,
    rb_frames=RB_FRAMES,
    mapping=INPUT_MAPPING,
    output_mapping=OUTPUT_MAPPING,
)
stream.start()
# 注意：回调的处理速度可能慢于实时（TCP 往返/调度等），所以不要用固定 sleep 来估算结束时刻。
# 这里以回调累计的帧数为准等待结束，并设置一个宽松超时避免意外卡死。
deadline = time.time() + (DURATION_MS / 1000.0) * 10.0 + 5.0
try:
    while frames_captured[0] < TARGET_FRAMES and time.time() < deadline:
        ad.sleep(50)
finally:
    stream.close()

if chunks:
    data = np.concatenate(chunks, axis=0)
    if data.shape[0] > TARGET_FRAMES:
        data = data[:TARGET_FRAMES]
    save_wav(out_path, data, SAMPLERATE, int(data.shape[1] if data.ndim == 2 else SAVE_CH))
    dur_s = data.shape[0] / SAMPLERATE
    print(f"完成，保存 WAV: {out_path}  (frames={data.shape[0]}, duration={dur_s:.3f}s, channels={data.shape[1]})")
else:
    print("完成，但没有录到数据（可尝试切换 HostAPI/设备）。")

