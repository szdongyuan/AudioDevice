from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple, Union


class _InputOutputPair(list):
    # Mimic audiodevice._InputOutputPair repr/shape (prints like [in, out]).
    def __init__(self, input_value=None, output_value=None) -> None:
        """Create an (input, output) pair container.

        Args:
            input_value (Any): Input-side value.
            output_value (Any): Output-side value.
        """
        super().__init__([input_value, output_value])

    @property
    def input(self):
        """Return the input-side value."""
        return self[0]

    @property
    def output(self):
        """Return the output-side value."""
        return self[1]


@dataclass
class DefaultConfig:
    """Global default configuration for the audiodevice compatibility layer."""
    host: str = "127.0.0.1"
    port: int = 18789

    # audiodevice-compatible default properties:
    # - default.hostapi is read-only (int index); derived from device / device_in / device_out
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

    # Optional override: input/output device index (int only). -1 = use default.device or hostapi default.
    device_in: int = -1
    device_out: int = -1

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
        """Create a holder that mimics `sounddevice.default` behavior."""
        super().__setattr__("_cfg", DefaultConfig())
        super().__setattr__("_resolving_device", False)

    def _device_tuple_raw(self) -> Tuple[int, int]:
        """Return raw `(input_index, output_index)` from `default.device`.

        Returns:
            tuple[int, int]: Device indices, where -1 means "unspecified".
        """
        d = getattr(self._cfg, "device", (-1, -1))
        if isinstance(d, tuple) and len(d) == 2:
            return int(d[0]), int(d[1])
        return -1, -1

    def _sync_hostapi_from_device_index(self, idx: int) -> None:
        """Update hostapi_index and hostapi_name from the given device index (read-only hostapi is derived from device)."""
        if idx is None or idx < 0:
            return
        try:
            from .api import query_devices, query_hostapis

            d = query_devices(int(idx))
            hi = int(d.get("hostapi", -1))
            if hi < 0:
                return
            h = query_hostapis(hi)
            self._cfg.hostapi_index = hi
            self._cfg.hostapi_name = str(h.get("name", self._cfg.hostapi_name))
        except Exception:
            pass

    @property
    def hostapi(self) -> int:
        """Return the default host API index (read-only). Derived from current default.device / device_in / device_out."""
        return int(getattr(self._cfg, "hostapi_index", 0) or 0)

    @hostapi.setter
    def hostapi(self, value) -> None:
        """hostapi is read-only. Set default.device (or device_in / device_out) to change the effective host API."""
        raise AttributeError(
            "default.hostapi is read-only; set default.device (or device_in/device_out) by device index to change host API"
        )

    @property
    def channels(self) -> _InputOutputPair:
        """Return default `(input_channels, output_channels)`."""
        ch = getattr(self._cfg, "channels", (None, None))
        if not isinstance(ch, tuple) or len(ch) != 2:
            ch = (None, None)
        return _InputOutputPair(ch[0], ch[1])

    @channels.setter
    def channels(self, value) -> None:
        """Set default channels.

        Args:
            value (int | tuple[int|None, int|None] | None): Channel count(s).

        Raises:
            TypeError: If `value` is not supported.
        """
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
        """Return the default samplerate (Hz), or None if unspecified."""
        sr = getattr(self._cfg, "samplerate", None)
        return None if sr is None else float(sr)

    @samplerate.setter
    def samplerate(self, value) -> None:
        """Set the default samplerate.

        Args:
            value (float | None): Samplerate in Hz, or None for unspecified.
        """
        if value is None:
            self._cfg.samplerate = None
            return
        self._cfg.samplerate = float(value)

    @property
    def dtype(self) -> _InputOutputPair:
        """Return the default `(input_dtype, output_dtype)` pair."""
        dt = getattr(self._cfg, "dtype", (None, None))
        if not isinstance(dt, tuple) or len(dt) != 2:
            dt = (None, None)
        return _InputOutputPair(dt[0], dt[1])

    @dtype.setter
    def dtype(self, value) -> None:
        """Set default dtype.

        Args:
            value (str | tuple[str|None, str|None] | None): Dtype name(s), or None.

        Raises:
            TypeError: If `value` is not supported.
        """
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
        """Return the default `(input_latency, output_latency)` pair."""
        lat = getattr(self._cfg, "latency", (None, None))
        if not isinstance(lat, tuple) or len(lat) != 2:
            lat = (None, None)
        return _InputOutputPair(lat[0], lat[1])

    @latency.setter
    def latency(self, value) -> None:
        """Set default latency.

        Args:
            value (str | float | tuple[Any, Any] | None): Latency setting(s).

        Raises:
            TypeError: If `value` is not supported.
        """
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
        """Return the default device selection as (input_index, output_index).

        Only integer indices are stored; device names are not supported.
        This is lazily resolved on first access when indices are (-1, -1).
        """
        # Lazy-initialize to something meaningful on first access, similar to sd.default.device.
        if getattr(self, "_resolving_device", False):
            di, do = self._device_tuple_raw()
            return _InputOutputPair(int(di), int(do))

        if (
            isinstance(self._cfg.device, tuple)
            and len(self._cfg.device) == 2
            and int(self._cfg.device[0]) == -1
            and int(self._cfg.device[1]) == -1
            and int(getattr(self._cfg, "device_in", -1)) < 0
            and int(getattr(self._cfg, "device_out", -1)) < 0
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

    @property
    def device_in(self) -> int:
        """Default input device index. -1 means unspecified (use default.device[0] or hostapi default)."""
        return int(getattr(self._cfg, "device_in", -1))

    @device_in.setter
    def device_in(self, value: int) -> None:
        """Set default input device by index only (int). -1 = unspecified. Updates effective hostapi from this device."""
        if not isinstance(value, int):
            raise TypeError("default.device_in must be int (device index); -1 means unspecified")
        self._cfg.device_in = int(value)
        self._sync_hostapi_from_device_index(self._cfg.device_in)

    @property
    def device_out(self) -> int:
        """Default output device index. -1 means unspecified (use default.device[1] or hostapi default)."""
        return int(getattr(self._cfg, "device_out", -1))

    @device_out.setter
    def device_out(self, value: int) -> None:
        """Set default output device by index only (int). -1 = unspecified. Updates effective hostapi from this device."""
        if not isinstance(value, int):
            raise TypeError("default.device_out must be int (device index); -1 means unspecified")
        self._cfg.device_out = int(value)
        self._sync_hostapi_from_device_index(self._cfg.device_out)

    @device.setter
    def device(self, value: Union[int, Sequence[int], None]) -> None:
        """Set the default device selection by index only.

        Args:
            value (int | Sequence[int] | None): A single device index (input and output),
                a 2-tuple/list `(input_index, output_index)`, or None to reset.
                Only integer indices are accepted; device names are not supported.

        Raises:
            TypeError: If `value` is not int, (int, int), or None.
        """
        if value is None:
            self._cfg.device = (-1, -1)
            self._cfg.device_in = -1
            self._cfg.device_out = -1
            return
        if isinstance(value, int):
            di = int(value)
            self._cfg.device = (di, di)
            self._cfg.device_in = -1
            self._cfg.device_out = -1
            self._sync_hostapi_from_device_index(di)
            return
        if isinstance(value, (list, tuple)) and len(value) == 2:
            di = int(value[0])
            do = int(value[1])
            self._cfg.device = (di, do)
            self._cfg.device_in = -1
            self._cfg.device_out = -1
            # Derive hostapi from input device (or output if input unspecified)
            self._sync_hostapi_from_device_index(di if di >= 0 else do)
            return
        raise TypeError("default.device must be int, (in, out), or None; device names are not supported")

    def reset(self) -> None:
        """Reset defaults to audiodevice-like defaults."""
        self._cfg.hostapi_name = "MME"
        self._cfg.hostapi_index = 0
        self._cfg.samplerate = None
        self._cfg.channels = (None, None)
        self._cfg.dtype = ("float32", "float32")
        self._cfg.latency = (None, None)
        self._cfg.device = (-1, -1)
        self._cfg.device_in = -1
        self._cfg.device_out = -1

    def __getattr__(self, name: str):
        """Forward attribute access to the underlying config dataclass."""
        return getattr(self._cfg, name)

    def __setattr__(self, name: str, value) -> None:
        """Set a default attribute with property-aware routing."""
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
        if name == "device_in":
            type(self).device_in.fset(self, value)
            return
        if name == "device_out":
            type(self).device_out.fset(self, value)
            return
        setattr(self._cfg, name, value)

    def as_dict(self) -> dict:
        """Return a shallow dict copy of the current defaults."""
        return self._cfg.__dict__.copy()


default = _DefaultHolder()

