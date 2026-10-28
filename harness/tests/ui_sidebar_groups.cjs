/* Browser proofs for the primary sidebar: Home, Agents and Connections in the rail, and the conversation list in
 * four sections: Organization (fixed), Action, Tracker, Recent. One row grammar: icon, title, state.
 * The list follows GET /api/harness, the shell of the template the harness serves (pack.py): retail draws
 * Cameras, Action and Tracker, the job-shop test template draws Office, Dispatch and Desk. Until a shell lands the
 * page draws what every template has, the Organization chat and Recent, or the shell this browser kept for the company.
 * Every API answer is fixture-owned; no provider, model or network call leaves the page.
 * NODE_PATH=/path/to/playwright/node_modules PLAYWRIGHT_CHROMIUM_EXECUTABLE=/path/to/chromium \
 *   node --test harness/tests/ui_sidebar_groups.cjs
 * ROCOTO_SIDEBAR_SCREENSHOTS names the folder for the 1600x1000 and 390x844 captures.
 */
const { test, before, after } = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http'), { execFileSync } = require('node:child_process');
const { chromium } = require('playwright');

const web = process.env.ROCOTO_UI_TEST_WEB || require('./web_fixture.cjs').web(), pub = process.env.ROCOTO_UI_TEST_PUBLIC || path.resolve(__dirname, '../../app/public');
const theme = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../theme.json'))), catalogue = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../runtime/plugin_catalogue.json')));
const shots = process.env.ROCOTO_SIDEBAR_SCREENSHOTS || '/tmp/rocoto-sidebar-ui';
let browser, server, origin;
/* The shell a template gives the page, from the loader itself: a folder name under packs/, or a folder. */
const shellOf = template => JSON.parse(execFileSync(process.env.ROCOTO_UI_TEST_PYTHON || 'python3', ['-c',
  'import layout; layout.install()\nimport json, pathlib, sys, pack\nsource = sys.argv[1]\nprint(json.dumps(pack.load(pathlib.Path(source) if "/" in source else source).shell()))', template],
  { cwd: path.resolve(__dirname, '..'), env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' }, encoding: 'utf8' }));
const jobshop = path.resolve(__dirname, 'verticals/jobshop');
let retailShell = null;
const retail = () => retailShell ||= shellOf('retail');

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

const readiness = { configurable: true, ready: false, notice: 'Configure your agent here. Connect a live camera feed to start scheduled runs.' };
const said = [{ role: 'user', content: 'Describe this source.' }, { role: 'assistant', content: 'Fixture saved response.' }];
const chat = (id, title, updated_at, change = {}) => ({ id, title, kind: 'chat', dataset: 'cafe', environment_id: 'demo', owner_principal_id: 'owner', pinned: false, archived: false, metadata_revision: 0, updated_at, messages: said, running: false, ...change });
const saved = (id, chat_id, title, agent_kind, status) => ({ id, chat_id, title, description: title, agent_kind, input_kind: 'tracker_events', mode: 'ad_hoc', dataset: 'cafe', status, max_runs: 1, runs_started: 0, interval_seconds: 60, fresh_inspections_per_run: 0, revision: 1, instruction: title, execution: readiness, runs: [] });
const agentChat = agent => chat(agent.chat_id, agent.title, 1791620000, { kind: 'agent_conversation', agent_id: agent.id, agent_setup: { agent_id: agent.id, agent_kind: agent.agent_kind, mode: agent.mode } });
/* A Core Tracker is host-owned and has no conversation; it stays out of the list. */
const coreTracker = { ...saved('core-queue-time', null, 'Queue time', 'tracker', 'active') };

async function appFor(width = 1600, options = {}) {
  const context = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 1000 } }), page = await context.newPage(), errors = [], writes = [];
  const currentReadiness = options.readiness || readiness;
  const agents = options.agents || [saved('agent-act1', 'chat-act1', 'Notify supplier', 'action', 'paused'), saved('agent-4f0c', 'chat-91ab', 'Queue watch', 'tracker', 'active'), coreTracker];
  const chats = options.emptyHistory ? agents.filter(agent=>agent.chat_id).map(agentChat) : [
    chat('org-main', 'Organization', 1791600000, { kind: 'organization_chat', organization_purpose: 'organization' }),
    chat('org-cameras', 'Camera setup', 1791600100, { kind: 'organization_chat', organization_purpose: 'cameras' }),
    ...(options.chats || []),
    ...agents.filter(agent => agent.chat_id).map(agentChat),
    chat('fixture-chat', 'Fixture café review', 1791610000),
    chat('chat-fridge', 'Fridge door watch', 1791630000, { pinned: true }),
    chat('chat-rush', 'Morning rush review', 1791640000),
    chat('chat-old', 'Archived delivery check', 1791650000, { archived: true }),
  ];
  page.setDefaultTimeout(5000); page.on('pageerror', error => errors.push(error.message));
  await page.route('https://raw.githubusercontent.com/**', route => route.fulfill({ contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><rect width="40" height="40" fill="gray"/></svg>' }));
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url()), p = url.pathname, method = request.method(), body = method === 'POST' ? request.postDataJSON() : null;
    const json = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) });
    if (method !== 'GET') writes.push({ method, path: p, body });
    /* The chat makes itself a tracker: the stream names the saved agent, and every later answer carries it. */
    if (method === 'POST' && p === '/api/chat' && options.promote === body.chat_id) {
      const promoted = saved('agent-new1', body.chat_id, 'Fridge door watch', options.promoteKind || 'tracker', 'draft'), record = chats.find(item => item.id === body.chat_id);
      agents.push(promoted); Object.assign(record, agentChat(promoted), { updated_at: 1791660000 });
      return route.fulfill({ status: 200, contentType: 'application/x-ndjson', body: [{ type: 'agent', agent: promoted }, { type: 'delta', text: 'Saved as a tracker.' }, { type: 'done', status: 'completed' }].map(event => JSON.stringify(event)).join('\n') + '\n' });
    }
    /* A fixed chat opens through its own route; the fixture answers with that purpose's conversation. */
    if (method === 'POST' && p === '/api/organization/chats' && options.organization) return json(chat('org-' + body.purpose, body.purpose, 1791600300, { kind: 'organization_chat', organization_purpose: body.purpose, read_only: false, server_saved: true }));
    assert.equal(method, 'GET', 'Unexpected mutation during UI test: ' + p);
    let data = {};
    /* The route answers with the fixture's shell: retail's unless the case names another. `harnessRoute` is the case's own
       switchboard: a status for the next answers, a wait before each answer, and the count of reads so far. */
    if (p === '/api/harness') {
      const held = options.harnessRoute || {}; held.reads = (held.reads || 0) + 1;
      if (held.wait) await new Promise(resolve => setTimeout(resolve, held.wait).unref());
      if (held.fail > 0) { held.fail -= 1; return json({ detail: 'The workspace is reconnecting.' }, 500).catch(() => {}); }
      if (held.status) return json({ detail: 'The workspace is reconnecting.' }, held.status).catch(() => {});
      return json('harness' in options ? options.harness : retail()).catch(() => {});
    }
    if (p === '/api/session') data = { authenticated: true, demo_browsing: false, can_query: true, read_only: false, role: 'owner', principal: { id: 'owner', display_name: 'Fixture Owner' }, effective_principal_id: 'owner', environment: { id: 'demo', name: 'Demo company' }, environments: [{ id: 'demo', name: 'Demo company', category: 'demo', collections: [{ id: 'cafe', name: 'Café' }] }], viewing_as: null , ...(options.session || {}) };
    else if (p === '/api/status') data = { demo_guidance: { cafe: { name: 'Fixture Café', questions: [{ label: 'Review café service', prompt: 'Inspect the counter and cite a moment.' }] } }, agent: { authenticated: true, active_chats: {} }, corpus: [{ video_id: 'fixture-source', dataset: 'cafe', duration: 8 }], connection: { host: 'fixture-host', managed_authenticated: true, check_status: 'authenticated', checked_at: new Date().toISOString() } };
    else if (p === '/api/observers') data = { principals: [] };
    else if (p === '/api/chats') data = { chats };
    else if (p.startsWith('/api/chats/')) data = chats.find(item => p.endsWith('/' + item.id)) || chats.find(item => item.id === 'fixture-chat');
    else if (p === '/api/agents') data = { agents, capabilities: { recurring_agents: currentReadiness } };
    else if (p === '/api/agents/catalogue') data = { templates: [] };
    else if (/^\/api\/agents\/[^/]+\/phone/.test(p)) return json({ detail: 'Choose a Tracking Agent from your current workspace.' }, 404);
    else if (p.startsWith('/api/agents/')) { const agent = agents.find(item => item.id === p.split('/')[3]); if (!agent) return json({}, 404); data = agent; }
    else if (p === '/api/monitoring/profiles') data = { profiles: [] };
    else if (p === '/api/workspace/capabilities') data = { recurring_agents: currentReadiness };
    else if (p === '/api/plugins/installed') data = { plugins: [], available: true, installable_ids: [] };
    else if (p === '/api/plugins/catalogue') data = catalogue;
    else if (p === '/api/connections' || p === '/api/connections/custom') data = { connections: [] };
    else if (p === '/api/files') data = { files: [] };
    else if (p === '/api/skills/catalogue' || p === '/api/skills') data = { skills: [] };
    else if (p === '/api/business/map') data = { status: 'empty', map: null, data: null };
    else if (p === '/api/usage') data = { company: { available_usd: '30', prepaid_funded_usd: '40', reserved_usd: '0', monthly_used_usd: '10', monthly_limit_usd: '40' }, context: { used_tokens: 12345, window_tokens: 1000000 }, codex: { route: 'OpenAI API', limits: [] }, storage: {}, billing: { status: 'unconfigured' } };
    else if (p === '/api/cameras') data = { cameras: [] };
    return json(data);
  });
  if(options.bareAssets)await page.route('**/*',route=>{
    const request=route.request(),url=new URL(request.url());
    if(request.resourceType()==='document'&&url.origin===origin&&url.pathname==='/')return route.fulfill({contentType:'text/html',body:fs.readFileSync(path.join(require('./web_fixture.cjs').web('bare'),'index.html'),'utf8')});
    return route.fallback();
  });
  await page.goto(origin + (options.start || '/?chat=fixture-chat'));
  if (!options.start) await page.locator('#messages .message').first().waitFor().catch(error=>{throw new Error([...errors,error.message].join('\n'));});
  await page.locator('#chat-list [data-chat-section="recent"]').waitFor({ state: 'attached' });
  if (!options.early && agents.some(agent => agent.chat_id)) await page.locator('#chat-list [data-agent-kind]').first().waitFor({ state: 'attached' });
  return { page, writes, errors, close: async () => { assert.deepEqual(errors, []); await context.close(); } };
}
const shot = (page, name, clip) => { fs.mkdirSync(shots, { recursive: true }); return page.screenshot({ path: path.join(shots, name + '.png'), ...(clip ? { clip } : {}) }); };
const list = page => page.locator('#chat-list');
const sectionOrder = page => list(page).locator('.chat-list-section').evaluateAll(items => items.map(item => item.dataset.chatSection));
/* Each row as the reader sees it: its title, then its state. */
const rows = (page, section) => list(page).locator(`[data-chat-section="${section}"] .chat-row`).evaluateAll(items => items.map(item => [item.querySelector('.chat-row-title').textContent, item.querySelector('.chat-row-state')?.textContent || ''].filter(Boolean).join(' · ')));
const fitsWidth = page => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);

