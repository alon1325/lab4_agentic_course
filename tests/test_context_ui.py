"""Execute the dashboard's context routing/search functions with minimal DOM stubs."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]



class ContextUITests(unittest.TestCase):
    def test_context_default_is_hidden(self):
        text = (ROOT / 'proxy.py').read_text()
        self.assertIn('<body class="hide-side hide-injected">', text)
        self.assertIn('id="f-inj"> show context', text)
        self.assertIn('body.hide-injected .setup{display:none}', text)
        self.assertNotIn('${ev.system_summary.first_line.slice(0, 80)}', text)

    @unittest.skipUnless(shutil.which('node'), 'Node required to exercise dashboard JavaScript')
    def test_hidden_context_search_and_role_routing(self):
        source = (ROOT / 'proxy.py').read_text()
        render = source[source.index('function renderMessage('):source.index('/* ---------- raw view')]
        search = source[source.index('function searchNode('):source.index('function applySearch(')]
        script = r'''
const assert = require('node:assert/strict');
let hide = true;
const S = {q: 'secret'};
const document = {body: {classList: {contains: () => hide}}};
function node(classes, text) {
  const values = new Set(classes);
  return {textContent: text, classList: {contains: c => values.has(c), toggle: (c, on) => on ? values.add(c) : values.delete(c)}};
}
const captured = [];
const injected = (call, role, text) => captured.push({role, text});
const userText = () => { throw Error('context entered user rendering'); };
const modelText = () => { throw Error('context entered model rendering'); };
'''+ render + search + r'''
const hidden = node(['entry', 'k-inj'], 'secret injected instructions');
searchNode(hidden);
assert(hidden.classList.contains('miss'));
hide = false;
searchNode(hidden);
assert(!hidden.classList.contains('miss'));
for (const role of ['system', 'developer']) renderMessage({}, {role, content: [{type: 'text', text: 'secret'}]});
assert.deepEqual(captured, [{role: 'system', text: 'secret'}, {role: 'developer', text: 'secret'}]);
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
