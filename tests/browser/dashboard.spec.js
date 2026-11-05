const { test, expect } = require('@playwright/test');

function start(id, provider = 'codex', messages = []) {
  return { type: 'call_start', call_id: id, session_id: `${provider}-session`, time: Date.now(),
    method: 'POST', path: provider === 'codex' ? '/responses' : '/v1/messages',
    provider, model: provider === 'codex' ? 'codex-test-model' : 'claude-test-model', kind: 'main',
    tools: ['shell'], system_summary: {length: 25, first_line: 'private system setup'},
    new_messages: messages, track: {index: 1, label: 'main', total: 1} };
}
const stop = id => ({type: 'call_end', call_id: id, status: 200, model: 'test-model', stop_reason: 'completed', usage: {input: 3, output: 2}, duration_ms: 20});
const send = (request, events) => request.post('/test/events', {data: events});

test.beforeEach(async ({page, request}) => {
  await request.post('/test/reset');
  await page.goto('/');
  await expect(page.locator('#statustext')).toHaveText('live');
});

test('context stays hidden from display, picker, and search until explicitly revealed', async ({page, request}) => {
  await send(request, [start('context', 'codex', [
    {role: 'user', content: '<environment_context>private workspace</environment_context>\n<user_request>Ship the dashboard</user_request>\n<system-reminder>secret injected note</system-reminder>'},
    {role: 'developer', content: 'secret developer instructions'},
  ]), stop('context')]);
  await expect(page.locator('.k-user .body')).toHaveText('Ship the dashboard');
  await expect(page.locator('.k-inj').first()).toBeHidden();
  await expect(page.locator('.setup')).toBeHidden();
  await expect(page.locator('#session')).not.toContainText('private workspace');
  await page.locator('#search').fill('secret');
  await expect(page.locator('#matches')).toHaveText('0 matches');
  await page.locator('#f-inj').check();
  await expect(page.locator('#matches')).toHaveText('2 matches');
  await page.locator('#search').fill('');
  await expect(page.locator('.k-inj').first()).toBeVisible();
  await page.locator('.k-inj summary').first().click();
  await expect(page.locator('.k-inj pre').first()).toContainText('private workspace');
  await page.getByRole('button', {name: 'raw', exact: true}).click();
  await expect(page.locator('.raw')).toContainText('<environment_context>');
});

test('reply streams live, preserves source code tags, and pairs tools with results', async ({page, request}) => {
  await send(request, [start('stream', 'codex', [{role: 'user', content: 'Run a tool'}]),
    {type: 'block_start', call_id: 'stream', index: 0, block_type: 'text'},
    {type: 'block_delta', call_id: 'stream', index: 0, text: 'Working live'},
  ]);
  await expect(page.locator('.k-model .body')).toContainText('Working live');
  await expect(page.locator('.cursor')).toBeVisible();
  await send(request, [{type: 'block_delta', call_id: 'stream', index: 0, text: '\n```xml\n<user_request>example</user_request>\n```'},
    {type: 'block_stop', call_id: 'stream', index: 0, text: 'Working live\n```xml\n<user_request>example</user_request>\n```'},
    {type: 'block_start', call_id: 'stream', index: 1, block_type: 'tool_use', tool_name: 'shell', tool_use_id: 'tool-1'},
    {type: 'block_delta', call_id: 'stream', index: 1, partial_json: '{"command":"pwd"}'},
    {type: 'block_stop', call_id: 'stream', index: 1, input: {command: 'pwd'}}, stop('stream'),
    start('result', 'codex', [{role: 'user', content: [{type: 'tool_result', tool_use_id: 'tool-1', content: '/workspace/result'}]}]), stop('result'),
  ]);
  await expect(page.locator('.k-model pre')).toHaveText('<user_request>example</user_request>');
  await expect(page.locator('.cursor')).toHaveCount(0);
  await expect(page.locator('.k-tool .results .result')).toContainText('/workspace/result');
});

test('EventSource reconnect resumes without duplicate messages', async ({page, request}) => {
  await send(request, [start('first', 'codex', [{role: 'user', content: 'Only once'}]), stop('first')]);
  await expect(page.locator('.k-user .body')).toHaveText('Only once');
  await request.post('/test/disconnect');
  await expect(page.locator('#statustext')).toHaveText('reconnecting…');
  await expect(page.locator('#statustext')).toHaveText('live', {timeout: 10000});
  await send(request, [start('second', 'codex', [{role: 'user', content: 'After reconnect'}]), stop('second')]);
  await expect(page.locator('.k-user .body')).toHaveCount(2);
  await expect(page.getByText('Only once', {exact: true})).toHaveCount(1);
});

test('provider filter selects Codex or Claude and exposes connection details', async ({page, request}) => {
  await expect(page.locator('#launch-command')).toContainText('--proxy-port 8051');
  await send(request, [start('codex-call', 'codex', [{role: 'user', content: 'Codex prompt'}]), stop('codex-call'),
    start('claude-call', 'claude', [{role: 'user', content: 'Claude prompt'}]), stop('claude-call')]);
  await page.locator('#provider').selectOption('codex');
  await expect(page.getByText('Codex prompt', {exact: true})).toBeVisible();
  await expect(page.locator('#launch-command')).toHaveText('./codex-proxy --proxy-port 8051');
  await expect(page.getByText('Claude prompt', {exact: true})).toBeHidden();
  await page.locator('#provider').selectOption('claude');
  await expect(page.getByText('Claude prompt', {exact: true})).toBeVisible();
  await expect(page.locator('#launch-command')).toHaveText('./claude-proxy --proxy-port 8051');
  await expect(page.getByText('Codex prompt', {exact: true})).toBeHidden();
});

test('upstream error is visible and remains searchable', async ({page, request}) => {
  await send(request, [start('error', 'claude', [{role: 'user', content: 'Try a reply'}]),
    {...stop('error'), status: 429, error: 'rate_limit_error: capacity exhausted'}]);
  await expect(page.locator('.k-error')).toContainText('capacity exhausted');
  await expect(page.locator('#connection-detail')).toContainText('Rate limit');
  await page.locator('#search').fill('capacity exhausted');
  await expect(page.locator('#matches')).toHaveText('1 match');
  await expect(page.locator('.k-error')).toBeVisible();
});

for (const [status, message] of [[401, 'Sign in again'], [502, 'Cannot reach upstream']]) {
  test(`HTTP ${status} shows an actionable recovery banner`, async ({page, request}) => {
    await send(request, [start(`failure-${status}`), {...stop(`failure-${status}`), status, error: `HTTP ${status}`}]);
    await expect(page.locator('#connection-detail')).toContainText(message);
    await expect(page.locator('#connection-detail')).toBeVisible();
  });
}

test('proxy unavailability gives a startup instruction and clears on reconnect', async ({page, request}) => {
  await page.route('**/health', route => route.abort('connectionrefused'));
  await request.post('/test/disconnect');
  await expect(page.locator('#connection-detail')).toContainText('Proxy unavailable');
  await expect(page.locator('#connection-detail')).toContainText('./start');
  await page.unroute('**/health');
  await expect(page.locator('#statustext')).toHaveText('live', {timeout: 10000});
  await expect(page.locator('#connection-detail')).toBeHidden();
});

test('a successful call clears the prior upstream recovery banner', async ({page, request}) => {
  await send(request, [start('failed-call'), {...stop('failed-call'), status: 401, error: 'HTTP 401'}]);
  await expect(page.locator('#connection-detail')).toContainText('Sign in again');
  await send(request, [start('recovered-call'), stop('recovered-call')]);
  await expect(page.locator('#connection-detail')).toBeHidden();
});
