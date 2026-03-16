from __future__ import annotations

import base64
import os
import socket
import subprocess
import threading
import time
import wave
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import numpy as np

from .client import AudioDeviceClient
from .default import default
from .engine import ensure_engine_available


_DEFAULT_SR_FALLBACK = 48_000.0
# Enumeration can be slow; use default.timeout by default.
_LIST_TIMEOUT_S = 30.0

_ENGINE_LOCK = threading.Lock()
_ENGINE_PROC: Optional[subprocess.Popen] = None
# Windows-only: keep a Job Object handle alive so the engine process is
# automatically terminated when this Python process exits (even abruptly).
_ENGINE_JOB_HANDLE: Optional[int] = None


def _win32_job_add_process_kill_on_close(proc: subprocess.Popen) -> None:
    """Best-effort: ensure a spawned engine is killed when Python exits (Windows).

    On Windows, child processes do NOT automatically terminate when the parent process
    exits. To avoid leaving `audiodevice.exe` running in the background, we place it
    in a Job Object configured with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
    """
    if os.name != "nt":
        return

    # Lazily import and define Windows-only pieces.
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return

    global _ENGINE_JOB_HANDLE

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    JobObjectExtendedLimitInformation = 9
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    SIZE_T = ctypes.c_size_t
    ULONG_PTR = wintypes.WPARAM

    class LARGE_INTEGER(ctypes.Structure):
        _fields_ = [("QuadPart", ctypes.c_longlong)]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", LARGE_INTEGER),
            ("PerJobUserTimeLimit", LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", SIZE_T),
            ("MaximumWorkingSetSize", SIZE_T),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ULONG_PTR),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", SIZE_T),
            ("JobMemoryLimit", SIZE_T),
            ("PeakProcessMemoryUsed", SIZE_T),
            ("PeakJobMemoryUsed", SIZE_T),
        ]

    k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, wintypes.INT, wintypes.LPVOID, wintypes.DWORD]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL

    job = _ENGINE_JOB_HANDLE
    if not job:
        job_h = k32.CreateJobObjectW(None, None)
        if not job_h:
            return

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = k32.SetInformationJobObject(
            job_h,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            try:
                k32.CloseHandle(job_h)
            except Exception:
                pass
            return

        _ENGINE_JOB_HANDLE = int(job_h)
        job = _ENGINE_JOB_HANDLE

    ph = getattr(proc, "_handle", None) or getattr(proc, "handle", None)
    if not ph:
        return

    # Best-effort: if the process is already in a Job (and the environment doesn't
    # allow nested jobs), this may fail with ERROR_ACCESS_DENIED.
    _ = k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(int(ph)))

_CACHE_LOCK = threading.Lock()
_CACHED_HOSTAPIS: Optional[Tuple[List[str], Dict[str, List[str]]]] = None
_CACHED_DEVICES: Optional[DeviceList] = None

_GLOBAL_SESSION_LOCK = threading.Lock()
_GLOBAL_SESSION_THREAD: Optional[threading.Thread] = None
_GLOBAL_SESSION_STOP: Optional[threading.Event] = None
_GLOBAL_SESSION_DONE: Optional[threading.Event] = None
_GLOBAL_SESSION_ERROR: Optional[BaseException] = None
_GLOBAL_STREAM: Optional[object] = None


class DeviceList(list):
    """A list of device dicts with an audiodevice-like repr()."""

    def __init__(
        self,
        devices: Iterable[Dict[str, Any]],
        *,
        hostapi_names: Optional[List[str]] = None,
        default_input_index: Optional[int] = None,
        default_output_index: Optional[int] = None,
    ) -> None:
        """Create a printable device list.

        Args:
            devices (Iterable[dict[str, Any]]): Device dictionaries (compatible with
                `audiodevice.query_devices()` style).
            hostapi_names (Optional[list[str]]): Optional host API names used for display.
            default_input_index (Optional[int]): Index of the default input device.
            default_output_index (Optional[int]): Index of the default output device.
        """
        super().__init__(devices)
        self._hostapi_names = list(hostapi_names or [])
        self._default_input_index = default_input_index
        self._default_output_index = default_output_index

    def _hostapi_name_for_index(self, hi: int) -> str:
        """Resolve a host API name for a hostapi index.

        Args:
            hi (int): Host API index (as stored in device dicts).

        Returns:
            str: Display name for the host API, or "Unknown" when not resolvable.
        """
        if hi < 0:
            return "Unknown"
        try:
            per = query_hostapis()
            if isinstance(per, (list, tuple)) and 0 <= hi < len(per):
                n = (per[hi].get("name") if isinstance(per[hi], dict) else None) or ""
                if n:
                    return str(n)
        except Exception:
            pass
        if 0 <= hi < len(self._hostapi_names):
            return self._hostapi_names[hi] or "Unknown"
        return "Unknown"

    def __repr__(self) -> str:
        # Match the common "audiodevice.query_devices()" table-ish output
        # (marker + index + "name, hostapi (X in, Y out)").
        lines: List[str] = []
        for i, d in enumerate(self):
            name = str(d.get("name", ""))
            # NOTE: hostapi index 0 is valid; don't treat it as falsy.
            _hi_raw = d.get("hostapi", -1)
            hi = int(_hi_raw) if _hi_raw is not None else -1
            hostapi = self._hostapi_name_for_index(hi)
            mi = int(d.get("max_input_channels", 0) or 0)
            mo = int(d.get("max_output_channels", 0) or 0)

            marker = " "
            if self._default_input_index is not None and i == self._default_input_index:
                marker = ">"
            if self._default_output_index is not None and i == self._default_output_index:
                marker = "<" if marker == " " else "*"

            lines.append(f"{marker} {i} {name}, {hostapi} ({mi} in, {mo} out)")
        return "\n".join(lines)


def _backend_for_hostapi(hostapi_name: str) -> str:
    """Select the engine backend for a given host API name.

    Args:
        hostapi_name (str): Display host API name (e.g. "ASIO", "Windows WASAPI", "MME").

    Returns:
        str: Backend name understood by the engine ("cpal" or "portaudio").
    """
    # Backend selection (no public backend parameter):
    # - ASIO -> cpal backend
    # - everything else exposed in the audiodevice-like layer -> portaudio backend
    h = str(hostapi_name or "").strip().upper()
    if h in ("ASIO", "WASAPI"):
        return "cpal"
    return "portaudio"


def _hostapi_display_to_engine(hostapi_display: str) -> Tuple[str, str, str]:
    """Map display host API name to engine parameters.

    Args:
        hostapi_display (str): Public/display host API name (e.g. "Windows WASAPI").

    Returns:
        tuple[str, str, str]: `(backend, engine_hostapi, display_name)`.
    """
    disp = str(hostapi_display or "").strip()
    if not disp:
        disp = "MME"
    if disp.lower().startswith("windows "):
        engine_name = disp[len("Windows ") :].strip()
        # Keep backend selection consistent with unprefixed names.
        return _backend_for_hostapi(engine_name), engine_name, disp
    # Keep unprefixed names as engine names.
    return _backend_for_hostapi(disp), disp, disp


def _resolve_device_from_default_index(which: str) -> Tuple[str, str]:
    """Resolve device name from `default.device` indices.

    Args:
        which (str): "input" or "output".

    Returns:
        tuple[str, str]: `(hostapi_name, device_name)`; returns ("","") if unspecified
        or resolution fails.
    """
    try:
        di, do = default._device_tuple_raw()
        idx = int(di if which == "input" else do)
    except Exception:
        idx = -1
    return _resolve_device_index_to_name(idx)


def _resolve_device_index_to_name(idx: int) -> Tuple[str, str]:
    """Resolve (hostapi_name, device_name) from a global device index.

    Args:
        idx (int): Global device index (e.g. from query_devices()). Use -1 for unspecified.

    Returns:
        tuple[str, str]: `(hostapi_name, device_name)`; returns ("", "") if idx < 0 or resolution fails.
    """
    if idx is None or idx < 0:
        return "", ""
    try:
        return _device_name_from_index(int(idx))
    except Exception:
        return "", ""


def _resolve_hostapi_and_devices(
    *,
    hostapi: Optional[str],
    device_in: Optional[int],
    device_out: Optional[int],
) -> Tuple[str, str, str]:
    """Resolve effective host API and device names from device indices.

    device_in / device_out are interpreted as device indices (int) only; device names are not accepted.

    Args:
        hostapi (Optional[str]): Preferred host API display name.
        device_in (Optional[int]): Input device index (global index from query_devices()). None = use default.
        device_out (Optional[int]): Output device index. None = use default.

    Returns:
        tuple[str, str, str]: `(hostapi_name, input_device_name, output_device_name)`.
    """
    hostapi_eff = str(hostapi or getattr(default, "hostapi_name", "") or "MME")

    # Effective index: call argument if provided, else default.device_in_override/device_out_override if >= 0, else default.device tuple.
    di_raw, do_raw = default._device_tuple_raw()
    def_in = int(getattr(default, "device_in_override", -1))
    def_out = int(getattr(default, "device_out_override", -1))
    in_idx = device_in if device_in is not None else (def_in if def_in >= 0 else di_raw)
    out_idx = device_out if device_out is not None else (def_out if def_out >= 0 else do_raw)

    def _to_index(v) -> int:
        if v is None or v == "":
            return -1
        try:
            return int(v)
        except (TypeError, ValueError):
            return -1

    in_host, in_name = _resolve_device_index_to_name(_to_index(in_idx))
    out_host, out_name = _resolve_device_index_to_name(_to_index(out_idx))

    picked_hosts = [h for h in (in_host, out_host) if h]
    if picked_hosts:
        if in_host and out_host and in_host.strip().upper() != out_host.strip().upper():
            raise ValueError(f"default.device input/output must use same hostapi (got {in_host!r} vs {out_host!r})")
        hostapi_eff = picked_hosts[0]

    return hostapi_eff, in_name, out_name

def _list_hostapis_raw_backend(backend: str) -> Dict[str, Any]:
    """List host APIs for a specific backend.

    Args:
        backend (str): Engine backend ("portaudio" or "cpal").

    Returns:
        dict[str, Any]: Engine reply payload.
    """
    proc = _ensure_engine_running()
    c = AudioDeviceClient(default.host, default.port, timeout=float(default.timeout))
    try:
        return c.request({"cmd": "list_hostapis", "backend": str(backend)})
    finally:
        c.close()
        _ = proc


def _hostapis_all() -> Tuple[List[str], Dict[str, List[str]]]:
    """Return cached host API display names and a backend mapping.

    Returns:
        tuple[list[str], dict[str, list[str]]]: `(display_order, by_backend)`.
    """
    global _CACHED_HOSTAPIS
    with _CACHE_LOCK:
        if _CACHED_HOSTAPIS is not None:
            return _CACHED_HOSTAPIS[0], _CACHED_HOSTAPIS[1]

    # Build an audiodevice-like hostapi list (display names).
    by_backend: Dict[str, List[str]] = {"portaudio": [], "cpal": []}
    order: List[str] = []

    def _hostapi_name_from_item(x: Any) -> str:
        """Normalize host API items returned by engine to a display name.

        Args:
            x (Any): Engine host API item (string or dict containing "name").

        Returns:
            str: A non-empty display name when possible.
        """
        if isinstance(x, dict) and "name" in x:
            return str(x["name"]).strip() or str(x)
        return str(x).strip() if x is not None else ""

    # PortAudio hostapis: match audiodevice naming on Windows.
    try:
        r = _list_hostapis_raw_backend("portaudio")
        pa = [_hostapi_name_from_item(x) for x in (r.get("hostapis") or []) if _hostapi_name_from_item(x)]
    except Exception:
        pa = []
    by_backend["portaudio"] = pa
    for n in pa:
        disp = n
        if n.upper() in ("DIRECTSOUND", "WASAPI"):
            disp = f"Windows {n}"
        if disp not in order:
            order.append(disp)

    # CPAL hostapis: currently only expose ASIO in the audiodevice-like layer.
    try:
        r = _list_hostapis_raw_backend("cpal")
        cp = [_hostapi_name_from_item(x) for x in (r.get("hostapis") or []) if _hostapi_name_from_item(x)]
    except Exception:
        cp = []
    by_backend["cpal"] = cp
    for n in cp:
        if str(n).strip().upper() != "ASIO":
            continue
        if n not in order:
            order.append(n)

    with _CACHE_LOCK:
        _CACHED_HOSTAPIS = (order, by_backend)
    return order, by_backend


def _cache_set_devices(devs: DeviceList) -> None:
    """Store the device list cache.

    Args:
        devs (DeviceList): Cached device list.
    """
    with _CACHE_LOCK:
        global _CACHED_DEVICES
        _CACHED_DEVICES = devs


def _cache_get_devices() -> Optional[DeviceList]:
    """Get the cached device list (if any).

    Returns:
        Optional[DeviceList]: Cached device list, or None when not cached.
    """
    with _CACHE_LOCK:
        return _CACHED_DEVICES


def init(*, engine_exe: Optional[str] = None, engine_cwd: Optional[str] = None, timeout: Optional[float] = None) -> None:
    """Initialize audiodevice (engine + caches).

    This is a convenience API (audiodevice doesn't require explicit init).

    Args:
        engine_exe (Optional[str]): Path to `audiodevice.exe`. If None, resolution falls back
            to `default.engine_exe` and other discovery methods.
        engine_cwd (Optional[str]): Working directory for the engine process.
        timeout (Optional[float]): TCP RPC timeout (seconds) for engine requests.
    """
    if engine_exe is not None:
        default.engine_exe = str(engine_exe)
    if engine_cwd is not None:
        default.engine_cwd = str(engine_cwd)
    if timeout is not None:
        default.timeout = float(timeout)

    default.auto_start = True

    # Start engine and verify it responds.
    _ = _ensure_engine_running()

    # Clear caches before warming.
    with _CACHE_LOCK:
        global _CACHED_HOSTAPIS, _CACHED_DEVICES
        _CACHED_HOSTAPIS = None
        _CACHED_DEVICES = None

    # Warm caches (hostapis + devices).
    _ = query_hostapis_raw()
    _ = query_devices()