test('the conversation list holds Organization, Action, Tracker and Recent in that order, each with its own rows', async () => {
  const h = await appFor(1600), p = h.page;
  try {
    assert.deepEqual(await sectionOrder(p), ['organization', 'action', 'tracker', 'recent']);
    assert.deepEqual(await list(p).locator('.chat-list-section > h2').allInnerTexts(), ['Organization', 'Action', 'Tracker', 'Recent'], 'headings read as plain words');
    assert.deepEqual(await rows(p, 'organization'), ['Organization', 'Cameras', 'Messaging setup']);
    assert.deepEqual(await rows(p, 'action'), ['Notify supplier · Paused']);
    assert.deepEqual(await rows(p, 'tracker'), ['Queue watch · Active'], 'a Core Tracker has no conversation and stays out of the list');
    assert.deepEqual(await rows(p, 'recent'), ['Morning rush review', 'Fridge door watch', 'Fixture café review'], 'newest message first; an archived chat stays in the archive');
    // One row grammar: every row is one button holding an icon, a title and, where the agent has one, a state.
    assert.deepEqual(await list(p).locator('.chat-row').evaluateAll(items => [...new Set(items.map(item => { const link = item.querySelector(':scope > button.chat-link'); return [link.children[0].tagName.toLowerCase(), link.children[1].className].join(' > '); }))]), ['svg > chat-row-title']);
    // The product's own font, in normal case, at the weight of the rest of the interface.
    const type = await list(p).evaluate(el => { const body = getComputedStyle(document.body).fontFamily; return [...el.querySelectorAll('.chat-section-toggle, .chat-link, .chat-row-state')].filter(item => { const style = getComputedStyle(item); return style.fontFamily !== body || style.textTransform !== 'none' || parseFloat(style.letterSpacing) > 0; }).map(item => item.textContent); });
    assert.deepEqual(type, []);
    assert.equal(await p.locator('#chat-list .chat-link[aria-current="page"] .chat-row-title').textContent(), 'Fixture café review');
    assert.equal(await fitsWidth(p), true);
    await shot(p, 'sidebar-1600'); await shot(p, 'sidebar-1600-list', { x: 0, y: 0, width: 420, height: 1000 });
  } finally { await h.close(); }
});

