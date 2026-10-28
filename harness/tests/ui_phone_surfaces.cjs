/* Browser proofs for where phone alerts live in the app: a Tracking Agent's info and canvas, its chat, and the
 * wording that places them (CHANNELS.md, CONTROL-ROOM.md "Tracking Agent phone block").
 * Every API answer is fixture-owned; no provider, model or network call leaves the page.
 * NODE_PATH=/path/to/playwright/node_modules PLAYWRIGHT_CHROMIUM_EXECUTABLE=/path/to/chromium \
 *   node --test harness/tests/ui_phone_surfaces.cjs
 * ROCOTO_PHONE_SCREENSHOTS names the folder for the 1600x1000 and 390x844 captures.
 */
const { test, before, after } = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const { chromium } = require('playwright');

const web = process.env.ROCOTO_UI_TEST_WEB || require('./web_fixture.cjs').web(), pub = process.env.ROCOTO_UI_TEST_PUBLIC || path.resolve(__dirname, '../../app/public');
const theme = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../theme.json'))), catalogue = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../runtime/plugin_catalogue.json')));
const shots = process.env.ROCOTO_PHONE_SCREENSHOTS || '/tmp/rocoto-phone-ui';
const TRACKER = 'agent-4f0c', ACTION = 'agent-act1', PHONE = `/api/agents/${TRACKER}/phone`;
let browser, server, origin;

before(async () => {
  server = http.createServer((req, res) => {
    const route = new URL(req.url, 'http://localhost').pathname;
    if (route === '/theme.css' || route === '/static/harness/theme.css') {
      res.setHeader('Content-Type', 'text/css');
      res.end(':root{--accent:#de0637;--font:Arial,sans-serif;--wordmark-font:Fraunces,serif;--radius:.75rem;--radius-sm:.5rem;--radius-lg:1.25rem;--connection-online:green;' + Object.entries({ ...theme.palette, ...theme.typography }).map(([k, v]) => '--' + k + ':' + v + ';').join('') + '}');
      return;
    }
    const file = path.resolve(route.startsWith('/brand/') || route.startsWith('/connection-brands/') || route.startsWith('/fonts/') ? pub : web, '.' + (route === '/' ? '/index.html' : route));
    if (![web, pub].some(root => file.startsWith(root + path.sep)) || !fs.existsSync(file)) { res.writeHead(404); res.end(); return; }
    res.setHeader('Content-Type', ({ '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.svg': 'image/svg+xml', '.png': 'image/png' })[path.extname(file)] || 'application/octet-stream');
    fs.createReadStream(file).pipe(res);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  origin = 'http://127.0.0.1:' + server.address().port;
  browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : {}), args: ['--no-sandbox'] });
});
after(async () => { await browser?.close(); await new Promise(resolve => server?.close(resolve)); });