def _initialize() -> None:
    """Internal alias used by some audiodevice-like integrations."""
    init()


def _terminate() -> None:
    """Best-effort terminate current session and engine.

    This stops the current session (if any), terminates the engine process started by this
    module, and clears caches. It does not guarantee termination if the engine was started
    externally.
    """
    stop()
    global _ENGINE_PROC
    with _ENGINE_LOCK:
        if _ENGINE_PROC is not None and _ENGINE_PROC.poll() is None:
            try:
                _ENGINE_PROC.terminate()
            except Exception:
                pass
        _ENGINE_PROC = None

    with _CACHE_LOCK:
        global _CACHED_HOSTAPIS, _CACHED_DEVICES
        _CACHED_HOSTAPIS = None
        _CACHED_DEVICES = None


def sleep(ms: int) -> None:
    """Sleep for the given duration in milliseconds.

    Args:
        ms (int): Milliseconds to sleep.
    """
    time.sleep(float(ms) / 1000.0)


def get_stream():
    """Return the current active Stream object (if any).

    Returns:
        Any: The active stream instance (`Stream`, `InputStream`, or `OutputStream`), or None.
    """
    with _GLOBAL_SESSION_LOCK:
        return _GLOBAL_STREAM


def get_status() -> Optional[Dict[str, Any]]:
    """Get the engine status for the current session (best-effort).

    Returns:
        Optional[dict[str, Any]]: Engine status payload, or None if unavailable.
    """
    try:
        _ = _ensure_engine_running()
        c = AudioDeviceClient(default.host, default.port, timeout=default.timeout)
        try:
            return c.request({"cmd": "status"})
        finally:
            c.close()
    except Exception:
        return None


def print_default_devices() -> None:
    """Print current default input and output device (index + name).

    Uses effective indices: default.device_in_override / default.device_out_override if >= 0, else default.device.
    Call this after `init()` if you want to quickly verify device selection.
    """
    try:
        di_raw, do_raw = default._device_tuple_raw()
        def_in = int(getattr(default, "device_in_override", -1))
        def_out = int(getattr(default, "device_out_override", -1))
        di = def_in if def_in >= 0 else int(di_raw)
        do = def_out if def_out >= 0 else int(do_raw)
        if di >= 0:
            d = query_devices(di)
            print(f"Default input:  [{di}] {d.get('name', '')}")
        else:
            print("Default input:  (none)")
        if do >= 0:
            d = query_devices(do)
            print(f"Default output: [{do}] {d.get('name', '')}")
        else:
            print("Default output: (none)")
    except Exception as e:
        print("Default devices: (query failed)", e)


def device_index_for_hostapi(
    hostapi_name: str,
    direction: str = "input",
) -> Optional[int]:
    """Return a device index for the given host API name (e.g. \"WASAPI\", \"ASIO\").

    Use this to set default.device so that the read-only default.hostapi reflects the desired API.

    Args:
        hostapi_name (str): Host API display name (case-insensitive substring match).
        direction (str): \"input\" or \"output\"; picks first device with that direction.

    Returns:
        Optional[int]: Global device index, or None if not found.
    """
    name = (hostapi_name or "").strip().lower()
    if not name:
        return None
    try:
        hs = query_hostapis()
        hi = None
        for i, h in enumerate(hs):
            if name in str(h.get("name", "") or "").strip().lower():
                hi = i
                break
        if hi is None:
            return None
        devs = query_devices()
        for d in devs:
            if int(d.get("hostapi", -1)) != hi:
                continue
            if direction == "input" and ((int(d.get("max_input_channels", 0) or 0) > 0)):
                return int(d.get("index", -1))
            if direction == "output" and ((int(d.get("max_output_channels", 0) or 0) > 0)):
                return int(d.get("index", -1))
        for d in devs:
            if int(d.get("hostapi", -1)) == hi:
                return int(d.get("index", -1))
    except Exception:
        pass
    return None


def stop() -> None:
    """Stop the current playback/recording/stream (best-effort).

    This signals any tracked worker thread to stop and sends `session_stop` to the engine.
    """
    with _GLOBAL_SESSION_LOCK:
        if _GLOBAL_SESSION_STOP is not None:
            _GLOBAL_SESSION_STOP.set()

    try:
        _ = _ensure_engine_running()
        c = AudioDeviceClient(default.host, default.port, timeout=default.timeout)
        try:
            _ = c.request({"cmd": "session_stop"})
        finally:
            c.close()
    except Exception:
        pass


def wait() -> None:
    """Wait for the last non-blocking operation to finish.

    Raises:
        BaseException: Re-raises the exception captured from the worker thread (if any).
    """
    t = None
    done = None
    err = None
    with _GLOBAL_SESSION_LOCK:
        t = _GLOBAL_SESSION_THREAD
        done = _GLOBAL_SESSION_DONE

    if done is not None:
        # Avoid hanging forever if the worker thread gets stuck but the engine session ended.
        # Poll engine status and return when no active session.
        deadline = time.time() + 60.0
        while True:
            if done.wait(timeout=0.2):
                break
            st = get_status() or {}
            if not st.get("has_session", False):
                # Best-effort: ask the worker to stop and return.
                stop()
                if t is not None:
                    try:
                        t.join(timeout=0.5)
                    except Exception:
                        pass
                break
            if time.time() >= deadline:
                # Last resort: don't block forever.
                break
    elif t is not None:
        t.join(timeout=60.0)
    else:
        # No thread tracked; fall back to polling engine status.
        st = get_status() or {}
        if st.get("has_session", False):
            c = AudioDeviceClient(default.host, default.port, timeout=default.timeout)
            try:
                _wait_session_end(c, timeout_s=3600.0)
            finally:
                c.close()

    with _GLOBAL_SESSION_LOCK:
        err = _GLOBAL_SESSION_ERROR
    if err is not None:
        raise err


def _list_devices_raw(
    *,
    hostapi: Optional[str],
    direction: str,
) -> Dict[str, Any]:
    """Query the engine for a raw device listing.

    Args:
        hostapi (Optional[str]): Host API display name (e.g. "MME", "Windows WASAPI", "ASIO").
        direction (str): "input" or "output".

    Returns:
        dict[str, Any]: Engine reply payload containing a `devices` list (when available).
    """
    hostapi_eff = str(hostapi or getattr(default, "hostapi_name", "") or "MME")
    backend_eff, engine_hostapi, _disp = _hostapi_display_to_engine(hostapi_eff)
    proc = _ensure_engine_running()
    c = AudioDeviceClient(default.host, default.port, timeout=float(default.timeout))
    try:
        return c.request(
            {
                "cmd": "list_devices",
                "backend": backend_eff,
                "hostapi": engine_hostapi,
                "direction": direction,
            }
        )
    finally:
        c.close()
        _ = proc


def _merge_devices(
    devs_in: Iterable[Dict[str, Any]],
    devs_out: Iterable[Dict[str, Any]],
    *,
    hostapi_index: int,
    hostapi_name: str,
) -> List[Dict[str, Any]]:
    """Merge separate input/output device listings into audiodevice-like device dicts.

    Args:
        devs_in (Iterable[dict[str, Any]]): Raw input-device items from engine.
        devs_out (Iterable[dict[str, Any]]): Raw output-device items from engine.
        hostapi_index (int): Host API index to stamp into merged device dicts.
        hostapi_name (str): Host API display name (currently unused, kept for clarity).

    Returns:
        list[dict[str, Any]]: Merged device dictionaries (without global `index` assigned).
    """
    order: List[str] = []
    by_name: Dict[str, Dict[str, Any]] = {}

    def ensure(name: str) -> Dict[str, Any]:
        if name not in by_name:
            by_name[name] = {
                "name": name,
                # audiodevice uses an integer hostapi index.
                "hostapi": int(hostapi_index),
                "max_input_channels": 0,
                "max_output_channels": 0,
                "default_samplerate": float(default.samplerate if default.samplerate is not None else _DEFAULT_SR_FALLBACK),
                "default_low_input_latency": 0.0,
                "default_low_output_latency": 0.0,
                "default_high_input_latency": 0.0,
                "default_high_output_latency": 0.0,
            }
            order.append(name)
        return by_name[name]

    for d in devs_in:
        name = str(d.get("name", ""))
        if not name:
            continue
        info = ensure(name)
        info["max_input_channels"] = max(int(info["max_input_channels"]), int(d.get("max_input_channels", 0) or 0))
        sr = d.get("default_sr", None)
        if sr is not None:
            try:
                info["default_samplerate"] = float(sr)
            except Exception:
                pass

    for d in devs_out:
        name = str(d.get("name", ""))
        if not name:
            continue
        info = ensure(name)
        info["max_output_channels"] = max(int(info["max_output_channels"]), int(d.get("max_output_channels", 0) or 0))
        sr = d.get("default_sr", None)
        if sr is not None:
            try:
                info["default_samplerate"] = float(sr)
            except Exception:
                pass

    return [by_name[n] for n in order]


