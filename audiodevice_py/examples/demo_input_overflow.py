import time

import audiodevice as ad

samplerate = 48000
blocksize = 1024


def callback(indata, frames, time_info, status):
    if status:
        print("Status:", status)

    # ❌ 故意让 callback 变慢（模拟溢出）
    time.sleep(0.1)  # 50ms > 21ms（block 时长）


ad.init(timeout=10)

with ad.InputStream(
    samplerate=samplerate,
    blocksize=blocksize,
    channels=1,
    callback=callback,
):
    print("Running overflow test...")
    ad.sleep(10000)

