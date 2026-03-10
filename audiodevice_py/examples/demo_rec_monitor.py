import os

import audiodevice as ad

from pathlib import Path
from typing import Optional

current_file = Path(__file__).resolve()
engine_path = current_file.parent.parent / "audiodevice.exe"
ENGINE_EXE = str(engine_path)


def _pick_device(devs: list[dict], prefer: list[str]) -> str:
    for p in prefer:
        p_lc = p.lower()
        for d in devs:
            name = str(d.get("name", ""))
            if p_lc in name.lower():
                return name
    return ""


def main() -> None:
    ad.default.auto_start = True
    if engine_path.is_file():
        ad.default.engine_exe = ENGINE_EXE
        ad.default.engine_cwd = os.path.dirname(ENGINE_EXE)

    wav_path = os.path.join(os.path.dirname(__file__), "rec_monitor.wav")

    last_err: Optional[Exception] = None
    for hostapi in ("ASIO", "WASAPI"):
        ad.default.hostapi = hostapi
        print("\ntrying hostapi:", hostapi)

        dev_in = ad.query_devices_raw(hostapi=hostapi, direction="input")["devices"]
        dev_out = ad.query_devices_raw(hostapi=hostapi, direction="output")["devices"]

        ad.default.device_in = _pick_device(
            dev_in,
            prefer=["microphone", "mic", "umc", "usb"],
        )
        ad.default.device_out = _pick_device(
            dev_out,
            prefer=["speaker", "speakers", "headphone", "headphones", "umc", "usb"],
        )

        # For ASIO, monitor/duplex typically expects the same device.
        if hostapi.upper() == "ASIO" and ad.default.device_in:
            ad.default.device_out = ad.default.device_in

        print("device_in:", ad.default.device_in or "<default>")
        print("device_out:", ad.default.device_out or "<default>")

        try:
            x = ad.rec_monitor(
                10.0,  # seconds
                save_wav=True,
                wav_path=wav_path,
                blocking=True,
                samplerate=48_000,
                channels=2,
            )
            print("captured:", x.shape, x.dtype, "wav:", wav_path)
            last_err = None
            break
        except Exception as e:
            last_err = e
            print("failed on hostapi:", hostapi, "err:", e)

    if last_err is not None:
        raise last_err


if __name__ == "__main__":
    main()