def _is_port_open(host: str, port: int) -> bool:
    """Check whether a TCP port is open.

    Args:
        host (str): Host/IP.
        port (int): TCP port.

    Returns:
        bool: True if connect succeeds, else False.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex((host, port)) == 0


def _ensure_engine_running() -> Optional[subprocess.Popen]:
    """Start the engine process if needed (and if auto_start is enabled).

    Returns:
        Optional[subprocess.Popen]: The started process if this call spawned one; otherwise None.

    Raises:
        RuntimeError: If the engine fails to start and the port never opens.
        FileNotFoundError: If the engine executable cannot be resolved.
    """
    if not default.auto_start:
        return None
    if _is_port_open(default.host, default.port):
        return None

    global _ENGINE_PROC
    with _ENGINE_LOCK:
        if _is_port_open(default.host, default.port):
            return None

        # If we already started one and it's still alive, don't spawn another.
        if _ENGINE_PROC is not None and _ENGINE_PROC.poll() is None:
            proc = _ENGINE_PROC
        else:
            exe = ensure_engine_available(
                default.engine_exe,
                download_url=default.engine_download_url,
                sha256=default.engine_sha256,
            )
            # Keep defaults in sync (helps subsequent calls).
            default.engine_exe = exe
            if default.engine_cwd is None:
                default.engine_cwd = os.path.dirname(exe)

            proc = subprocess.Popen(
                [exe],
                cwd=default.engine_cwd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                _win32_job_add_process_kill_on_close(proc)
            except Exception:
                pass
            _ENGINE_PROC = proc

        deadline = time.time() + float(default.startup_timeout)
        while time.time() < deadline:
            if _is_port_open(default.host, default.port):
                return proc
            time.sleep(0.05)

        try:
            proc.terminate()
        except Exception:
            pass
        if _ENGINE_PROC is proc:
            _ENGINE_PROC = None
        raise RuntimeError("audiodevice engine did not start (port not open).")


def query_backends() -> Dict[str, Any]:
    """List engine backends.

    Returns:
        dict[str, Any]: Engine reply payload.
    """
    proc = _ensure_engine_running()
    c = AudioDeviceClient(default.host, default.port, timeout=default.timeout)
    try:
        return c.request({"cmd": "list_backends"})
    finally:
        c.close()
        _ = proc


def query_hostapis_raw() -> Dict[str, Any]:
    """Return available host APIs (raw-ish) from the engine.

    Returns:
        dict[str, Any]: At least `{"hostapis": [...]}` and (when available)
        `{"by_backend": {"portaudio": [...], "cpal": [...]}}`.
    """
    hostapis, by_backend = _hostapis_all()
    return {"hostapis": hostapis, "by_backend": by_backend}


def query_hostapis(index=None):
    """List host APIs (audiodevice-compatible).

    Args:
        index (Optional[int | str]): If None, return all host APIs. If int, return the host API
            dict at that index. If str, match by host API name (case-insensitive).

    Returns:
        tuple[dict[str, Any], ...] | dict[str, Any]: A tuple of host API dicts, or a single dict.

    Raises:
        ValueError: If an int index is out of range, or a name is not found.
        TypeError: If `index` is not int/str/None.
    """
    hostapis, _by_backend = _hostapis_all()
    # Build a global device list for stable indices.
    dev_list = query_devices()
    devs = list(dev_list)

    # Compute per-hostapi device indices.
    per: List[Dict[str, Any]] = []
    for hi, hn in enumerate(hostapis):
        devices_idx = [int(d.get("index")) for d in devs if int(d.get("hostapi", -1)) == int(hi)]

        # Prefer default.device indices if they belong to this hostapi.
        try:
            def_in, def_out = default._device_tuple_raw()
        except Exception:
            def_in, def_out = -1, -1

        def pick_default(direction: str) -> int:
            if direction == "input":
                if def_in in devices_idx:
                    return int(def_in)
                for di in devices_idx:
                    if int(devs[di].get("max_input_channels", 0) or 0) > 0:
                        return int(di)
                return -1
            else:
                if def_out in devices_idx:
                    return int(def_out)
                for di in devices_idx:
                    if int(devs[di].get("max_output_channels", 0) or 0) > 0:
                        return int(di)
                return -1

        per.append(
            {
                "name": str(hn),
                "devices": devices_idx,
                "default_input_device": pick_default("input"),
                "default_output_device": pick_default("output"),
            }
        )

    if index is None:
        return tuple(per)
    if isinstance(index, int):
        if index < 0 or index >= len(per):
            raise ValueError(f"invalid hostapi index: {index}")
        return dict(per[int(index)])
    if isinstance(index, str):
        q = index.strip().lower()
        for h in per:
            if str(h.get("name", "")).strip().lower() == q:
                return dict(h)
        raise ValueError(f"hostapi not found: {index}")
    raise TypeError("index must be int, str, or None")


def query_devices_raw(
    *,
    hostapi: Optional[str] = None,
    direction: str = "input",
) -> Dict[str, Any]:
    """Return the engine's raw device listing.

    Args:
        hostapi (Optional[str]): Host API display name (e.g. "MME", "Windows WASAPI", "ASIO").
        direction (str): "input" or "output".

    Returns:
        dict[str, Any]: Engine reply payload (typically contains `devices`).
    """
    return _list_devices_raw(hostapi=hostapi, direction=direction)


def query_devices(
    device: Optional[Union[int, str]] = None,
    kind: Optional[str] = None,
) -> Union[DeviceList, Dict[str, Any]]:
    """Query audio devices (audiodevice-compatible).

    Args:
        device (Optional[int | str]): If None, return a `DeviceList`. If int, treat as a global
            device index. If str, match by exact name first, then substring.
        kind (Optional[str]): If provided, must be `"input"` or `"output"` and returns the
            default device dict for that direction.

    Returns:
        DeviceList | dict[str, Any]: A printable list of devices, or a single device dict.

    Raises:
        ValueError: If an index is out of range, a name is empty/not found, or `kind` is invalid.
        TypeError: If `device` has an unsupported type.
    """
    if device is None and kind is None:
        cached = _cache_get_devices()
        if cached is not None:
            return cached

    hostapis, _ = _hostapis_all()

    devs: List[Dict[str, Any]] = []
    for hi, hn in enumerate(hostapis):
        try:
            raw_in = _list_devices_raw(hostapi=hn, direction="input")
            raw_out = _list_devices_raw(hostapi=hn, direction="output")
        except Exception:
            continue
        devs.extend(
            _merge_devices(
                raw_in.get("devices", []),
                raw_out.get("devices", []),
                hostapi_index=hi,
                hostapi_name=str(hn),
            )
        )

    # Assign stable indices (audiodevice has dev["index"]).
    for i, d in enumerate(devs):
        d["index"] = int(i)

    # Ensure hostapi_names covers all hostapi indices used by devices, so we avoid showing
    # "Unknown" when the engine's hostapi indexing/order differs.
    # hostapi index 0 is valid; don't treat it as falsy.
    max_hi = (
        max((int(d.get("hostapi", -1)) if d.get("hostapi", -1) is not None else -1) for d in devs)
        if devs
        else -1
    )
    hostapi_names_final: List[str] = list(hostapis)
    for idx in range(len(hostapi_names_final), max_hi + 1):
        hostapi_names_final.append(f"HostAPI {idx}")

    def find_index_by_default_name(wanted: Optional[str]) -> Optional[int]:
        if not wanted:
            return None
        w = str(wanted).strip()
        if not w:
            return None
        if w.isdigit():
            idx = int(w)
            return idx if 0 <= idx < len(devs) else None
        wl = w.lower()
        for i, d in enumerate(devs):
            if str(d.get("name", "")).lower() == wl:
                return i
        return None

    default_in_idx = None
    default_out_idx = None
    try:
        di, do = default._device_tuple_raw()
        if 0 <= int(di) < len(devs):
            default_in_idx = int(di)
        if 0 <= int(do) < len(devs):
            default_out_idx = int(do)
    except Exception:
        pass

    dev_list = DeviceList(
        devs,
        hostapi_names=hostapi_names_final,
        default_input_index=default_in_idx,
        default_output_index=default_out_idx,
    )

    if device is None and kind is None:
        _cache_set_devices(dev_list)
        return dev_list

    if device is None and kind is not None:
        k = str(kind).strip().lower()
        if k not in ("input", "output"):
            raise ValueError("kind must be 'input' or 'output'")
        if k == "input":
            if default_in_idx is not None:
                return dict(devs[default_in_idx])
            for d in devs:
                if int(d.get("max_input_channels", 0) or 0) > 0:
                    return dict(d)
            raise ValueError("no input device available")
        else:
            if default_out_idx is not None:
                return dict(devs[default_out_idx])
            for d in devs:
                if int(d.get("max_output_channels", 0) or 0) > 0:
                    return dict(d)
            raise ValueError("no output device available")

    if isinstance(device, int):
        if device < 0 or device >= len(devs):
            raise ValueError(f"invalid device index: {device}")
        return dict(devs[int(device)])

    if isinstance(device, str):
        q = device.strip()
        if not q:
            raise ValueError("device name must be non-empty")
        ql = q.lower()
        # exact match first
        for d in devs:
            if str(d.get("name", "")).lower() == ql:
                return dict(d)
        # then substring match
        for d in devs:
            if ql in str(d.get("name", "")).lower():
                return dict(d)
        raise ValueError(f"device not found: {device}")

    raise TypeError("device must be int, str, or None")


@dataclass
class RecordingHandle:
    """Handle for a non-blocking recording session.

    Use `read()` to pull audio frames incrementally, `wait()` to read everything until EOF,
    and `stop()` to stop the session and release resources.
    """
    started_at: float
    duration_s: float
    channels: int
    samplerate: int
    _proc: Optional[subprocess.Popen]
    _client: AudioDeviceClient

    def stop(self) -> None:
        """Stop the recording session and close underlying resources."""
        try:
            self._client.request({"cmd": "session_stop"})
        finally:
            self._client.close()
            if self._proc is not None:
                try:
                    self._proc.terminate()
                except Exception:
                    pass

    def read(self, max_frames: int = 4096) -> Tuple[np.ndarray, bool]:
        """Read up to `max_frames` frames from the engine capture buffer.

        Args:
            max_frames (int): Maximum number of frames to request from the engine.

        Returns:
            tuple[np.ndarray, bool]: `(audio, eof)` where `audio` is float32 shaped
            `(frames, channels)` and `eof` indicates the engine ended the stream.
        """
        data = self._client.request({"cmd": "capture_read", "max_frames": int(max_frames)})
        pcm16 = base64.b64decode(data["pcm16_b64"])
        frames = int(data["frames"])
        ch = int(data["channels"])
        eof = bool(data["eof"])
        if frames <= 0 or ch <= 0:
            return np.zeros((0, self.channels), dtype=np.float32), eof
        # Be robust against occasional metadata/payload mismatch from the engine:
        # compute usable frames from payload length and channel count.
        if (len(pcm16) % 2) != 0:
            pcm16 = pcm16[: len(pcm16) - 1]
        s16 = np.frombuffer(pcm16, dtype=np.int16)
        frames_payload = int(s16.size // ch) if ch > 0 else 0
        if frames_payload <= 0:
            return np.zeros((0, self.channels), dtype=np.float32), eof
        usable_frames = frames_payload if frames <= 0 else min(frames_payload, frames)
        usable_samples = int(usable_frames * ch)
        s16 = s16[:usable_samples]
        x = s16.astype(np.float32) / 32767.0
        x = x.reshape(usable_frames, ch)
        return x, eof

    def wait(self) -> np.ndarray:
        """Read audio until EOF, then stop the session.

        Returns:
            np.ndarray: Concatenated float32 audio array with shape `(frames, channels)`.
        """
        chunks = []
        while True:
            x, eof = self.read(4096)
            if x.size:
                chunks.append(x)
            if eof and x.size == 0:
                break
            time.sleep(0.005)
        self.stop()
        if not chunks:
            return np.zeros((0, self.channels), dtype=np.float32)
        return np.concatenate(chunks, axis=0)


def rec_monitor(
    duration_s: float,
    *,
    wav_path: str = "",
    save_wav: bool = False,
    blocking: bool = True,
    monitor_channel: Optional[int] = None,
    samplerate: Optional[int] = None,
    channels: Optional[int] = None,
    device_in: Optional[int] = None,
    device_out: Optional[int] = None,
    rb_seconds: Optional[int] = None,
) -> Union[np.ndarray, RecordingHandle]:
    """Record while monitoring (listen-through) for a fixed duration.

    Args:
        duration_s: Recording duration in seconds; must be > 0.
            Format: float, e.g. 5.0.
        wav_path: Output WAV path when save_wav=True; must be non-empty then.
            Format: str, local file path.
        save_wav: Whether to also save a WAV file.
        blocking: If True, return the recorded array. If False, return a RecordingHandle for
            incremental reads.
        monitor_channel: Which input channel to listen to during monitoring (1-based).
            - 1 means CH1 (the first input channel), 2 means CH2, etc.
            - None keeps the engine's legacy monitoring mapping behavior.
        samplerate: Target sample rate in Hz. Uses default.samplerate if None.
            Format: int or None, e.g. 44100.
        channels: Number of input channels to record. Uses default.channels[0] or 1 if None.
            Format: int or None.
        device_in: Input device index (global index from query_devices()). None = use default.
            Only int is accepted; device names are not supported.
        device_out: Output device index used for monitoring playback. None = use default.
            Only int is accepted.
        rb_seconds: Ring buffer size in seconds; larger reduces overrun risk.
            Format: int or None.

    Returns:
        np.ndarray | RecordingHandle: Recorded audio when blocking; otherwise a handle.

    Raises:
        ValueError: If `duration_s<=0`, or `save_wav=True` with an empty `wav_path`.
        RuntimeError: If the engine fails to start the monitor+record session.
    """
    proc = _ensure_engine_running()

    dur = float(duration_s)
    if dur <= 0.0:
        raise ValueError("duration_s must be > 0")

    fs0 = int(samplerate if samplerate is not None else (default.samplerate if default.samplerate is not None else _DEFAULT_SR_FALLBACK))
    ch_def = default.channels[0] if getattr(default, "channels", None) is not None else None
    ch0 = int(channels if channels is not None else (int(ch_def) if ch_def is not None else 1))

    monitor_in_idx: Optional[int] = None
    if monitor_channel is not None:
        mc = int(monitor_channel)
        if mc < 1:
            raise ValueError("monitor_channel must be >= 1 (1-based), or None")
        monitor_in_idx = mc - 1

    if save_wav and not wav_path:
        raise ValueError("save_wav=True requires a non-empty wav_path")
    wav_path_abs = os.path.abspath(wav_path) if (save_wav and wav_path) else ""

    rb_send = int(rb_seconds if rb_seconds is not None else default.rb_seconds)
    rb_send = max(rb_send, int(np.ceil(dur)) + 1, 2)

    hostapi_eff, dev_in, dev_out = _resolve_hostapi_and_devices(
        hostapi=None,
        device_in=device_in,
        device_out=device_out,
    )
    backend_eff, engine_hostapi, _ = _hostapi_display_to_engine(hostapi_eff)

    c = AudioDeviceClient(default.host, default.port, timeout=default.timeout)
    fs = fs0
    ch = ch0
    tried = []
    last_err: Optional[Exception] = None

    # Build a robust sample-rate candidate list:
    # - Prefer the user's requested/default samplerate (fs0)
    # - Then try the selected input device's reported default samplerate (if available)
    # - Then fall back to common rates
    dev_default_sr: Optional[int] = None
    try:
        hostapis_order, _ = _hostapis_all()
        hi = -1
        qh = str(hostapi_eff).strip().lower()
        for i, hn in enumerate(hostapis_order):
            if str(hn).strip().lower() == qh:
                hi = int(i)
                break
        if hi >= 0 and dev_in:
            devs = query_devices()
            if isinstance(devs, list):
                for d in devs:
                    if str(d.get("name", "")) == str(dev_in) and int(d.get("hostapi", -1)) == hi:
                        sr = d.get("default_samplerate", None)
                        if sr is not None:
                            dev_default_sr = int(round(float(sr)))
                        break
    except Exception:
        dev_default_sr = None

    fs_candidates: List[int] = []
    for v in (fs0, dev_default_sr, 48_000, 44_100, 96_000, 88_200, 32_000, 16_000):
        if v is None:
            continue
        try:
            iv = int(v)
        except Exception:
            continue
        if iv <= 0:
            continue
        if iv not in fs_candidates:
            fs_candidates.append(iv)

    for fs_try in fs_candidates:
        for in_ch_try in (ch0, 1, 2):
            if in_ch_try <= 0:
                continue
            if monitor_in_idx is not None and int(in_ch_try) <= int(monitor_in_idx):
                # Need enough input channels to include the requested monitor channel.
                continue
            for out_ch_try in (2, 1, int(in_ch_try)):
                if out_ch_try <= 0:
                    continue
                tried.append((int(fs_try), int(in_ch_try), int(out_ch_try)))
                try:
                    req = {
                        "cmd": "session_start",
                        "backend": backend_eff,
                        "hostapi": engine_hostapi,
                        "mode": "monitor_record",
                        "sr": int(fs_try),
                        "in_ch": int(in_ch_try),
                        "out_ch": int(out_ch_try),
                        "device_in": dev_in,
                        "device_out": dev_out,
                        "duration_s": 0,
                        "rotate_s": 0,
                        "path": wav_path_abs,
                        "play_path": "",
                        "return_audio": True,
                        "rb_seconds": rb_send,
                    }
                    if monitor_in_idx is not None:
                        req["monitor_in_idx"] = int(monitor_in_idx)
                    c.request(req)
                    fs = int(fs_try)
                    ch = int(in_ch_try)
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
            if last_err is None:
                break
        if last_err is None:
            break
    if last_err is not None:
        c.close()
        raise RuntimeError(f"failed to start monitor_record; tried={tried!r}; last={last_err}")

    h = RecordingHandle(
        started_at=time.time(),
        duration_s=dur,
        channels=ch,
        samplerate=fs,
        _proc=proc,
        _client=c,
    )

    if blocking:
        target_frames = int(np.round(dur * float(fs)))
        target_frames = max(target_frames, 1)
        chunks = []
        got_frames = 0
        timeout_at = time.time() + dur + 15.0
        try:
            # Do not stop by wall-clock here. Instead, read until we collected enough frames.
            # This keeps rec_buf/WAV duration stable even if engine-stop or timing jitters.
            while got_frames < target_frames and time.time() < timeout_at:
                x, eof = h.read(4096)
                if x.size:
                    chunks.append(x)
                    got_frames += int(x.shape[0])
                if eof and x.size == 0:
                    break
                time.sleep(0.005)

            # Stop the session, then drain remaining capture briefly (best-effort).
            try:
                c.request({"cmd": "session_stop"})
            except Exception:
                pass

            drain_deadline = time.time() + 2.0
            while time.time() < drain_deadline:
                x, eof = h.read(4096)
                if x.size:
                    chunks.append(x)
                    got_frames += int(x.shape[0])
                if eof and x.size == 0:
                    break
                time.sleep(0.005)
        finally:
            try:
                c.close()
            finally:
                if proc is not None:
                    try:
                        proc.terminate()
                    except Exception:
                        pass

        if not chunks:
            y = np.zeros((target_frames, ch), dtype=np.float32)
        else:
            y = np.concatenate(chunks, axis=0)
            if y.shape[0] < target_frames:
                pad = np.zeros((target_frames - y.shape[0], y.shape[1]), dtype=np.float32)
                y = np.concatenate([y, pad], axis=0)
            elif y.shape[0] > target_frames:
                y = y[:target_frames]

        if save_wav and wav_path_abs:
            _write_wav_from_float32(wav_path_abs, y, fs)
        return y

    # Non-blocking: auto-stop by closing the session after duration_s.
    def _auto_stop() -> None:
        try:
            time.sleep(dur)
            h.stop()
        except Exception:
            pass

    import threading

    threading.Thread(target=_auto_stop, daemon=True).start()
    return h


def _write_wav_from_float32(path: str, y: np.ndarray, sr: int) -> None:
    """Write a float32 audio array to a 16-bit PCM WAV file (atomic replace).

    Args:
        path (str): Destination path (will be replaced atomically).
        y (np.ndarray): Audio array. Shape `(frames,)` or `(frames, channels)`. Values should
            be in [-1.0, 1.0] (will be clipped).
        sr (int): Samplerate in Hz.

    Raises:
        ValueError: If `y` is not 1D/2D, or `sr<=0`.
        OSError: If writing or replacing the file fails.
    """
    x = np.asarray(y, dtype=np.float32)
    if x.ndim == 1:
        x = x[:, None]
    if x.ndim != 2:
        raise ValueError("audio array must be 1D or 2D")
    if int(sr) <= 0:
        raise ValueError("samplerate must be > 0")
    if x.shape[0] > 0:
        ch = int(x.shape[1])
    else:
        ch0 = None
        try:
            ch0 = default.channels[0] if getattr(default, "channels", None) is not None else None
        except Exception:
            ch0 = None
        ch = int(ch0) if ch0 else 1
    ch = max(ch, 1)

    pcm16 = np.clip(x, -1.0, 1.0)
    pcm16 = (pcm16 * 32767.0).astype(np.int16, copy=False)
    tmp_path = f"{path}.tmp"
    try:
        with wave.open(tmp_path, "wb") as wf:
            wf.setnchannels(ch)
            wf.setsampwidth(2)
            wf.setframerate(int(sr))
            wf.writeframes(pcm16.tobytes())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def _rec_engine(
    frames: Union[int, float],
    *,
    wav_path: str = "",
    save_wav: bool = False,
    blocking: bool = True,
    samplerate: Optional[int] = None,
    channels: Optional[int] = None,
    hostapi: Optional[str] = None,
    device_in: Optional[int] = None,
    rb_seconds: Optional[int] = None,
) -> Union[np.ndarray, RecordingHandle]:
    """Start an engine recording session.

    Args:
        frames (int | float): If int, number of frames to record. If float, duration seconds.
        wav_path (str): WAV output path when `save_wav=True`.
        save_wav (bool): Whether to write the recording to a WAV file.
        blocking (bool): If True, return the recorded audio array; otherwise return a handle.
        samplerate (Optional[int]): Samplerate in Hz.
        channels (Optional[int]): Number of input channels.
        hostapi (Optional[str]): Host API display name.
        device_in (Optional[int]): Input device index (global index from query_devices()). None = use default. Only int accepted.
        rb_seconds (Optional[int]): Ringbuffer size in seconds.

    Returns:
        np.ndarray | RecordingHandle: Recorded audio (float32) if blocking; otherwise a handle.

    Raises:
        ValueError: If `save_wav=True` and `wav_path` is empty.
        RuntimeError: If the engine fails to start the session.
    """
    proc = _ensure_engine_running()

    fs = int(samplerate if samplerate is not None else (default.samplerate if default.samplerate is not None else _DEFAULT_SR_FALLBACK))
    ch_default = default.channels[0] if getattr(default, "channels", None) is not None else None
    ch = int(channels if channels is not None else (int(ch_default) if ch_default is not None else 1))
    duration_s = float(frames) / float(fs) if isinstance(frames, int) else float(frames)

    if save_wav and not wav_path:
        raise ValueError("save_wav=True requires a non-empty wav_path")

    wav_path_abs = os.path.abspath(wav_path) if (save_wav and wav_path) else ""

    rb_send = int(rb_seconds if rb_seconds is not None else default.rb_seconds)
    if blocking:
        # Reduce risk of capture overruns for longer blocking recordings.
        rb_send = max(rb_send, int(np.ceil(duration_s)) + 1, 2)

    hostapi_eff, dev_in, _dev_out = _resolve_hostapi_and_devices(
        hostapi=hostapi,
        device_in=device_in,
        device_out=None,
    )
    backend_eff, engine_hostapi, _ = _hostapi_display_to_engine(hostapi_eff)

    c = AudioDeviceClient(default.host, default.port, timeout=default.timeout)
    c.request(
        {
            "cmd": "session_start",
            "backend": backend_eff,
            "hostapi": engine_hostapi,
            "mode": "record",
            "sr": fs,
            "in_ch": ch,
            "out_ch": 0,
            "device_in": dev_in,
            "device_out": "",
            "duration_s": duration_s,
            "rotate_s": 0,
            "path": wav_path_abs,
            "return_audio": True,
            "rb_seconds": rb_send,
        }
    )

    h = RecordingHandle(
        started_at=time.time(),
        duration_s=duration_s,
        channels=ch,
        samplerate=fs,
        _proc=proc,
        _client=c,
    )
    if blocking:
        y = h.wait()
        # For int `frames`, match the requested length exactly. In practice, the engine may
        # return +/- a few frames due to buffering/timing; pad or trim to keep `rec()` stable.
        if isinstance(frames, int) and int(frames) > 0:
            target = int(frames)
            if y.ndim == 1:
                y = y[:, None]
            if y.shape[0] < target:
                pad = np.zeros((target - y.shape[0], int(y.shape[1]) if y.ndim == 2 else ch), dtype=np.float32)
                y = np.concatenate([y, pad], axis=0)
            elif y.shape[0] > target:
                y = y[:target]
        if save_wav and wav_path_abs:
            _write_wav_from_float32(wav_path_abs, y, fs)
        return y
    return h


def _play_engine(
    y: np.ndarray,
    *,
    blocking: bool = True,
    samplerate: Optional[int] = None,
    hostapi: Optional[str] = None,
    device_out: Optional[int] = None,
    rb_seconds: Optional[int] = None,
    chunk_frames: int = 4096,
) -> None:
    """Play audio through the engine.

    Args:
        y (np.ndarray): Audio array (float32). Shape `(frames,)` or `(frames, channels)`.
        blocking (bool): If True, wait until playback finishes.
        samplerate (Optional[int]): Samplerate in Hz.
        hostapi (Optional[str]): Host API display name.
        device_out (Optional[int]): Output device index (global index from query_devices()). None = use default. Only int accepted.
        rb_seconds (Optional[int]): Engine ringbuffer size in seconds.
        chunk_frames (int): Chunk size (frames) per network write.

    Raises:
        RuntimeError: If the engine rejects the session or I/O fails.
    """
    proc = _ensure_engine_running()
    _ = proc

    fs = int(samplerate if samplerate is not None else (default.samplerate if default.samplerate is not None else _DEFAULT_SR_FALLBACK))
    y = np.asarray(y, dtype=np.float32)
    if y.ndim == 1:
        y = y[:, None]
    frames, ch = y.shape

    hostapi_eff, _dev_in, dev_out = _resolve_hostapi_and_devices(
        hostapi=hostapi,
        device_in=None,
        device_out=device_out,
    )
    backend_eff, engine_hostapi, _ = _hostapi_display_to_engine(hostapi_eff)

    c = AudioDeviceClient(default.host, default.port, timeout=default.timeout)
    c.request(
        {
            "cmd": "session_start",
            "backend": backend_eff,
            "hostapi": engine_hostapi,
            "mode": "play",
            "sr": fs,
            "in_ch": 0,
            "out_ch": int(ch),
            "device_in": "",
            "device_out": dev_out,
            # Do not use duration-based auto-stop for playback.
            "duration_s": 0,
            "rotate_s": 0,
            "path": "",
            "play_path": "",
            "return_audio": False,
            "rb_seconds": int(rb_seconds if rb_seconds is not None else default.rb_seconds),
        }
    )

    try:
        for i in range(0, frames, int(chunk_frames)):
            blk = y[i : i + int(chunk_frames)]
            off = 0
            while off < int(blk.shape[0]):
                sub = blk[off:]
                pcm16 = np.clip(sub, -1.0, 1.0)
                pcm16 = (pcm16 * 32767.0).astype(np.int16)
                b64 = base64.b64encode(pcm16.tobytes()).decode("ascii")
                r = c.request({"cmd": "play_write", "pcm16_b64": b64})
                accepted = int(r.get("accepted_frames", int(sub.shape[0])))
                if accepted <= 0:
                    time.sleep(0.002)
                    continue
                off += accepted
        c.request({"cmd": "play_finish"})
        if blocking:
            _wait_session_end(c, timeout_s=float(frames) / float(fs) + 2.0)
    finally:
        try:
            c.request({"cmd": "session_stop"})
        except Exception:
            pass
        c.close()


def _playrec_engine(
    y: np.ndarray,
    *,
    wav_path: str = "",
    save_wav: bool = False,
    blocking: bool = True,
    samplerate: Optional[int] = None,
    in_channels: Optional[int] = None,
    hostapi: Optional[str] = None,
    device_in: Optional[int] = None,
    device_out: Optional[int] = None,
    rb_seconds: Optional[int] = None,
    chunk_frames: int = 4096,
    delay_time_ms=0,
    alignment: bool = False,
    alignment_channel: int = 1,
) -> np.ndarray:
    """Simultaneously play and record (duplex) using the engine.

    Args:
        y (np.ndarray): Output audio array (float32). Shape `(frames,)` or `(frames, out_ch)`.
        wav_path (str): WAV output path when `save_wav=True`.
        save_wav (bool): Whether to save the recorded input to a WAV file.
        blocking (bool): If True, run in a blocking mode and return recorded audio.
        samplerate (Optional[int]): Samplerate in Hz.
        in_channels (Optional[int]): Input channels to record.
        hostapi (Optional[str]): Host API display name.
        device_in (Optional[int]): Input device index (global index from query_devices()). None = use default. Only int accepted.
        device_out (Optional[int]): Output device index. None = use default. Only int accepted.
        rb_seconds (Optional[int]): Engine ringbuffer size in seconds.
        chunk_frames (int): Chunk size (frames) per network write.

    Returns:
        np.ndarray: Recorded input audio as float32 with shape `(frames, in_channels)`.

    Raises:
        ValueError: If `save_wav=True` with an empty `wav_path`.
        RuntimeError: If the engine session cannot be started.
    """
    proc = _ensure_engine_running()
    _ = proc

    fs = int(samplerate if samplerate is not None else (default.samplerate if default.samplerate is not None else _DEFAULT_SR_FALLBACK))
    y = np.asarray(y, dtype=np.float32)
    if y.ndim == 1:
        y = y[:, None]
    frames, out_ch = y.shape
    ch_default = default.channels[0] if getattr(default, "channels", None) is not None else None
    in_ch = int(in_channels if in_channels is not None else (int(ch_default) if ch_default is not None else 1))

    if save_wav and not wav_path:
        raise ValueError("save_wav=True requires a non-empty wav_path")
    wav_path_abs = os.path.abspath(wav_path) if (save_wav and wav_path) else ""

    rb_send = int(rb_seconds if rb_seconds is not None else default.rb_seconds)
    if blocking and rb_seconds is None:
        rb_send = max(rb_send, 8)

    hostapi_eff, dev_in, dev_out = _resolve_hostapi_and_devices(
        hostapi=hostapi,
        device_in=device_in,
        device_out=device_out,
    )
    backend_eff, engine_hostapi, _ = _hostapi_display_to_engine(hostapi_eff)

    c = AudioDeviceClient(default.host, default.port, timeout=default.timeout)
    c.request(
        {
            "cmd": "session_start",
            "backend": backend_eff,
            "hostapi": engine_hostapi,
            "mode": "playrec",
            "sr": fs,
            "in_ch": int(in_ch),
            "out_ch": int(out_ch),
            "device_in": dev_in,
            "device_out": dev_out,
            # Do not use duration-based auto-stop for playrec.
            "duration_s": 0,
            "rotate_s": 0,
            "path": wav_path_abs,
            "play_path": "",
            "return_audio": True,
            "rb_seconds": rb_send,
        }
    )

    delay_ms = 0.0 if delay_time_ms is None else float(delay_time_ms)
    delay_frames = int(round(delay_ms * float(fs) / 1000.0)) if int(fs) > 0 else 0

    chunks = []
    try:
        def _pcm16_to_float32_frames(pcm_bytes: bytes, got_frames: int, got_ch: int) -> np.ndarray:
            """Best-effort decode for engine capture_read payload.

            The engine may occasionally return a pcm length that doesn't match frames*channels
            exactly; handle by truncating/padding and deriving an effective frame count.
            """
            if got_frames <= 0 or got_ch <= 0 or (not pcm_bytes):
                return np.zeros((0, max(0, int(got_ch))), dtype=np.float32)
            s16 = np.frombuffer(pcm_bytes, dtype=np.int16)
            n = int(s16.size)
            ch = int(got_ch)
            if ch <= 0:
                return np.zeros((0, 0), dtype=np.float32)
            expected = int(got_frames) * ch
            if expected <= 0:
                return np.zeros((0, ch), dtype=np.float32)

            if n < expected:
                # Derive an effective frame count that fits.
                frames_eff = n // ch
                if frames_eff <= 0:
                    return np.zeros((0, ch), dtype=np.float32)
                s16 = s16[: frames_eff * ch]
            elif n > expected:
                s16 = s16[:expected]

            x = s16.astype(np.float32) / 32767.0
            # At this point, length is a multiple of ch.
            frames_eff = int(x.size) // ch
            if frames_eff <= 0:
                return np.zeros((0, ch), dtype=np.float32)
            return x.reshape(frames_eff, ch)

        # Optional pre-roll: record before starting playback.
        if delay_frames < 0 and int(in_ch) > 0:
            pre = int(-delay_frames)
            got = 0
            deadline = time.time() + (float(pre) / float(fs) if float(fs) > 0 else 0.0) + 2.0
            while got < pre and time.time() < deadline:
                r = c.request({"cmd": "capture_read", "max_frames": int(min(chunk_frames, pre - got))})
                pcm = base64.b64decode(r.get("pcm16_b64", "") or "")
                got_frames = int(r.get("frames", 0))
                got_ch = int(r.get("channels", 0))
                if got_frames > 0 and got_ch > 0 and pcm:
                    x0 = _pcm16_to_float32_frames(pcm, got_frames, got_ch)
                    if x0.size <= 0:
                        time.sleep(0.002)
                        continue
                    chunks.append(x0)
                    got += int(x0.shape[0])
                else:
                    time.sleep(0.002)

        for i in range(0, frames, int(chunk_frames)):
            blk = y[i : i + int(chunk_frames)]
            off = 0
            while off < int(blk.shape[0]):
                sub = blk[off:]
                pcm16 = np.clip(sub, -1.0, 1.0)
                pcm16 = (pcm16 * 32767.0).astype(np.int16)
                b64 = base64.b64encode(pcm16.tobytes()).decode("ascii")
                r0 = c.request({"cmd": "play_write", "pcm16_b64": b64})
                accepted = int(r0.get("accepted_frames", int(sub.shape[0])))
                if accepted <= 0:
                    if blocking:
                        # Keep draining capture while output buffer is full.
                        try:
                            _ = c.request({"cmd": "capture_read", "max_frames": int(chunk_frames)})
                        except Exception:
                            pass
                    time.sleep(0.002)
                    continue
                off += accepted

            if blocking:
                r = c.request({"cmd": "capture_read", "max_frames": int(chunk_frames)})
                pcm = base64.b64decode(r["pcm16_b64"])
                got_frames = int(r["frames"])
                got_ch = int(r["channels"])
                if got_frames > 0 and got_ch > 0:
                    x = _pcm16_to_float32_frames(pcm, got_frames, got_ch)
                    if x.size > 0:
                        chunks.append(x)

        c.request({"cmd": "play_finish"})

        if blocking:
            t_end = time.time() + float(frames) / float(fs) + 2.0
            while time.time() < t_end:
                r = c.request({"cmd": "capture_read", "max_frames": int(chunk_frames)})
                pcm = base64.b64decode(r["pcm16_b64"])
                got_frames = int(r["frames"])
                got_ch = int(r["channels"])
                eof = bool(r.get("eof"))
                if got_frames > 0 and got_ch > 0:
                    x = _pcm16_to_float32_frames(pcm, got_frames, got_ch)
                    if x.size > 0:
                        chunks.append(x)
                if eof and got_frames == 0:
                    break
                time.sleep(0.005)
            _wait_session_end(c, timeout_s=1.0)
    finally:
        try:
            c.request({"cmd": "session_stop"})
        except Exception:
            pass
        c.close()

    if int(frames) <= 0:
        return np.zeros((0, int(in_ch)), dtype=np.float32)

    if not chunks:
        x = np.zeros((0, int(in_ch)), dtype=np.float32)
    else:
        x = np.concatenate(chunks, axis=0)

    # Ensure (frames, in_ch).
    if x.ndim == 1:
        x = x[:, None]
    if x.shape[1] != int(in_ch):
        # Best-effort: pad or truncate channels to match requested in_ch.
        if x.shape[1] < int(in_ch):
            pad_ch = np.zeros((x.shape[0], int(in_ch) - x.shape[1]), dtype=np.float32)
            x = np.concatenate([x, pad_ch], axis=1)
        else:
            x = x[:, : int(in_ch)]

    if alignment:
        # Align recording using one selected input channel, then apply the same
        # time shift to all input channels. This intentionally ignores delay_time windowing.
        try:
            from .alignment_processing import AlignmentProcessing

            stim = np.asarray(y, dtype=np.float32)
            if stim.ndim == 2 and stim.shape[1] > 1:
                stim_mono = np.mean(stim, axis=1)
            else:
                stim_mono = stim.reshape(-1)

            rec = np.asarray(x, dtype=np.float32)
            if rec.ndim == 1:
                rec = rec[:, None]

            # alignment_channel is 1-based (1=CH1).
            ci_ref = int(alignment_channel) - 1
            if ci_ref < 0:
                ci_ref = 0
            if ci_ref >= int(in_ch):
                ci_ref = int(in_ch) - 1 if int(in_ch) > 0 else 0
            rec_ref = rec[:, ci_ref].reshape(-1)

            align_frames, _, _ = AlignmentProcessing.gcc_phat(stim_mono, rec_ref)
            start = int(align_frames)
            if start < 0:
                start = 0
            end = start + int(frames)
            if end > rec.shape[0]:
                end = int(rec.shape[0])
            x = rec[start:end, :]
            if x.shape[0] < int(frames):
                pad = np.zeros((int(frames) - x.shape[0], int(in_ch)), dtype=np.float32)
                x = np.concatenate([x, pad], axis=0)
        except Exception:
            # Best-effort fallback: if alignment fails, keep old behavior.
            start = int(delay_frames) if int(delay_frames) > 0 else 0
            need = int(start) + int(frames)
            if x.shape[0] < need:
                pad = np.zeros((need - x.shape[0], int(in_ch)), dtype=np.float32)
                x = np.concatenate([x, pad], axis=0)
            x = x[start : start + int(frames)]
    else:
        # Apply delay windowing.
        start = int(delay_frames) if int(delay_frames) > 0 else 0
        need = int(start) + int(frames)
        if x.shape[0] < need:
            pad = np.zeros((need - x.shape[0], int(in_ch)), dtype=np.float32)
            x = np.concatenate([x, pad], axis=0)
        x = x[start : start + int(frames)]

    # Ensure saved WAV matches returned array length and samplerate.
    if save_wav and wav_path_abs:
        _write_wav_from_float32(wav_path_abs, x, fs)
    return x


def _hostapi_name_from_any(value) -> str:
    """Normalize a host API selector to a display name.

    Args:
        value (Any): None, int hostapi index, or str hostapi name.

    Returns:
        str: Host API display name (empty string if value is None).

    Raises:
        TypeError: If `value` is not int/str/None.
    """
    if value is None:
        return ""
    if isinstance(value, int):
        return str(query_hostapis(int(value)).get("name", ""))
    if isinstance(value, str):
        return value.strip()
    raise TypeError("hostapi must be int, str, or None")


def _device_index_from_any(value, which: str) -> Optional[int]:
    """Extract a device index from an audiodevice-style `device` argument.

    Args:
        value (Any): None, an int device index, or a 2-tuple/list `(in_idx, out_idx)`.
        which (str): "input" or "output".

    Returns:
        Optional[int]: The selected index, or None if not provided/derivable.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return int(value)
    if isinstance(value, (list, tuple)) and len(value) == 2:
        v = value[0] if which == "input" else value[1]
        return None if v is None else int(v)
    return None


