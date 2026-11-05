"""One running proxy forwards both agent protocols to their own upstream."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('routing_proxy', Path(__file__).resolve().parents[1] / 'proxy.py')
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


class RoutingTests(unittest.TestCase):
    def test_detects_compaction_and_custom_gateway(self):
        self.assertEqual(proxy.request_provider('/responses/compact'), 'codex')
        self.assertEqual(proxy.request_provider('/v1/messages?beta=true'), 'claude')
        with patch.object(proxy, 'UPSTREAM', 'http://127.0.0.1:1234/gateway'):
            self.assertEqual(proxy.request_upstream('/v1/messages'), 'http://127.0.0.1:1234/gateway')

    def test_both_protocols_route_without_restart(self):
        received = []
        class Upstream(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append((self.server.server_port, self.path, self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200)
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'{}')
            def log_message(self, *args):
                pass
        codex = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        claude = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        relay = ThreadingHTTPServer(('127.0.0.1', 0), proxy.Tap)
        for server in (codex, claude, relay):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        targets = {'codex': f'http://127.0.0.1:{codex.server_port}/codex', 'claude': f'http://127.0.0.1:{claude.server_port}'}
        try:
            with patch.dict(proxy.PROVIDER_UPSTREAMS, targets), patch.object(proxy, 'UPSTREAM', targets['codex']), contextlib.redirect_stdout(io.StringIO()):
                for path in ('/responses', '/v1/messages?beta=true', '/responses/compact'):
                    request = urllib.request.Request(f'http://127.0.0.1:{relay.server_port}{path}', data=b'{}')
                    with urllib.request.urlopen(request, timeout=5) as response:
                        self.assertEqual(response.read(), b'{}')
            self.assertEqual(received, [(codex.server_port, '/codex/responses', b'{}'), (claude.server_port, '/v1/messages?beta=true', b'{}'), (codex.server_port, '/codex/responses/compact', b'{}')])
        finally:
            for server in (relay, codex, claude):
                server.shutdown()
                server.server_close()

    def test_health_reports_configuration(self):
        ui = ThreadingHTTPServer(('127.0.0.1', 0), proxy.UI)
        threading.Thread(target=ui.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{ui.server_port}/health', timeout=3) as response:
                data = json.load(response)
            self.assertEqual(data['status'], 'ok')
            self.assertTrue(data['auto_route'])
            self.assertEqual(data['proxy_port'], 8041)
        finally:
            ui.shutdown()
            ui.server_close()

    def test_unreachable_upstream_returns_502_and_dashboard_error(self):
        # Reserve a local port without listening, making connection refusal deterministic.
        import socket
        reserved = socket.socket()
        reserved.bind(('127.0.0.1', 0))
        relay = ThreadingHTTPServer(('127.0.0.1', 0), proxy.Tap)
        threading.Thread(target=relay.serve_forever, daemon=True).start()
        proxy.DASH = proxy.Dashboard()
        try:
            with patch.object(proxy, 'UPSTREAM', f'http://127.0.0.1:{reserved.getsockname()[1]}'), contextlib.redirect_stdout(io.StringIO()):
                request = urllib.request.Request(f'http://127.0.0.1:{relay.server_port}/responses', data=b'{"input":"hello"}')
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(request, timeout=3)
                self.assertEqual(failure.exception.code, 502)
                failure.exception.close()
            end = [json.loads(e[3]) for e in proxy.DASH.events if e[2] == 'call_end'][-1]
            self.assertIn('cannot reach', end['error'])
        finally:
            relay.shutdown()
            relay.server_close()
            reserved.close()
