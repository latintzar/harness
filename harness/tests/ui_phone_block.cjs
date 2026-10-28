/* Browser proofs for a Tracking Agent's phone block and its contact card (CHANNELS.md, "HTTP interface").
 * Every API answer is fixture-owned; no provider, model or network call leaves the page.
 * NODE_PATH=/path/to/playwright/node_modules PLAYWRIGHT_CHROMIUM_EXECUTABLE=/path/to/chromium \
 *   node --test harness/tests/ui_phone_block.cjs
 * ROCOTO_PHONE_SCREENSHOTS names the folder for the 1600x1000 and 390x844 captures.
 */
const { test, before, after } = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const { chromium } = require('playwright');

const base = path.resolve(__dirname, '..'), web = process.env.ROCOTO_MESSAGING_TEST_WEB || require('./web_fixture.cjs').web();
const theme = JSON.parse(fs.readFileSync(path.join(base, 'theme.json')));
const shots = process.env.ROCOTO_PHONE_SCREENSHOTS || '/tmp/rocoto-phone-ui';
const PHONE = '/api/agents/agent-4f0c/phone';
// The copy law, as tools/copy-gate.py reads it.
const NEGATIVE = /(^|[\s>«(·'"`])(no|not|nothing|never|nobody|none|don't|doesn't|can't|cannot|won't|isn't|aren't|didn't|wasn't|hasn't|haven't|without|unless)\b/i;
let server, browser, origin;

before(async () => {
  server = http.createServer((req, res) => {
    const route = new URL(req.url, 'http://localhost').pathname;
    if (route === '/') {
      res.setHeader('content-type', 'text/html');
      res.end('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/theme.css"><link rel="stylesheet" href="/messaging-settings.css"><script defer src="/messaging-settings.js"></script></head><body><button id="opener">Before the block</button><main id="root"></main></body></html>');
      return;
    }
    if (route === '/theme.css' || route === '/static/harness/theme.css') {
      res.setHeader('content-type', 'text/css');
      res.end(':root{' + Object.entries({ ...theme.palette, ...theme.typography, radius: '12px', 'radius-sm': '8px', 'radius-lg': '20px' }).map(([k, v]) => '--' + k + ':' + v + ';').join('') + '}*{box-sizing:border-box}body{margin:0;padding:36px 24px;background:var(--surface);font:var(--font-size-ui)/1.5 var(--font);color:var(--ink)}button,input,select{font:inherit}button{cursor:pointer}');
      return;
    }
    const file = path.resolve(web, '.' + route);
    if (!file.startsWith(web + path.sep) || !fs.existsSync(file)) { res.writeHead(404); res.end(); return; }
    res.setHeader('content-type', route.endsWith('.js') ? 'text/javascript' : route.endsWith('.png') ? 'image/png' : 'text/css');
    fs.createReadStream(file).pipe(res);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  origin = 'http://127.0.0.1:' + server.address().port;
  browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : {}), args: ['--no-sandbox'] });
});
after(async () => { await browser?.close(); await new Promise(resolve => server?.close(resolve)); });

/* The sample phone block of CHANNELS.md, seen by its creator. */
function sample(change = {}) {
  return {
    agent_id: 'agent-4f0c', chat_id: 'chat-91ab', collection: 'retail-demo', generation: 4, delivery: 'on', can_manage: true, read_only: false,
    channels: [
      { channel: 'viber', label: 'Viber', ready: false, reason: 'provider_setup_required', sender: null },
      { channel: 'sms', label: 'SMS', ready: false, reason: 'provider_setup_required', sender: null },
      { channel: 'whatsapp', label: 'WhatsApp', ready: true, reason: null, sender: '+12025550123' },
    ],
    participants: [
      { member_id: 'member-01', name: 'Maria Ivanova', role: 'creator', is_self: true, channel: 'whatsapp', channel_choice: 'automatic', verified_channels: ['whatsapp'], address_masked: '+359•••••••56', verification: 'verified', enabled: true,
        last_delivery: { id: 'dlv-8cf7835a1323e7c5b4bcee0a0f60bdda', channel: 'whatsapp', status: 'delivered', held_reason: null, updated_at: 1791626400.0 }, contact_request: null },
      { member_id: 'member-02', name: 'Ivan Petrov', role: 'added', is_self: false, channel: null, channel_choice: 'automatic', verified_channels: [], address_masked: null, verification: 'number_required', enabled: true, last_delivery: null,
        contact_request: { id: 'contact-7a1e0c9b5d3f4e2a8b6c4d2e0f1a3b5c', status: 'open', created_at: 1791626000.0, expires_at: 1792230800.0 } },
    ],
    available_members: [{ member_id: 'member-03', name: 'Elena Georgieva', verified: true }],
    spend: { state: 'open', agent: { used: 3, cap: 50 }, company: { used: 12, cap: 200 }, resets_at: 1791676800.0 },
    ...change,
  };
}
/* The same agent seen by Ivan Petrov, an added participant who still needs a number. */
function asIvan(change = {}) {
  const block = sample({ can_manage: false, available_members: [] });
  block.participants[0].is_self = false; block.participants[1].is_self = true; block.participants[1].contact_request = null;
  return Object.assign(block, change);
}

/* A fixture host that applies the contract's requests to one block and records every call. */
async function host(page, block, { intercept, messaging } = {}) {
  const calls = [], member = id => block.participants.find(item => item.member_id === id);
  const numbers = { ready: true, otp_ready: true, hello_ready: true, sender_phone: '+12025550123' };
  await page.route('**/api/**', route => {
    const request = route.request(), url = new URL(request.url()), where = url.pathname, method = request.method(), body = method === 'POST' ? request.postDataJSON() : null;
    calls.push({ method, path: where, query: url.search, body, demo: request.headers()['x-rocoto-page'] || null });
    const json = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) });
    const handled = intercept?.({ method, path: where, body, json, route, block });
    if (handled) return handled;
    if (where === PHONE && method === 'GET') return json(block);
    if (where.startsWith(PHONE + '/') && method === 'POST') {
      const rest = where.slice(PHONE.length + 1).split('/');
      if (rest[0] === 'contact-request') {
        member(body.member_id).contact_request = { id: 'contact-' + '0'.repeat(32), status: 'open', created_at: 1791626500.0, expires_at: 1792231300.0 };
        block.generation += 1; return json(block);
      }
      if (body.expected_generation !== block.generation) return json({ detail: 'Refresh the phone alerts and repeat your change.' }, 409);
      if (rest[0] === 'delivery') block.delivery = body.state;
      else if (rest[0] === 'participants' && rest.length === 1) {
        const added = block.available_members.find(item => item.member_id === body.member_id);
        block.available_members = block.available_members.filter(item => item !== added);
        block.participants.push({ member_id: added.member_id, name: added.name, role: 'added', is_self: false, channel: added.verified ? 'whatsapp' : null, channel_choice: 'automatic', verified_channels: added.verified ? ['whatsapp'] : [], address_masked: added.verified ? '+359•••••••12' : null, verification: added.verified ? 'verified' : 'number_required', enabled: true, last_delivery: null, contact_request: null });
      } else if (rest[0] === 'participants' && rest[2] === 'remove') block.participants = block.participants.filter(item => item.member_id !== rest[1]);
      else if (rest[0] === 'participants') {
        const person = member(rest[1]);
        if (Object.hasOwn(body, 'enabled')) person.enabled = body.enabled;
        if (Object.hasOwn(body, 'channel')) Object.assign(person, body.channel === 'automatic' ? { channel_choice: 'automatic' } : { channel_choice: 'picked', channel: body.channel });
      }
      block.generation += 1; return json(block);
    }
    if (where === '/api/messaging' && method === 'GET') return json({ provider: 'whatsapp', readiness: { provider: 'whatsapp', configured: true, reason: null, webhook_ready: true, ...numbers }, contact: null, challenge: null, outbox: [], waiting: false, contacts: [], collection: url.searchParams.get('collection'), member_id: url.searchParams.get('member_id'), setup_chat_id: 'setup-1', setup_chat_url: '/?chat=setup-1', members: [], read_only: false, channels: block.channels, ...messaging });
    if (where === '/api/messaging/verification' && method === 'POST') return json({ id: 'challenge-1', member_id: body.member_id, phone: body.phone, status: 'sent', expires_at: Math.floor(Date.now() / 1000) + 300, resend_after: 60, attempts_remaining: 5, setup_chat_id: 'setup-1' });
    if (where === '/api/messaging/verification/confirm' && method === 'POST') {
      const person = member(body.member_id), channel = block.verifying || 'whatsapp';
      Object.assign(person, { verification: 'verified', channel, verified_channels: [channel], address_masked: '+359•••••••56', contact_request: null }); block.generation += 1;
      return json({ member_id: body.member_id, phone: '+359888123456', verified: true, verified_at: 1791626600.0, generation: 1, setup_chat_id: 'setup-1', collection: body.collection });
    }
    return json({}, 404);
  });
  return calls;
}
async function pageFor(width = 1600) {
  const context = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 1000 } }), page = await context.newPage(), errors = [];
  page.setDefaultTimeout(3000); page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', route => route.request().url().startsWith(origin + '/') ? route.fallback() : route.abort());
  return { page, errors, close: async () => { assert.deepEqual(errors, []); await context.close(); } };
}
async function mount(page, options = {}) {
  await page.goto(origin);
  await page.waitForFunction(() => window.RocotoMessaging);
  await page.evaluate(options => { window.phone = window.RocotoMessaging.mountPhone({ agentId: 'agent-4f0c', agentTitle: 'Queue watch', ...options }); document.querySelector('#root').append(window.phone.element); }, options);
  await page.locator('.phone-block[aria-busy="false"]').waitFor();
}
const posts = calls => calls.filter(call => call.method === 'POST');
const row = (page, name) => page.locator('.phone-participant', { hasText: name });

