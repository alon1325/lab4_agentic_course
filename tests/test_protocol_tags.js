const assert = require('node:assert/strict');
const {stripAgentTags} = require('../protocol_tags.js');
assert.equal(stripAgentTags('<user_request>Hello</user_request>'), 'Hello');
assert.equal(stripAgentTags('<final_answer>Hello</final_answer>'), 'Hello');
assert.equal(stripAgentTags('<USER_MESSAGE mode="text">Hi</USER_MESSAGE>'), 'Hi');
assert.equal(stripAgentTags('Before <assistant_response>inside</assistant_response> after'), 'Before inside after');
assert.equal(stripAgentTags('Hello </user_req'), 'Hello ');
assert.equal(stripAgentTags('<user_request mode="'), '');
assert.equal(stripAgentTags('<user_requ'), '');
assert.equal(stripAgentTags(null), '');
for (const sample of [
  '<div>Hello</div>', '<widget>value</widget>', 'a < b', '<u',
  '`<user_request>x</user_request>`',
  '``<user_request>`x`</user_request>``',
  '```xml\n<user_request>x</user_request>\n```',
  '~~~~xml\n<user_request>x</user_request>\n~~~~',
  '```xml\n<user_request>unfinished code',
  '`<user_req',
]) assert.equal(stripAgentTags(sample), sample);
assert.equal(stripAgentTags('``<user_request>x</user_request>`` <user_request>real prompt</user_request>'), '``<user_request>x</user_request>`` real prompt');
console.log('Protocol tags: 19 regression assertions passed');
