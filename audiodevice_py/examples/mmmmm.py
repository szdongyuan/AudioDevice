import audiodevice as ad
import numpy as np

ad.init()

ad.default.device = (14, 18)
ad.default.samplerate = 48000
ad.default.channels = [6,2]
wav_path = "./111.wav"

y = ad.rec(48000*3, wav_path=wav_path, save_wav=True, blocking=True, channels=6)

print(y.shape)

print(y)

ad.play(y, blocking=True, samplerate=48000)