test('the module exposes the phone block beside the member profile', async () => {
  const { page, close } = await pageFor();
  try {
    await host(page, sample()); await page.goto(origin); await page.waitForFunction(() => window.RocotoMessaging);
    assert.deepEqual(await page.evaluate(() => Object.keys(window.RocotoMessaging).sort()), ['channelLabel', 'chipLabel', 'deliveryLabel', 'deliveryText', 'mountPhone', 'openContactCard', 'openMember', 'render', 'senderLabel']);
  } finally { await close(); }
});

test('the phone block names the thing and shows each participant from host records', async () => {
  const { page, close } = await pageFor();
  try {
    const calls = await host(page, sample()); await mount(page);
    assert.equal(await page.getByRole('heading', { level: 3, name: 'Alerts on your phone' }).count(), 1);
    assert.equal(await page.getByRole('region', { name: 'Alerts on your phone' }).count(), 1);
    const maria = await row(page, 'Maria Ivanova').innerText(), ivan = await row(page, 'Ivan Petrov').innerText(), all = await page.locator('.phone-block').innerText();
    assert.match(maria, /Creator · You/); assert.match(maria, /WhatsApp · \+359•••••••56/); assert.match(maria, /WhatsApp · Delivered · /);
    assert.doesNotMatch(maria, /359888/, 'the block shows the masked number only');
    assert.match(ivan, /Number requested/); assert.doesNotMatch(ivan, /Creator|You/);
    assert.match(all, /WhatsApp is ready\. Viber and SMS are being connected\./);
    assert.match(all, /3 of 50 messages sent today/);
    assert.equal(await page.getByRole('switch', { name: 'Phone alerts for this agent' }).isChecked(), true);
    assert.equal(await page.getByRole('switch', { name: 'Alerts for Maria Ivanova' }).isChecked(), true);
    assert.deepEqual(calls.map(call => [call.method, call.path]), [['GET', PHONE]]);
  } finally { await close(); }
});

