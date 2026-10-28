/* Browser proofs for the tracker canvases and their tiles (CONTROL-ROOM.md, "Tile and canvas contract").
 * The real shell (web/index.html) runs against fixture routes shaped like GET /api/agents and
 * GET /api/agents/{id}/observations; no provider, model or network call leaves the page.
 * Home is the location view only (owner, 10 October 2026): its tiles live in that view and ask the shell for a
 * tracker's canvas. A canvas is one of a set of families, each naming the data it needs; a Core Tracker's is locked.
 * NODE_PATH=/path/to/playwright/node_modules PLAYWRIGHT_CHROMIUM_EXECUTABLE=/path/to/chromium \
 *   node --test harness/tests/ui_tracker_canvas.cjs
 * ROCOTO_CANVAS_SCREENSHOTS names the folder for the 1600x1000 and 390x844 captures.
 * ROCOTO_CANVAS_MEDIA names a folder of stills and clips for the fixture events (optional).
 * ROCOTO_CANVAS_BUNDLE names a built viewer bundle for the Home location frame (optional).
 */
const { test, before, after } = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const { chromium } = require('playwright');

const base = path.resolve(__dirname, '..'), web = require('./web_fixture.cjs').web(), pub = path.resolve(base, '../app/public');
const retailHarness=JSON.parse(require('node:child_process').execFileSync(process.env.ROCOTO_UI_TEST_PYTHON || 'python3',['-c','import layout; layout.install(); import pack,json; print(json.dumps(pack.load("retail").shell()))'],{cwd:base,env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'},encoding:'utf8'}));
const theme = JSON.parse(fs.readFileSync(path.join(base, 'theme.json'))), catalogue = JSON.parse(fs.readFileSync(path.join(base, 'runtime/plugin_catalogue.json')));
const shots = process.env.ROCOTO_CANVAS_SCREENSHOTS || '', media = process.env.ROCOTO_CANVAS_MEDIA || '', bundle = process.env.ROCOTO_CANVAS_BUNDLE || '';
// The copy law, as tools/copy-gate.py reads it, and the provenance phrases a number never carries.
const NEGATIVE = /(^|[\s>«(·'"`])(no|not|nothing|never|nobody|none|don't|doesn't|can't|cannot|won't|isn't|aren't|didn't|wasn't|hasn't|haven't|without|unless)\b/i;
const PROVENANCE = /from \d+ recordings?|of \d+ interactions|saved observations|available recordings|demo footage|recorded replay|committed|source interval|evidence will appear/i;
const NOW = Date.parse('2026-10-10T13:42:00Z');   // Saturday 10 October 2026, 16:42 in Sofia
const PIXEL = Buffer.from('/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA=', 'base64');
const PLACE = { id: 'sofia', name: 'Vitosha Boulevard 85', address: '85 Vitosha Boulevard, Sofia', city: 'Sofia' };
let browser, server, origin;

function themeCss() {
  // server.py theme(), statement for statement: the brand block, the video palette, the two kit faces.
  const source = fs.readFileSync(path.resolve(base, '../app/src/rocoto-theme.ts'), 'utf8'), brand = source.split('export const BRAND: Brand = {')[1].split('};')[0];
  const values = Object.fromEntries(['ink', 'accent', 'paper'].map(name => [name, new RegExp(name + ":\\s*'([^']+)'").exec(brand)[1]]));
  const font = /const ROCOTO_SANS = '([^']+)'/.exec(source)[1], wordmark = /const ROCOTO_SERIF = '([^']+)'/.exec(source)[1];
  Object.assign(values, theme.palette);
  let css = "@font-face{font-family:Fraunces;src:url('/fonts/fraunces/fraunces-latin-500-700.woff2') format('woff2');font-style:normal;font-weight:500 700;font-display:swap;}:root{";
  css += Object.entries(values).map(([key, value]) => `--${key}:${value};`).join('');
  css += `--font:${font};--wordmark-font:${wordmark};--muted:color-mix(in srgb,var(--ink) 60%,var(--paper));--connection-online:${theme.connection_online};`;
  css += '--border:color-mix(in srgb,var(--ink) 14%,var(--paper));--surface:color-mix(in srgb,var(--ink) 3%,var(--paper));';
  css += ['font', 'font-code', 'font-size-ui', 'font-size-code'].map(key => `--${key}:${theme.typography[key]};`).join('') + '--radius:0.75rem;--radius-sm:0.5rem;--radius-lg:1.25rem;}';
  return css;
}
const TYPES = { '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.mp4': 'video/mp4', '.json': 'application/json', '.woff2': 'font/woff2' };
function send(req, res, file) {
  const size = fs.statSync(file).size, range = /^bytes=(\d*)-(\d*)$/.exec(req.headers.range || '');
  res.setHeader('Content-Type', TYPES[path.extname(file)] || 'application/octet-stream'); res.setHeader('Accept-Ranges', 'bytes'); res.setHeader('Cache-Control', 'no-store');
  if (!range) { res.setHeader('Content-Length', size); fs.createReadStream(file).pipe(res); return; }
  const start = Number(range[1] || 0), end = Math.min(range[2] ? Number(range[2]) : size - 1, size - 1);
  res.writeHead(206, { 'Content-Range': `bytes ${start}-${end}/${size}`, 'Content-Length': end - start + 1 }); fs.createReadStream(file, { start, end }).pipe(res);
}
const inside = (root, relative) => { const file = path.resolve(root, './' + relative); return file.startsWith(path.resolve(root) + path.sep) && fs.existsSync(file) && fs.statSync(file).isFile() ? file : null; };

before(async () => {
  server = http.createServer((req, res) => {
    const route = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
    if (route === '/theme.css' || route === '/static/harness/theme.css') { res.setHeader('Content-Type', 'text/css'); res.end(themeCss()); return; }
    // A stand-in for the location view: it records what the shell tells it, and the test speaks through it.
    if (route === '/frame-stub.html') { res.setHeader('Content-Type', 'text/html'); res.end('<!doctype html><meta charset="utf-8"><title>Location view</title><script>window.received=[];addEventListener("message",event=>{if(event.origin===location.origin&&event.source===parent)received.push(event.data);});window.ready=true;</script>'); return; }
    // The tiles on a bare page, as a location view draws them.
    if (route === '/tiles.html') { res.setHeader('Content-Type', 'text/html'); res.end('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/theme.css"><link rel="stylesheet" href="/display-canvas.css"><link rel="stylesheet" href="/tracker-canvas.css"><style>body{margin:0;padding:40px 24px;background:var(--camera-paper);font:var(--font-size-ui)/1.5 var(--font);color:var(--ink)}@media(max-width:640px){body{padding:20px 16px}}</style><script defer src="/display-canvas.js"></script><script defer src="/tracker-canvas.js"></script></head><body><main id="tiles" class="tc-tiles"></main></body></html>'); return; }
    // Home's tiles on a bare page: the location view with its own model, marks and styles.
    if (route === '/home-tiles.html') { res.setHeader('Content-Type', 'text/html'); res.end('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><base href="/control-room/"><link rel="stylesheet" href="/theme.css"><link rel="stylesheet" href="kit/rocoto-kit.css"><link rel="stylesheet" href="kit/twelvelabs-material.css"><link rel="stylesheet" href="/display-charts.css"><link rel="stylesheet" href="location-view.css"><style>html,body{margin:0;background:var(--camera-paper)}*{box-sizing:border-box}[hidden]{display:none!important}</style></head><body><div id="locationRoot"></div><script src="/display-charts.js"></script><script src="home-model.js"></script><script src="home-marks.js"></script><script src="location-view.js"></script></body></html>'); return; }
    if (route.startsWith('/media/')) {
      const file = media && inside(media, route.slice(7));
      if (file) return send(req, res, file);
      if (route.endsWith('.jpg')) { res.setHeader('Content-Type', 'image/jpeg'); res.end(PIXEL); return; }
      res.writeHead(404); res.end(); return;
    }
    if (route.startsWith('/control-room/')) {
      // The location frame: the product's page code, and a built bundle's data when one is named.
      const asset = route.slice(14) || 'index.html';
      if (asset === 'data.json' && bundle && inside(bundle, asset)) {
        const data = JSON.parse(fs.readFileSync(inside(bundle, asset))); data.location = { id: 'sofia', name: 'Vitosha Boulevard 85', weather: { name: 'Vitosha Boulevard 85', address: '85 Vitosha Boulevard, Sofia', city: 'Sofia', latitude: 42.6842677, longitude: 23.3162691, timezone: 'Europe/Sofia' } };
        res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(data)); return;
      }
      const file = inside(path.join(web, 'control-room'), asset) || (bundle && inside(bundle, asset));
      if (file) return send(req, res, file);
      res.writeHead(404); res.end(); return;
    }
    const file = inside(route.startsWith('/brand/') || route.startsWith('/connection-brands/') || route.startsWith('/fonts/') ? pub : web, route === '/' ? 'index.html' : route.slice(1));
    if (!file) { res.writeHead(404); res.end(); return; }
    send(req, res, file);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  origin = 'http://127.0.0.1:' + server.address().port;
  browser = await chromium.launch({ headless: true, ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE } : {}), args: ['--no-sandbox', '--disable-dev-shm-usage', '--autoplay-policy=no-user-gesture-required'] });
});
after(async () => { await browser?.close(); await new Promise(resolve => server?.close(resolve)); });

/* ---------- tracker records, shaped as the host answers them ---------- */
const sofia = (day, clock) => new Date(Date.parse(`2026-10-10T${clock}:00Z`) + day * 86400e3 - 3 * 3600e3);   // a Sofia wall-clock time on 10 October plus `day`
const STILLS = ['intel-clip2.jpg', 'shop-evidence/intel-clip14-8b-0-0-context.jpg', 'intel-clip8.jpg', 'shop-evidence/intel-clip2-8b-0-0-context.jpg', 'intel-clip14.jpg', 'shop-evidence/intel-clip2-8b-0-1-context.jpg', 'shop-evidence/intel-clip14-8b-0-2-context.jpg', 'shop-evidence/intel-clip2-8b-0-2-context.jpg', 'shop-evidence/intel-clip14-8b-0-1-context.jpg', 'shop-evidence/intel-clip14-8b-0-3-context.jpg'];
const CLIPS = ['intel-clip2', 'intel-clip14', 'intel-clip8', 'intel-clip2', 'intel-clip14', 'intel-clip2', 'intel-clip14', 'intel-clip2'];
let made = 0;
function event(profile, type, description, when, extra = {}) {
  const index = made++, clip = CLIPS[index % CLIPS.length], start = 1 + (index % 4), id = `event-${String(index).padStart(4, '0')}`;
  return { id, profile_id: profile, profile_revision: 1, event_key: `key-${index}`, type, description, source: { clip_id: clip, start_s: start, end_s: start + 6 + (index % 5) },
    track_id: `track-${index % 7}`, object_ref: null, object_description: null, bbox: null, owner_principal_id: 'owner', dataset: 'vitosha', visit_id: `visit-${index % 7}`, item_id: null,
    recording_id: clip, camera_id: 'camera-aisle', captured_at: when.toISOString(), committed_at: when.getTime() / 1000 + 4, origin: 'pipeline_observation', review_state: 'observation',
    evidence_url: `/?clip=${clip}`, thumbnail_url: `/media/${STILLS[index % STILLS.length]}`, media_url: `/media/${clip}.mp4`, source_sha256: 'a'.repeat(64), ...extra };
}
const profile = (id, name, types) => ({ id, name, dataset: 'vitosha', criteria: '', revision: 1, owner_principal_id: 'owner', camera_ids: ['camera-aisle'], location_id: 'sofia', room_id: null, updated_at: NOW / 1000 - 86400, vocabulary: types.map(([type, description]) => ({ type, description, group: 'shopper_journey' })) });
const readiness = { configurable: true, ready: true, notice: '' };
const agent = (id, title, description, change = {}) => ({ id, chat_id: 'chat-' + id, title, description, agent_kind: 'tracker', input_kind: 'tracker_events', mode: 'ad_hoc', dataset: 'vitosha', status: 'active', max_runs: 1, runs_started: 1,
  interval_seconds: 3600, fresh_inspections_per_run: 0, revision: 2, instruction: description, profile_ids: ['profile-' + id], owner_principal_id: 'owner', created_at: NOW / 1000 - 12 * 86400, updated_at: NOW / 1000 - 600, execution: readiness, runs: [], ...change });
const observed = (record, profiles, incidents, summary, extra = {}) => ({ agent_id: record.id, agent_kind: 'tracker', profiles, profile: profiles[0] || null, summary, incidents: [...incidents].sort((a, b) => b.committed_at - a.committed_at), event_count: incidents.length, more_available: false, ...extra });

const ASSIST_LINES = ['A team member helped a visitor choose cable ties.', 'A team member fetched a pocket saw from the top hook.', 'A team member showed two visitors the rope reels.', 'A team member checked a price for a visitor.', 'A team member walked a visitor to the garden ties.', 'A team member opened a pack of hooks for a visitor.', 'A team member carried a basket to the till for a visitor.', 'A team member answered a question at the pegboard.'];
const HOURS = ['09:20', '10:05', '10:50', '11:15', '11:40', '12:10', '12:35', '13:05', '14:20', '15:10', '15:55', '16:30'];
function fixtures() {
  made = 0;
  // Metric: one count per assist. The last seven days hold 42, the seven before them 36.
  const assists = agent('assist', 'Staff assists', 'Counts each time a team member helps a visitor in the tools aisle.', { mode: 'scheduled', interval_seconds: 3600 });
  const assistEvents = [];
  [4, 6, 5, 5, 7, 6, 3, 5, 7, 4, 8, 6, 9, 3].forEach((count, index) => { for (let n = 0; n < count; n++) assistEvents.push(event('profile-assist', 'staff_assist', ASSIST_LINES[(index * 3 + n) % ASSIST_LINES.length], sofia(index - 13, HOURS[(n * 2 + index) % (index === 13 ? 9 : HOURS.length)]))); });
  [[-20, 5], [-27, 6], [-34, 4], [-41, 7], [-55, 5], [-70, 6], [-90, 8], [-120, 5], [-150, 3]].forEach(([day, count], index) => { for (let n = 0; n < count; n++) assistEvents.push(event('profile-assist', 'staff_assist', ASSIST_LINES[(index + n) % ASSIST_LINES.length], new Date(NOW + day * 86400e3 - n * 3600e3))); });
  // Occurrences: a collection with one summary and its recommendation.
  const waits = agent('waits', 'Long waits for help', 'Every visitor who waited over two minutes at the pegboard before a team member arrived.');
  const waitEvents = [[0, '15:12', 'A visitor waited 3 min 10 s at the cable ties before a team member arrived.'], [0, '12:41', 'Two visitors waited 2 min 30 s at the rope reels.'], [0, '11:26', 'A visitor waited 4 min at the pocket saws and left the aisle.'],
    [-1, '17:05', 'A visitor waited 2 min 15 s with a basket at the pegboard.'], [-1, '12:18', 'A visitor waited 3 min 45 s at the garden ties.'], [-2, '11:52', 'A visitor waited 4 min 40 s at the hooks, the longest wait this week.'],
    [-2, '11:07', 'Two visitors waited 2 min 5 s at the cable ties.'], [-3, '12:33', 'A visitor waited 2 min 50 s at the rope reels and left the aisle.'], [-4, '11:44', 'A visitor waited 3 min 20 s at the pocket saws.'],
    [-5, '16:20', 'A visitor waited 2 min 40 s at the cable ties.'], [-5, '12:02', 'A visitor waited 3 min 5 s at the hooks.'], [-6, '11:31', 'A visitor waited 2 min 20 s at the garden ties.']].map(([day, clock, line]) => event('profile-waits', 'long_wait', line, sofia(day, clock)));
  // Live: entries as they happen, a memo after each set, key points pinned.
  const live = agent('live', 'Tools aisle today', 'Every visit to the tools aisle as it happens, with a memo after each set of updates.', { mode: 'ongoing', interval_seconds: 60 });
  const liveEvents = [['16:38', 'aisle_visit', 'Two visitors stopped at the cable ties and a team member joined them.'], ['16:31', 'pick_up', 'A visitor picked up a green pack of garden ties.'], ['16:24', 'aisle_visit', 'A visitor walked the aisle with a red basket.'],
    ['15:52', 'put_back', 'A visitor put a pocket saw back on its hook.'], ['15:40', 'aisle_visit', 'Three visitors were in the aisle at once.'], ['15:21', 'pick_up', 'A visitor picked up two packs of cable ties.'], ['14:58', 'aisle_visit', 'A team member restocked the lower hooks.'],
    ['13:36', 'pick_up', 'A visitor picked up a rope reel after comparing two.'], ['13:10', 'aisle_visit', 'A visitor read labels at the pegboard for two minutes.'], ['12:44', 'put_back', 'A visitor put a pack of hooks back.'], ['12:20', 'aisle_visit', 'Two visitors asked a team member about rope.'],
    ['11:05', 'pick_up', 'A visitor picked up a pocket saw.'], ['10:32', 'aisle_visit', 'The first visitor of the day stopped at the garden ties.']].map(([clock, type, line]) => event('profile-live', type, line, sofia(0, clock)));
  const memos = [
    { id: 'run-3', updated_at: sofia(0, '16:00').getTime() / 1000, event_count: 4, text: 'The afternoon is busier than the morning: three visitors shared the aisle at 15:40 and cable ties moved twice.\n\n## Recommendation\nKeep the cable ties topped up before 17:00.' },
    { id: 'run-2', updated_at: sofia(0, '14:00').getTime() / 1000, event_count: 4, text: 'Visitors compare before they pick: the rope reel went after two were held side by side, and a pack of hooks went back.\n\n## Recommendation\nPut the two rope sizes next to each other at eye level.' },
    { id: 'run-1', updated_at: sofia(0, '12:00').getTime() / 1000, event_count: 2, text: 'A quiet first hour with two visits and one pocket saw picked up.' },
  ];
  // State: each shelf now, and what changed.
  const stock = agent('stock', 'Pegboard stock', 'Each pegboard bay in the tools aisle: stocked, empty or replenished.', { mode: 'ongoing', interval_seconds: 300 });
  const shelf = (ref, name, type, day, clock) => event('profile-stock', type, `${name}: ${type === 'shelf_empty' ? 'the hooks are empty' : type === 'shelf_replenished' ? 'a team member refilled the hooks' : 'the hooks are full'}.`, sofia(day, clock), { object_ref: ref, object_description: name, item_id: 'item-' + ref });
  const stockEvents = [shelf('bay-2', 'Cable ties, bay 2', 'shelf_stocked', -4, '08:10'), shelf('bay-2', 'Cable ties, bay 2', 'shelf_empty', 0, '14:05'), shelf('bay-2', 'Cable ties, bay 2', 'shelf_replenished', 0, '15:20'),
    shelf('bay-1', 'Rope reels, bay 1', 'shelf_stocked', -5, '08:05'), shelf('bay-1', 'Rope reels, bay 1', 'shelf_empty', 0, '15:48'),
    shelf('bay-3', 'Hooks and hangers, bay 3', 'shelf_stocked', -3, '08:12'),
    shelf('bay-4', 'Garden ties, bay 4', 'shelf_empty', -1, '17:30'), shelf('bay-4', 'Garden ties, bay 4', 'shelf_replenished', 0, '08:20'),
    shelf('bay-5', 'Pocket saws, bay 5', 'shelf_stocked', -2, '08:15')];
  // A tracker with zero events shows 0.
  const queue = agent('queue', 'Queue watch', 'Alerts when the till queue passes five people.', { runs_started: 0 });
  const notify = { ...agent('notify', 'Notify supplier', 'Messages the hardware supplier when a bay stays empty for an hour.'), agent_kind: 'action', profile_ids: [] };
  const profiles = { assist: profile('profile-assist', 'Tools aisle', [['staff_assist', 'A team member helps a visitor.']]), waits: profile('profile-waits', 'Tools aisle', [['long_wait', 'A visitor waits over two minutes for help.']]),
    live: profile('profile-live', 'Tools aisle', [['aisle_visit', 'A visit to the aisle.'], ['pick_up', 'A product leaves its hook.'], ['put_back', 'A product returns to its hook.']]),
    stock: profile('profile-stock', 'Pegboard bays', [['shelf_empty', 'A bay with empty hooks.'], ['shelf_replenished', 'A bay refilled.'], ['shelf_stocked', 'A bay with full hooks.']]), queue: profile('profile-queue', 'Till', [['queue_long', 'More than five people wait.']]) };
  const report = (text, clock, run) => ({ text, updated_at: sofia(0, clock).getTime() / 1000, run_id: run, definition_revision: 2 });
  const observations = {
    assist: observed(assists, [profiles.assist], assistEvents, report('Assists cluster between 11:00 and 13:00, when two visitors at a time reach the pegboard. Friday was the busiest day, with nine assists, most of them about cable ties and rope.\n\n## Recommendation\nKeep a second team member near the tools aisle from 11:00 to 13:00 on weekdays.', '16:00', 'run-a7')),
    waits: observed(waits, [profiles.waits], waitEvents, report('Twelve visitors waited over two minutes for help this week. Nine of the waits began between 11:00 and 13:00, and the longest ran 4 min 40 s on Thursday.\n\nThree waits ended with the visitor leaving the aisle before a team member arrived.\n\n## Recommendation\nPlace one team member at the pegboard from 11:00 to 13:00. A call point at the aisle end covers the afternoon waits.', '15:30', 'run-w4')),
    live: observed(live, [profiles.live], liveEvents, report(memos[0].text, '16:00', 'run-3'), { memos, key_points: ['Thirteen updates today, six of them since 15:00.', 'Cable ties are the most handled product: picked up twice, with a team member called once.', 'Two products went back on their hooks: a pocket saw and a pack of hooks.'] }),
    stock: observed(stock, [profiles.stock], stockEvents, report('Rope reels in bay 1 have been empty since 15:48. Cable ties emptied at 14:05 and were back on their hooks at 15:20.\n\n## Recommendation\nRefill the rope reels before the evening visitors at 17:30.', '15:50', 'run-s9')),
    queue: observed(queue, [profiles.queue], [], null),
  };
  return { agents: [assists, waits, live, stock, queue, notify], observations, profiles: Object.values(profiles) };
}
/* ---------- trackers whose output is published, shaped as the host answers once a presentation is applied ----------
 * The observations then carry `presentation` (what the harness saved), `output` (the tile row Home drew, with its
 * week, month and year readings, and the canvas with the presentation's blocks) and `families`. */
const KIT = JSON.parse(fs.readFileSync(path.join(base, 'sdk/presentation_kit.json')));
const RETAIL_KIT = JSON.parse(fs.readFileSync(path.join(base, 'verticals/retail/core_trackers/presentation_kit.json')));
for (const [key, value] of Object.entries(RETAIL_KIT)) KIT[key] = value && typeof value === 'object' ? { ...KIT[key], ...value } : value;
const BLOCKS = Object.fromEntries(Object.entries(KIT.families).map(([id, family]) => [id, family.default]));   // each family's own blocks, as the kit gives them
const FAMILY_NAMES = { metric: 'Chart', live: 'Timeline', occurrences: 'Occurrences', state: 'Shelves', ranking: 'Products' };
const dayStart = Date.parse('2026-10-09T21:00:00Z');   // midnight in Sofia, Saturday 10 October
const daily = values => values.map((value, index) => ({ t: new Date(dayStart - (values.length - 1 - index) * 86400e3).toISOString(), value }));
const hourly = values => values.map((value, index) => ({ t: new Date(dayStart + index * 3600e3).toISOString(), value }));
const outputRow = (index, when, description, extra = {}) => ({ id: `row-${extra.key || 'x'}-${index}`, at: when.toISOString(), label: extra.label || 'Occurrence', description, thumb: `/media/${STILLS[index % STILLS.length]}`,
  visitor_id: null, source_id: CLIPS[index % CLIPS.length], camera_id: 'camera-aisle', person_id: null, at_s: 1 + index % 4, type: extra.type || 'occurrence', product: extra.product || null, ...(extra.shelf ? { shelf: extra.shelf } : {}) });
/* publish: one tracker's observations with its output. `readings` holds the day and, when given, the week, month and
 * year, each {value, series, rows, total, summary, ...its own canvas fields}. */
function publish(record, presentation, readings, more = {}) {
  const spec = { schema: 'rocoto.tracker-presentation.v1', revision: 1, saved_at: '2026-10-10T09:00:00Z', saved_by: 'harness', ...presentation };
  const revision = more.revision || 4, format = spec.tile.format, flags = { core: false, chat: true, locked: false };
  const unitOf = reading => format === 'rate' ? (reading.unit === '%' || reading.value > 1 ? '%' : null) : format === 'count' && 'unit' in spec.tile ? spec.tile.unit : reading.unit ?? null;
  const canvas = reading => { const { value, unit, series, rows, total, summary, label, fraction, trend, ...own } = reading; return { key: record.id, label: spec.tile.label, family: 'occurrences', value, unit: unit ?? null, state: 'ready', needs: null,
    period: { start: new Date(dayStart).toISOString(), end: new Date(dayStart + 86400e3).toISOString(), label: label || 'Sat 10 Oct 2026' }, summary: summary || '', series: series || [], history: readings.day.trend || [], rows: rows || [], total_rows: total ?? (rows || []).length,
    ...own, tracker_id: 'tracker-' + record.id, ...flags, revision, presentation: { family: spec.canvas.family, blocks: spec.canvas.blocks, revision: spec.revision }, problems: more.problems || [] }; };
  const face = reading => ({ value: reading.value, unit: unitOf(reading), state: 'ready', needs: null, series: reading.series || [], summary: reading.summary || '', ...(reading.fraction != null ? { fraction: reading.fraction } : {}) });
  const periods = Object.fromEntries(['week', 'month', 'year'].filter(name => readings[name]).map(name => [name, { ...face(readings[name]), period_label: readings[name].label, canvas: canvas(readings[name]) }]));
  const day = readings.day, tile = { key: record.id, label: spec.tile.label, ...face(day), events: (day.rows || []).slice(0, 40), period: canvas(day).period, trend: day.trend || [], tracker_id: 'tracker-' + record.id, ...flags, revision,
    kind: format, mark: spec.tile.mark, view: spec.canvas.family === 'state' ? 'shelves' : format === 'rate' ? 'rate' : 'count', presentation_revision: spec.revision, agent_id: record.id, ...(more.currency ? { currency: more.currency } : {}), ...(Object.keys(periods).length ? { periods } : {}) };
  const open = more.open || [spec.canvas.family];
  return observed(record, [profile('profile-' + record.id, more.watches || 'Shop floor', [['occurrence', 'An occurrence.']])], more.incidents || [], more.report || null, { tracker_id: 'tracker-' + record.id, presentation: spec,
    output: { revision, tile, canvas: canvas(day) }, families: Object.keys(BLOCKS).map(id => ({ id, name: FAMILY_NAMES[id], needs: SET.find(([name]) => name === FAMILY_NAMES[id])[1], available: open.includes(id), blocks: BLOCKS[id] })) });
}
function presented() {
  made = 0;
  // Chart: a count with its history line and the instances behind it.
  const freezer = agent('freezer', 'Freezer door', 'Counts each time the freezer door stays open for longer than a minute.', { mode: 'scheduled', interval_seconds: 900 });
  const doors = ['The freezer door stayed open for 2 min 10 s after a restock.', 'A visitor left the freezer door ajar for 1 min 20 s.', 'The freezer door stayed open for 3 min while a team member loaded it.'];
  const doorRows = (count, spread) => Array.from({ length: count }, (_, n) => outputRow(n, new Date(NOW - 40 * 60e3 - n * spread), doors[n % doors.length], { key: 'freezer', label: 'Door open', type: 'door_open' }));
  const freezerSeen = publish(freezer, { tile: { label: 'Freezer door left open', mark: 'line', format: 'count', unit: 'times', bind: { series: 'series' } },
    canvas: { family: 'metric', blocks: [{ block: 'figure' }, { block: 'period' }, { block: 'line', bind: { series: 'series' } }, { block: 'summary', bind: { text: 'summary' } }, { block: 'instances', title: 'Times it stayed open', bind: { rows: 'rows' } }] } }, {
    day: { value: 6, unit: 'times', series: hourly([0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 1, 2, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0]), trend: daily([4, 6, 5, 7, 8, 6, 6]), rows: doorRows(3, 50 * 60e3), total: 6, summary: 'The door stayed open 6 times today.' },
    week: { value: 42, unit: 'times', series: daily([4, 6, 5, 7, 8, 6, 6]), rows: doorRows(3, 26 * 3600e3), total: 42, label: '4 Oct to 10 Oct 2026', summary: 'The freezer door stayed open 42 times this week, most often between 11:00 and 13:00 during restocking.' },
    month: { value: 180, unit: 'times', series: daily(Array.from({ length: 30 }, (_, n) => 4 + (n * 7) % 5)), rows: doorRows(3, 5 * 86400e3), total: 180, label: '11 Sep to 10 Oct 2026', summary: 'The freezer door stayed open 180 times this month.' },
    year: { value: 1900, unit: 'times', series: daily(Array.from({ length: 365 }, (_, n) => 3 + (n * 11) % 6)), rows: doorRows(3, 40 * 86400e3), total: 1900, label: '11 Oct 2025 to 10 Oct 2026', summary: 'The freezer door stayed open 1,900 times this year.' },
  }, { open: ['metric', 'live', 'occurrences'], watches: 'Freezer aisle' });
  // Timeline: a rate with its ring, key points and a comment after each set of events.
  const checks = agent('checks', 'Shelf checks', 'Follows each hourly shelf check and whether it happened on time.', { mode: 'ongoing', interval_seconds: 60 });
  const checkLines = [['16:05', 'The 16:00 check of the dairy shelf happened at 16:05.'], ['15:02', 'The 15:00 check of the dairy shelf happened on time.'], ['14:20', 'The 14:00 check started 20 minutes late.'], ['13:01', 'The 13:00 check happened on time.'], ['12:04', 'The 12:00 check happened on time.'], ['11:00', 'The 11:00 check happened on time.'], ['10:03', 'The 10:00 check happened on time.'], ['09:01', 'The first check of the day happened on time.']];
  const checkRows = checkLines.map(([clock, line], n) => outputRow(n, sofia(0, clock), line, { key: 'checks', label: 'Shelf check', type: 'shelf_check' }));
  const checksSeen = publish(checks, { tile: { label: 'Shelf checks on time', mark: 'ring', format: 'rate' },
    canvas: { family: 'live', blocks: [{ block: 'figure' }, { block: 'gauge', chart: 'ring', bind: { fraction: 'ratio' } }, { block: 'key_points', bind: { items: 'key_points' } }, { block: 'timeline', bind: { rows: 'rows', comments: 'memos' } }] } }, {
    day: { value: 86.5, unit: '%', fraction: .865, ratio: .865, series: hourly([100, 100, 100, 100, 100, 50, 100, 100]), trend: daily([92, 88, 95, 81, 90, 84, 86.5]), rows: checkRows, total: 8, label: 'Sat 10 Oct 2026', summary: 'Seven of eight checks happened on time today.',
      key_points: ['Seven of eight checks happened on time today.', 'The 14:00 check started 20 minutes late.', 'Mornings run on time: every check before 13:00 was on the hour.'],
      memos: [{ id: 'memo-2', at: sofia(0, '15:30').toISOString(), text: 'The afternoon slipped once: the 14:00 check started at 14:20, and the next two were back on the hour.', recommendation: 'Set a reminder for the 14:00 check on Saturdays.' },
        { id: 'memo-1', at: sofia(0, '12:30').toISOString(), text: 'A clean morning: four checks, each within five minutes of the hour.' }] },
  }, { open: ['metric', 'live', 'occurrences'], watches: 'Dairy shelf' });
  // Occurrences: an amount, a summary and a scroll of instances with a thumbnail each.
  const returns = agent('returns', 'Returns at the desk', 'Every return a visitor brings to the service desk, with its estimated value.');
  const returnLines = ['A visitor returned a boxed drill and left with a receipt.', 'A visitor returned two packs of cable ties.', 'A visitor returned a rope reel after comparing lengths.', 'A visitor returned a pocket saw in its packaging.', 'A visitor returned a tarp and chose a larger one.', 'A visitor returned a headlamp with its batteries.'];
  const returnsSeen = publish(returns, { tile: { label: 'Returns at the desk', mark: 'bars', format: 'money', bind: { series: 'trend' } },
    canvas: { family: 'occurrences', blocks: [{ block: 'figure' }, { block: 'facts' }, { block: 'summary', bind: { text: 'summary' } }, { block: 'recommendation', bind: { text: 'advice' } }, { block: 'instances', title: 'Returns', bind: { rows: 'rows' } }] } }, {
    day: { value: 84.5, unit: 'EUR', series: daily([40, 62.5, 18, 96, 71, 55, 84.5]), trend: daily([40, 62.5, 18, 96, 71, 55, 84.5]), rows: returnLines.map((line, n) => outputRow(n, new Date(NOW - 25 * 60e3 - n * 55 * 60e3), line, { key: 'returns', label: 'Return', type: 'return' })), total: 6,
      summary: 'Six returns reached the desk today, worth an estimated €84.50. Four of them came back in their packaging and went straight to the shelf.', advice: 'Keep one team member at the desk between 12:00 and 14:00, when four of the six returns arrived.' },
  }, { open: ['metric', 'live', 'occurrences'], currency: 'EUR', watches: 'Service desk' });
  // Shelves: each shelf now, and what changed.
  const chiller = agent('chiller', 'Chiller shelves', 'Each shelf of the chiller: stocked, empty or replenished.', { mode: 'ongoing', interval_seconds: 300 });
  const shelfRow = (n, clock, name, key, state) => outputRow(n, sofia(0, clock), `${name} · ${state}`, { key: 'chiller', label: state, type: 'shelf_' + state.toLowerCase(), shelf: key });
  const chillerSeen = publish(chiller, { tile: { label: 'Chiller shelves', mark: 'ring', format: 'count', unit: 'empty', bind: { fraction: 'empty_share' } },
    canvas: { family: 'state', blocks: [{ block: 'state_counts', bind: { shelves: 'shelves' } }, { block: 'state_cards', bind: { shelves: 'shelves' } }, { block: 'summary', bind: { text: 'summary' } }, { block: 'instances', title: 'Changes', bind: { rows: 'rows' } }] } }, {
    day: { value: 1, unit: 'empty', fraction: .25, empty_share: .25, series: hourly([0, 0, 1, 1, 0, 1, 1, 1]), trend: daily([0, 1, 0, 2, 1, 0, 1]), total: 4, summary: 'Yoghurt on the top shelf has been empty since 15:48. Milk emptied at 14:05 and was back at 15:20.',
      rows: [shelfRow(0, '15:48', 'Yoghurt, top shelf', 'top', 'Empty'), shelfRow(1, '15:20', 'Milk, middle shelf', 'middle', 'Replenished'), shelfRow(2, '14:05', 'Milk, middle shelf', 'middle', 'Empty'), shelfRow(3, '08:10', 'Butter, lower shelf', 'lower', 'Stocked')],
      shelves: [{ shelf: 'top', label: 'Yoghurt, top shelf', state: 'empty', empty_since: sofia(0, '15:48').toISOString(), replenished_at: null }, { shelf: 'middle', label: 'Milk, middle shelf', state: 'stocked', empty_since: sofia(0, '14:05').toISOString(), replenished_at: sofia(0, '15:20').toISOString() },
        { shelf: 'lower', label: 'Butter, lower shelf', state: 'stocked', empty_since: null, replenished_at: null }, { shelf: 'door', label: 'Juice, door rack', state: 'stocked', empty_since: null, replenished_at: null }] },
  }, { open: ['metric', 'live', 'occurrences', 'state'], watches: 'Chiller' });
  // Products: a ranking with the sessions behind each product.
  const handled = agent('handled', 'Most handled at the till', 'The products visitors handle most at the till display.', { mode: 'scheduled', interval_seconds: 3600 });
  const till = [['Chewing gum', 14, 9, 3, 2], ['Phone cable', 9, 6, 2, 1], ['Hand cream', 5, 4, 1, 0], ['Batteries', 3, 2, 1, 0]];
  const tillRows = till.flatMap(([name], index) => [0, 1].map(n => outputRow(index * 2 + n, new Date(NOW - (index * 2 + n + 1) * 35 * 60e3), `A visitor ${n ? 'put back' : 'picked up'} ${name.toLowerCase()}.`, { key: 'handled', label: n ? 'Put back' : 'Picked up', type: n ? 'put_back' : 'pick_up', product: name })));
  const handledSeen = publish(handled, { tile: { label: 'Most handled at the till', mark: 'line', format: 'count', unit: 'handled', bind: { series: 'series' } },
    canvas: { family: 'ranking', blocks: [{ block: 'figure' }, { block: 'ranking', bind: { items: 'products' } }, { block: 'summary', bind: { text: 'summary' } }, { block: 'instances', bind: { rows: 'rows' } }] } }, {
    day: { value: 31, unit: 'handled', series: hourly([0, 1, 3, 2, 4, 6, 5, 3, 4, 3]), trend: daily([22, 27, 25, 30, 34, 29, 31]), rows: tillRows, total: 31, summary: 'Chewing gum is handled most at the till, and one pick-up in five goes back.',
      products: till.map(([product, value, picked, returned, bought], index) => ({ product, value, picked, returned, bought, thumb: `/media/${STILLS[(index * 3 + 1) % STILLS.length]}` })) },
  }, { open: ['metric', 'live', 'occurrences', 'ranking'], watches: 'Till display' });
  // A duration with a bars tile and a band for each length of wait.
  const wait = agent('wait', 'Wait at the counter', 'How long a visitor waits at the service counter before a team member arrives.', { mode: 'scheduled', interval_seconds: 1800 });
  const waitSeen = publish(wait, { tile: { label: 'Wait at the counter', mark: 'bars', format: 'duration', bind: { series: 'series' } },
    canvas: { family: 'metric', blocks: [{ block: 'figure' }, { block: 'line', bind: { series: 'series' } }, { block: 'bars', title: 'Waits by length', bind: { values: 'bands' } }, { block: 'summary', bind: { text: 'summary' } }] } }, {
    day: { value: 95, unit: 's', series: hourly([40, 55, 80, 120, 150, 95, 70, 95]), trend: daily([70, 85, 60, 110, 95, 80, 95]), rows: [], total: 0, summary: 'A visitor waits 1 min 35 s on average today, longest around noon.',
      bands: [{ band: 'Under 1 min', value: 14 }, { band: '1 to 2 min', value: 21 }, { band: '2 to 3 min', value: 9 }, { band: 'Over 3 min', value: 4 }] },
  }, { open: ['metric', 'occurrences'], watches: 'Service counter' });
  const trackers = [freezer, checks, returns, chiller, handled, wait];
  return { agents: trackers, observations: { freezer: freezerSeen, checks: checksSeen, returns: returnsSeen, chiller: chillerSeen, handled: handledSeen, wait: waitSeen }, profiles: trackers.map(item => profile('profile-' + item.id, 'Shop floor', [['occurrence', 'An occurrence.']])) };
}
function phoneBlock(id) {
  return { agent_id: id, chat_id: 'chat-' + id, collection: 'vitosha', generation: 4, delivery: 'on', can_manage: true, read_only: false,
    channels: [{ channel: 'viber', label: 'Viber', ready: true, reason: null, sender: 'Rocoto' }, { channel: 'sms', label: 'SMS', ready: false, reason: 'provider_setup_required', sender: null }, { channel: 'whatsapp', label: 'WhatsApp', ready: true, reason: null, sender: '+12025550123' }],
    participants: [
      { member_id: 'owner', name: 'Maria Ivanova', role: 'creator', is_self: true, channel: 'viber', channel_choice: 'automatic', verified_channels: ['viber', 'whatsapp'], address_masked: '+359•••••••56', verification: 'verified', enabled: true, last_delivery: { id: 'dlv-8cf7835a1323e7c5b4bcee0a0f60bdda', channel: 'viber', status: 'delivered', held_reason: null, updated_at: NOW / 1000 - 1500 }, contact_request: null },
      { member_id: 'member-02', name: 'Ivan Petrov', role: 'added', is_self: false, channel: 'whatsapp', channel_choice: 'automatic', verified_channels: ['whatsapp'], address_masked: '+359•••••••12', verification: 'verified', enabled: true, last_delivery: { id: 'dlv-11111111111111111111111111111111', channel: 'whatsapp', status: 'read', held_reason: null, updated_at: NOW / 1000 - 1400 }, contact_request: null }],
    available_members: [{ member_id: 'member-03', name: 'Elena Georgieva', verified: true }], spend: { state: 'open', agent: { used: 3, cap: 50 }, company: { used: 12, cap: 200 }, resets_at: NOW / 1000 + 30000 } };
}

async function appFor(width = 1600, options = {}) {
  const context = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 1000 }, timezoneId: 'Europe/Sofia', locale: 'en-GB', reducedMotion: options.reducedMotion ? 'reduce' : 'no-preference', hasTouch: !!options.touch });
  const page = await context.newPage(), errors = [], calls = [], saves = [], world = options.world || fixtures();
  page.setDefaultTimeout(6000); page.on('pageerror', error => errors.push(error.message));
  await page.clock.setFixedTime(new Date(NOW));
  if (options.config) await page.addInitScript(config => { window.RocotoControlRoomConfig = config; }, options.config);
  await page.route('**/*', route => route.request().url().startsWith(origin + '/') ? route.fallback() : route.abort());
  if (options.withoutKit) await page.route('**/tracker-canvas.js', route => route.fulfill({ status: 404, body: '' }));
  if (options.withoutCharts) await page.route('**/display-charts.js', route => route.fulfill({ status: 404, body: '' }));
  const chat = { id: 'fixture-chat', title: 'Which bays empty first on Saturdays?', kind: 'chat', dataset: 'vitosha', owner_principal_id: 'owner', pinned: false, archived: false, metadata_revision: 0, updated: NOW / 1000 - 3000, context: { used_tokens: 12345, window_tokens: 1000000 }, messages: [{ role: 'user', content: 'Which bays empty first on Saturdays?' }, { role: 'assistant', content: 'Rope reels and cable ties.' }], running: false };
  const agentChats = () => world.agents.map(item => ({ ...chat, id: item.chat_id, title: item.title, kind: 'agent_conversation', agent_id: item.id, agent_kind: item.agent_kind, agent_setup: { agent_id: item.id, agent_kind: item.agent_kind, mode: item.mode }, messages: [{ role: 'user', content: item.instruction }, { role: 'assistant', content: 'Agent conversation ready.' }] }));
  await page.route('**/api/**', async route => {
    const request = route.request(), url = new URL(request.url()), p = url.pathname, method = request.method();
    const json = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) });
    calls.push({ method, path: p });
    const phone = /^\/api\/agents\/([^/]+)\/phone$/.exec(p), watched = /^\/api\/agents\/([^/]+)\/observations$/.exec(p);
    if (phone) { const record = world.agents.find(item => item.id === phone[1]); return record?.agent_kind === 'tracker' ? json(phoneBlock(record.id)) : json({ detail: 'Choose a Tracking Agent from your current workspace.' }, 404); }
    // The one write a canvas makes: its picked family, saved with the tracker as the server saves it.
    const pick = /^\/api\/agents\/([^/]+)\/presentation$/.exec(p);
    if (pick && method === 'POST') {
      const body = request.postDataJSON(), record = world.agents.find(item => item.id === pick[1]), current = world.observations[pick[1]] || {}, prior = current.presentation;
      saves.push({ id: pick[1], body });
      const set = options.answer ? options.answer(pick[1], body, saves.length) : null;
      if (set) return json(set.detail === undefined ? {} : { detail: set.detail }, set.status);
      if (options.refuse) return json({ detail: options.refuse }, 400);
      if (body.expected_revision !== (prior?.revision || 0)) return json({ detail: 'This tracker’s presentation changed while the canvas was open. Pick the canvas again.' }, 409);
      const saved = { schema: 'rocoto.tracker-presentation.v1', tile: prior?.tile || { label: record.title, mark: 'line', format: 'count', bind: { series: 'series' } },
        canvas: { family: body.canvas.family, blocks: BLOCKS[body.canvas.family] }, revision: (prior?.revision || 0) + 1, saved_at: new Date(NOW).toISOString(), saved_by: 'person' };
      world.observations[pick[1]] = { ...current, tracker_id: 'tracker-' + pick[1], presentation: saved,
        ...(current.output ? { output: { ...current.output, tile: { ...current.output.tile, presentation_revision: saved.revision }, canvas: { ...current.output.canvas, presentation: { family: saved.canvas.family, blocks: saved.canvas.blocks, revision: saved.revision } } } } : {}) };
      return json({ tracker_id: 'tracker-' + pick[1], key: pick[1], presentation: saved, page_updated: false });
    }
    assert.equal(method, 'GET', 'Unexpected mutation during UI test: ' + p);
    if (watched) {
      const key = watched[1], failed = options.failRead ? options.failRead(key, calls.filter(call => call.method === 'GET' && call.path === p).length) : 0;
      if (failed) return json({ detail: 'The service is restarting.' }, failed);
      return key in world.observations ? json(world.observations[key]) : json({ detail: 'Choose a record from your current workspace.' }, 404);
    }
    let data = {};
    if (p === '/api/harness') data = retailHarness;
    else if (p === '/api/session') data = { authenticated: true, demo_browsing: false, can_query: true, read_only: false, role: 'owner', principal: { id: 'owner', display_name: 'Maria Ivanova' }, effective_principal_id: 'owner', environment: { id: 'demo', name: 'Vitosha Hardware' }, environments: [{ id: 'demo', name: 'Vitosha Hardware', category: 'demo', collections: [{ id: 'vitosha', name: 'Vitosha Boulevard 85' }] }], viewing_as: null };
    else if (p === '/api/status') data = { demo_guidance: {}, agent: { authenticated: true, active_chats: {} }, corpus: [{ video_id: 'intel-clip2', dataset: 'vitosha', duration: 12 }], connection: { host: 'fixture-host', managed_authenticated: true, check_status: 'authenticated', checked_at: new Date(NOW).toISOString() } };
    else if (p === '/api/observers') data = { principals: [] };
    else if (p === '/api/chats') data = { chats: [chat, ...agentChats()] };
    else if (p.startsWith('/api/chats/')) data = agentChats().find(item => p.endsWith('/' + item.id)) || chat;
    else if (p === '/api/agents') data = { agents: world.agents, capabilities: { recurring_agents: readiness } };
    else if (p === '/api/agents/catalogue') data = { templates: [] };
    else if (p.startsWith('/api/agents/')) { const record = world.agents.find(item => item.id === p.split('/')[3]); if (!record) return json({}, 404); data = record; }
    else if (p === '/api/retail/monitoring/profiles') data = { profiles: world.profiles };
    else if (p === '/api/workspace/capabilities') data = { recurring_agents: readiness };
    else if (p === '/api/plugins/installed') data = { plugins: [], available: true, installable_ids: [] };
    else if (p === '/api/plugins/catalogue') data = catalogue;
    else if (p === '/api/connections' || p === '/api/connections/custom') data = { connections: [] };
    else if (p === '/api/files') data = { files: [] };
    else if (p === '/api/skills/catalogue' || p === '/api/skills') data = { skills: [] };
    else if (p === '/api/business/map') data = { status: 'empty', map: null, data: null };
    else if (p === '/api/usage') data = { company: { available_usd: '30', prepaid_funded_usd: '40', reserved_usd: '0', monthly_used_usd: '10', monthly_limit_usd: '40' }, context: chat.context, codex: { route: 'OpenAI API', limits: [] }, storage: {}, billing: { status: 'unconfigured' } };
    else if (p === '/api/retail/cameras') data = { cameras: [] };
    // options.frame: a stand-in location view, so the messages between the view and the shell are the test's own.
    else if (p === '/api/retail/viewer') data = options.frame ? { ready: true, bundle_url: '/frame-stub.html', location: PLACE } : bundle && options.location ? { ready: true, bundle_url: '/control-room/', location: PLACE } : {};
    else if (p === '/api/retail/viewer/bootstrap') data = { schema: 'rocoto.player-bootstrap.v1', read_authority: 'legacy', sources: [] };
    return json(data);
  });
  await page.goto(origin + '/?chat=fixture-chat');
  await page.locator('#messages .message').first().waitFor();
  await page.locator('#new-chat').click();
  const home = page.locator('#workspace-tab-view');
  await home.locator('.cr-home').waitFor();
  const reads = id => calls.filter(call => call.method === 'GET' && call.path === `/api/agents/${id}/observations`).length;
  const stub = async () => { await home.locator('.cr-location-frame').waitFor(); await page.waitForFunction(() => document.querySelector('.cr-location-frame')?.contentWindow?.ready === true); return page.frames().find(item => item.url().includes('/frame-stub.html')); };
  return { page, home, world, calls, saves, reads, errors, stub, close: async () => { assert.deepEqual(errors, []); await context.close(); } };
}
/* A tile in the location view asks the shell for its tracker's canvas with this event (or the same message from the frame). */
async function open(h, title) {
  // A tracker is named by its agent's title, or by the label its presentation gives its tile and canvas.
  const id = (h.world.agents.find(item => item.title === title) || h.world.agents.find(item => h.world.observations[item.id]?.presentation?.tile?.label === title)).id;
  await h.page.evaluate(agentId => window.dispatchEvent(new CustomEvent('rocoto:home-open-tracker', { detail: { agentId } })), id);
  const canvas = h.home.locator('.tc-canvas'); await canvas.locator('h1', { hasText: title }).waitFor(); try { await h.page.locator('.tc-canvas[aria-busy="false"]').waitFor(); } catch (error) { error.message += '\nCanvas: ' + await canvas.innerText() + '\nPage errors: ' + JSON.stringify(h.errors); throw error; } return canvas;
}
async function back(h) { await h.home.getByRole('button', { name: '← Home' }).click(); await h.home.locator('.tc-canvas').waitFor({ state: 'detached' }); await h.home.locator('.cr-home').waitFor(); }
/* The tiles a location view draws: this module's own tile for each of the shop's trackers, on a bare page. */
async function tilesFor(width, world = fixtures()) {
  const context = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 1000 }, timezoneId: 'Europe/Sofia', locale: 'en-GB' }), page = await context.newPage(), errors = [];
  page.setDefaultTimeout(6000); page.on('pageerror', error => errors.push(error.message)); await page.clock.setFixedTime(new Date(NOW));
  await page.route('**/*', route => route.request().url().startsWith(origin + '/') ? route.fallback() : route.abort());
  await page.goto(origin + '/tiles.html'); await page.waitForFunction(() => window.RocotoTrackerCanvas);
  await page.evaluate(({ agents, observations }) => { const kit = window.RocotoTrackerCanvas; window.tiles = agents.filter(item => item.agent_kind === 'tracker').map(item => kit.tile({ agent: item, observations: observations[item.id] })); document.querySelector('#tiles').append(...window.tiles.map(item => item.element)); }, world);
  return { page, world, close: async () => { assert.deepEqual(errors, []); await context.close(); } };
}
const tile = (page, title) => page.locator('.tc-tile', { hasText: title });
const fitsWidth = page => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth && [...document.querySelectorAll('#workspace-tab-view, .tc-canvas, .tc-frame, .tc-tiles')].every(node => node.scrollWidth <= node.clientWidth + 1));
const settle = async page => { await page.evaluate(() => document.fonts.ready); await page.waitForTimeout(900); };
async function shot(page, name) { if (!shots) return; fs.mkdirSync(shots, { recursive: true }); await settle(page); await page.screenshot({ path: path.join(shots, name + '.png') }); }
const lines = async locator => (await locator.innerText()).split('\n').map(line => line.trim()).filter(Boolean);
const SET = [['Chart', 'Needs a number over time.'], ['Timeline', 'Needs timed events.'], ['Occurrences', 'Needs instances with a thumbnail.'], ['Shelves', 'Needs shelf states.'], ['Products', 'Needs events that name a product.']];