def _device_name_from_index(idx: int) -> Tuple[str, str]:
    """Resolve `(hostapi_name, device_name)` from a global device index.

    Args:
        idx (int): Global device index as returned by `query_devices()`.

    Returns:
        tuple[str, str]: `(hostapi_name, device_name)`.
    """
    d = query_devices(int(idx))
    hostapi_name = str(query_hostapis(int(d.get("hostapi", -1))).get("name", ""))
    return hostapi_name, str(d.get("name", ""))


def _remix_channels(y: np.ndarray, target_channels: int) -> np.ndarray:
    """Coerce audio array to a desired channel count.

    Rules:
      - If target==1: downmix by averaging all channels.
      - If source==1 and target>1: duplicate mono to target channels.
      - If source>target: truncate to the first target channels.
      - If source<target and source>1: tile channels cyclically to reach target.
    """
    y = np.asarray(y, dtype=np.float32)
    if y.ndim == 1:
        y = y[:, None]
    t = int(target_channels)
    if t <= 0:
        raise ValueError("channels must be a positive int")
    src = int(y.shape[1])
    if src == t:
        return y
    if t == 1:
        return np.mean(y, axis=1, keepdims=True).astype(np.float32, copy=False)
    if src == 1:
        return np.repeat(y, t, axis=1)
    if src > t:
        return y[:, :t]
    reps = (t + src - 1) // src
    return np.tile(y, (1, reps))[:, :t].astype(np.float32, copy=False)


