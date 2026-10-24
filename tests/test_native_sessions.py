"""Native session records must preserve MCP identity and call lifecycle."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from observatory.core import Store
from observatory.local_agents import LocalAgents, Session, discover_mcp_configurations
from observatory.local_runtime import project_directories


class NativeSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "events.db")

    def session(self, runtime, details=True):
        path = self.root / (runtime + ".jsonl")
        path.touch()
        return Session(self.store, path, runtime, capture_details=details)

    def test_mcp_configuration_inventory_reads_names_only_across_client_formats(self):
        codex = self.root / 'config.toml'
        claude = self.root / '.mcp.json'
        cursor_dir = self.root / '.cursor'
        vscode_dir = self.root / '.vscode'
        gemini_dir = self.root / '.gemini'
        opencode_dir = self.root / 'opencode-global'
        cursor_dir.mkdir(); vscode_dir.mkdir(); gemini_dir.mkdir(); opencode_dir.mkdir()
        codex.write_text('[mcp_servers.github]\ncommand = "secret-command"\nargs = ["private-argument"]\n')
        claude.write_text(json.dumps({'mcpServers': {'linear': {'command': 'private'}}}))
        (cursor_dir / 'mcp.json').write_text(json.dumps({'mcpServers': {'docs': {'url': 'https://secret'}}}))
        (vscode_dir / 'mcp.json').write_text(json.dumps({'servers': {'db': {'env': {'TOKEN': 'secret'}}}}))
        (gemini_dir / 'settings.json').write_text(json.dumps({'mcpServers': {'calendar': {'command': 'private'}}}))
        opencode_jsonc = opencode_dir / 'opencode.jsonc'
        opencode_jsonc.write_text('''{
          // OpenCode v1 format; comment markers in strings are not comments.
          "mcp": {
            "docs": {"type": "remote", "url": "https://example.test/mcp",},
          },
        }''')
        (self.root / 'opencode.json').write_text(json.dumps({'mcp': {'servers': {
            'v2-server': {'type': 'remote', 'url': 'https://private.example'}}}}))
        result = discover_mcp_configurations(
            global_paths=[(codex, 'Codex', 'User'), (opencode_jsonc, 'OpenCode', 'User')],
            project_directories=[self.root])
        self.assertEqual({(item['client'], item['server']) for item in result}, {
            ('Codex', 'github'), ('Claude Code', 'linear'), ('Cursor', 'docs'),
            ('Gemini CLI', 'calendar'), ('VS Code', 'db'), ('OpenCode', 'docs'),
            ('OpenCode', 'v2-server')})
        self.assertNotIn('secret', json.dumps(result))
        self.assertNotIn('example.test', json.dumps(result))

    def test_mcp_configuration_inventory_ignores_malformed_and_oversized_files(self):
        malformed = self.root / 'broken.json'
        malformed.write_text('{broken')
        oversized = self.root / 'large.toml'
        oversized.write_text('x' * (1024 * 1024 + 1))
        errors = []
        self.assertEqual(discover_mcp_configurations(
            global_paths=[(malformed, 'Claude Code', 'User'), (oversized, 'Codex', 'User')],
            on_error=lambda: errors.append('invalid')), [])
        self.assertEqual(len(errors), 2)

    def test_opencode_global_config_environment_overrides(self):
        config_dir = self.root / 'custom-opencode'
        config_dir.mkdir()
        (config_dir / 'opencode.jsonc').write_text('{"mcp":{"global-docs":{"type":"remote"}}}')
        with patch.dict(os.environ, {'OPENCODE_CONFIG_DIR': str(config_dir),
                                     'OPENCODE_CONFIG': '', 'XDG_CONFIG_HOME': str(self.root / 'xdg')}):
            result = discover_mcp_configurations(global_paths=None, project_directories=[])
        self.assertIn(('OpenCode', 'User', 'global-docs'),
                      {(item['client'], item['scope'], item['server']) for item in result})

    def test_copilot_cli_global_mcp_config_uses_copilot_home_and_retains_names_only(self):
        copilot_home = self.root / 'copilot-home'
        copilot_home.mkdir()
        (copilot_home / 'mcp-config.json').write_text(json.dumps({'mcpServers': {
            'issues': {'command': 'private-command', 'env': {'TOKEN': 'secret'}},
            'docs': {'url': 'https://private.example/mcp'}}}))
        with patch.dict(os.environ, {'COPILOT_HOME': str(copilot_home)}):
            result = discover_mcp_configurations(global_paths=None, project_directories=[])
        self.assertIn(('GitHub Copilot CLI', 'User', 'issues'),
                      {(item['client'], item['scope'], item['server']) for item in result})
        self.assertIn(('GitHub Copilot CLI', 'User', 'docs'),
                      {(item['client'], item['scope'], item['server']) for item in result})
        serialized = json.dumps(result)
        self.assertNotIn('private-command', serialized)
        self.assertNotIn('secret', serialized)
        self.assertNotIn('private.example', serialized)

    def test_project_directories_come_only_from_selected_same_user_processes(self):
        proc_root = self.root / 'proc'
        project = self.root / 'gemini-project'
        project.mkdir()
        for pid, name in (("101", "gemini"), ("102", "opencode"), ("202", "python")):
            process = proc_root / pid
            process.mkdir(parents=True)
            (process / 'comm').write_text(name)
            (process / 'cwd').symlink_to(project)
        self.assertEqual(project_directories({'gemini', 'opencode'}, proc_root), {str(project)})
        gemini = project / '.gemini'
        gemini.mkdir()
        (gemini / 'settings.json').write_text(json.dumps({'mcpServers': {'workspace-tools': {}}}))
        inventory = discover_mcp_configurations(global_paths=[],
            project_directories=project_directories({'gemini', 'opencode'}, proc_root))
        self.assertEqual([(item['client'], item['scope'], item['server']) for item in inventory],
                         [('Gemini CLI', 'Project', 'workspace-tools')])

    def test_local_discovery_reports_when_the_recent_session_cap_hides_sources(self):
        codex_home = self.root / 'codex-home'
        session_root = codex_home / 'sessions'
        session_root.mkdir(parents=True)
        for index in range(52):
            (session_root / f'{index:03}.jsonl').touch()
        agents = LocalAgents(self.store)
        with patch.dict(os.environ, {'CODEX_HOME': str(codex_home),
                                     'CLAUDE_CONFIG_DIR': str(self.root / 'claude-home'),
                                     'PI_CODING_AGENT_DIR': str(self.root / 'pi-home')}):
            agents.discover()
        self.assertEqual(agents.discovered_sources, 52)
        self.assertEqual(agents.omitted_sources, 2)
        self.assertEqual(len(agents.sessions), 50)

    def test_local_discovery_includes_recent_pi_sessions(self):
        pi_home = self.root / 'pi-home'
        session_root = pi_home / 'sessions' / 'project'
        session_root.mkdir(parents=True)
        session_file = session_root / '2026-10-24T12-00-00Z_pi-session.jsonl'
        session_file.write_text(json.dumps({'type': 'session', 'id': 'pi-session',
                                            'cwd': '/work/pi-demo'}) + '\n')
        agents = LocalAgents(self.store)
        with patch.dict(os.environ, {'CODEX_HOME': str(self.root / 'codex-home'),
                                     'CLAUDE_CONFIG_DIR': str(self.root / 'claude-home'),
                                     'PI_CODING_AGENT_DIR': str(pi_home)}):
            agents.discover()
        self.assertEqual(agents.discovered_sources, 1)
        self.assertEqual([session.runtime for session in agents.sessions.values()], ['Pi'])

    def append(self, session, *rows):
        with session.path.open("a") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")
        session.poll()
        self.assertEqual(session.skipped, 0)

    def test_native_model_spans_keep_reported_usage_breakdowns_only(self):
        codex = self.session("Codex")
        codex_span = codex.model_span(("usage", "codex"), {
            "input_tokens": 80, "output_tokens": 25, "cached_input_tokens": 12,
            "reasoning_output_tokens": 6}, 100)
        codex_attrs = codex_span["attributes"]
        self.assertEqual(codex_attrs["gen_ai.usage.input_tokens"], 80)
        self.assertEqual(codex_attrs["gen_ai.usage.cache_read.input_tokens"], 12)
        self.assertEqual(codex_attrs["gen_ai.usage.reasoning_tokens"], 6)
        self.assertNotIn("gen_ai.usage.cache_write.input_tokens", codex_attrs)

        claude = self.session("Claude Code")
        claude_span = claude.model_span(("usage", "claude"), {
            "input_tokens": 70, "output_tokens": 15, "cache_read_input_tokens": 20,
            "cache_creation_input_tokens": 5, "reasoning_tokens": 3}, 100, claude=True)
        claude_attrs = claude_span["attributes"]
        self.assertEqual(claude_attrs["gen_ai.usage.input_tokens"], 95)
        self.assertEqual(claude_attrs["gen_ai.usage.cache_read.input_tokens"], 20)
        self.assertEqual(claude_attrs["gen_ai.usage.cache_write.input_tokens"], 5)
        self.assertEqual(claude_attrs["gen_ai.usage.reasoning_tokens"], 3)

    def test_diagnostics_distinguish_successful_empty_poll_from_captured_events(self):
        session = self.session("Codex")
        self.append(session, self.codex({"type": "task_started"}, 1, "event_msg"))
        diag = session.diagnostics()
        self.assertEqual(diag["records_last_poll"], 1)
        self.assertEqual(diag["spans_last_poll"], 1)
        self.assertEqual(diag["logs_last_poll"], 1)
        self.assertEqual(diag["records_total"], 1)
        self.assertEqual(diag["spans_total"], 1)
        self.assertEqual(diag["logs_total"], 1)
        self.assertEqual(diag["source_size_bytes"], diag["bytes_read"])
        session.poll()
        self.assertEqual(session.diagnostics()["records_last_poll"], 0)
        self.assertEqual(session.diagnostics()["records_total"], 1)

    def test_pi_session_messages_emit_session_model_tool_and_activity_records(self):
        session = self.session("Pi")
        rows = [
            {"type": "session", "version": 3, "id": "pi-session", "cwd": "/work/pi-demo"},
            {"type": "message", "id": "user-1", "timestamp": "2026-10-24T12:00:00Z",
             "message": {"role": "user", "content": [{"type": "text", "text": "Inspect the project"}]}},
            {"type": "message", "id": "assistant-1", "timestamp": "2026-10-24T12:00:01Z",
             "message": {"role": "assistant", "provider": "test-provider", "model": "test-model",
                         "stopReason": "toolUse", "usage": {"input": 8, "output": 3, "cacheRead": 2},
                         "content": [{"type": "toolCall", "id": "call-1", "name": "read",
                                      "arguments": {"path": "README.md"}}]}},
            {"type": "message", "id": "tool-1", "timestamp": "2026-10-24T12:00:02Z",
             "message": {"role": "toolResult", "toolCallId": "call-1", "toolName": "read",
                         "isError": False, "content": [{"type": "text", "text": "ok"}]}},
            {"type": "message", "id": "assistant-2", "timestamp": "2026-10-24T12:00:03Z",
             "message": {"role": "assistant", "provider": "test-provider", "model": "test-model",
                         "stopReason": "stop", "content": [{"type": "text", "text": "Done"}]}},
        ]
        self.append(session, *rows)
        self.assertEqual(session.session_id, "pi-session")
        self.assertEqual(session.project, "pi-demo")
        self.assertEqual(session.run_state, "completed")
        self.assertEqual(session.tasks.get(session.turn), "Inspect the project")
        with self.store._connect() as db:
            spans = db.execute("SELECT name,attributes FROM spans WHERE service='pi'").fetchall()
            logs = db.execute("SELECT event_name,attributes FROM logs WHERE service='pi'").fetchall()
        names = {row["name"] for row in spans}
        self.assertIn("agent.run", names)
        self.assertIn("read", names)
        model = next(json.loads(row["attributes"]) for row in spans if row["name"].startswith("Pi ·"))
        self.assertEqual(model["gen_ai.provider.name"], "test-provider")
        self.assertEqual(model["gen_ai.usage.input_tokens"], 8)
        self.assertEqual(model["gen_ai.usage.cache_read.input_tokens"], 2)
        domains = [json.loads(row["attributes"]).get("agent.activity.domain") for row in logs]
        self.assertIn("message", domains)
        self.assertIn("tool", domains)

    def claude(self, kind, content, second, **extra):
        return {"type": kind, "timestamp": f"2026-10-15T12:00:{second:02d}Z",
                "uuid": str(second), "sessionId": "claude-session", "cwd": "/work/project",
                "message": {"content": content}, **extra}

    def codex(self, payload, second, kind="response_item"):
        return {"type": kind, "timestamp": f"2026-10-15T12:00:{second:02d}Z", "payload": payload}

    def test_claude_mcp_start_result_stays_one_call_with_full_identity(self):
        session = self.session("Claude Code")
        name = "mcp__plugin_project-github__search_issues"
        self.append(session, self.claude("user", "Inspect issues", 0),
            self.claude("assistant", [{"type": "tool_use", "id": "call-1", "name": name,
                                      "input": {"query": "bug"}}], 1))
        call, = self.store.mcp_calls()
        self.assertEqual((call["server"], call["tool"], call["status"]),
                         ("plugin_project-github", "search_issues", "RUNNING"))
        call_id = call["id"]
        self.append(session, self.claude("user", [{"type": "tool_result", "tool_use_id": "call-1",
                      "content": "Permission denied", "is_error": True}], 3))
        call, = self.store.mcp_calls()
        self.assertEqual(call["id"], call_id)
        self.assertEqual(call["status"], "ERROR")
        self.assertEqual(call["duration_ms"], 2000)
        self.assertIn("bug", str(call["arguments"]))
        self.assertIn("Permission denied", str(call["output"]))
        self.assertEqual(call["source"], "session log + span")
        # Re-reading a file after restart must not create duplicate calls.
        replay = Session(self.store, session.path, "Claude Code")
        replay.poll()
        self.assertEqual(len(self.store.mcp_calls()), 1)

    def test_codex_function_mcp_and_normal_tool_classification(self):
        session = self.session("Codex")
        self.append(session,
            self.codex({"turn_id": "turn-1"}, 0, "turn_context"),
            self.codex({"type": "function_call", "call_id": "call-1", "name": "mcp__my_server__read_file",
                        "arguments": '{"path":"/docs/README.md"}'}, 1),
            self.codex({"type": "function_call", "call_id": "shell-1", "name": "exec_command",
                        "arguments": '{"cmd":"pwd"}'}, 2))
        call, = self.store.mcp_calls()
        self.assertEqual((call["server"], call["tool"]), ("my_server", "read_file"))
        self.append(session, self.codex({"type": "function_call_output", "call_id": "call-1",
                        "output": '{"isError":true,"content":[]}'}, 4))
        call, = self.store.mcp_calls()
        self.assertEqual(call["status"], "ERROR")
        self.assertEqual(call["duration_ms"], 3000)
        self.assertIn("README.md", str(call["arguments"]))

    def test_parallel_claude_calls_do_not_collide_and_metadata_stays_private(self):
        session = self.session("Claude Code", details=False)
        self.append(session, self.claude("assistant", [
            {"type": "tool_use", "id": key, "name": "mcp__github__search", "input": {"query": "private-query"}}
            for key in ("one", "two")], 1))
        self.assertEqual(len(self.store.mcp_calls()), 2)
        self.append(session, self.claude("user", [
            {"type": "tool_result", "tool_use_id": key, "content": "private-result"}
            for key in ("one", "two")], 2))
        calls = self.store.mcp_calls()
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call["status"] == "OK" for call in calls))
        self.assertNotIn("private-", json.dumps(calls))

    def test_codex_explicit_mcp_is_error_updates_span(self):
        session = self.session("Codex")
        self.append(session, self.codex({"type": "item_completed", "item": {
            "type": "McpToolCall", "id": "one", "server": "github", "tool": "search",
            "status": "completed", "result": {"isError": True}}}, 1, "event_msg"))
        call, = self.store.mcp_calls()
        self.assertEqual(call["status"], "ERROR")
        span, = self.store.trace(call["trace_id"])["spans"]
        self.assertEqual(span["status"], "ERROR")

    def test_session_status_waiting_completion_and_project_preservation(self):
        session = self.session('Codex')
        self.append(session, self.codex({'id': 'session', 'cwd': '/work/repo',
                    'git': {'branch': 'feature/mcp'}}, 0, 'session_meta'),
                    self.codex({'turn_id': 'turn-1'}, 1, 'turn_context'),
                    self.codex({'type': 'task_started'}, 2, 'event_msg'))
        self.assertEqual((session.project, session.branch, session.run_state), ('repo', 'feature/mcp', 'running'))
        self.append(session, self.codex({'type': 'function_call', 'call_id': 'input',
                    'name': 'functions.request_user_input', 'arguments': '{}'}, 3))
        self.assertEqual((session.run_state, session.waiting_for), ('waiting', 'User input'))
        self.append(session, self.codex({'type': 'function_call_output', 'call_id': 'input', 'output': 'yes'}, 4))
        self.assertEqual((session.run_state, session.waiting_for), ('running', ''))
        self.append(session, self.codex({'type': 'task_complete'}, 5, 'event_msg'))
        self.assertEqual((session.run_state, session.waiting_for), ('completed', ''))
        self.assertEqual(session.working_directory, '/work/repo')

    def test_mcp_inventory_covers_history_and_respects_scope(self):
        from observatory.core import normalize_native
        spans = []
        for i in range(105):
            spans.append(normalize_native({'trace_id': f'{i+1:032x}', 'span_id': f'{i+1:016x}',
                'name': 'tool', 'start_unix_nano': (i+1)*1_000_000_000, 'end_unix_nano': (i+1)*1_000_000_000,
                'status': 'ERROR' if i == 0 else 'OK',
                'attributes': {'mcp.server.name': 'older-server' if i == 0 else 'github',
                               'mcp.tool.name': 'search', 'session.id': 'a' if i < 104 else 'b'}}, 'codex'))
        self.store.ingest(spans)
        page = self.store.mcp_page(limit=100)
        self.assertEqual(len(page['calls']), 100)
        self.assertEqual(page['summary']['calls'], 105)
        self.assertEqual(page['summary']['errors'], 1)
        self.assertEqual({row['server'] for row in page['summary']['servers']}, {'github', 'older-server'})
        self.assertEqual(self.store.mcp_page(session_id='b')['summary']['calls'], 1)
        # Search and pagination change the table, not the scope-wide inventory.
        self.assertEqual(self.store.mcp_page(query='no-matches')['summary'], page['summary'])
        self.assertEqual(self.store.mcp_page(cursor=page['next_cursor'])['summary'], page['summary'])