const baseChat = { id: 'fixture-chat', title: 'Fixture café review', kind: 'chat', dataset: 'cafe', owner_principal_id: 'owner', pinned: false, archived: false, metadata_revision: 0, context: { used_tokens: 12345, window_tokens: 1000000 }, messages: [{ role: 'user', content: 'Describe this source.' }, { role: 'assistant', content: 'Fixture saved response.' }], running: false };
const readiness = { configurable: true, ready: false, notice: 'Configure your agent here. Connect a live camera feed to start scheduled runs.' };
const tracker = () => ({ id: TRACKER, chat_id: 'chat-91ab', title: 'Queue watch', description: 'Alerts when the till queue passes five people.', agent_kind: 'tracker', input_kind: 'tracker_events', mode: 'ad_hoc', dataset: 'cafe', status: 'draft', max_runs: 1, runs_started: 0, interval_seconds: 60, fresh_inspections_per_run: 0, revision: 1, instruction: 'Watch the till queue.', execution: readiness, runs: [] });
const action = () => ({ id: ACTION, chat_id: 'chat-act1', title: 'Notify supplier', description: 'Messages the bakery supplier when stock runs low.', agent_kind: 'action', input_kind: 'tracker_events', mode: 'ad_hoc', dataset: 'cafe', status: 'draft', max_runs: 1, runs_started: 0, interval_seconds: 60, fresh_inspections_per_run: 0, revision: 1, instruction: 'Message the supplier.', execution: readiness, runs: [] });
/* The sample phone block of CHANNELS.md, with the signed-in fixture owner as its creator. */
function phoneBlock(change = {}) {
  return {
    agent_id: TRACKER, chat_id: 'chat-91ab', collection: 'cafe', generation: 4, delivery: 'on', can_manage: true, read_only: false,
    channels: [{ channel: 'viber', label: 'Viber', ready: false, reason: 'provider_setup_required', sender: null }, { channel: 'sms', label: 'SMS', ready: false, reason: 'provider_setup_required', sender: null }, { channel: 'whatsapp', label: 'WhatsApp', ready: true, reason: null, sender: '+12025550123' }],
    participants: [
      { member_id: 'owner', name: 'Fixture Owner', role: 'creator', is_self: true, channel: 'whatsapp', channel_choice: 'automatic', verified_channels: ['whatsapp'], address_masked: '+359•••••••56', verification: 'verified', enabled: true, last_delivery: { id: 'dlv-8cf7835a1323e7c5b4bcee0a0f60bdda', channel: 'whatsapp', status: 'delivered', held_reason: null, updated_at: 1791626400.0 }, contact_request: null },
      { member_id: 'member-02', name: 'Ivan Petrov', role: 'added', is_self: false, channel: null, channel_choice: 'automatic', verified_channels: [], address_masked: null, verification: 'number_required', enabled: true, last_delivery: null, contact_request: { id: 'contact-7a1e0c9b5d3f4e2a8b6c4d2e0f1a3b5c', status: 'open', created_at: 1791626000.0, expires_at: 1792230800.0 } },
    ],
    available_members: [{ member_id: 'member-03', name: 'Elena Georgieva', verified: true }],
    spend: { state: 'open', agent: { used: 3, cap: 50 }, company: { used: 12, cap: 200 }, resets_at: 1791676800.0 },
    ...change,
  };
}