test('the creator pauses the agent and switches participants inside the field rules', async () => {
  const { page, close } = await pageFor();
  try {
    const calls = await host(page, sample()); await mount(page);
    await page.getByRole('switch', { name: 'Phone alerts for this agent' }).click();
    await page.locator('.phone-delivery', { hasText: 'Phone alerts paused' }).waitFor();
    await page.getByRole('switch', { name: 'Alerts for Maria Ivanova' }).click();
    await row(page, 'Maria Ivanova').getByText('Alerts off').waitFor();
    await page.getByRole('switch', { name: 'Alerts for Ivan Petrov' }).click();
    await row(page, 'Ivan Petrov').getByText('Alerts off').waitFor();
    assert.equal(await page.getByRole('switch', { name: 'Alerts for Ivan Petrov' }).isDisabled(), true, 'each participant switches their own alerts on');
    assert.equal(await page.getByRole('switch', { name: 'Alerts for Maria Ivanova' }).isDisabled(), false);
    assert.deepEqual(posts(calls).map(call => [call.path, call.body, call.demo]), [
      [PHONE + '/delivery', { state: 'paused', expected_generation: 4 }, '1'],
      [PHONE + '/participants/member-01', { enabled: false, expected_generation: 5 }, '1'],
      [PHONE + '/participants/member-02', { enabled: false, expected_generation: 6 }, '1'],
    ]);
  } finally { await close(); }
});

test('the creator adds a company member and removes an added participant', async () => {
  const { page, close } = await pageFor();
  try {
    const calls = await host(page, sample()); await mount(page);
    assert.deepEqual(await page.getByLabel('Add a participant').locator('option').allInnerTexts(), ['Elena Georgieva']);
    await page.getByRole('button', { name: 'Add', exact: true }).click();
    await row(page, 'Elena Georgieva').waitFor();
    assert.equal(await page.getByLabel('Add a participant').count(), 0, 'every available member is a participant now');
    assert.equal(await row(page, 'Maria Ivanova').getByRole('button', { name: /Remove|Leave/ }).count(), 0, 'the creator stays a participant');
    await page.getByRole('button', { name: 'Remove Ivan Petrov' }).click();
    await row(page, 'Ivan Petrov').waitFor({ state: 'detached' });
    assert.deepEqual(posts(calls).map(call => [call.path, call.body]), [
      [PHONE + '/participants', { member_id: 'member-03', expected_generation: 4 }],
      [PHONE + '/participants/member-02/remove', { expected_generation: 5 }],
    ]);
  } finally { await close(); }
});