test('the list draws the template the harness serves: the job-shop template brings Office, Dispatch and Desk', async () => {
  const shell = shellOf(jobshop), office = chat('org-office', 'Office', 1791600200, { kind: 'organization_chat', organization_purpose: 'office' });
  assert.deepEqual(shell.sections.map(part => part.key), ['Organization', 'Dispatch', 'Desk', 'Recent']);
  for (const width of [1600, 390]) {
    const h = await appFor(width, { harness: shell, chats: [office], agents: [saved('agent-desk1', 'chat-desk1', 'Laser queue', 'tracker', 'active')], ...(width === 390 ? { start: '/' } : {}) }), p = h.page;
    try {
      if (width === 390) { await p.locator('#rail-chats').click(); await list(p).locator('[data-chat-section="recent"] .chat-row').first().waitFor(); }
      assert.deepEqual(await sectionOrder(p), ['organization', 'dispatch', 'desk', 'recent']);
      assert.deepEqual(await list(p).locator('.chat-list-section > h2').allInnerTexts(), ['Organization', 'Dispatch', 'Desk', 'Recent']);
      assert.deepEqual(await rows(p, 'organization'), ['Organization', 'Office'], 'the fixed chats are the template’s: the Cameras row belongs to retail');
      assert.deepEqual(await rows(p, 'desk'), ['Laser queue · Active'], 'a stored tracker sits under the label its template gives the class');
      assert.deepEqual(await rows(p, 'dispatch'), ['New dispatch agent']);
      // Each row draws the icon its template names; a name the page has no drawing for takes the chat mark.
      assert.deepEqual(await list(p).locator('[data-chat-section="organization"] .chat-row use').evaluateAll(items => items.map(item => item.getAttribute('href'))), ['#i-building', '#i-chat']);
      assert.equal(await p.locator('#settings-cameras').getAttribute('data-settings-available'), 'false', 'the Cameras page belongs to a template that ships it');
      assert.equal(await p.locator('#cameras-nav').isVisible(), false);
      assert.equal(await fitsWidth(p), true);
      await shot(p, 'jobshop-sidebar-' + width, width === 1600 ? { x: 0, y: 0, width: 420, height: 1000 } : undefined);
      if (width === 390) continue;
      await shot(p, 'jobshop-1600');
      await list(p).getByRole('button', { name: 'New dispatch agent', exact: true }).click();
      assert.equal(await p.locator('#message').inputValue(), shell.sections[1].starter_prompt);
      assert.match(shell.sections[1].starter_prompt, /^Help me create a Dispatch agent\b/);
      // The Office row opens the company's Office conversation.
      await list(p).locator('[data-chat-section="organization"] .chat-row').nth(1).getByRole('button').click();
      await p.waitForFunction(() => document.querySelector('#chat-title').textContent === 'Office');
      assert.deepEqual(h.writes, []);
    } finally { await h.close(); }
  }
});

