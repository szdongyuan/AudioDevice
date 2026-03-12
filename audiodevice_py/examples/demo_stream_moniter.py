"""
demo_stream_duplex.py - 最简 Stream(全双工) 示例：麦克风直通到扬声器 1.5 秒
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
    ad.init(timeout=10)
ad.print_default_devices()

SAMPLERATE = 48_000
BLOCKSIZE = 1024
IN_CH = 6
OUT_CH = 2
DURATION_MS = 3000
TARGET_FRAMES = int(round(SAMPLERATE * (DURATION_MS / 1000.0)))
SAVE_CH = int(OUT_CH)

# 对 Stream demo 更稳一些（避免调度抖动导致的缓冲问题）
ad.default.samplerate = SAMPLERATE
ad.default.rb_seconds = 8
ad.default.device = (14,18)
print(ad.default.device)


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

    # 录音：保存输入的前 SAVE_CH 个通道（通常等于 OUT_CH，方便对齐监听的声道数）
    if take > 0 and indata.shape[1] > 0:
        ch = int(min(int(SAVE_CH), int(indata.shape[1])))
        if ch > 0:
            chunks.append(indata[:take, :ch].copy())

    if outdata.shape[1] > 0 and indata.shape[1] > 0:
        n = min(outdata.shape[1], indata.shape[1])
        if take > 0:
            outdata[:take, :n] = indata[:take, :n]
        if int(frames) > int(take):
            outdata[take:, :n] = 0
        if outdata.shape[1] > n:
            outdata[:, n:] = 0
    elif outdata.shape[1] > 0:
        outdata[:] = 0

    frames_captured[0] += int(take)
    if frames_captured[0] >= TARGET_FRAMES:
        raise ad.CallbackStop()


out_path = Path(__file__).resolve().parent / "demo_stream_moniter_recording.wav"
print(f"全双工直通并录音 {DURATION_MS}ms -> {out_path.name} （注意可能啸叫，建议先把音量调小）...")
stream = ad.Stream(
    callback=callback,
    channels=(IN_CH, OUT_CH),
    samplerate=SAMPLERATE,
    blocksize=BLOCKSIZE,
)
stream.start()
# 注意：sleep 只是“等待”，实际精确停止由 callback 中的 TARGET_FRAMES 控制
ad.sleep(DURATION_MS + 500)
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