test('a tracker picks its canvas from one set of families, each naming the data it needs', async () => {
  const h = await appFor();
  try {
    const result = await h.page.evaluate(world => {
      const kit = window.RocotoTrackerCanvas, by = id => world.agents.find(item => item.id === id), set = id => kit.families(by(id), world.observations[id]);
      return { api: Object.keys(kit).sort(), families: kit.FAMILIES, periods: kit.PERIODS, named: set('waits').map(item => [item.name, item.needs]),
        open: Object.fromEntries(['assist', 'waits', 'live', 'stock', 'queue'].map(id => [id, set(id).filter(item => item.available).map(item => item.id)])),
        read: ['assist', 'waits', 'live', 'stock', 'queue'].map(id => kit.family(by(id), world.observations[id])),
        declared: kit.family({ mode: 'ad_hoc', canvas: { family: 'ranking' } }, { incidents: [] }), bare: kit.family({}, null),
        picked: kit.model(by('waits'), world.observations.waits, { family: 'live' }).family, refused: kit.model(by('waits'), world.observations.waits, { family: 'state' }).family,
        locked: [kit.model(by('waits'), world.observations.waits).locked, kit.model({ ...by('stock'), core: true }, world.observations.stock).locked],
        legacy: kit.model({ id: 'a', title: 'Queue watch' }, { summary: 'The till queue passed five people twice this morning.', incidents: [] }).summary.text };
    }, h.world);
    assert.deepEqual(result.api, ['FAMILIES', 'PERIODS', 'families', 'family', 'model', 'mount', 'mountDisplay', 'tile', 'tileDisplay']);
    assert.deepEqual(result.families, ['metric', 'live', 'occurrences', 'state', 'ranking']);
    assert.deepEqual(result.periods, { week: 'Weekly', month: 'Monthly', year: 'Annual' });
    assert.deepEqual(result.named, SET);
    for (const [, sentence] of SET) { assert.match(sentence, /^Needs [a-z ]+\.$/); assert.equal(NEGATIVE.test(sentence), false); }
    // A family is open to a tracker whose records hold the data it names.
    assert.deepEqual(result.open, { assist: ['metric', 'live', 'occurrences'], waits: ['metric', 'live', 'occurrences'], live: ['metric', 'live', 'occurrences'], stock: ['metric', 'live', 'occurrences', 'state'], queue: [] });
    assert.deepEqual(result.read, ['metric', 'occurrences', 'live', 'state', 'occurrences']);
    assert.equal(result.declared, 'ranking'); assert.equal(result.bare, 'occurrences');
    assert.equal(result.picked, 'live'); assert.equal(result.refused, 'occurrences', 'a pick the records cannot fill leaves the canvas as it was');
    assert.deepEqual(result.locked, [false, true]);
    assert.equal(result.legacy, 'The till queue passed five people twice this morning.');
  } finally { await h.close(); }
});

