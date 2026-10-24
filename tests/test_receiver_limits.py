"""Receiver worker admission, slow-client deadlines, and shutdown regressions."""
import http.client
import socket
import threading
import time
import unittest
from http.server import ThreadingHTTPServer as StandardServer
from unittest.mock import Mock

from observatory.server import BoundedHTTPServer, ThreadingHTTPServer, handler_for


class ReceiverLimitsTests(unittest.TestCase):
    def test_bounded_server_is_exported_under_both_names(self):
        self.assertIs(BoundedHTTPServer, ThreadingHTTPServer)
        with self.assertRaises(ValueError):
            BoundedHTTPServer(("127.0.0.1", 0), handler_for(Mock()), max_workers=0)

    def start_server(self, server_class=ThreadingHTTPServer, **kwargs):
        self.store = Mock()
        handler = handler_for(self.store, quiet=True)
        handler.socket_timeout = 0.25
        handler.header_timeout = 0.45
        handler.body_timeout = 0.45
        handler.write_timeout = 0.15
        server = server_class(("127.0.0.1", 0), handler, **kwargs)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01})
        thread.start()

        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join(2)
            self.assertFalse(thread.is_alive())

        self.addCleanup(cleanup)
        return server

    def connect(self, server):
        client = socket.create_connection(server.server_address, timeout=2)
        self.addCleanup(client.close)
        return client

    def response(self, client):
        response = http.client.HTTPResponse(client)
        response.begin()
        body = response.read()
        return response.status, body

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate():
            if time.monotonic() > deadline:
                self.fail("condition did not become true")
            time.sleep(0.005)

    def test_admission_rejects_before_thread_creation_and_recovers(self):
        server = self.start_server(max_workers=1)
        first = self.connect(server)
        first.sendall(b"GET / HTTP/1.0\r\n")
        self.wait_for(lambda: len(server._connections) == 1)
        workers = list(server._threads)
        for _ in range(8):
            client = self.connect(server)
            self.assertEqual(self.response(client)[0], 503)
        self.assertEqual(list(server._threads), workers)
        self.assertEqual(len(server._connections), 1)
        self.assertEqual(self.response(first)[0], 408)
        self.wait_for(lambda: not server._connections)
        client = self.connect(server)
        client.sendall(b"GET / HTTP/1.0\r\n\r\n")
        self.assertEqual(self.response(client)[0], 200)

    def test_idle_socket_and_stalled_headers_expire(self):
        server = self.start_server(max_workers=2)
        for request in (b"", b"GET / HTTP/1.0\r\nHost: partial"):
            with self.subTest(request=request):
                client = self.connect(server)
                client.sendall(request)
                self.assertEqual(self.response(client)[0], 408)
        self.wait_for(lambda: not server._connections)

    def trickle(self, client, data, interval=0.08):
        stop = threading.Event()

        def send():
            for byte in data:
                if stop.wait(interval):
                    return
                try:
                    client.sendall(bytes([byte]))
                except OSError:
                    return

        thread = threading.Thread(target=send)
        thread.start()

        def cleanup():
            stop.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())

        self.addCleanup(cleanup)

    def test_trickled_headers_have_absolute_deadline(self):
        server = self.start_server()
        client = self.connect(server)
        client.sendall(b"GET / HTTP/1.0\r\nX-Slow: ")
        self.trickle(client, b"a" * 100)
        start = time.monotonic()
        self.assertEqual(self.response(client)[0], 408)
        self.assertLess(time.monotonic() - start, 1.5)

    def test_stalled_and_trickled_bodies_are_not_ingested(self):
        server = self.start_server()
        for trickled in (False, True):
            with self.subTest(trickled=trickled):
                client = self.connect(server)
                client.sendall(b"POST /api/spans HTTP/1.0\r\n"
                               b"Content-Length: 100\r\n\r\n[")
                if trickled:
                    self.trickle(client, b" " * 100)
                start = time.monotonic()
                self.assertEqual(self.response(client)[0], 408)
                self.assertLess(time.monotonic() - start, 1.5)
        self.store.ingest.assert_not_called()

    def test_handler_remains_compatible_with_standard_server(self):
        server = self.start_server(StandardServer)
        client = self.connect(server)
        client.sendall(b"POST /api/spans HTTP/1.0\r\nContent-Length: 10\r\n\r\n")
        self.assertEqual(self.response(client)[0], 408)
        client = self.connect(server)
        client.sendall(b"GET / HTTP/1.0\r\n\r\n")
        self.assertEqual(self.response(client)[0], 200)

    def test_nonreading_response_client_releases_worker(self):
        server = self.start_server(max_workers=1)
        self.store.connections.return_value = ["x" * (16 * 1024 * 1024)]
        client = self.connect(server)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        client.sendall(b"GET /api/connections HTTP/1.0\r\n\r\n")
        self.wait_for(lambda: len(server._connections) == 1)
        self.wait_for(lambda: not server._connections)
        self.assertFalse(any(thread.is_alive() for thread in server._threads))

    def test_shutdown_interrupts_reads_and_joins_workers(self):
        server = self.start_server(max_workers=2)
        server.RequestHandlerClass.socket_timeout = 60
        server.RequestHandlerClass.header_timeout = 60
        clients = [self.connect(server), self.connect(server)]
        clients[0].sendall(b"GET / HTTP/1.0\r\n")
        clients[1].sendall(b"POST /api/spans HTTP/1.0\r\nContent-Length: 100\r\n\r\n[")
        self.wait_for(lambda: len(server._connections) == 2)
        workers = list(server._threads)
        start = time.monotonic()
        server.shutdown()
        server.server_close()
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertFalse(any(thread.is_alive() for thread in workers))
        self.assertFalse(server._connections)


if __name__ == "__main__":
    unittest.main()
