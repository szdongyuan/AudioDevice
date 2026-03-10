import numpy as np
import audiodevice as ad
import os

from pathlib import Path

current_file = Path(__file__).resolve()
engine_path = current_file.parent.parent / "audiodevice.exe"
ENGINE_EXE = str(engine_path)

def main() -> None:
    ad.default.auto_start = True
    if engine_path.is_file():
        ad.default.engine_exe = ENGINE_EXE
        ad.default.engine_cwd = os.path.dirname(ENGINE_EXE)

    # Note: CPAL+ASIO works well for input-only (rec) or output-only (play),
    # but full-duplex playrec may produce zero input frames on some ASIO drivers.
    # WASAPI is the most compatible hostapi for duplex on Windows.
    ad.default.hostapi = "ASIO"
    ad.default.samplerate = 48_000
    ad.default.channels = 2
    # ad.default.device_in = "ASIO4ALL v2"
    # ad.default.device_out = "ASIO4ALL v2"
    # Leave device names empty to use system defaults (recommended for portability).
    ad.default.device_in = "UMC ASIO Driver"
    ad.default.device_out = "UMC ASIO Driver"

    fs = ad.default.samplerate
    t = np.arange(fs*5, dtype=np.float32) / fs
    y = 0.1 * np.sin(2 * np.pi * 1000* t).astype(np.float32)
    y = np.stack([y, y], axis=1)  # (frames, channels)

    wav_path = os.path.join(os.path.dirname(__file__), "playrec.wav")
    x = ad.playrec(y, wav_path=wav_path, save_wav=True, blocking=True)
    print("captured:", x.shape, x.dtype)


if __name__ == "__main__":
    main()