test('a reader who can only look sees each empty section with the line its template wrote', async () => {
  for (const [shell, lines] of [[shellOf(jobshop), ['Dispatch agents appear here.', 'Desks appear here.']], [retail(), ['Action agents appear here.', 'Trackers appear here.']]]) {
    const h = await appFor(1600, { harness: shell, agents: [coreTracker], session: { can_query: false }, start: '/' }), p = h.page;
    try {
      await list(p).locator('.chat-section-empty').nth(1).waitFor({ state: 'attached' });
      assert.deepEqual(await list(p).locator('.chat-list-section:not([data-chat-section="recent"]) .chat-section-empty').allInnerTexts(), lines);
      assert.equal(await list(p).locator('.chat-row-new').count(), 0, 'starting an agent belongs to a person who can ask');
    } finally { await h.close(); }
  }
});

test('the retail template served by the route draws Cameras, Messaging setup, Action and Tracker', async () => {
  const h = await appFor(1600, { harness: retail() }), p = h.page;
  try {
    assert.deepEqual(await list(p).locator('.chat-list-section > h2').allInnerTexts(), ['Organization', 'Action', 'Tracker', 'Recent']);
    assert.deepEqual(await rows(p, 'organization'), ['Organization', 'Cameras', 'Messaging setup']);
    assert.deepEqual(await rows(p, 'action'), ['Notify supplier · Paused']); assert.deepEqual(await rows(p, 'tracker'), ['Queue watch · Active']);
    assert.equal(await p.locator('#settings-cameras').getAttribute('data-settings-available'), 'true');
    await shot(p, 'retail-1600'); await shot(p, 'retail-sidebar-1600', { x: 0, y: 0, width: 420, height: 1000 });
  } finally { await h.close(); }
  const phone = await appFor(390, { harness: retail(), start: '/' });
  try {
    await phone.page.locator('#rail-chats').click(); await list(phone.page).locator('[data-chat-section="recent"] .chat-row').first().waitFor();
    assert.deepEqual(await rows(phone.page, 'organization'), ['Organization', 'Cameras', 'Messaging setup']);
    assert.equal(await fitsWidth(phone.page), true); await shot(phone.page, 'retail-sidebar-390');
  } finally { await phone.close(); }
});

test('until a shell lands the page draws what every template has: the Organization chat and Recent', async () => {
  const lost = { ...retail(), sections: retail().sections.map(part => part.type==='agent_class' ? {...part, default:false} : part) };
  const twice = { ...shellOf(jobshop), sections: shellOf(jobshop).sections.map(part => part.kind === 'tracker' ? { ...part, key: 'Recent' } : part) };
  for (const [name, options] of [['a failed answer', { harnessRoute: { status: 500 } }], ['a shell that lost its default class', { harness: lost }], ['another schema', { harness: { ...shellOf(jobshop), schema: 'rocoto.harness-shell.v0' } }],
    ['a class that takes the name of a fixed section', { harness: twice }], ['a service from before the route', { harness: {} }]]) {
    for (const width of name === 'a failed answer' ? [1600, 390] : [1600]) {
      const h = await appFor(width, { ...options, early: true, ...(width === 390 ? { start: '/' } : {}) }), p = h.page;
      try {
        if (width === 390) { await p.locator('#rail-chats').click(); await list(p).locator('[data-chat-section="recent"] .chat-row').first().waitFor(); }
        assert.deepEqual(await list(p).locator('.chat-list-section > h2').allInnerTexts(), ['Organization', 'Recent'], name);
        assert.deepEqual(await rows(p, 'organization'), ['Organization'], name + ': the rows of one segment stay with its shell');
        assert.deepEqual(await rows(p, 'recent'), ['Morning rush review', 'Fridge door watch', 'Fixture café review'], name);
        assert.equal(await list(p).locator('.chat-row-new, [data-agent-kind]').count(), 0, name);
        assert.equal(await p.locator('#settings-cameras').getAttribute('data-settings-available'), 'true', name + ': a page of the service stays as shipped until a shell decides');
        assert.equal(await fitsWidth(p), true);
        if (name === 'a failed answer') await shot(p, 'stand-in-' + width, width === 1600 ? { x: 0, y: 0, width: 420, height: 1000 } : undefined);
      } finally { await h.close(); }
    }
  }
});

