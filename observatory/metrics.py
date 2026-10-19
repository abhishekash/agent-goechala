"""Normalize OTLP metric data points from JSON and HTTP/protobuf."""
from __future__ import annotations

import base64
import math
import re
import struct

from .core import _attrs
from .otlp_proto import _fields, _nested, _attributes, _text, _kv, _signed

KINDS = {"gauge": 5, "sum": 7, "histogram": 9,
         "exponentialHistogram": 10, "summary": 11}


def _json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value


def _normalize(service, resource, scope, metric, kind, temporality, point, metric_metadata=None):
    timestamp = int(point.get("timeUnixNano") or 0)
    if timestamp <= 0:
        raise ValueError("metric point requires timeUnixNano")
    start = int(point.get("startTimeUnixNano") or 0)
    value = point.get("asDouble", point.get("asInt"))
    if value is not None:
        value = float(value) if "asDouble" in point else int(value)
    data = {key: item for key, item in point.items() if key not in
            ("timeUnixNano", "startTimeUnixNano", "attributes", "asDouble", "asInt")}
    if "flags" in data:
        try:
            data["flags"] = max(0, int(data["flags"]))
        except (TypeError, ValueError, OverflowError):
            data.pop("flags", None)
    if metric_metadata:
        data.update(metric_metadata)
    if "exemplars" in point:
        data["exemplars"] = _normalize_exemplars(point.get("exemplars"))
    if value is not None:
        data["value"] = value
    return {"service": service, "resource_attributes": _json_safe(resource), "scope_name": scope,
            "name": str(metric.get("name") or "unnamed"),
            "description": str(metric.get("description") or ""),
            "unit": str(metric.get("unit") or ""), "kind": kind,
            "temporality": temporality, "time_unix_nano": timestamp,
            "start_unix_nano": start, "attributes": _json_safe(_attrs(point.get("attributes"))),
            "data": _json_safe(data)}



def _normalize_exemplars(items):
    result = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        trace_id = item.get("trace_id", item.get("traceId"))
        span_id = item.get("span_id", item.get("spanId"))
        if isinstance(trace_id, bytes):
            trace_id = trace_id.hex()
        if isinstance(span_id, bytes):
            span_id = span_id.hex()
        trace_id = str(trace_id).lower() if trace_id is not None else ""
        span_id = str(span_id).lower() if span_id is not None else ""
        if trace_id and not re.fullmatch(r"[0-9a-f]{32}", trace_id):
            trace_id = ""
        if span_id and not re.fullmatch(r"[0-9a-f]{16}", span_id):
            span_id = ""
        try:
            time_ns = max(0, int(item.get("time_unix_nano", item.get("timeUnixNano", 0))))
        except (TypeError, ValueError, OverflowError):
            time_ns = 0
        value = item.get("value", item.get("asDouble", item.get("asInt")))
        attrs = item.get("filtered_attributes", item.get("filteredAttributes", {}))
        if isinstance(attrs, list):
            attrs = _attrs(attrs)
        result.append({"trace_id": trace_id or None, "span_id": span_id or None,
                       "time_unix_nano": time_ns, "value": _json_safe(value),
                       "filtered_attributes": _json_safe(attrs if isinstance(attrs, dict) else {})})
    return result


def _exemplar(raw, depth):
    exemplar = {}
    filtered = {}
    for number, wire, value in _nested(raw, depth):
        if number == 2 and wire == 1:
            exemplar["time_unix_nano"] = value
        elif number == 3 and wire == 1:
            exemplar["value"] = _double(value)
        elif number == 4 and wire == 2:
            exemplar["span_id"] = value.hex()
        elif number == 5 and wire == 2:
            exemplar["trace_id"] = value.hex()
        elif number == 6 and wire == 1:
            exemplar["value"] = _signed(value)
        elif number == 7 and wire == 2:
            key, item = _kv(value, depth + 1)
            if key:
                filtered[key] = item
    if filtered:
        exemplar["filtered_attributes"] = filtered
    return exemplar

def normalize_otlp_metrics(body):
    result = []
    for group in body.get("resourceMetrics", []):
        resource = _attrs(group.get("resource", {}).get("attributes"))
        service = str(resource.get("service.name") or "unknown")
        for scope in group.get("scopeMetrics", []):
            scope_name = str(scope.get("scope", {}).get("name") or "")
            for metric in scope.get("metrics", []):
                matched = False
                for kind in KINDS:
                    if kind in metric:
                        matched = True
                        payload = metric[kind]
                        temporality = (0 if kind == "summary" else
                                       int(payload.get("aggregationTemporality") or 0))
                        metadata = {}
                        if kind == "sum" and isinstance(payload.get("isMonotonic"), bool):
                            metadata["isMonotonic"] = payload["isMonotonic"]
                        if kind == "summary":
                            metadata["temporality_assumed"] = "cumulative"
                        result.extend(_normalize(service, resource, scope_name, metric,
                                                  kind, temporality, point, metadata)
                                      for point in payload.get("dataPoints", []))
                        break
                if not matched:
                    raise ValueError(f"metric {metric.get('name', 'unnamed')} has no supported data type")
    return result


def _double(value):
    return struct.unpack("<d", value.to_bytes(8, "little"))[0]


def _packed_fixed64(value):
    if len(value) % 8:
        raise ValueError("invalid packed fixed64 array")
    return [int.from_bytes(value[i:i + 8], "little") for i in range(0, len(value), 8)]


