import importlib.util
import json
from pathlib import Path
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

spec = importlib.util.spec_from_file_location('proxy', Path(__file__).resolve().parents[1] / 'proxy.py')
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


class CodexTests(unittest.TestCase):
    def test_responses_forwarding_and_dashboard(self):
        proxy.DASH = proxy.Dashboard()
        events = [
            {'type': 'response.output_text.delta', 'output_index': 0, 'delta': 'Hello Codex'},
            {'type': 'response.output_item.done', 'output_index': 0, 'item': {'type': 'message'}},
            {'type': 'response.output_item.added', 'output_index': 1, 'item': {'type': 'function_call', 'name': 'shell', 'call_id': 'tool1'}},
            {'type': 'response.function_call_arguments.delta', 'output_index': 1, 'delta': '{"command":"pwd"}'},
            {'type': 'response.output_item.done', 'output_index': 1, 'item': {'type': 'function_call'}},
            {'type': 'response.completed', 'response': {'status': 'completed', 'usage': {'input_tokens': 10, 'output_tokens': 4, 'input_tokens_details': {'cached_tokens': 3}}}},
        ]
        payload = ''.join('data: ' + json.dumps(e) + '\n\n' for e in events).encode()
        received = {}

        class Upstream(BaseHTTPRequestHandler):
            def do_POST(self):
                received['path'] = self.path
                received['body'] = self.rfile.read(int(self.headers['Content-Length']))
                received['auth'] = self.headers['Authorization']
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        relay = ThreadingHTTPServer(('127.0.0.1', 0), proxy.Tap)
        proxy.UPSTREAM = f'http://127.0.0.1:{upstream.server_port}/backend-api/codex'
        for server in (upstream, relay):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        body = json.dumps({'model': 'test', 'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'Hi'}]}, {'type': 'function_call_output', 'call_id': 'previous', 'output': 'done'}]}).encode()
        try:
            request = urllib.request.Request(f'http://127.0.0.1:{relay.server_port}/responses', data=body, headers={'Authorization': 'Bearer secret-test', 'session_id': 'test-session'})
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.read(), payload)
            self.assertEqual(received, {'path': '/backend-api/codex/responses', 'body': body, 'auth': 'Bearer secret-test'})
            rendered = [json.loads(e[3]) for e in proxy.DASH.events]
            start = next(e for e in rendered if e['type'] == 'call_start')
            self.assertEqual(start['kind'], 'main')
            self.assertEqual(start['new_messages'][0]['content'][0]['text'], 'Hi')
            self.assertEqual(start['new_messages'][1]['content'][0]['type'], 'tool_result')
            stops = [e for e in rendered if e['type'] == 'block_stop']
            self.assertEqual(stops[0]['text'], 'Hello Codex')
            self.assertEqual(stops[1]['input'], {'command': 'pwd'})
            end = next(e for e in rendered if e['type'] == 'call_end')
            self.assertEqual(end['usage']['cache_read'], 3)
            self.assertNotIn('secret-test', json.dumps(list(proxy.DASH.raw.values())))
        finally:
            for server in (relay, upstream):
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