test('chats load while the shell is on its way, a failed read is tried again, and the list is drawn again when the shell lands', async () => {
  // A slow answer: the saved chat is on the page first, on the rows every template has.
  const slow = { wait: 7000 };
  const h = await appFor(1600, { harness: shellOf(jobshop), harnessRoute: slow, early: true, agents: [saved('agent-desk1', 'chat-desk1', 'Laser queue', 'tracker', 'active')] }), p = h.page;
  try {
    assert.equal(await p.locator('#messages .message').count(), 2, 'the conversation is drawn before the shell');
    assert.deepEqual(await sectionOrder(p), ['organization', 'recent']); assert.equal(slow.reads, 1);
    await list(p).locator('[data-chat-section="desk"] .chat-row').first().waitFor({ timeout: 12000 });
    assert.deepEqual(await sectionOrder(p), ['organization', 'dispatch', 'desk', 'recent']);
    assert.deepEqual(await rows(p, 'desk'), ['Laser queue · Active']);
    assert.equal(await p.locator('#messages .message').count(), 2);
  } finally { await h.close(); }
  // An answer that never comes leaves the page working on those rows, and it keeps the company chats the service lists,
  // whatever their purpose. (tests/ui_tracker_canvas.cjs starts a tracker chat from Home on a fixture with no shell.)
  const never = { wait: 60000 }, held = await appFor(1600, { harness: shellOf(jobshop), harnessRoute: never, early: true }), q = held.page;
  try {
    assert.equal(await q.locator('#messages .message').count(), 2);
    assert.deepEqual(await rows(q, 'recent'), ['Morning rush review', 'Fridge door watch', 'Fixture café review']);
    assert.equal(await q.locator('#chat-title').textContent(), 'Fixture café review');
    await q.goto(origin + '/?chat=org-cameras'); await q.waitForFunction(() => document.querySelector('#chat-title').textContent === 'Camera setup');
    assert.deepEqual(held.writes, []);
  } finally { await held.close(); }
  // A failed read is read again: the second answer brings the template's rows.
  const flaky = { fail: 1 }, again = await appFor(1600, { harness: shellOf(jobshop), harnessRoute: flaky, early: true }), r = again.page;
  try {
    await list(r).locator('[data-chat-section="dispatch"]').waitFor({ state: 'attached', timeout: 8000 });
    assert.deepEqual(await rows(r, 'organization'), ['Organization', 'Office']); assert.equal(flaky.reads, 2);
  } finally { await again.close(); }
});

test('the shell a company last drew is kept in the browser and stands in on the next visit', async () => {
  const route = {}, h = await appFor(1600, { harness: shellOf(jobshop), harnessRoute: route, agents: [saved('agent-desk1', 'chat-desk1', 'Laser queue', 'tracker', 'active')] }), p = h.page;
  try {
    assert.deepEqual(await sectionOrder(p), ['organization', 'dispatch', 'desk', 'recent']);
    const kept = await p.evaluate(() => JSON.parse(localStorage.getItem('rocoto-harness-shells')));
    assert.deepEqual(Object.keys(kept), ['demo'], 'one shell for each company');
    assert.deepEqual(kept.demo.fixed_chats, shellOf(jobshop).fixed_chats, 'names, jobs and labels: a conversation id stays with the service');
    assert.ok(kept.demo.fixed_chats.every(item => !('id' in item)));
    route.status = 500;
    await p.reload(); await list(p).locator('[data-chat-section="desk"] .chat-row').first().waitFor();
    assert.deepEqual(await list(p).locator('.chat-list-section > h2').allInnerTexts(), ['Organization', 'Dispatch', 'Desk', 'Recent']);
    assert.deepEqual(await rows(p, 'organization'), ['Organization', 'Office']);
    assert.equal(await p.locator('#settings-cameras').getAttribute('data-settings-available'), 'false');
  } finally { await h.close(); }
});

test('the camera and messaging entries follow the shell: a template without them keeps to its own fixed chats', async () => {
  // Retail: Settings, Messaging offers the member setup chat, and the row opens the purpose the shell names.
  const shop = await appFor(1600, { harness: retail(), organization: true, start: '/?settings=messaging' }), p = shop.page;
  try {
    await p.locator('#messaging-view').getByRole('button', { name: 'Messaging setup chat', exact: true }).click();
    await p.waitForFunction(() => new URL(location.href).searchParams.get('chat') === 'org-messaging');
    assert.deepEqual(shop.writes.map(write => [write.path, write.body.purpose]), [['/api/organization/chats', 'messaging']]);
  } finally { await shop.close(); }
  // The job shop has neither a member setup chat nor a Cameras page.
  const office = await appFor(1600, { harness: shellOf(jobshop), organization: true, start: '/?settings=messaging' }), q = office.page;
  try {
    await list(q).locator('[data-chat-section="dispatch"]').waitFor({ state: 'attached' });
    await q.locator('#messaging-view h1').waitFor();
    assert.equal(await q.locator('#messaging-view').getByRole('button', { name: 'Messaging setup chat', exact: true }).count(), 0);
    assert.deepEqual(office.writes, []);
  } finally { await office.close(); }
  const cameras = await appFor(1600, { harness: shellOf(jobshop), start: '/?cameras=1' }), r = cameras.page;
  try {
    await list(r).locator('[data-chat-section="dispatch"]').waitFor({ state: 'attached' });
    await r.locator('#workspace-tab-view').waitFor();
    assert.equal(await r.locator('#cameras-view').isVisible(), false, 'the Cameras page belongs to a template that ships it');
    assert.equal(new URL(r.url()).searchParams.has('cameras'), false);
  } finally { await cameras.close(); }
  const shopCameras = await appFor(1600, { harness: retail(), start: '/?cameras=1' }), t = shopCameras.page;
  try {
    await list(t).locator('[data-chat-section="tracker"]').waitFor({ state: 'attached' });
    assert.equal(await t.locator('#cameras-view').isVisible(), true); assert.equal(new URL(t.url()).searchParams.get('cameras'), '1');
  } finally { await shopCameras.close(); }
});

