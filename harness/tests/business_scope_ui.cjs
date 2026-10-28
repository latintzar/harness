/* Synthetic browser proofs for business locations and chat location selection. */
const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { chromium } = require('playwright');
const base = path.resolve(__dirname, '..');
const web = process.env.ROCOTO_SCOPE_TEST_WEB || require('./web_fixture.cjs').web();
const theme = JSON.parse(fs.readFileSync(path.join(base, 'theme.json')));
let server, browser, origin;
before(async () => {
  server = http.createServer((req, res) => {
    const route = new URL(req.url, 'http://localhost').pathname;
    if (route === '/') {
      res.setHeader('content-type', 'text/html');
      res.end('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/theme.css"><link rel="stylesheet" href="/static/retail/retail-scope.css"><script defer src="/static/retail/retail-scope.js"></script><main id="root"></main></html>'); return;
    }
    if (route === '/theme.css') {
      res.setHeader('content-type', 'text/css');
      res.end(':root{' + Object.entries({ ...theme.palette, ...theme.typography, radius: '12px', 'radius-lg': '20px' }).map(([k, v]) => '--' + k + ':' + v + ';').join('') + '}*{box-sizing:border-box}body{font:var(--font-size-ui) var(--font);margin:0;padding:36px 24px;background:var(--paper);color:var(--ink)}button,input,select{font:inherit}button{cursor:pointer}.agent-primary,.agent-secondary{padding:10px 14px;border:1px solid var(--border);border-radius:var(--radius);color:var(--ink);background:var(--paper)}.agent-primary{background:var(--ink);color:var(--paper)}@media(max-width:640px){body{padding:24px 16px}}'); return;
    }
    const file = path.resolve(web, '.' + route);
    if (!file.startsWith(web + path.sep) || !fs.existsSync(file)) { res.writeHead(404); res.end(); return; }
    res.setHeader('content-type', route.endsWith('.js') ? 'text/javascript' : 'text/css'); fs.createReadStream(file).pipe(res);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  origin = 'http://127.0.0.1:' + server.address().port;
  browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : {}), args: ['--no-sandbox'] });
});
after(async () => { await browser?.close(); await new Promise(resolve => server?.close(resolve)); });
async function pageFor(width = 1600) {
  const context = await browser.newContext({ viewport: { width, height: 1000 } });
  const page = await context.newPage(), external = [], errors = [];
  page.setDefaultTimeout(4000);
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/*', route => {
    if (!route.request().url().startsWith(origin + '/')) { external.push(route.request().url()); return route.abort(); }
    return route.continue();
  });
  await page.goto(origin);
  await page.waitForFunction(() => window.RocotoRetailScope);
  return { page, external, errors, close: () => context.close() };
}
test('scope renderer exposes a local callback-only surface', async () => {
  const h = await pageFor();
  try {
    assert.equal(await h.page.evaluate(() => typeof RocotoRetailScope.render), 'function');
    assert.deepEqual(h.external, []); assert.deepEqual(h.errors, []);
  } finally { await h.close(); }
});
function graph() {
  return { schema_version: 1, collection: 'company', revision: 7, configured: true, can_manage: true, can_manage_kinds: ['locations', 'functions', 'rooms', 'cameras', 'members'],
    locations: [{ id: 'north', name: 'North café', address: '10 Market Street' }, { id: 'south', name: 'South café', address: '22 High Street' }],
    functions: [{ id: 'cooking', name: 'Cooking' }, { id: 'service', name: 'Customer service' }],
    rooms: [{ id: 'kitchen', name: 'Kitchen', location_id: 'north', function_ids: ['cooking'] }, { id: 'counter', name: 'Counter', location_id: 'south', function_ids: ['service'] }],
    cameras: [{ id: 'camera-1', name: 'Kitchen camera', location_id: 'north', room_ids: ['kitchen'], view_revision: 3, source_ids: ['source-owned'] }],
    members: [{ member_id: 'owner', display_name: 'Alex Owner', account_role: 'owner', role: null, location_ids: [], function_ids: [] }, { member_id: 'colleague', display_name: 'Sam Cook', account_role: 'operator', role: 'colleague', location_ids: ['north'], function_ids: ['cooking'] }] };
}
async function scope(page, data = graph(), behavior = 'success') {
  await page.evaluate(({ data, behavior }) => {
    window.events = []; window.fixtureGraph = data;
    document.querySelector('#root').replaceChildren(RocotoRetailScope.render({ data, onSave: async (kind, value, revision) => {
      events.push({ kind, value, revision });
      if (behavior === 'hold') await new Promise(resolve => { window.resolveSave = resolve; });
      if (behavior === 'conflict') throw new Error('Locations changed. Refresh and save your edits again.');
      const next = structuredClone(fixtureGraph); next.revision++;
      if (kind === 'locations') next.locations.push({ ...value, id: value.id || 'host-location' });
      return next;
    } }));
  }, { data, behavior });
}
test('location creation carries exact revision and leaves stable IDs to the host', async () => {
  const h = await pageFor(); try {
    await scope(h.page); await h.page.getByRole('button', { name: 'Add location', exact: true }).click();
    await h.page.getByLabel('Name', { exact: true }).fill('East café'); await h.page.getByLabel('Address', { exact: true }).fill('3 River Road');
    await h.page.getByRole('button', { name: 'Save changes', exact: true }).click();
    await h.page.getByRole('heading', { name: 'East café', exact: true }).waitFor();
    assert.deepEqual(await h.page.evaluate(() => events), [{ kind: 'locations', value: { name: 'East café', address: '3 River Road' }, revision: 7 }]);
    assert.equal(await h.page.locator('dialog').count(), 0);
  } finally { await h.close(); }
});
test('room edits use real function IDs and camera moves use rooms from the chosen location', async () => {
  const h = await pageFor(); try {
    await scope(h.page); await h.page.getByRole('button', { name: 'Edit Kitchen', exact: true }).click();
    await h.page.getByLabel('Customer service', { exact: true }).check(); await h.page.getByRole('button', { name: 'Save changes', exact: true }).click();
    assert.deepEqual(await h.page.evaluate(() => events[0]), { kind: 'rooms', value: { id: 'kitchen', name: 'Kitchen', location_id: 'north', function_ids: ['cooking', 'service'] }, revision: 7 });
    await h.page.getByRole('button', { name: 'Edit Kitchen camera', exact: true }).click();
    await h.page.getByLabel('Location', { exact: true }).selectOption('south'); assert.equal(await h.page.getByLabel('Kitchen', { exact: true }).count(), 0);
    await h.page.getByLabel('Counter', { exact: true }).check(); await h.page.getByRole('button', { name: 'Save changes', exact: true }).click();
    assert.deepEqual(await h.page.evaluate(() => events[1]), { kind: 'cameras', value: { id: 'camera-1', name: 'Kitchen camera', location_id: 'south', room_ids: ['counter'], source_ids: ['source-owned'] }, revision: 8 });
  } finally { await h.close(); }
});
test('member access separates organization authority, scoped function roles and revocation', async () => {
  const h = await pageFor(); try {
    await scope(h.page); assert.equal(await h.page.getByRole('button', { name: 'Manage access for Alex Owner' }).count(), 0);
    await h.page.getByRole('button', { name: 'Manage access for Sam Cook' }).click();
    await h.page.getByLabel('Scope role', { exact: true }).selectOption('function_admin'); await h.page.getByLabel('Cooking', { exact: true }).uncheck();
    await h.page.getByRole('button', { name: 'Save changes', exact: true }).click(); await h.page.getByText('Choose a function for this function admin.', { exact: true }).waitFor(); assert.deepEqual(await h.page.evaluate(() => events), []);
    await h.page.getByLabel('North café', { exact: true }).uncheck(); await h.page.getByLabel('Scope role', { exact: true }).selectOption('colleague'); await h.page.getByRole('button', { name: 'Save changes', exact: true }).click();
    assert.deepEqual(await h.page.evaluate(() => events[0]), { kind: 'members', value: { member_id: 'colleague', role: 'colleague', location_ids: [], function_ids: [] }, revision: 7 });
  } finally { await h.close(); }
});
test('revision conflicts preserve drafts and pending writes disable repeat submission', async () => {
  const h = await pageFor(); try {
    await scope(h.page, graph(), 'conflict'); await h.page.getByRole('button', { name: 'Add function', exact: true }).click(); await h.page.getByLabel('Name', { exact: true }).fill('Storage');
    await h.page.getByRole('button', { name: 'Save changes', exact: true }).click(); await h.page.getByText('Locations changed. Refresh and save your edits again.', { exact: true }).waitFor(); assert.equal(await h.page.getByLabel('Name', { exact: true }).inputValue(), 'Storage');
    await h.page.getByRole('button', { name: 'Cancel', exact: true }).click(); await scope(h.page, graph(), 'hold');
    await h.page.getByRole('button', { name: 'Add function', exact: true }).click(); await h.page.getByLabel('Name', { exact: true }).fill('Storage'); await h.page.getByRole('button', { name: 'Save changes', exact: true }).click();
    assert.equal(await h.page.getByRole('button', { name: 'Save changes', exact: true }).isDisabled(), true); await h.page.keyboard.press('Escape'); assert.equal(await h.page.locator('dialog[open]').count(), 1);
    await h.page.evaluate(() => resolveSave()); await h.page.waitForFunction(() => !document.querySelector('dialog'));
    assert.equal(await h.page.evaluate(() => events.length), 1);
  } finally { await h.close(); }
});
test('host permissions control every mutation surface and hostile labels remain text', async () => {
  const h = await pageFor(); try {
    const data = graph(); data.can_manage_kinds = ['rooms']; data.locations[0].name = '<img src="https://attacker.invalid/x" onerror="alert(1)">'; data.functions[0].name = '<script>alert(1)</script>';
    await scope(h.page, data); assert.equal(await h.page.getByRole('button', { name: 'Add location', exact: true }).count(), 0); assert.equal(await h.page.getByRole('button', { name: 'Add function', exact: true }).count(), 0); assert.equal(await h.page.getByRole('button', { name: 'Add room', exact: true }).count(), 2);
    assert.equal(await h.page.locator('#root img,#root script').count(), 0); assert.deepEqual(h.external, []);
    data.can_manage = false; await scope(h.page, data); assert.equal(await h.page.locator('#root button').count(), 0); assert.deepEqual(await h.page.evaluate(() => events), []);
  } finally { await h.close(); }
});
test('location management fits desktop and mobile and keeps dialog keyboard focus', async () => {
  for (const width of [1600, 390, 320]) {
    const h = await pageFor(width); try {
      await scope(h.page); assert.equal(await h.page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      if (process.env.ROCOTO_SCOPE_PROOF_DIR) await h.page.screenshot({ path: path.join(process.env.ROCOTO_SCOPE_PROOF_DIR, `locations-${width}.png`), fullPage: true });
      await h.page.getByRole('button', { name: 'Manage access for Sam Cook', exact: true }).click();
      assert.equal(await h.page.locator('dialog').evaluate(el => el.scrollWidth <= el.clientWidth), true);
      if (process.env.ROCOTO_SCOPE_PROOF_DIR) await h.page.screenshot({ path: path.join(process.env.ROCOTO_SCOPE_PROOF_DIR, `member-access-${width}.png`) });
      await h.page.keyboard.press('Escape'); await h.page.waitForFunction(() => !document.querySelector('dialog')); assert.equal(await h.page.locator('dialog').count(), 0);
      assert.equal(await h.page.getByRole('button', { name: 'Manage access for Sam Cook', exact: true }).evaluate(el => el === document.activeElement), true);
      assert.deepEqual(h.errors, []);
    } finally { await h.close(); }
  }
});
function selectedScope(ids = null, revision = 7, selectionRevision = 2) { return { collection: 'company', configured: true, can_query: true, revision, selection_revision: selectionRevision, selected_location_ids: ids, location_ids: ids || ['north', 'south'] }; }
async function selector(page, options = {}) {
  await page.evaluate(({ data, locations, options }) => {
    window.events = []; window.scopeData = data;
    document.querySelector('#root').replaceChildren(RocotoRetailScope.renderSelector({ data, locations, ...options, onOpen: async () => ({ data: scopeData, locations }), onSave: async (...args) => { events.push(args); if (options.conflict) throw new Error('Locations changed. Reopen the selector to review the latest access.'); return { ...scopeData, location_ids: args[0] || locations.map(row => row.id), selected_location_ids: args[0], selection_revision: scopeData.selection_revision + 1 }; } }));
  }, { data: selectedScope(), locations: graph().locations, options });
}
test('chat selector saves all, one and multiple locations with both host revisions', async () => {
  const h = await pageFor(); try {
    await selector(h.page); await h.page.getByRole('button', { name: 'Chat locations: All locations', exact: true }).click();
    await h.page.getByLabel('All permitted locations', { exact: true }).uncheck(); await h.page.getByLabel('North café', { exact: true }).check(); await h.page.getByRole('button', { name: 'Apply locations', exact: true }).click();
    await h.page.getByRole('button', { name: 'Chat locations: North café', exact: true }).waitFor(); assert.deepEqual(await h.page.evaluate(() => events), [[['north'], 7, 2]]);
    await selector(h.page); await h.page.getByRole('button', { name: 'Chat locations: All locations', exact: true }).click(); await h.page.getByLabel('All permitted locations', { exact: true }).uncheck();
    assert.equal(await h.page.getByRole('button', { name: 'Apply locations', exact: true }).isDisabled(), true);
    await h.page.getByLabel('North café', { exact: true }).check(); await h.page.getByLabel('South café', { exact: true }).check(); await h.page.getByRole('button', { name: 'Apply locations', exact: true }).click();
    await h.page.getByRole('button', { name: 'Chat locations: 2 locations', exact: true }).waitFor(); assert.deepEqual(await h.page.evaluate(() => events), [[['north', 'south'], 7, 2]]);
    await selector(h.page); await h.page.getByRole('button', { name: 'Chat locations: All locations', exact: true }).click(); await h.page.getByRole('button', { name: 'Apply locations', exact: true }).click();
    assert.deepEqual(await h.page.evaluate(() => events), [[null, 7, 2]]);
  } finally { await h.close(); }
});
test('chat selector preserves choices after conflicts, closes on Escape and respects busy or read-only access', async () => {
  const h = await pageFor(); try {
    await selector(h.page, { conflict: true }); await h.page.getByRole('button', { name: 'Chat locations: All locations', exact: true }).click(); await h.page.getByLabel('South café', { exact: true }).uncheck(); await h.page.getByRole('button', { name: 'Apply locations', exact: true }).click();
    await h.page.getByText('Locations changed. Reopen the selector to review the latest access.', { exact: true }).waitFor(); assert.equal(await h.page.getByLabel('North café', { exact: true }).isChecked(), true); assert.equal(await h.page.getByLabel('South café', { exact: true }).isChecked(), false);
    await h.page.keyboard.press('Escape'); assert.equal(await h.page.locator('.chat-location-popover').evaluate(el => el.matches(':popover-open')), false);
    await selector(h.page, { busy: true }); assert.equal(await h.page.getByRole('button', { name: 'Chat locations: All locations', exact: true }).isDisabled(), true);
    await selector(h.page, { readOnly: true }); assert.equal(await h.page.getByRole('button', { name: 'Chat locations: All locations', exact: true }).isDisabled(), true);
  } finally { await h.close(); }
});
test('chat location popover stays within desktop and mobile viewport', async () => {
  for (const width of [1600, 390, 320]) {
    const h = await pageFor(width); try {
      await selector(h.page); await h.page.getByRole('button', { name: 'Chat locations: All locations', exact: true }).click(); await h.page.getByLabel('North café', { exact: true }).waitFor();
      const bounds = await h.page.locator('.chat-location-popover').boundingBox(); assert(bounds.x >= 0 && bounds.x + bounds.width <= width);
      assert.equal(await h.page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      if (process.env.ROCOTO_SCOPE_PROOF_DIR) await h.page.screenshot({ path: path.join(process.env.ROCOTO_SCOPE_PROOF_DIR, `chat-locations-${width}.png`) });
      assert.deepEqual(h.errors, []);
    } finally { await h.close(); }
  }
});
test('scoped administrators receive only assignable roles and their own membership stays read-only', async () => {
  const h = await pageFor(); try {
    const data = graph(); data.effective = { role: 'function_admin', principal_id: 'admin' }; data.can_manage_kinds = ['cameras', 'members']; data.members.push({ member_id: 'admin', display_name: 'Function Admin', account_role: 'operator', role: 'function_admin', location_ids: ['north'], function_ids: ['cooking'] });
    await scope(h.page, data); assert.equal(await h.page.getByRole('button', { name: 'Manage access for Function Admin', exact: true }).count(), 0);
    await h.page.getByRole('button', { name: 'Manage access for Sam Cook', exact: true }).click(); assert.deepEqual(await h.page.getByLabel('Scope role', { exact: true }).locator('option').evaluateAll(rows => rows.map(row => row.value)), ['colleague', 'alert_recipient']);
  } finally { await h.close(); }
});