def play(data, samplerate=None, mapping=None, blocking=False, loop=False, **kwargs) -> None:
    """Play audio data (audiodevice-compatible).

    Args:
        data: Audio to play. Format: array-like, shape (frames,) or (frames, channels);
            converted to float32 internally; each column is one channel.
        samplerate: Sample rate in Hz. Uses default.samplerate if None.
            Format: int or float, e.g. 44100, 48000.
        mapping: 1-based channel mapping to select/reorder output channels from data columns.
            Format: non-empty sequence of 1-based channel indices, e.g. [1, 2] or [2, 1].
        blocking: If True, block until playback finishes. If False, start playback in a
            background thread and return immediately.
        loop: Whether to loop (not supported; kept for API compatibility).
        **kwargs: Optional compatibility args:
            - device (int|tuple[int,int]|None): Device index or (input_index, output_index).
            - channels (int|None): Force output channel count (e.g. 1 or 2). If the selected
              output device supports fewer channels, audio is automatically downmixed/truncated.
            - rb_seconds (int|None): Engine ring buffer size in seconds.
            - chunk_frames (int): Frames per write; default 4096.

    Raises:
        NotImplementedError: If `loop=True`.
        ValueError: If `mapping` is invalid.
        RuntimeError: If playback fails in the engine.
    """
    if mapping is not None:
        # Minimal mapping: reorder/select output channels from data columns (1-based like audiodevice).
        m = list(mapping) if isinstance(mapping, (list, tuple)) else None
        if not m:
            raise ValueError("mapping must be a non-empty sequence")
        y0 = np.asarray(data, dtype=np.float32)
        if y0.ndim == 1:
            y0 = y0[:, None]
        cols = []
        for ch in m:
            ci = int(ch) - 1
            if ci < 0 or ci >= int(y0.shape[1]):
                raise ValueError("mapping channel out of range")
            cols.append(ci)
        data = y0[:, cols]
    if loop:
        raise NotImplementedError("loop is not supported in audiodevice.play()")

    if "hostapi" in kwargs:
        raise TypeError(
            "play() does not accept hostapi; set ad.default.device (or ad.default.device_out) to choose host API"
        )
    device_kw = kwargs.pop("device", None) if "device" in kwargs else None
    channels_kw = kwargs.pop("channels", None) if "channels" in kwargs else None
    rb_seconds = kwargs.pop("rb_seconds", None)
    chunk_frames = int(kwargs.pop("chunk_frames", 4096))

    fs = float(samplerate) if samplerate is not None else (default.samplerate if default.samplerate is not None else _DEFAULT_SR_FALLBACK)

    y = np.asarray(data, dtype=np.float32)
    if y.ndim == 1:
        y = y[:, None]

    dev_out_name = ""
    hostapi_name = str(getattr(default, "hostapi_name", "") or "MME")

    out_idx = _device_index_from_any(device_kw, "output")
    if out_idx is None:
        try:
            out_idx = int(default.device[1])
        except Exception:
            out_idx = None
    if out_idx is not None and out_idx >= 0:
        hostapi_name, dev_out_name = _device_name_from_index(int(out_idx))

    if channels_kw is not None:
        y = _remix_channels(y, int(channels_kw))

    # Guard against requesting more channels than the selected output device supports.
    if out_idx is not None and out_idx >= 0:
        try:
            max_out = int(query_devices(int(out_idx)).get("max_output_channels", 0) or 0)
        except Exception:
            max_out = 0
        if max_out > 0 and int(y.shape[1]) > max_out:
            y = _remix_channels(y, max_out)

    if not blocking:
        stop_ev = threading.Event()
        done_ev = threading.Event()

        def _worker() -> None:
            global _GLOBAL_SESSION_ERROR
            try:
                # Best-effort interruption: if stop is requested, just ask engine to stop.
                _play_engine(
                    y,
                    blocking=True,
                    samplerate=int(fs),
                    hostapi=hostapi_name,
                    device_out=dev_out_name,
                    rb_seconds=rb_seconds,
                    chunk_frames=chunk_frames,
                )
            except BaseException as e:
                with _GLOBAL_SESSION_LOCK:
                    _GLOBAL_SESSION_ERROR = e
            finally:
                done_ev.set()

        t = threading.Thread(target=_worker, daemon=True)
        with _GLOBAL_SESSION_LOCK:
            global _GLOBAL_SESSION_THREAD, _GLOBAL_SESSION_STOP, _GLOBAL_SESSION_DONE, _GLOBAL_SESSION_ERROR, _GLOBAL_STREAM
            _GLOBAL_SESSION_THREAD = t
            _GLOBAL_SESSION_STOP = stop_ev
            _GLOBAL_SESSION_DONE = done_ev
            _GLOBAL_SESSION_ERROR = None
            _GLOBAL_STREAM = None
        t.start()
        return

    _play_engine(
        y,
        blocking=True,
        samplerate=int(fs),
        hostapi=hostapi_name,
        device_out=dev_out_name,
        rb_seconds=rb_seconds,
        chunk_frames=chunk_frames,
    )


