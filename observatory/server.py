"""Small local HTTP receiver and API for the terminal cockpit."""
from __future__ import annotations

import hmac
import gzip
import io
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .core import Store, normalize_native, normalize_otlp, TRACE_ID
from .otlp_proto import decode_trace_request
from .logs import normalize_otlp_logs, decode_log_request
from .metrics import normalize_otlp_metrics, decode_metric_request
from .watch import start_watchers

MAX_BODY = 8 * 1024 * 1024


def handler_for(store: Store, token: str | None = None, quiet=False):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, code, value):
            data = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _protobuf_ok(self):
            # Empty OTLP export response means the full batch was accepted.
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _authorized(self):
            if not token:
                return True
            header = self.headers.get("Authorization", "")
            return hmac.compare_digest(header, f"Bearer {token}")

        def _route(self):
            route = urlparse(self.path)
            # The root is a static, non-sensitive hint for someone who opens
            # the receiver URL in a browser. Keep every data/API route gated.
            if route.path != "/" and not self._authorized():
                self._json(401, {"error": "unauthorized"})
                return None
            return route

        def do_GET(self):
            route = self._route()
            if route is None:
                return
            path = route.path
            if path == "/":
                data = ("Agent Observatory is terminal-first. Run `agent-observatory` in a terminal "
                        "to monitor local sessions. This HTTP service receives OTLP and serves the local API.\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            elif path == "/api/health":
                local = getattr(store, "local_agent_monitor", None)
                self._json(200, {"ok": True, "local_agents": local.status() if local else {"enabled": False}})
            elif path == "/api/connections":
                self._json(200, {"connections": store.connections()})
            elif path == "/api/activity":
                query = parse_qs(route.query)
                try:
                    page = store.activity_page(limit=int(query.get("limit", [100])[0]),
                        service=query.get("service", [None])[0], domain=query.get("domain", [None])[0],
                        query=query.get("q", [None])[0], session_id=query.get("session_id", [None])[0],
                        cursor=query.get("cursor", [None])[0],
                        include_reviews=query.get("include_reviews", ["1"])[0]!="0",
                        errors_only=query.get("errors_only", ["0"])[0]=="1",
                        action=query.get("action", [None])[0])
                    self._json(200, page)
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
            elif path == "/api/metrics":
                self._json(200, store.metrics())
            elif path == "/api/mcp":
                query = parse_qs(route.query)
                try:
                    page = store.mcp_page(limit=int(query.get('limit', [100])[0]),
                        service=query.get('service', [None])[0], session_id=query.get('session_id', [None])[0],
                        query=query.get('q', [None])[0], cursor=query.get('cursor', [None])[0],
                        include_reviews=query.get('include_reviews', ['1'])[0]!='0')
                    self._json(200, {**page, 'scope': 'stored MCP operations'})
                except ValueError as exc:
                    self._json(400, {'error': str(exc)})
            elif path == "/api/usage-series":
                query = parse_qs(route.query)
                try:
                    days = int(query.get("days", [7])[0])
                    step = query.get("step", ["day"])[0]
                    service = query.get("service", [None])[0]
                    session_id = query.get("session_id", [None])[0]
                    self._json(200, store.usage_series(days=days, step=step, service=service,
                                                       session_id=session_id))
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
            elif path == "/api/logs":
                self._json(200, {"logs": store.logs(limit=500)})
            elif path == "/api/measurements":
                query = parse_qs(route.query, keep_blank_values=True)
                try:
                    limit = max(1, min(500, int(query.get("limit", [500])[0])))
                    if query.get("latest", ["0"])[0] == "1":
                        points = store.metric_latest_points(limit=limit,
                            service=query.get("service", [None])[0])
                        self._json(200, {"points": points})
                        return
                    temporality = query.get("temporality", [None])[0]
                    points = store.metric_points(limit=limit,
                        service=query.get("service", [None])[0], name=query.get("name", [None])[0],
                        kind=query.get("kind", [None])[0],
                        temporality=int(temporality) if temporality is not None else None,
                        scope_name=query.get("scope_name", [None])[0],
                        unit=query.get("unit", [None])[0],
                        description=query.get("description", [None])[0])
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                    return
                self._json(200, {"points": points})
            elif path == "/api/metric-catalog":
                query = parse_qs(route.query)
                try:
                    limit = max(1, min(500, int(query.get("limit", [200])[0])))
                    self._json(200, {"metrics": store.metric_catalog(limit=limit)})
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
            elif path == "/api/findings":
                query = parse_qs(route.query)
                try:
                    limit = max(1, min(500, int(query.get("limit", [100])[0])))
                    page = store.finding_page(limit=limit, cursor=query.get("cursor", [None])[0])
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                    return
                self._json(200, page)
            elif path == "/api/sessions":
                query = parse_qs(route.query)
                try:
                    limit = max(1, min(500, int(query.get("limit", [100])[0])))
                    self._json(200, {"sessions": store.sessions(limit=limit)})
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
            elif path == "/api/endpoints":
                query = parse_qs(route.query)
                try:
                    limit = max(1, min(500, int(query.get("limit", [100])[0])))
                    self._json(200, {"endpoints": store.endpoints(limit=limit)})
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
            elif path == "/api/outcomes":
                self._json(200, {"outcomes": list(store.outcomes().values())})
            elif path == "/api/traces":
                query = parse_qs(route.query)
                try:
                    limit = max(1, min(500, int(query.get("limit", [100])[0])))
                except ValueError:
                    self._json(400, {"error": "invalid limit"})
                    return
                try:
                    page = store.trace_page(limit, query.get("service", [None])[0],
                                            query.get("status", [None])[0],
                                            query.get("cursor", [None])[0],
                                            query.get("session_id", [None])[0],
                                            query.get("q", [None])[0],
                                            query.get("endpoint_id", [None])[0],
                                            query.get("include_reviews", ["1"])[0]!="0")
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                    return
                self._json(200, page)
            elif path.startswith("/api/traces/") and path.endswith("/related"):
                trace_id = path.removeprefix("/api/traces/").removesuffix("/related")
                if not TRACE_ID.fullmatch(trace_id):
                    self._json(400, {"error": "invalid trace_id"})
                    return
                self._json(200, {"related": store.related_traces(trace_id)})
            elif path.startswith("/api/traces/") and path.endswith("/spans"):
                trace_id = path.removeprefix("/api/traces/").removesuffix("/spans")
                if not TRACE_ID.fullmatch(trace_id):
                    self._json(400, {"error": "invalid trace_id"})
                    return
                query = parse_qs(route.query, keep_blank_values=True)
                try:
                    limit = max(1, min(500, int(query.get("limit", [200])[0])))
                    page = store.span_page(trace_id, limit=limit,
                        cursor=query.get("cursor", [None])[0],
                        query=query.get("q", [""])[0].strip()[:200],
                        span_filter=query.get("filter", ["all"])[0])
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                    return
                self._json(200, page)
            elif path.startswith("/api/traces/"):
                trace_id = path.removeprefix("/api/traces/")
                if not TRACE_ID.fullmatch(trace_id):
                    self._json(400, {"error": "invalid trace_id"})
                    return
                query = parse_qs(route.query, keep_blank_values=True)
                if "span_limit" in query:
                    try:
                        limit = max(1, min(500, int(query.get("span_limit", [200])[0])))
                        result = store.trace_overview(trace_id)
                        span_page = store.span_page(trace_id, limit=limit,
                            query=query.get("q", [""])[0].strip()[:200],
                            span_filter=query.get("filter", ["all"])[0])
                    except ValueError as exc:
                        self._json(400, {"error": str(exc)})
                        return
                    result["spans"] = span_page["spans"]
                    result["span_page"] = {key: span_page[key] for key in
                                            ("total", "trace_total", "next_cursor", "start_unix_nano", "end_unix_nano")}
                else:
                    result = store.trace(trace_id)
                self._json(200 if result["summary"] or result["progress"] or result["logs"] or result["outcome"] else 404, result)
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            route = self._route()
            if route is None:
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size <= 0 or size > MAX_BODY:
                    self._json(413, {"error": "body size must be 1 to 8 MiB"})
                    return
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise ValueError("truncated request body")
                encoding = self.headers.get("Content-Encoding", "identity").lower()
                if encoding == "gzip":
                    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                        raw = stream.read(MAX_BODY + 1)
                    if len(raw) > MAX_BODY:
                        self._json(413, {"error": "decompressed body exceeds 8 MiB"})
                        return
                elif encoding != "identity":
                    self._json(415, {"error": "unsupported content encoding"})
                    return
                content_type = self.headers.get("Content-Type", "").split(";")[0].strip()
                if route.path in ("/v1/traces", "/v1/logs", "/v1/metrics"):
                    if content_type == "application/x-protobuf":
                        if route.path == "/v1/traces":
                            store.ingest(decode_trace_request(raw))
                        elif route.path == "/v1/logs":
                            store.ingest_logs(decode_log_request(raw))
                        else:
                            store.ingest_metrics(decode_metric_request(raw))
                        self._protobuf_ok()
                        return
                    if content_type != "application/json":
                        self._json(415, {"error": "OTLP requires application/json or application/x-protobuf"})
                        return
                body = json.loads(raw)
                if route.path == "/api/spans":
                    raw_spans = body if isinstance(body, list) else body.get("spans", [])
                    if not isinstance(raw_spans, list):
                        raise ValueError("spans must be an array")
                    service = body.get("service", "agent-harness") if isinstance(body, dict) else "agent-harness"
                    count = store.ingest([normalize_native(s, service) for s in raw_spans])
                    self._json(200, {"accepted": count})
                elif route.path == "/v1/traces":
                    count = store.ingest(normalize_otlp(body))
                    self._json(200, {"partialSuccess": {"rejectedSpans": 0}, "accepted": count})
                elif route.path == "/v1/logs":
                    count = store.ingest_logs(normalize_otlp_logs(body))
                    self._json(200, {"partialSuccess": {"rejectedLogRecords": 0}, "accepted": count})
                elif route.path == "/v1/metrics":
                    count = store.ingest_metrics(normalize_otlp_metrics(body))
                    self._json(200, {"partialSuccess": {"rejectedDataPoints": 0}, "accepted": count})
                elif route.path == "/api/progress":
                    store.add_progress(body["trace_id"], body["stage"], body.get("message", ""),
                                       body.get("percent"), body.get("session_id"), body.get("service"))
                    self._json(200, {"accepted": 1})
                elif route.path == "/api/connections":
                    store.register_connection(body)
                    self._json(200, {"accepted": 1})
                elif route.path == "/api/outcomes":
                    records = body if isinstance(body, list) else body.get("results", [body])
                    count = store.ingest_outcomes(records, source="api")
                    self._json(200, {"accepted": count})
                else:
                    self._json(404, {"error": "not found"})
            except (ValueError, TypeError, KeyError, AttributeError, OSError, EOFError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})

        def log_message(self, format, *args):
            if not quiet and os.environ.get("OBSERVATORY_QUIET") != "1":
                super().log_message(format, *args)

    return Handler


