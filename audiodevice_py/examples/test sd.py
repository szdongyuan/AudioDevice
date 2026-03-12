import numpy as np
import sounddevice as sd

samplerate = 48000
duration = 5  # 秒
channels = 2  # 双声道输出，可改为1或其他

# 生成一段空白音频，全零
blank_audio = np.zeros((int(duration * samplerate), channels), dtype='float32')

print(f"播放 {duration} 秒空白音频 ({samplerate}Hz) ...")
sd.play(blank_audio, samplerate=samplerate)
sd.wait()  # 阻塞直到播放结束
print("播放结束, shape:", blank_audio.shape)