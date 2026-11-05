import importlib.util
from pathlib import Path
import signal
import subprocess
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('start', Path(__file__).resolve().parents[1] / 'start.py')
start = importlib.util.module_from_spec(spec)
spec.loader.exec_module(start)


class StartTests(unittest.TestCase):
    def test_reuses_matching_proxy_without_stopping_it(self):
        metadata = {'status': 'ok', 'provider': 'codex', 'proxy_port': 8041, 'ui_port': 8042, 'auto_route': True}
        agent = Mock()
        agent.wait.return_value = 0
        agent.poll.return_value = 0
        with patch.object(start, 'health', return_value=metadata), \
             patch.object(start, 'accepting', return_value=True), \
             patch.object(start.shutil, 'which', side_effect=lambda name: '/bin/' + name), \
             patch.object(start.subprocess, 'Popen', return_value=agent) as spawn:
            self.assertEqual(start.main(['--no-browser', '--', '--help']), 0)
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(spawn.call_args.args[0][-1], '--help')
        agent.terminate.assert_not_called()

    def test_cross_provider_reuses_auto_routing_proxy(self):
        metadata = {'status': 'ok', 'provider': 'codex', 'proxy_port': 8041,
                    'ui_port': 8042, 'auto_route': True}
        agent = Mock()
        agent.wait.return_value = 0
        agent.poll.return_value = 0
        with patch.object(start, 'health', return_value=metadata), \
             patch.object(start, 'accepting', return_value=True), \
             patch.object(start.shutil, 'which', side_effect=lambda name: '/bin/' + name), \
             patch.object(start.subprocess, 'Popen', return_value=agent) as spawn:
            self.assertEqual(start.main(['--provider', 'claude', '--no-browser']), 0)
        self.assertEqual(spawn.call_count, 1)
        self.assertIn('claude-proxy', spawn.call_args.args[0][1])
        agent.terminate.assert_not_called()

    def test_incompatible_occupied_ports_error(self):
        metadata = {'status': 'ok', 'provider': 'unknown', 'proxy_port': 8041, 'ui_port': 8042, 'auto_route': True}
        with patch.object(start, 'health', return_value=metadata), \
             patch.object(start, 'accepting', return_value=True), \
             patch.object(start.subprocess, 'Popen') as spawn:
            self.assertEqual(start.main(['--no-agent', '--no-browser']), 1)
        spawn.assert_not_called()

    def test_startup_failure_cleans_up_owned_proxy(self):
        owned = Mock()
        owned.poll.return_value = None
        owned.wait.return_value = 0
        with patch.object(start, 'health', return_value=None), \
             patch.object(start, 'accepting', return_value=False), \
             patch.object(start.subprocess, 'Popen', return_value=owned), \
             patch.object(start.time, 'monotonic', side_effect=[0, 20]):
            self.assertEqual(start.main(['--no-agent', '--no-browser']), 1)
        owned.terminate.assert_called_once()

    def test_missing_agent_is_actionable(self):
        with patch.object(start.shutil, 'which', return_value=None), \
             patch.object(start.subprocess, 'Popen') as spawn:
            self.assertEqual(start.main(['--no-browser']), 1)
        spawn.assert_not_called()

    def test_stubborn_owned_process_is_killed(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired('proxy', 3), 0]
        start.stop_process(process)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()

    def test_wsl_opens_windows_browser(self):
        with patch.dict(start.os.environ, {'WSL_DISTRO_NAME': 'Ubuntu'}), \
             patch.object(start.shutil, 'which', side_effect=lambda name: '/bin/cmd.exe' if name == 'cmd.exe' else None), \
             patch.object(start.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            self.assertTrue(start.open_dashboard('http://localhost:8042'))
        self.assertEqual(run.call_args.args[0], ['cmd.exe', '/c', 'start', '', 'http://localhost:8042'])

    def test_custom_upstream_is_not_reused(self):
        metadata = {'status': 'ok', 'provider': 'codex', 'proxy_port': 8041,
                    'ui_port': 8042, 'auto_route': False}
        self.assertFalse(start.compatible(metadata, 'codex', 8041, 8042))

    def test_port_validation(self):
        for value in ('0', '65536', 'oops'):
            with self.assertRaises(start.argparse.ArgumentTypeError):
                start.port_number(value)
        self.assertEqual(start.port_number('8051'), 8051)


if __name__ == '__main__':
    unittest.main()
