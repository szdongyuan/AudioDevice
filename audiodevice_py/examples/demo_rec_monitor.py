import os

import audiodevice as ad

from pathlib import Path

_root = Path(__file__).resolve().parent.parent
_engine = _root / "audiodevice.exe"
if _engine.is_file():
    ad.init(engine_exe=str(_engine), engine_cwd=str(_root), timeout=10)
else:
    ad.init(timeout=10)
ad.print_default_devices()

SAMPLERATE = 48000
DURATION_S = 10.0
MONITOR_CH = 3  # 1-based
OUTPUT_MAPPING = [1]  # 1-based
WAV_PATH = os.path.join(os.path.dirname(__file__), "rec_monitor.wav")
DEVICE = (10, 12)  # (device_in, device_out)
DEFAULT_CHANNELS_NUM = (6, 2)  # (in_ch, out_ch)

ad.default.samplerate = SAMPLERATE
ad.default.device = DEVICE
ad.default.channels = DEFAULT_CHANNELS_NUM


def main() -> None:
    IN_CH = int(ad.default.channels.input or 1)

    print("hostapi:", ad.default.hostapi)
    print("device_in:", ad.default.device_in)
    print("device_out:", ad.default.device_out)
    print("device:", ad.default.device)

    x = ad.rec_monitor(
        DURATION_S,
        save_wav=True,
        wav_path=WAV_PATH,
        blocking=True,
        monitor_channel=MONITOR_CH,
        output_mapping=OUTPUT_MAPPING,
        samplerate=SAMPLERATE,
        channels=IN_CH,
    )
    print("captured:", x.shape, x.dtype, "wav:", WAV_PATH)

if __name__ == "__main__":
    main()

