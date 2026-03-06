from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class RpcReply:
    ok: bool
    data: Dict[str, Any]
    err: Optional[str]


class AudioDeviceClient:
    """
    TCP JSON lines client.

    Protocol:
    - Send one JSON object per line (UTF-8)
    - Receive one JSON object per line
    """

    def __init__(self, host: str, port: int, timeout: float = 5.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._rxbuf = b""

    def connect(self) -> None:
        if self._sock is not None:
            return
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        s.settimeout(self.timeout)
        self._sock = s

    def close(self) -> None:
        self._rxbuf = b""
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self) -> "AudioDeviceClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.connect()
        assert self._sock is not None

        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self._sock.sendall(line)

        deadline = time.time() + float(self.timeout)
        while b"\n" not in self._rxbuf:
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                if time.time() >= deadline:
                    raise TimeoutError("Engine response timed out.")
                continue
            if not chunk:
                raise ConnectionError("No response from engine (connection closed).")
            self._rxbuf += chunk

        resp_line, _, rest = self._rxbuf.partition(b"\n")
        self._rxbuf = rest

        resp = json.loads(resp_line.decode("utf-8"))
        if not isinstance(resp, dict):
            raise ValueError(f"Bad response: {resp!r}")
        if not resp.get("ok", False):
            raise RuntimeError(resp.get("err") or "Engine returned ok=false")
        data = resp.get("data")
        if not isinstance(data, dict):
            raise ValueError(f"Bad data: {data!r}")
        return data