test('each participant picks their own channel among their verified ready channels', async () => {
  const { page, close } = await pageFor();
  try {
    const block = sample(); block.channels[1] = { channel: 'sms', label: 'SMS', ready: true, reason: null, sender: '+359882000111' }; block.participants[0].verified_channels = ['sms', 'whatsapp']; block.participants[0].channel = 'sms';
    const calls = await host(page, block); await mount(page);
    const choice = page.getByLabel('Channel for Maria Ivanova');
    assert.equal(await choice.inputValue(), 'automatic');
    assert.deepEqual(await choice.locator('option').evaluateAll(options => options.map(option => [option.value, option.textContent, option.disabled])), [['automatic', 'Automatic', false], ['viber', 'Viber · being connected', true], ['sms', 'SMS', false], ['whatsapp', 'WhatsApp', false]]);
    assert.match(await row(page, 'Maria Ivanova').innerText(), /SMS · \+359•••••••56/);
    await choice.selectOption('whatsapp');
    await row(page, 'Maria Ivanova').getByText('WhatsApp · +359•••••••56').waitFor();
    assert.equal(await page.getByLabel('Channel for Maria Ivanova').inputValue(), 'whatsapp');
    assert.equal(await page.getByLabel('Channel for Ivan Petrov').count(), 0);
    assert.deepEqual(posts(calls).map(call => [call.path, call.body]), [[PHONE + '/participants/member-01', { channel: 'whatsapp', expected_generation: 4 }]]);
  } finally { await close(); }
});

test('a participant who needs a number completes the contact card on the built verification routes', async () => {
  const { page, close } = await pageFor();
  try {
    const calls = await host(page, asIvan()); await mount(page);
    assert.match(await row(page, 'Ivan Petrov').innerText(), /Add a number to get alerts on your phone\./);
    await page.getByRole('button', { name: 'Add a number' }).click();
    const card = page.getByRole('dialog', { name: 'Ivan Petrov' }); await card.waitFor();
    assert.match(await card.innerText(), /Alerts from Queue watch/);
    assert.deepEqual(await card.getByRole('radio').evaluateAll(radios => radios.map(radio => [radio.value, radio.checked, radio.disabled, radio.closest('label').innerText.replace(/\s+/g, ' ').trim()])), [['viber', false, true, 'Viber Being connected'], ['sms', false, true, 'SMS Being connected'], ['whatsapp', true, false, 'WhatsApp Ready']]);
    assert.equal(await card.getByLabel('Country code').evaluate(el => el === document.activeElement), true);
    await card.getByLabel('Country code').fill('+359'); await card.getByLabel('Phone number', { exact: true }).fill('888 123 456');
    await card.getByRole('button', { name: 'Send security code' }).click();
    await card.getByLabel('Security code').fill('123456'); await card.getByRole('button', { name: 'Verify number' }).click();
    await card.getByText('Your WhatsApp number is verified.').waitFor();
    assert.match(await card.innerText(), /Alerts from Queue watch arrive on this number\. Your replies continue the same chat\./);
    await card.getByRole('button', { name: 'Done' }).click();
    await row(page, 'Ivan Petrov').getByText('WhatsApp · +359•••••••56').waitFor();
    assert.equal(await page.locator('dialog').count(), 0);
    assert.equal(await page.evaluate(() => document.querySelector('.phone-block').contains(document.activeElement)), true, 'focus returns to the phone block');
    assert.deepEqual(posts(calls).map(call => [call.path, call.body, call.demo]), [
      [PHONE + '/contact-request', { member_id: 'member-02' }, '1'],
      ['/api/messaging/verification', { phone: '+359888123456', member_id: 'member-02', collection: 'retail-demo' }, '1'],
      ['/api/messaging/verification/confirm', { challenge_id: 'challenge-1', code: '123456', member_id: 'member-02', collection: 'retail-demo' }, '1'],
    ]);
    assert.ok(calls.some(call => call.method === 'GET' && call.path === '/api/messaging' && call.query === '?member_id=member-02&collection=retail-demo'));
  } finally { await close(); }
});

test('a ready SMS channel carries its own name through the card and the verification request', async () => {
  const { page, close } = await pageFor();
  try {
    const block = asIvan(); block.channels[1] = { channel: 'sms', label: 'SMS', ready: true, reason: null, sender: '+359882000111' }; block.verifying = 'sms';
    block.participants[1].contact_request = { id: 'contact-' + '1'.repeat(32), status: 'open', created_at: 1791626000.0, expires_at: 1792230800.0 };
    const calls = await host(page, block); await mount(page);
    await page.getByRole('button', { name: 'Add a number' }).click();
    const card = page.getByRole('dialog', { name: 'Ivan Petrov' }); await card.waitFor();
    assert.equal(await card.getByRole('radio', { name: /SMS/ }).isChecked(), true, 'the first ready channel in channel order is in use');
    assert.match(await card.innerText(), /Rocoto sends a security code by SMS\./);
    await card.getByRole('radio', { name: /WhatsApp/ }).focus(); await page.keyboard.press('ArrowUp');
    assert.equal(await card.getByRole('radio', { name: /SMS/ }).isChecked(), true, 'arrow keys move between the ready channels');
    await card.getByLabel('Country code').fill('+359'); await card.getByLabel('Phone number', { exact: true }).fill('888123456');
    await card.getByRole('button', { name: 'Send security code' }).click();
    await card.getByText('Enter the security code sent to +359888123456 by SMS.').waitFor();
    await card.getByLabel('Security code').fill('123456'); await card.getByRole('button', { name: 'Verify number' }).click();
    await card.getByText('Your SMS number is verified.').waitFor();
    assert.deepEqual(posts(calls).map(call => [call.path, call.body]), [
      ['/api/messaging/verification', { phone: '+359888123456', member_id: 'member-02', channel: 'sms', collection: 'retail-demo' }],
      ['/api/messaging/verification/confirm', { challenge_id: 'challenge-1', code: '123456', member_id: 'member-02', collection: 'retail-demo' }],
    ], 'an open request is reused and the channel travels with the number');
  } finally { await close(); }
});

