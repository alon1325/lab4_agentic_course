"""Local deterministic dashboard/SSE fixture. Never forwards or uses credentials."""
import importlib.util
import json
from pathlib import Path
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('dashboard', ROOT / 'proxy.py')
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)
lock = threading.Lock()
events = []
subscribers = set()


class Fixture(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def reply(self, body, content_type='application/json'):
        encoded = body.encode()
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
        with lock:
            if self.path == '/test/reset':
                events.clear()
            elif self.path == '/test/disconnect':
                for subscriber in subscribers:
                    subscriber.put(None)
            else:
                for event in data:
                    events.append(event)
                    for subscriber in subscribers:
                        subscriber.put((len(events), event))
        self.reply('{}')

    def do_GET(self):
        if self.path == '/':
            self.reply(proxy.PAGE, 'text/html; charset=utf-8')
        elif self.path == '/health':
            self.reply(json.dumps({'provider': 'codex', 'proxy_port': 8051, 'ui_port': 18042, 'status': 'ok'}))
        elif self.path.startswith('/raw/'):
            self.reply(json.dumps({'request': {'method': 'POST', 'path': '/responses', 'headers': [], 'body': '<environment_context>private workspace</environment_context>'}, 'response': {'status': 200, 'body': 'ok', 'headers': []}}))
        elif self.path == '/events':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'close')
            self.end_headers()
            subscriber = queue.Queue()
            with lock:
                resume = int(self.headers.get('Last-Event-ID', 0))
                replay = list(enumerate(events, start=1))[resume:]
                subscribers.add(subscriber)
            try:
                self.emit(None, {'type': 'hello', 'boot': 'browser-fixture', 'replay': True})
                for eid, event in replay:
                    self.emit(eid, event)
                self.emit(None, {'type': 'replay_done'})
                while True:
                    try:
                        item = subscriber.get(timeout=1)
                    except queue.Empty:
                        self.wfile.write(b': keepalive\n\n')
                        self.wfile.flush()
                        continue
                    if item is None:
                        return
                    self.emit(*item)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with lock:
                    subscribers.discard(subscriber)
                self.close_connection = True
        else:
            self.send_error(404)

    def emit(self, eid, event):
        data = (f'id: {eid}\n' if eid else '') + 'data: ' + json.dumps(event) + '\n\n'
        self.wfile.write(data.encode())
        self.wfile.flush()


if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1', 18042), Fixture).serve_forever()