def _point(raw, kind, depth):
    attrs_field = 1 if kind == "exponentialHistogram" else 9 if kind == "histogram" else 7
    flags_field = 8 if kind in ("gauge", "sum", "summary") else 10
    point = {"attributes": []}
    buckets = {"positive": None, "negative": None}
    exemplar_field = 5 if kind in ("gauge", "sum") else 8 if kind == "histogram" else 11 if kind == "exponentialHistogram" else None
    for number, wire, value in _nested(raw, depth):
        if number == exemplar_field and wire == 2:
            point.setdefault("exemplars", []).append(_exemplar(value, depth + 1))
        elif number == attrs_field and wire == 2:
            key, item = _kv(value, depth + 1)
            if key:
                point["attributes"].append({"key": key, "value": _json_any(item)})
        elif number == 2 and wire == 1:
            point["startTimeUnixNano"] = value
        elif number == 3 and wire == 1:
            point["timeUnixNano"] = value
        elif number == flags_field and wire == 0:
            point["flags"] = value
        elif kind in ("gauge", "sum"):
            if number == 4 and wire == 1:
                point["asDouble"] = _double(value)
            elif number == 6 and wire == 1:
                point["asInt"] = _signed(value)
        elif kind in ("histogram", "exponentialHistogram", "summary"):
            if number == 4 and wire == 1:
                point["count"] = value
            elif number == 5 and wire == 1:
                point["sum"] = _double(value)
            elif kind == "histogram":
                if number == 6 and wire == 1:
                    point.setdefault("bucketCounts", []).append(value)
                elif number == 6 and wire == 2:
                    point.setdefault("bucketCounts", []).extend(_packed_fixed64(value))
                elif number == 7 and wire == 1:
                    point.setdefault("explicitBounds", []).append(_double(value))
                elif number == 7 and wire == 2:
                    point.setdefault("explicitBounds", []).extend(_double(v) for v in _packed_fixed64(value))
                elif number in (11, 12) and wire == 1:
                    point["min" if number == 11 else "max"] = _double(value)
            elif kind == "exponentialHistogram":
                if number == 6 and wire == 0:
                    point["scale"] = (value >> 1) ^ -(value & 1)
                elif number == 7 and wire == 1:
                    point["zeroCount"] = value
                elif number in (8, 9) and wire == 2:
                    buckets["positive" if number == 8 else "negative"] = _exp_buckets(value, depth + 1)
                elif number in (12, 13, 14) and wire == 1:
                    point[{12: "min", 13: "max", 14: "zeroThreshold"}[number]] = _double(value)
            elif kind == "summary" and number == 6 and wire == 2:
                quantile = {}
                for field, typ, val in _nested(value, depth + 1):
                    if field in (1, 2) and typ == 1:
                        quantile["quantile" if field == 1 else "value"] = _double(val)
                point.setdefault("quantileValues", []).append(quantile)
    if kind == "exponentialHistogram":
        point.update({key: value for key, value in buckets.items() if value is not None})
    return point


def _json_any(value):
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": value}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, bytes):
        return {"bytesValue": base64.b64encode(value).decode()}
    if isinstance(value, list):
        return {"arrayValue": {"values": [_json_any(v) for v in value]}}
    if isinstance(value, dict):
        return {"kvlistValue": {"values": [{"key": k, "value": _json_any(v)} for k, v in value.items()]}}
    return {}


def _exp_buckets(raw, depth):
    result = {"offset": 0, "bucketCounts": []}
    for number, wire, value in _nested(raw, depth):
        if number == 1 and wire == 0:
            result["offset"] = (value >> 1) ^ -(value & 1)
        elif number == 2 and wire == 0:
            result["bucketCounts"].append(value)
        elif number == 2 and wire == 2:
            result["bucketCounts"].extend(_unpack_varints(value))
    return result


def _unpack_varints(raw):
    from .otlp_proto import _varint
    data = memoryview(raw)
    pos = 0
    values = []
    while pos < len(data):
        value, pos = _varint(data, pos)
        values.append(value)
    return values


def _metric(raw, service, resource, scope, depth):
    metric = {"name": "", "description": "", "unit": ""}
    payload = None
    kind = None
    for number, wire, value in _nested(raw, depth):
        if number in (1, 2, 3) and wire == 2:
            metric[{1: "name", 2: "description", 3: "unit"}[number]] = _text(value)
        elif number in KINDS.values() and wire == 2:
            kind = next(name for name, field in KINDS.items() if field == number)
            payload = value
    if payload is None:
        raise ValueError(f"metric {metric['name'] or 'unnamed'} has no supported data type")
    points = []
    temporality = 0
    metadata = {}
    for number, wire, value in _nested(payload, depth + 1):
        if number == 1 and wire == 2:
            points.append(_point(value, kind, depth + 2))
        elif number == 2 and wire == 0 and kind != "summary":
            temporality = value
        elif kind == "sum" and number == 3 and wire == 0:
            metadata["isMonotonic"] = bool(value)
    if kind == "summary":
        metadata["temporality_assumed"] = "cumulative"
    return [_normalize(service, resource, scope, metric, kind, temporality, point, metadata)
            for point in points]


def decode_metric_request(raw):
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
            metrics = []
            for field, kind, value in _nested(scope, 2):
                if field == 1 and kind == 2:
                    for key, typ, item in _nested(value, 3):
                        if key == 1 and typ == 2:
                            scope_name = _text(item)
                elif field == 2 and kind == 2:
                    metrics.append(value)
            for metric in metrics:
                result.extend(_metric(metric, service, resource, scope_name, 3))
    return result
