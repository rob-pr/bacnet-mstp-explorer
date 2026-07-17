"""BACnet MS/TP (RS-485) frame decoding.

Implements the datalink framing from ANSI/ASHRAE 135 clause 9: preamble
detection, the header CRC-8 and data CRC-16 algorithms, and a streaming
parser that turns an arbitrary byte stream off the wire into validated
frames.

This module is transport-agnostic: feed it bytes from anywhere (a serial
port, a capture file, a test) via ``FrameParser.feed``.
"""

from __future__ import annotations

from dataclasses import dataclass

PREAMBLE = b"\x55\xff"

# MS/TP frame types (ASHRAE 135 Table 9-1).
FRAME_TYPES = {
    0x00: "Token",
    0x01: "Poll For Master",
    0x02: "Reply To Poll For Master",
    0x03: "Test_Request",
    0x04: "Test_Response",
    0x05: "BACnet Data Expecting Reply",
    0x06: "BACnet Data Not Expecting Reply",
    0x07: "Reply Postponed",
}

BROADCAST_ADDR = 0xFF


def frame_type_name(ftype: int) -> str:
    if ftype in FRAME_TYPES:
        return FRAME_TYPES[ftype]
    if 0x80 <= ftype <= 0xFF:
        return f"Proprietary (0x{ftype:02X})"
    return f"Reserved (0x{ftype:02X})"


def is_data_frame(ftype: int) -> bool:
    """True for frame types that carry a BACnet NPDU payload."""
    return ftype in (0x03, 0x04, 0x05, 0x06)


def _crc8_header(data: bytes) -> int:
    """Accumulate the MS/TP header CRC over ``data`` (ASHRAE 135 9.5.1)."""
    crc = 0xFF
    for value in data:
        crc = crc ^ value
        crc = (
            crc
            ^ (crc << 1)
            ^ (crc << 2)
            ^ (crc << 3)
            ^ (crc << 4)
            ^ (crc << 5)
            ^ (crc << 6)
            ^ (crc << 7)
        )
        crc = (crc & 0xFE) ^ ((crc >> 8) & 0x01)
    return crc & 0xFF


def header_crc(data: bytes) -> int:
    """The header CRC byte transmitted on the wire (ones-complement)."""
    return (~_crc8_header(data)) & 0xFF


def _crc16_data(data: bytes) -> int:
    """Accumulate the MS/TP data CRC over ``data`` (ASHRAE 135 9.5.2)."""
    crc = 0xFFFF
    for value in data:
        crc_low = (crc & 0xFF) ^ value
        crc = (
            (crc >> 8)
            ^ (crc_low << 8)
            ^ (crc_low << 3)
            ^ (crc_low << 12)
            ^ (crc_low >> 4)
            ^ (crc_low & 0x0F)
            ^ ((crc_low & 0x0F) << 7)
        ) & 0xFFFF
    return crc


def data_crc(data: bytes) -> int:
    """The 16-bit data CRC transmitted on the wire (ones-complement)."""
    return (~_crc16_data(data)) & 0xFFFF


@dataclass
class Frame:
    frame_type: int
    destination: int
    source: int
    data: bytes = b""
    header_crc_ok: bool = True
    data_crc_ok: bool = True

    @property
    def type_name(self) -> str:
        return frame_type_name(self.frame_type)

    @property
    def is_data(self) -> bool:
        return is_data_frame(self.frame_type)

    @property
    def crc_ok(self) -> bool:
        return self.header_crc_ok and self.data_crc_ok

    def dest_str(self) -> str:
        return "broadcast" if self.destination == BROADCAST_ADDR else str(self.destination)


def build_frame(frame_type: int, destination: int, source: int, data: bytes = b"") -> bytes:
    """Encode a well-formed MS/TP frame (used by tests and, if wanted, TX)."""
    header = bytes([frame_type, destination, source, (len(data) >> 8) & 0xFF, len(data) & 0xFF])
    out = bytearray(PREAMBLE)
    out += header
    out.append(header_crc(header))
    if data:
        out += data
        crc = data_crc(data)
        out += bytes([crc & 0xFF, (crc >> 8) & 0xFF])
    return bytes(out)


class FrameParser:
    """Streaming MS/TP frame parser.

    Feed it whatever bytes arrive off the serial line; it resynchronises on
    the ``55 FF`` preamble, validates both CRCs, and yields :class:`Frame`
    objects. Malformed or corrupt frames are still yielded (with the relevant
    ``*_ok`` flag cleared) so a monitor can surface bus errors.
    """

    MAX_DATA_LEN = 1501  # ASHRAE 135 caps MS/TP data at 501; allow headroom.

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes):
        self._buf.extend(chunk)
        yield from self._drain()

    def _drain(self):
        buf = self._buf
        while True:
            start = buf.find(PREAMBLE)
            if start < 0:
                # Keep only a trailing byte that might be a partial preamble.
                if buf and buf[-1] == 0x55:
                    del buf[:-1]
                else:
                    buf.clear()
                return
            if start:
                del buf[:start]

            # Need preamble(2) + header(5) + header-crc(1) = 8 bytes.
            if len(buf) < 8:
                return

            frame_type, dest, src = buf[2], buf[3], buf[4]
            length = (buf[5] << 8) | buf[6]
            hdr_crc_rx = buf[7]
            hdr_ok = header_crc(bytes(buf[2:7])) == hdr_crc_rx

            if not hdr_ok or length > self.MAX_DATA_LEN:
                # Bad header: drop the preamble and resync past it.
                del buf[:2]
                continue

            if length == 0:
                yield Frame(frame_type, dest, src, b"", hdr_ok, True)
                del buf[:8]
                continue

            total = 8 + length + 2  # + data + data-crc
            if len(buf) < total:
                return  # Wait for the rest of the payload.

            payload = bytes(buf[8 : 8 + length])
            data_crc_rx = buf[8 + length] | (buf[8 + length + 1] << 8)
            data_ok = data_crc(payload) == data_crc_rx
            yield Frame(frame_type, dest, src, payload, hdr_ok, data_ok)
            del buf[:total]


def _self_test() -> None:
    # Round-trip a Poll For Master (no data) and an I-Am data frame.
    pfm = build_frame(0x01, 0xFF, 0x05)
    iam_data = bytes.fromhex("01001008000ac6b0")  # dummy NPDU-ish payload
    data_frame = build_frame(0x06, 0xFF, 0x0C, iam_data)

    parser = FrameParser()
    frames = list(parser.feed(b"\x00\x12" + pfm)) + list(parser.feed(data_frame))
    assert frames[0].type_name == "Poll For Master", frames[0]
    assert frames[0].crc_ok
    assert frames[1].data == iam_data, frames[1]
    assert frames[1].crc_ok
    print("mstp self-test OK:", [f.type_name for f in frames])


if __name__ == "__main__":
    _self_test()