async function appFor(width = 1600, options = {}) {
  const context = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 1000 } }), page = await context.newPage(), errors = [], calls = [];
  const agents = options.agents || [tracker(), action()], block = options.block || phoneBlock();
  /* options.participant: the signed-in person is an added participant. The creator owns the chat and the server marks it participant: true (tests/test_phone_app_access.py). */
  const agentChats = agents.map(agent => ({ ...baseChat, ...(options.participant ? { owner_principal_id: 'member-01', participant: true, read_only: false } : {}), id: agent.chat_id, title: agent.title, kind: 'agent_conversation', agent_id: agent.id, agent_kind: agent.agent_kind, agent_setup: { agent_id: agent.id, agent_kind: agent.agent_kind, mode: agent.mode }, dataset: agent.dataset, messages: options.messages?.[agent.id] || [{ role: 'user', content: agent.instruction }, { role: 'assistant', content: 'Agent conversation ready.' }] }));
  page.setDefaultTimeout(5000); page.on('pageerror', error => errors.push(error.message));
  await page.route('https://raw.githubusercontent.com/**', route => route.fulfill({ contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><rect width="40" height="40" fill="gray"/></svg>' }));
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url()), p = url.pathname, method = request.method(), body = method === 'POST' ? request.postDataJSON() : null;
    const json = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) });
    if (p.includes('/phone') || p.startsWith('/api/messaging') || method !== 'GET') calls.push({ method, path: p, query: url.search, body, demo: request.headers()['x-rocoto-page'] || null });
    if (p === PHONE && method === 'GET') return options.phoneStatus ? json({ detail: 'Choose a Tracking Agent from your current workspace.' }, options.phoneStatus) : json(block);
    if (p.startsWith(PHONE + '/') && method === 'POST') {
      const rest = p.slice(PHONE.length + 1).split('/'), person = block.participants.find(item => item.member_id === rest[1]);
      if (rest[0] === 'delivery') block.delivery = body.state; else if (rest[0] === 'participants' && person && Object.hasOwn(body, 'enabled')) person.enabled = body.enabled; else assert.fail('Unexpected phone change during UI test: ' + p);
      block.generation += 1; return json(block);
    }
    if (/^\/api\/agents\/[^/]+\/phone/.test(p)) return json({ detail: 'Choose a Tracking Agent from your current workspace.' }, 404);
    // POST /api/chat as server.py answers a participant: a body carrying agent_setup, a follow-up, skills or clips is refused.
    if (options.participant && method === 'POST' && p === '/api/chat') return (body.agent_setup || body.followup_run_chat_id || (body.skill_ids || []).length || (body.clip_attachments || []).length) ? json({ detail: 'Send text and your own images in this agent’s conversation.' }, 400) : route.fulfill({ status: 200, contentType: 'application/x-ndjson', body: JSON.stringify({ type: 'delta', text: 'Two people are waiting at the till.' }) + '\n' + JSON.stringify({ type: 'done', status: 'completed' }) + '\n' });
    if (p === '/api/messaging') return json({ provider: 'whatsapp', readiness: { provider: 'whatsapp', configured: true, ready: true, otp_ready: true, hello_ready: true, reason: null, sender_phone: '+12025550123' }, contact: null, challenge: null, outbox: [], waiting: false, contacts: [], collection: url.searchParams.get('collection'), member_id: url.searchParams.get('member_id'), members: [{ id: 'owner', display_name: 'Fixture Owner', is_self: true, role: 'owner' }], read_only: false, channels: block.channels });
    assert.equal(method, 'GET', 'Unexpected mutation during UI test: ' + p);
    let data = {};
    if (p === '/api/session' && options.sessionDelay) await new Promise(resolve => setTimeout(resolve, options.sessionDelay));
    if (p === '/api/session') data = { authenticated: true, demo_browsing: false, can_query: true, read_only: false, role: 'owner', principal: { id: 'owner', display_name: 'Fixture Owner' }, effective_principal_id: 'owner', environment: { id: 'demo', name: 'Demo company' }, environments: [{ id: 'demo', name: 'Demo company', category: 'demo', collections: [{ id: 'cafe', name: 'Café' }] }], viewing_as: null };
    else if (p === '/api/status') data = { demo_guidance: { cafe: { name: 'Fixture Café', questions: [{ label: 'Review café service', prompt: 'Inspect the counter and cite a moment.' }] } }, agent: { authenticated: true, active_chats: {} }, corpus: [{ video_id: 'fixture-source', dataset: 'cafe', duration: 8 }], connection: { host: 'fixture-host', managed_authenticated: true, check_status: 'authenticated', checked_at: new Date().toISOString() } };
    else if (p === '/api/observers') data = { principals: [] };
    else if (p === '/api/chats') data = { chats: [baseChat, ...agentChats] };
    else if (p.startsWith('/api/chats/')) data = agentChats.find(item => p.endsWith('/' + item.id)) || baseChat;
    else if (p === '/api/agents') data = { agents, capabilities: { recurring_agents: readiness } };
    else if (p === '/api/agents/catalogue') data = { templates: [] };
    else if (/^\/api\/agents\/[^/]+\/observations$/.test(p)) data = { summary: 'The till queue passed five people twice this morning.', incidents: [] };
    else if (p.startsWith('/api/agents/')) { const agent = agents.find(item => item.id === p.split('/')[3]); if (!agent) return json({}, 404); data = agent; }
    else if (p === '/api/retail/monitoring/profiles') data = { profiles: [] };
    else if (p === '/api/workspace/capabilities') data = { recurring_agents: readiness };
    else if (p === '/api/plugins/installed') data = { plugins: [], available: true, installable_ids: [] };
    else if (p === '/api/plugins/catalogue') data = catalogue;
    else if (p === '/api/connections') data = { connections: [] };
    else if (p === '/api/connections/custom') data = { connections: [] };
    else if (p === '/api/files') data = { files: [] };
    else if (p === '/api/skills/catalogue' || p === '/api/skills') data = { skills: [] };
    else if (p === '/api/business/map') data = { status: 'empty', map: null, data: null };
    else if (p === '/api/usage') data = { company: { available_usd: '30', prepaid_funded_usd: '40', reserved_usd: '0', monthly_used_usd: '10', monthly_limit_usd: '40' }, context: baseChat.context, codex: { route: 'OpenAI API', limits: [] }, storage: {}, billing: { status: 'unconfigured' } };
    else if (p === '/api/retail/cameras') data = { cameras: [] };
    return json(data);
  });
  await page.goto(origin + (options.start || '/?chat=fixture-chat'));
  if (!options.start) await page.locator('#messages .message').first().waitFor();
  const phoneReads = id => calls.filter(call => call.method === 'GET' && call.path === `/api/agents/${id}/phone`).length;
  return { page, calls, errors, block, agents, phoneReads, close: async () => { assert.deepEqual(errors, []); await context.close(); } };
}
const shot = (page, name) => { fs.mkdirSync(shots, { recursive: true }); return page.screenshot({ path: path.join(shots, name + '.png') }); };
const fitsWidth = page => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
/* Every heading, sentence and control of the phone block is drawn inside the block's own box. */
const outsideBlock = phone => phone.evaluate(block => { const frame = block.getBoundingClientRect(); return [...block.querySelectorAll('h3,p,strong,span,input,select,button,label')].filter(item => { const box = item.getBoundingClientRect(); return box.width > 0 && (box.left < frame.left - 1 || box.right > frame.right + 1); }).map(item => item.getAttribute('aria-label') || item.textContent.trim().slice(0, 40)); });
async function openInfo(page, title) { await page.locator('#agents-nav').click(); await page.getByRole('button', { name: 'About ' + title, exact: true }).click(); await page.locator('#agent-info-dialog').getByRole('button', { name: 'Edit in chat' }).waitFor(); }