test('each tracker has its tile: the compact view of its canvas, on the same revision', async () => {
  const world = fixtures(), record = agent('rank', 'Most handled products', 'The products visitors handle most in the tools aisle.', { mode: 'scheduled', canvas: { family: 'ranking' } });
  const item = (ref, name, type, clock) => event('profile-rank', type, `A visitor handled ${name.toLowerCase()}.`, sofia(0, clock), { object_ref: ref, object_description: name, item_id: 'item-' + ref });
  world.agents.push(record); world.observations.rank = observed(record, [], [item('ties', 'Cable ties', 'pick_up', '10:00'), item('ties', 'Cable ties', 'pick_up', '11:00'), item('saw', 'Pocket saw', 'pick_up', '12:30')], null);
  for (const width of [1600, 390]) {
    const t = await tilesFor(width, world);
    try {
      assert.deepEqual(await t.page.locator('.tc-tile').evaluateAll(items => items.map(node => [node.querySelector('.tc-tile-title').textContent, node.dataset.family])), [['Staff assists', 'metric'], ['Long waits for help', 'occurrences'], ['Tools aisle today', 'live'], ['Pegboard stock', 'state'], ['Queue watch', 'occurrences'], ['Most handled products', 'ranking']]);
      assert.deepEqual(await lines(tile(t.page, 'Staff assists')), ['Staff assists', '42', 'this week', '6 more than last week']);
      assert.deepEqual(await lines(tile(t.page, 'Long waits for help')), ['Long waits for help', '12', 'A visitor waited 3 min 10 s at the cable ties before a team member arrived.', '15:12']);
      assert.deepEqual(await lines(tile(t.page, 'Tools aisle today')), ['Tools aisle today', '13', 'today', 'Two visitors stopped at the cable ties and a team member joined them.', '16:38']);
      assert.deepEqual(await lines(tile(t.page, 'Pegboard stock')), ['Pegboard stock', '1', 'empty', 'Rope reels, bay 1 · Empty', '15:48']);
      assert.deepEqual(await lines(tile(t.page, 'Queue watch')), ['Queue watch', '0'], 'a tracker with zero events shows 0');
      assert.deepEqual(await lines(tile(t.page, 'Most handled products')), ['Most handled products', 'Cable ties', '2', 'Pocket saw', '1']);
      for (const node of await t.page.locator('.tc-tile').all()) assert.deepEqual(await node.locator('.tc-tile-open').evaluate(inner => [inner.scrollWidth <= inner.clientWidth, inner.scrollHeight <= inner.clientHeight + 1]), [true, true], 'a tile holds its own content: ' + await node.locator('.tc-tile-title').innerText());
      assert.equal(await fitsWidth(t.page), true, `the tiles keep their width at ${width}`);
      assert.equal(await t.page.evaluate(() => getComputedStyle(document.querySelector('.tc-tile-numeral')).fontFamily === getComputedStyle(document.body).fontFamily), true, 'tiles read in the product’s own face');
      await shot(t.page, `tiles-${width}`);
    } finally { await t.close(); }
  }
  // The canvas opens on the reading its tile showed.
  const h = await appFor();
  try {
    const canvas = await open(h, 'Long waits for help');
    assert.equal(await canvas.getAttribute('data-revision'), await h.page.evaluate(w => window.RocotoTrackerCanvas.tile({ agent: w.agents.find(a => a.id === 'waits'), observations: w.observations.waits }).revision, h.world));
  } finally { await h.close(); }
});

