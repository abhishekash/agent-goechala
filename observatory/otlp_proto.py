"""Decode OTLP/HTTP protobuf trace requests without a protobuf runtime.

Field numbers follow opentelemetry-proto's collector/trace, trace, resource,
and common v1 messages. Unknown fields are skipped as protobuf requires.
This module handles the trace signal only; it is not a general protobuf library.
"""
from __future__ import annotations

import base64
import struct

from .core import normalize_native

MAX_DEPTH = 16


def _varint(data: memoryview, pos: int):
    value = 0
    for shift in range(0, 70, 7):
        if pos >= len(data):
            raise ValueError("truncated protobuf varint")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7f) << shift
        if byte < 128:
            if value >= 1 << 64:
                raise ValueError("protobuf varint exceeds uint64")
            return value, pos
    raise ValueError("protobuf varint exceeds 10 bytes")


def _fields(raw: bytes, depth=0):
    if depth > MAX_DEPTH:
        raise ValueError("protobuf nesting is too deep")
    data = memoryview(raw)
    pos = 0
    while pos < len(data):
        tag, pos = _varint(data, pos)
        number, wire = tag >> 3, tag & 7
        if not number:
            raise ValueError("invalid protobuf field number")
        if wire == 0:
            value, pos = _varint(data, pos)
        elif wire == 1:
            if len(data) - pos < 8:
                raise ValueError("truncated protobuf fixed64")
            value = int.from_bytes(data[pos:pos + 8], "little")
            pos += 8
        elif wire == 2:
            size, pos = _varint(data, pos)
            if size > len(data) - pos:
                raise ValueError("truncated protobuf field")
            value = bytes(data[pos:pos + size])
            pos += size
        elif wire == 5:
            if len(data) - pos < 4:
                raise ValueError("truncated protobuf fixed32")
            value = int.from_bytes(data[pos:pos + 4], "little")
            pos += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        yield number, wire, value


def _text(value):
    return value.decode("utf-8")


def _nested(value, depth):
    return _fields(value, depth + 1)


def _signed(value):
    return value - (1 << 64) if value >= (1 << 63) else value


def _any_value(raw, depth):
    result = None
    for number, wire, value in _nested(raw, depth):
        if number == 1 and wire == 2:
            result = _text(value)
        elif number == 2 and wire == 0:
            result = bool(value)
        elif number == 3 and wire == 0:
            result = _signed(value)
        elif number == 4 and wire == 1:
            result = struct.unpack("<d", value.to_bytes(8, "little"))[0]
        elif number == 5 and wire == 2:
            result = [_any_value(v, depth + 2) for n, w, v in _nested(value, depth + 1) if n == 1 and w == 2]
        elif number == 6 and wire == 2:
            result = _attributes(value, depth + 1)
        elif number == 7 and wire == 2:
            result = base64.b64encode(value).decode("ascii")
    return result


def _kv(raw, depth):
    key, value = "", None
    for number, wire, item in _nested(raw, depth):
        if number == 1 and wire == 2:
            key = _text(item)
        elif number == 2 and wire == 2:
            value = _any_value(item, depth + 1)
    return key, value


def _attributes(raw, depth, field=1):
    result = {}
    for number, wire, value in _nested(raw, depth):
        if number == field and wire == 2:
            key, item = _kv(value, depth + 1)
            if key:
                result[key] = item
    return result


def _event(raw, depth):
    event = {"name": "event", "time_unix_nano": 0, "attributes": {}}
    for number, wire, value in _nested(raw, depth):
        if number == 1 and wire == 1:
            event["time_unix_nano"] = value
        elif number == 2 and wire == 2:
            event["name"] = _text(value)
        elif number == 3 and wire == 2:
            key, item = _kv(value, depth + 1)
            if key:
                event["attributes"][key] = item
    return event


def _status(raw, depth):
    code = 0
    for number, wire, value in _nested(raw, depth):
        if number == 3 and wire == 0:
            code = value
    return {0: "UNSET", 1: "OK", 2: "ERROR"}.get(code, "UNSET")


def _link(raw, depth):
    result = {"trace_id": None, "span_id": None, "attributes": {}}
    for number, wire, value in _nested(raw, depth):
        if number == 1 and wire == 2:
            result["trace_id"] = value.hex()
        elif number == 2 and wire == 2:
            result["span_id"] = value.hex()
        elif number == 4 and wire == 2:
            key, item = _kv(value, depth + 1)
            if key:
                result["attributes"][key] = item
    return result


def _span(raw, service, resource, scope_name, depth):
    native = {"trace_id": None, "span_id": None, "parent_span_id": None,
              "name": "unnamed", "start_unix_nano": 0, "end_unix_nano": 0,
              "status": "UNSET", "attributes": {}, "events": [], "links": [],
              "resource_attributes": resource, "scope_name": scope_name}
    for number, wire, value in _nested(raw, depth):
        if number == 1 and wire == 2:
            native["trace_id"] = value.hex()
        elif number == 2 and wire == 2:
            native["span_id"] = value.hex()
        elif number == 4 and wire == 2:
            native["parent_span_id"] = value.hex() if value else None
        elif number == 5 and wire == 2:
            native["name"] = _text(value)
        elif number == 7 and wire == 1:
            native["start_unix_nano"] = value
        elif number == 8 and wire == 1:
            native["end_unix_nano"] = value
        elif number == 9 and wire == 2:
            key, item = _kv(value, depth + 1)
            if key:
                native["attributes"][key] = item
        elif number == 11 and wire == 2:
            native["events"].append(_event(value, depth + 1))
        elif number == 13 and wire == 2:
            native["links"].append(_link(value, depth + 1))
        elif number == 15 and wire == 2:
            native["status"] = _status(value, depth + 1)
    return normalize_native(native, service)


def decode_trace_request(raw: bytes):
    """Return normalized spans from an ExportTraceServiceRequest protobuf body."""
    spans = []
    for number, wire, resource_spans in _fields(raw):
        if number != 1 or wire != 2:
            continue
        resource = {}
        scopes = []
        for field, kind, value in _nested(resource_spans, 1):
            if field == 1 and kind == 2:
                resource = _attributes(value, 2)
            elif field == 2 and kind == 2:
                scopes.append(value)
        for scope in scopes:
            scope_name = ""
            raw_spans = []
            for field, kind, value in _nested(scope, 2):
                if field == 1 and kind == 2:
                    for scope_field, scope_kind, scope_value in _nested(value, 3):
                        if scope_field == 1 and scope_kind == 2:
                            scope_name = _text(scope_value)
                elif field == 2 and kind == 2:
                    raw_spans.append(value)
            for span in raw_spans:
                spans.append(_span(span, str(resource.get("service.name") or "unknown"),
                                   resource, scope_name, 3))
    return spans
