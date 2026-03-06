from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple, Union


class _InputOutputPair(list):
    # Mimic audiodevice._InputOutputPair repr/shape (prints like [in, out]).
    def __init__(self, input_value=None, output_value=None) -> None:
        super().__init__([input_value, output_value])

    @property
    def input(self):
        return self[0]

    @property
    def output(self):
        return self[1]


@dataclass
class DefaultConfig:
    host: str = "127.0.0.1"
    port: int = 18789

    # audiodevice-compatible default properties:
    # - default.hostapi is an index (int)
    # - default.samplerate is Optional[float] (None means "unspecified")
    # - default.channels is a pair (in, out), each Optional[int]
    hostapi_name: str = "MME"
    hostapi_index: int = 0
    samplerate: Optional[float] = None
    channels: Tuple[Optional[int], Optional[int]] = (None, None)
    # audiodevice default is ['float32', 'float32']
    dtype: Tuple[Optional[str], Optional[str]] = ("float32", "float32")
    latency: Tuple[Optional[Union[str, float]], Optional[Union[str, float]]] = (None, None)

    # audiodevice-compatible device indices (global indices returned by ad.query_devices()).
    # -1 means "unspecified".
    device: Tuple[int, int] = (-1, -1)

    # Non-audiodevice extras (kept for convenience/back-compat):
    device_in: str = ""
    device_out: str = ""

    rb_seconds: int = 2

    auto_start: bool = False
    engine_exe: str = "audiodevice.exe"
    engine_cwd: Optional[str] = None
    startup_timeout: float = 3.0

    # Optional: auto-download engine artifacts on first use when engine_exe isn't found.
    # If empty, SDK will only try bundled engine (wheel) or PATH / common dev locations.
    engine_download_url: str = ""
    engine_sha256: str = ""

    timeout: float = 5.0