test('the creator asks another participant for a number and the card stays with that member', async () => {
  const { page, close } = await pageFor();
  try {
    const block = sample(); block.participants[1].contact_request = null;
    const calls = await host(page, block); await mount(page);
    assert.match(await row(page, 'Ivan Petrov').innerText(), /Waiting for a verified number/);
    await page.getByRole('button', { name: 'Ask Ivan Petrov for a number' }).click();
    await row(page, 'Ivan Petrov').getByText(/Number requested/).waitFor();
    assert.equal(await page.locator('dialog').count(), 0);
    assert.equal(await page.getByRole('button', { name: 'Ask Ivan Petrov for a number' }).count(), 0);
    assert.deepEqual(posts(calls).map(call => [call.path, call.body]), [[PHONE + '/contact-request', { member_id: 'member-02' }]]);
  } finally { await close(); }
});

test('an added participant manages their own alerts and confirms before leaving', async () => {
  const { page, close } = await pageFor();
  try {
    const block = asIvan(); Object.assign(block.participants[1], { verification: 'verified', channel: 'whatsapp', verified_channels: ['whatsapp'], address_masked: '+359•••••••78' });
    const calls = await host(page, block); await mount(page);
    assert.equal(await page.getByRole('switch', { name: 'Phone alerts for this agent' }).count(), 0);
    assert.equal(await page.locator('.phone-delivery').innerText(), 'Phone alerts on');
    assert.equal(await page.getByLabel('Add a participant').count(), 0);
    assert.equal(await page.getByRole('switch', { name: 'Alerts for Maria Ivanova' }).isDisabled(), true);
    assert.equal(await page.getByRole('button', { name: /Remove/ }).count(), 0);
    await page.getByRole('switch', { name: 'Alerts for Ivan Petrov' }).click();
    await row(page, 'Ivan Petrov').getByText('Alerts off').waitFor();
    await page.getByRole('switch', { name: 'Alerts for Ivan Petrov' }).click();
    await row(page, 'Ivan Petrov').getByText('Alerts on').waitFor();
    await page.getByRole('button', { name: 'Leave these phone alerts' }).click();
    assert.equal(posts(calls).length, 2, 'the first press asks for confirmation');
    const confirm = page.getByRole('button', { name: 'Confirm leaving these phone alerts' });
    assert.equal(await confirm.evaluate(el => el === document.activeElement), true);
    await confirm.click(); await row(page, 'Ivan Petrov').waitFor({ state: 'detached' });
    assert.deepEqual(posts(calls).map(call => [call.path, call.body]), [
      [PHONE + '/participants/member-02', { enabled: false, expected_generation: 4 }],
      [PHONE + '/participants/member-02', { enabled: true, expected_generation: 5 }],
      [PHONE + '/participants/member-02/remove', { expected_generation: 6 }],
    ]);
  } finally { await close(); }
});

test('an observation session reads the phone block with every control at rest', async () => {
  const { page, close } = await pageFor();
  try {
    const calls = await host(page, sample({ read_only: true })); await mount(page);
    const controls = page.locator('.phone-block').locator('input,select,button');
    assert.ok(await controls.count() >= 3);
    assert.deepEqual(await controls.evaluateAll(items => items.filter(item => !item.disabled).map(item => item.getAttribute('aria-label') || item.textContent)), []);
    assert.equal(await page.getByLabel('Add a participant').count(), 0);
    assert.equal(posts(calls).length, 0);
  } finally { await close(); }
});

