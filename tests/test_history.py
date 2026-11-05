"""Regression coverage for ordered Codex history and echoed output items."""
import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('history_proxy', Path(__file__).resolve().parents[1] / 'proxy.py')
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


def user(text):
    return {'role': 'user', 'content': [{'type': 'input_text', 'text': text}]}


def assistant(text):
    return {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': text}]}


def tool(call_id):
    return {'type': 'function_call', 'call_id': call_id, 'name': 'shell', 'arguments': '{"command":"pwd"}'}


def result(call_id):
    return {'type': 'function_call_output', 'call_id': call_id, 'output': '/workspace'}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        proxy.DASH = proxy.Dashboard()

    def call(self, items, response_id=None, previous_response_id=None):
        request = {'model': 'test', 'input': items, 'instructions': 'stable instructions'}
        if previous_response_id:
            request['previous_response_id'] = previous_response_id
        tap = proxy.CallTap('POST', '/responses', {'session_id': 'history-session'}, json.dumps(request).encode())
        tap.start()
        if response_id:
            tap.responses_event('response.created', {'response': {'id': response_id}})
        start = [json.loads(e[3]) for e in proxy.DASH.events if e[2] == 'call_start'][-1]
        return tap, start

    def finish(self, tap, output):
        tap.response(200, 'OK', [('Content-Type', 'text/event-stream')])
        for i, item in enumerate(output):
            tap.responses_event('response.output_item.done', {'output_index': i, 'item': item})
        tap.finish()
        self.assertFalse(tap.broken)

    def test_multi_item_output_echoed_across_tool_cycles(self):
        prompt = user('Inspect the repository')
        output1 = [assistant('I will inspect it.'), tool('call-1'), tool('call-2')]
        first, start = self.call([prompt])
        self.assertEqual(len(start['new_messages']), 1)
        self.finish(first, output1)
        history = [prompt, *output1, result('call-1'), result('call-2')]
        second, start = self.call(history)
        self.assertEqual([m['content'][0]['tool_use_id'] for m in start['new_messages']], ['call-1', 'call-2'])
        self.assertEqual(start['track']['rewritten'], 0)
        self.finish(second, [assistant('The directory is /workspace.'), tool('call-3')])
        history.extend([assistant('The directory is /workspace.'), tool('call-3'), result('call-3')])
        third, start = self.call(history)
        self.assertEqual(len(start['new_messages']), 1)
        self.assertEqual(start['new_messages'][0]['content'][0]['tool_use_id'], 'call-3')
        self.assertEqual(start['track']['index'], 1)
        self.assertEqual(start['track']['rewritten'], 0)

    def test_same_user_prompt_repeated_after_reply_is_new(self):
        first, _ = self.call([user('Continue')])
        self.finish(first, [assistant('Working')])
        _, start = self.call([user('Continue'), assistant('Working'), user('Continue')])
        self.assertEqual(len(start['new_messages']), 1)
        self.assertEqual(start['new_messages'][0]['content'][0]['text'], 'Continue')

    def test_resumed_history_without_new_input_is_not_redisplayed(self):
        first, _ = self.call([user('Hello')])
        output = [assistant('Hello back'), tool('resume-tool')]
        self.finish(first, output)
        _, start = self.call([user('Hello'), *output])
        self.assertEqual(start['new_messages'], [])
        self.assertEqual(start['track']['index'], 1)
        self.assertEqual(start['track']['rewritten'], 0)

    def test_compaction_retained_suffix_keeps_thread_and_only_displays_new_input(self):
        first, _ = self.call([user('Old question'), assistant('Old answer'), user('Latest question')])
        self.finish(first, [assistant('Latest answer'), tool('retained')])
        context = {'role': 'developer', 'content': [{'type': 'input_text', 'text': 'A compact summary'}]}
        _, start = self.call([context, user('Latest question'), assistant('Latest answer'), tool('retained'), result('retained')])
        self.assertEqual(len(start['new_messages']), 1)
        self.assertEqual(start['new_messages'][0]['content'][0]['tool_use_id'], 'retained')
        self.assertEqual(start['track']['index'], 1)
        self.assertEqual(start['track']['rewritten'], 2)

    def test_compaction_with_changed_instructions_still_matches_retained_tail(self):
        dash = proxy.DASH
        previous = proxy.responses_messages([user('Old'), assistant('First'), user('Recent'), assistant('Last')])
        track, _, _ = dash.assign('s', previous, 'old-system', responses=True)
        compacted = proxy.responses_messages([user('Recent'), assistant('Last'), user('Next')])
        same, start, info = dash.assign('s', compacted, 'new-system', responses=True)
        self.assertIs(same, track)
        self.assertEqual(start, 2)
        self.assertEqual(info['rewritten'], 2)

    def test_compaction_does_not_discard_new_prompt_beside_context(self):
        first, _ = self.call([user('Old question')])
        self.finish(first, [assistant('Retained answer')])
        mixed = user('<environment_context>cwd: /workspace</environment_context>\nA new instruction')
        _, start = self.call([mixed, assistant('Retained answer'), user('Continue')])
        self.assertEqual(len(start['new_messages']), 3)
        self.assertIn('A new instruction', start['new_messages'][0]['content'][0]['text'])

    def test_complete_rewrite_reports_divider_and_preserves_repeated_user(self):
        first, _ = self.call([user('Continue')])
        self.finish(first, [assistant('Old answer')])
        _, start = self.call([user('Continue again')])
        self.assertEqual(start['track']['index'], 1)
        self.assertEqual(start['track']['rewritten'], 2)
        self.assertEqual(len(start['new_messages']), 1)
        # A compacted prompt that repeats an earlier user prompt is still shown.
        _, start = self.call([user('Continue')])
        self.assertEqual(len(start['new_messages']), 1)

    def test_previous_response_id_appends_even_identical_prompt(self):
        first, _ = self.call([user('Continue')], response_id='response-1')
        self.finish(first, [assistant('Working')])
        second, start = self.call([user('Continue')], response_id='response-2', previous_response_id='response-1')
        self.assertEqual(len(start['new_messages']), 1)
        self.assertEqual(start['track']['rewritten'], 0)
        self.assertEqual(len(second.track.hashes), 3)
        self.finish(second, [tool('chain-tool')])
        _, start = self.call([result('chain-tool')], previous_response_id='response-2')
        self.assertEqual(start['track']['index'], 1)
        self.assertEqual(len(start['new_messages']), 1)
        self.assertEqual(start['track']['rewritten'], 0)

    def test_slow_response_cannot_append_to_newer_request_history(self):
        first, _ = self.call([user('First')])
        second, _ = self.call([user('First'), user('New request')])
        before = list(second.track.hashes)
        self.finish(first, [assistant('Stale response')])
        self.assertEqual(second.track.hashes, before)

    def test_same_header_session_id_is_isolated_by_provider(self):
        codex, _ = self.call([user('Hello')])
        body = {'model': 'test', 'max_tokens': 100, 'tools': [{'name': 'shell'}],
                'system': 'stable instructions', 'messages': [{'role': 'user', 'content': 'Hello'}]}
        claude = proxy.CallTap('POST', '/v1/messages', {'X-Claude-Code-Session-Id': 'history-session'}, json.dumps(body).encode())
        claude.start()
        self.assertFalse(claude.broken)
        self.assertIsNot(codex.track, claude.track)
        self.assertIn('codex:history-session', proxy.DASH.sessions)
        self.assertIn('claude:history-session', proxy.DASH.sessions)
        events = [json.loads(e[3]) for e in proxy.DASH.events if e[2] == 'call_start']
        self.assertEqual(events[-1]['session_id'], events[-2]['session_id'])
        self.assertEqual(len(events[-1]['new_messages']), 1)

    def test_claude_still_matches_regrouped_assistant_reply(self):
        dash = proxy.DASH
        messages = [{'role': 'user', 'content': 'Hi'}]
        track, _, _ = dash.assign('claude', messages, 'system')
        dash.add_reply(track, proxy.fingerprint({'role': 'assistant', 'content': 'Hello'}))
        rewritten_reply = {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Hello'}, {'type': 'text', 'text': '!'}]}
        _, start, info = dash.assign('claude', [*messages, rewritten_reply, {'role': 'user', 'content': 'Next'}], 'system')
        self.assertEqual(start, 2)
        self.assertEqual(info['rewritten'], 0)


if __name__ == '__main__':
    unittest.main()
