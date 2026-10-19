"""Run the published Node client against a real local Observatory receiver."""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from observatory.core import Store
from observatory.server import handler_for


NODE = shutil.which('node')
try:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('127.0.0.1', 0))
    LOOPBACK_AVAILABLE = True
except OSError:
    LOOPBACK_AVAILABLE = False


@unittest.skipUnless(NODE and LOOPBACK_AVAILABLE,
                     'Node.js and loopback socket access are required for JavaScript client interoperability')
class JavaScriptClientEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'javascript.db')
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(self.store, 'test-token', quiet=True))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.store.close()
        self.temp.cleanup()

    def test_javascript_client_exports_nested_agent_tool_usage_progress_and_errors(self):
        client_module = (Path(__file__).resolve().parents[1] / 'clients/javascript/index.mjs').as_uri()
        script = f'''import {{ Client }} from {json.dumps(client_module)};
const client = new Client({{
  endpoint: {json.dumps(self.endpoint)},
  service: 'node-interop-agent',
  token: 'test-token',
  resourceAttributes: {{ 'deployment.environment.name': 'test' }},
}});
const value = await client.trace({{
  task: 'Review the current release',
  sessionId: 'node-session-42',
  attributes: {{ 'gen_ai.agent.name': 'release-reviewer' }},
}}, async run => {{
  await run.progress('planning', 'Checking release metadata', 15);
  await run.span('chat completion', {{ attributes: {{
    'gen_ai.operation.name': 'chat',
    'gen_ai.request.model': 'test-model',
    'gen_ai.usage.input_tokens': 31,
    'gen_ai.usage.output_tokens': 9,
  }} }}, async model => {{
    model.addEvent('gen_ai.client.inference.operation.details', {{ response_id: 'response-1' }});
    await run.span('github / search_issues', {{ attributes: {{
      'rpc.system': 'mcp', 'rpc.method': 'tools/call',
      'mcp.server.name': 'github', 'mcp.tool.name': 'search_issues',
      'gen_ai.tool.call.id': 'call-1', 'tool.input': '{{"query":"release"}}',
    }} }}, async tool => tool.setStatus('OK'));
  }});
  return 'complete';
}});
try {{
  await client.trace({{ task: 'Expected failure', sessionId: 'node-session-42' }}, async () => {{
    throw new Error('deliberate test failure');
  }});
}} catch (error) {{
  if (error.message !== 'deliberate test failure') throw error;
}}
console.log(JSON.stringify({{ value }}));
'''
        script_path = Path(self.temp.name) / 'interop.mjs'
        script_path.write_text(script, encoding='utf-8')
        completed = subprocess.run([NODE, str(script_path)], capture_output=True, text=True,
                                   timeout=20, check=True)
        result = json.loads(completed.stdout)
        self.assertEqual(result['value'], 'complete')

        traces = self.store.traces(service='node-interop-agent')
        self.assertEqual(len(traces), 2)
        success = next(item for item in traces if item['task'] == 'Review the current release')
        detail = self.store.trace(success['trace_id'])
        self.assertEqual(detail['summary']['session_id'], 'node-session-42')
        self.assertEqual(detail['summary']['input_tokens'], 31)
        self.assertEqual(detail['summary']['output_tokens'], 9)
        self.assertEqual(detail['summary']['status'], 'OK')
        root = next(span for span in detail['spans'] if span['name'] == 'agent.run')
        model = next(span for span in detail['spans'] if span['name'] == 'chat completion')
        tool = next(span for span in detail['spans'] if span['name'] == 'github / search_issues')
        self.assertEqual(root['attributes']['gen_ai.agent.name'], 'release-reviewer')
        self.assertEqual(model['parent_span_id'], root['span_id'])
        self.assertEqual(tool['parent_span_id'], model['span_id'])
        self.assertEqual(tool['resource_attributes']['deployment.environment.name'], 'test')

        progress = detail['progress']
        self.assertEqual(progress[0]['stage'], 'planning')
        self.assertEqual(progress[0]['percent'], 15)
        self.assertEqual(progress[0]['session_id'], 'node-session-42')
        mcp = self.store.mcp_page(service='node-interop-agent')
        self.assertEqual(mcp['calls'][0]['server'], 'github')
        self.assertEqual(mcp['calls'][0]['tool'], 'search_issues')
        self.assertEqual(mcp['calls'][0]['trace_id'], success['trace_id'])

        failure = next(item for item in traces if item['task'] == 'Expected failure')
        failed_detail = self.store.trace(failure['trace_id'])
        self.assertEqual(failed_detail['summary']['status'], 'ERROR')
        self.assertEqual(failed_detail['spans'][0]['attributes']['exception.type'], 'Error')
        # The exception message is deliberately omitted by the JavaScript client.
        self.assertNotIn('deliberate test failure', json.dumps(failed_detail))


if __name__ == '__main__':
    unittest.main()