test('Organization and Cameras stay fixed at the top; other rows keep their own controls', async () => {
  const h = await appFor(1600), p = h.page;
  try {
    const fixed = list(p).locator('[data-chat-section="organization"] .chat-row');
    assert.equal(await fixed.count(), 3);
    assert.deepEqual(await fixed.evaluateAll(items => items.map(item => [item.dataset.fixed, item.querySelectorAll('button').length, item.querySelectorAll('.chat-row-actions').length].join(' '))), ['true 1 0', 'true 1 0', 'true 1 0'], 'each fixed row is its one opening button');
    assert.equal(await list(p).locator('[data-chat-section="organization"]').getByRole('button', { name: /^(Pin|Unpin|Archive|Move|Delete|Remove)\b/ }).count(), 0);
    assert.equal(await list(p).getByRole('button', { name: /^(Pin|Unpin)\b/ }).count(), 0, 'the list has no Pinned section to move a row into');
    assert.equal(await list(p).locator('.chat-list-section').first().getAttribute('data-chat-section'), 'organization');
    const recent = list(p).locator('[data-chat-section="recent"] .chat-row', { hasText: 'Morning rush review' });
    await recent.hover(); assert.equal(await recent.getByRole('button', { name: 'Archive Morning rush review', exact: true }).isVisible(), true);
    // The fixed rows open their conversations.
    await fixed.nth(1).getByRole('button').click();
    await p.waitForFunction(() => document.querySelector('#chat-title').textContent === 'Camera setup');
    assert.equal(await fixed.nth(1).locator('.chat-link').getAttribute('aria-current'), 'page');
    assert.deepEqual(h.writes, []);
  } finally { await h.close(); }
});

test('a chat that makes itself a tracker leaves Recent and appears under Tracker', async () => {
  const h = await appFor(1600, { start: '/?chat=chat-fridge', promote: 'chat-fridge' }), p = h.page;
  try {
    await p.locator('#messages .message').first().waitFor();
    assert.deepEqual(await rows(p, 'recent'), ['Morning rush review', 'Fridge door watch', 'Fixture café review']);
    assert.deepEqual(await rows(p, 'tracker'), ['Queue watch · Active']);
    await p.locator('#message').fill('Save this as a tracker for the fridge door.'); await p.locator('#send').click();
    await list(p).locator('[data-chat-section="tracker"] .chat-row', { hasText: 'Fridge door watch' }).locator('.chat-row-state', { hasText: 'Draft' }).waitFor();
    assert.deepEqual(await rows(p, 'tracker'), ['Queue watch · Active', 'Fridge door watch · Draft']);
    assert.deepEqual(await rows(p, 'recent'), ['Morning rush review', 'Fixture café review']);
    assert.deepEqual(await rows(p, 'action'), ['Notify supplier · Paused']);
    assert.equal(await list(p).locator('[data-chat-section="tracker"] .chat-link[aria-current="page"] .chat-row-title').textContent(), 'Fridge door watch', 'the list stays beside the agent’s conversation');
    assert.equal(await list(p).isVisible(), true);
    assert.equal(await p.locator('#agents-nav').getAttribute('aria-current'), 'page');
    assert.equal(await fitsWidth(p), true); await shot(p, 'tracker-conversation-1600');
  } finally { await h.close(); }
});

test('an empty Action or Tracker section offers one row that starts a new one', async () => {
  const h = await appFor(1600, { agents: [coreTracker] }), p = h.page;
  try {
    assert.deepEqual(await sectionOrder(p), ['organization', 'action', 'tracker', 'recent']);
    assert.deepEqual(await rows(p, 'action'), ['New action agent']); assert.deepEqual(await rows(p, 'tracker'), ['New tracker']);
    await list(p).getByRole('button', { name: 'New tracker', exact: true }).click();
    assert.match(await p.locator('#message').inputValue(), /^Help me create a Tracker Agent for this location\./);
    await list(p).getByRole('button', { name: 'New action agent', exact: true }).click();
    assert.match(await p.locator('#message').inputValue(), /^Help me create an Action Agent\b/);
    assert.deepEqual(h.writes, []);
  } finally { await h.close(); }
});

test('each section collapses, remembers its state and is reached from the keyboard', async () => {
  const h = await appFor(1600), p = h.page;
  try {
    assert.equal(await list(p).locator('button').evaluateAll(items => items.every(item => item.tabIndex === 0 && item.type === 'button')), true);
    const toggle = list(p).getByRole('button', { name: 'Tracker', exact: true }), contents = list(p).locator('#chat-section-tracker');
    assert.equal(await toggle.getAttribute('aria-expanded'), 'true');
    await toggle.focus(); await p.keyboard.press('Enter');
    assert.equal(await toggle.getAttribute('aria-expanded'), 'false'); assert.equal(await contents.isVisible(), false);
    assert.equal(await p.evaluate(() => document.activeElement.textContent), 'Tracker', 'the heading keeps the focus it was given');
    await p.reload(); await list(p).locator('[data-agent-kind="action"]').waitFor();
    assert.equal(await list(p).getByRole('button', { name: 'Tracker', exact: true }).getAttribute('aria-expanded'), 'false');
    assert.equal(await list(p).locator('#chat-section-tracker').isVisible(), false); assert.equal(await list(p).locator('#chat-section-action').isVisible(), true);
    await list(p).getByRole('button', { name: 'Tracker', exact: true }).focus(); await p.keyboard.press('Enter');
    await p.keyboard.press('Tab'); assert.match(await p.evaluate(() => document.activeElement.textContent), /^Queue watch/);
    await p.keyboard.press('Enter'); await list(p).locator('[data-chat-section="tracker"] .chat-link[aria-current="page"]', { hasText: 'Queue watch' }).waitFor();
    assert.equal(new URL(p.url()).searchParams.get('agent'), 'agent-4f0c', 'the row opens the agent’s conversation');
  } finally { await h.close(); }
});