test('a stale generation shows the host sentence and reloads the block', async () => {
  const { page, close } = await pageFor();
  try {
    const block = sample();
    const calls = await host(page, block, { intercept: ({ method, path: where, json }) => method === 'POST' && where === PHONE + '/delivery' ? (block.participants[0].enabled = false, block.generation = 9, json({ detail: 'Refresh the phone alerts and repeat your change.' }, 409)) : null });
    await mount(page);
    await page.getByRole('switch', { name: 'Phone alerts for this agent' }).click();
    assert.equal(await page.getByRole('alert').innerText(), 'Refresh the phone alerts and repeat your change.');
    assert.equal(await page.getByRole('switch', { name: 'Phone alerts for this agent' }).isChecked(), true, 'the switch shows the saved policy');
    assert.equal(await page.getByRole('switch', { name: 'Alerts for Maria Ivanova' }).isChecked(), false, 'the reloaded block is on screen');
    assert.deepEqual(calls.map(call => [call.method, call.path]), [['GET', PHONE], ['POST', PHONE + '/delivery'], ['GET', PHONE]]);
    await page.getByRole('switch', { name: 'Alerts for Maria Ivanova' }).click();
    await row(page, 'Maria Ivanova').getByText('Alerts on').waitFor();
    assert.deepEqual(posts(calls).at(-1).body, { enabled: true, expected_generation: 9 });
    assert.equal(await page.getByRole('alert').count(), 0);
  } finally { await close(); }
});

test('spend, plain-text refusals and unexpected answers stay truthful', async () => {
  const { page, close } = await pageFor();
  try {
    const block = sample({ spend: { state: 'cap_reached', agent: { used: 50, cap: 50 }, company: { used: 61, cap: 200 }, resets_at: 1791676800.0 } });
    block.participants[0].last_delivery = { id: 'dlv-' + 'a'.repeat(32), channel: null, status: 'held', held_reason: 'spend_cap', updated_at: 1791626400.0 };
    let answer = null;
    await host(page, block, { intercept: ({ method, path: where, json, route }) => method === 'GET' && where === PHONE && answer ? answer({ json, route }) : null });
    await mount(page);
    assert.match(await page.locator('.phone-spend').innerText(), /^Today’s alert limit reached\. Alerts stay in Rocoto and phone delivery starts again on /);
    assert.match(await row(page, 'Maria Ivanova').innerText(), /Today’s alert limit reached · /);
    // The block says what holds the alerts. A verified participant with no delivery yet reads that state, and "ready" waits for an open bound.
    block.participants[0].last_delivery = null;
    block.spend = { state: 'stopped', agent: { used: 3, cap: 50 }, company: { used: 12, cap: 200 }, resets_at: 1791676800.0 }; await page.evaluate(() => window.phone.refresh());
    await page.getByText('Phone alerts are paused for your company. Alerts arrive in Rocoto.').waitFor();
    assert.equal(await page.locator('.phone-delivery').innerText(), 'Phone alerts paused for your company');
    assert.equal(await row(page, 'Maria Ivanova').locator('.phone-participant-delivery').innerText(), 'Alerts paused');
    block.spend = { state: 'unavailable', agent: null, company: null, resets_at: 1791676800.0 }; await page.evaluate(() => window.phone.refresh());
    await page.getByText('Phone alerts wait for your company’s daily message limit. Alerts arrive in Rocoto.').waitFor();
    assert.equal(await page.locator('.phone-delivery').innerText(), 'Phone alerts waiting');
    assert.equal(await row(page, 'Maria Ivanova').locator('.phone-participant-delivery').innerText(), 'Waiting for the daily message limit');
    assert.equal(await page.getByText('Ready for the first alert').count(), 0);
    assert.equal(await page.getByText('being checked').count(), 0);
    block.spend = { state: 'open', agent: { used: 3, cap: 50 }, company: { used: 12, cap: 200 }, resets_at: 1791676800.0 }; block.history = 'full'; await page.evaluate(() => window.phone.refresh());
    await page.getByText('Phone delivery history is full. Alerts arrive in Rocoto.').waitFor();
    assert.equal(await page.locator('.phone-delivery').innerText(), 'Phone alerts in Rocoto');
    assert.equal(await row(page, 'Maria Ivanova').locator('.phone-participant-delivery').innerText(), 'Alert kept in Rocoto');
    block.history = 'open'; await page.evaluate(() => window.phone.refresh());
    await row(page, 'Maria Ivanova').getByText('Ready for the first alert').waitFor();
    assert.equal(await page.locator('.phone-delivery').innerText(), 'Phone alerts on');
    // A delivery that needs attention reads in ink, apart from the muted states around it.
    block.participants[0].last_delivery = { id: 'dlv-' + 'b'.repeat(32), channel: 'whatsapp', status: 'failed', held_reason: null, updated_at: 1791626400.0 }; await page.evaluate(() => window.phone.refresh());
    const attention = row(page, 'Maria Ivanova').locator('.phone-participant-delivery'); await attention.getByText('WhatsApp · Delivery needs attention').waitFor();
    assert.deepEqual(await attention.evaluate(el => [el.classList.contains('phone-attention'), el.classList.contains('messaging-muted'), getComputedStyle(el).color === getComputedStyle(el.closest('.phone-block')).color]), [true, false, true]);
    // A saved agent record in place of a phone block, as an older host answers this path.
    answer = ({ json }) => json({ id: 'agent-4f0c', title: 'Queue watch', agent_kind: 'tracker' });
    await page.goto(origin); await page.waitForFunction(() => window.RocotoMessaging);
    await page.evaluate(() => { window.phone = window.RocotoMessaging.mountPhone({ agentId: 'agent-4f0c' }); document.querySelector('#root').append(window.phone.element); });
    await page.getByText('Phone alerts are reconnecting. Try again.').waitFor();
    assert.equal(await page.locator('.phone-participant').count(), 0);
    answer = ({ route }) => route.fulfill({ status: 401, contentType: 'text/plain', body: 'Enter your company access code.' });
    await page.getByRole('button', { name: 'Try again' }).click();
    await page.getByText('Enter your company access code.').waitFor();
    answer = null; await page.getByRole('button', { name: 'Try again' }).click();
    await row(page, 'Maria Ivanova').waitFor();
  } finally { await close(); }
});

