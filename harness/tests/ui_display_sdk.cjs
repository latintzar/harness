/* A manufacturing display uses only the common SDK, at the narrowest supported viewport. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const theme = JSON.parse(fs.readFileSync(path.join(root, 'theme.json')));
const css = ':root{' + Object.entries({ ...theme.palette, ...theme.typography }).map(([key, value]) => `--${key}:${value};`).join('') + '--radius-sm:8px;}body{margin:0;font-family:var(--font)}';
const assets = new Set(['display-canvas.js', 'display-canvas.css', 'display-charts.js', 'display-charts.css']);

test('the shared display renders owned manufacturing output and keeps its unit at 320px', async () => {
  const loaded = [], errors = [];
  const server = http.createServer((req, res) => {
    const name = new URL(req.url, 'http://localhost').pathname.slice(1);
    if (!name) { res.setHeader('Content-Type', 'text/html'); res.end('<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/theme.css"><link rel="stylesheet" href="/display-charts.css"><link rel="stylesheet" href="/display-canvas.css"><script defer src="/display-charts.js"></script><script defer src="/display-canvas.js"></script><main></main>'); return; }
    if (name === 'theme.css') { res.setHeader('Content-Type', 'text/css'); res.end(css); return; }
    if (!assets.has(name)) { res.writeHead(404); res.end(); return; }
    loaded.push(name); res.setHeader('Content-Type', name.endsWith('.css') ? 'text/css' : 'application/javascript'); res.end(fs.readFileSync(path.join(root, 'web', name)));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || '/snap/bin/chromium', args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  try {
    for (const width of [320, 390, 1600]) {
      const page = await browser.newPage({ viewport: { width, height: 900 }, reducedMotion: 'reduce' });
      page.on('pageerror', error => errors.push(error.message));
      await page.route('**/*', route => route.request().url().startsWith(origin + '/') ? route.continue() : route.abort());
      await page.goto(origin); await page.waitForFunction(() => window.RocotoDisplayCanvas);
      const result = await page.evaluate(() => {
        const points = [{ t: '2026-10-11T12:00:00Z', value: 12 }, { t: '2026-10-11T13:00:00Z', value: 14 }];
        const presentation = { schema: 'rocoto.display-presentation.v1', revision: 2, tile: { label: 'Orders ready', mark: 'line', format: 'count', unit: 'orders', bind: { series: 'series' } },
          canvas: { family: 'metric', blocks: [{ block: 'figure' }, { block: 'line', bind: { series: 'series' } }, { block: 'summary', bind: { text: 'summary' } }, { block: 'instances', bind: { rows: 'rows' } }] } };
        const display = { display_id: 'orders-ready', owner_agent_id: 'agent-production', revision: 4, presentation,
          tile: { label: 'Orders ready', value: 14, unit: 'orders', kind: 'count', mark: 'line', series: points },
          canvas: { value: 14, series: points, summary: 'Fourteen orders are ready. [External](https://example.test/)', rows: [{ id: 'order-1', description: 'Order 1 is ready.', at: '2026-10-11T13:00:00Z', href: '/orders/1' }] } };
        const kit = window.RocotoDisplayCanvas;
        window.canvas = kit.mountDisplay({ display, phone: false });
        window.tile = kit.tileDisplay({ display });
        document.querySelector('main').append(window.canvas.element);
        window.savedDisplay = display;
        return { families: kit.FAMILIES, retailLoaded: !!window.RocotoTrackerCanvas, owner: window.canvas.element.dataset.ownerAgentId, tileOwner: window.tile.element.dataset.ownerAgentId, revision: window.canvas.output };
      });
      assert.deepEqual(result, { families: ['metric', 'live', 'occurrences'], retailLoaded: false, owner: 'agent-production', tileOwner: 'agent-production', revision: 4 });
      await page.locator('.tc-chart .rocoto-chart-ink').waitFor();
      assert.equal(await page.locator('.tc-summary a').count(), 0);
      const box = await page.locator('.tc-figure').evaluate(node => { const [number, unit] = node.children; const n = number.getBoundingClientRect(), u = unit.getBoundingClientRect(); return { sameLine: u.top < n.bottom && u.bottom > n.top, right: Math.max(n.right, u.right), page: document.documentElement.scrollWidth }; });
      assert.ok(box.sameLine && box.right <= width + 1 && box.page <= width + 1, `${width}: ${JSON.stringify(box)}`);
      await page.evaluate(() => { const display = structuredClone(window.savedDisplay); display.revision = 5; display.tile.value = display.canvas.value = 18; window.canvas.update({ display }); });
      assert.equal(await page.locator('.tc-figure .tc-numeral').innerText(), '18');
      assert.equal(await page.locator('.tc-canvas').getAttribute('data-output'), '5');
      assert.equal(await page.evaluate(() => window.canvas.output), 5);
      await page.evaluate(() => window.canvas.destroy());
      assert.equal(await page.locator('.tc-canvas').count(), 0);
      await page.close();
    }
    assert.deepEqual(errors, []);
    assert.deepEqual([...new Set(loaded)].sort(), [...assets].sort());
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
});