def rec(
    frames=None,
    samplerate=None,
    channels=None,
    out=None,
    mapping=None,
    blocking=False,
    delay_time=0,
    **kwargs,
):
    """Record audio (audiodevice-compatible).

    Args:
        frames: Number of frames to record (one sample per channel per frame). Inferred from
            out.shape[0] if None. Format: non-negative int; can be omitted if out is given.
        samplerate: Sample rate in Hz. Uses default.samplerate if None.
            Format: int or float, e.g. 44100.
        channels: Number of input channels to record. Inferred from out or default.channels[0].
            Format: positive int, e.g. 1, 2.
        out: Pre-allocated array for recording; shape must be (frames, channels).
            Format: np.ndarray shape (frames, channels); if provided, frames can be omitted.
        mapping: 1-based input channel mapping; when given, channels=len(mapping).
            Format: non-empty sequence of 1-based channel indices, e.g. [1, 2].
        blocking: If True, record synchronously and return. If False, return immediately and
            fill out in a background thread (errors not raised).
        delay_time: Delay before starting recording, in milliseconds. The returned recording
            length still matches `frames` exactly (the whole capture window shifts later).
            Format: non-negative float/int (ms).
        **kwargs: Optional compatibility args:
            - device (int|tuple[int,int]|None): Device index or (in_idx, out_idx).
            - wav_path (str): WAV file path when save_wav=True (required then).
            - save_wav (bool): Whether to also write a WAV file.
            - rb_seconds (int|None): Engine ring buffer size in seconds.

    Returns:
        np.ndarray: Recorded audio (same object as out when out is provided).

    Raises:
        ValueError: If parameters are inconsistent (e.g. `frames` missing with `out=None`).
    """
    if mapping is not None:
        # Minimal mapping: record specific input channels into output columns (1-based).
        m = list(mapping) if isinstance(mapping, (list, tuple)) else None
        if not m:
            raise ValueError("mapping must be a non-empty sequence")
        # We'll record len(mapping) channels and return those columns.
        channels = len(m)

    if frames is None:
        if out is None:
            raise ValueError("frames must be given when out is None")
        frames = int(np.asarray(out).shape[0])
    frames_i = int(frames)
    if frames_i < 0:
        raise ValueError("frames must be >= 0")

    if "hostapi" in kwargs:
        raise TypeError(
            "rec() does not accept hostapi; set ad.default.device (or ad.default.device_in) to choose host API"
        )
    device_kw = kwargs.pop("device", None) if "device" in kwargs else None
    wav_path = str(kwargs.pop("wav_path", "") or "")
    save_wav = bool(kwargs.pop("save_wav", False))
    rb_seconds = kwargs.pop("rb_seconds", None)

    if save_wav and not wav_path:
        raise ValueError("save_wav=True requires wav_path")

    fs = float(samplerate) if samplerate is not None else (default.samplerate if default.samplerate is not None else _DEFAULT_SR_FALLBACK)

    if channels is None:
        if out is not None and np.asarray(out).ndim >= 2:
            channels = int(np.asarray(out).shape[1])
        else:
            ch_def = default.channels[0]
            channels = int(ch_def) if ch_def is not None else 1
    ch_i = int(channels)

    if out is None:
        out_arr = np.zeros((frames_i, ch_i), dtype=np.float32)
    else:
        out_arr = np.asarray(out)
        if out_arr.dtype != np.float32:
            raise TypeError("rec() only supports float32 output; provide out with dtype=np.float32")
        if out_arr.shape[0] != frames_i:
            raise ValueError("out has incompatible number of frames")
        if out_arr.ndim != 2 or int(out_arr.shape[1]) != int(ch_i):
            raise ValueError("out must have shape (frames, channels)")

    # Use device index so _rec_engine can resolve to name for the engine (device_in must be int).
    in_idx = _device_index_from_any(device_kw, "input")
    if in_idx is None:
        def_in = int(getattr(default, "device_in_override", -1))
        if def_in >= 0:
            in_idx = def_in
        else:
            try:
                in_idx = int(default.device[0])
            except Exception:
                in_idx = None

    def _do_record() -> np.ndarray:
        delay_ms = 0.0 if delay_time is None else float(delay_time)
        if delay_ms < 0:
            raise ValueError("delay_time must be >= 0 (milliseconds)")
        if delay_ms > 0:
            time.sleep(float(delay_ms) / 1000.0)
        y = _rec_engine(
            frames_i,
            wav_path=wav_path,
            save_wav=save_wav,
            blocking=True,
            samplerate=int(fs),
            channels=int(ch_i),
            hostapi=None,
            device_in=in_idx,
            rb_seconds=rb_seconds,
        )
        assert isinstance(y, np.ndarray)
        return y

    if not blocking:
        stop_ev = threading.Event()
        done_ev = threading.Event()

        def _worker() -> None:
            try:
                y = _do_record()
                out_arr[...] = y
            except Exception:
                # match audiodevice behavior: errors surface when waiting; we have no wait()
                pass
            finally:
                done_ev.set()

        t = threading.Thread(target=_worker, daemon=True)
        with _GLOBAL_SESSION_LOCK:
            global _GLOBAL_SESSION_THREAD, _GLOBAL_SESSION_STOP, _GLOBAL_SESSION_DONE, _GLOBAL_SESSION_ERROR, _GLOBAL_STREAM
            _GLOBAL_SESSION_THREAD = t
            _GLOBAL_SESSION_STOP = stop_ev
            _GLOBAL_SESSION_DONE = done_ev
            _GLOBAL_SESSION_ERROR = None
            _GLOBAL_STREAM = None
        t.start()
        return out_arr

    y = _do_record()
    out_arr[...] = y
    return out_arr


def playrec(
    data,
    samplerate=None,
    channels=None,
    out=None,
    input_mapping=None,
    output_mapping=None,
    blocking=False,
    delay_time=0,
    alignment: bool = False,
    alignment_channel: int = 1,
    **kwargs,
):
    """Simultaneously play and record (audiodevice-compatible).

    Args:
        data: Audio to play while recording from the input device.
            Format: array-like, shape (frames,) or (frames, channels); converted to float32.
        samplerate: Sample rate in Hz. Uses default.samplerate if None.
            Format: int or float, e.g. 44100.
        channels: Number of input channels to record; default default.channels[0] or 1.
            Format: positive int.
        out: Pre-allocated array for recorded audio; shape (frames, channels), frames = data rows.
            Format: np.ndarray or None.
        input_mapping: 1-based input channel mapping; when given, channels=len(input_mapping).
            Format: non-empty sequence, e.g. [1, 2].
        output_mapping: 1-based output channel mapping applied to data columns.
            Format: non-empty sequence, e.g. [1, 2] or [2, 1].
        blocking: If True, run synchronously and return recorded array. If False, run in background
            and return immediately.
        delay_time: Recording delay relative to playback, in milliseconds.
            - delay_time > 0: recording starts later (recorded window shifts later)
            - delay_time < 0: recording starts earlier (captures pre-roll before playback)
            The returned array length is always `len(data)` frames.
            Format: float/int (ms).
        **kwargs: Optional compatibility args:
            - device (int|tuple[int,int]|None): Device index or (in_idx, out_idx).
            - wav_path (str): WAV path for recorded audio when save_wav=True (required then).
            - save_wav (bool): Whether to save recorded audio to WAV.
            - rb_seconds (int|None): Engine ring buffer size in seconds.
            - chunk_frames (int): Frames per write.

    Returns:
        np.ndarray: Recorded audio (same object as out when out is provided).
    """
    if input_mapping is not None:
        m = list(input_mapping) if isinstance(input_mapping, (list, tuple)) else None
        if not m:
            raise ValueError("input_mapping must be a non-empty sequence")
        channels = len(m)
    if output_mapping is not None:
        m = list(output_mapping) if isinstance(output_mapping, (list, tuple)) else None
        if not m:
            raise ValueError("output_mapping must be a non-empty sequence")
        y_out0 = np.asarray(data, dtype=np.float32)
        if y_out0.ndim == 1:
            y_out0 = y_out0[:, None]
        cols = []
        for ch in m:
            ci = int(ch) - 1
            if ci < 0 or ci >= int(y_out0.shape[1]):
                raise ValueError("output_mapping channel out of range")
            cols.append(ci)
        data = y_out0[:, cols]

    if "hostapi" in kwargs:
        raise TypeError(
            "playrec() does not accept hostapi; set ad.default.device to choose host API for duplex"
        )
    # Compatibility: allow `in_channels` kwarg (documented in dist/API_USAGE.md and examples).
    if "in_channels" in kwargs and channels is None:
        channels = kwargs.pop("in_channels", None)
    else:
        _ = kwargs.pop("in_channels", None)
    device_kw = kwargs.pop("device", None) if "device" in kwargs else None
    wav_path = str(kwargs.pop("wav_path", "") or "")
    save_wav = bool(kwargs.pop("save_wav", False))
    rb_seconds = kwargs.pop("rb_seconds", None)
    chunk_frames = int(kwargs.pop("chunk_frames", 4096))

    if save_wav and not wav_path:
        raise ValueError("save_wav=True requires wav_path")

    fs = float(samplerate) if samplerate is not None else (default.samplerate if default.samplerate is not None else _DEFAULT_SR_FALLBACK)

    y_out = np.asarray(data, dtype=np.float32)
    if y_out.ndim == 1:
        y_out = y_out[:, None]
    frames_i = int(y_out.shape[0])

    if channels is None:
        ch_def = default.channels[0]
        channels = int(ch_def) if ch_def is not None else 1
    in_ch = int(channels)

    if out is None:
        out_arr = np.zeros((frames_i, in_ch), dtype=np.float32)
    else:
        out_arr = np.asarray(out)
        if out_arr.dtype != np.float32:
            raise TypeError("playrec() only supports float32 output; provide out with dtype=np.float32")
        if out_arr.shape[0] != frames_i:
            raise ValueError("out has incompatible number of frames")
        if out_arr.ndim != 2 or int(out_arr.shape[1]) != int(in_ch):
            raise ValueError("out must have shape (frames, channels)")

    hostapi_name = str(getattr(default, "hostapi_name", "") or "MME")
    dev_in_name = ""
    dev_out_name = ""

    in_idx = _device_index_from_any(device_kw, "input")
    out_idx = _device_index_from_any(device_kw, "output")
    if in_idx is None:
        try:
            in_idx = int(default.device[0])
        except Exception:
            in_idx = None
    if out_idx is None:
        try:
            out_idx = int(default.device[1])
        except Exception:
            out_idx = None
    if in_idx is not None and in_idx >= 0:
        hostapi_name, dev_in_name = _device_name_from_index(int(in_idx))
    if out_idx is not None and out_idx >= 0:
        hostapi2, dev_out_name = _device_name_from_index(int(out_idx))
        if hostapi2 and hostapi_name and hostapi2.strip().upper() != hostapi_name.strip().upper():
            raise ValueError("input/output devices must belong to same hostapi")
        else:
            hostapi_name = hostapi2 or hostapi_name

    def _do_playrec() -> np.ndarray:
        x = _playrec_engine(
            y_out,
            wav_path=wav_path,
            save_wav=save_wav,
            blocking=True,
            samplerate=int(fs),
            in_channels=int(in_ch),
            hostapi=hostapi_name,
            device_in=dev_in_name,
            device_out=dev_out_name,
            rb_seconds=rb_seconds,
            chunk_frames=chunk_frames,
            delay_time_ms=delay_time,
            alignment=bool(alignment),
            alignment_channel=int(alignment_channel),
        )
        return x

    if not blocking:
        stop_ev = threading.Event()
        done_ev = threading.Event()

        def _worker() -> None:
            try:
                x = _do_playrec()
                out_arr[...] = x
            except Exception:
                pass
            finally:
                done_ev.set()

        t = threading.Thread(target=_worker, daemon=True)
        with _GLOBAL_SESSION_LOCK:
            global _GLOBAL_SESSION_THREAD, _GLOBAL_SESSION_STOP, _GLOBAL_SESSION_DONE, _GLOBAL_SESSION_ERROR, _GLOBAL_STREAM
            _GLOBAL_SESSION_THREAD = t
            _GLOBAL_SESSION_STOP = stop_ev
            _GLOBAL_SESSION_DONE = done_ev
            _GLOBAL_SESSION_ERROR = None
            _GLOBAL_STREAM = None
        t.start()
        return out_arr

    x = _do_playrec()
    out_arr[...] = x
    return out_arr


