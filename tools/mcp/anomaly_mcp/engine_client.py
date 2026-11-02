"""One-shot TCP client for the engine endpoint: fresh connection per request."""

import socket
import time

from . import protocol

CONNECT_TIMEOUT = 1.0


class EngineNotRunning(Exception):
    """Nothing is listening (connection refused or connect timed out)."""


class EngineTimeout(Exception):
    """Connected, but no reply arrived in time. The request may still run later."""


class EngineError(Exception):
    """The engine replied '-' (message = reply text), or broke the protocol."""


class EngineClient:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port

    def request(self, op: str, arg: str = "", timeout: float = 10.0) -> str:
        """Send one request, return the text of a '+' reply. Raises on anything else."""
        try:
            frame = protocol.encode_frame(protocol.build_request(op, arg))
        except protocol.ProtocolError as e:
            raise EngineError(f"request too large: {e}") from e
        try:
            sock = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT)
        except (ConnectionRefusedError, socket.timeout) as e:
            raise EngineNotRunning(f"no engine listening on {self.host}:{self.port}") from e
        except OSError as e:
            raise EngineNotRunning(f"cannot connect to {self.host}:{self.port}: {e}") from e
        with sock:
            deadline = time.monotonic() + timeout
            try:
                sock.settimeout(timeout)
                sock.sendall(frame)
                header = _recv_exact(sock, protocol.HEADER.size, deadline)
                payload = _recv_exact(sock, protocol.decode_length(header), deadline)
            except socket.timeout as e:
                raise EngineTimeout(f"no reply to '{op}' within {timeout:g} s") from e
            except protocol.ProtocolError as e:
                raise EngineError(str(e)) from e
            except OSError as e:
                raise EngineError(f"connection lost during '{op}': {e}") from e
        try:
            ok, text = protocol.parse_reply(payload)
        except protocol.ProtocolError as e:
            raise EngineError(str(e)) from e
        if not ok:
            raise EngineError(text)
        return text

    def status(self, timeout: float = 2.0) -> dict[str, int]:
        return protocol.parse_status(self.request("status", timeout=timeout))


def _recv_exact(sock: socket.socket, n: int, deadline: float) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout()
        sock.settimeout(remaining)
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EngineError("engine closed the connection before replying")
        buf += chunk
    return bytes(buf)