test('Home is the location view, edge to edge, and its tiles open the canvas', async () => {
  const h = await appFor(1600, { frame: true }), p = h.page;
  try {
    const stub = await h.stub();
    assert.equal(await h.home.locator('.cr-agent-section, .cr-recents, .cr-section-grid, .cr-agent-card, .cr-empty-card, .tc-tiles').count(), 0);
    assert.deepEqual(await h.home.locator('h1, h2, h3').allInnerTexts(), [], 'Home is the location view');
    assert.deepEqual(await h.home.getByRole('button').allInnerTexts(), [], 'New tracker lives on the location view as its plus tile');
    const [view, frame, host] = [await h.home.locator('.cr-home').boundingBox(), await h.home.locator('.cr-location-frame').boundingBox(), await p.locator('#workspace-tab-view').boundingBox()];
    assert.ok(Math.abs(frame.x - host.x) < 1 && Math.abs(frame.width - host.width) < 1 && Math.abs(frame.y - view.y) < 1 && Math.abs(frame.height - host.height) < 2, 'the location view fills the workspace');
    // The same request from the location view's own New tracker tile.
    const again = stub;
    await again.evaluate(() => parent.postMessage({ type: 'rocoto:home-new-tracker' }, location.origin));
    await p.waitForFunction(() => /Help me create a Tracker Agent/.test(document.querySelector('#message').value) && document.querySelector('#workspace-tab-view').hidden);
    // A tile of a tracker the shop made asks for its canvas; Home stays mounted behind it.
    await p.locator('#new-chat').click(); const view2 = await h.stub();
    await h.home.locator('.cr-location-frame').evaluate(node => { node.dataset.kept = 'yes'; });
    await view2.evaluate(() => parent.postMessage({ type: 'rocoto:home-open-tracker', agent_id: 'waits' }, location.origin));
    const canvas = h.home.locator('.tc-canvas'); await canvas.locator('h1', { hasText: 'Long waits for help' }).waitFor();
    assert.equal(await h.home.locator('.cr-home').isHidden(), true);
    assert.equal(await p.evaluate(() => document.activeElement.textContent), '← Home', 'the canvas takes the focus');
    await canvas.getByRole('radiogroup', { name: 'Period' }).count();
    await back(h);
    assert.equal(await h.home.locator('.cr-location-frame').getAttribute('data-kept'), 'yes', 'the location view is the same one, left as it was');
    assert.equal(await p.evaluate(() => document.activeElement.className), 'cr-location-frame', 'focus returns to the location view');
    await view2.waitForFunction(() => window.received.some(item => item.type === 'rocoto:home-focus-tile'));
    assert.deepEqual(await view2.evaluate(() => window.received.filter(item => /^rocoto:(player|home)-/.test(item.type)).map(item => item.type + (item.key ? ' ' + item.key : ''))), ['rocoto:player-suspend', 'rocoto:player-resume', 'rocoto:home-focus-tile agent:waits']);
    // Only the location view speaks for Home.
    await p.evaluate(() => window.postMessage({ type: 'rocoto:home-open-tracker', agent_id: 'stock' }, location.origin)); await p.waitForTimeout(200);
    assert.equal(await h.home.locator('.tc-canvas').count(), 0);
    await shot(p, 'home-stand-in-1600');
  } finally { await h.close(); }
});

test('the chart canvas leads with its numeral, a line over time, the period and the events behind it', async () => {
  const h = await appFor();
  try {
    const canvas = await open(h, 'Staff assists');
    assert.equal(await canvas.getAttribute('data-family'), 'metric');
    assert.deepEqual(await lines(canvas.locator('.tc-figure')), ['42', 'this week']);
    assert.equal(await canvas.locator('.tc-delta').innerText(), '6 more than last week');
    assert.deepEqual(await canvas.getByRole('radiogroup', { name: 'Period' }).getByRole('radio').evaluateAll(items => items.map(item => [item.textContent, item.getAttribute('aria-checked')])), [['Weekly', 'true'], ['Monthly', 'false'], ['Annual', 'false']]);
    assert.match(await canvas.locator('.tc-rail-head').innerText(), /^Events\s+42$/);
    // The chart is one keyboard stop: arrows read each day, Enter opens that day's events.
    const chart = canvas.getByRole('slider', { name: 'Staff assists over time' });
    await chart.focus(); await h.page.keyboard.press('End');
    assert.equal(await chart.getAttribute('aria-valuetext'), '3, Today');
    await h.page.keyboard.press('ArrowLeft'); await h.page.keyboard.press('ArrowLeft');
    assert.equal(await chart.getAttribute('aria-valuetext'), '6, Thu 8 Oct');
    assert.equal(await canvas.locator('.tc-chart .rocoto-chart').count(), 1, 'the shell carries the shared marks, and the chart is drawn with them');
    assert.deepEqual(await lines(canvas.locator('.rocoto-chart-readout')), ['6', 'Thu 8 Oct']);
    await h.page.keyboard.press('Enter');
    assert.match(await canvas.locator('.tc-rail-head').innerText(), /^Thu 8 Oct\s+6\s+Show the whole week$/);
    assert.equal(await canvas.locator('.tc-occurrence').count(), 6);
    assert.equal(await h.page.evaluate(() => document.activeElement.getAttribute('role')), 'slider', 'the chart keeps the focus');
    await canvas.getByRole('button', { name: 'Show the whole week' }).click();
    assert.equal(await canvas.locator('.tc-occurrence').count(), 42);
    // The period is one setting: it travels to the location view and to the next canvas.
    await h.page.evaluate(() => { window.periods = []; window.addEventListener('rocoto:period', event => window.periods.push(event.detail.period)); });
    await canvas.getByRole('radio', { name: 'Weekly' }).focus(); await h.page.keyboard.press('ArrowRight');
    assert.deepEqual(await lines(canvas.locator('.tc-figure')), ['89', 'this month']);
    assert.equal(await h.page.evaluate(() => document.activeElement.textContent), 'Monthly');
    await canvas.getByRole('radio', { name: 'Annual' }).click();
    assert.deepEqual(await lines(canvas.locator('.tc-figure')), ['127', 'this year']);
    assert.deepEqual(await h.page.evaluate(() => window.periods), ['month', 'year']);
    await back(h);
    assert.equal(await (await open(h, 'Pegboard stock')).getAttribute('data-period'), 'year');
  } finally { await h.close(); }
});

test('a page without the shared marks draws the chart canvas with its own line', async () => {
  const h = await appFor(1600, { withoutCharts: true });
  try {
    assert.equal(await h.page.evaluate(() => typeof window.RocotoCharts), 'undefined');
    const canvas = await open(h, 'Staff assists'), chart = canvas.getByRole('slider', { name: 'Staff assists over time' });
    assert.equal(await canvas.locator('.tc-chart-art').count(), 1); assert.equal(await canvas.locator('.rocoto-chart').count(), 0);
    await chart.focus(); await h.page.keyboard.press('End'); await h.page.keyboard.press('ArrowLeft'); await h.page.keyboard.press('ArrowLeft');
    assert.equal(await chart.getAttribute('aria-valuetext'), '6, Thu 8 Oct');
    assert.deepEqual(await lines(canvas.locator('.tc-chart-chip')), ['6', 'Thu 8 Oct']);
    await h.page.keyboard.press('Enter');
    assert.match(await canvas.locator('.tc-rail-head').innerText(), /^Thu 8 Oct\s+6\s+Show the whole week$/);
    assert.equal(await h.page.evaluate(() => document.activeElement.getAttribute('role')), 'slider');
  } finally { await h.close(); }
});

test('the occurrence canvas gives five eighths to the heading and summary and three eighths to its own scrolling list', async () => {
  const h = await appFor();
  try {
    const canvas = await open(h, 'Long waits for help');
    assert.equal(await canvas.getAttribute('data-family'), 'occurrences');
    assert.equal(await canvas.getByRole('heading', { level: 1 }).innerText(), 'Long waits for help');
    assert.deepEqual(await lines(canvas.locator('.tc-lead .tc-figure')), ['12', 'occurrences']);
    assert.match(await canvas.locator('.tc-summary').first().innerText(), /^Twelve visitors waited over two minutes for help this week\./);
    assert.deepEqual(await lines(canvas.locator('.tc-recommendation')), ['Recommendation', 'Place one team member at the pegboard from 11:00 to 13:00. A call point at the aisle end covers the afternoon waits.']);
    assert.deepEqual(await lines(canvas.locator('.tc-updated')), ['Updated', 'Today 15:30', 'Runs', 'On request', 'Watches', 'Tools aisle']);
    const frame = await canvas.locator('.tc-frame').evaluate(node => { const style = getComputedStyle(node), box = node.getBoundingClientRect(); return { inner: box.width - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight), gap: parseFloat(style.columnGap) }; });
    const main = await canvas.locator('.tc-main').boundingBox(), rail = await canvas.locator('.tc-rail').boundingBox(), column = (frame.inner - 7 * frame.gap) / 8;
    assert.ok(Math.abs(main.width - (5 * column + 4 * frame.gap)) < 2, 'the summary column spans five of eight'); assert.ok(Math.abs(rail.width - (3 * column + 2 * frame.gap)) < 2, 'the list spans three of eight');
    assert.ok(rail.x > main.x + main.width);
    assert.match(await canvas.locator('.tc-rail-head').innerText(), /^Occurrences$/);
    const rows = canvas.locator('.tc-occurrence');
    assert.equal(await rows.count(), 12);
    assert.deepEqual((await lines(rows.first())).slice(-2), ['A visitor waited 3 min 10 s at the cable ties before a team member arrived.', '15:12']);
    assert.deepEqual((await lines(rows.nth(3))).slice(-2), ['A visitor waited 2 min 15 s with a basket at the pegboard.', 'Yesterday, 17:05']);
    assert.match((await lines(rows.first()))[0], /^0:\d\d$/, 'a thumbnail carries the length of its clip');
    assert.match(await rows.first().getByRole('link').getAttribute('href'), /\/\?clip=intel-clip\d+$/);
    // The list scrolls on its own while the page stays where it is.
    const list = canvas.locator('.tc-rail-scroll');
    assert.equal(await list.evaluate(node => node.scrollHeight > node.clientHeight + 100), true);
    await list.evaluate(node => { node.scrollTop = 400; });
    assert.equal(await h.home.evaluate(node => node.scrollTop), 0);
    // A thumbnail plays its own interval while the pointer or the keyboard focus rests on its row.
    await rows.nth(4).scrollIntoViewIfNeeded(); await rows.nth(4).hover();
    const video = rows.nth(4).locator('video'); await video.waitFor({ state: 'attached' });
    assert.deepEqual(await video.evaluate(node => [node.muted, /^\/media\/intel-clip\d+\.mp4$/.test(new URL(node.src).pathname)]), [true, true]);
    await rows.nth(5).hover();
    assert.equal(await canvas.locator('video').count(), 1, 'one preview plays at a time'); assert.equal(await rows.nth(4).locator('video').count(), 0);
    await canvas.locator('h1').hover();
    assert.equal(await canvas.locator('video').count(), 0, 'the preview stops when the pointer leaves');
    await rows.nth(5).getByRole('link').focus(); await h.page.keyboard.press('Tab');
    assert.equal(await rows.nth(6).locator('video').count(), 1, 'keyboard focus plays the preview too');
    // A change of period with the same records leaves the list, its scroll position and the focus alone.
    const kept = await list.evaluate(node => { node.scrollTop = 300; node.dataset.kept = 'yes'; return node.scrollTop; });
    await h.page.evaluate(() => window.dispatchEvent(new CustomEvent('rocoto:period', { detail: { period: 'month' } })));
    assert.equal(await canvas.getAttribute('data-period'), 'month');
    assert.equal(await canvas.locator('.tc-rail-scroll').evaluate(node => node.scrollTop), kept);
    assert.equal(await h.page.evaluate(() => document.activeElement.closest('.tc-occurrence')?.dataset.occurrenceId), await rows.nth(6).getAttribute('data-occurrence-id'));
  } finally { await h.close(); }
});

test('the timeline is a moving feed: newest first, a memo after each set, key points pinned, new updates held for a reader', async () => {
  const h = await appFor(1600, { config: { liveRefreshMs: 1200 } });
  try {
    const canvas = await open(h, 'Tools aisle today');
    assert.equal(await canvas.getAttribute('data-family'), 'live');
    assert.deepEqual(await lines(canvas.getByRole('region', { name: 'Key points' })), ['Key points', 'Thirteen updates today, six of them since 15:00.', 'Cable ties are the most handled product: picked up twice, with a team member called once.', 'Two products went back on their hooks: a pocket saw and a pack of hooks.']);
    const feed = canvas.locator('.tc-feed > li');
    assert.deepEqual(await feed.evaluateAll(items => items.slice(0, 8).map(item => [item.className.split(' ')[0], item.querySelector('.tc-time, h2').textContent])), [['tc-feed-day', 'Today'], ['tc-feed-entry', '16:38'], ['tc-feed-entry', '16:31'], ['tc-feed-entry', '16:24'], ['tc-feed-memo', '16:00'], ['tc-feed-entry', '15:52'], ['tc-feed-entry', '15:40'], ['tc-feed-entry', '15:21']]);
    assert.equal(await canvas.locator('.tc-feed-memo').count(), 3);
    assert.deepEqual(await lines(canvas.locator('.tc-feed-memo').first()), ['16:00', 'Memo', 'The afternoon is busier than the morning: three visitors shared the aisle at 15:40 and cable ties moved twice.', 'Recommendation', 'Keep the cable ties topped up before 17:00.']);
    assert.deepEqual(await feed.evaluateAll(items => { const times = items.filter(item => !item.classList.contains('tc-feed-day')).map(item => item.querySelector('.tc-time').textContent); return times.join() === [...times].sort().reverse().join(); }), true, 'entries run newest first');
    assert.equal(await canvas.getByRole('button', { name: 'Earlier updates' }).count(), 0, 'a short timeline shows whole');
    // An update that arrives while the reader is at the top joins the feed and lights once.
    const first = { ...h.world.observations.live.incidents[0], id: 'event-new-1', description: 'A visitor picked up a pack of hooks.', captured_at: new Date(NOW - 60e3).toISOString(), committed_at: NOW / 1000 - 56 };
    h.world.observations.live.incidents.unshift(first); h.world.observations.live.event_count += 1;
    await canvas.locator('.tc-feed-entry', { hasText: 'A visitor picked up a pack of hooks.' }).waitFor();
    assert.equal(await canvas.locator('.tc-new-updates').count(), 0);
    // Further down the feed, new updates wait behind a control and the reader's place holds.
    await canvas.locator('.tc-feed-entry').last().scrollIntoViewIfNeeded();
    const place = await h.home.evaluate(node => node.scrollTop); assert.ok(place > 200);
    for (const [index, line] of ['Two visitors compared rope reels.', 'A team member refilled the cable ties.'].entries()) { h.world.observations.live.incidents.unshift({ ...first, id: 'event-new-' + (index + 2), description: line, captured_at: new Date(NOW - 30e3 + index * 1000).toISOString(), committed_at: NOW / 1000 - 26 + index }); h.world.observations.live.event_count += 1; }
    const more = canvas.getByRole('button', { name: '2 new updates' }); await more.waitFor();
    assert.equal(await canvas.locator('.tc-feed-entry', { hasText: 'Two visitors compared rope reels.' }).count(), 0);
    assert.ok(Math.abs(await h.home.evaluate(node => node.scrollTop) - place) < 4, 'the reader keeps their place');
    await more.click();
    await canvas.locator('.tc-feed-entry', { hasText: 'A team member refilled the cable ties.' }).waitFor();
    assert.equal(await canvas.locator('.tc-new-updates').count(), 0);
    assert.equal(await canvas.locator('.tc-feed-entry').first().innerText().then(text => text.includes('A team member refilled the cable ties.')), true);
    assert.equal(await h.page.evaluate(() => document.activeElement.closest('.tc-feed-entry')?.textContent.includes('A team member refilled the cable ties.')), true, 'focus lands on the newest update');
  } finally { await h.close(); }
});

test('a long timeline runs on: the next set of entries is drawn as the reader reaches the end', async () => {
  const world = fixtures();
  world.observations.live.incidents.push(...Array.from({ length: 60 }, (_, index) => event('profile-live', 'aisle_visit', `Earlier visit ${index + 1} to the tools aisle.`, new Date(NOW - 86400e3 - index * 600e3))));
  world.observations.live.event_count = world.observations.live.incidents.length;
  const h = await appFor(1600, { world });
  try {
    const canvas = await open(h, 'Tools aisle today'), entries = canvas.locator('.tc-feed-entry'), more = canvas.getByRole('button', { name: 'Earlier updates' });
    assert.equal(await canvas.locator('.tc-feed > li:not(.tc-feed-day)').count(), 20, 'the first set');
    assert.equal(await entries.count(), 17); assert.equal(await more.count(), 1);
    // Reaching the end draws the next set, and the reader's place holds.
    await h.home.evaluate(node => { node.scrollTop = node.scrollHeight; }); const place = await h.home.evaluate(node => node.scrollTop);
    await h.page.waitForFunction(() => document.querySelectorAll('.tc-feed-entry').length >= 37);
    assert.ok(await h.home.evaluate(node => node.scrollTop) >= place - 4);
    assert.deepEqual(await canvas.locator('.tc-feed-day h2').allInnerTexts(), ['Today', 'Yesterday']);
    // The control is a keyboard stop of its own: reaching it draws the next set and the focus stays in the feed.
    const before = await entries.count();
    await more.focus();
    await h.page.waitForFunction(count => document.querySelectorAll('.tc-feed-entry').length > count, before);
    assert.equal(await h.page.evaluate(() => !!document.activeElement.closest('.tc-feed-entry, .tc-feed-more')), true);
    for (let step = 0; step < 8 && await more.count(); step++) { await h.home.evaluate(node => { node.scrollTop = node.scrollHeight; }); await h.page.waitForTimeout(300); }
    assert.equal(await entries.count(), 73); assert.equal(await more.count(), 0);
  } finally { await h.close(); }
});

test('the shelf canvas shows each shelf now, its Empty since and Replenished times, and its history', async () => {
  const h = await appFor();
  try {
    const canvas = await open(h, 'Pegboard stock');
    assert.equal(await canvas.getAttribute('data-family'), 'state');
    assert.deepEqual(await lines(canvas.locator('.tc-counts')), ['Empty', '1', 'Replenished', '2', 'Stocked', '2']);
    const shelves = canvas.locator('.tc-shelf');
    assert.deepEqual(await shelves.evaluateAll(items => items.map(item => [item.querySelector('.tc-shelf-name').textContent, item.querySelector('.tc-shelf-state').textContent, item.dataset.state])), [['Rope reels, bay 1', 'Empty', 'empty'], ['Cable ties, bay 2', 'Replenished', 'replenished'], ['Garden ties, bay 4', 'Replenished', 'replenished'], ['Pocket saws, bay 5', 'Stocked', 'stocked'], ['Hooks and hangers, bay 3', 'Stocked', 'stocked']]);
    assert.deepEqual(await lines(shelves.nth(0).locator('.tc-shelf-facts')), ['Empty since', '15:48', '54 min']);
    assert.deepEqual(await lines(shelves.nth(1).locator('.tc-shelf-facts')), ['Empty since', '14:05', 'Replenished', '15:20', 'after 1 h 15 min']);
    assert.deepEqual(await lines(shelves.nth(2).locator('.tc-shelf-facts')), ['Empty since', 'Yesterday, 17:30', 'Replenished', '08:20', 'after 14 h 50 min']);
    assert.deepEqual(await lines(shelves.nth(3).locator('.tc-shelf-facts')), ['Stocked since', 'Thu 8 Oct, 08:15']);
    assert.match(await canvas.locator('.tc-rail-head').innerText(), /^History\s+9$/);
    assert.deepEqual((await lines(canvas.locator('.tc-occurrence').first())).slice(-2), ['Rope reels, bay 1 · Empty', '15:48']);
    // A shelf is a toggle: its own history, then every shelf again. The keyboard stays on the shelf.
    await shelves.nth(1).focus(); await h.page.keyboard.press('Enter');
    assert.match(await canvas.locator('.tc-rail-head').innerText(), /^Cable ties, bay 2\s+3\s+Show every shelf$/);
    assert.deepEqual(await canvas.locator('.tc-occurrence strong').allInnerTexts(), ['Cable ties, bay 2 · Replenished', 'Cable ties, bay 2 · Empty', 'Cable ties, bay 2 · Stocked']);
    assert.deepEqual(await h.page.evaluate(() => [document.activeElement.dataset.shelf, document.activeElement.getAttribute('aria-pressed')]), ['item-bay-2', 'true']);
    await h.page.keyboard.press('Space');
    assert.match(await canvas.locator('.tc-rail-head').innerText(), /^History\s+9$/);
    assert.equal(await h.page.evaluate(() => document.activeElement.dataset.shelf), 'item-bay-2');
  } finally { await h.close(); }
});

