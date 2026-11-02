import time

import pytest

from conftest import CLOSE

from anomaly_mcp.engine_client import EngineClient, EngineError, EngineNotRunning, EngineTimeout


def test_status(engine):
    client = EngineClient("127.0.0.1", engine.port)
    assert client.status() == {"frame": 1234, "paused": 0, "level": 1, "pending": 2}
    assert engine.requests == [("status", "")]


def test_eval_and_args_verbatim(engine):
    client = EngineClient("127.0.0.1", engine.port)
    assert client.request("eval", "return 1+1") == "2"
    assert client.request("console", "cfg_save") == ""
    assert engine.requests == [("eval", "return 1+1"), ("console", "cfg_save")]


def test_error_reply(engine):
    client = EngineClient("127.0.0.1", engine.port)
    with pytest.raises(EngineError, match="boom"):
        client.request("eval", "error()")
    with pytest.raises(EngineError, match="unknown op: nope"):
        client.request("nope")


def test_timeout(engine):
    client = EngineClient("127.0.0.1", engine.port)
    t0 = time.monotonic()
    with pytest.raises(EngineTimeout):
        client.request("eval", "hang", timeout=0.5)
    assert 0.4 < time.monotonic() - t0 < 2


def test_not_running(dead_port):
    t0 = time.monotonic()
    with pytest.raises(EngineNotRunning):
        EngineClient("127.0.0.1", dead_port).request("status")
    assert time.monotonic() - t0 < 1.5


def test_large_reply(engine):
    big = "x" * 900_000
    engine.handler = lambda op, arg: ("+" + big).encode()
    assert EngineClient("127.0.0.1", engine.port).request("log", "2000") == big


def test_closed_without_reply(engine):
    engine.handler = lambda op, arg: CLOSE
    with pytest.raises(EngineError, match="closed"):
        EngineClient("127.0.0.1", engine.port).request("status", timeout=2)


def test_request_too_large(engine):
    with pytest.raises(EngineError, match="too large"):
        EngineClient("127.0.0.1", engine.port).request("eval", "x" * (1 << 20))
    assert engine.requests == []
