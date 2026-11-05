import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('provider_proxy', ROOT / 'proxy.py')
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        proxy.DASH = proxy.Dashboard()

    def events(self):
        return [json.loads(event[3]) for event in proxy.DASH.events]

    def test_claude_forwarding_stream_and_pairing(self):
        events = [
            {'type': 'message_start', 'message': {'model': 'claude-test', 'usage': {'input_tokens': 20}}},
            {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
            {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'Hello Claude'}},
            {'type': 'content_block_stop', 'index': 0},
            {'type': 'content_block_start', 'index': 1, 'content_block': {'type': 'tool_use', 'id': 'tool-next', 'name': 'Bash'}},
            {'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'input_json_delta', 'partial_json': '{"command":"pwd"}'}},
            {'type': 'content_block_stop', 'index': 1},
            {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 9}},
        ]
        payload = ''.join('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n' for event in events).encode()
        received = {}

        class Upstream(BaseHTTPRequestHandler):
            def do_POST(self):
                received.update(path=self.path, body=self.rfile.read(int(self.headers['Content-Length'])), key=self.headers['x-api-key'])
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        relay = ThreadingHTTPServer(('127.0.0.1', 0), proxy.Tap)
        original = proxy.UPSTREAM
        proxy.UPSTREAM = f'http://127.0.0.1:{upstream.server_port}'
        for server in (upstream, relay):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        request_data = {'model': 'claude-test', 'max_tokens': 100, 'tools': [{'name': 'Bash'}], 'messages': [
            {'role': 'user', 'content': 'Hi'},
            {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'old-tool', 'name': 'Bash', 'input': {}}]},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'old-tool', 'content': 'done'}]},
        ]}
        body = json.dumps(request_data).encode()
        try:
            req = urllib.request.Request(f'http://127.0.0.1:{relay.server_port}/v1/messages?beta=true', data=body,
                                         headers={'x-api-key': 'test-secret', 'X-Claude-Code-Session-Id': 'claude-session'})
            with contextlib.redirect_stdout(io.StringIO()), urllib.request.urlopen(req, timeout=5) as response:
                self.assertEqual(response.read(), payload)
            # Wait for the side-effect publication after the last forwarded byte.
            for _ in range(100):
                if any(e['type'] == 'call_end' for e in self.events()):
                    break
                threading.Event().wait(.01)
            self.assertEqual(received, {'path': '/v1/messages?beta=true', 'body': body, 'key': 'test-secret'})
            rendered = self.events()
            start = next(e for e in rendered if e['type'] == 'call_start')
            self.assertEqual(start['kind'], 'main')
            self.assertEqual(start['new_messages'][1]['content'][0]['id'], start['new_messages'][2]['content'][0]['tool_use_id'])
            stops = [e for e in rendered if e['type'] == 'block_stop']
            self.assertEqual(stops[0]['text'], 'Hello Claude')
            self.assertEqual(stops[1]['input'], {'command': 'pwd'})
            self.assertEqual(next(e for e in rendered if e['type'] == 'call_end')['usage']['output'], 9)
            self.assertNotIn('test-secret', json.dumps(list(proxy.DASH.raw.values())))
        finally:
            for server in (relay, upstream):
                server.shutdown()
                server.server_close()
            proxy.UPSTREAM = original

    def test_codex_complete_items_without_deltas(self):
        tap = proxy.CallTap('POST', '/responses', {'session_id': 's'}, json.dumps({'model': 'test', 'input': 'Hi'}).encode())
        tap.start()
        tap.response(200, 'OK', [('Content-Type', 'application/json')])
        body = {'object': 'response', 'status': 'completed', 'error': None, 'output': [
            {'type': 'message', 'content': [{'type': 'output_text', 'text': 'Answer'}]},
            {'type': 'function_call', 'name': 'shell', 'call_id': 'c1', 'arguments': '{"command":"pwd"}'},
        ], 'usage': {'input_tokens': 5, 'output_tokens': 4}}
        tap.feed(json.dumps(body).encode())
        tap.finish()
        self.assertFalse(tap.broken)
        stops = [e for e in self.events() if e['type'] == 'block_stop']
        self.assertEqual(stops[0]['text'], 'Answer')
        self.assertEqual(stops[1]['input'], {'command': 'pwd'})
        self.assertEqual(self.events()[-1]['usage']['output'], 4)

    def test_provider_cli_options_and_invalid_urls(self):
        result = subprocess.run(['python3', str(ROOT / 'proxy.py'), '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn('--provider {codex,claude}', result.stdout)
        result = subprocess.run(['python3', str(ROOT / 'proxy.py'), '--upstream', 'file:///tmp/test'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('HTTP(S) base URL', result.stderr)
        self.assertEqual(proxy.PROVIDER_UPSTREAMS['claude'], 'https://api.anthropic.com')
        self.assertEqual(proxy.PROVIDER_UPSTREAMS['codex'], 'https://chatgpt.com/backend-api/codex')


if __name__ == '__main__':
    unittest.main()