test('a tracker that declares the products family lists its products in order', async () => {
  const world = fixtures(), record = agent('rank', 'Most handled products', 'The products visitors handle most in the tools aisle.', { mode: 'scheduled', canvas: { family: 'ranking' } });
  const item = (ref, name, type, clock) => event('profile-rank', type, `A visitor ${type === 'pick_up' ? 'picked up' : type === 'put_back' ? 'put back' : 'bought'} ${name.toLowerCase()}.`, sofia(0, clock), { object_ref: ref, object_description: name, item_id: 'item-' + ref });
  world.agents.unshift(record);
  world.observations.rank = observed(record, [profile('profile-rank', 'Tools aisle', [['pick_up', 'x'], ['put_back', 'y'], ['purchase', 'z']])], [item('ties', 'Cable ties', 'pick_up', '10:00'), item('ties', 'Cable ties', 'pick_up', '11:00'), item('ties', 'Cable ties', 'purchase', '11:05'), item('ties', 'Cable ties', 'put_back', '12:00'), item('saw', 'Pocket saw', 'pick_up', '12:30'), item('saw', 'Pocket saw', 'put_back', '12:40'), item('rope', 'Rope reel', 'pick_up', '13:00')], null);
  const h = await appFor(1600, { world });
  try {
    const canvas = await open(h, 'Most handled products');
    assert.equal(await canvas.getAttribute('data-family'), 'ranking');
    assert.equal(await canvas.getByRole('button', { name: 'Canvas: Products' }).count(), 1);
    assert.deepEqual(await lines(canvas.locator('.tc-rank').first()), ['1', 'Cable ties', '2 picked up', '1 put back', '1 bought', '4']);
    await canvas.locator('.tc-rank').nth(1).click();
    assert.match(await canvas.locator('.tc-rail-head').innerText(), /^Pocket saw\s+2\s+Show every product$/);
  } finally { await h.close(); }
});

test('the canvas control shows the set with what each family needs, rests the ones the records cannot fill, and changes the canvas', async () => {
  const h = await appFor(), p = h.page;
  try {
    const canvas = await open(h, 'Long waits for help'), control = canvas.getByRole('button', { name: 'Canvas: Occurrences' }), menu = canvas.getByRole('menu', { name: 'Canvas' });
    assert.equal(await control.innerText(), 'Occurrences'); assert.equal(await control.getAttribute('aria-expanded'), 'false'); assert.equal(await menu.count(), 0);
    await control.focus(); await p.keyboard.press('Enter'); await menu.waitFor();
    const rows = () => menu.getByRole('menuitemradio').evaluateAll(items => items.map(item => [item.querySelector('.tc-pick-name').textContent, item.querySelector('.tc-pick-needs').textContent, item.getAttribute('aria-checked'), item.getAttribute('aria-disabled')]));
    assert.deepEqual(await rows(), [['Chart', 'Needs a number over time.', 'false', null], ['Timeline', 'Needs timed events.', 'false', null], ['Occurrences', 'Needs instances with a thumbnail.', 'true', null], ['Shelves', 'Needs shelf states.', 'false', 'true'], ['Products', 'Needs events that name a product.', 'false', 'true']]);
    const focused = () => p.evaluate(() => document.activeElement.dataset.family || document.activeElement.getAttribute('aria-label'));
    assert.equal(await focused(), 'occurrences', 'the menu opens on the canvas in use');
    // A family the records cannot fill is read like the others and stays at rest.
    await p.keyboard.press('ArrowDown'); assert.equal(await focused(), 'state');
    await p.keyboard.press('Enter'); await p.waitForTimeout(100);
    assert.equal(await menu.count(), 1); assert.equal(await canvas.getAttribute('data-family'), 'occurrences');
    await p.keyboard.press('Escape');
    assert.equal(await menu.count(), 0); assert.equal(await focused(), 'Canvas: Occurrences', 'Escape hands the focus back');
    // Picking a family the records can fill redraws the canvas from the same records.
    await control.click(); await menu.getByRole('menuitemradio', { name: /Timeline/ }).click();
    assert.equal(await canvas.getAttribute('data-family'), 'live');
    assert.equal(await canvas.getByRole('button', { name: 'Canvas: Timeline' }).evaluate(node => node === document.activeElement), true);
    assert.equal(await canvas.locator('.tc-feed-entry').count(), 12); assert.equal(await canvas.locator('.tc-feed-memo').count(), 1);
    assert.equal(await canvas.locator('.phone-block').count(), 1, 'the phone block stays through the change');
    // The pick is saved with the tracker: one POST with the revision read, and the browser keeps nothing of its own.
    await p.waitForFunction(() => document.querySelector('.tc-canvas').dataset.revision.endsWith('presentation-1'));
    assert.deepEqual(h.saves, [{ id: 'waits', body: { expected_revision: 0, canvas: { family: 'live' } } }]);
    assert.equal(h.world.observations.waits.presentation.canvas.family, 'live');
    assert.deepEqual(await p.evaluate(() => Object.keys(localStorage).filter(key => key.startsWith('rocoto.canvas'))), []);
    await back(h); const again = await open(h, 'Long waits for help');
    assert.equal(await again.getAttribute('data-family'), 'live');
    // Another browser opens the same tracker on the saved canvas.
    const other = await appFor(1600, { world: h.world });
    try { assert.equal(await (await open(other, 'Long waits for help')).getAttribute('data-family'), 'live'); assert.deepEqual(other.saves, []); } finally { await other.close(); }
    // A press outside closes the menu.
    await again.getByRole('button', { name: 'Canvas: Timeline' }).click(); await menu.waitFor(); await shot(p, 'canvas-control-1600');
    await again.locator('h1').click(); assert.equal(await menu.count(), 0);
    await again.getByRole('button', { name: 'Canvas: Timeline' }).click(); await menu.getByRole('menuitemradio', { name: /Chart/ }).click();
    assert.deepEqual(await lines(again.locator('.tc-figure')), ['12', 'this week']);
    await p.waitForFunction(() => document.querySelector('.tc-canvas').dataset.revision.endsWith('presentation-2'));
    assert.deepEqual(h.saves.at(-1), { id: 'waits', body: { expected_revision: 1, canvas: { family: 'metric' } } }, 'the next pick names the revision the canvas read');
    // A tracker with zero events sees the whole set at rest, each row saying what it needs.
    await back(h); const empty = await open(h, 'Queue watch');
    await empty.getByRole('button', { name: 'Canvas: Occurrences' }).click();
    assert.deepEqual((await rows()).map(row => row[3]), ['true', 'true', 'true', 'true', 'true']);
  } finally { await h.close(); }
});

test('a pick the server refuses returns the canvas to what is saved, under the server’s sentence for a person', async () => {
  const sentence = 'The canvas is saved with this tracker once it belongs to a location. Its chat sets that up.';
  const h = await appFor(1600, { answer: () => ({ status: 409, detail: sentence }) });
  try {
    const canvas = await open(h, 'Long waits for help');
    await canvas.getByRole('button', { name: 'Canvas: Occurrences' }).click(); await canvas.getByRole('menuitemradio', { name: /Timeline/ }).click();
    await canvas.locator('.tc-notice').waitFor();
    assert.equal(await canvas.locator('.tc-notice').innerText(), sentence); assert.equal(await canvas.locator('.tc-notice').getAttribute('role'), 'status');
    // The screen shows what the tracker holds: the canvas the harness reads, with the sentence beside it.
    assert.equal(await canvas.getAttribute('data-family'), 'occurrences'); assert.equal(h.world.observations.waits.presentation, undefined);
    assert.equal(await canvas.getByRole('button', { name: 'Canvas: Occurrences' }).count(), 1);
    assert.equal(h.saves.length, 1, 'a tracker whose presentation stands as it was read is asked once');
    await back(h); const again = await open(h, 'Long waits for help');
    assert.equal(await again.getAttribute('data-family'), 'occurrences'); assert.equal(await again.locator('.tc-notice').count(), 0);
    assert.equal(NEGATIVE.test(sentence), false);
  } finally { await h.close(); }
});

test('a presentation the harness saved in between is read, and the pick is sent once more with its revision', async () => {
  const world = presented(), h = await appFor(1600, { world, config: { liveRefreshMs: 60000 } });
  try {
    const canvas = await open(h, 'Freezer door left open');
    // The harness saves revision 2 in the chat while the canvas is open.
    const seen = world.observations.freezer;
    world.observations.freezer = { ...seen, presentation: { ...seen.presentation, revision: 2, saved_by: 'harness' }, output: { ...seen.output, tile: { ...seen.output.tile, presentation_revision: 2 } } };
    await canvas.getByRole('button', { name: 'Canvas: Chart' }).click(); await canvas.getByRole('menuitemradio', { name: /Occurrences/ }).click();
    await h.page.waitForFunction(() => document.querySelector('.tc-canvas').dataset.revision.endsWith('presentation-3'));
    assert.deepEqual(h.saves.map(save => save.body), [{ expected_revision: 1, canvas: { family: 'occurrences' } }, { expected_revision: 2, canvas: { family: 'occurrences' } }]);
    assert.equal(await canvas.getAttribute('data-family'), 'occurrences'); assert.equal(await canvas.locator('.tc-notice').count(), 0);
    assert.equal(world.observations.freezer.presentation.canvas.family, 'occurrences');
  } finally { await h.close(); }
  // A canvas the output leaves unfilled answers the sentence its control shows, and the canvas stays as saved.
  const refused = await appFor(1600, { world: presented(), answer: () => ({ status: 400, detail: 'Needs timed events.' }) });
  try {
    const canvas = await open(refused, 'Freezer door left open');
    await canvas.getByRole('button', { name: 'Canvas: Chart' }).click(); await canvas.getByRole('menuitemradio', { name: /Timeline/ }).click();
    await canvas.locator('.tc-notice').waitFor();
    assert.deepEqual([await canvas.locator('.tc-notice').innerText(), await canvas.getAttribute('data-family'), refused.saves.length], ['Needs timed events.', 'metric', 1]);
    assert.deepEqual(await lines(canvas.locator('.tc-figure-box')), ['42', 'times', '4 Oct to 10 Oct 2026']);
  } finally { await refused.close(); }
});

test('a pick an earlier version kept in the browser moves to the tracker once', async () => {
  const world = fixtures(), h = await appFor(1600, { world });
  try {
    await h.page.evaluate(() => { localStorage.setItem('rocoto.canvas.family.waits', 'live'); localStorage.setItem('rocoto.canvas.family.assist', 'heatmap'); });
    const canvas = await open(h, 'Long waits for help');
    await h.page.waitForFunction(() => localStorage.getItem('rocoto.canvas.family.waits') === null);
    await h.page.locator('.tc-canvas[data-family="live"]').waitFor();
    assert.deepEqual(h.saves, [{ id: 'waits', body: { expected_revision: 0, canvas: { family: 'live' } } }]);
    assert.equal(world.observations.waits.presentation.saved_by, 'person'); assert.equal(await canvas.getAttribute('data-family'), 'live');
    // A second visit finds the tracker's own presentation and sends nothing.
    await back(h); await open(h, 'Long waits for help'); await h.page.waitForTimeout(200);
    assert.equal(h.saves.length, 1);
    // A name outside the set has no canvas: the browser's copy goes and nothing is sent.
    await back(h); await open(h, 'Staff assists');
    await h.page.waitForFunction(() => localStorage.getItem('rocoto.canvas.family.assist') === null);
    assert.equal(h.saves.length, 1); assert.equal(await h.home.locator('.tc-canvas').getAttribute('data-family'), 'metric');
  } finally { await h.close(); }
  // An answer about the session or the service keeps the browser's copy for the next visit; a refusal of the kit ends it.
  for (const [status, kept] of [[503, 'live'], [401, 'live'], [429, 'live'], [409, 'live'], [400, null]]) {
    const failing = await appFor(1600, { answer: () => ({ status, detail: status === 400 ? 'Needs timed events.' : status === 409 ? 'The canvas is saved with this tracker once it belongs to a location. Its chat sets that up.' : undefined }) });
    try {
      await failing.page.evaluate(() => localStorage.setItem('rocoto.canvas.family.waits', 'live'));
      const canvas = await open(failing, 'Long waits for help');
      await canvas.locator('.tc-notice').waitFor();
      assert.equal(await failing.page.evaluate(() => localStorage.getItem('rocoto.canvas.family.waits')), kept, `the browser’s copy after ${status}`);
      assert.equal(await canvas.getAttribute('data-family'), 'occurrences', 'the canvas shows what the tracker holds');
      assert.equal(NEGATIVE.test(await canvas.locator('.tc-notice').innerText()), false);
    } finally { await failing.close(); }
  }
});

test('a tracker with a published output draws its canvas from that output, in the order its presentation names', async () => {
  const world = presented(), h = await appFor(1600, { world });
  try {
    const canvas = await open(h, 'Freezer door left open');
    assert.deepEqual([await canvas.getAttribute('data-family'), await canvas.getAttribute('data-output'), await canvas.getAttribute('data-rail')], ['metric', '4', 'true']);
    assert.equal(await canvas.getByRole('heading', { level: 1 }).innerText(), 'Freezer door left open', 'the heading is the presentation’s label');
    // The number is the output's own: 42 this week, though the canvas lists three of its rows.
    assert.deepEqual(await lines(canvas.locator('.tc-figure-box')), ['42', 'times', '4 Oct to 10 Oct 2026']);
    // The list says how many of the output's rows it shows.
    assert.match(await canvas.locator('.tc-rail-head').innerText(), /^Times it stayed open\s+3 of 42$/); assert.equal(await canvas.locator('.tc-occurrence').count(), 3);
    assert.deepEqual(await canvas.locator('.tc-main > *').evaluateAll(items => items.map(item => item.className.split(' ')[0])), ['tc-instrument', 'tc-rich']);
    assert.equal(await canvas.locator('.tc-instrument .tc-figure-box, .tc-instrument .tc-period, .tc-instrument .tc-chart').count(), 3, 'the number, the period and the line share one panel');
    assert.match(await canvas.locator('.tc-summary').innerText(), /^The freezer door stayed open 42 times this week/);
    // The line is the published series: seven days, read in the tile's own words.
    const chart = canvas.getByRole('slider', { name: 'Freezer door left open over time' });
    await chart.focus(); await h.page.keyboard.press('End');
    assert.equal(await chart.getAttribute('aria-valuetext'), '6 times, Sat 10 Oct');
    await h.page.keyboard.press('ArrowLeft'); await h.page.keyboard.press('ArrowLeft');
    assert.equal(await chart.getAttribute('aria-valuetext'), '8 times, Thu 8 Oct');
    assert.deepEqual(await canvas.locator('.tc-chart-ticks span').allInnerTexts(), ['Sun', 'Tue', 'Thu', 'Sat']);
    // The period reads the output's own readings.
    await canvas.getByRole('radio', { name: 'Monthly' }).click();
    assert.deepEqual(await lines(canvas.locator('.tc-figure-box')), ['180', 'times', '11 Sep to 10 Oct 2026']);
    await canvas.getByRole('radio', { name: 'Annual' }).click();
    assert.deepEqual(await lines(canvas.locator('.tc-figure-box')), ['1,900', 'times', '11 Oct 2025 to 10 Oct 2026']);
    assert.match(await canvas.locator('.tc-rail-head').innerText(), /^Times it stayed open\s+3 of 1,900$/);
    await canvas.getByRole('radio', { name: 'Weekly' }).click();
    // The harness saves another order: the summary now stands before the line, each in its own place.
    const seen = world.observations.freezer, blocks = [{ block: 'figure' }, { block: 'period' }, { block: 'summary', bind: { text: 'summary' } }, { block: 'line', bind: { series: 'series' } }, { block: 'instances', bind: { rows: 'rows' } }];
    const reorder = item => ({ ...item, presentation: { family: 'metric', blocks, revision: 2 } });
    world.observations.freezer = { ...seen, presentation: { ...seen.presentation, revision: 2, canvas: { family: 'metric', blocks } },
      output: { ...seen.output, tile: { ...seen.output.tile, presentation_revision: 2, periods: Object.fromEntries(Object.entries(seen.output.tile.periods).map(([name, part]) => [name, { ...part, canvas: reorder(part.canvas) }])) }, canvas: reorder(seen.output.canvas) } };
    await back(h); const again = await open(h, 'Freezer door left open');
    await h.page.waitForFunction(() => document.querySelector('.tc-canvas').dataset.revision.endsWith('presentation-2'));
    assert.deepEqual(await again.locator('.tc-main > *').evaluateAll(items => items.map(item => item.className.split(' ')[0])), ['tc-lead', 'tc-rich', 'tc-instrument']);
    assert.match(await again.locator('.tc-rail-head').innerText(), /^Events\s+3 of 42$/, 'a list without a title of its own takes the family’s');
    // A block whose field the output stopped publishing rests, and the canvas says what it needs.
    const lacking = item => ({ ...item, problems: [{ block: 'line', field: 'series', needs: 'Needs a number over time.', sentence: 'The line block binds series to «series», which is a name the output leaves out. Needs a number over time.' }] });
    const now = world.observations.freezer;
    world.observations.freezer = { ...now, output: { ...now.output, revision: 5, tile: { ...now.output.tile, revision: 5, periods: Object.fromEntries(Object.entries(now.output.tile.periods).map(([name, part]) => [name, { ...part, canvas: lacking(part.canvas) }])) }, canvas: lacking(now.output.canvas) } };
    await back(h); const resting = await open(h, 'Freezer door left open');
    await h.page.waitForFunction(() => document.querySelector('.tc-canvas').dataset.output === '5');
    assert.equal(await resting.locator('.tc-needs').first().innerText(), 'Needs a number over time.');
    assert.deepEqual(await resting.locator('.tc-main > *').evaluateAll(items => items.map(item => item.className.split(' ')[0])), ['tc-lead', 'tc-rich']);
  } finally { await h.close(); }
});