test('a Tracking Agent’s info holds its phone block and an Action Agent’s info states the rule', async () => {
  for (const width of [1600, 390]) {
    const h = await appFor(width), p = h.page;
    try {
      await openInfo(p, 'Queue watch');
      const info = p.locator('#agent-info-dialog'), phone = info.getByRole('region', { name: 'Alerts on your phone' });
      await phone.locator('.phone-participant').first().waitFor();
      const text = await phone.innerText();
      assert.match(text, /Fixture Owner\s+Creator · You/); assert.match(text, /WhatsApp · \+359•••••••56/); assert.match(text, /WhatsApp · Delivered · /); assert.match(text, /Ivan Petrov/); assert.match(text, /Number requested/);
      assert.doesNotMatch(await info.innerText(), /Delivery integrations|In development/);
      await phone.scrollIntoViewIfNeeded(); await shot(p, `tracking-agent-info-${width}`);
      const frame = await info.boundingBox(), box = await phone.boundingBox();
      assert.ok(box.x >= frame.x && box.x + box.width <= frame.x + frame.width + 1, 'the block stays inside the dialog');
      assert.equal(await info.locator('.agent-editor-body').evaluate(el => el.scrollWidth <= el.clientWidth), true);
      assert.deepEqual(await outsideBlock(phone), [], `the block holds its own content at ${width}`);
      assert.equal(await fitsWidth(p), true);
      assert.ok((await phone.locator('.phone-add').boundingBox()).height <= 150, `the add form keeps its own compact layout inside the dialog at ${width}`);
      // A later agent refresh keeps the same block, its place and its one read.
      await phone.evaluate(el => { el.dataset.kept = 'yes'; }); await phone.getByRole('switch', { name: 'Alerts for Fixture Owner' }).focus();
      h.agents[0].description = 'Alerts when the till queue passes six people.';
      await info.getByText('Alerts when the till queue passes six people.').waitFor({ timeout: 9000 });
      assert.equal(await phone.getAttribute('data-kept'), 'yes'); assert.equal(await info.locator('.phone-block').count(), 1); assert.equal(h.phoneReads(TRACKER), 1);
      assert.equal(await p.evaluate(() => document.activeElement.getAttribute('aria-label')), 'Alerts for Fixture Owner');
      await phone.getByRole('switch', { name: 'Phone alerts for this agent' }).click();
      await phone.getByText('Phone alerts paused').waitFor();
      assert.deepEqual(h.calls.filter(call => call.method === 'POST').map(call => [call.path, call.body, call.demo]), [[PHONE + '/delivery', { state: 'paused', expected_generation: 4 }, '1']]);
      await p.keyboard.press('Escape'); await info.waitFor({ state: 'detached' });
      await p.getByRole('button', { name: 'About Notify supplier', exact: true }).click();
      const other = p.locator('#agent-info-dialog'); await other.getByRole('button', { name: 'Edit in chat' }).waitFor();
      assert.equal(await other.locator('.phone-block').count(), 0);
      assert.match(await other.innerText(), /Alerts on your phone come from Tracking Agents\. An Action Agent messages other people under its own grants\./);
      assert.doesNotMatch(await other.innerText(), /Delivery integrations|In development/);
      assert.equal(h.phoneReads(ACTION), 0, 'an Action Agent holds no phone block');
    } finally { await h.close(); }
  }
});

