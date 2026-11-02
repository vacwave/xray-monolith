"""Fake engine endpoint implementing PROTOCOL.md for tests."""

import socket
import socketserver
import threading

import pytest

from anomaly_mcp import protocol

NO_REPLY = object()  # hold the connection open, never reply
CLOSE = object()  # close the connection without replying


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("client closed")
        buf += chunk
    return buf


class FakeEngine:
    """Replies to each request with handler(op, arg) -> reply payload bytes, NO_REPLY or CLOSE."""

    def __init__(self, handler):
        self.handler = handler
        self.requests = []
        self.release = threading.Event()  # set on stop so NO_REPLY handlers exit
        engine = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                try:
                    length = protocol.decode_length(_recv_exact(self.request, 4))
                    text = _recv_exact(self.request, length).decode("utf-8")
                except ConnectionError:
                    return
                op, _, arg = text.partition(" ")
                engine.requests.append((op, arg))
                reply = engine.handler(op, arg)
                if reply is CLOSE:
                    return
                if reply is NO_REPLY:
                    engine.release.wait(10)
                    return
                self.request.sendall(protocol.encode_frame(reply))

        socketserver.ThreadingTCPServer.allow_reuse_address = False
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


def default_handler(op, arg):
    if op == "status":
        return b"+frame=1234\npaused=0\nlevel=1\npending=2\nfuture_key=abc"
    if op == "log":
        return ("+" + "\n".join(f"line {i}" for i in range(int(arg or 50)))).encode()
    if op == "console":
        return b"+"
    if op == "eval":
        if arg == "return 1+1":
            return b"+2"
        if arg == "error()":
            return b"-[string \"error()\"]:1: boom"
        if arg == "hang":
            return NO_REPLY
        return b"+"
    return f"-unknown op: {op}".encode()


@pytest.fixture
def engine(monkeypatch):
    fake = FakeEngine(default_handler)
    monkeypatch.setenv("ANOMALY_MCP_PORT", str(fake.port))
    yield fake
    fake.stop()


@pytest.fixture
def dead_port(monkeypatch):
    """A port with nothing listening on it."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    monkeypatch.setenv("ANOMALY_MCP_PORT", str(port))
    return port