test('the tile on Home and the canvas of one tracker show the same number from the same output revision', async () => {
  const world = presented(), h = await appFor(1600, { world });
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 }, timezoneId: 'Europe/Sofia', locale: 'en-GB' }), home = await context.newPage(), errors = [];
  try {
    home.on('pageerror', error => errors.push(error.message));
    await home.route('**/*', route => route.request().url().startsWith(origin + '/') ? route.fallback() : route.abort());
    await home.goto(origin + '/home-tiles.html'); await home.waitForFunction(() => window.LocationView && window.RocotoHomeModel);
    // Home draws the very rows the canvases read: the page rows of the trackers' outputs.
    const rows = Object.values(world.observations).map(item => item.output.tile);
    await home.evaluate(given => { window.view = window.LocationView.create({ root: document.querySelector('#locationRoot'), agents: false, data: { location: { id: 'sofia', name: 'Vitosha Boulevard 85' }, currency: 'EUR', core_trackers: given } }); window.view.show(); }, rows);
    const tileOf = key => home.evaluate(id => { const node = document.querySelector(`.home-tile[data-tile="${id}"]`), tile = window.view.model().tile(id); return { title: node.querySelector('.home-tile-title').textContent, number: node.querySelector('.home-tile-value').textContent, unit: node.querySelector('.home-tile-unit').textContent, mark: node.dataset.mark, drawn: !!node.querySelector('.home-tile-mark svg'), revision: tile.revision, presentation: tile.presentation_revision, agent: tile.agent_id }; }, key);
    const wanted = { freezer: ['42', 'times', 'line'], checks: ['86.5', '%', 'ring'], returns: ['€84.50', '', 'bars'], chiller: ['1', 'empty', 'ring'], handled: ['31', 'handled', 'line'], wait: ['1:35', 'min', 'bars'] };
    for (const record of world.agents) {
      const tile = await tileOf(record.id), label = world.observations[record.id].presentation.tile.label, canvas = await open(h, label);
      assert.deepEqual([tile.number, tile.unit, tile.mark], wanted[record.id], `${label} on Home`);
      assert.equal(tile.drawn, true, `${label} carries its mark`); assert.equal(tile.title, label); assert.equal(tile.agent, record.id);
      // The canvas: the same characters, from the output revision the tile names.
      assert.equal(await canvas.getAttribute('data-output'), String(tile.revision), `${label}: one output revision`);
      assert.ok((await canvas.getAttribute('data-revision')).endsWith(`output-${tile.revision}:presentation-${tile.presentation}`));
      if (record.id !== 'chiller') assert.deepEqual((await lines(canvas.locator('.tc-figure'))), [tile.number, tile.unit].filter(Boolean), `${label}: the canvas writes the tile’s number`);
      else assert.deepEqual(await lines(canvas.locator('.tc-counts')), ['Empty', '1', 'Replenished', '1', 'Stocked', '2'], 'the shelf canvas counts the same empty shelf');
      await back(h);
    }
    // A rate published as 86.5 reads 86.5 % on both, and a new output revision reaches both.
    assert.deepEqual(await lines((await open(h, 'Shelf checks on time')).locator('.tc-figure')), ['86.5', '%']);
    assert.deepEqual(errors, []);
  } finally { await context.close(); await h.close(); }
});

test('each family draws a published output through its blocks, and text a script wrote stays text', async () => {
  const world = presented();
  // A script wrote link syntax into every text field: the page shows its words and makes it nowhere a link.
  const link = ' See [the full report](https://example.test/report) and [](https://example.test/bare).';
  world.observations.checks.output.canvas.key_points[0] += link; world.observations.checks.output.canvas.memos[0].text += link; world.observations.checks.output.canvas.memos[0].recommendation += link;
  world.observations.returns.output.canvas.summary += link; world.observations.returns.output.canvas.advice += link;
  const h = await appFor(1600, { world });
  try {
    // Timeline: the rate, its ring, the key points and a comment between the events.
    const live = await open(h, 'Shelf checks on time');
    assert.deepEqual([await live.getAttribute('data-family'), await live.getAttribute('data-output')], ['live', '4']);
    assert.deepEqual(await lines(live.locator('.tc-figure')), ['86.5', '%']);
    assert.equal(await live.locator('.tc-gauge .rocoto-chart').count(), 1); assert.match(await live.locator('.tc-gauge [role="img"]').getAttribute('aria-label'), /^86\.5% of the whole$/);
    assert.deepEqual((await lines(live.getByRole('region', { name: 'Key points' }))).slice(0, 2), ['Key points', 'Seven of eight checks happened on time today. See the full report and https://example.test/bare.']);
    assert.deepEqual(await live.locator('.tc-feed > li').evaluateAll(items => items.slice(0, 5).map(item => [item.className.split(' ')[0], item.querySelector('.tc-time, h2').textContent])), [['tc-feed-day', 'Today'], ['tc-feed-entry', '16:05'], ['tc-feed-memo', '15:30'], ['tc-feed-entry', '15:02'], ['tc-feed-entry', '14:20']]);
    assert.deepEqual(await lines(live.locator('.tc-feed-memo').first()), ['15:30', 'Memo', 'The afternoon slipped once: the 14:00 check started at 14:20, and the next two were back on the hour. See the full report and https://example.test/bare.', 'Recommendation', 'Set a reminder for the 14:00 check on Saturdays. See the full report and https://example.test/bare.']);
    assert.equal(await live.locator('.tc-main a, .tc-live .tc-memo a').count(), 0, 'a timeline’s script text carries zero links');
    assert.equal(await live.locator('.phone-block').count(), 1);
    await back(h);
    // Occurrences: the amount, the facts, the summary, a recommendation and its list.
    const list = await open(h, 'Returns at the desk');
    assert.deepEqual(await lines(list.locator('.tc-lead .tc-figure')), ['€84.50']);
    assert.deepEqual(await lines(list.locator('.tc-updated')), ['Updated', 'Today 16:17', 'Runs', 'On request', 'Watches', 'Service desk']);
    assert.equal(await list.locator('.tc-summary').innerText(), 'Six returns reached the desk today, worth an estimated €84.50. Four of them came back in their packaging and went straight to the shelf. See the full report and https://example.test/bare.');
    assert.deepEqual(await lines(list.locator('.tc-recommendation')), ['Recommendation', 'Keep one team member at the desk between 12:00 and 14:00, when four of the six returns arrived. See the full report and https://example.test/bare.']);
    assert.equal(await list.locator('.tc-main a').count(), 0, 'script text carries zero links');
    assert.match(await list.locator('.tc-rail-head').innerText(), /^Returns\s+6$/); assert.equal(await list.locator('.tc-occurrence').count(), 6);
    assert.equal(await list.locator('.tc-occurrence img').count(), 6, 'each instance carries its thumbnail');
    await back(h);
    // Shelves: counts, a card for each shelf, and its changes; a shelf is a toggle for its own changes.
    const shelves = await open(h, 'Chiller shelves');
    assert.deepEqual(await lines(shelves.locator('.tc-counts')), ['Empty', '1', 'Replenished', '1', 'Stocked', '2']);
    assert.deepEqual(await shelves.locator('.tc-shelf').evaluateAll(items => items.map(item => [item.querySelector('.tc-shelf-name').textContent, item.dataset.state])), [['Yoghurt, top shelf', 'empty'], ['Milk, middle shelf', 'replenished'], ['Butter, lower shelf', 'stocked'], ['Juice, door rack', 'stocked']]);
    assert.deepEqual(await lines(shelves.locator('.tc-shelf').nth(0).locator('.tc-shelf-facts')), ['Empty since', '15:48', '54 min']);
    assert.deepEqual(await lines(shelves.locator('.tc-shelf').nth(1).locator('.tc-shelf-facts')), ['Empty since', '14:05', 'Replenished', '15:20', 'after 1 h 15 min']);
    assert.match(await shelves.locator('.tc-rail-head').innerText(), /^Changes\s+4$/);
    await shelves.locator('.tc-shelf').nth(1).click();
    assert.match(await shelves.locator('.tc-rail-head').innerText(), /^Milk, middle shelf\s+2\s+Show every shelf$/);
    await shelves.getByRole('button', { name: 'Show every shelf' }).click(); assert.equal(await shelves.locator('.tc-occurrence').count(), 4);
    await back(h);
    // Products: the published ranking with each product's count, and the sessions of a chosen product.
    const rank = await open(h, 'Most handled at the till');
    assert.deepEqual(await lines(rank.locator('.tc-figure')), ['31', 'handled']);
    assert.deepEqual(await lines(rank.locator('.tc-rank').first()), ['1', 'Chewing gum', '9 picked up', '3 put back', '2 bought', '14']);
    assert.equal(await rank.locator('.tc-rank').count(), 4);
    await rank.locator('.tc-rank').nth(1).click();
    assert.match(await rank.locator('.tc-rail-head').innerText(), /^Phone cable\s+2\s+Show every product$/);
    await back(h);
    // A duration with a band for each length of wait, and a canvas whose presentation names its main blocks alone.
    const wait = await open(h, 'Wait at the counter');
    assert.deepEqual(await lines(wait.locator('.tc-figure')), ['1:35', 'min']);
    assert.deepEqual([await wait.getAttribute('data-rail'), await wait.locator('.tc-rail').count(), await wait.getByRole('radiogroup', { name: 'Period' }).count()], ['false', 0, 0], 'a tracker published for one period shows that reading alone');
    assert.equal(await wait.locator('.tc-bars h2').innerText(), 'Waits by length');
    assert.deepEqual(await wait.locator('.tc-bars .rocoto-bars-labels li').evaluateAll(items => items.map(item => [item.querySelector('strong').textContent, item.querySelector('span').textContent])), [['14', 'Under 1 min'], ['21', '1 to 2 min'], ['9', '2 to 3 min'], ['4', 'Over 3 min']]);
    const [main, frame] = [await wait.locator('.tc-main').boundingBox(), await wait.locator('.tc-frame').boundingBox()];
    assert.ok(main.width > frame.width * .8, 'the main blocks take the whole width');
    // The canvas control reads the families this output fills.
    await wait.getByRole('button', { name: 'Canvas: Chart' }).click();
    assert.deepEqual(await wait.getByRole('menuitemradio').evaluateAll(items => items.map(item => [item.querySelector('.tc-pick-name').textContent, item.getAttribute('aria-disabled')])), [['Chart', null], ['Timeline', 'true'], ['Occurrences', null], ['Shelves', 'true'], ['Products', 'true']]);
  } finally { await h.close(); }
});

test('an open canvas of a published tracker reads again by itself and keeps its period, its scroll and its focus', async () => {
  const world = presented(), h = await appFor(1600, { world, config: { liveRefreshMs: 1200 } });
  try {
    // More rows, so the list scrolls.
    const seen = world.observations.freezer, many = Array.from({ length: 14 }, (_, n) => outputRow(n, new Date(NOW - (n + 1) * 5 * 3600e3), `The freezer door stayed open, time ${n + 1} of the month.`, { key: 'freezer', label: 'Door open' }));
    seen.output.tile.periods.month.canvas.rows = many;
    const canvas = await open(h, 'Freezer door left open'), phone = canvas.getByRole('region', { name: 'Alerts on your phone' });
    await phone.locator('.phone-participant').first().waitFor(); await phone.evaluate(node => { node.dataset.kept = 'yes'; });
    await canvas.getByRole('radio', { name: 'Monthly' }).click();
    assert.deepEqual(await lines(canvas.locator('.tc-figure-box')), ['180', 'times', '11 Sep to 10 Oct 2026']);
    const list = canvas.locator('.tc-rail-scroll'); await list.evaluate(node => { node.scrollTop = 260; });
    const place = await list.evaluate(node => node.scrollTop); assert.ok(place > 100);
    const chart = canvas.getByRole('slider', { name: 'Freezer door left open over time' }); await chart.focus();
    // The same output again: the canvas asks, and nothing on it is redrawn.
    await canvas.locator('.tc-main').evaluate(node => { node.dataset.kept = 'yes'; });
    const before = h.reads('freezer'); await h.page.waitForTimeout(2800);
    assert.ok(h.reads('freezer') >= before + 2, 'a canvas with a published output reads again on the timer');
    assert.equal(await canvas.locator('.tc-main').getAttribute('data-kept'), 'yes', 'the same revision leaves the canvas as it stands');
    // A tracker pass publishes revision 5: the month now reads 181.
    const next = JSON.parse(JSON.stringify(world.observations.freezer));
    next.output.revision = next.output.tile.revision = next.output.canvas.revision = 5; next.output.tile.periods.month.value = 181; next.output.tile.periods.week.value = 43;
    world.observations.freezer = next;
    await h.page.waitForFunction(() => document.querySelector('.tc-canvas').dataset.output === '5');
    assert.deepEqual(await lines(canvas.locator('.tc-figure-box')), ['181', 'times', '11 Sep to 10 Oct 2026'], 'the period the reader chose holds');
    assert.equal(await canvas.getByRole('radio', { name: 'Monthly' }).getAttribute('aria-checked'), 'true');
    assert.ok(Math.abs(await canvas.locator('.tc-rail-scroll').evaluate(node => node.scrollTop) - place) < 2, 'the list keeps its scroll position');
    assert.equal(await h.page.evaluate(() => document.activeElement.getAttribute('role')), 'slider', 'the focus stays on the chart');
    assert.equal(await phone.getAttribute('data-kept'), 'yes'); assert.equal(h.calls.filter(call => call.path === '/api/agents/freezer/phone').length, 1, 'one phone block, read once');
    assert.deepEqual(h.saves, []);
  } finally { await h.close(); }
});

test('a Core Tracker’s canvas is locked: its records, with neither the chat control, the canvas control nor a phone block', async () => {
  for (const width of [1600, 390]) {
    const h = await appFor(width, { frame: true }), p = h.page;
    try {
      const stub = await h.stub();
      await stub.evaluate(tracker => parent.postMessage({ type: 'rocoto:home-open-tracker', tracker }, location.origin), { key: 'empty_shelves', title: 'Empty shelves and replenishment', description: 'Every shelf the cameras see: stocked, empty or replenished.', family: 'state', observations: h.world.observations.stock });
      const canvas = h.home.locator('.tc-canvas'); await canvas.locator('h1', { hasText: 'Empty shelves and replenishment' }).waitFor();
      assert.deepEqual([await canvas.getAttribute('data-locked'), await canvas.getAttribute('data-family'), await canvas.getAttribute('aria-busy')], ['true', 'state', 'false']);
      assert.deepEqual(await lines(canvas.locator('.tc-counts')), ['Empty', '1', 'Replenished', '2', 'Stocked', '2']);
      assert.deepEqual(await canvas.locator('.tc-head').getByRole('button').allInnerTexts(), ['← Home'], 'a locked canvas carries its way back and nothing to change it');
      assert.equal(await canvas.getByRole('button', { name: /^Chat with|^Canvas:/ }).count(), 0);
      assert.equal(await canvas.locator('.phone-block, [data-cr-phone-slot]').count(), 0);
      await p.waitForTimeout(300);
      assert.deepEqual(h.calls.filter(call => /core|empty_shelves/.test(call.path)), [], 'a Core Tracker’s records ride with the request');
      assert.equal(await fitsWidth(p), true);
      await shot(p, `canvas-core-locked-${width}`);
      await back(h);
      await stub.waitForFunction(() => window.received.some(item => item.type === 'rocoto:home-focus-tile'));
      // The shell's location snapshot may land at any moment; the way back is the last word about a tile.
      assert.deepEqual((await stub.evaluate(() => window.received.filter(item => item.type === 'rocoto:home-focus-tile').at(-1))), { type: 'rocoto:home-focus-tile', key: 'empty_shelves' });
    } finally { await h.close(); }
  }
});

test('a tracker the shop made carries its chat control beside the heading and one phone block that stays through refreshes', async () => {
  const h = await appFor(1600, { config: { liveRefreshMs: 1200 } });
  try {
    for (const [title, id] of [['Staff assists', 'assist'], ['Long waits for help', 'waits'], ['Pegboard stock', 'stock'], ['Tools aisle today', 'live']]) {
      const canvas = await open(h, title), phone = canvas.getByRole('region', { name: 'Alerts on your phone' });
      await phone.locator('.phone-participant').first().waitFor();
      assert.equal(await canvas.locator('.phone-block').count(), 1);
      assert.equal(await canvas.evaluate(node => [...node.querySelectorAll('.cr-detail-heading, .cr-phone-slot, .cr-detail-grid')].map(item => item.className.split(' ')[0]).join(' > ')), 'cr-detail-heading > cr-phone-slot > cr-detail-grid');
      const chat = canvas.getByRole('button', { name: `Chat with ${title}` });
      assert.equal(await chat.innerText(), 'Chat');
      const [head, button] = [await canvas.locator('h1').boundingBox(), await chat.boundingBox()];
      assert.ok(button.x >= head.x + head.width && Math.abs((button.y + button.height / 2) - (head.y + head.height / 2)) < head.height / 2 + 4, 'the chat control sits beside the heading');
      assert.ok(button.height >= 40 && button.height <= 44, 'a small control with a full target');
      if (id === 'live') {
        // The timeline refreshes on its own; the phone block, its one read and the keyboard focus hold.
        await phone.getByRole('switch', { name: 'Alerts for Ivan Petrov' }).focus(); await phone.evaluate(node => { node.dataset.kept = 'yes'; });
        const before = h.reads('live'); await h.page.waitForTimeout(2800);
        assert.ok(h.reads('live') >= before + 2, 'the timeline reads again');
        assert.equal(await phone.getAttribute('data-kept'), 'yes');
        assert.equal(await h.page.evaluate(() => document.activeElement.getAttribute('aria-label')), 'Alerts for Ivan Petrov');
        assert.equal(h.calls.filter(call => call.path === '/api/agents/live/phone').length, 1);
        await chat.click();
        await h.page.locator('#messages .message', { hasText: 'Agent conversation ready.' }).waitFor();
        assert.equal(new URL(h.page.url()).searchParams.get('chat') || new URL(h.page.url()).searchParams.get('agent'), new URL(h.page.url()).searchParams.get('chat') ? 'chat-live' : 'live');
        break;
      }
      await back(h);
      assert.equal(await h.home.locator('.phone-block').count(), 0);
    }
  } finally { await h.close(); }
});

test('the keyboard reaches every control of a canvas in reading order', async () => {
  const h = await appFor();
  try {
    const canvas = await open(h, 'Staff assists'); await canvas.getByRole('region', { name: 'Alerts on your phone' }).locator('.phone-participant').first().waitFor();
    const focused = () => h.page.evaluate(() => { const node = document.activeElement; return node.getAttribute('aria-label') || node.textContent.trim().slice(0, 40) || node.tagName; });
    assert.equal(await focused(), '← Home');
    const order = [];
    for (let step = 0; step < 6; step++) { await h.page.keyboard.press('Tab'); order.push(await focused()); }
    assert.deepEqual(order, ['Chat with Staff assists', 'Canvas: Chart', 'Weekly', 'Staff assists over time', 'Phone alerts for this agent', 'Channel for Maria Ivanova']);
    for (const node of await canvas.locator('button, a, [tabindex="0"]').all()) { const box = await node.boundingBox(); if (box && await node.evaluate(item => !item.closest('.phone-block') && !item.matches('.tc-rail-scroll, .tc-chart, .tc-thumb-play'))) assert.ok(box.height >= 32, 'controls keep a target: ' + await node.innerText()); }
  } finally { await h.close(); }
});

