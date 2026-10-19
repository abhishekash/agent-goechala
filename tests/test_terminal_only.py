"""Terminal-first behavior stays clear at the receiver and in the cockpit."""
from email.message import Message
import io
import tempfile
import unittest
from pathlib import Path

from observatory.core import Store
from observatory.server import handler_for


class TerminalOnlyReceiverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'receiver.db')
        self.addCleanup(self.store.close)
        self.handler = handler_for(self.store, quiet=True)

    def get(self, path):
        handler = self.handler.__new__(self.handler)
        handler.command = 'GET'
        handler.path = path
        handler.headers = Message()
        handler.wfile = io.BytesIO()
        response = {'headers': {}}
        handler.send_response = lambda code: response.update(status=code)
        handler.send_header = lambda key, value: response['headers'].update({key: value})
        handler.end_headers = lambda: None
        handler.do_GET()
        return response, handler.wfile.getvalue()

    def test_root_is_a_plain_text_receiver_message(self):
        response, body = self.get('/')
        self.assertEqual(response['status'], 200)
        self.assertTrue(response['headers']['Content-Type'].startswith('text/plain'))
        self.assertIn(b'Agent Goechala is terminal-first', body)
        self.assertNotIn(b'<html', body.lower())

    def test_browser_and_agent_setup_pages_are_not_served(self):
        for path in ('/index.html', '/setup.js', '/api/setup', '/api/package'):
            with self.subTest(path=path):
                response, _ = self.get(path)
                self.assertEqual(response['status'], 404)


if __name__ == '__main__':
    unittest.main()