class _DefaultHolder:
    def __init__(self) -> None:
        super().__setattr__("_cfg", DefaultConfig())
        super().__setattr__("_resolving_device", False)

    def _device_tuple_raw(self) -> Tuple[int, int]:
        d = getattr(self._cfg, "device", (-1, -1))
        if isinstance(d, tuple) and len(d) == 2:
            return int(d[0]), int(d[1])
        return -1, -1

    @property
    def hostapi(self) -> int:
        return int(getattr(self._cfg, "hostapi_index", 0) or 0)

    @hostapi.setter
    def hostapi(self, value) -> None:
        if value is None:
            self._cfg.hostapi_index = 0
            # Keep name as-is; it will be resolved lazily when possible.
            return
        if isinstance(value, int):
            self._cfg.hostapi_index = int(value)
            try:
                from .api import query_hostapis

                h = query_hostapis(int(value))
                self._cfg.hostapi_name = str(h.get("name", self._cfg.hostapi_name))
            except Exception:
                pass
            return
        if isinstance(value, str):
            name = value.strip()
            if not name:
                raise ValueError("default.hostapi name must be non-empty")
            self._cfg.hostapi_name = name
            try:
                from .api import query_hostapis

                hs = query_hostapis()
                want = name.strip().lower()
                for i, h in enumerate(hs):
                    if str(h.get("name", "")).strip().lower() == want:
                        self._cfg.hostapi_index = int(i)
                        break
            except Exception:
                pass
            return
        raise TypeError("default.hostapi must be int, str, or None")

    @property
    def channels(self) -> _InputOutputPair:
        ch = getattr(self._cfg, "channels", (None, None))
        if not isinstance(ch, tuple) or len(ch) != 2:
            ch = (None, None)
        return _InputOutputPair(ch[0], ch[1])

    @channels.setter
    def channels(self, value) -> None:
        if value is None:
            self._cfg.channels = (None, None)
            return
        if isinstance(value, int):
            self._cfg.channels = (int(value), int(value))
            return
        if isinstance(value, (list, tuple)) and len(value) == 2:
            a = None if value[0] is None else int(value[0])
            b = None if value[1] is None else int(value[1])
            self._cfg.channels = (a, b)
            return
        raise TypeError("default.channels must be int, (in, out), or None")

    @property
    def samplerate(self) -> Optional[float]:
        sr = getattr(self._cfg, "samplerate", None)
        return None if sr is None else float(sr)

    @samplerate.setter
    def samplerate(self, value) -> None:
        if value is None:
            self._cfg.samplerate = None
            return
        self._cfg.samplerate = float(value)

    @property
    def dtype(self) -> _InputOutputPair:
        dt = getattr(self._cfg, "dtype", (None, None))
        if not isinstance(dt, tuple) or len(dt) != 2:
            dt = (None, None)
        return _InputOutputPair(dt[0], dt[1])

    @dtype.setter
    def dtype(self, value) -> None:
        if value is None:
            self._cfg.dtype = (None, None)
            return
        if isinstance(value, str):
            v = value.strip() or None
            self._cfg.dtype = (v, v)
            return
        if isinstance(value, (list, tuple)) and len(value) == 2:
            a = None if value[0] is None else str(value[0]).strip() or None
            b = None if value[1] is None else str(value[1]).strip() or None
            self._cfg.dtype = (a, b)
            return
        raise TypeError("default.dtype must be str, (in, out), or None")

    @property
    def latency(self) -> _InputOutputPair:
        lat = getattr(self._cfg, "latency", (None, None))
        if not isinstance(lat, tuple) or len(lat) != 2:
            lat = (None, None)
        return _InputOutputPair(lat[0], lat[1])

    @latency.setter
    def latency(self, value) -> None:
        if value is None:
            self._cfg.latency = (None, None)
            return
        if isinstance(value, str):
            v = value.strip() or None
            self._cfg.latency = (v, v)
            return
        if isinstance(value, (int, float)):
            self._cfg.latency = (float(value), float(value))
            return
        if isinstance(value, (list, tuple)) and len(value) == 2:
            a = value[0]
            b = value[1]
            a2 = None if a is None else (float(a) if isinstance(a, (int, float)) else str(a).strip() or None)
            b2 = None if b is None else (float(b) if isinstance(b, (int, float)) else str(b).strip() or None)
            self._cfg.latency = (a2, b2)
            return
        raise TypeError("default.latency must be str, float, (in, out), or None")

    @property
    def device(self) -> _InputOutputPair:
        # Lazy-initialize to something meaningful on first access, similar to sd.default.device.
        if getattr(self, "_resolving_device", False):
            di, do = self._device_tuple_raw()
            return _InputOutputPair(int(di), int(do))

        if (
            isinstance(self._cfg.device, tuple)
            and len(self._cfg.device) == 2
            and int(self._cfg.device[0]) == -1
            and int(self._cfg.device[1]) == -1
            and not str(self._cfg.device_in or "")
            and not str(self._cfg.device_out or "")
        ):
            try:
                super().__setattr__("_resolving_device", True)
                # Local import to avoid circular import at module load.
                from .api import query_hostapis

                hs = query_hostapis()
                want = str(self._cfg.hostapi_name or "").strip().lower()
                picked = None
                for h in hs:
                    if str(h.get("name", "")).strip().lower() == want:
                        picked = h
                        break
                if picked is None and hs:
                    picked = hs[0]
                if picked is not None:
                    di = int(picked.get("default_input_device", -1))
                    do = int(picked.get("default_output_device", -1))
                    self._cfg.device = (di, do)
            except Exception:
                # Keep (-1, -1) if we can't determine defaults.
                pass
            finally:
                super().__setattr__("_resolving_device", False)

        di, do = self._cfg.device if isinstance(self._cfg.device, tuple) and len(self._cfg.device) == 2 else (-1, -1)
        return _InputOutputPair(int(di), int(do))

    @device.setter
    def device(self, value: Union[int, Sequence[int], None]) -> None:
        if isinstance(value, str):
            name = value.strip()
            if not name:
                raise ValueError("default.device name must be non-empty")
            # Local import to avoid circular import at module load.
            from .api import query_devices

            d = query_devices(name)
            if not isinstance(d, dict) or "index" not in d:
                raise ValueError(f"device not found: {value}")
            di = int(d["index"])
            self._cfg.device = (di, di)
            self._cfg.device_in = ""
            self._cfg.device_out = ""
            return
        if value is None:
            self._cfg.device = (-1, -1)
            self._cfg.device_in = ""
            self._cfg.device_out = ""
            return
        if isinstance(value, int):
            di = int(value)
            self._cfg.device = (di, di)
            self._cfg.device_in = ""
            self._cfg.device_out = ""
            return
        if isinstance(value, (list, tuple)) and len(value) == 2:
            di = int(value[0])
            do = int(value[1])
            self._cfg.device = (di, do)
            self._cfg.device_in = ""
            self._cfg.device_out = ""
            return
        raise TypeError("default.device must be int, (in, out), or None")

    def reset(self) -> None:
        """Reset defaults to audiodevice-like defaults."""
        self._cfg.hostapi_name = "MME"
        self._cfg.hostapi_index = 0
        self._cfg.samplerate = None
        self._cfg.channels = (None, None)
        self._cfg.dtype = ("float32", "float32")
        self._cfg.latency = (None, None)
        self._cfg.device = (-1, -1)
        self._cfg.device_in = ""
        self._cfg.device_out = ""

    def __getattr__(self, name: str):
        return getattr(self._cfg, name)

    def __setattr__(self, name: str, value) -> None:
        if name == "_cfg":
            super().__setattr__(name, value)
            return
        if name == "_resolving_device":
            super().__setattr__(name, bool(value))
            return
        if name == "device":
            type(self).device.fset(self, value)
            return
        if name == "hostapi":
            type(self).hostapi.fset(self, value)
            return
        if name == "channels":
            type(self).channels.fset(self, value)
            return
        if name == "samplerate":
            type(self).samplerate.fset(self, value)
            return
        if name == "dtype":
            type(self).dtype.fset(self, value)
            return
        if name == "latency":
            type(self).latency.fset(self, value)
            return
        setattr(self._cfg, name, value)

    def as_dict(self) -> dict:
        return self._cfg.__dict__.copy()


default = _DefaultHolder()