test('canvas copy says what is, in the product’s own face, with numbers standing alone', async () => {
  const h = await appFor();
  try {
    const seen = [], collect = async locator => seen.push(...await lines(locator));
    // One face: the one the chats read in. Sizes hold a 12px floor; labels keep their own case.
    const typography = () => h.home.evaluate(node => { const face = getComputedStyle(document.body).fontFamily, faults = []; for (const item of node.querySelectorAll('.tc-canvas *')) { if (!item.textContent.trim() || item.closest('.phone-block, svg')) continue; const style = getComputedStyle(item); if (style.fontFamily !== face) faults.push('face: ' + style.fontFamily + ' ' + item.className); if (/mono|courier|menlo|consolas|fraunces|georgia|serif$/i.test(style.fontFamily.replace(/sans-serif/gi, ''))) faults.push('family: ' + style.fontFamily); if (style.textTransform === 'uppercase') faults.push('uppercase: ' + item.className); if (parseFloat(style.fontSize) < 12) faults.push('small: ' + item.className); } return [...new Set(faults)]; });
    assert.equal(await h.page.evaluate(() => getComputedStyle(document.body).fontFamily === getComputedStyle(document.querySelector('#message')).fontFamily), true, 'the shell and its chat share one face');
    for (const title of ['Staff assists', 'Long waits for help', 'Tools aisle today', 'Pegboard stock', 'Queue watch']) {
      const canvas = await open(h, title);
      await collect(canvas.locator('.tc-head')); await collect(canvas.locator('.tc-main')); await collect(canvas.locator('.cr-detail-grid'));
      await canvas.locator('.tc-pick-open').click(); await collect(canvas.locator('.tc-pick-menu')); assert.deepEqual(await typography(), [], title); await h.page.keyboard.press('Escape');
      assert.equal(await canvas.locator('.tc-head').evaluate(node => node.firstElementChild.tagName === 'BUTTON' && node.querySelector('h1').previousElementSibling === null), true, 'the heading stands first in its row');
      await back(h);
    }
    assert.ok(seen.length > 150);
    assert.deepEqual([...new Set(seen)].filter(line => NEGATIVE.test(line)), []);
    assert.deepEqual([...new Set(seen)].filter(line => PROVENANCE.test(line)), []);
    assert.deepEqual([...new Set(seen)].filter(line => /^[—–-]$|^Location$|Starts with/.test(line)), []);
    // Queue watch has zero events: its canvas shows 0 and a finished waiting list.
    const empty = await open(h, 'Queue watch');
    assert.deepEqual(await lines(empty.locator('.tc-lead .tc-figure')), ['0', 'occurrences']);
    assert.equal(await empty.locator('.tc-waiting p').innerText(), 'The first occurrence lands here.');
  } finally { await h.close(); }
});

test('reduced motion holds every mark still and previews wait for a press', async () => {
  const h = await appFor(1600, { reducedMotion: true });
  try {
    const canvas = await open(h, 'Staff assists');
    await canvas.locator('.tc-pick-open').click(); await canvas.getByRole('menu', { name: 'Canvas' }).waitFor();
    assert.deepEqual(await h.page.evaluate(() => document.getAnimations().filter(item => item.playState === 'running' && item.effect?.target?.closest?.('.tc-canvas')).map(item => item.animationName || 'transition')), []);
    await h.page.keyboard.press('Escape');
    await canvas.locator('.tc-occurrence').first().hover(); await h.page.waitForTimeout(200);
    assert.equal(await canvas.locator('video').count(), 0);
  } finally { await h.close(); }
});

test('a touch screen plays a preview from its own control', async () => {
  const h = await appFor(390, { touch: true });
  try {
    const canvas = await open(h, 'Long waits for help'), row = canvas.locator('.tc-occurrence').first(), play = row.getByRole('button', { name: 'Play preview' });
    await h.page.emulateMedia({ media: 'screen' });
    await play.scrollIntoViewIfNeeded();
    if (await play.isVisible()) { await play.tap(); await row.locator('video').waitFor({ state: 'attached' }); assert.equal(await play.getAttribute('aria-pressed'), 'true'); await play.tap(); assert.equal(await row.locator('video').count(), 0); }
    assert.match(await row.getByRole('link').getAttribute('href'), /\/\?clip=intel-clip\d+$/);
  } finally { await h.close(); }
});

test('without the canvas module the shell keeps its plain tracker view', async () => {
  const h = await appFor(1600, { withoutKit: true });
  try {
    await h.page.evaluate(() => window.dispatchEvent(new CustomEvent('rocoto:home-open-tracker', { detail: { agentId: 'waits' } })));
    const detail = h.home.locator('.cr-detail'); await detail.getByText('Twelve visitors waited over two minutes for help this week.', { exact: false }).first().waitFor();
    assert.equal(await detail.locator('.tc-frame').count(), 0);
    await detail.getByRole('region', { name: 'Alerts on your phone' }).locator('.phone-participant').first().waitFor();
    await detail.getByRole('button', { name: '← Home' }).click(); await h.home.locator('.cr-home').waitFor();
    assert.equal(await h.home.locator('.cr-detail').count(), 0);
  } finally { await h.close(); }
});

test('the four canvases fit 1600x1000 and 390x844', async () => {
  for (const width of [1600, 390]) {
    const h = await appFor(width, { location: true });
    try {
      if (bundle && shots) { await h.home.locator('.cr-location-frame').waitFor(); await h.page.waitForTimeout(2500); await shot(h.page, `home-${width}`); }
      assert.equal(await fitsWidth(h.page), true, `Home keeps its width at ${width}`);
      for (const [title, name] of [['Staff assists', 'chart'], ['Long waits for help', 'occurrences'], ['Tools aisle today', 'timeline'], ['Pegboard stock', 'shelves']]) {
        const canvas = await open(h, title);
        await canvas.getByRole('region', { name: 'Alerts on your phone' }).locator('.phone-participant').first().waitFor();
        assert.equal(await fitsWidth(h.page), true, `${title} keeps its width at ${width}`);
        const outside = await canvas.evaluate(node => { const frame = node.getBoundingClientRect(); return [...node.querySelectorAll('h1,h2,h3,p,strong,li,button,a,dd,dt,img,.tc-thumb')].filter(item => { const box = item.getBoundingClientRect(); return box.width > 0 && (box.left < frame.left - 1 || box.right > frame.right + 1); }).map(item => item.className + ':' + item.textContent.trim().slice(0, 30)); });
        assert.deepEqual(outside, [], `${title} holds its content at ${width}`);
        assert.deepEqual(await canvas.locator('.phone-block').evaluate(block => { const frame = block.getBoundingClientRect(); return [...block.querySelectorAll('h3,p,strong,input,select,button,label')].filter(item => { const box = item.getBoundingClientRect(); return box.width > 0 && (box.left < frame.left - 1 || box.right > frame.right + 1); }).map(item => item.getAttribute('aria-label') || item.textContent.trim().slice(0, 30)); }), [], `the phone block holds its controls at ${width}`);
        const clipped = await canvas.evaluate(node => [...node.querySelectorAll('.tc-head h1, .tc-figure, .tc-shelf, .tc-counts, .tc-updated, .tc-period, .tc-points, .tc-controls')].filter(item => item.scrollWidth > item.clientWidth + 1).map(item => item.className));
        assert.deepEqual(clipped, [], `${title} draws each block whole at ${width}`);
        await h.home.evaluate(node => { node.scrollTop = 0; });
        await shot(h.page, `canvas-${name}-${width}`);
        if (shots && width === 390) { await canvas.locator('.cr-detail-grid').evaluate(node => node.scrollIntoView({ block: 'start' })); await shot(h.page, `canvas-${name}-${width}-list`); }
        if (name === 'occurrences') {
          await h.home.evaluate(node => { node.scrollTop = 0; }); await canvas.locator('.tc-pick-open').click(); const menu = canvas.getByRole('menu', { name: 'Canvas' }); await menu.waitFor();
          const [box, view] = [await menu.boundingBox(), await h.home.boundingBox()]; assert.ok(box.x >= view.x && box.x + box.width <= view.x + view.width + 1, `the canvas set stays on screen at ${width}`);
          await shot(h.page, `canvas-set-${width}`); await h.page.keyboard.press('Escape');
        }
        await h.home.evaluate(node => { node.scrollTop = 0; });
        await back(h);
      }
    } finally { await h.close(); }
  }
});

test('a published tracker’s tile on Home and its canvas in each family fit 1600x1000 and 390x844', async () => {
  const core = [['visitors', 'Visitors', 142, ''], ['product_interactions', 'Product interactions', 354, ''], ['picked_up', 'Picked up', 225, ''], ['put_back', 'Put back', 62, ''], ['basket', 'Basket', 10.55, ''], ['queue_time', 'Queue time', 15, 's'], ['empty_shelves', 'Empty shelves', 2, ''], ['sales', 'Sales', 2638.9, ''], ['conversion', 'Conversion', 18.6, '%']]
    .map(([key, label, value, unit]) => ({ key, label, value, unit, state: 'ready', core: true, chat: false, locked: true, series: daily([120, 140, 150, 160, 170, 240, 112].map(point => Math.round(point * value / 112 * 10) / 10)), ...(key === 'basket' || key === 'sales' ? { currency: 'EUR', basis: 'estimated' } : {}) }));
  for (const width of [1600, 390]) {
    const world = presented(), h = await appFor(width, { world });
    try {
      for (const [id, name] of [['freezer', 'chart'], ['checks', 'timeline'], ['returns', 'occurrences'], ['chiller', 'shelves'], ['handled', 'products'], ['wait', 'bands']]) {
        const label = world.observations[id].presentation.tile.label, canvas = await open(h, label);
        await canvas.getByRole('region', { name: 'Alerts on your phone' }).locator('.phone-participant').first().waitFor();
        assert.equal(await fitsWidth(h.page), true, `${label} keeps its width at ${width}`);
        const outside = await canvas.evaluate(node => { const frame = node.getBoundingClientRect(); return [...node.querySelectorAll('h1,h2,h3,p,strong,li,button,a,dd,dt,img,.tc-thumb,.tc-gauge,.tc-bars')].filter(item => { const box = item.getBoundingClientRect(); return box.width > 0 && (box.left < frame.left - 1 || box.right > frame.right + 1); }).map(item => item.className + ':' + item.textContent.trim().slice(0, 30)); });
        assert.deepEqual(outside, [], `${label} holds its content at ${width}`);
        const clipped = await canvas.evaluate(node => [...node.querySelectorAll('.tc-head h1, .tc-figure, .tc-figure-box, .tc-lead-figure, .tc-shelf, .tc-counts, .tc-updated, .tc-period, .tc-points, .tc-controls, .tc-rank')].filter(item => item.scrollWidth > item.clientWidth + 1).map(item => item.className));
        assert.deepEqual(clipped, [], `${label} draws each block whole at ${width}`);
        // The count beside a list stays on one line: «3 of 42» reads as one figure.
        assert.deepEqual(await canvas.locator('.tc-rail-head .tc-count').evaluateAll(items => items.filter(item => item.getClientRects().length > 1 || item.offsetHeight > 30).map(item => item.textContent)), [], `${label}: the list’s count stands on one line at ${width}`);
        // The blocks of the first row stand side by side, clear of each other.
        const overlap = await canvas.evaluate(node => { const boxes = [...node.querySelectorAll('.tc-figure-box, .tc-gauge, .tc-period, .tc-updated')].map(item => item.getBoundingClientRect()).filter(box => box.width > 0); return boxes.some((a, i) => boxes.some((b, j) => i < j && a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1)); });
        assert.equal(overlap, false, `${label}: the first row’s blocks stand clear at ${width}`);
        await h.home.evaluate(node => { node.scrollTop = 0; });
        await shot(h.page, `presented-${name}-${width}`);
        if (shots && width === 390) { const rail = canvas.locator('.cr-detail-grid'); if (await rail.count()) { await rail.evaluate(node => node.scrollIntoView({ block: 'start' })); await shot(h.page, `presented-${name}-${width}-list`); } }
        await h.home.evaluate(node => { node.scrollTop = 0; });
        await back(h);
      }
    } finally { await h.close(); }
    // Home: the six tiles after the Core Trackers, each with the mark and the number its presentation names.
    const context = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 1000 }, timezoneId: 'Europe/Sofia', locale: 'en-GB' }), home = await context.newPage(), errors = [];
    try {
      home.on('pageerror', error => errors.push(error.message)); await home.clock.setFixedTime(new Date(NOW));
      await home.route('**/*', route => route.request().url().startsWith(origin + '/') ? route.fallback() : route.abort());
      await home.goto(origin + '/home-tiles.html'); await home.waitForFunction(() => window.LocationView && window.RocotoHomeModel);
      await home.evaluate(rows => { window.view = window.LocationView.create({ root: document.querySelector('#locationRoot'), agents: false, data: { location: { id: 'sofia', name: 'Vitosha Boulevard 85', address: '85 Vitosha Boulevard, Sofia' }, currency: 'EUR', core_trackers: rows } }); window.view.show(); }, [...core, ...Object.values(presented().observations).map(item => item.output.tile)]);
      await home.waitForFunction(() => document.querySelectorAll('#locationPageTiles .home-tile[data-tile] .home-tile-mark svg').length >= 11);
      const fit = await home.evaluate(() => { const w = document.documentElement.clientWidth;
        const squeezed = [...document.querySelectorAll('.home-tile[data-tile]')].filter(tile => { const copy = [...tile.querySelectorAll('.home-tile-figure,.home-tile-needs')].find(n => n.getClientRects().length), mark = tile.querySelector('.home-tile-mark').getBoundingClientRect(), box = tile.getBoundingClientRect(), text = copy.getBoundingClientRect(); return text.right > box.right - 8 || text.left < box.left || mark.width > 0 && (text.right > mark.left + .5 || mark.width < 24 || mark.right > box.right); }).map(tile => tile.dataset.tile);
        return { page: document.documentElement.scrollWidth <= w, squeezed }; });
      assert.deepEqual(fit, { page: true, squeezed: [] }, `Home’s tiles stand whole at ${width}`);
      assert.deepEqual(await home.locator('#locationPageTiles .home-tile[data-tile]').evaluateAll(items => items.slice(5).map(item => [item.querySelector('.home-tile-title').textContent, item.querySelector('.home-tile-value').textContent + item.querySelector('.home-tile-unit').textContent, item.dataset.mark])),
        [['Freezer door left open', '42times', 'line'], ['Shelf checks on time', '86.5%', 'ring'], ['Returns at the desk', '€84.50', 'bars'], ['Chiller shelves', '1empty', 'ring'], ['Most handled at the till', '31handled', 'line'], ['Wait at the counter', '1:35min', 'bars']]);
      if (shots) { await home.locator('#locationPageTiles').scrollIntoViewIfNeeded(); await home.evaluate(() => { const grid = document.querySelector('#locationPageTiles'); window.scrollTo(0, grid.getBoundingClientRect().top + window.scrollY - 24); }); await shot(home, `presented-home-tiles-${width}`); }
      assert.deepEqual(errors, []);
    } finally { await context.close(); }
  }
});

/* ---------- what a review broke, held ---------- */
test('a row of a published output shows and opens addresses of this origin alone', async () => {
  const world = presented(), seen = world.observations.returns, outside = 'https://evil.example';
  // A list a script wrote whole, bound as the rail, with addresses under every name a page ever read; and a row of the output's own with one.
  const own = [0, 1].map(n => ({ id: `own-${n}`, at: new Date(NOW - (n + 1) * 20 * 60e3).toISOString(), description: `A row the script wrote, ${n + 1}.`, evidence_url: outside + '/phish', href: outside + '/phish', thumbnail_url: outside + '/t.jpg',
    thumbnail: '//evil.example/protocol-relative.jpg', thumb: outside + '/thumb.jpg', image: outside + '/i.jpg', poster: outside + '/p.jpg', media_url: outside + '/v.mp4', media: outside + '/m.mp4' }));
  const blocks = [{ block: 'figure' }, { block: 'summary', bind: { text: 'summary' } }, { block: 'instances', title: 'Rows of its own', bind: { rows: 'my_rows' } }];
  const withOwn = canvas => ({ ...canvas, my_rows: own, presentation: { family: 'occurrences', blocks, revision: 1 } });
  world.observations.returns = { ...seen, presentation: { ...seen.presentation, canvas: { family: 'occurrences', blocks } }, output: { ...seen.output, canvas: withOwn(seen.output.canvas) } };
  world.observations.handled.output.canvas.products[0].thumb = outside + '/gum.jpg';
  world.observations.handled.output.canvas.rows[0].thumb = '//evil.example/row.jpg';
  const h = await appFor(1600, { world }), asked = [];
  h.page.on('request', request => { if (!request.url().startsWith(origin + '/')) asked.push(request.url()); });
  try {
    const canvas = await open(h, 'Returns at the desk');
    assert.match(await canvas.locator('.tc-rail-head').innerText(), /^Rows of its own\s+2$/);
    assert.deepEqual(await canvas.locator('.tc-occurrence strong').allInnerTexts(), ['A row the script wrote, 1.', 'A row the script wrote, 2.']);
    assert.deepEqual(await canvas.locator('.tc-rail').evaluate(node => [node.querySelectorAll('a').length, node.querySelectorAll('img').length, node.querySelectorAll('video, .tc-thumb-play').length]), [0, 0, 0], 'a script’s row is words and a time');
    await canvas.locator('.tc-occurrence').first().hover(); await h.page.waitForTimeout(200);
    await back(h);
    const rank = await open(h, 'Most handled at the till');
    assert.equal(await rank.locator('.tc-rank').first().locator('img').count(), 0); assert.equal(await rank.locator('.tc-rank').nth(1).locator('img').count(), 1, 'a picture this origin serves is shown');
    assert.equal(await rank.locator('.tc-occurrence img').count(), 7, 'seven of the eight rows carry a picture of this origin');
    assert.deepEqual(await h.page.evaluate(() => [...document.querySelectorAll('.tc-canvas a[href], .tc-canvas img[src], .tc-canvas video[src]')].map(node => node.href || node.src).filter(address => !address.startsWith(location.origin + '/'))), []);
    assert.deepEqual(asked, [], 'the page asks this origin alone');
  } finally { await h.close(); }
});