test('delivery labels follow the contract table and name the channel', async () => {
  const { page, close } = await pageFor();
  try {
    await host(page, sample()); await page.goto(origin); await page.waitForFunction(() => window.RocotoMessaging);
    const labels = await page.evaluate(() => {
      const m = window.RocotoMessaging, held = reason => m.deliveryLabel({ status: 'held', held_reason: reason });
      return {
        states: ['queued', 'dispatching', 'sent', 'delivered', 'read', 'uncertain', 'failed', 'cancelled', 'expired'].map(status => m.deliveryLabel({ status })),
        held: ['spend_cap', 'stopped', 'number_required', 'opted_out', 'channel_unready', 'window_closed', 'access_required', 'spend_unreadable'].map(held),
        text: [m.deliveryText({ status: 'delivered', channel: 'whatsapp' }), m.deliveryText({ status: 'sent', channel: 'viber' }), m.deliveryText({ status: 'failed', channel: 'sms' }), m.deliveryText({ status: 'held', held_reason: 'spend_cap', channel: 'sms' }), m.deliveryText({ status: 'expired', channel: null })],
        chips: [m.chipLabel({ status: 'sent' }), m.chipLabel({ status: 'sent', channel: 'sms' }), m.chipLabel({ status: 'delivered', channel: 'viber' }), m.chipLabel({ status: 'read', channel: 'whatsapp' }), m.chipLabel({ status: 'uncertain', channel: 'sms' }), m.chipLabel({ status: 'sent', waiting_for_reply: true, channel: 'viber' }), m.chipLabel({ status: 'failed', channel: 'sms' }), m.chipLabel({ status: 'dispatching', channel: 'whatsapp' }), m.chipLabel({ status: 'queued' }), m.chipLabel({ status: 'cancelled', channel: 'sms' })],
        senders: [m.senderLabel({ principal_id: 'member-01', display_name: 'Maria Ivanova', channel: 'whatsapp' }, 'member-01'), m.senderLabel({ principal_id: 'member-02', display_name: 'Ivan Petrov', channel: 'viber' }, 'member-01'), m.senderLabel({ principal_id: 'member-01', channel: 'web' }, 'member-01')],
        names: ['viber', 'sms', 'whatsapp', 'slack'].map(m.channelLabel),
      };
    });
    assert.deepEqual(labels.states, ['Sending', 'Sending', 'Sent', 'Delivered', 'Read', 'Confirming delivery', 'Delivery needs attention', 'Alerts paused', 'Alert kept in Rocoto']);
    assert.deepEqual(labels.held, ['Today’s alert limit reached', 'Alerts paused', 'Add a number', 'Alert kept in Rocoto', 'Alert kept in Rocoto', 'Alert kept in Rocoto', 'Alert kept in Rocoto', 'Alert kept in Rocoto']);
    assert.deepEqual(labels.text, ['WhatsApp · Delivered', 'Viber · Sent', 'SMS · Delivery needs attention', 'Today’s alert limit reached', 'Alert kept in Rocoto']);
    // An agent's own answer shows its send while it is on its way and when it needs attention.
    assert.deepEqual(labels.chips, ['Sent to WhatsApp', 'Sent to SMS', 'Delivered in Viber', 'Read in WhatsApp', 'Confirming SMS delivery', 'Waiting for your reply in Viber', 'SMS delivery needs attention', 'Sending to WhatsApp', 'Sending to WhatsApp', '']);
    assert.deepEqual(labels.senders, ['Sent from WhatsApp', 'Ivan Petrov · sent from Viber', '']);
    assert.deepEqual(labels.names, ['Viber', 'SMS', 'WhatsApp', '']);
  } finally { await close(); }
});

