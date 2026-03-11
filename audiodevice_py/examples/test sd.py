import sounddevice as sd

# channels_in = 6
samplerate = 48000
duration = 1  # 秒

print(f"录音 {duration} 秒 ({samplerate}Hz) ...")
rec_buf = sd.rec(int(duration * samplerate), samplerate=samplerate, dtype='float32')
sd.wait()  # 阻塞直到录音结束
print("录音结束, shape:", rec_buf.shape)