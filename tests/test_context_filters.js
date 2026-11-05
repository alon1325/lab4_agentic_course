'use strict';
const assert = require('node:assert/strict');
const {splitAgentContext} = require('../context_filters.js');

const mixed = splitAgentContext('Please fix this.\n<environment_context>cwd /private\n<shell>bash</shell></environment_context>\nThanks');
assert.equal(mixed.text, 'Please fix this.\n\nThanks');
assert.deepEqual(mixed.contexts, [{tag: 'environment_context', text: 'cwd /private\n<shell>bash</shell>'}]);
assert.equal(splitAgentContext('<environment_context>secret').text, '');
assert.equal(splitAgentContext('Hi <environment_con').text, 'Hi');
assert.equal(splitAgentContext('Hi <environment_context source="').text, 'Hi');
assert.equal(splitAgentContext('<environment_context/>Hello').text, 'Hello');
assert.equal(splitAgentContext('<system-reminder>private</system-reminder>Prompt').text, 'Prompt');
assert.equal(splitAgentContext('<developer><environment_context>private</environment_context>more</developer>Prompt').text, 'Prompt');
assert.equal(splitAgentContext('Prompt</environment_context>').text, 'Prompt');
assert.equal(splitAgentContext('<environment_context>one</environment_context><system>two</system>').contexts.length, 2);
assert.equal(splitAgentContext('<div>keep me</div>').text, '<div>keep me</div>');
for (const example of [
  '`<environment_context>example</environment_context>`',
  '```xml\n<environment_context>example</environment_context>\n```',
  '~~~xml\n<system-reminder>example</system-reminder>\n~~~',
  '``literal `<environment_context>` example``',
]) assert.equal(splitAgentContext(example).text, example);
assert.equal(splitAgentContext('`example` <environment_context>secret</environment_context>Prompt').text, '`example` Prompt');
assert.equal(splitAgentContext('```\n<environment_context>example</environment_context>\n```\n<environment_context>secret</environment_context>Prompt').text, '```\n<environment_context>example</environment_context>\n```\nPrompt');
assert.equal(splitAgentContext(null).text, '');
console.log('Context filters: 18 regression assertions passed');