test('the rail holds Home, Agents and Connections; Home is selected first and retired entries are gone', async () => {
  for (const width of [1600, 390]) {
    const h = await appFor(width, { start: '/' }), p = h.page;
    try {
      await p.locator('#workspace-tab-view').waitFor();
      // The destinations, top to bottom; the account and the founder's demo switch sit apart at the foot of the rail.
      const entries = await p.locator('#sidebar > button:visible:not(#founder-nav, #account-button)').evaluateAll(items => items.map(item => item.getAttribute('aria-label')));
      assert.deepEqual(entries, width === 390 ? ['Home', 'Your Chats', 'Agents', 'Connections'] : ['Home', 'Agents', 'Connections']);
      assert.equal(await p.locator('#new-chat').getAttribute('aria-current'), 'page'); assert.equal(await p.locator('#agents-nav').getAttribute('aria-current'), 'false');
      for (const id of ['quick-search-nav', 'library-nav']) assert.equal(await p.locator('#' + id).count(), 0, id + ' left the navigation');
      assert.deepEqual(await p.locator('#sidebar button, #sidebar a').evaluateAll(items => items.map(item => (item.getAttribute('aria-label') || '') + ' ' + item.textContent).filter(text => /quick search|gallery|library|ingest|moment|pattern/i.test(text))), []);
      await p.locator('#marketplace-nav').click(); await p.locator('#marketplace-view').getByRole('heading', { name: 'Connections', exact: true }).waitFor();
      assert.equal(await p.locator('#marketplace-nav').getAttribute('aria-current'), 'page');
      if (width === 390) await p.keyboard.press('Escape');
      await p.locator('#agents-nav').click(); await p.locator('.agent-collection-card', { hasText: 'Queue watch' }).waitFor();
      assert.equal(await p.locator('#agents-nav').getAttribute('aria-current'), 'page'); assert.equal(await p.locator('#new-chat').getAttribute('aria-current'), 'false');
      assert.equal(await fitsWidth(p), true);
      if (width === 1600) { assert.deepEqual(await sectionOrder(p), ['organization', 'action', 'tracker', 'recent']); assert.equal(await list(p).isVisible(), true, 'Agents opens beside its four sections'); await shot(p, 'agents-1600'); }
      else {
        await p.locator('#rail-chats').click(); await list(p).locator('[data-chat-section="recent"] .chat-row').first().waitFor();
        assert.deepEqual(await rows(p, 'tracker'), ['Queue watch · Active']);
        const frame = await list(p).boundingBox(); assert.ok(frame.x + frame.width <= 390, 'the list sits inside the phone');
        assert.deepEqual(await list(p).locator('.chat-link').evaluateAll(items => items.filter(item => item.scrollWidth > item.clientWidth + 1).map(item => item.textContent)), []);
        await shot(p, 'sidebar-390');
      }
    } finally { await h.close(); }
  }
});


test('a fixed conversation introduces the job declared by its vertical', async () => {
  const shell = shellOf(jobshop), declared = shell.fixed_chats.find(item => item.purpose === 'office');
  const manifest = JSON.parse(fs.readFileSync(path.join(jobshop, 'vertical.json')));
  const expected = manifest.fixed_chats.find(item => item.purpose === 'office').job;
  assert.equal(declared.job, expected, 'the shell carries the vertical job to the page');
  const office = chat('org-office', 'Office', 1791600200, { kind: 'organization_chat', organization_purpose: 'office', messages: [], read_only: false });
  const h = await appFor(1600, { harness: shell, chats: [office], start: '/?chat=org-office' });
  try {
    await h.page.locator('#welcome').waitFor();
    await h.page.waitForFunction(text => document.querySelector('#welcome > p').textContent === text, expected);
    assert.equal(await h.page.locator('#welcome > p').textContent(), expected);
    await shot(h.page, 'vertical-chat-1600');
  } finally { await h.close(); }
});

test('a records agent uses its readiness message and the vertical setup actions', async () => {
  const notice = 'Connect your order records to start this job.';
  const recordAgent = { ...saved('agent-records', 'chat-records', 'Order follow-up', 'action', 'paused'),
    input_kind: 'tracker_events', mode: 'scheduled', execution: { configurable: true, ready: false, notice } };
  for (const width of [1600, 390]) {
    const h = await appFor(width, { harness: shellOf(jobshop), agents: [recordAgent], readiness: { configurable: true, ready: true, notice: null }, start: '/?agents=1&agent=agent-records' });
    try {
      await h.page.getByRole('button', { name: 'Agent info', exact: true }).waitFor();
      await h.page.locator('#agent-setup-notice').waitFor();
      assert.match(await h.page.locator('#agent-setup-notice').textContent(), /Connect your order records/);
      await h.page.getByRole('button', { name: 'Agent info', exact: true }).click();
      await h.page.locator('.agent-readiness-note').waitFor();
      assert.equal(await h.page.locator('.agent-readiness-note p').textContent(), notice);
      assert.equal(await h.page.getByRole('button', { name: 'Activate', exact: true }).getAttribute('title'), notice);
      assert.equal(await h.page.getByRole('button', { name: 'Connect cameras', exact: true }).count(), 0);
      await shot(h.page, 'records-agent-' + width);
    } finally { await h.close(); }
  }
});