def serve(db_path="observatory.db", host="127.0.0.1", port=4318, token=None,
          watch_jsonl=(), watch_service="agent-harness", retention_days=None, local_agents=True, metadata_only=False):
    store = Store(db_path, async_derived_refresh=True)
    if retention_days is not None:
        store.prune(retention_days)
    server = ThreadingHTTPServer((host, port), handler_for(store, token))
    local = None
    if local_agents and host in {"127.0.0.1", "localhost", "::1"}:
        from .local_agents import LocalAgents
        local = LocalAgents(store, capture_details=not metadata_only).start()
    store.local_agent_monitor = local
    watch = start_watchers(store, watch_jsonl, watch_service) if watch_jsonl else None
    maintenance = _start_retention(store, retention_days) if retention_days is not None else None
    print(f"Observatory receiver listening at http://{host}:{server.server_port} (database: {db_path})", flush=True)
    if local:
        print("Watching local Codex / Claude Code sessions · " + ("metadata only" if metadata_only else "activity and bounded, redacted content"), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if local:
            local.close()
        if watch:
            watch[0].set()
            watch[1].join(timeout=2)
        if maintenance:
            maintenance[0].set()
            maintenance[1].join(timeout=2)
        store.close()


def _start_retention(store, retention_days, interval_seconds=3600):
    stop = threading.Event()

    def maintain():
        while not stop.is_set():
            stop.wait(interval_seconds)
            if stop.is_set():
                break
            store.prune(retention_days)

    thread = threading.Thread(target=maintain, name="observatory-retention", daemon=True)
    thread.start()
    return stop, thread
