"""Wire format from PROTOCOL.md: u32 LE length + UTF-8 payload, max 1 MiB."""

import struct

MAX_PAYLOAD = 1 << 20
HEADER = struct.Struct("<I")


class ProtocolError(Exception):
    pass


def encode_frame(payload: bytes) -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ProtocolError(f"payload too large: {len(payload)} bytes (max {MAX_PAYLOAD})")
    return HEADER.pack(len(payload)) + payload


def decode_length(header: bytes) -> int:
    (length,) = HEADER.unpack(header)
    if length > MAX_PAYLOAD:
        raise ProtocolError(f"frame too large: {length} bytes (max {MAX_PAYLOAD})")
    return length


def decode_frame(buf: bytes) -> tuple[bytes, bytes] | None:
    """Split one frame off the front of buf: (payload, rest), or None if incomplete."""
    if len(buf) < HEADER.size:
        return None
    length = decode_length(buf[: HEADER.size])
    end = HEADER.size + length
    if len(buf) < end:
        return None
    return buf[HEADER.size : end], buf[end:]


def build_request(op: str, arg: str = "") -> bytes:
    """Request payload: op, then one space and the argument verbatim (if any)."""
    text = f"{op} {arg}" if arg else op
    return text.encode("utf-8")


def parse_reply(payload: bytes) -> tuple[bool, str]:
    """Reply payload -> (ok, text). ok is True for '+', False for '-'."""
    if not payload or payload[:1] not in (b"+", b"-"):
        raise ProtocolError(f"bad reply status byte: {payload[:16]!r}")
    return payload[:1] == b"+", payload[1:].decode("utf-8", errors="replace")


def parse_status(text: str) -> dict[str, int]:
    """key=value lines -> dict of ints. Lines that are not key=<int> are ignored."""
    result = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        try:
            result[key.strip()] = int(value.strip())
        except ValueError:
            continue
    return result