test('the tracker canvas header carries the phone block and an Action Agent has none', async () => {
  for (const width of [1600, 390]) {
    const h = await appFor(width), p = h.page;
    try {
      await p.locator('#new-chat').click();
      const home = p.locator('#control-room-view');
      // Home's tile for a tracker the shop made asks the shell for its canvas.
      await p.evaluate(id => window.dispatchEvent(new CustomEvent('rocoto:home-open-tracker', { detail: { agentId: id } })), TRACKER);
      const canvas = home.locator('.cr-detail, .tc-canvas'), phone = canvas.getByRole('region', { name: 'Alerts on your phone' });
      await phone.locator('.phone-participant').first().waitFor(); await canvas.getByText('The till queue passed five people twice this morning.').waitFor();
      assert.equal(await canvas.locator('.phone-block').count(), 1); assert.equal(h.phoneReads(TRACKER), 1, 'the canvas keeps one block across its own refresh');
      assert.equal(await canvas.evaluate(el => { const order = [...el.querySelectorAll('.cr-detail-heading, .cr-phone-slot, .cr-detail-grid')].map(item => item.className.split(' ')[0]); return order.join(' > '); }), 'cr-detail-heading > cr-phone-slot > cr-detail-grid');
      assert.match(await phone.innerText(), /WhatsApp is ready\. Viber and SMS are being connected\./);
      await shot(p, `tracking-agent-canvas-${width}`);
      assert.equal(await fitsWidth(p), true);
      const view = await home.boundingBox(), box = await phone.boundingBox(); assert.ok(box.x >= view.x && box.x + box.width <= view.x + view.width + 1);
      assert.deepEqual(await outsideBlock(phone), [], `the block holds its own content at ${width}`);
      await phone.getByRole('switch', { name: 'Alerts for Fixture Owner' }).focus(); await p.keyboard.press('Space');
      await phone.getByText('Alerts off').first().waitFor();
      assert.deepEqual(h.calls.filter(call => call.method === 'POST').map(call => [call.path, call.body]), [[PHONE + '/participants/owner', { enabled: false, expected_generation: 4 }]]);
      await canvas.getByRole('button', { name: '← Home' }).click();
      assert.equal(await home.locator('.phone-block').count(), 0);
      assert.equal(h.phoneReads(ACTION), 0);
    } finally { await h.close(); }
  }
});

test('an agent record without the tracker kind shows no phone block', async () => {
  const legacy = tracker(); delete legacy.agent_kind;
  const h = await appFor(1600, { agents: [legacy] }), p = h.page;
  try {
    await p.locator('#new-chat').click();
    await p.evaluate(id => window.dispatchEvent(new CustomEvent('rocoto:home-open-tracker', { detail: { agentId: id } })), TRACKER);
    await p.locator('#control-room-view').locator('.cr-detail, .tc-canvas').getByText('The till queue passed five people twice this morning.').waitFor();
    assert.equal(await p.locator('.phone-block').count(), 0);
    await openInfo(p, 'Queue watch');
    assert.equal(await p.locator('.phone-block').count(), 0); assert.equal(h.phoneReads(TRACKER), 0);
  } finally { await h.close(); }
});

const alertChat = [
  { role: 'user', content: 'Watch the till queue.' },
  { role: 'assistant', content: 'Queue watch: six people are waiting at till 2.', alert: { id: 'evt-71', revision: 1 }, created_at: 1791626400.0, channel_deliveries: [
    { id: 'dlv-8cf7835a1323e7c5b4bcee0a0f60bdda', member_id: 'owner', channel: 'whatsapp', status: 'delivered', updated_at: 1791626402.0 },
    { id: 'dlv-11111111111111111111111111111111', member_id: 'member-02', channel: null, status: 'held', updated_at: 1791626401.0 },
    { id: 'dlv-22222222222222222222222222222222', member_id: 'member-09', channel: 'sms', status: 'uncertain', updated_at: 1791626401.0 }] },
  { role: 'user', content: 'Open till 3.', sender_principal_id: 'member-02', channel: { principal_id: 'member-02', display_name: 'Ivan Petrov', channel: 'viber', admission_key: 'viber:5d41', provider_event_id: 'viber-event-1' } },
  { role: 'user', content: 'Thanks, on my way.', sender_principal_id: 'owner', channel: { principal_id: 'owner', display_name: 'Fixture Owner', channel: 'whatsapp', admission_key: 'whatsapp:9a0c', provider_event_id: 'wamid.1' } },
  { role: 'user', content: 'Typed in the app.', sender_principal_id: 'owner', channel: { principal_id: 'owner', display_name: 'Fixture Owner', channel: 'web' } },
  { role: 'assistant', content: 'Till 3 opened at 10:04.', channel_delivery: { id: 'out-1', kind: 'text', status: 'sent', channel: 'viber', waiting_for_reply: false } },
  { role: 'assistant', content: 'I will check again at 10:15.', channel_delivery: { id: 'out-2', kind: 'text', status: 'delivered' } },
  { role: 'assistant', content: 'Reply when the queue clears.', channel_delivery: { id: 'out-3', kind: 'text', status: 'sent', channel: 'sms', waiting_for_reply: true } },
];