def stream_playrecord(
    data,
    samplerate=None,
    channels=None,
    *,
    blocksize: int = 1024,
    delay_time: float = 0,
    alignment: bool = False,
    alignment_channel: int = 1,
    input_mapping=None,
    output_mapping=None,
    wav_path: str = "",
    save_wav: bool = False,
):
    """Stream-based play+record helper (blocking).

    This helper uses `ad.Stream` (callback streaming) to play `data` while recording input,
    then returns the captured audio as a single ndarray.

    Args:
        data: Audio to play. Shape (frames,) or (frames, out_channels), float32-like.
        samplerate: Sample rate in Hz; defaults to `default.samplerate`.
        channels: Number of input channels to record; defaults to `default.channels[0]` or 1.
        blocksize: Callback block size (frames).
        delay_time: Additional recording delay in milliseconds (>= 0). Implemented as a post-slice.
        alignment: If True, align recorded audio to stimulus using GCC-PHAT (see alignment_processing).
        input_mapping: 1-based input channel mapping; when given, channels=len(input_mapping).
        output_mapping: 1-based output channel mapping applied to data columns.
        wav_path: Path to save WAV when save_wav=True.
        save_wav: Whether to save returned audio to a WAV file.

    Returns:
        np.ndarray: Recorded audio with shape (frames, channels).
    """
    if input_mapping is not None:
        m = list(input_mapping) if isinstance(input_mapping, (list, tuple)) else None
        if not m:
            raise ValueError("input_mapping must be a non-empty sequence")
        channels = len(m)

    y_out0 = np.asarray(data, dtype=np.float32)
    if y_out0.ndim == 1:
        y_out0 = y_out0[:, None]

    if output_mapping is not None:
        m = list(output_mapping) if isinstance(output_mapping, (list, tuple)) else None
        if not m:
            raise ValueError("output_mapping must be a non-empty sequence")
        cols = []
        for ch in m:
            ci = int(ch) - 1
            if ci < 0 or ci >= int(y_out0.shape[1]):
                raise ValueError("output_mapping channel out of range")
            cols.append(ci)
        y_out0 = y_out0[:, cols]

    fs = int(
        float(samplerate)
        if samplerate is not None
        else (
            default.samplerate
            if default.samplerate is not None
            else _DEFAULT_SR_FALLBACK
        )
    )
    frames_i = int(y_out0.shape[0])

    if channels is None:
        ch_def = default.channels[0]
        channels = int(ch_def) if ch_def is not None else 1
    in_ch = int(channels)

    delay_ms = 0.0 if delay_time is None else float(delay_time)
    if delay_ms < 0:
        raise ValueError("delay_time must be >= 0 (milliseconds)")
    delay_frames = int(round(delay_ms * float(fs) / 1000.0)) if fs > 0 else 0

    if save_wav and not wav_path:
        raise ValueError("save_wav=True requires wav_path")
    wav_path_abs = os.path.abspath(wav_path) if (save_wav and wav_path) else ""

    rec_chunks = []
    out_pos = [0]
    captured = [0]
    # When alignment=True we need extra tail capture; otherwise the stimulus may appear
    # late in the recording (device/output latency), causing the aligned segment to
    # run out of data and get zero-padded.
    extra_tail_frames = 0
    if alignment and fs > 0:
        extra_tail_frames = int(fs)  # 1.0s tail capture
    target_capture = int(frames_i + max(0, delay_frames) + max(0, extra_tail_frames))

    def cb(indata, outdata, frames, time_info, status):
        # output
        p0 = int(out_pos[0])
        p1 = p0 + int(frames)
        if p0 < frames_i:
            outdata[:] = 0.0
            outdata[: max(0, min(frames_i - p0, int(frames)))] = y_out0[p0: min(frames_i, p1)]
        else:
            outdata[:] = 0.0
        out_pos[0] = p1

        # input
        if indata.size and int(indata.shape[1]) > 0:
            rec_chunks.append(indata.copy())
            captured[0] += int(indata.shape[0])

        if captured[0] >= target_capture and out_pos[0] >= frames_i:
            raise CallbackStop()

    with Stream(
        samplerate=int(fs),
        channels=(int(in_ch), int(y_out0.shape[1])),
        blocksize=int(blocksize),
        callback=cb,
    ):
        # Busy-wait with sleep; Stream runs in its own worker thread.
        # Give some extra wall-clock slack for scheduling jitter.
        timeout_s = (float(target_capture) / float(fs) if fs > 0 else 0.0) + 2.0
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            if captured[0] >= target_capture and out_pos[0] >= frames_i:
                break
            time.sleep(0.01)

    if rec_chunks:
        rec_raw = np.concatenate(rec_chunks, axis=0)
    else:
        rec_raw = np.zeros((0, int(in_ch)), dtype=np.float32)

    # Ensure channels.
    if rec_raw.ndim == 1:
        rec_raw = rec_raw[:, None]
    if rec_raw.shape[1] != int(in_ch):
        if rec_raw.shape[1] < int(in_ch):
            pad_ch = np.zeros((rec_raw.shape[0], int(in_ch) - rec_raw.shape[1]), dtype=np.float32)
            rec_raw = np.concatenate([rec_raw, pad_ch], axis=1)
        else:
            rec_raw = rec_raw[:, : int(in_ch)]

    if alignment:
        try:
            from .alignment_processing import AlignmentProcessing

            stim = np.asarray(y_out0, dtype=np.float32)
            stim_mono = np.mean(stim, axis=1) if (stim.ndim == 2 and stim.shape[1] > 1) else stim.reshape(-1)

            # alignment_channel is 1-based (1=CH1).
            ci_ref = int(alignment_channel) - 1
            if ci_ref < 0:
                ci_ref = 0
            if ci_ref >= int(in_ch):
                ci_ref = int(in_ch) - 1 if int(in_ch) > 0 else 0
            rec_ref = rec_raw[:, ci_ref].reshape(-1)

            align_frames, _, _ = AlignmentProcessing.gcc_phat(stim_mono, rec_ref)
            start = int(align_frames)
            if start < 0:
                start = 0
            end = start + int(frames_i)
            if end > rec_raw.shape[0]:
                end = int(rec_raw.shape[0])
            x = rec_raw[start:end, :]
        except Exception:
            x = rec_raw
    else:
        x = rec_raw

    # Apply delay windowing (unless alignment already extracted exact segment).
    if not alignment:
        start = int(delay_frames) if int(delay_frames) > 0 else 0
        need = int(start) + int(frames_i)
        if x.shape[0] < need:
            pad = np.zeros((need - x.shape[0], int(in_ch)), dtype=np.float32)
            x = np.concatenate([x, pad], axis=0)
        x = x[start: start + int(frames_i)]
    else:
        if x.shape[0] < int(frames_i):
            pad = np.zeros((int(frames_i) - x.shape[0], int(in_ch)), dtype=np.float32)
            x = np.concatenate([x, pad], axis=0)
        x = x[: int(frames_i)]

    if save_wav and wav_path_abs:
        _write_wav_from_float32(wav_path_abs, x, int(fs))
    return x.astype(np.float32, copy=False)


def _wait_session_end(client: AudioDeviceClient, timeout_s: float) -> None:
    """Poll engine status until the current session ends or timeout elapses.

    Args:
        client (AudioDeviceClient): Connected client instance.
        timeout_s (float): Timeout in seconds.
    """
    deadline = time.time() + float(timeout_s)
    while time.time() < deadline:
        try:
            st = client.request({"cmd": "status"})
        except Exception:
            return
        if not st.get("has_session", True):
            return
        time.sleep(0.02)


class CallbackFlags:
    """Minimal placeholder compatible with common callback usage patterns.

    Attributes are best-effort booleans and may not reflect real driver state in this MVP
    implementation.
    """
    input_underflow = False
    input_overflow = False
    output_underflow = False
    output_overflow = False
    priming_output = False

    # --- audiodevice_py extensions (best-effort) ---
    # Callback timing diagnostics (seconds). Updated by the stream worker loop.
    callback_seconds: float = 0.0
    block_seconds: float = 0.0
    callback_overrun: bool = False
    callback_overrun_ratio: float = 0.0
    callback_overrun_consecutive: int = 0
    callback_overrun_total: int = 0


class CallbackStop(Exception):
    """Raise from a stream callback to stop the stream gracefully."""
    pass


class CallbackAbort(Exception):
    """Raise from a stream callback to abort the stream."""
    pass


