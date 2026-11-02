import pytest

from anomaly_mcp import protocol


def test_frame_round_trip_with_rest():
    payload = "eval return 'ä'\nprint(1)".encode()
    frame = protocol.encode_frame(payload)
    assert frame[:4] == len(payload).to_bytes(4, "little")
    assert protocol.decode_frame(frame + b"xyz") == (payload, b"xyz")


def test_decode_incomplete():
    frame = protocol.encode_frame(b"hello")
    assert protocol.decode_frame(frame[:3]) is None
    assert protocol.decode_frame(frame[:-1]) is None


def test_empty_frame():
    assert protocol.decode_frame(protocol.encode_frame(b"")) == (b"", b"")


def test_size_limit():
    protocol.encode_frame(b"x" * protocol.MAX_PAYLOAD)
    with pytest.raises(protocol.ProtocolError):
        protocol.encode_frame(b"x" * (protocol.MAX_PAYLOAD + 1))
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_length((protocol.MAX_PAYLOAD + 1).to_bytes(4, "little"))


def test_build_request():
    assert protocol.build_request("status") == b"status"
    assert protocol.build_request("log", "20") == b"log 20"
    assert protocol.build_request("eval", " a\nb ") == b"eval  a\nb "


def test_parse_reply():
    assert protocol.parse_reply(b"+") == (True, "")
    assert protocol.parse_reply(b"+2") == (True, "2")
    assert protocol.parse_reply(b"-bad") == (False, "bad")
    ok, text = protocol.parse_reply(b"+caf\xe9")  # cp1251/latin-1 bytes from the log
    assert ok and text == "caf�"
    for bad in (b"", b"?x"):
        with pytest.raises(protocol.ProtocolError):
            protocol.parse_reply(bad)


def test_parse_status():
    text = "frame=10\npaused=1\nlevel=0\npending=3\nnew=hello\nnoequals\n"
    assert protocol.parse_status(text) == {"frame": 10, "paused": 1, "level": 0, "pending": 3}