test('chat delivery chips name the channel for each participant and a phone reply names its channel', async () => {
  for (const width of [1600, 390]) {
    const h = await appFor(width, { messages: { [TRACKER]: alertChat } }), p = h.page;
    try {
      await p.locator('#agents-nav').click(); await p.locator('.agent-collection-card', { hasText: 'Queue watch' }).click();
      await p.getByText('Queue watch: six people are waiting at till 2.').waitFor();
      const chips = p.locator('.message-channel-deliveries li');
      await chips.filter({ hasText: 'Ivan Petrov' }).waitFor();
      assert.deepEqual(await chips.allInnerTexts(), ['You · WhatsApp · Delivered', 'Ivan Petrov · Alert kept in Rocoto', 'Participant · SMS · Confirming delivery']);
      assert.deepEqual(await p.locator('.message-channel-sender').allInnerTexts(), ['Ivan Petrov · sent from Viber', 'Sent from WhatsApp']);
      assert.deepEqual(await p.locator('.message-channel-status').allInnerTexts(), ['Sent to Viber', 'Delivered in WhatsApp', 'Waiting for your reply in SMS']);
      await p.locator('#agent-info-button').waitFor(); await p.waitForTimeout(600); assert.equal(h.phoneReads(TRACKER), 1, 'opening the chat reads its phone block once');
      await p.locator('.message-channel-deliveries').scrollIntoViewIfNeeded(); await shot(p, `tracking-agent-chat-${width}`);
      assert.equal(await fitsWidth(p), true);
      assert.equal(await p.locator('.message-channel-deliveries').evaluate(el => el.scrollWidth <= el.clientWidth), true);
    } finally { await h.close(); }
  }
});

test('the link in a phone alert opens that alert in the Tracking Agent’s chat', async () => {
  const long = [{ role: 'user', content: 'Watch the till queue.' }, alertChat[1], ...Array.from({ length: 24 }, (_, index) => ({ role: 'assistant', content: `Later update ${index + 1}. The queue stays under five people.` }))];
  for (const width of [1600, 390]) {
    const h = await appFor(width, { messages: { [TRACKER]: long }, start: '/?chat=chat-91ab&alert=evt-71' }), p = h.page;
    try {
      const alert = p.locator('.message', { hasText: 'Queue watch: six people are waiting at till 2.' }); await alert.waitFor();
      const onScreen = () => alert.evaluate(el => { const box = el.getBoundingClientRect(), view = document.querySelector('#conversation').getBoundingClientRect(); return box.top >= view.top - 1 && box.top < view.bottom; });
      await p.getByText('Later update 24. The queue stays under five people.').waitFor(); await p.locator('#agent-info-button').waitFor();
      assert.equal(await p.locator('#conversation').evaluate(el => el.scrollHeight > el.clientHeight + 200), true, 'the chat is long enough to prove the jump');
      await p.waitForFunction(() => document.activeElement?.classList.contains('message') && document.activeElement.textContent.includes('six people are waiting at till 2'), null, { timeout: 5000 }).catch(() => assert.fail('the linked alert holds focus for the keyboard and screen reader'));
      assert.equal(await onScreen(), true, 'the linked alert is on screen');
      await p.waitForTimeout(4500); assert.equal(await onScreen(), true, 'the alert stays on screen through the agent refresh');
      assert.equal(h.phoneReads(TRACKER), 1, 'opening the chat from its link reads the phone block once');
      await p.keyboard.press('Tab');
      assert.equal(new URL(p.url()).searchParams.has('alert'), false, 'the first input releases the alert and cleans the address');
      await p.locator('#conversation').evaluate(el => { el.scrollTop = el.scrollHeight; }); await p.waitForTimeout(4500);
      assert.equal(await onScreen(), false, 'the person’s own scroll position stays theirs');
    } finally { await h.close(); }
  }
  // Keys pressed while the workspace opens, as during sign-in, leave the link in place.
  const h = await appFor(1600, { messages: { [TRACKER]: long }, start: '/?chat=chat-91ab&alert=evt-71', sessionDelay: 700 }), p = h.page;
  try {
    await p.keyboard.press('Shift'); await p.mouse.click(5, 5);
    const alert = p.locator('.message', { hasText: 'Queue watch: six people are waiting at till 2.' }); await alert.waitFor();
    await p.waitForFunction(() => document.activeElement?.classList.contains('message') && document.activeElement.textContent.includes('six people are waiting at till 2'), null, { timeout: 5000 }).catch(() => assert.fail('keys pressed ahead of the chat leave the link in place'));
  } finally { await h.close(); }
});

