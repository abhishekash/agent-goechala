"""Bounded HTTP workers and absolute read deadlines for the local receiver."""
from __future__ import annotations

import io
import socket
import threading
import time
from http.server import ThreadingHTTPServer as _ThreadingHTTPServer


class BoundedHTTPServer(_ThreadingHTTPServer):
    """Reject excess connections before creating a worker (no pending queue)."""

    daemon_threads = False
    request_queue_size = 32

    def __init__(self, server_address, RequestHandlerClass, bind_and_activate=True,
                 *, max_workers=32):
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self._slots = threading.BoundedSemaphore(max_workers)
        self._connections = set()
        self._connections_lock = threading.Lock()
        self._closing = False
        super().__init__(server_address, RequestHandlerClass, bind_and_activate)

    def process_request(self, request, client_address):
        with self._connections_lock:
            admitted = not self._closing and self._slots.acquire(blocking=False)
            if admitted:
                self._connections.add(request)
        if not admitted:
            try:
                # This runs on the accept loop: never wait for a client to read.
                request.settimeout(0.1)
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\n"
                                b"Connection: close\r\nRetry-After: 1\r\n"
                                b"Content-Length: 0\r\n\r\n")
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release(request)
            raise

    def _release(self, request):
        with self._connections_lock:
            self._connections.discard(request)
        self._slots.release()

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release(request)

    def _interrupt_connections(self):
        with self._connections_lock:
            self._closing = True
            for connection in self._connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def shutdown(self):
        self._interrupt_connections()
        super().shutdown()

    def server_close(self):
        self._interrupt_connections()
        # Non-daemon workers are joined before the caller closes the Store.
        super().server_close()


class _DeadlineReader(io.RawIOBase):
    def __init__(self, handler):
        super().__init__()
        self.handler = handler

    def readable(self):
        return True

    def readinto(self, buffer):
        handler = self.handler
        remaining = handler._read_deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("request read deadline exceeded")
        handler.connection.settimeout(min(handler.socket_timeout, remaining))
        return handler.connection.recv_into(buffer)


class DeadlineHandler:
    """Also works with a standard-library ThreadingHTTPServer."""

    socket_timeout = 5.0
    header_timeout = 10.0
    body_timeout = 15.0
    write_timeout = 5.0

    def setup(self):
        # Preserve in-memory request adapters used by handler-level callers.
        self._socket_io = (hasattr(self.request, "settimeout") and
                           hasattr(self.request, "recv_into"))
        if self._socket_io:
            self.request.settimeout(self.socket_timeout)
        super().setup()
        if self._socket_io:
            self.rfile.close()
            self.rfile = io.BufferedReader(_DeadlineReader(self))

    def handle(self):
        try:
            super().handle()
        except OSError:
            # Disconnected/non-reading clients must not cause a second response.
            self.close_connection = True

    def handle_one_request(self):
        self._read_deadline = time.monotonic() + self.header_timeout
        self.requestline = ""
        self.request_version = "HTTP/1.0"
        self.command = None
        self._response_started = False
        try:
            # Mirror BaseHTTPRequestHandler, but return an explicit 408 on reads
            # rather than its silent timeout close. No keep-alive is advertised.
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                self.send_error(414)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                return
            method = getattr(self, "do_" + self.command, None)
            if method is None:
                self.send_error(501, "Unsupported method (%r)" % self.command)
                return
            method()
            self.wfile.flush()
        except TimeoutError:
            self.close_connection = True
            if not self._response_started:
                self.send_error(408, "Request read deadline exceeded")

    def send_response(self, *args, **kwargs):
        self._response_started = True
        super().send_response(*args, **kwargs)

    def read_body(self, size):
        self._read_deadline = time.monotonic() + self.body_timeout
        return self.rfile.read(size)

    def end_headers(self):
        if self._socket_io:
            self.connection.settimeout(self.write_timeout)
        super().end_headers()
