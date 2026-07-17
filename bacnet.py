"""BACnet NPDU / APDU codec for an MS/TP explorer.

Covers the application-layer services this tool actually uses as a client:
Who-Is / I-Am (discovery), ReadProperty and WriteProperty (the read/write
logic), plus decoding of ComplexACK / Error / Reject / Abort responses. It is
not a complete BACnet stack, but it round-trips everything the GUI sends and
understands the replies it gets back.

Encoders build APDUs; decoders turn received APDUs into readable summaries and
Python values. NPDU wrapping (version + control octet) is handled here too.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# ---------------------------------------------------------------- tables ---

PDU_TYPES = {
    0x0: "Confirmed-Request",
    0x1: "Unconfirmed-Request",
    0x2: "SimpleACK",
    0x3: "ComplexACK",
    0x4: "SegmentACK",
    0x5: "Error",
    0x6: "Reject",
    0x7: "Abort",
}

UNCONFIRMED_SERVICES = {
    0: "I-Am", 1: "I-Have", 2: "unconfirmedCOVNotification",
    3: "unconfirmedEventNotification", 4: "unconfirmedPrivateTransfer",
    5: "unconfirmedTextMessage", 6: "timeSynchronization", 7: "Who-Has",
    8: "Who-Is", 9: "utcTimeSynchronization", 10: "writeGroup",
}

CONFIRMED_SERVICES = {
    0: "acknowledgeAlarm", 1: "confirmedCOVNotification",
    2: "confirmedEventNotification", 3: "getAlarmSummary",
    4: "getEnrollmentSummary", 5: "subscribeCOV", 6: "atomicReadFile",
    7: "atomicWriteFile", 8: "addListElement", 9: "removeListElement",
    10: "createObject", 11: "deleteObject", 12: "readProperty",
    14: "readPropertyMultiple", 15: "writeProperty", 16: "writePropertyMultiple",
    17: "deviceCommunicationControl", 20: "reinitializeDevice", 24: "readRange",
}

OBJECT_TYPES = {
    0: "analog-input", 1: "analog-output", 2: "analog-value",
    3: "binary-input", 4: "binary-output", 5: "binary-value",
    8: "device", 13: "multi-state-input", 14: "multi-state-output",
    19: "multi-state-value", 16: "program", 17: "schedule",
    10: "file", 15: "loop", 20: "trend-log", 12: "group",
    28: "load-control", 40: "characterstring-value", 56: "network-port",
}
OBJECT_TYPES_BY_NAME = {v: k for k, v in OBJECT_TYPES.items()}

PROPERTY_IDS = {
    4: "active-text", 12: "application-software-version", 22: "cov-increment",
    28: "description", 36: "event-state", 44: "firmware-revision",
    46: "inactive-text", 65: "max-pres-value", 69: "min-pres-value",
    70: "model-name", 74: "number-of-states", 75: "object-identifier",
    76: "object-list", 77: "object-name", 79: "object-type",
    81: "out-of-service", 84: "polarity", 85: "present-value",
    87: "priority-array", 98: "protocol-version", 103: "reliability",
    104: "relinquish-default", 110: "state-text", 111: "status-flags",
    112: "system-status", 117: "units", 120: "vendor-identifier",
    121: "vendor-name", 139: "protocol-revision", 371: "property-list",
}
PROPERTY_IDS_BY_NAME = {v: k for k, v in PROPERTY_IDS.items()}

# Common enumerations for readable display.
ERROR_CLASSES = {0: "device", 1: "object", 2: "property", 3: "resources",
                 4: "security", 5: "services", 6: "vt", 7: "communication"}
ERROR_CODES = {
    2: "configuration-in-progress", 25: "no-space-for-object",
    31: "unknown-object", 32: "unknown-property", 40: "write-access-denied",
    42: "invalid-data-type", 37: "value-out-of-range", 9: "inconsistent-parameters",
    27: "operational-problem", 45: "unknown-property (deprecated)",
}
ABORT_REASONS = {
    0: "other", 1: "buffer-overflow", 2: "invalid-apdu-in-this-state",
    3: "preempted-by-higher-priority-task", 4: "segmentation-not-supported",
    9: "tsm-timeout", 10: "apdu-too-long",
}
REJECT_REASONS = {
    0: "other", 1: "buffer-overflow", 2: "inconsistent-parameters",
    3: "invalid-parameter-data-type", 4: "invalid-tag", 5: "missing-required-parameter",
    6: "parameter-out-of-range", 7: "too-many-arguments", 8: "undefined-enumeration",
    9: "unrecognized-service",
}
SEGMENTATION = {0: "segmented-both", 1: "segmented-transmit",
                2: "segmented-receive", 3: "no-segmentation"}

# Max APDU length accepted, encoded (ASHRAE 135 20.1.2.5). MS/TP uses 480.
MAX_APDU_480 = 3
DEVICE_INSTANCE_UNKNOWN = 0x3FFFFF


def object_type_name(t: int) -> str:
    return OBJECT_TYPES.get(t, f"object-type-{t}")


def property_name(p: int) -> str:
    return PROPERTY_IDS.get(p, f"property-{p}")


# --------------------------------------------------------------- results ---

@dataclass
class Decoded:
    summary: str
    details: list[str] = field(default_factory=list)


@dataclass
class ReadResult:
    """Outcome of a ReadProperty/WriteProperty transaction as seen by the app."""
    ok: bool
    invoke_id: int = 0
    object_id: tuple[int, int] | None = None   # (type, instance)
    property_id: int | None = None
    values: list = field(default_factory=list)  # decoded python values
    display: str = ""                            # human-readable value string
    error: str = ""                              # populated when ok is False


# ------------------------------------------------------------ low-level ----

@dataclass
class Tag:
    number: int
    is_context: bool
    lvt: int          # length-value-type nibble (6=opening, 7=closing)
    value: bytes = b""

    @property
    def is_opening(self) -> bool:
        return self.lvt == 6

    @property
    def is_closing(self) -> bool:
        return self.lvt == 7


class Reader:
    """Cursor over a byte buffer with BACnet tag helpers."""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def eof(self) -> bool:
        return self.pos >= len(self.data)

    def u8(self) -> int:
        b = self.data[self.pos]
        self.pos += 1
        return b

    def take(self, n: int) -> bytes:
        b = self.data[self.pos : self.pos + n]
        if len(b) != n:
            raise IndexError("unexpected end of APDU")
        self.pos += n
        return b

    def peek_tag_byte(self) -> int:
        return self.data[self.pos]

    def read_tag(self) -> Tag:
        tag_byte = self.u8()
        number = tag_byte >> 4
        is_context = bool(tag_byte & 0x08)
        lvt = tag_byte & 0x07
        if number == 0xF:  # extended tag number
            number = self.u8()
        if lvt == 6 or lvt == 7:  # opening / closing tag: no content
            return Tag(number, is_context, lvt, b"")
        if not is_context and number == 1:  # application boolean: value in lvt
            return Tag(number, is_context, 1, bytes([lvt & 1]))
        if lvt == 5:  # extended length
            length = self.u8()
            if length == 254:
                length = int.from_bytes(self.take(2), "big")
            elif length == 255:
                length = int.from_bytes(self.take(4), "big")
        else:
            length = lvt
        return Tag(number, is_context, lvt, self.take(length))


def _uint(b: bytes) -> int:
    return int.from_bytes(b, "big") if b else 0


def encode_object_id(obj_type: int, instance: int) -> bytes:
    value = ((obj_type & 0x3FF) << 22) | (instance & 0x3FFFFF)
    return value.to_bytes(4, "big")


def decode_object_id(b: bytes) -> tuple[int, int]:
    value = _uint(b)
    return value >> 22, value & 0x3FFFFF


def object_id_str(obj_type: int, instance: int) -> str:
    return f"{object_type_name(obj_type)},{instance}"


# ----------------------------------------------------------- tag encoders --

def _min_bytes(value: int) -> bytes:
    if value == 0:
        return b"\x00"
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


def ctx_tag(number: int, content: bytes) -> bytes:
    """Context-tagged primitive of arbitrary (short) length."""
    n = len(content)
    if n <= 4:
        return bytes([(number << 4) | 0x08 | n]) + content
    return bytes([(number << 4) | 0x08 | 5, n]) + content


def ctx_unsigned(number: int, value: int) -> bytes:
    return ctx_tag(number, _min_bytes(value))


def ctx_object_id(number: int, obj_type: int, instance: int) -> bytes:
    return bytes([(number << 4) | 0x08 | 4]) + encode_object_id(obj_type, instance)


def open_tag(number: int) -> bytes:
    return bytes([(number << 4) | 0x08 | 6])


def close_tag(number: int) -> bytes:
    return bytes([(number << 4) | 0x08 | 7])


def _app_tag(number: int, content: bytes) -> bytes:
    n = len(content)
    if n <= 4:
        return bytes([(number << 4) | n]) + content
    if n <= 253:
        return bytes([(number << 4) | 5, n]) + content
    return bytes([(number << 4) | 5, 254]) + n.to_bytes(2, "big") + content


# Value types the write panel can produce.
WRITE_TYPES = ["Real", "Unsigned", "Signed", "Boolean", "Enumerated",
               "Null", "CharacterString"]


def encode_application_value(value_type: str, text: str) -> bytes:
    """Encode a single application-tagged value from GUI (type, text)."""
    vt = value_type.lower()
    if vt == "null":
        return bytes([0x00])
    if vt == "boolean":
        truthy = text.strip().lower() in ("1", "true", "active", "on", "yes")
        return bytes([0x10 | (1 if truthy else 0)])
    if vt == "real":
        return bytes([0x44]) + struct.pack(">f", float(text))
    if vt == "unsigned":
        v = int(text, 0)
        if v < 0:
            raise ValueError("unsigned value must be >= 0")
        return _app_tag(2, _min_bytes(v))
    if vt == "signed":
        v = int(text, 0)
        n = max(1, (v.bit_length() + 8) // 8)
        return _app_tag(3, v.to_bytes(n, "big", signed=True))
    if vt == "enumerated":
        return _app_tag(9, _min_bytes(int(text, 0)))
    if vt == "characterstring":
        body = b"\x00" + text.encode("utf-8")  # 0 = UTF-8 encoding
        return _app_tag(7, body)
    raise ValueError(f"unsupported value type: {value_type}")


# ---------------------------------------------------------- value decode ---

def decode_application_value(tag: Tag):
    """Decode one application-tagged value -> (python_value, display_str)."""
    n, v = tag.number, tag.value
    if n == 0:
        return None, "Null"
    if n == 1:
        b = bool(v[0]) if v else False
        return b, "TRUE" if b else "FALSE"
    if n == 2:
        u = _uint(v)
        return u, str(u)
    if n == 3:
        s = int.from_bytes(v, "big", signed=True) if v else 0
        return s, str(s)
    if n == 4:
        f = struct.unpack(">f", v)[0] if len(v) == 4 else 0.0
        return f, f"{f:g}"
    if n == 5:
        d = struct.unpack(">d", v)[0] if len(v) == 8 else 0.0
        return d, f"{d:g}"
    if n == 6:
        return v, v.hex()
    if n == 7:  # character string
        enc = v[0] if v else 0
        raw = v[1:]
        try:
            s = raw.decode("utf-8") if enc == 0 else raw.decode("latin-1")
        except UnicodeDecodeError:
            s = raw.decode("latin-1", "replace")
        return s, s
    if n == 8:  # bit string
        return v, "bits:" + v.hex()
    if n == 9:
        u = _uint(v)
        return u, str(u)
    if n == 10:  # date
        return v, "date:" + v.hex()
    if n == 11:  # time
        return v, "time:" + v.hex()
    if n == 12:
        ot, inst = decode_object_id(v)
        return (ot, inst), object_id_str(ot, inst)
    return v, v.hex()


# --------------------------------------------------------- request build ---

def npdu_wrap(apdu: bytes, expecting_reply: bool) -> bytes:
    control = 0x04 if expecting_reply else 0x00
    return bytes([0x01, control]) + apdu


def build_who_is(low: int | None = None, high: int | None = None) -> bytes:
    """Unconfirmed Who-Is APDU (NPDU-wrapped, not expecting reply)."""
    apdu = bytes([0x10, 0x08])
    if low is not None and high is not None:
        apdu += ctx_unsigned(0, low) + ctx_unsigned(1, high)
    return npdu_wrap(apdu, expecting_reply=False)


def build_read_property(invoke_id: int, obj_type: int, instance: int,
                        prop_id: int, array_index: int | None = None) -> bytes:
    apdu = bytes([0x00, MAX_APDU_480, invoke_id & 0xFF, 0x0C])  # confirmed, readProperty
    apdu += ctx_object_id(0, obj_type, instance)
    apdu += ctx_unsigned(1, prop_id)
    if array_index is not None:
        apdu += ctx_unsigned(2, array_index)
    return npdu_wrap(apdu, expecting_reply=True)


def build_write_property(invoke_id: int, obj_type: int, instance: int,
                         prop_id: int, value_bytes: bytes,
                         array_index: int | None = None,
                         priority: int | None = None) -> bytes:
    apdu = bytes([0x00, MAX_APDU_480, invoke_id & 0xFF, 0x0F])  # confirmed, writeProperty
    apdu += ctx_object_id(0, obj_type, instance)
    apdu += ctx_unsigned(1, prop_id)
    if array_index is not None:
        apdu += ctx_unsigned(2, array_index)
    apdu += open_tag(3) + value_bytes + close_tag(3)
    if priority is not None:
        apdu += ctx_unsigned(4, priority)
    return npdu_wrap(apdu, expecting_reply=True)


# ------------------------------------------------------- response decode ---

def _skip_npdu(reader: Reader) -> bool:
    """Advance a Reader past the NPDU header. Returns False if not a
    BACnet APDU (e.g. version mismatch or a network-layer message)."""
    if reader.remaining() < 2:
        return False
    version = reader.u8()
    control = reader.u8()
    if version != 0x01 or (control & 0x80):
        return False
    if control & 0x20:  # DNET/DLEN/DADR
        reader.take(2)
        reader.take(reader.u8())
    if control & 0x08:  # SNET/SLEN/SADR
        reader.take(2)
        reader.take(reader.u8())
    if control & 0x20:  # hop count
        reader.u8()
    return True


def parse_response(npdu: bytes, want_invoke: int | None = None) -> ReadResult:
    """Decode a confirmed-service response NPDU into a ReadResult.

    Handles ComplexACK (readProperty), SimpleACK (writeProperty), Error,
    Reject and Abort. ``want_invoke`` lets the caller confirm invoke-id match.
    """
    reader = Reader(npdu)
    if not _skip_npdu(reader):
        return ReadResult(False, error="not a BACnet APDU")
    try:
        first = reader.u8()
        pdu_type = first >> 4
        if pdu_type == 0x3:  # ComplexACK
            invoke = reader.u8()
            service = reader.u8()
            if service != 12:
                return ReadResult(True, invoke, display=f"{CONFIRMED_SERVICES.get(service, service)}-ACK")
            return _parse_read_property_ack(reader, invoke)
        if pdu_type == 0x2:  # SimpleACK (writeProperty etc.)
            invoke = reader.u8()
            service = reader.u8()
            return ReadResult(True, invoke, display="OK",
                              property_id=None)
        if pdu_type == 0x5:  # Error
            invoke = reader.u8()
            service = reader.u8()
            return _parse_error(reader, invoke)
        if pdu_type == 0x6:  # Reject
            invoke = reader.u8()
            reason = reader.u8()
            return ReadResult(False, invoke,
                              error=f"Reject: {REJECT_REASONS.get(reason, reason)}")
        if pdu_type == 0x7:  # Abort
            invoke = reader.u8()
            reason = reader.u8()
            return ReadResult(False, invoke,
                              error=f"Abort: {ABORT_REASONS.get(reason, reason)}")
        if pdu_type == 0x4:  # SegmentACK — response was segmented
            return ReadResult(False, error="segmented response (not supported)")
    except (IndexError, ValueError, struct.error) as exc:
        return ReadResult(False, error=f"decode error: {exc}")
    return ReadResult(False, error=f"unexpected PDU type {pdu_type}")


def _parse_read_property_ack(reader: Reader, invoke: int) -> ReadResult:
    obj_id = None
    prop_id = None
    values = []
    while not reader.eof():
        tag = reader.read_tag()
        if tag.is_context and tag.number == 0:
            obj_id = decode_object_id(tag.value)
        elif tag.is_context and tag.number == 1:
            prop_id = _uint(tag.value)
        elif tag.is_context and tag.number == 2:
            pass  # array index echo
        elif tag.is_context and tag.number == 3 and tag.is_opening:
            while not reader.eof():
                inner = reader.read_tag()
                if inner.is_context and inner.number == 3 and inner.is_closing:
                    break
                if inner.is_context:
                    continue  # nested constructed value we don't model
                values.append(decode_application_value(inner))
    display = ", ".join(d for _, d in values) if values else ""
    py_values = [pv for pv, _ in values]
    return ReadResult(True, invoke, obj_id, prop_id, py_values, display)


def _parse_error(reader: Reader, invoke: int) -> ReadResult:
    try:
        cls = reader.read_tag()
        code = reader.read_tag()
        cls_v = _uint(cls.value)
        code_v = _uint(code.value)
        msg = (f"Error: {ERROR_CLASSES.get(cls_v, cls_v)} / "
               f"{ERROR_CODES.get(code_v, code_v)}")
    except (IndexError, ValueError):
        msg = "Error (undecodable)"
    return ReadResult(False, invoke, error=msg)


# ---------------------------------------------------- monitor-log decode ---

def _decode_who_is(reader: Reader, out: Decoded) -> None:
    if reader.eof():
        out.summary = "Who-Is (global broadcast)"
        return
    low = high = None
    while not reader.eof():
        tag = reader.read_tag()
        if tag.is_context and tag.number == 0:
            low = _uint(tag.value)
        elif tag.is_context and tag.number == 1:
            high = _uint(tag.value)
    if low is not None:
        out.summary = f"Who-Is (instance {low}..{high})"
    else:
        out.summary = "Who-Is"


def _decode_i_am(reader: Reader, out: Decoded) -> None:
    try:
        objid = reader.read_tag().value
        max_apdu = reader.read_tag().value
        seg = reader.read_tag().value
        vendor = reader.read_tag().value
    except (IndexError, ValueError):
        out.summary = "I-Am (truncated)"
        return
    ot, inst = decode_object_id(objid)
    out.summary = f"I-Am {object_id_str(ot, inst)} (vendor {_uint(vendor)})"
    out.details.append(f"max-apdu-length-accepted: {_uint(max_apdu)}")
    out.details.append(f"segmentation: {SEGMENTATION.get(_uint(seg), _uint(seg))}")
    out.details.append(f"__device_instance={inst}")
    out.details.append(f"__vendor_id={_uint(vendor)}")


def _decode_read_property(reader: Reader, out: Decoded, label: str) -> None:
    obj = prop = None
    while not reader.eof():
        try:
            tag = reader.read_tag()
        except (IndexError, ValueError):
            break
        if tag.is_context and tag.number == 0:
            obj = object_id_str(*decode_object_id(tag.value))
        elif tag.is_context and tag.number == 1:
            prop = property_name(_uint(tag.value))
    out.summary = " ".join(p for p in (label, obj, prop) if p)


def decode_apdu(data: bytes) -> Decoded:
    if not data:
        return Decoded("empty APDU")
    reader = Reader(data)
    first = reader.u8()
    pdu_type = first >> 4
    out = Decoded(PDU_TYPES.get(pdu_type, f"PDU-{pdu_type}"))
    try:
        if pdu_type == 0x1:  # Unconfirmed
            service = reader.u8()
            out.summary = UNCONFIRMED_SERVICES.get(service, f"unconfirmed-{service}")
            if service == 8:
                _decode_who_is(reader, out)
            elif service == 0:
                _decode_i_am(reader, out)
        elif pdu_type == 0x0:  # Confirmed request
            reader.u8()  # max segs / apdu
            invoke = reader.u8()
            service = reader.u8()
            name = CONFIRMED_SERVICES.get(service, f"confirmed-{service}")
            out.summary = f"{name} [inv {invoke}]"
            if service in (12, 15):
                _decode_read_property(reader, out, name)
        elif pdu_type in (0x2, 0x3):  # ACKs
            invoke = reader.u8()
            service = reader.u8()
            name = CONFIRMED_SERVICES.get(service, f"confirmed-{service}")
            out.summary = f"{name}-ACK [inv {invoke}]"
        elif pdu_type == 0x5:
            invoke = reader.u8()
            reader.u8()
            out.summary = f"Error [inv {invoke}]"
        elif pdu_type in (0x6, 0x7):
            invoke = reader.u8()
            out.summary = f"{PDU_TYPES[pdu_type]} [inv {invoke}]"
    except (IndexError, ValueError):
        out.details.append("(decode truncated)")
    return out


def decode_npdu(data: bytes) -> Decoded:
    """Decode an NPDU for the monitor log (returns embedded APDU summary)."""
    if not data:
        return Decoded("empty NPDU")
    reader = Reader(data)
    version = reader.u8()
    if version != 0x01:
        return Decoded(f"non-BACnet NPDU (v{version:#x})", [f"raw: {data.hex()}"])
    control = reader.u8()
    is_nsdu = bool(control & 0x80)
    details: list[str] = []
    try:
        if control & 0x20:
            dnet = int.from_bytes(reader.take(2), "big")
            dlen = reader.u8()
            dadr = reader.take(dlen)
            details.append(f"dest-net {dnet} mac {dadr.hex() or 'bcast'}")
        if control & 0x08:
            snet = int.from_bytes(reader.take(2), "big")
            slen = reader.u8()
            sadr = reader.take(slen)
            details.append(f"src-net {snet} mac {sadr.hex()}")
        if control & 0x20:
            details.append(f"hop {reader.u8()}")
    except (IndexError, ValueError):
        return Decoded("NPDU (truncated)", details)
    if is_nsdu:
        result = Decoded("Network-Layer Message")
        result.details = details
        return result
    apdu = decode_apdu(data[reader.pos:])
    apdu.details = details + apdu.details
    return apdu


# --------------------------------------------------------------- selftest --

def _self_test() -> None:
    # I-Am round trip via decode.
    objid = encode_object_id(8, 10)
    apdu = bytes([0x10, 0x00, 0xC4]) + objid + bytes([0x22, 0x05, 0xC4, 0x91, 0x00, 0x22, 0x01, 0x04])
    dec = decode_npdu(bytes([0x01, 0x00]) + apdu)
    assert "I-Am device,10" in dec.summary, dec.summary
    assert any("__device_instance=10" in d for d in dec.details)

    # ReadProperty request encode -> decode.
    rp = build_read_property(5, OBJECT_TYPES_BY_NAME["analog-value"], 1,
                             PROPERTY_IDS_BY_NAME["present-value"])
    dec = decode_npdu(rp)
    assert "readProperty" in dec.summary and "analog-value,1" in dec.summary, dec.summary

    # ReadProperty-ACK (Real present-value 72.5) round trip.
    ack = bytes([0x30, 5, 0x0C]) + ctx_object_id(0, 2, 1) + ctx_unsigned(1, 85)
    ack += open_tag(3) + bytes([0x44]) + struct.pack(">f", 72.5) + close_tag(3)
    res = parse_response(bytes([0x01, 0x00]) + ack, want_invoke=5)
    assert res.ok and abs(res.values[0] - 72.5) < 1e-3, res
    assert res.display == "72.5", res.display

    # WriteProperty encode -> decode summary; SimpleACK parse.
    wp = build_write_property(6, 1, 3, 85, encode_application_value("Real", "50"),
                              priority=8)
    assert "writeProperty" in decode_npdu(wp).summary
    sack = parse_response(bytes([0x01, 0x00, 0x20, 6, 0x0F]))
    assert sack.ok and sack.display == "OK", sack

    # Error parse.
    err = bytes([0x01, 0x00, 0x50, 6, 0x0C, 0x91, 0x01, 0x91, 0x1F])  # object/unknown-object
    eres = parse_response(err)
    assert not eres.ok and "unknown-object" in eres.error, eres.error

    # Object-id value decode (object-list element).
    ack2 = bytes([0x30, 7, 0x0C]) + ctx_object_id(0, 8, 10) + ctx_unsigned(1, 76)
    ack2 += ctx_unsigned(2, 3) + open_tag(3) + bytes([0xC4]) + encode_object_id(0, 1) + close_tag(3)
    r2 = parse_response(bytes([0x01, 0x00]) + ack2)
    assert r2.values[0] == (0, 1), r2.values
    print("bacnet self-test OK:", res.display, "|", eres.error, "|", r2.display)


if __name__ == "__main__":
    _self_test()