test('the bare workspace opens Organization and its single declared agent class', async()=>{
 for(const width of [1600,390]){
  const h=await appFor(width,{harness:shellOf('bare'),agents:[],start:'/',bareAssets:true,emptyHistory:true}),p=h.page;
  try{
   await p.locator('#workspace-tab-view .workspace-home').waitFor().catch(async error=>{throw new Error([...h.errors,await p.locator('#session-message').textContent(),error.message].join('\n'));});
   assert.equal(await p.locator('#workspace-tab-view button').innerText(),'Open Organization');
   assert.deepEqual(await p.evaluate(()=>[typeof window.RocotoCameraStreams,typeof window.RocotoTrackerCanvas,typeof window.RocotoRetailScope]),['undefined','undefined','undefined']);
   assert.equal(await p.locator('#cameras-view,#camera-provider-dialog,#clip-dialog,#source-upload-dialog,#chat-location-scope').count(),0);
   await shot(p,'bare-home-'+width);
   if(width===390)await p.locator('#rail-chats').click();
   assert.deepEqual(await sectionOrder(p),['organization','agent','recent']);
   assert.equal(await p.locator('#settings-cameras').count(),0);
   await p.locator('#account-button').click();await p.locator('#open-settings').click();
   assert.equal(await p.locator('#settings-cameras').count(),0);
   assert.doesNotMatch(await p.locator('#settings-nav').textContent(),/Cameras/);
   assert.deepEqual(await p.evaluate(()=>performance.getEntriesByType('resource').map(row=>new URL(row.name).pathname).filter(path=>path.startsWith('/static/retail/'))),[]);
   assert.equal(await fitsWidth(p),true);
   await shot(p,'bare-'+width);
  }finally{await h.close();}
 }
});

test('bare agent controls and status load from shared assets with generic fields',async()=>{
 const agent={...saved('bare-job','bare-conversation','Order follow-up','action','paused'),input_kind:'none',mode:'scheduled',max_runs:3,runs_started:8,interval_seconds:3600,grant:{kind:'standing'},execution:{configurable:true,ready:true}};
 for(const width of [1600,390]){const h=await appFor(width,{harness:shellOf('bare'),agents:[agent],start:'/?agents=1&agent=bare-job',bareAssets:true,emptyHistory:true}),p=h.page;try{
  await p.getByRole('button',{name:'Agent info',exact:true}).click();const dialog=p.locator('#agent-info-dialog');await dialog.waitFor();
  assert.equal(await dialog.getByLabel('Runs / day',{exact:true}).inputValue(),'3');assert.equal(await dialog.getByRole('button',{name:'Activate',exact:true}).isEnabled(),true);
  assert.equal(await p.locator('#agent-fresh-inspections,#agent-credit-estimate,#corpus-summary,#corpus-list').count(),0);
  assert.doesNotMatch(await dialog.textContent(),/Footage|Cosmos|GPU credit|Tracking Agent|evidence clips/i);
  assert.doesNotMatch(await p.locator('#composer-note').textContent(),/footage/i);assert.equal(await fitsWidth(p),true);await shot(p,'bare-agent-info-'+width);
 }finally{await h.close();}}
});


test('a streamed agent preserves an arbitrary class declared by the selected vertical',async()=>{
 const shell=shellOf('bare'),definition=shell.sections.find(item=>item.type==='agent_class');Object.assign(definition,{kind:'order_desk',key:'Order desk',create_label:'New order desk',empty_label:'Order desks appear here.'});
 const h=await appFor(390,{harness:shell,agents:[],start:'/?chat=chat-fridge',promote:'chat-fridge',promoteKind:'order_desk',bareAssets:true}),p=h.page;try{
  await p.locator('#messages .message').first().waitFor();await p.locator('#message').fill('Save this order follow-up agent.');await p.locator('#send').click();
  await p.waitForFunction(()=>Object.entries(localStorage).filter(([key])=>key.startsWith('rocoto-video-chats-v1')).some(([,value])=>JSON.parse(value).some(chat=>chat.id==='chat-fridge'&&chat.agent_kind==='order_desk')));
  if(!await p.locator('#chat-list').isVisible())await p.locator('#rail-chats').click();
  assert.equal(await list(p).locator('[data-agent-kind="order_desk"] .chat-row-state').textContent(),'Draft');assert.equal(await list(p).locator('[data-agent-kind="tracker"]').count(),0);
 }finally{await h.close();}
});

test('bare custom connectors use shared form fields without retail storage assets',async()=>{
 for(const width of [1600,390]){
  const h=await appFor(width,{harness:shellOf('bare'),agents:[],start:'/',bareAssets:true,emptyHistory:true}),p=h.page;
  try{
   await p.locator('#marketplace-nav').click();await p.keyboard.press('Escape');
   await p.locator('#add-custom-connector').click();
   const dialog=p.locator('#custom-connector-dialog');await dialog.waitFor();
   assert.equal(await p.locator('#custom-connector-title').textContent(),'Add Custom Connector');
   assert.equal(await p.locator('#custom-connector-kind').inputValue(),'api');
   assert.equal(await p.locator('#custom-connector-description').getAttribute('placeholder'),'Read records from our business system and help with the next step.');
   assert.doesNotMatch(await dialog.textContent(),/recording|folder imports|watch folder/i);
   assert.equal(await p.locator('.storage-setup').count(),0);
   assert.deepEqual(await p.evaluate(()=>performance.getEntriesByType('resource').map(row=>new URL(row.name).pathname).filter(path=>path.startsWith('/static/retail/'))),[]);
   assert.equal(await fitsWidth(p),true);await shot(p,'bare-custom-connector-'+width);
   await p.keyboard.press('Escape');assert.deepEqual(h.writes,[]);
  }finally{await h.close();}
 }
});