test('the contact card appears in the Tracking Agent’s chat for a participant who needs a number', async () => {
  for (const width of [1600, 390]) {
    const block = phoneBlock(); Object.assign(block.participants[0], { channel: null, verified_channels: [], address_masked: null, verification: 'number_required', last_delivery: null, contact_request: { id: 'contact-' + '3'.repeat(32), status: 'open', created_at: 1791626000.0, expires_at: 1792230800.0 } });
    const h = await appFor(width, { block }), p = h.page;
    try {
      await p.locator('#agents-nav').click(); await p.locator('.agent-collection-card', { hasText: 'Queue watch' }).click();
      const notice = p.getByRole('region', { name: 'Contact card' }); await notice.waitFor();
      assert.equal(await notice.innerText().then(text => text.replace(/\s+/g, ' ').trim()), 'Add a number to get alerts from Queue watch on your phone. Add a number');
      await shot(p, `tracking-agent-contact-notice-${width}`);
      assert.equal(await fitsWidth(p), true);
      await notice.getByRole('button', { name: 'Add a number' }).focus(); await p.keyboard.press('Enter');
      const card = p.getByRole('dialog', { name: 'Fixture Owner' }); await card.waitFor(); await card.getByText('WhatsApp', { exact: true }).waitFor();
      assert.match(await card.innerText(), /Alerts from Queue watch/);
      assert.equal(await card.getByLabel('Country code').evaluate(el => el === document.activeElement), true);
      await shot(p, `tracking-agent-contact-card-${width}`);
      const box = await card.boundingBox(); assert.ok(box.x >= 0 && box.x + box.width <= width);
      assert.ok(h.calls.some(call => call.method === 'GET' && call.path === '/api/messaging' && call.query === '?member_id=owner&collection=cafe'));
      await p.keyboard.press('Escape'); await card.waitFor({ state: 'detached' });
      assert.equal(await p.evaluate(() => document.activeElement.textContent), 'Add a number', 'focus returns to the card’s action');
      assert.equal(h.calls.filter(call => call.method === 'POST').length, 0, 'an open request is reused');
      // An Action Agent's chat carries no contact card and reads no phone block.
      await p.locator('#agents-nav').click(); await p.locator('.agent-collection-card', { hasText: 'Notify supplier' }).click();
      await p.getByText('Agent conversation ready.').waitFor();
      assert.equal(await p.getByRole('region', { name: 'Contact card' }).count(), 0); assert.equal(h.phoneReads(ACTION), 0);
    } finally { await h.close(); }
  }
});

test('a member outside the participants reads the agent with its phone block at rest', async () => {
  const h = await appFor(1600, { phoneStatus: 404 }), p = h.page;
  try {
    await p.locator('#agents-nav').click(); await p.locator('.agent-collection-card', { hasText: 'Queue watch' }).click();
    await p.getByText('Agent conversation ready.').waitFor();
    assert.equal(await p.getByRole('region', { name: 'Contact card' }).count(), 0);
    await p.locator('#agent-info-button').click();
    const phone = p.locator('#agent-info-dialog').getByRole('region', { name: 'Alerts on your phone' });
    await phone.getByText('Choose a Tracking Agent from your current workspace.').waitFor();
    assert.equal(await phone.locator('.phone-participant').count(), 0);
  } finally { await h.close(); }
});