test('the keyboard path reaches every control and keeps its place after a change', async () => {
  const { page, close } = await pageFor();
  try {
    const calls = await host(page, sample()); await mount(page);
    const focused = () => page.evaluate(() => document.activeElement.getAttribute('aria-label') || document.activeElement.textContent || document.activeElement.tagName);
    await page.locator('#opener').focus();
    const order = [];
    for (let step = 0; step < 7; step++) { await page.keyboard.press('Tab'); order.push(await focused()); }
    assert.deepEqual(order, ['Phone alerts for this agent', 'Channel for Maria Ivanova', 'Alerts for Maria Ivanova', 'Alerts for Ivan Petrov', 'Remove Ivan Petrov', 'Elena Georgieva', 'Add']);
    await page.getByRole('switch', { name: 'Alerts for Ivan Petrov' }).focus(); await page.keyboard.press('Space');
    await row(page, 'Ivan Petrov').getByText('Alerts off').waitFor();
    assert.equal(await focused(), 'Remove Ivan Petrov', 'focus stays in the same participant when their switch rests');
    await page.getByRole('switch', { name: 'Phone alerts for this agent' }).focus(); await page.keyboard.press('Space');
    await page.locator('.phone-delivery', { hasText: 'Phone alerts paused' }).waitFor();
    assert.equal(await focused(), 'Phone alerts for this agent');
    await page.getByLabel('Add a participant').focus(); await page.keyboard.press('Tab');
    assert.equal(await focused(), 'Add'); await page.keyboard.press('Enter');
    await row(page, 'Elena Georgieva').waitFor();
    assert.deepEqual(posts(calls).map(call => call.path), [PHONE + '/participants/member-02', PHONE + '/delivery', PHONE + '/participants']);
  } finally { await close(); }
});

test('the phone block and the contact card fit desktop and phone widths', async () => {
  fs.mkdirSync(shots, { recursive: true });
  for (const width of [1600, 390]) {
    const { page, close } = await pageFor(width);
    try {
      const block = asIvan(); block.can_manage = true; block.available_members = [{ member_id: 'member-03', name: 'Elena Georgieva', verified: false }];
      block.participants[0].is_self = false; block.participants[1].is_self = true;
      await host(page, block); await mount(page);
      await page.screenshot({ path: path.join(shots, `phone-block-${width}.png`), fullPage: true });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth), true, 'the page keeps its width');
      const boxes = await page.locator('.phone-block').locator('h3,p,strong,input,select,button,label').evaluateAll(items => items.map(item => { const box = item.getBoundingClientRect(); return [item.tagName, Math.floor(box.left), Math.ceil(box.right), Math.round(box.height)]; }));
      const frame = await page.locator('.phone-block').boundingBox();
      for (const [tag, left, right, height] of boxes) { assert.ok(left >= Math.floor(frame.x) && right <= Math.ceil(frame.x + frame.width), `${tag} stays inside the block at ${width}`); assert.ok(height > 0, `${tag} is drawn at ${width}`); }
      for (const control of await page.locator('.phone-block').locator('label.phone-field-switch, select, button').all()) assert.ok((await control.boundingBox()).height >= 40, 'controls keep a 40px target');
      await page.getByRole('button', { name: 'Add a number' }).click();
      const card = page.getByRole('dialog', { name: 'Ivan Petrov' }); await card.waitFor(); await card.getByText('WhatsApp', { exact: true }).waitFor();
      await page.screenshot({ path: path.join(shots, `channel-card-${width}.png`), fullPage: true });
      const box = await card.boundingBox();
      assert.ok(box.x >= 0 && box.x + box.width <= width && box.y >= 0 && box.y + box.height <= (width === 390 ? 844 : 1000));
      assert.equal(await card.evaluate(el => el.scrollWidth <= el.clientWidth), true);
    } finally { await close(); }
  }
});

test('every sentence in the block and the card says what is', async () => {
  const { page, close } = await pageFor();
  try {
    const seen = [], collect = async locator => seen.push(...(await locator.innerText()).split('\n').map(line => line.trim()).filter(Boolean));
    for (const block of [sample(), asIvan(), sample({ delivery: 'paused', read_only: true }), sample({ spend: { state: 'cap_reached', agent: { used: 50, cap: 50 }, company: { used: 60, cap: 200 }, resets_at: 1791676800.0 } }), sample({ spend: { state: 'unavailable', agent: null, company: null, resets_at: 1791676800.0 } })]) {
      for (const person of block.participants) for (const verification of [person.verification, 'in_progress', 'opted_out']) {
        person.verification = verification; await page.unrouteAll({ behavior: 'ignoreErrors' }); await page.route('**/*', route => route.request().url().startsWith(origin + '/') ? route.fallback() : route.abort());
        await host(page, block); await mount(page); await collect(page.locator('.phone-block'));
      }
    }
    await page.unrouteAll({ behavior: 'ignoreErrors' }); await page.route('**/*', route => route.request().url().startsWith(origin + '/') ? route.fallback() : route.abort());
    await host(page, asIvan()); await mount(page); await page.getByRole('button', { name: 'Add a number' }).click();
    const card = page.getByRole('dialog', { name: 'Ivan Petrov' }); await card.getByText('WhatsApp', { exact: true }).waitFor(); await collect(card);
    assert.ok(seen.length > 60);
    assert.deepEqual([...new Set(seen)].filter(line => NEGATIVE.test(line)), []);
  } finally { await close(); }
});
