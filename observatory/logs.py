"""Normalize OTLP log records from JSON and HTTP/protobuf requests."""
from __future__ import annotations

import time

from .core import TRACE_ID, SPAN_ID, _attrs, _value
from .otlp_proto import _fields, _nested, _attributes, _any_value, _text


def _optional_id(value, pattern):
    if isinstance(value, str) and pattern.fullmatch(value) and any(c != "0" for c in value):
        return value.lower()
    return None


def _log(raw, service, resource, scope):
    timestamp = int(raw.get("time_unix_nano") or raw.get("observed_time_unix_nano") or time.time_ns())
    if timestamp < 0:
        raise ValueError("invalid log timestamp")
    attrs = raw.get("attributes") or {}
    if not isinstance(attrs, dict):
        raise ValueError("log attributes must be an object")
    return {"time_unix_nano": timestamp,
            "trace_id": _optional_id(raw.get("trace_id"), TRACE_ID),
            "span_id": _optional_id(raw.get("span_id"), SPAN_ID),
            "service": service, "scope_name": scope,
            "resource_attributes": resource,
            "severity_number": int(raw.get("severity_number") or 0),
            "severity_text": str(raw.get("severity_text") or ""),
            "event_name": str(raw.get("event_name") or attrs.get("event.name") or ""),
            "body": raw.get("body"), "attributes": attrs}


def normalize_otlp_logs(body):
    result = []
    for group in body.get("resourceLogs", []):
        resource = _attrs(group.get("resource", {}).get("attributes"))
        service = str(resource.get("service.name") or "unknown")
        for scope in group.get("scopeLogs", []):
            scope_name = str(scope.get("scope", {}).get("name") or "")
            for item in scope.get("logRecords", []):
                raw = {"time_unix_nano": item.get("timeUnixNano"),
                       "observed_time_unix_nano": item.get("observedTimeUnixNano"),
                       "trace_id": item.get("traceId"), "span_id": item.get("spanId"),
                       "severity_number": item.get("severityNumber"),
                       "severity_text": item.get("severityText"),
                       "event_name": item.get("eventName"),
                       "body": _value(item.get("body", {})),
                       "attributes": _attrs(item.get("attributes"))}
                result.append(_log(raw, service, resource, scope_name))
    return result


def _record_proto(raw, service, resource, scope, depth):
    item = {"attributes": {}}
    for number, wire, value in _nested(raw, depth):
        if number == 1 and wire == 1:
            item["time_unix_nano"] = value
        elif number == 11 and wire == 1:
            item["observed_time_unix_nano"] = value
        elif number == 2 and wire == 0:
            item["severity_number"] = value
        elif number == 3 and wire == 2:
            item["severity_text"] = _text(value)
        elif number == 5 and wire == 2:
            item["body"] = _any_value(value, depth + 1)
        elif number == 6 and wire == 2:
            key, attr = _kv_log(value, depth + 1)
            if key:
                item["attributes"][key] = attr
        elif number == 9 and wire == 2:
            item["trace_id"] = value.hex()
        elif number == 10 and wire == 2:
            item["span_id"] = value.hex()
        elif number == 12 and wire == 2:
            item["event_name"] = _text(value)
    return _log(item, service, resource, scope)


def _kv_log(raw, depth):
    from .otlp_proto import _kv
    return _kv(raw, depth)


def decode_log_request(raw):
    result = []
    for number, wire, group in _fields(raw):
        if number != 1 or wire != 2:
            continue
        resource = {}
        scopes = []
        for field, kind, value in _nested(group, 1):
            if field == 1 and kind == 2:
                resource = _attributes(value, 2)
            elif field == 2 and kind == 2:
                scopes.append(value)
        service = str(resource.get("service.name") or "unknown")
        for scope in scopes:
            scope_name = ""
            records = []
            for field, kind, value in _nested(scope, 2):
                if field == 1 and kind == 2:
                    for key, typ, item in _nested(value, 3):
                        if key == 1 and typ == 2:
                            scope_name = _text(item)
                elif field == 2 and kind == 2:
                    records.append(value)
            result.extend(_record_proto(item, service, resource, scope_name, 3) for item in records)
    return result
