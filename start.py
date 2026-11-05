#!/usr/bin/env python3
"""Start the local dashboard and selected agent together, standard library only."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent


def port_number(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('port must be an integer') from None
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError('port must be between 1 and 65535')
    return number


def accepting(port: int) -> bool:
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.3):
            return True
    except OSError:
        return False


def health(port: int) -> dict | None:
    try:
        # Ignore proxy environment variables for this strictly local request.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{port}/health', timeout=0.5) as response:
            value = json.loads(response.read(8192))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def compatible(value: dict | None, provider: str, port: int, ui_port: int) -> bool:
    return bool(value and value.get('status') == 'ok'
                and value.get('provider') in ('codex', 'claude')
                and value.get('proxy_port') == port
                and value.get('ui_port') == ui_port
                and value.get('auto_route') is True)


def open_dashboard(url: str) -> bool:
    """Open a Windows browser under WSL, otherwise use the platform default."""
    wsl = bool(os.environ.get('WSL_DISTRO_NAME')) or 'microsoft' in platform.release().lower()
    command = None
    if wsl:
        if shutil.which('wslview'):
            command = ['wslview', url]
        elif shutil.which('cmd.exe'):
            command = ['cmd.exe', '/c', 'start', '', url]
    if command:
        try:
            result = subprocess.run(command, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=10)
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    try:
        return bool(webbrowser.open(url, new=2))
    except webbrowser.Error:
        return False


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', choices=('codex', 'claude'), default='codex')
    parser.add_argument('--port', type=port_number, default=8041)
    parser.add_argument('--ui-port', type=port_number, default=8042)
    parser.add_argument('--no-browser', action='store_true', help='print the dashboard URL without opening it')
    parser.add_argument('--no-agent', action='store_true', help='run only the proxy and dashboard until Ctrl+C')
    parser.add_argument('agent_args', nargs=argparse.REMAINDER, help='agent options after --')
    args = parser.parse_args(argv)
    if args.port == args.ui_port:
        parser.error('--port and --ui-port must be different')
    if not args.no_agent and not shutil.which(args.provider):
        print(f'{args.provider} CLI was not found on PATH. Install/sign in to it first, '
              'or use --no-agent to open only the dashboard.', file=sys.stderr)
        return 1
    bash = shutil.which('bash')
    if not args.no_agent and not bash:
        print('bash was not found on PATH; run this command inside WSL or another Bash environment.', file=sys.stderr)
        return 1

    owned = None
    agent = None
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        existing = health(args.ui_port)
        if compatible(existing, args.provider, args.port, args.ui_port) and accepting(args.port):
            print('Using the compatible proxy already running on these ports.', flush=True)
        else:
            occupied = [str(port) for port in (args.port, args.ui_port) if accepting(port)]
            if occupied:
                print(f'Port(s) {", ".join(occupied)} already in use by a different or older service. '
                      'Stop that service or choose --port and --ui-port.', file=sys.stderr)
                return 1
            owned = subprocess.Popen(
                [sys.executable, '-u', str(ROOT / 'proxy.py'), '--provider', args.provider,
                 '--port', str(args.port), '--ui-port', str(args.ui_port)],
                cwd=ROOT, stdout=subprocess.DEVNULL, start_new_session=True,
            )
            deadline = time.monotonic() + 10
            while True:
                if owned.poll() is not None:
                    print(f'Proxy exited during startup (status {owned.returncode}). '
                          'Check its error above and whether the ports are available.', file=sys.stderr)
                    return 1
                if compatible(health(args.ui_port), args.provider, args.port, args.ui_port) and accepting(args.port):
                    break
                if time.monotonic() >= deadline:
                    print('Proxy did not become ready within 10 seconds.', file=sys.stderr)
                    return 1
                time.sleep(0.1)
        url = f'http://localhost:{args.ui_port}'
        print(f'Dashboard: {url}', flush=True)
        if not args.no_browser and not open_dashboard(url):
            print(f'Could not open a browser automatically. Open {url} in your browser.', file=sys.stderr)
        if args.no_agent:
            print('Proxy ready. Press Ctrl+C to exit.', flush=True)
            while owned is None or owned.poll() is None:
                time.sleep(0.5)
            return owned.returncode or 0
        forwarded = args.agent_args[1:] if args.agent_args[:1] == ['--'] else args.agent_args
        agent = subprocess.Popen([bash, str(ROOT / f'{args.provider}-proxy'),
                                  '--proxy-port', str(args.port), *forwarded], cwd=Path.cwd())
        return agent.wait()
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        print(f'Unable to start {args.provider}: {exc}', file=sys.stderr)
        return 1
    finally:
        stop_process(agent)
        stop_process(owned)
        signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == '__main__':
    raise SystemExit(main())