test('wording places phone alerts with Tracking Agents and keeps Messaging as the company list', async () => {
  const h = await appFor(1600, { agents: [] }), p = h.page;
  try {
    const editor = await p.locator('#agent-editor').evaluate(el => el.textContent);
    assert.doesNotMatch(editor, /Delivery integrations|In development/);
    assert.match(editor, /Alerts on your phone\s*A Tracking Agent sends its alerts to its participants’ verified phones\. Messages to other people belong to Action Agents\./);
    await p.goto(origin + '/?settings=notifications'); await p.locator('#notifications-view h1').waitFor();
    const notifications = await p.locator('#notifications-view').innerText();
    assert.equal(await p.locator('#notifications-view').getByRole('heading', { level: 2, name: 'Alerts on your phone' }).count(), 1);
    assert.match(notifications, /Each Tracking Agent sends its alerts to its participants’ verified phones\. Open a Tracking Agent’s info to manage its participants, channels and switch\./);
    assert.doesNotMatch(notifications, /Agent alerts|alert connection is available/);
    await p.goto(origin + '/?settings=messaging'); await p.locator('#messaging-view h1').waitFor();
    assert.equal(await p.locator('#settings-messaging').innerText(), 'Messaging');
    await p.locator('#messaging-view').getByText('Verified numbers of people in your organization.').waitFor();
    await p.locator('#messaging-view').getByRole('button', { name: 'Add a number' }).click();
    const card = p.locator('dialog.messaging-profile-dialog'); await card.waitFor();
    assert.equal(await card.getByRole('radio').count(), 3); assert.equal(await card.getByRole('radio', { name: /WhatsApp/ }).isChecked(), true);
    await p.goto(origin + '/?chat=fixture-chat'); await p.locator('#messages .message').first().waitFor();
    assert.equal(await p.locator('.phone-block').count(), 0); assert.equal(h.phoneReads(TRACKER), 0);
  } finally { await h.close(); }
});

test('an added participant opens the Tracking Agent from the Agents list, after a reload, and writes in it', async () => {
  const creatorFirst = { participants: [
    { member_id: 'member-01', name: 'Maria Ivanova', role: 'creator', is_self: false, channel: 'whatsapp', channel_choice: 'automatic', verified_channels: ['whatsapp'], address_masked: '+359•••••••12', verification: 'verified', enabled: true, last_delivery: null, contact_request: null },
    { member_id: 'owner', name: 'Fixture Owner', role: 'added', is_self: true, channel: 'whatsapp', channel_choice: 'automatic', verified_channels: ['whatsapp'], address_masked: '+359•••••••56', verification: 'verified', enabled: true, last_delivery: null, contact_request: null }], can_manage: false, available_members: [] };
  for (const width of [1600, 390]) {
    const joined = { ...tracker(), owner_principal_id: 'member-01', read_only: true };
    const h = await appFor(width, { participant: true, agents: [joined], block: phoneBlock(creatorFirst) }), p = h.page;
    try {
      // The agent the server lists for a participant opens its conversation.
      await p.locator('#agents-nav').click();
      await p.locator('.agent-collection-card', { hasText: 'Queue watch' }).first().click();
      await p.locator('#messages .message', { hasText: 'Agent conversation ready.' }).waitFor();
      assert.equal(await p.getByText('Open this agent from its owner workspace.').count(), 0);
      assert.equal(new URL(p.url()).searchParams.get('agent'), TRACKER);
      // A reload of that address lands in the same conversation.
      await p.reload();
      await p.locator('#messages .message', { hasText: 'Agent conversation ready.' }).waitFor();
      assert.equal(await p.getByText('Open this agent from its owner workspace.').count(), 0);
      // A plain question typed here is an ordinary message: the agent's setup stays its creator's.
      const box = p.locator('#message'); await box.fill('How long is the queue now?'); await p.keyboard.press('Enter');
      await p.locator('#messages .message', { hasText: 'Two people are waiting at the till.' }).waitFor();
      const sent = h.calls.filter(call => call.method === 'POST' && call.path === '/api/chat');
      assert.equal(sent.length, 1);
      assert.deepEqual([sent[0].body.message, sent[0].body.chat_id, sent[0].body.dataset, Object.hasOwn(sent[0].body, 'agent_setup'), sent[0].demo], ['How long is the queue now?', 'chat-91ab', 'cafe', false, '1']);
      assert.equal(await box.inputValue(), '');
      assert.equal(await p.getByText('Send text and your own images in this agent’s conversation.').count(), 0);
      await p.locator('#messages .message', { hasText: 'How long is the queue now?' }).waitFor();
      assert.equal(await fitsWidth(p), true);
      await shot(p, `participant-agent-chat-${width}`);
    } finally { await h.close(); }
  }
});