class _StreamBase:
    """Base class for callback-driven streaming APIs.

    This is a small subset modeled after `sounddevice.Stream` behavior.
    """
    def __init__(
        self,
        *,
        kind: str,
        samplerate=None,
        blocksize: int = 0,
        latency=None,
        channels=None,
        callback=None,
        delay_time=0,
    ) -> None:
        """Create a stream object (not started yet).

        Args:
            kind: Stream type: "input" (capture only), "output" (playback only), "duplex" (both).
                Format: str, one of "input" | "output" | "duplex".
            samplerate: Sample rate in Hz. Uses default.samplerate if None.
                Format: float or None, e.g. 44100.0.
            blocksize: Frames per callback; 0 uses default (e.g. 1024).
                Format: int.
            latency: Kept for compatibility; low-latency hint (best-effort).
            channels: Channel count. For duplex: int or (in_ch, out_ch). For input/output: int.
                Format: int or (int, int), e.g. 2 or (1, 2).
            callback: Callback with signature (indata, outdata, frames, time_info, status).
                Format: indata (frames, in_ch) float32; outdata (frames, out_ch) float32, write in
                callback; frames int; time_info dict; status CallbackFlags. Raise CallbackStop or
                CallbackAbort to end the stream.
            delay_time: Delay before starting to deliver input to the callback, in milliseconds.
                This is mainly useful for capture-only streams (InputStream). Format: float/int (ms).
        """
        self.kind = str(kind)
        self.samplerate = float(samplerate) if samplerate is not None else (
            default.samplerate if default.samplerate is not None else _DEFAULT_SR_FALLBACK
        )
        self.blocksize = int(blocksize or 1024)
        self.latency = latency
        self.channels = channels
        self.callback = callback
        self.delay_time = 0.0 if delay_time is None else float(delay_time)

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._closed = False
        self._active = False
        self._thread_exception: Optional[BaseException] = None
        # Keep a reference so stop() can break blocking I/O by closing the socket.
        self._client: Optional[AudioDeviceClient] = None

    @property
    def active(self) -> bool:
        return bool(self._active)

    @property
    def closed(self) -> bool:
        return bool(self._closed)

    @property
    def stopped(self) -> bool:
        return (not self._active) and (not self._closed)

    def start(self):
        """Start the stream worker thread.

        Returns:
            _StreamBase: Self.

        Raises:
            RuntimeError: If the stream is closed.
            ValueError: If `callback` is not provided.
        """
        if self._closed:
            raise RuntimeError("Stream is closed")
        if self._active:
            return self
        if self.callback is None:
            raise ValueError("callback is required for Stream API in audiodevice")

        self._stop.clear()
        self._active = True
        self._thread_exception = None

        def _worker() -> None:
            try:
                self._run()
            except BaseException as e:
                self._thread_exception = e
            finally:
                self._active = False

        self._thread = threading.Thread(target=_worker, daemon=True)
        with _GLOBAL_SESSION_LOCK:
            global _GLOBAL_STREAM
            _GLOBAL_STREAM = self
        self._thread.start()
        return self

    def stop(self):
        """Stop the stream worker thread (best-effort).

        Returns:
            _StreamBase: Self.

        Raises:
            RuntimeError: If the worker failed or did not finish in time.
        """
        if self._closed:
            return
        self._stop.set()
        try:
            stop()
        except Exception:
            pass
        # If the worker is blocked waiting for engine response (e.g. session_start on ASIO),
        # closing the socket will interrupt recv() and let the thread unwind.
        try:
            if self._client is not None:
                self._client.close()
        except Exception:
            pass
        if self._thread is not None:
            join_timeout = max(2.0, float(getattr(default, "timeout", 5.0) or 5.0) + 1.0)
            self._thread.join(timeout=join_timeout)
        self._active = False
        if self._thread_exception is not None:
            e = self._thread_exception
            self._thread_exception = None
            raise RuntimeError(f"Stream worker failed: {e}") from e
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError(
                "Stream worker did not finish (likely blocked in device I/O). "
                "Try MME or WASAPI instead of ASIO, or check device/sample rate."
            )
        return self

    def close(self):
        """Stop (if needed) and mark the stream as closed.

        Returns:
            _StreamBase: Self.
        """
        if self._closed:
            return
        try:
            self.stop()
        finally:
            self._closed = True
        return self

    def __enter__(self):
        """Context-manager enter: start the stream."""
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        """Context-manager exit: close the stream."""
        self.close()
        return False

    def _resolve_hostapi_and_devices_for_stream(self) -> Tuple[str, str, str]:
        """Resolve effective hostapi/device names for stream startup.

        Returns:
            tuple[str, str, str]: `(hostapi_name, device_in_name, device_out_name)`.
        """
        hostapi_name = str(getattr(default, "hostapi_name", "") or "MME")

        dev_in = ""
        dev_out = ""
        # Stream device selection is unified: always use global defaults.
        # (Users configure `default.device` / device_in_override / device_out_override.)
        try:
            di_raw, do_raw = default._device_tuple_raw()
            def_in = int(getattr(default, "device_in_override", -1))
            def_out = int(getattr(default, "device_out_override", -1))
            in_idx = def_in if def_in >= 0 else int(di_raw)
            out_idx = def_out if def_out >= 0 else int(do_raw)
        except Exception:
            in_idx = -1
            out_idx = -1

        if int(in_idx) >= 0:
            hostapi_name, dev_in = _device_name_from_index(int(in_idx))
        if int(out_idx) >= 0:
            hostapi2, dev_out = _device_name_from_index(int(out_idx))
            if hostapi2:
                hostapi_name = hostapi2

        return hostapi_name, dev_in, dev_out

    def _run(self) -> None:
        """Worker loop: start an engine session and drive the callback per block.

        Raises:
            RuntimeError: If engine session fails or I/O fails.
        """
        # MVP implementation: use engine playrec mode and call callback per block.
        hostapi_name, dev_in, dev_out = self._resolve_hostapi_and_devices_for_stream()
        backend_eff, engine_hostapi, _disp = _hostapi_display_to_engine(hostapi_name)

        # Channels
        in_ch = 0
        out_ch = 0
        mode = "playrec"
        return_audio = True
        if self.kind == "input":
            in_ch = int(self.channels or (default.channels[0] or 1))
            out_ch = 0
        elif self.kind == "output":
            # OutputStream does not need input; use "play" mode for stability.
            in_ch = 0
            out_ch = int(self.channels or (default.channels[1] or 1))
            mode = "play"
            return_audio = False
        else:
            if isinstance(self.channels, (list, tuple)) and len(self.channels) == 2:
                in_ch = int(self.channels[0] or 1)
                out_ch = int(self.channels[1] or 1)
            else:
                v = int(self.channels or (default.channels[0] or 1))
                in_ch = v
                out_ch = v

        c = AudioDeviceClient(default.host, default.port, timeout=float(default.timeout))
        self._client = c
        try:
            c.request(
                {
                    "cmd": "session_start",
                    "backend": backend_eff,
                    # Engine expects raw hostapi names (e.g. "WASAPI"/"DirectSound"/"MME"),
                    # while our public layer may use audiodevice-like display names (e.g. "Windows WASAPI").
                    "hostapi": engine_hostapi,
                    "mode": mode,
                    "sr": int(self.samplerate),
                    "in_ch": int(in_ch),
                    "out_ch": int(out_ch),
                    "device_in": dev_in,
                    "device_out": dev_out,
                    "duration_s": 0,
                    "rotate_s": 0,
                    "path": "",
                    "play_path": "",
                    "return_audio": return_audio,
                    "rb_seconds": int(default.rb_seconds),
                }
            )

            frames = int(self.blocksize)
            block_dt = float(frames) / float(self.samplerate) if float(self.samplerate) > 0 else 0.0

            # Optional capture delay for InputStream: discard initial frames so the effective
            # recording window starts later, while keeping the same callback frame counts.
            delay_remain = 0
            if self.kind == "input" and int(in_ch) > 0:
                delay_frames = int(round(float(self.delay_time) * float(self.samplerate) / 1000.0))
                if delay_frames > 0:
                    drained = 0
                    # Drain most of the delay in big chunks, but leave <1 block to be
                    # handled inside the main loop. This avoids a systematic +1 block
                    # latency between "drain finished" and "first callback block read".
                    drain_target = max(0, int(delay_frames) - int(frames))
                    deadline = time.time() + (float(delay_frames) / float(self.samplerate) if float(self.samplerate) > 0 else 0.0) + 2.0
                    while drained < drain_target and (not self._stop.is_set()) and time.time() < deadline:
                        # Drain in larger chunks than blocksize to reduce round-trip overhead,
                        # otherwise small blocksize would inflate the observed delay.
                        drain_chunk = max(1024, int(min(4096, drain_target - drained)))
                        r = c.request({"cmd": "capture_read", "max_frames": int(drain_chunk)})
                        got_frames = int(r.get("frames", 0))
                        if got_frames > 0:
                            drained += got_frames
                        else:
                            time.sleep(0.002)
                    delay_remain = max(0, int(delay_frames) - int(drained))

            # For output streams, pre-fill the engine ring buffer before entering
            # the real-time paced loop.  This prevents underruns caused by
            # Python-side timing jitter (GC, scheduling, TCP latency).
            _prefill_n = 0
            if self.kind == "output" and out_ch > 0 and block_dt > 0:
                _prefill_s = min(2.0, float(getattr(default, 'rb_seconds', 2)) * 0.2)
                _prefill_n = max(4, int(_prefill_s / block_dt))

            next_tick = time.perf_counter()
            status = CallbackFlags()
            cb_overrun_consecutive = 0
            cb_overrun_total = 0
            while not self._stop.is_set():
                # Read input
                indata = np.zeros((frames, in_ch), dtype=np.float32)
                if in_ch > 0:
                    # If delay_time is enabled for InputStream, drop initial samples until
                    # delay_remain is consumed, then deliver full blocks to the callback.
                    filled = 0
                    got_ch_eff = 0
                    while filled < frames and (not self._stop.is_set()):
                        r = c.request({"cmd": "capture_read", "max_frames": int(frames - filled)})
                        got_frames = int(r.get("frames", 0))
                        got_ch = int(r.get("channels", 0))
                        if got_frames <= 0 or got_ch <= 0:
                            time.sleep(0.002)
                            continue

                        pcm = base64.b64decode(r.get("pcm16_b64", "") or "")
                        if not pcm:
                            time.sleep(0.002)
                            continue

                        s16 = np.frombuffer(pcm, dtype=np.int16)
                        ch = int(got_ch)
                        if ch <= 0:
                            time.sleep(0.002)
                            continue
                        frames_eff = int(s16.size) // ch
                        if frames_eff <= 0:
                            time.sleep(0.002)
                            continue
                        if frames_eff != int(got_frames):
                            got_frames = int(frames_eff)
                        s16 = s16[: int(got_frames) * ch]
                        x = (s16.astype(np.float32) / 32767.0).reshape(int(got_frames), ch)
                        got_ch_eff = int(ch)

                        if delay_remain > 0:
                            if got_frames <= int(delay_remain):
                                delay_remain -= int(got_frames)
                                # Do not call callback yet; continue draining.
                                continue
                            # Partial drop within this chunk.
                            drop = int(delay_remain)
                            x = x[drop:]
                            got_frames = int(x.shape[0])
                            delay_remain = 0

                        if got_frames <= 0:
                            continue

                        take = int(min(got_frames, frames - filled))
                        indata[filled : filled + take, :take and got_ch_eff] = x[:take]
                        filled += take

                outdata = np.zeros((frames, out_ch), dtype=np.float32)
                tnow = time.time()
                time_info = {
                    "inputBufferAdcTime": tnow,
                    "currentTime": tnow,
                    "outputBufferDacTime": tnow,
                }

                try:
                    cb_t0 = time.perf_counter()
                    self.callback(indata, outdata, frames, time_info, status)
                    cb_s = time.perf_counter() - cb_t0
                except CallbackStop:
                    break
                except CallbackAbort:
                    break
                except Exception as e:
                    raise RuntimeError(f"Error in stream callback: {e}") from e

                # Best-effort callback timing feedback.
                status.callback_seconds = float(cb_s)
                status.block_seconds = float(block_dt)
                if block_dt > 0:
                    ratio = float(cb_s) / float(block_dt) if float(block_dt) > 0 else 0.0
                    overrun = bool(cb_s > block_dt)
                else:
                    ratio = 0.0
                    overrun = False
                status.callback_overrun = overrun
                status.callback_overrun_ratio = float(ratio)
                if overrun:
                    cb_overrun_total += 1
                    cb_overrun_consecutive += 1
                else:
                    cb_overrun_consecutive = 0
                status.callback_overrun_consecutive = int(cb_overrun_consecutive)
                status.callback_overrun_total = int(cb_overrun_total)

                # If callback is consistently far slower than real-time, raise so user sees feedback.
                # Keep thresholds conservative to avoid breaking borderline cases.
                if (
                    block_dt > 0
                    and cb_overrun_consecutive >= 3
                    and ratio >= 2.0
                ):
                    raise RuntimeError(
                        "Stream callback overrun: "
                        f"callback took {cb_s:.3f}s, block is {block_dt:.3f}s (x{ratio:.1f}); "
                        "callback must be non-blocking."
                    )

                # Write output
                if out_ch > 0:
                    off = 0
                    # Engine may accept partial frames when its output buffer is full.
                    # Never drop frames silently: retry until the whole block is accepted.
                    while off < frames and not self._stop.is_set():
                        sub = outdata[off:]
                        pcm16 = np.clip(sub, -1.0, 1.0)
                        pcm16 = (pcm16 * 32767.0).astype(np.int16)
                        b64 = base64.b64encode(pcm16.tobytes()).decode("ascii")
                        # Some backends/devices may report session as inactive briefly
                        # right after session_start; retry a bit to avoid spurious failures.
                        deadline = time.time() + 1.0
                        while True:
                            try:
                                r0 = c.request({"cmd": "play_write", "pcm16_b64": b64})
                                break
                            except RuntimeError as e:
                                msg = str(e)
                                transient = ("no active session" in msg) or ("out_ch must be > 0" in msg)
                                if transient and time.time() < deadline:
                                    time.sleep(0.01)
                                    continue
                                raise
                        accepted = int(r0.get("accepted_frames", int(sub.shape[0])))
                        if accepted <= 0:
                            # For OutputStream we use playrec with a dummy input channel.
                            # If output buffer is full, optionally drain input to avoid input ringbuffer overflow.
                            if self.kind == "output" and in_ch > 0:
                                try:
                                    _ = c.request({"cmd": "capture_read", "max_frames": int(frames)})
                                except Exception:
                                    pass
                            time.sleep(0.002)
                            continue
                        if accepted > int(sub.shape[0]):
                            accepted = int(sub.shape[0])
                        off += accepted

                # Pace the loop roughly in real-time to avoid overfilling engine buffers.
                # During pre-fill phase, skip pacing so blocks are sent as fast as the
                # engine accepts them, building up a safety margin in the ring buffer.
                if _prefill_n > 0:
                    _prefill_n -= 1
                    if _prefill_n == 0:
                        next_tick = time.perf_counter()
                elif block_dt > 0:
                    next_tick += block_dt
                    now = time.perf_counter()
                    sleep_s = next_tick - now
                    if sleep_s > 0:
                        time.sleep(sleep_s)
                    else:
                        # If we're running late, avoid accumulating unbounded lag.
                        next_tick = now
        finally:
            self._client = None
            try:
                c.request({"cmd": "session_stop"})
            except Exception:
                pass
            c.close()


class Stream(_StreamBase):
    """Duplex (input+output) callback stream."""
    def __init__(self, *args, **kwargs) -> None:
        """Create a duplex stream.

        Args:
            *args: Forwarded to `_StreamBase`.
            **kwargs: Forwarded to `_StreamBase` (notably `callback`, `samplerate`, `channels`).
        """
        super().__init__(kind="duplex", *args, **kwargs)


class InputStream(_StreamBase):
    """Input-only callback stream."""
    def __init__(self, *args, **kwargs) -> None:
        """Create an input stream.

        Args:
            *args: Forwarded to `_StreamBase`.
            **kwargs: Forwarded to `_StreamBase` (notably `callback`, `samplerate`, `channels`).
        """
        super().__init__(kind="input", *args, **kwargs)


class OutputStream(_StreamBase):
    """Output-only callback stream."""
    def __init__(self, *args, **kwargs) -> None:
        """Create an output stream.

        Args:
            *args: Forwarded to `_StreamBase`.
            **kwargs: Forwarded to `_StreamBase` (notably `callback`, `samplerate`, `channels`).
        """
        super().__init__(kind="output", *args, **kwargs)


@dataclass
class LongRecordingHandle:
    """Handle for a long-running disk recording session."""
    path: str
    _proc: Optional[subprocess.Popen]
    _client: AudioDeviceClient

    def stop(self) -> None:
        """Stop the long recording session and release resources."""
        try:
            self._client.request({"cmd": "session_stop"})
        finally:
            self._client.close()
            if self._proc is not None:
                try:
                    self._proc.terminate()
                except Exception:
                    pass

    def wait(self) -> str:
        """Return the output path (kept for compatibility)."""
        return self.path


def rec_long(
    path: str,
    *,
    rotate_s: float = 300.0,
    samplerate: Optional[int] = None,
    channels: Optional[int] = None,
    device_in: Optional[int] = None,
    rb_seconds: Optional[int] = None,
) -> LongRecordingHandle:
    """Record continuously to disk and rotate files periodically.

    Args:
        path: Base path for output WAV files; engine writes one file per rotate_s interval.
            Format: str, local file path (naming/placeholders depend on engine).
        rotate_s: Rotation interval in seconds; each segment is a separate file. Default 300.
            Format: float, e.g. 60.0, 300.0.
        samplerate: Sample rate in Hz. Uses default.samplerate if None.
            Format: int or None.
        channels: Number of input channels. Uses default.channels[0] or 1 if None.
            Format: int or None.
        device_in: Input device index (global index from query_devices()). None = use default. Only int accepted.
        rb_seconds: Engine ring buffer size in seconds.
            Format: int or None.

    Returns:
        LongRecordingHandle: Handle; call stop() to stop recording.
    """
    proc = _ensure_engine_running()

    fs = int(samplerate if samplerate is not None else (default.samplerate if default.samplerate is not None else _DEFAULT_SR_FALLBACK))
    ch_def = default.channels[0] if getattr(default, "channels", None) is not None else None
    ch = int(channels if channels is not None else (int(ch_def) if ch_def is not None else 1))

    path_abs = os.path.abspath(path)

    hostapi_eff, dev_in, _dev_out = _resolve_hostapi_and_devices(
        hostapi=None,
        device_in=device_in,
        device_out=None,
    )
    backend_eff, engine_hostapi, _ = _hostapi_display_to_engine(hostapi_eff)

    c = AudioDeviceClient(default.host, default.port, timeout=default.timeout)
    c.request(
        {
            "cmd": "session_start",
            "backend": backend_eff,
            "hostapi": engine_hostapi,
            "mode": "record_long",
            "sr": fs,
            "in_ch": ch,
            "out_ch": 0,
            "device_in": dev_in,
            "device_out": "",
            "duration_s": 0,
            "rotate_s": float(rotate_s),
            "path": path_abs,
            "play_path": "",
            "return_audio": False,
            "rb_seconds": int(rb_seconds if rb_seconds is not None else default.rb_seconds),
        }
    )

    return LongRecordingHandle(path=path_abs, _proc=proc, _client=c)