test('a tracker with no Tracking Agent opens the canvas its presentation names, from the row and the canvas its tile drew', async () => {
  // The location view: the tile of a tracker the harness made with save_tracker sends its published row and canvas to the shell.
  const made = presented().observations.handled, row = { ...made.output.tile }, canvas = made.output.canvas; delete row.agent_id;
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 }, timezoneId: 'Europe/Sofia', locale: 'en-GB' }), host = await context.newPage(), errors = [];
  let sent;
  try {
    host.on('pageerror', error => errors.push(error.message));
    await host.route('**/*', route => route.request().url().startsWith(origin + '/') ? route.fallback() : route.abort());
    await host.goto(origin + '/frame-stub.html');
    await host.evaluate(() => { window.posted = []; addEventListener('message', event => { if (event.origin === location.origin && event.data?.type === 'rocoto:home-open-tracker') window.posted.push(event.data); });
      const frame = document.createElement('iframe'); frame.src = '/home-tiles.html'; frame.style.cssText = 'width:1600px;height:900px;border:0'; document.body.append(frame); });
    const inner = host.frameLocator('iframe'); await host.waitForFunction(() => document.querySelector('iframe').contentWindow.LocationView && document.querySelector('iframe').contentWindow.RocotoHomeModel);
    await host.evaluate(data => { const page = document.querySelector('iframe').contentWindow; page.view = page.LocationView.create({ root: page.document.querySelector('#locationRoot'), agents: false, data }); page.view.show(); },
      { location: { id: 'sofia', name: 'Vitosha Boulevard 85' }, core_trackers: [row, { key: 'gate', label: 'Gate left open', value: null, state: 'needs', needs: 'Needs first run', core: false, kind: 'count', mark: 'line', view: 'count' }], core_tracker_canvases: { handled: canvas } });
    await inner.locator('.home-tile[data-tile="handled"]').click();
    await host.waitForFunction(() => window.posted.length === 1);
    sent = await host.evaluate(() => window.posted[0]);
    assert.deepEqual([sent.period, sent.tracker.key, sent.tracker.title, sent.agent_id], ['week', 'handled', 'Most handled at the till', undefined]);
    assert.deepEqual(sent.tracker.observations.presentation, { canvas: { family: 'ranking', blocks: canvas.presentation.blocks }, revision: 1 });
    assert.deepEqual(sent.tracker.observations.output, { revision: 4, tile: row, canvas }, 'the row and the canvas as published, on one revision');
    assert.equal(await host.evaluate(() => document.querySelector('iframe').contentWindow.document.querySelectorAll('dialog[open]').length), 0, 'its full view is the shell’s canvas');
    // A tracker ahead of its first output keeps the Home sheet, which says what it waits for.
    await inner.locator('.home-tile[data-tile="gate"]').click(); await inner.locator('dialog.rocoto-history, dialog[open]').first().waitFor();
    assert.equal(await host.evaluate(() => window.posted.length), 1);
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
  // The shell: that request draws the presentation's blocks, locked, at both sizes.
  for (const width of [1600, 390]) {
    const h = await appFor(width, { frame: true });
    try {
      const stub = await h.stub();
      await stub.evaluate(message => parent.postMessage(message, location.origin), sent);
      const shown = h.home.locator('.tc-canvas'); await shown.locator('h1', { hasText: 'Most handled at the till' }).waitFor();
      assert.deepEqual([await shown.getAttribute('data-locked'), await shown.getAttribute('data-family'), await shown.getAttribute('data-output'), await shown.getAttribute('aria-busy')], ['true', 'ranking', '4', 'false']);
      assert.deepEqual(await lines(shown.locator('.tc-figure')), ['31', 'handled']);
      assert.deepEqual(await lines(shown.locator('.tc-rank').first()), ['1', 'Chewing gum', '9 picked up', '3 put back', '2 bought', '14']); assert.equal(await shown.locator('.tc-rank').count(), 4);
      assert.match(await shown.locator('.tc-rail-head').innerText(), /^Sessions\s+8 of 31$/);
      assert.deepEqual(await shown.locator('.tc-head').getByRole('button').allInnerTexts(), ['← Home'], 'a tracker with no agent has no chat of its own and no phone block');
      assert.equal(await shown.locator('.phone-block, [data-cr-phone-slot]').count(), 0);
      await h.page.waitForTimeout(300);
      assert.deepEqual(h.calls.filter(call => /handled/.test(call.path)), [], 'its records ride with the request');
      assert.equal(await fitsWidth(h.page), true);
      await shot(h.page, `presented-no-agent-${width}`);
      await back(h);
      await stub.waitForFunction(() => window.received.some(item => item.type === 'rocoto:home-focus-tile'));
      assert.deepEqual((await stub.evaluate(() => window.received.filter(item => item.type === 'rocoto:home-focus-tile').at(-1))), { type: 'rocoto:home-focus-tile', key: 'handled' });
    } finally { await h.close(); }
  }
});

test('a paused tracker says Paused on its tile and beside its canvas heading', async () => {
  const world = presented();
  world.agents.find(item => item.id === 'freezer').status = 'paused';
  const h = await appFor(1600, { world });
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 }, timezoneId: 'Europe/Sofia', locale: 'en-GB' }), home = await context.newPage(), errors = [];
  try {
    home.on('pageerror', error => errors.push(error.message));
    await home.route('**/*', route => route.request().url().startsWith(origin + '/') ? route.fallback() : route.abort());
    await home.goto(origin + '/home-tiles.html'); await home.waitForFunction(() => window.LocationView && window.RocotoHomeModel);
    // Home holds the page rows of the published trackers and the agents list: the freezer's agent and one with no page row are paused.
    const agents = [...world.agents, { ...agent('door', 'Back door watch', 'Watches the back door.'), status: 'paused' }];
    await home.evaluate(data => { window.view = window.LocationView.create({ root: document.querySelector('#locationRoot'), data }); window.view.show(); },
      { location: { id: 'sofia', name: 'Vitosha Boulevard 85' }, currency: 'EUR', agents, core_trackers: Object.values(world.observations).map(item => item.output.tile) });
    const tiles = await home.locator('.home-tile[data-tile]').evaluateAll(items => items.map(item => [item.querySelector('.home-tile-title').textContent, item.dataset.state, item.querySelector('.home-tile-value').textContent, item.querySelector('.home-tile-needs').textContent, item.getAttribute('aria-label')]));
    assert.deepEqual(tiles.filter(tile => tile[1] === 'paused'), [['Freezer door left open', 'paused', '', 'Paused', 'Freezer door left open. Paused'], ['Back door watch', 'paused', '', 'Paused', 'Back door watch. Paused']]);
    assert.deepEqual(tiles.find(tile => tile[0] === 'Shelf checks on time').slice(1, 4), ['live', '86.5', '']);
    assert.equal(tiles.length, 7, 'one tile for each tracker');
    // The canvas says so beside its heading, and keeps the last published number.
    const canvas = await open(h, 'Freezer door left open');
    assert.deepEqual(await canvas.locator('.tc-title > *').evaluateAll(items => items.map(item => item.className.split(' ')[0] || item.tagName)), ['H1', 'tc-tag', 'tc-controls']);
    assert.equal(await canvas.locator('.tc-tag').innerText(), 'Paused');
    const [head, tag] = [await canvas.locator('h1').boundingBox(), await canvas.locator('.tc-tag').boundingBox()];
    assert.ok(tag.x >= head.x + head.width && tag.y >= head.y - 2 && tag.y + tag.height <= head.y + head.height + 8, 'the tag stands beside the heading');
    assert.deepEqual(await lines(canvas.locator('.tc-figure')), ['42', 'times']);
    await shot(h.page, 'presented-paused-1600');
    await back(h); assert.equal(await (await open(h, 'Shelf checks on time')).locator('.tc-tag').count(), 0);
    assert.deepEqual(errors, []);
  } finally { await context.close(); await h.close(); }
});

test('a tile whose mark waits for a field rests as its ghost, and the canvas says what it needs', async () => {
  const world = presented(), seen = world.observations.chiller;
  // The script stopped publishing the share the ring binds: the row says so, as the page data carries it.
  const problem = { block: 'tile', field: 'fraction', needs: 'Needs a share of a whole.', sentence: 'The tile binds fraction to «empty_share», which is a name the output leaves out. Needs a share of a whole. It reads: A number from 0 to 1.' };
  const row = { ...seen.output.tile, problems: [problem] }; delete row.fraction;
  world.observations.chiller = { ...seen, output: { ...seen.output, tile: row } };
  const h = await appFor(1600, { world });
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 }, timezoneId: 'Europe/Sofia', locale: 'en-GB' }), home = await context.newPage(), errors = [];
  try {
    home.on('pageerror', error => errors.push(error.message));
    await home.route('**/*', route => route.request().url().startsWith(origin + '/') ? route.fallback() : route.abort());
    await home.goto(origin + '/home-tiles.html'); await home.waitForFunction(() => window.LocationView && window.RocotoHomeModel);
    await home.evaluate(rows => { window.view = window.LocationView.create({ root: document.querySelector('#locationRoot'), agents: false, data: { location: { id: 'sofia', name: 'Vitosha Boulevard 85' }, currency: 'EUR', core_trackers: rows } }); window.view.show(); }, Object.values(world.observations).map(item => item.output.tile));
    await home.waitForFunction(() => document.querySelectorAll('.home-tile[data-tile] .home-tile-mark svg').length >= 6);
    const read = id => home.locator(`.home-tile[data-tile="${id}"]`).evaluate(node => ({ number: node.querySelector('.home-tile-value').textContent, ghost: !!node.querySelector('.home-tile-mark .rocoto-ghost-path'), rests: 'rests' in node.dataset, title: node.title, spoken: node.getAttribute('aria-label') }));
    const resting = await read('chiller'), drawn = await read('checks');
    assert.deepEqual([resting.number, resting.ghost, resting.rests, resting.title], ['1', true, true, 'Needs a share of a whole.'], 'the number stands, the mark rests');
    assert.equal(resting.spoken, 'Chiller shelves, 1 empty. The mark needs a share of a whole. Open history');
    assert.deepEqual([drawn.ghost, drawn.rests, drawn.title], [false, false, '']);
    await home.locator('#locationPageTiles').scrollIntoViewIfNeeded(); await shot(home, 'presented-home-tile-rests-1600');
    // The canvas names it under its heading, in a person's words, and draws every block the output fills.
    const canvas = await open(h, 'Chiller shelves');
    assert.equal(await canvas.locator('.tc-needs').first().innerText(), 'The tile’s mark needs a share of a whole.');
    assert.deepEqual(await lines(canvas.locator('.tc-counts')), ['Empty', '1', 'Replenished', '1', 'Stocked', '2']);
    assert.equal(NEGATIVE.test(await canvas.locator('.tc-needs').first().innerText()), false);
    // The compact tile of the canvas module rests the same way.
    const compact = await h.page.evaluate(({ agents, observations }) => Object.fromEntries(['chiller', 'checks'].map(id => [id, !!window.RocotoTrackerCanvas.tile({ agent: agents.find(item => item.id === id), observations: observations[id] }).element.querySelector('.rocoto-ghost-path')])), world);
    assert.deepEqual(compact, { chiller: true, checks: false });
    assert.deepEqual(errors, []);
  } finally { await context.close(); await h.close(); }
});

test('the canvas draws the family the tracker’s presentation names at every revision, under the agent’s own title until one is saved', async () => {
  const world = fixtures(), long = 'Every visitor who waited over two minutes at the pegboard';
  // Three linked agents ahead of a saved presentation: the read names Occurrences at revision 0, whatever the agent's mode.
  const unsaved = { schema: 'rocoto.tracker-presentation.v1', tile: { label: 'Every visitor who waited', mark: 'line', format: 'count', bind: { series: 'series' } }, canvas: { family: 'occurrences', blocks: BLOCKS.occurrences }, revision: 0, saved_at: null, saved_by: null };
  for (const id of ['assist', 'live', 'stock']) world.observations[id] = { ...world.observations[id], tracker_id: 'tracker-' + id, presentation: unsaved };
  world.agents.find(item => item.id === 'assist').title = long;
  const h = await appFor(1600, { world });
  try {
    for (const [title, id] of [[long, 'assist'], ['Tools aisle today', 'live'], ['Pegboard stock', 'stock']]) {
      const canvas = await open(h, title);
      assert.equal(await canvas.getAttribute('data-family'), 'occurrences', `${id} draws the family its read names`);
      assert.equal(await canvas.getByRole('heading', { level: 1 }).innerText(), title);
      await back(h);
    }
    // Its first output arrives: the same family, and the agent's full title still heads the canvas.
    const first = publish(world.agents.find(item => item.id === 'assist'), { ...unsaved }, { day: { value: 9, unit: 'assists', series: hourly([0, 1, 2, 1, 3, 2]), trend: daily([4, 6, 5, 7, 8, 6, 9]), rows: [outputRow(0, new Date(NOW - 3600e3), 'A team member helped a visitor choose cable ties.', { key: 'assist' })], total: 9, summary: 'Nine assists today.' } }, { open: ['metric', 'live', 'occurrences'] });
    world.observations.assist = { ...first, presentation: unsaved, output: { ...first.output, tile: { ...first.output.tile, presentation_revision: 0 }, canvas: { ...first.output.canvas, presentation: { family: 'occurrences', blocks: BLOCKS.occurrences, revision: 0 } } } };
    const canvas = await open(h, long);
    assert.deepEqual([await canvas.getAttribute('data-family'), await canvas.getAttribute('data-output')], ['occurrences', '4']);
    assert.equal(await canvas.getByRole('heading', { level: 1 }).innerText(), long, 'the default label is for the tile; the heading keeps the whole title');
    assert.deepEqual(await lines(canvas.locator('.tc-figure')), ['9', 'assists']);
    // A person saves a canvas: from here the presentation's label heads it.
    await canvas.getByRole('button', { name: 'Canvas: Occurrences' }).click(); await canvas.getByRole('menuitemradio', { name: /Chart/ }).click();
    await h.page.waitForFunction(() => document.querySelector('.tc-canvas').dataset.revision.endsWith('presentation-1'));
    assert.equal(await canvas.getByRole('heading', { level: 1 }).innerText(), 'Every visitor who waited');
  } finally { await h.close(); }
});

test('a canvas whose first read has yet to answer rests under its heading, and draws its numbers once the read answers', async () => {
  for (const status of [500, 401]) {
    const world = presented(); let failing = true;
    const h = await appFor(1600, { world, config: { liveRefreshMs: 1200 }, failRead: () => failing ? status : 0 });
    try {
      await h.page.evaluate(() => window.dispatchEvent(new CustomEvent('rocoto:home-open-tracker', { detail: { agentId: 'wait' } })));
      const canvas = h.home.locator('.tc-canvas'); await canvas.locator('.tc-notice').waitFor();
      assert.equal(await canvas.getAttribute('aria-busy'), 'true');
      assert.equal(await canvas.locator('.tc-notice').innerText(), status === 401 ? 'Open your workspace to review the control room.' : 'The control room is reconnecting.');
      assert.deepEqual(await canvas.locator('.tc-main > *').evaluateAll(items => items.map(item => item.className)), ['tc-figure tc-figure-rest'], 'the heading over a resting figure');
      assert.equal(await canvas.locator('.tc-numeral, .tc-chart, .tc-rail, .tc-period').count(), 0, 'numbers come from a read that answered');
      assert.doesNotMatch(await canvas.locator('.tc-main').innerText(), /\d/);
      assert.equal(await canvas.getByRole('heading', { level: 1 }).innerText(), 'Wait at the counter');
      if (status === 500) await shot(h.page, 'canvas-first-read-rests-1600');
      // The canvas asks again by itself; the read answers and the numbers arrive.
      failing = false;
      await h.page.locator('.tc-canvas[aria-busy="false"]').waitFor();
      assert.deepEqual(await lines(canvas.locator('.tc-figure')), ['1:35', 'min']); assert.equal(await canvas.locator('.tc-notice').count(), 0);
      // A read that fails after one that answered leaves the numbers where they stand.
      failing = true; const before = h.reads('wait'); await h.page.waitForTimeout(2600);
      assert.ok(h.reads('wait') > before); assert.deepEqual(await lines(canvas.locator('.tc-figure')), ['1:35', 'min']); assert.equal(await canvas.getAttribute('aria-busy'), 'false');
      failing = false;
    } finally { await h.close(); }
  }
});

test('a Home tile holds a label and a unit at the kit’s limits beside its mark', async () => {
  assert.deepEqual([KIT.limits.label, KIT.limits.unit], [24, 8]);
  const label = 'Doors left open at night', unit = 'visitors';
  assert.deepEqual([label.length, unit.length], [KIT.limits.label, KIT.limits.unit]);
  const made = (key, value, mark, more = {}) => ({ key, label, value, unit, state: 'ready', needs: null, series: daily([4, 6, 5, 7, 8, 6, 6]), summary: '', events: [], tracker_id: 'tracker-' + key, core: false, chat: true, locked: false, revision: 1,
    kind: 'count', mark, view: 'count', presentation_revision: 1, problems: [], ...(mark === 'ring' || mark === 'arc' ? { fraction: .4 } : {}), ...more });
  const rows = [made('short', 42, 'line'), made('long', 1900, 'line'), made('bars', 1900, 'bars'), made('ring', 1900, 'ring'), made('arc', 1900, 'arc'), made('script', 42, 'line', { unit: 'doors left open today' })];
  for (const width of [1600, 390]) {
    const context = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : 1000 }, timezoneId: 'Europe/Sofia', locale: 'en-GB' }), home = await context.newPage(), errors = [];
    try {
      home.on('pageerror', error => errors.push(error.message));
      await home.route('**/*', route => route.request().url().startsWith(origin + '/') ? route.fallback() : route.abort());
      await home.goto(origin + '/home-tiles.html'); await home.waitForFunction(() => window.LocationView && window.RocotoHomeModel);
      await home.evaluate(given => { window.view = window.LocationView.create({ root: document.querySelector('#locationRoot'), agents: false, data: { location: { id: 'sofia', name: 'Vitosha Boulevard 85' }, core_trackers: given } }); window.view.show(); }, rows);
      await home.waitForFunction(() => { const tiles = [...document.querySelectorAll('.home-tile[data-tile]')]; return tiles.length === 6 && tiles.every(tile => tile.querySelector('.home-tile-mark svg')); });
      const seen = await home.locator('.home-tile[data-tile]').evaluateAll(items => items.map(tile => { const title = tile.querySelector('.home-tile-title'), mark = tile.querySelector('.home-tile-mark').getBoundingClientRect(), figure = tile.querySelector('.home-tile-figure').getBoundingClientRect(), row = tile.querySelector('.home-tile-row').getBoundingClientRect(), unit = tile.querySelector('.home-tile-unit');
        return { key: tile.dataset.tile, cut: title.scrollWidth > title.clientWidth, mark: Math.round(mark.width), inside: mark.right <= row.right + .5 && figure.right <= mark.left + .5, unit: unit.hidden ? null : unit.textContent, spoken: tile.getAttribute('aria-label') }; }));
      // The mark keeps its room at every width: a unit that would take it steps aside and stays in the spoken name.
      for (const tile of seen) { assert.ok(tile.mark >= 24 && tile.inside, `${tile.key} keeps its mark at ${width}: ${JSON.stringify(tile)}`); assert.match(tile.spoken, tile.key === 'script' ? / doors left open today\. Open history$/ : / visitors\. Open history$/); }
      assert.equal(seen.find(tile => tile.key === 'short').unit, 'visitors', 'a unit that fits beside the mark is shown');
      assert.equal(seen.find(tile => tile.key === 'long').unit, null);
      // The label fits the tile's one line at the desktop width.
      if (width === 1600) assert.deepEqual(seen.filter(tile => tile.cut), []);
      await home.locator('#locationPageTiles').scrollIntoViewIfNeeded(); await shot(home, `presented-home-tiles-limits-${width}`);
      // A new width fits the tiles again.
      if (width === 1600) { await home.setViewportSize({ width: 390, height: 844 }); await home.waitForFunction(() => document.querySelector('.home-tile[data-tile="short"] .home-tile-mark').getBoundingClientRect().width >= 24); }
      assert.deepEqual(errors, []);
    } finally { await context.close(); }
  }
});

test('the shelf canvas has a place for the tile’s number', async () => {
  assert.deepEqual(BLOCKS.state[0], { block: 'figure' }); assert.ok(KIT.families.state.allows.includes('figure'));
  for (const family of Object.values(KIT.families)) assert.deepEqual(family.default[0], { block: 'figure' });
  // A script publishes a number of its own beside the shelf list: emptied three times today, one shelf empty now.
  const world = presented(), seen = world.observations.chiller, blocks = BLOCKS.state;
  const shown = canvas => ({ ...canvas, value: 3, presentation: { family: 'state', blocks, revision: 1 } });
  world.observations.chiller = { ...seen, presentation: { ...seen.presentation, tile: { ...seen.presentation.tile, unit: 'emptied' }, canvas: { family: 'state', blocks } }, output: { ...seen.output, tile: { ...seen.output.tile, value: 3, unit: 'emptied' }, canvas: shown(seen.output.canvas) } };
  const h = await appFor(1600, { world });
  try {
    const canvas = await open(h, 'Chiller shelves');
    assert.deepEqual(await lines(canvas.locator('.tc-figure')), ['3', 'emptied'], 'the canvas writes the tile’s number');
    assert.deepEqual(await lines(canvas.locator('.tc-counts')), ['Empty', '1', 'Replenished', '1', 'Stocked', '2']);
    assert.deepEqual(await canvas.locator('.tc-main > *').evaluateAll(items => items.map(item => item.className.split(' ')[0])), ['tc-lead', 'tc-counts', 'tc-shelves', 'tc-rich']);
    await shot(h.page, 'presented-shelves-figure-1600');
  } finally { await h.close(); }
});
