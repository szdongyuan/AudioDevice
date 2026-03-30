from __future__ import annotations

import base64
import inspect
import os
import socket
import subprocess
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
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

_SESSION_ID_COUNTER = 0
_SESSION_ID_LOCK = threading.Lock()


def _next_session_id() -> str:
    global _SESSION_ID_COUNTER
    with _SESSION_ID_LOCK:
        _SESSION_ID_COUNTER += 1
        return f"py-{os.getpid()}-{_SESSION_ID_COUNTER}"


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
    _session_id: str = ""

    def stop(self) -> None:
        """Stop the recording session and close underlying resources."""
        try:
            self._client.request({"cmd": "session_stop", "session_id": self._session_id})
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
        data = self._client.request({"cmd": "capture_read", "session_id": self._session_id, "max_frames": int(max_frames)})
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
    output_mapping: Optional[Iterable[int]] = None,
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
        output_mapping: Output channel mapping for monitoring (1-based).
            When provided (and monitor_channel is not None), the monitored input channel is routed
            only to the selected output channels; all other output channels are silence.
            Example: [2] routes to right channel only.
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

    monitor_out_idxs: Optional[List[int]] = None
    out_need_ch: Optional[int] = None
    if output_mapping is not None:
        if monitor_in_idx is None:
            raise ValueError("output_mapping requires monitor_channel (choose which input to monitor)")
        m = list(output_mapping) if isinstance(output_mapping, (list, tuple)) else list(output_mapping)
        if not m:
            raise ValueError("output_mapping must be a non-empty sequence (1-based)")
        idxs: List[int] = []
        need = 0
        for chv in m:
            ci = int(chv) - 1
            if ci < 0:
                raise ValueError("output_mapping channels must be >= 1 (1-based)")
            idxs.append(ci)
            need = max(need, ci + 1)
        monitor_out_idxs = idxs
        out_need_ch = int(need) if int(need) > 0 else None

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

    session_id = _next_session_id()
    c = AudioDeviceClient(default.host, default.port, timeout=default.timeout)
    fs = fs0
    ch = ch0
    tried = []
    last_err: Optional[Exception] = None

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
            out_candidates: List[int] = []
            if out_need_ch is not None:
                # Must open enough output channels to reach the requested mapping.
                for v in (out_need_ch, 2, 1, int(in_ch_try)):
                    iv = int(v)
                    if iv <= 0:
                        continue
                    if iv < int(out_need_ch):
                        continue
                    if iv not in out_candidates:
                        out_candidates.append(iv)
            else:
                for v in (2, 1, int(in_ch_try)):
                    iv = int(v)
                    if iv > 0 and iv not in out_candidates:
                        out_candidates.append(iv)

            for out_ch_try in out_candidates:
                if out_ch_try <= 0:
                    continue
                tried.append((int(fs_try), int(in_ch_try), int(out_ch_try)))
                try:
                    req = {
                        "cmd": "session_start",
                        "session_id": session_id,
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
                    if monitor_out_idxs is not None:
                        req["monitor_out_idxs"] = [int(x) for x in monitor_out_idxs]
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
        _session_id=session_id,
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

            try:
                c.request({"cmd": "session_stop", "session_id": session_id})
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

    session_id = _next_session_id()
    c = AudioDeviceClient(default.host, default.port, timeout=default.timeout)
    c.request(
        {
            "cmd": "session_start",
            "session_id": session_id,
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
        _session_id=session_id,
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

    session_id = _next_session_id()
    c = AudioDeviceClient(default.host, default.port, timeout=default.timeout)
    c.request(
        {
            "cmd": "session_start",
            "session_id": session_id,
            "backend": backend_eff,
            "hostapi": engine_hostapi,
            "mode": "play",
            "sr": fs,
            "in_ch": 0,
            "out_ch": int(ch),
            "device_in": "",
            "device_out": dev_out,
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
                r = c.request({"cmd": "play_write", "session_id": session_id, "pcm16_b64": b64})
                accepted = int(r.get("accepted_frames", int(sub.shape[0])))
                if accepted <= 0:
                    time.sleep(0.002)
                    continue
                off += accepted
        c.request({"cmd": "play_finish", "session_id": session_id})
        if blocking:
            _wait_session_end(c, timeout_s=float(frames) / float(fs) + 2.0, session_id=session_id)
    finally:
        try:
            c.request({"cmd": "session_stop", "session_id": session_id})
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
    return_full: bool = False,
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

    session_id = _next_session_id()
    c = AudioDeviceClient(default.host, default.port, timeout=default.timeout)
    try:
        # Some drivers reject certain sr/ch combos in duplex (e.g. 44.1k/1ch),
        # even though other combos work. Try a small set of fallbacks.
        max_in = 0
        dev_in_default_sr = None
        try:
            di = int(device_in) if device_in is not None else int(default.device[0])
            if di >= 0:
                d = query_devices(int(di))
                max_in = int(d.get("max_input_channels", 0) or 0)
                sr = d.get("default_samplerate", None)
                if sr is not None:
                    dev_in_default_sr = int(round(float(sr)))
        except Exception:
            max_in = 0
            dev_in_default_sr = None

        fs_candidates: list[int] = []
        for v in (fs, dev_in_default_sr, 48_000, 44_100, 96_000, 88_200, 32_000):
            if v is None:
                continue
            try:
                iv = int(v)
            except Exception:
                continue
            if iv > 0 and iv not in fs_candidates:
                fs_candidates.append(iv)

        in_candidates: list[int] = []
        for v in (in_ch, 2, 1, max_in):
            try:
                iv = int(v)
            except Exception:
                continue
            if iv <= 0:
                continue
            if max_in > 0 and iv > int(max_in):
                continue
            if iv not in in_candidates:
                in_candidates.append(iv)

        last_err: Optional[Exception] = None
        started = False
        for fs_try in fs_candidates:
            for in_try in in_candidates:
                try:
                    c.request(
                        {
                            "cmd": "session_start",
                            "session_id": session_id,
                            "backend": backend_eff,
                            "hostapi": engine_hostapi,
                            "mode": "playrec",
                            "sr": int(fs_try),
                            "in_ch": int(in_try),
                            "out_ch": int(out_ch),
                            "device_in": dev_in,
                            "device_out": dev_out,
                            "duration_s": 0,
                            "rotate_s": 0,
                            "path": wav_path_abs,
                            "play_path": "",
                            "return_audio": True,
                            "rb_seconds": rb_send,
                        }
                    )
                    fs = int(fs_try)
                    in_ch = int(in_try)
                    started = True
                    last_err = None
                    break
                except RuntimeError as e:
                    last_err = e
                    msg = str(e).lower()
                    retryable = ("no supported input config" in msg) or ("no supported output config" in msg) or ("sr/ch" in msg)
                    if not retryable:
                        raise
            if started:
                break
        if not started:
            if last_err is not None:
                raise last_err
            raise RuntimeError("failed to start playrec session")

        delay_ms = 0.0 if delay_time_ms is None else float(delay_time_ms)
        delay_frames = int(round(delay_ms * float(fs) / 1000.0)) if int(fs) > 0 else 0

        chunks = []

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

        try:
            # Optional pre-roll: record before starting playback.
            if delay_frames < 0 and int(in_ch) > 0:
                pre = int(-delay_frames)
                got = 0
                deadline = time.time() + (float(pre) / float(fs) if float(fs) > 0 else 0.0) + 2.0
                while got < pre and time.time() < deadline:
                    r = c.request({"cmd": "capture_read", "session_id": session_id, "max_frames": int(min(chunk_frames, pre - got))})
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
                    r0 = c.request({"cmd": "play_write", "session_id": session_id, "pcm16_b64": b64})
                    accepted = int(r0.get("accepted_frames", int(sub.shape[0])))
                    if accepted <= 0:
                        if blocking:
                            try:
                                _ = c.request({"cmd": "capture_read", "session_id": session_id, "max_frames": int(chunk_frames)})
                            except Exception:
                                pass
                        time.sleep(0.002)
                        continue
                    off += accepted

                if blocking:
                    r = c.request({"cmd": "capture_read", "session_id": session_id, "max_frames": int(chunk_frames)})
                    pcm = base64.b64decode(r["pcm16_b64"])
                    got_frames = int(r["frames"])
                    got_ch = int(r["channels"])
                    if got_frames > 0 and got_ch > 0:
                        x = _pcm16_to_float32_frames(pcm, got_frames, got_ch)
                        if x.size > 0:
                            chunks.append(x)

            c.request({"cmd": "play_finish", "session_id": session_id})

            if blocking:
                t_end = time.time() + float(frames) / float(fs) + 2.0
                while time.time() < t_end:
                    r = c.request({"cmd": "capture_read", "session_id": session_id, "max_frames": int(chunk_frames)})
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
                _wait_session_end(c, timeout_s=1.0, session_id=session_id)
        finally:
            try:
                c.request({"cmd": "session_stop", "session_id": session_id})
            except Exception:
                pass
    finally:
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

    if return_full:
        if save_wav and wav_path_abs:
            _write_wav_from_float32(wav_path_abs, x, fs)
        return x

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


def _parse_1based_mapping_cols(mapping, *, arg_name: str) -> list[int]:
    """Parse a 1-based channel mapping into 0-based column indices."""
    m = list(mapping) if isinstance(mapping, (list, tuple)) else None
    if not m:
        raise ValueError(f"{arg_name} must be a non-empty sequence")
    cols: list[int] = []
    for ch in m:
        ci = int(ch) - 1
        if ci < 0:
            raise ValueError(f"{arg_name} channel must be >= 1 (1-based)")
        cols.append(int(ci))
    return cols


def _select_channels_1based(data, mapping, *, arg_name: str) -> np.ndarray:
    """Select/reorder channels from a (frames, channels) array using 1-based mapping."""
    y = np.asarray(data, dtype=np.float32)
    if y.ndim == 1:
        y = y[:, None]
    cols = _parse_1based_mapping_cols(mapping, arg_name=arg_name)
    n_ch = int(y.shape[1]) if y.ndim >= 2 else 0
    for ci in cols:
        if ci >= n_ch:
            raise ValueError(f"{arg_name} channel out of range")
    return y[:, cols]


def _route_channels_1based(data, mapping, *, arg_name: str) -> np.ndarray:
    """Route data columns into target channels using a 1-based mapping.

    Semantics (like sounddevice/audiodevice output mapping):
    - mapping specifies the *target* output channel numbers (1-based).
    - The input data must have exactly len(mapping) channels; column j routes to mapping[j].
    - The returned array has channel count max(mapping) (missing channels are filled with zeros).
    - Duplicate entries in mapping are allowed; routed channels are summed.
    """
    y = np.asarray(data, dtype=np.float32)
    if y.ndim == 1:
        y = y[:, None]
    cols = _parse_1based_mapping_cols(mapping, arg_name=arg_name)
    if int(y.shape[1]) != int(len(cols)):
        raise ValueError(f"data channels must equal len({arg_name})")
    out_ch = int(max(cols) + 1) if cols else int(y.shape[1])
    y2 = np.zeros((int(y.shape[0]), out_ch), dtype=np.float32)
    for j, ci in enumerate(cols):
        y2[:, ci] += y[:, j]
    return y2


def _pad_channels_zeros(y: np.ndarray, target_channels: int) -> np.ndarray:
    """Pad (frames, channels) with trailing zero channels up to target_channels."""
    y = np.asarray(y, dtype=np.float32)
    if y.ndim == 1:
        y = y[:, None]
    t = int(target_channels)
    if t <= 0:
        raise ValueError("channels must be a positive int")
    ch = int(y.shape[1])
    if ch >= t:
        return y
    pad = np.zeros((int(y.shape[0]), int(t - ch)), dtype=np.float32)
    return np.concatenate([y, pad], axis=1)


def play(
    data,
    samplerate=None,
    mapping=None,
    blocking=False,
    loop=False,
    *,
    output_mapping=None,
    **kwargs,
) -> None:
    """Play audio data (audiodevice-compatible).

    Args:
        data: Audio to play. Format: array-like, shape (frames,) or (frames, channels);
            converted to float32 internally; each column is one channel.
        samplerate: Sample rate in Hz. Uses default.samplerate if None.
            Format: int or float, e.g. 44100, 48000.
        mapping: 1-based output channel mapping to route data columns into device channels.
            Format: non-empty sequence of 1-based channel indices, e.g. [1, 2] or [2, 1].
        output_mapping: Preferred alias for `mapping` (added for consistency with playrec/stream_playrecord).
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
        ValueError: If `mapping`/`output_mapping` is invalid.
        RuntimeError: If playback fails in the engine.
    """
    global _GLOBAL_SESSION_THREAD, _GLOBAL_SESSION_STOP, _GLOBAL_SESSION_DONE, _GLOBAL_SESSION_ERROR, _GLOBAL_STREAM

    if output_mapping is None:
        output_mapping = mapping
    elif mapping is not None and list(mapping) != list(output_mapping):
        raise TypeError("Provide only one of mapping or output_mapping")

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

    if output_mapping is not None:
        y = _route_channels_1based(data, output_mapping, arg_name="output_mapping")
    else:
        y = np.asarray(data, dtype=np.float32)
        if y.ndim == 1:
            y = y[:, None]

    hostapi_name = str(getattr(default, "hostapi_name", "") or "MME")

    out_idx = _device_index_from_any(device_kw, "output")
    if out_idx is None:
        try:
            out_idx = int(default.device[1])
        except Exception:
            out_idx = None
    if out_idx is not None and out_idx >= 0:
        hostapi_name, _ = _device_name_from_index(int(out_idx))

    # Prefer explicit channels kwarg, otherwise honor default output channels when set.
    out_ch_hint = None
    if channels_kw is not None:
        out_ch_hint = int(channels_kw)
    else:
        try:
            out_ch_def = default.channels.output
            out_ch_hint = None if out_ch_def is None else int(out_ch_def)
        except Exception:
            out_ch_hint = None

    # If user didn't specify a channel count but did specify output_mapping, try to pick a
    # compatible open-channel count for common devices where 1ch may be unsupported but 2ch is.
    if output_mapping is not None and out_ch_hint is None:
        want = int(y.shape[1])
        max_out = 0
        if out_idx is not None and out_idx >= 0:
            try:
                max_out = int(query_devices(int(out_idx)).get("max_output_channels", 0) or 0)
            except Exception:
                max_out = 0
        if max_out > 0:
            out_ch_hint = min(max_out, max(want, 2 if max_out >= 2 else 1))
        else:
            out_ch_hint = max(want, 2)

    if out_ch_hint is not None and out_ch_hint > 0:
        if output_mapping is not None:
            # If user routes into a subset of channels (e.g. [1]), still open the device with
            # the configured output channel count (often 2 on Windows), padding silence.
            if int(y.shape[1]) > int(out_ch_hint):
                raise ValueError(f"channels ({out_ch_hint}) must be >= max(output_mapping) ({int(y.shape[1])})")
            y = _pad_channels_zeros(y, int(out_ch_hint))
        else:
            y = _remix_channels(y, int(out_ch_hint))

    # Guard against requesting more channels than the selected output device supports.
    max_out = 0
    if out_idx is not None and out_idx >= 0:
        try:
            max_out = int(query_devices(int(out_idx)).get("max_output_channels", 0) or 0)
        except Exception:
            max_out = 0
        if max_out > 0 and int(y.shape[1]) > max_out:
            if output_mapping is not None:
                raise ValueError(
                    f"output_mapping requires {int(y.shape[1])} output channels, but device supports {int(max_out)}"
                )
            y = _remix_channels(y, max_out)

    # Some Windows drivers reject certain channel counts (e.g. 1ch) even though others (e.g. 2ch) work.
    # When output_mapping is used, we can safely try alternative open-channel counts by padding silence,
    # without changing the routing semantics.
    y_base = y
    if output_mapping is not None:
        required = int(y_base.shape[1])
        # Build candidate out_ch counts (>= required).
        cands: list[int] = []
        if out_ch_hint is not None:
            cands.append(int(out_ch_hint))
        # Prefer 2ch for mono mappings when possible.
        if required == 1:
            cands.append(2)
            cands.append(1)
        cands.append(required)
        if int(max_out) > 0:
            cands.append(int(max_out))
            # Common multichannel layouts (keep within device capability).
            for v in (2, 4, 6, 8, 12, 16):
                if int(v) <= int(max_out):
                    cands.append(int(v))
        # Normalize: unique, >= required, <= max_out (when known), stable order.
        seen = set()
        cands2: list[int] = []
        for v in cands:
            vv = int(v)
            if vv < required:
                continue
            if int(max_out) > 0 and vv > int(max_out):
                continue
            if vv not in seen:
                seen.add(vv)
                cands2.append(vv)
        # Ensure we don't accidentally try to open fewer channels than required routing.
        def _y_for_out_ch(out_ch: int) -> np.ndarray:
            return _pad_channels_zeros(y_base, int(out_ch)) if int(out_ch) > required else y_base

        def _play_try() -> None:
            last = None
            for out_ch in cands2:
                try:
                    _play_engine(
                        _y_for_out_ch(int(out_ch)),
                        blocking=True,
                        samplerate=int(fs),
                        hostapi=hostapi_name,
                        device_out=out_idx,
                        rb_seconds=rb_seconds,
                        chunk_frames=chunk_frames,
                    )
                    return
                except RuntimeError as e:
                    last = e
                    msg = str(e).lower()
                    # Only retry on config-related errors; otherwise surface immediately.
                    if ("no supported output config" not in msg) and ("sr/ch" not in msg):
                        raise
            if last is not None:
                raise last

        if not blocking:
            stop_ev = threading.Event()
            done_ev = threading.Event()

            def _worker() -> None:
                global _GLOBAL_SESSION_ERROR
                try:
                    _play_try()
                except BaseException as e:
                    with _GLOBAL_SESSION_LOCK:
                        _GLOBAL_SESSION_ERROR = e
                finally:
                    done_ev.set()

            t = threading.Thread(target=_worker, daemon=True)
            with _GLOBAL_SESSION_LOCK:
                _GLOBAL_SESSION_THREAD = t
                _GLOBAL_SESSION_STOP = stop_ev
                _GLOBAL_SESSION_DONE = done_ev
                _GLOBAL_SESSION_ERROR = None
                _GLOBAL_STREAM = None
            t.start()
            return

        _play_try()
        return

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
                    device_out=out_idx,
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
        device_out=out_idx,
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
    mapping_cols = None
    if mapping is not None:
        # Mapping selects/reorders input channels from the recorded multi-channel stream (1-based).
        m = list(mapping) if isinstance(mapping, (list, tuple)) else None
        if not m:
            raise ValueError("mapping must be a non-empty sequence")
        cols = []
        for ch in m:
            ci = int(ch) - 1
            if ci < 0:
                raise ValueError("mapping channel must be >= 1 (1-based)")
            cols.append(ci)
        mapping_cols = cols

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
    wav_path_abs = os.path.abspath(wav_path) if (save_wav and wav_path) else ""

    fs = float(samplerate) if samplerate is not None else (default.samplerate if default.samplerate is not None else _DEFAULT_SR_FALLBACK)

    if channels is None:
        if out is not None and np.asarray(out).ndim >= 2:
            channels = int(np.asarray(out).shape[1])
        else:
            ch_def = default.channels[0]
            channels = int(ch_def) if ch_def is not None else 1
    ch_i = int(channels)
    if mapping_cols is not None:
        need_ch = int(max(mapping_cols) + 1)
        if int(ch_i) < int(need_ch):
            raise ValueError(f"channels ({ch_i}) must be >= max(mapping) ({need_ch})")

    out_ch = int(len(mapping_cols)) if mapping_cols is not None else int(ch_i)
    if out is None:
        out_arr = np.zeros((frames_i, out_ch), dtype=np.float32)
    else:
        out_arr = np.asarray(out)
        if out_arr.dtype != np.float32:
            raise TypeError("rec() only supports float32 output; provide out with dtype=np.float32")
        if out_arr.shape[0] != frames_i:
            raise ValueError("out has incompatible number of frames")
        if out_arr.ndim != 2 or int(out_arr.shape[1]) != int(out_ch):
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

    def _do_record_full() -> np.ndarray:
        delay_ms = 0.0 if delay_time is None else float(delay_time)
        if delay_ms < 0:
            raise ValueError("delay_time must be >= 0 (milliseconds)")
        if delay_ms > 0:
            time.sleep(float(delay_ms) / 1000.0)
        # When mapping is used, record full channels then map in Python.
        y = _rec_engine(
            frames_i,
            wav_path="",
            save_wav=False,
            blocking=True,
            samplerate=int(fs),
            channels=int(ch_i),
            hostapi=None,
            device_in=in_idx,
            rb_seconds=rb_seconds,
        )
        assert isinstance(y, np.ndarray)
        return y

    def _apply_mapping_and_save(y_full: np.ndarray) -> np.ndarray:
        y = np.asarray(y_full, dtype=np.float32)
        if y.ndim == 1:
            y = y[:, None]
        if mapping_cols is not None:
            y = y[:, mapping_cols]
        if save_wav and wav_path_abs:
            _write_wav_from_float32(wav_path_abs, y, int(fs))
        return y

    if not blocking:
        stop_ev = threading.Event()
        done_ev = threading.Event()

        def _worker() -> None:
            try:
                y_full = _do_record_full()
                y = _apply_mapping_and_save(y_full)
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

    y_full = _do_record_full()
    y = _apply_mapping_and_save(y_full)
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
        output_mapping: 1-based output channel mapping to route data columns into device channels.
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
    in_map_cols = None
    if input_mapping is not None:
        m = list(input_mapping) if isinstance(input_mapping, (list, tuple)) else None
        if not m:
            raise ValueError("input_mapping must be a non-empty sequence")
        cols = []
        for ch in m:
            ci = int(ch) - 1
            if ci < 0:
                raise ValueError("input_mapping channel must be >= 1 (1-based)")
            cols.append(ci)
        in_map_cols = cols
    y_out = np.asarray(data, dtype=np.float32)
    if y_out.ndim == 1:
        y_out = y_out[:, None]
    if output_mapping is not None:
        y_out = _route_channels_1based(y_out, output_mapping, arg_name="output_mapping")
        # If default output channels are set, pad with silence so duplex opens with that count.
        # Otherwise, best-effort pad to 2 when the output device supports >=2 (some drivers reject 1ch).
        try:
            out_ch_def = default.channels.output
            out_ch_pad = None if out_ch_def is None else int(out_ch_def)
        except Exception:
            out_ch_pad = None
        if out_ch_pad is None:
            max_out0 = 0
            try:
                dk = kwargs.get("device", None)
                out_idx0 = _device_index_from_any(dk, "output")
                if out_idx0 is None:
                    out_idx0 = int(default.device[1])
                if out_idx0 is not None and int(out_idx0) >= 0:
                    max_out0 = int(query_devices(int(out_idx0)).get("max_output_channels", 0) or 0)
            except Exception:
                max_out0 = 0
            out_ch_pad = 2 if int(max_out0) >= 2 else None
        if out_ch_pad is not None and int(out_ch_pad) > 0 and int(y_out.shape[1]) <= int(out_ch_pad):
            y_out = _pad_channels_zeros(y_out, int(out_ch_pad))

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
    wav_path_abs = os.path.abspath(wav_path) if (save_wav and wav_path) else ""

    fs = float(samplerate) if samplerate is not None else (default.samplerate if default.samplerate is not None else _DEFAULT_SR_FALLBACK)

    # y_out already normalized to float32 (frames, channels) above, and output_mapping already applied.
    frames_i = int(y_out.shape[0])

    if channels is None:
        ch_def = default.channels[0]
        channels = int(ch_def) if ch_def is not None else 1
    in_ch_capture = int(channels)
    if in_map_cols is not None:
        need_ch = int(max(in_map_cols) + 1)
        if int(in_ch_capture) < int(need_ch):
            # capture enough channels to be able to map
            in_ch_capture = int(need_ch)
    out_ch_return = int(len(in_map_cols)) if in_map_cols is not None else int(in_ch_capture)

    if out is None:
        out_arr = np.zeros((frames_i, out_ch_return), dtype=np.float32)
    else:
        out_arr = np.asarray(out)
        if out_arr.dtype != np.float32:
            raise TypeError("playrec() only supports float32 output; provide out with dtype=np.float32")
        if out_arr.shape[0] != frames_i:
            raise ValueError("out has incompatible number of frames")
        if out_arr.ndim != 2 or int(out_arr.shape[1]) != int(out_ch_return):
            raise ValueError("out must have shape (frames, channels) matching mapping")

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

        try:
            max_out = int(query_devices(int(out_idx)).get("max_output_channels", 0) or 0)
        except Exception:
            max_out = 0
        if max_out > 0 and int(y_out.shape[1]) > max_out:
            raise ValueError(
                f"output_mapping requires {int(y_out.shape[1])} output channels, but device supports {int(max_out)}"
            )

    def _do_playrec() -> np.ndarray:
        x_full = _playrec_engine(
            y_out,
            wav_path=wav_path,
            save_wav=False,
            blocking=True,
            samplerate=int(fs),
            in_channels=int(in_ch_capture),
            hostapi=hostapi_name,
            device_in=in_idx if (in_idx is not None and int(in_idx) >= 0) else None,
            device_out=out_idx if (out_idx is not None and int(out_idx) >= 0) else None,
            rb_seconds=rb_seconds,
            chunk_frames=chunk_frames,
            delay_time_ms=delay_time,
            alignment=bool(alignment),
            alignment_channel=int(alignment_channel),
        )
        x_full = np.asarray(x_full, dtype=np.float32)
        if x_full.ndim == 1:
            x_full = x_full[:, None]
        x = x_full[:, in_map_cols] if in_map_cols is not None else x_full
        if save_wav and wav_path_abs:
            _write_wav_from_float32(wav_path_abs, x, int(fs))
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
    drain_time: float = 0,
    return_full: bool = False,
    alignment: bool = False,
    alignment_channel: int = 1,
    input_mapping=None,
    output_mapping=None,
    wav_path: str = "",
    save_wav: bool = False,
):
    """Stream-based play+record helper (blocking).

    Implementation note:
    This helper is built on the engine-driven duplex path (same core logic as `playrec()`),
    which drains capture until the engine indicates EOF. This avoids needing an explicit tail
    "drain_time" to prevent end truncation.

    Args:
        data: Audio to play. Shape (frames,) or (frames, out_channels), float32-like.
        samplerate: Sample rate in Hz; defaults to `default.samplerate`.
        channels: Number of input channels to record; defaults to `default.channels[0]` or 1.
        blocksize: Callback block size (frames).
        delay_time: Recording delay relative to playback, in milliseconds.
            Mirrors `playrec()` semantics:
            - delay_time > 0: window shifts later
            - delay_time < 0: window starts earlier (pre-roll)
        drain_time: Deprecated for `stream_playrecord()` and ignored (kept for backward compatibility).
        return_full: If True, return the full captured recording (including any lead-in and tail),
            without delay/alignment windowing. This is useful for debugging latency and truncation.
        alignment: If True, align recorded audio to stimulus using GCC-PHAT (see alignment_processing).
        input_mapping: 1-based input channel mapping; when given, channels=len(input_mapping).
        output_mapping: 1-based output channel mapping to route data columns into device channels.
        wav_path: Path to save WAV when save_wav=True.
        save_wav: Whether to save returned audio to a WAV file.

    Returns:
        np.ndarray: Recorded audio with shape (frames, channels).
    """
    in_map_cols = None
    if input_mapping is not None:
        m = list(input_mapping) if isinstance(input_mapping, (list, tuple)) else None
        if not m:
            raise ValueError("input_mapping must be a non-empty sequence")
        cols = []
        for ch in m:
            ci = int(ch) - 1
            if ci < 0:
                raise ValueError("input_mapping channel must be >= 1 (1-based)")
            cols.append(ci)
        in_map_cols = cols

    y_out0 = np.asarray(data, dtype=np.float32)
    if y_out0.ndim == 1:
        y_out0 = y_out0[:, None]

    if output_mapping is not None:
        y_out0 = _route_channels_1based(y_out0, output_mapping, arg_name="output_mapping")
        # If default output channels are set, pad with silence so engine can open that config.
        # Otherwise, best-effort pad to 2 when the output device supports >=2 (some drivers reject 1ch).
        out_ch_pad = None
        try:
            out_ch_def = default.channels.output
            out_ch_pad = None if out_ch_def is None else int(out_ch_def)
        except Exception:
            out_ch_pad = None
        if out_ch_pad is None:
            out_idx = None
            try:
                def_out = int(getattr(default, "device_out_override", -1))
                out_idx = def_out if def_out >= 0 else int(default.device[1])
            except Exception:
                out_idx = None
            if out_idx is not None and int(out_idx) >= 0:
                try:
                    max_out = int(query_devices(int(out_idx)).get("max_output_channels", 0) or 0)
                except Exception:
                    max_out = 0
                out_ch_pad = 2 if int(max_out) >= 2 else None
        if out_ch_pad is not None and int(out_ch_pad) > 0 and int(y_out0.shape[1]) <= int(out_ch_pad):
            y_out0 = _pad_channels_zeros(y_out0, int(out_ch_pad))

        # Best-effort: validate against default output device capability (if resolvable).
        out_idx = None
        try:
            def_out = int(getattr(default, "device_out_override", -1))
            out_idx = def_out if def_out >= 0 else int(default.device[1])
        except Exception:
            out_idx = None
        if out_idx is not None and int(out_idx) >= 0:
            try:
                max_out = int(query_devices(int(out_idx)).get("max_output_channels", 0) or 0)
            except Exception:
                max_out = 0
            if max_out > 0 and int(y_out0.shape[1]) > max_out:
                raise ValueError(
                    f"output_mapping requires {int(y_out0.shape[1])} output channels, but device supports {int(max_out)}"
                )

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
    in_ch_capture = int(channels)
    if in_map_cols is not None:
        need_ch = int(max(in_map_cols) + 1)
        if int(in_ch_capture) < int(need_ch):
            raise ValueError(f"channels ({in_ch_capture}) must be >= max(input_mapping) ({need_ch})")
    out_ch_return = int(len(in_map_cols)) if in_map_cols is not None else int(in_ch_capture)

    if save_wav and not wav_path:
        raise ValueError("save_wav=True requires wav_path")
    wav_path_abs = os.path.abspath(wav_path) if (save_wav and wav_path) else ""

    # Engine-driven playrec drains to EOF; drain_time is not needed here (deprecated).
    delay_ms = 0.0 if delay_time is None else float(delay_time)

    if return_full:
        rec_full = _playrec_engine(
            y_out0,
            wav_path="",
            save_wav=False,
            blocking=True,
            samplerate=int(fs),
            in_channels=int(in_ch_capture),
            hostapi=None,
            device_in=None,
            device_out=None,
            rb_seconds=None,
            chunk_frames=int(blocksize),
            delay_time_ms=0.0,
            alignment=False,
            alignment_channel=int(alignment_channel),
            return_full=True,
        )
        rec_full = np.asarray(rec_full, dtype=np.float32)
        if rec_full.ndim == 1:
            rec_full = rec_full[:, None]
        rec_full = rec_full[:, in_map_cols] if in_map_cols is not None else rec_full

        if save_wav and wav_path_abs:
            _write_wav_from_float32(wav_path_abs, rec_full, int(fs))
        return rec_full.astype(np.float32, copy=False)

    x_full = _playrec_engine(
        y_out0,
        wav_path="",
        save_wav=False,
        blocking=True,
        samplerate=int(fs),
        in_channels=int(in_ch_capture),
        hostapi=None,
        device_in=None,
        device_out=None,
        rb_seconds=None,
        chunk_frames=int(blocksize),
        delay_time_ms=float(delay_ms),
        alignment=bool(alignment),
        alignment_channel=int(alignment_channel),
        return_full=False,
    )
    x_full = np.asarray(x_full, dtype=np.float32)
    if x_full.ndim == 1:
        x_full = x_full[:, None]
    x = x_full[:, in_map_cols] if in_map_cols is not None else x_full
    if save_wav and wav_path_abs:
        _write_wav_from_float32(wav_path_abs, x, int(fs))
    return x.astype(np.float32, copy=False)


def _wait_session_end(client: AudioDeviceClient, timeout_s: float, session_id: str = "") -> None:
    """Poll engine status until the current session ends or timeout elapses.

    Args:
        client (AudioDeviceClient): Connected client instance.
        timeout_s (float): Timeout in seconds.
        session_id (str): Session ID to query. Empty string uses legacy single-session behavior.
    """
    deadline = time.time() + float(timeout_s)
    req: dict = {"cmd": "status"}
    if session_id:
        req["session_id"] = session_id
    while time.time() < deadline:
        try:
            st = client.request(req)
        except Exception:
            return
        if not st.get("has_session", True):
            return
        time.sleep(0.02)


class CallbackFlags:
    """Flag bits for the *status* argument to a stream *callback*.

    This follows the same usage pattern as `sounddevice.CallbackFlags`:
    - `bool(status)` is True if any flags are set
    - `str(status)` lists set flags (comma-separated)
    - flags can be accumulated with `errors |= status`

    Notes:
        In this MVP, engine/driver overflow/underflow flags are not populated automatically.
        The properties exist for compatibility and for user-side bookkeeping.
    """

    __slots__ = (
        "_flags",
        # --- audiodevice_py extensions (best-effort) ---
        # Callback timing diagnostics (seconds). Updated by the stream worker loop.
        "callback_seconds",
        "block_seconds",
        "callback_overrun",
        "callback_overrun_ratio",
        "callback_overrun_consecutive",
        "callback_overrun_total",
    )

    _INPUT_UNDERFLOW = 0x01
    _INPUT_OVERFLOW = 0x02
    _OUTPUT_UNDERFLOW = 0x04
    _OUTPUT_OVERFLOW = 0x08
    _PRIMING_OUTPUT = 0x10

    _FLAG_NAMES = (
        "input_underflow",
        "input_overflow",
        "output_underflow",
        "output_overflow",
        "priming_output",
    )

    def __init__(self, flags: int = 0x0):
        self._flags = int(flags)
        self.callback_seconds = 0.0
        self.block_seconds = 0.0
        self.callback_overrun = False
        self.callback_overrun_ratio = 0.0
        self.callback_overrun_consecutive = 0
        self.callback_overrun_total = 0

    def clear(self) -> None:
        """Clear all flag bits (does not touch timing diagnostics)."""
        self._flags = 0x0

    def __repr__(self) -> str:
        flags = str(self)
        if not flags:
            flags = "no flags set"
        return f"<audiodevice.CallbackFlags: {flags}>"

    def __str__(self) -> str:
        return ", ".join(name.replace("_", " ") for name in self._FLAG_NAMES if getattr(self, name))

    def __bool__(self) -> bool:
        return bool(self._flags)

    def __ior__(self, other):
        if not isinstance(other, CallbackFlags):
            return NotImplemented
        self._flags |= other._flags
        return self

    def __or__(self, other):
        if not isinstance(other, CallbackFlags):
            return NotImplemented
        return CallbackFlags(self._flags | other._flags)

    def _hasflag(self, flag: int) -> bool:
        return bool(self._flags & int(flag))

    def _updateflag(self, flag: int, value: bool) -> None:
        if value:
            self._flags |= int(flag)
        else:
            self._flags &= ~int(flag)

    @property
    def input_underflow(self) -> bool:
        return self._hasflag(self._INPUT_UNDERFLOW)

    @input_underflow.setter
    def input_underflow(self, value: bool) -> None:
        self._updateflag(self._INPUT_UNDERFLOW, bool(value))

    @property
    def input_overflow(self) -> bool:
        return self._hasflag(self._INPUT_OVERFLOW)

    @input_overflow.setter
    def input_overflow(self, value: bool) -> None:
        self._updateflag(self._INPUT_OVERFLOW, bool(value))

    @property
    def output_underflow(self) -> bool:
        return self._hasflag(self._OUTPUT_UNDERFLOW)

    @output_underflow.setter
    def output_underflow(self, value: bool) -> None:
        self._updateflag(self._OUTPUT_UNDERFLOW, bool(value))

    @property
    def output_overflow(self) -> bool:
        return self._hasflag(self._OUTPUT_OVERFLOW)

    @output_overflow.setter
    def output_overflow(self, value: bool) -> None:
        self._updateflag(self._OUTPUT_OVERFLOW, bool(value))

    @property
    def priming_output(self) -> bool:
        return self._hasflag(self._PRIMING_OUTPUT)

    @priming_output.setter
    def priming_output(self, value: bool) -> None:
        self._updateflag(self._PRIMING_OUTPUT, bool(value))


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
        mapping=None,
        output_mapping=None,
        device=None,
        callback=None,
        delay_time=0,
        pacing: bool = True,
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
            mapping: 1-based input channel mapping (InputStream/Stream only). If provided, the
                engine still captures `channels` input channels, but the callback receives only
                the selected/reordered channels with shape (frames, len(mapping)).
                Requires: channels >= max(mapping). Format: non-empty sequence, e.g. [1] or [2, 1].
            output_mapping: 1-based output channel mapping (OutputStream/Stream only). If provided,
                the engine plays `channels` output channels, but the callback receives only
                shape (frames, len(output_mapping)); those columns are routed into the selected
                device output channels. Requires: channels >= max(output_mapping).
            device: Device selection for the stream. Same semantics as audiodevice `rec()`/`play()`:
                `int` for a single device index, or `(in_idx, out_idx)` tuple. If not provided,
                uses global defaults (`ad.default.device` / overrides).
            callback: Callback with signature (indata, outdata, frames, time_info, status).
                Format: indata (frames, in_ch) float32; outdata (frames, out_ch) float32, write in
                callback; frames int; time_info dict; status CallbackFlags. Raise CallbackStop or
                CallbackAbort to end the stream.
            delay_time: Delay before starting to deliver input to the callback, in milliseconds.
                This is mainly useful for capture-only streams (InputStream). Format: float/int (ms).
            pacing: Whether to pace the callback loop roughly in real-time (best-effort).
                Set False for stress-testing (may overfill engine buffers).
        """
        self.kind = str(kind)
        self.samplerate = float(samplerate) if samplerate is not None else (
            default.samplerate if default.samplerate is not None else _DEFAULT_SR_FALLBACK
        )
        self.blocksize = int(blocksize or 1024)
        self.latency = latency
        self.channels = channels
        self._mapping_cols = None
        if mapping is not None:
            self._mapping_cols = _parse_1based_mapping_cols(mapping, arg_name="mapping")
        self._output_mapping_cols = None
        if output_mapping is not None:
            self._output_mapping_cols = _parse_1based_mapping_cols(output_mapping, arg_name="output_mapping")
        self._device_in_idx = _device_index_from_any(device, "input")
        self._device_out_idx = _device_index_from_any(device, "output")
        self.callback = callback
        self._cb_style: Optional[str] = None
        self.delay_time = 0.0 if delay_time is None else float(delay_time)
        self.pacing = bool(pacing)

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._closed = False
        self._active = False
        self._thread_exception: Optional[BaseException] = None
        self._resolved_hostapi_name: Optional[str] = None
        self._resolved_device_in: Optional[str] = None
        self._resolved_device_out: Optional[str] = None
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

        # Snapshot device selection at start-time, so concurrent starts don't race on global defaults.
        try:
            hostapi_name, dev_in, dev_out = self._resolve_hostapi_and_devices_for_stream()
            self._resolved_hostapi_name = str(hostapi_name or "")
            self._resolved_device_in = str(dev_in or "")
            self._resolved_device_out = str(dev_out or "")
        except Exception:
            # Fall back to runtime resolution in the worker.
            self._resolved_hostapi_name = None
            self._resolved_device_in = None
            self._resolved_device_out = None

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

    def _pick_callback_style(self) -> str:
        """Pick a sounddevice-compatible callback calling convention.

        Returns:
            str: One of:
                - "full5": callback(indata, outdata, frames, time_info, status)
                - "sd_input4": callback(indata, frames, time_info, status)
                - "sd_output4": callback(outdata, frames, time_info, status)
        """
        if self.kind == "duplex":
            return "full5"
        cb = self.callback
        if cb is None:
            return "full5"
        try:
            sig = inspect.signature(cb)
            params = list(sig.parameters.values())
        except Exception:
            return "full5"

        for p in params:
            if p.kind == inspect.Parameter.VAR_POSITIONAL:
                return "full5"

        positional = [
            p for p in params
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        max_positional = len(positional)

        if self.kind == "input" and max_positional <= 4:
            return "sd_input4"
        if self.kind == "output" and max_positional <= 4:
            return "sd_output4"
        return "full5"

    def _invoke_callback(self, *, indata, outdata, frames: int, time_info: dict, status: CallbackFlags) -> None:
        style = self._cb_style or "full5"
        cb = self.callback
        if cb is None:
            return
        if style == "sd_input4":
            cb(indata, frames, time_info, status)
            return
        if style == "sd_output4":
            cb(outdata, frames, time_info, status)
            return
        cb(indata, outdata, frames, time_info, status)

    def _resolve_hostapi_and_devices_for_stream(self) -> Tuple[str, str, str]:
        """Resolve effective hostapi/device names for stream startup.

        Returns:
            tuple[str, str, str]: `(hostapi_name, device_in_name, device_out_name)`.
        """
        # Resolve per-stream device if explicitly provided; otherwise use defaults.
        in_idx = self._device_in_idx
        out_idx = self._device_out_idx

        if self.kind == "input":
            # Ignore output selection to avoid hostapi mismatch checks for unused output.
            return _resolve_hostapi_and_devices(hostapi=None, device_in=in_idx, device_out=-1)
        if self.kind == "output":
            # Ignore input selection to avoid hostapi mismatch checks for unused input.
            return _resolve_hostapi_and_devices(hostapi=None, device_in=-1, device_out=out_idx)
        # Duplex: resolve both sides (must share a host API if both provided/effective).
        return _resolve_hostapi_and_devices(hostapi=None, device_in=in_idx, device_out=out_idx)

    def _run(self) -> None:
        """Worker loop: start an engine session and drive the callback per block.

        Raises:
            RuntimeError: If engine session fails or I/O fails.
        """
        # MVP implementation: use engine playrec mode and call callback per block.
        hostapi_name = self._resolved_hostapi_name
        dev_in = self._resolved_device_in
        dev_out = self._resolved_device_out
        if hostapi_name is None or dev_in is None or dev_out is None:
            hostapi_name, dev_in, dev_out = self._resolve_hostapi_and_devices_for_stream()
        backend_eff, engine_hostapi, _disp = _hostapi_display_to_engine(hostapi_name)

        # Channels
        in_ch_capture = 0
        in_ch_cb = 0
        out_ch = 0
        mode = "playrec"
        return_audio = True
        if self.kind == "input":
            in_ch_capture = int(self.channels or (default.channels[0] or 1))
            out_ch = 0
        elif self.kind == "output":
            # OutputStream does not need input; use "play" mode for stability.
            in_ch_capture = 0
            out_ch = int(self.channels or (default.channels[1] or 1))
            mode = "play"
            return_audio = False
        else:
            if isinstance(self.channels, (list, tuple)) and len(self.channels) == 2:
                in_ch_capture = int(self.channels[0] or 1)
                out_ch = int(self.channels[1] or 1)
            else:
                v = int(self.channels or (default.channels[0] or 1))
                in_ch_capture = v
                out_ch = v

        if int(in_ch_capture) < 0:
            in_ch_capture = 0
        if int(out_ch) < 0:
            out_ch = 0

        mapping_cols = self._mapping_cols
        if mapping_cols is not None and int(in_ch_capture) > 0:
            need_ch = int(max(mapping_cols) + 1)
            if int(in_ch_capture) < int(need_ch):
                raise ValueError(f"channels ({in_ch_capture}) must be >= max(mapping) ({need_ch})")
            in_ch_cb = int(len(mapping_cols))
        else:
            mapping_cols = None
            in_ch_cb = int(in_ch_capture)

        out_map_cols = self._output_mapping_cols
        if out_map_cols is not None and int(out_ch) > 0:
            need_ch = int(max(out_map_cols) + 1)
            if int(out_ch) < int(need_ch):
                raise ValueError(f"channels ({out_ch}) must be >= max(output_mapping) ({need_ch})")
            out_ch_cb = int(len(out_map_cols))
        else:
            out_map_cols = None
            out_ch_cb = int(out_ch)

        session_id = _next_session_id()
        c = AudioDeviceClient(default.host, default.port, timeout=float(default.timeout))
        self._client = c
        try:
            c.request(
                {
                    "cmd": "session_start",
                    "session_id": session_id,
                    "backend": backend_eff,
                    "hostapi": engine_hostapi,
                    "mode": mode,
                    "sr": int(self.samplerate),
                    "in_ch": int(in_ch_capture),
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
            if self.kind == "input" and int(in_ch_capture) > 0:
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
                        r = c.request({"cmd": "capture_read", "session_id": session_id, "max_frames": int(drain_chunk)})
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
            prefill_s = 0.0
            if self.kind == "output" and out_ch > 0 and block_dt > 0:
                prefill_s = float(min(2.0, float(getattr(default, "rb_seconds", 2)) * 0.2))
                _prefill_n = max(4, int(prefill_s / block_dt))

            next_tick = time.perf_counter()
            status = CallbackFlags()
            self._cb_style = self._pick_callback_style()
            pending_flags = 0
            cb_overrun_consecutive = 0
            cb_overrun_total = 0
            stopped_gracefully = False
            while not self._stop.is_set():
                # Match sounddevice semantics: each callback sees a "fresh" status flags object.
                # (Timing diagnostics are updated below.)
                status.clear()
                if pending_flags:
                    # Apply I/O events observed in the previous block.
                    status._flags |= int(pending_flags)
                    pending_flags = 0
                # Best-effort populate sounddevice-like overflow/underflow flags from the
                # previous callback timing diagnostics.
                if bool(getattr(status, "callback_overrun", False)):
                    if self.kind in ("input", "duplex"):
                        status.input_overflow = True
                    if self.kind in ("output", "duplex"):
                        status.output_underflow = True
                # Read input
                indata_cap = np.zeros((frames, int(in_ch_capture)), dtype=np.float32)
                if int(in_ch_capture) > 0:
                    # If delay_time is enabled for InputStream, drop initial samples until
                    # delay_remain is consumed, then deliver full blocks to the callback.
                    filled = 0
                    got_ch_eff = 0
                    while filled < frames and (not self._stop.is_set()):
                        r = c.request({"cmd": "capture_read", "session_id": session_id, "max_frames": int(frames - filled)})
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
                        ch_take = int(min(int(got_ch_eff), int(in_ch_capture), int(x.shape[1])))
                        if take > 0 and ch_take > 0:
                            indata_cap[filled : filled + take, :ch_take] = x[:take, :ch_take]
                        filled += take

                indata = indata_cap[:, mapping_cols] if mapping_cols is not None else indata_cap
                outdata = np.zeros((frames, out_ch_cb), dtype=np.float32)
                tnow = time.time()
                time_info = {
                    "inputBufferAdcTime": tnow,
                    "currentTime": tnow,
                    "outputBufferDacTime": tnow,
                }

                try:
                    cb_t0 = time.perf_counter()
                    self._invoke_callback(
                        indata=indata,
                        outdata=outdata,
                        frames=frames,
                        time_info=time_info,
                        status=status,
                    )
                    cb_s = time.perf_counter() - cb_t0
                except CallbackStop:
                    stopped_gracefully = True
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

                # sounddevice-like behavior: do NOT raise on callback overruns.
                # Report overruns via `status` instead (e.g. `status.input_overflow`).

                # Write output
                if out_ch > 0:
                    if out_map_cols is not None:
                        outdata_cap = np.zeros((frames, out_ch), dtype=np.float32)
                        for j, ci in enumerate(out_map_cols):
                            outdata_cap[:, ci] += outdata[:, j]
                        out_send = outdata_cap
                    else:
                        out_send = outdata
                    off = 0
                    # Engine may accept partial frames when its output buffer is full.
                    # Never drop frames silently: retry until the whole block is accepted.
                    while off < frames and not self._stop.is_set():
                        sub = out_send[off:]
                        pcm16 = np.clip(sub, -1.0, 1.0)
                        pcm16 = (pcm16 * 32767.0).astype(np.int16)
                        b64 = base64.b64encode(pcm16.tobytes()).decode("ascii")
                        # Some backends/devices may report session as inactive briefly
                        # right after session_start; retry a bit to avoid spurious failures.
                        deadline = time.time() + 1.0
                        while True:
                            try:
                                r0 = c.request({"cmd": "play_write", "session_id": session_id, "pcm16_b64": b64})
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
                            # Output buffer is full (backpressure). Report as sounddevice-like
                            # output_overflow in the *next* callback.
                            pending_flags |= int(status._OUTPUT_OVERFLOW)
                            # For OutputStream we use playrec with a dummy input channel.
                            # If output buffer is full, optionally drain input to avoid input ringbuffer overflow.
                            if self.kind == "output" and int(in_ch_capture) > 0:
                                try:
                                    _ = c.request({"cmd": "capture_read", "session_id": session_id, "max_frames": int(frames)})
                                except Exception:
                                    pass
                            time.sleep(0.002)
                            continue
                        if accepted > int(sub.shape[0]):
                            accepted = int(sub.shape[0])
                        if accepted < int(sub.shape[0]):
                            # Partial accept indicates output buffer pressure; mark overflow.
                            pending_flags |= int(status._OUTPUT_OVERFLOW)
                        off += accepted

                # Pace the loop roughly in real-time to avoid overfilling engine buffers.
                # During pre-fill phase, skip pacing so blocks are sent as fast as the
                # engine accepts them, building up a safety margin in the ring buffer.
                if _prefill_n > 0:
                    _prefill_n -= 1
                    if _prefill_n == 0:
                        next_tick = time.perf_counter()
                elif self.pacing and block_dt > 0:
                    next_tick += block_dt
                    now = time.perf_counter()
                    sleep_s = next_tick - now
                    if sleep_s > 0:
                        time.sleep(sleep_s)
                    else:
                        # If we're running late, avoid accumulating unbounded lag.
                        next_tick = now
        finally:
            # If the user stopped the stream by raising CallbackStop, try to let the engine
            # drain its output ring buffer instead of truncating queued audio.
            if stopped_gracefully and self.kind == "output" and (not self._stop.is_set()) and out_ch > 0:
                try:
                    c.request({"cmd": "play_finish", "session_id": session_id})
                except Exception:
                    pass
                if self.pacing:
                    try:
                        drain_timeout = float(max(2.0, prefill_s + 2.0))
                        drain_timeout = float(min(15.0, drain_timeout))
                        _wait_session_end(c, timeout_s=drain_timeout, session_id=session_id)
                    except Exception:
                        pass

            self._client = None
            try:
                c.request({"cmd": "session_stop", "session_id": session_id})
            except Exception:
                pass
            c.close()


class Stream(_StreamBase):
    """Duplex (input+output) callback stream."""
    def __init__(self, *args, **kwargs) -> None:
        """Create a duplex stream.

        Args:
            *args: Forwarded to `_StreamBase`.
            **kwargs: Forwarded to `_StreamBase` (notably `callback`, `samplerate`, `channels`, `device`).
        """
        super().__init__(kind="duplex", *args, **kwargs)


class InputStream(_StreamBase):
    """Input-only callback stream."""
    def __init__(
        self,
        *,
        samplerate=None,
        blocksize: int = 0,
        latency=None,
        channels=None,
        mapping=None,
        device=None,
        callback=None,
        delay_time=0,
        pacing: bool = True,
    ) -> None:
        """Create an input stream.

        Args:
            samplerate: Sample rate in Hz. Uses default.samplerate if None.
            blocksize: Frames per callback; 0 uses default.
            latency: Compatibility arg.
            channels: Input channel count captured from the device.
            mapping: 1-based input channel mapping applied before callback. Requires channels >= max(mapping).
            device: Device selection for this stream. `int` or `(in_idx, out_idx)`; output part is ignored.
            callback: Callback signature like sounddevice:
                callback(indata, frames, time_info, status) -> None
                (The 5-arg form callback(indata, outdata, frames, time_info, status) is also accepted.)
            delay_time: Delay before delivering input to callback (ms).
        """
        super().__init__(
            kind="input",
            samplerate=samplerate,
            blocksize=blocksize,
            latency=latency,
            channels=channels,
            mapping=mapping,
            device=device,
            callback=callback,
            delay_time=delay_time,
            pacing=pacing,
        )


class OutputStream(_StreamBase):
    """Output-only callback stream."""
    def __init__(
        self,
        *,
        samplerate=None,
        blocksize: int = 0,
        latency=None,
        channels=None,
        mapping=None,
        output_mapping=None,
        device=None,
        callback=None,
        pacing: bool = True,
    ) -> None:
        """Create an output stream.

        Args:
            samplerate: Sample rate in Hz. Uses default.samplerate if None.
            blocksize: Frames per callback; 0 uses default.
            latency: Compatibility arg.
            channels: Output channel count played by the device.
            mapping: Alias for output_mapping (sounddevice-compatible name). 1-based output channel
                mapping applied after callback. Requires channels >= max(mapping). Callback outdata
                has shape (frames, len(mapping)).
            output_mapping: 1-based output channel mapping applied after callback. Requires
                channels >= max(output_mapping). Callback outdata has shape (frames, len(output_mapping)).
            device: Device selection for this stream. `int` or `(in_idx, out_idx)`; input part is ignored.
            callback: Callback signature like sounddevice:
                callback(outdata, frames, time_info, status) -> None
                (The 5-arg form callback(indata, outdata, frames, time_info, status) is also accepted.)
        """
        # For OutputStream, sounddevice uses `mapping` to mean output mapping.
        if mapping is not None:
            mapping = list(mapping)
        if output_mapping is not None:
            output_mapping = list(output_mapping)
        if output_mapping is None:
            output_mapping = mapping
        elif mapping is not None and mapping != output_mapping:
            raise TypeError("Provide only one of mapping or output_mapping")

        super().__init__(
            kind="output",
            samplerate=samplerate,
            blocksize=blocksize,
            latency=latency,
            channels=channels,
            output_mapping=output_mapping,
            device=device,
            callback=callback,
            pacing=pacing,
        )


@dataclass
class LongRecordingHandle:
    """Handle for a long-running disk recording session."""
    path: str
    _proc: Optional[subprocess.Popen]
    _client: AudioDeviceClient
    _session_id: str = ""
    _postprocess_stop: Optional["threading.Event"] = None
    _postprocess_thread: Optional["threading.Thread"] = None

    def stop(self) -> None:
        """Stop the long recording session and release resources."""
        try:
            self._client.request({"cmd": "session_stop", "session_id": self._session_id})
        finally:
            if self._postprocess_stop is not None:
                try:
                    self._postprocess_stop.set()
                except Exception:
                    pass
            if self._postprocess_thread is not None:
                try:
                    self._postprocess_thread.join()
                except Exception:
                    pass
            self._client.close()
            if self._proc is not None:
                try:
                    self._proc.terminate()
                except Exception:
                    pass

    def wait(self) -> str:
        """Return the output path (kept for compatibility)."""
        if self._postprocess_thread is not None:
            try:
                self._postprocess_thread.join()
            except Exception:
                pass
        return self.path


def _rec_long_rotated_path(base_path: str, seg_idx: int) -> str:
    # Keep consistent with audio_engine/src/tasks/recorder.rs rotated_path()
    if int(seg_idx) <= 0:
        return base_path
    p = Path(base_path)
    stem = p.stem or "record"
    ext = p.suffix[1:] if p.suffix else "wav"
    parent = p.parent
    return str(parent / f"{stem}_{int(seg_idx):05}.{ext}")


def _wav_map_channels_atomic(path: str, *, mapping_cols: list[int], block_frames: int = 65536) -> None:
    """
    Rewrite an int16 PCM WAV file keeping only selected channels (0-based cols).
    Uses atomic replace (write to .tmp then os.replace).
    """
    if not mapping_cols:
        return

    tmp_path = f"{path}.tmp"
    try:
        with wave.open(path, "rb") as r:
            ch = int(r.getnchannels())
            sw = int(r.getsampwidth())
            sr = int(r.getframerate())
            nframes = int(r.getnframes())

            if ch <= 0 or sw <= 0 or sr <= 0:
                return
            if sw != 2:
                raise ValueError(f"only 16-bit PCM wav is supported for mapping (sampwidth={sw})")
            if any((ci < 0 or ci >= ch) for ci in mapping_cols):
                raise ValueError(f"mapping out of range: file has {ch} channels, cols={mapping_cols}")

            with wave.open(tmp_path, "wb") as w:
                w.setnchannels(int(len(mapping_cols)))
                w.setsampwidth(2)
                w.setframerate(int(sr))

                frames_left = nframes
                while frames_left > 0:
                    n = int(min(int(block_frames), frames_left))
                    frames = r.readframes(n)
                    if not frames:
                        break
                    pcm = np.frombuffer(frames, dtype="<i2")
                    # Defensive: ensure whole frames.
                    if pcm.size % ch != 0:
                        pcm = pcm[: (pcm.size // ch) * ch]
                    if pcm.size == 0:
                        break
                    pcm = pcm.reshape(-1, ch)
                    pcm2 = pcm[:, mapping_cols]
                    w.writeframes(pcm2.astype("<i2", copy=False).tobytes(order="C"))
                    frames_left -= int(pcm.shape[0])

        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def _rec_long_postprocess_loop(base_path_abs: str, *, mapping_cols: list[int], stop: "threading.Event") -> None:
    # Process segment i only when segment i+1 exists (ensures segment i is closed).
    next_idx = 0
    sleep_s = 0.2

    def _exists(idx: int) -> bool:
        return os.path.exists(_rec_long_rotated_path(base_path_abs, idx))

    def _stable(path: str) -> bool:
        try:
            s1 = os.path.getsize(path)
            time.sleep(sleep_s)
            s2 = os.path.getsize(path)
            return int(s1) == int(s2) and int(s2) > 0
        except Exception:
            return False

    while not stop.is_set():
        p_i = _rec_long_rotated_path(base_path_abs, next_idx)
        p_next = _rec_long_rotated_path(base_path_abs, next_idx + 1)
        if os.path.exists(p_i) and os.path.exists(p_next):
            try:
                _wav_map_channels_atomic(p_i, mapping_cols=mapping_cols)
            except Exception:
                # Best-effort: skip this segment and try next time.
                time.sleep(sleep_s)
                continue
            next_idx += 1
            continue
        time.sleep(sleep_s)

    # Stop requested: process any remaining segments once they look stable.
    while True:
        p_i = _rec_long_rotated_path(base_path_abs, next_idx)
        if not os.path.exists(p_i):
            break
        if not _stable(p_i):
            # Give it a bit more time to finalize.
            time.sleep(sleep_s)
            if not _stable(p_i):
                break
        try:
            _wav_map_channels_atomic(p_i, mapping_cols=mapping_cols)
        except Exception:
            break
        next_idx += 1


def rec_long(
    path: str,
    *,
    rotate_s: float = 300.0,
    samplerate: Optional[int] = None,
    channels: Optional[int] = None,
    device_in: Optional[int] = None,
    rb_seconds: Optional[int] = None,
    mapping: Optional[list[int]] = None,
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
        mapping: 1-based input channel mapping; when given, the engine records `channels` (or default)
            and each rotated WAV segment is post-processed to keep only the selected channels.
            Format: non-empty list of 1-based channel indices, e.g. [1] or [1, 3, 2].
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
    mapping_cols: Optional[list[int]] = None
    if mapping is not None:
        m = list(mapping) if isinstance(mapping, (list, tuple)) else None
        if not m:
            raise ValueError("mapping must be a non-empty sequence")
        cols = []
        for ch1 in m:
            ci = int(ch1) - 1
            if ci < 0:
                raise ValueError("mapping channel must be >= 1 (1-based)")
            cols.append(ci)
        mapping_cols = cols
        need_ch = int(max(mapping_cols) + 1)
        if int(ch) < int(need_ch):
            raise ValueError(f"channels ({ch}) must be >= max(mapping) ({need_ch})")

    path_abs = os.path.abspath(path)

    hostapi_eff, dev_in, _dev_out = _resolve_hostapi_and_devices(
        hostapi=None,
        device_in=device_in,
        device_out=None,
    )
    backend_eff, engine_hostapi, _ = _hostapi_display_to_engine(hostapi_eff)

    session_id = _next_session_id()
    c = AudioDeviceClient(default.host, default.port, timeout=default.timeout)
    c.request(
        {
            "cmd": "session_start",
            "session_id": session_id,
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

    h = LongRecordingHandle(path=path_abs, _proc=proc, _client=c, _session_id=session_id)
    if mapping_cols is not None:
        import threading

        stop = threading.Event()
        t = threading.Thread(
            target=_rec_long_postprocess_loop,
            args=(path_abs,),
            kwargs={"mapping_cols": mapping_cols, "stop": stop},
            daemon=True,
        )
        t.start()
        h._postprocess_stop = stop
        h._postprocess_thread = t
    return h