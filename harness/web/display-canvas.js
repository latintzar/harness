/* Shared display canvases: a published output, a presentation and a selected vertical adapter. */
(() => {
  'use strict';
  function create(extend = () => ({})) {

  const BASE_FAMILIES = [
    { id: 'metric', name: 'Chart', needs: 'Needs a number over time.', has: found => found.timed > 0 },
    { id: 'live', name: 'Timeline', needs: 'Needs timed events.', has: found => found.timed > 0 },
    { id: 'occurrences', name: 'Occurrences', needs: 'Needs a list of instances.', has: found => found.rows > 0 },
  ];
  let adapter;
  let FAMILY_SET, FAMILIES;
  const PAGE = 20;   // timeline entries drawn at a time; the next set loads as the reader reaches the end
  const PERIODS = {
    week: { label: 'Weekly', unit: 'this week', last: 'last week', whole: 'Show the whole week' },
    month: { label: 'Monthly', unit: 'this month', last: 'last month', whole: 'Show the whole month' },
    year: { label: 'Annual', unit: 'this year', last: 'last year', whole: 'Show the whole year' },
  };
  const DAY = 86400000;
  let serial = 0;

  /* ---------- small tools ---------- */
  function el(tag, props, ...kids) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value == null || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key === 'data') Object.assign(node.dataset, value);
      else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? '' : String(value));
    }
    node.append(...kids.flat(3).filter(kid => kid != null && kid !== false));
    return node;
  }
  function svg(tag, props, ...kids) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [key, value] of Object.entries(props || {})) if (value != null && value !== false) node.setAttribute(key, String(value));
    node.append(...kids.flat().filter(Boolean));
    return node;
  }
  const put = (node, ...kids) => { node.append(...kids.flat(3).filter(kid => kid != null && kid !== false)); return node; };
  const ICONS = { chat: 'M20 11.5a8 8 0 0 1-8 8H4l1.7-4.2A8 8 0 1 1 20 11.5Z', play: 'm9 5 11 7-11 7Z', canvas: 'M4 5h16v14H4ZM4 10h16M10 10v9', check: 'm5 12 5 5 9-10', chevron: 'm7 10 5 5 5-5' };
  const icon = name => svg('svg', { class: `tc-icon tc-icon-${name}`, viewBox: '0 0 24 24', 'aria-hidden': 'true', focusable: 'false' }, svg('path', { d: ICONS[name] }));
  const plain = value => typeof value === 'string' ? value : value && typeof value === 'object' ? String(value.text || value.summary || value.description || '') : '';
  const finite = value => Number.isFinite(Number(value)) && value !== null && value !== '' ? Number(value) : null;
  function safeUrl(value) {
    if (typeof value !== 'string' || !value.trim()) return '';
    try { const url = new URL(value, location.href); return ['http:', 'https:'].includes(url.protocol) ? url.href : ''; } catch { return ''; }
  }
  /* An address this origin serves: what a row of a published output may show or open. */
  function ownUrl(value) {
    if (typeof value !== 'string' || !value.trim()) return '';
    try { const url = new URL(value, location.href); return url.origin === location.origin && ['http:', 'https:'].includes(url.protocol) ? url.href : ''; } catch { return ''; }
  }
  const grouped = value => new Intl.NumberFormat('en', { maximumFractionDigits: 0 }).format(value);
  function stamp(value) {
    if (value == null || value === '') return null;
    if (typeof value === 'number') return Number.isFinite(value) ? (value < 1e12 ? value * 1000 : value) : null;
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  const humanize = value => { const words = String(value || '').replace(/[_-]+/g, ' ').trim(); return words ? words[0].toUpperCase() + words.slice(1) : ''; };
  const reducedMotion = () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  const hoverless = () => window.matchMedia?.('(hover: none)').matches;

  /* ---------- time, in words and tabular figures ---------- */
  const startOfDay = ms => { const date = new Date(ms); date.setHours(0, 0, 0, 0); return date.getTime(); };
  const clock = ms => new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(ms);
  const shortDay = ms => new Intl.DateTimeFormat(undefined, { weekday: 'short', day: 'numeric', month: 'short' }).format(ms);
  function dayName(ms, now) {
    const days = Math.round((startOfDay(now) - startOfDay(ms)) / DAY);
    return days === 0 ? 'Today' : days === 1 ? 'Yesterday' : shortDay(ms);
  }
  function when(ms, now) {
    if (ms == null) return '';
    const day = dayName(ms, now);
    return day === 'Today' ? clock(ms) : `${day}, ${clock(ms)}`;
  }
  function span(ms) {
    const minutes = Math.max(0, Math.round(ms / 60000));
    if (minutes < 1) return `${Math.max(1, Math.round(ms / 1000))} s`;
    if (minutes < 60) return `${minutes} min`;
    const hours = Math.floor(minutes / 60), rest = minutes % 60;
    if (hours < 24) return rest ? `${hours} h ${rest} min` : `${hours} h`;
    const days = Math.floor(hours / 24), left = hours % 24;
    return left ? `${days} d ${left} h` : `${days} d`;
  }
  const clipLength = seconds => { const whole = Math.max(1, Math.round(seconds)); return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`; };
  function cadence(agent) {
    if (!agent || agent.mode === 'ad_hoc') return 'On request';
    if (agent.mode === 'ongoing') return 'Continuous';
    const seconds = Number(agent.interval_seconds);
    if (!Number.isFinite(seconds) || seconds <= 0) return 'Scheduled';
    if (seconds % 86400 === 0) return seconds === 86400 ? 'Every day' : `Every ${seconds / 86400} days`;
    if (seconds % 3600 === 0) return seconds === 3600 ? 'Every hour' : `Every ${seconds / 3600} h`;
    if (seconds % 60 === 0) return `Every ${seconds / 60} min`;
    return `Every ${seconds} s`;
  }

  /* ---------- rich text: the run report's Markdown subset, built as nodes ---------- */
  /* Text a script published is drawn as text: a link's own words stay and its address goes. */
  const inertText = value => String(value ?? '').replace(/\[([^\]\n]*)\]\(([^)\s]*)\)/g, (whole, words, address) => words || address);
  function inline(text, inert) {
    const out = [], pattern = /\*\*([^*]+)\*\*|\*([^*]+)\*|`([^`]+)`|\[([^\]]+)\]\(([^)\s]+)\)/g;
    if (inert) text = inertText(text);
    let last = 0, match;
    while ((match = pattern.exec(text))) {
      if (match.index > last) out.push(text.slice(last, match.index));
      if (match[1] != null) out.push(el('strong', { text: match[1] }));
      else if (match[2] != null) out.push(el('em', { text: match[2] }));
      else if (match[3] != null) out.push(match[3]);
      else { const href = inert ? '' : safeUrl(match[5]); out.push(href ? el('a', { href, text: match[4], rel: 'noopener noreferrer' }) : match[4]); }
      last = pattern.lastIndex;
    }
    if (last < text.length) out.push(text.slice(last));
    return out;
  }
  function rich(text, className = 'tc-rich', inert = false) {
    const box = el('div', { class: className });
    let list = null, paragraph = [];
    const flush = () => { if (paragraph.length) box.append(el('p', {}, inline(paragraph.join(' '), inert))); paragraph = []; };
    for (const raw of String(text || '').replace(/\r/g, '').split('\n')) {
      const line = raw.trim(), bullet = /^(?:[-*•]|\d+[.)])\s+(.*)$/.exec(line), heading = /^#{1,6}\s+(.*)$/.exec(line);
      if (!line) { flush(); list = null; continue; }
      if (bullet) { flush(); if (!list) { list = el('ul'); box.append(list); } list.append(el('li', {}, inline(bullet[1], inert))); continue; }
      list = null;
      if (heading) { flush(); box.append(el('h3', {}, inline(heading[1], inert))); continue; }
      paragraph.push(line);
    }
    flush();
    return box;
  }
  /* A report reads as summary, then key points and a recommendation when it names them. */
  function sections(text) {
    const parts = { body: [], points: [], recommendation: [] };
    let target = 'body';
    for (let raw of String(text || '').replace(/\r/g, '').split('\n')) {
      const lead = /^\s*(?:\*\*)?(Recommendations?|Next steps?|Key points)(?:\*\*)?\s*:\s*(?:\*\*)?\s*(\S.*)$/i.exec(raw);
      if (lead) { target = /^key/i.test(lead[1]) ? 'points' : 'recommendation'; raw = lead[2]; }
      const label = lead ? null : /^\s*(?:#{1,6}\s*|\*\*)?\s*([A-Za-z ]{3,24}?)\s*:?\s*(?:\*\*)?\s*:?\s*$/.exec(raw);
      const name = label ? label[1].trim().toLowerCase() : '';
      if (/^(recommendations?|recommended actions?|next steps?|what to do)$/.test(name)) { target = 'recommendation'; continue; }
      if (/^(key points|highlights)$/.test(name)) { target = 'points'; continue; }
      if (/^(summary|memo|overview)$/.test(name)) { target = 'body'; continue; }
      parts[target].push(raw);
    }
    const tidy = lines => lines.join('\n').trim();
    return { body: tidy(parts.body), recommendation: tidy(parts.recommendation), points: parts.points.map(line => line.replace(/^\s*(?:[-*•]|\d+[.)])\s+/, '').trim()).filter(Boolean) };
  }
  const listText = value => Array.isArray(value) ? value.map(plain).filter(Boolean).map(item => `- ${item}`).join('\n') : plain(value);

  /* ---------- the model: one reading of the display's records ---------- */
  /* One row. A committed event carries the host's own addresses. A row of a published output (`published`) is read
   * by the names the host stamps, and only for addresses on this origin: a script's words stay words, and the
   * page asks this origin alone. */
  function occurrence(raw, index, published) {
    const own = published === true, source = raw && typeof raw.source === 'object' && raw.source ? raw.source : {};
    const start = finite(source.start_s) ?? finite(raw.at_s), end = finite(source.end_s) ?? finite(raw.end_s);
    const type = typeof raw.type === 'string' ? raw.type : '';
    return {
      id: String(raw.id || raw.event_key || `occurrence-${index}`), type,
      text: plain(raw.description || raw.summary || raw.label || raw.event) || humanize(type) || 'Occurrence',
      at: stamp(raw.captured_at) ?? stamp(raw.committed_at) ?? stamp(raw.at) ?? stamp(raw.timestamp) ?? stamp(raw.time),
      timeLabel: typeof raw.time_label === 'string' ? raw.time_label : '',
      thumb: own ? ownUrl(raw.thumb) : safeUrl(raw.thumbnail_url || raw.thumbnail || raw.thumb || raw.image || raw.poster), media: own ? ownUrl(raw.media) : safeUrl(raw.media_url || raw.media),
      start, end, length: start != null && end != null && end > start ? end - start : null,
      href: own ? ownUrl(raw.href) : safeUrl(raw.evidence_url), subject: plain(raw.subject), subjectId: String(raw.subject_id || ''), ...adapter.occurrence?.(raw),
    };
  }
  /* A display's presentation names the family, saved or still its default: the page draws what the harness reads. */
  const presented = data => { const saved = data?.presentation; return saved && typeof saved === 'object' && saved.canvas && typeof saved.canvas === 'object' ? saved : null; };
  const outputOf = data => { const out = data?.output; return out && typeof out === 'object' && out.tile && typeof out.tile === 'object' && out.canvas && typeof out.canvas === 'object' ? out : null; };
  function family(agent, observations) {
    const declared = presented(observations)?.canvas.family || observations?.canvas?.family || agent?.canvas?.family || agent?.canvas_family;
    if (FAMILIES.includes(declared)) return declared;
    const selected = adapter.family?.(agent, observations);
    if (FAMILIES.includes(selected)) return selected;
    if (agent?.mode === 'ongoing') return 'live';
    if (agent?.mode === 'scheduled') return 'metric';
    return 'occurrences';
  }
  /* What a display's records hold, and so which families it can show. */
  function signals(events) {
    return { rows: events.length, timed: events.filter(event => event.at != null).length, thumbs: events.filter(event => event.thumb).length, ...adapter.signals?.(events) };
  }
  const familySet = found => FAMILY_SET.map(item => ({ id: item.id, name: item.name, needs: item.needs, available: item.has(found) }));
  const isLocked = (agent, flag) => flag ?? !!(agent?.core || agent?.locked || agent?.kind === 'core' || agent?.agent_kind === 'core');
  function buckets(period, now) {
    const list = [];
    if (period === 'year') {
      const anchor = new Date(now); anchor.setDate(1); anchor.setHours(0, 0, 0, 0);
      for (let back = 11; back >= 0; back--) {
        const from = new Date(anchor); from.setMonth(anchor.getMonth() - back);
        const to = new Date(from); to.setMonth(from.getMonth() + 1);
        list.push({ start: from.getTime(), end: to.getTime(), tick: new Intl.DateTimeFormat(undefined, { month: 'short' }).format(from), name: new Intl.DateTimeFormat(undefined, { month: 'long', year: 'numeric' }).format(from) });
      }
      return list;
    }
    const days = period === 'month' ? 30 : 7, today = startOfDay(now);
    for (let back = days - 1; back >= 0; back--) {
      const from = new Date(today); from.setDate(from.getDate() - back);
      const to = new Date(from); to.setDate(to.getDate() + 1);
      const tick = period === 'month' ? (back % 7 === 1 ? new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short' }).format(from) : '') : new Intl.DateTimeFormat(undefined, { weekday: 'short' }).format(from);
      list.push({ start: from.getTime(), end: to.getTime(), tick, name: back === 0 ? 'Today' : back === 1 ? 'Yesterday' : shortDay(from.getTime()) });
    }
    return list;
  }
  function metric(events, period, now) {
    const points = buckets(period, now).map(bucket => ({ ...bucket, value: 0 }));
    const from = points[0].start, to = points[points.length - 1].end, width = to - from;
    let previous = 0;
    const inside = [];
    for (const event of events) {
      if (event.at == null) continue;
      if (event.at >= from && event.at < to) { inside.push(event); const point = points.find(item => event.at >= item.start && event.at < item.end); if (point) point.value += 1; }
      else if (event.at >= from - width && event.at < from) previous += 1;
    }
    const total = inside.length, gap = total - previous, last = PERIODS[period].last;
    const delta = !total && !previous ? '' : gap === 0 ? `Level with ${last}` : `${Math.abs(gap)} ${gap > 0 ? 'more' : 'fewer'} than ${last}`;
    return { period, total, previous, delta, points, events: inside };
  }
  function memosOf(data, summary) {
    const given = Array.isArray(data?.memos) ? data.memos : summary.text ? [{ id: summary.runId || 'memo', text: summary.text, updated_at: summary.at, recommendation: summary.recommendationText }] : [];
    return given.map((memo, index) => {
      const parts = sections(plain(memo.text || memo.summary || memo));
      return { id: String(memo.id || memo.run_id || memo.slot || `memo-${index}`), kind: 'memo', at: stamp(memo.updated_at) ?? stamp(memo.created_at) ?? stamp(memo.completed_at) ?? stamp(memo.at) ?? stamp(memo.written_at),
        body: parts.body, recommendation: listText(memo.recommendation || memo.recommendations) || parts.recommendation, points: parts.points, covers: finite(memo.event_count) };
    }).filter(memo => memo.body || memo.recommendation).sort((a, b) => (b.at || 0) - (a.at || 0));
  }
  /* ---------- a published output, read as its tile reads it ---------- */
  /* The number format of a Home tile (the tile formatter, `format` and `suppliedTile`): the canvas writes the
   * same characters, so one output shows one number on both. */
  function format(kind, value, currency) {
    if (value === null) return { text: '', unit: '' };
    if (kind === 'duration') { const s = Math.max(0, Math.round(value)); return s < 60 ? { text: String(s), unit: 's' } : { text: `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`, unit: 'min' }; }
    if (kind === 'rate') return { text: String(Number((value * 100).toFixed(1))), unit: '%' };
    if (kind === 'money') {
      if (typeof currency === 'string' && /^[A-Z]{3}$/.test(currency)) { try { return { text: new Intl.NumberFormat('en', { style: 'currency', currency, maximumFractionDigits: Math.abs(value) >= 100 ? 0 : 2 }).format(value), unit: '' }; } catch { /* the plain number follows */ } }
      return { text: new Intl.NumberFormat('en', { maximumFractionDigits: Math.abs(value) >= 100 ? 0 : 2 }).format(value), unit: '' };
    }
    return { text: new Intl.NumberFormat('en', { maximumFractionDigits: 1 }).format(value), unit: '' };
  }
  function written(row, other) {
    const kind = row.kind || 'count', given = other === undefined ? row.value : other, value = typeof given === 'boolean' ? null : finite(given);
    const ratio = kind === 'rate' && row.unit === '%' && value !== null ? value / 100 : value;
    const shown = format(kind, ratio, row.currency), own = other === undefined && typeof row.text === 'string' && row.text !== '';
    return { text: own ? row.text : shown.text, unit: typeof row.unit === 'string' && (own || kind === 'count') ? row.unit : shown.unit, value };
  }
  /* One period of an output: the reading its tile shows for that period, and that reading's canvas. */
  function reading(out, period) {
    const row = out.tile, part = row.periods && typeof row.periods === 'object' && row.periods[period] && typeof row.periods[period] === 'object' ? row.periods[period] : null;
    return { row: part ? { ...row, ...part } : row, canvas: part && part.canvas && typeof part.canvas === 'object' ? part.canvas : out.canvas,
      label: plain(part ? part.period_label : row.period && typeof row.period === 'object' ? row.period.label : ''),
      periods: Object.keys(PERIODS).every(key => row.periods && row.periods[key] && typeof row.periods[key] === 'object') };
  }
  /* The value a block's binding names: a field of the canvas; for text, also a text slot the display's agent wrote. */
  function bound(canvas, name, text) {
    if (typeof name !== 'string' || !name) return undefined;
    if (Object.prototype.hasOwnProperty.call(canvas, name)) return canvas[name];
    const slot = text ? (Array.isArray(canvas.texts) ? canvas.texts : []).find(item => item && item.slot === name) : null;
    return slot ? slot.text || '' : undefined;
  }
  const newestFirst = (a, b) => (b.at ?? -Infinity) - (a.at ?? -Infinity);
  function shownOutput(out, saved, kind, set, period) {
    const read = reading(out, period), canvas = read.canvas, own = !!saved && saved.canvas.family === kind;
    const named = own ? (canvas.presentation && canvas.presentation.family === kind && Array.isArray(canvas.presentation.blocks) ? canvas.presentation.blocks : saved.canvas.blocks) : set.find(item => item.id === kind)?.blocks;
    const faults = own && Array.isArray(canvas.problems) ? canvas.problems.filter(item => item && typeof item === 'object') : [];
    const resting = new Set(faults.map(item => item.block));
    const blocks = (Array.isArray(named) ? named : []).filter(item => item && typeof item.block === 'string' && !resting.has(item.block));
    const block = name => blocks.find(item => item.block === name) || null;
    const value = (name, field, text) => { const item = block(name); return item ? bound(canvas, item.bind?.[field], text) : undefined; };
    const rail = block('timeline') || block('instances'), listed = rail ? bound(canvas, rail.bind?.rows) : null;
    // A tile whose mark waits for a field says so here, in a person's words; a block that rests gives its own sentence.
    const tileWaits = (Array.isArray(read.row.problems) ? read.row.problems : []).filter(item => item && typeof item === 'object' && item.block === 'tile').map(item => plain(item.needs)).filter(Boolean);
    return { revision: out.revision, row: read.row, canvas, label: read.label, periods: read.periods, blocks, block, value, rail, tileWaits: tileWaits.length > 0,
      rows: (Array.isArray(listed) ? listed : []).filter(row => row && typeof row === 'object').map((row, index) => occurrence(row, index, true)).sort(newestFirst),
      needs: [...new Set([...tileWaits.map(text => `The tile’s mark ${text[0].toLowerCase()}${text.slice(1)}`), ...faults.map(item => plain(item.needs)).filter(Boolean)])].join(' ') };
  }
  /* The line of a published series: each point with its own span of time, its name and, for a few of them, a tick. */
  function outputMetric(series, events, period) {
    const list = (Array.isArray(series) ? series : []).filter(point => point && typeof point === 'object' && finite(point.value) !== null && stamp(point.t) != null);
    const step = list.length > 1 ? stamp(list[1].t) - stamp(list[0].t) : DAY, hourly = step < DAY, every = Math.max(1, Math.ceil(list.length / 6));
    const long = list.length ? stamp(list[list.length - 1].t) - stamp(list[0].t) > 8 * DAY : false;
    const tickOf = ms => hourly ? clock(ms) : new Intl.DateTimeFormat(undefined, long ? { day: 'numeric', month: 'short' } : { weekday: 'short' }).format(ms);
    const points = list.map((point, index) => {
      const start = stamp(point.t), end = index + 1 < list.length ? stamp(list[index + 1].t) : start + step;
      return { start, end, value: finite(point.value), name: hourly ? `${shortDay(start)}, ${clock(start)}` : shortDay(start), tick: index % every === 0 ? tickOf(start) : '' };
    });
    return { period, total: null, previous: null, delta: '', points, events };
  }

  function model(agent, observations, options = {}) {
    const now = options.now ?? Date.now(), period = PERIODS[options.period] ? options.period : 'week', data = observations && typeof observations === 'object' ? observations : null;
    const incidents = (Array.isArray(data?.incidents) ? data.incidents : []).map(occurrence).sort(newestFirst);
    const saved = data?.summary, report = sections(plain(saved) || plain(agent?.summary));
    const summary = { text: report.body, points: report.points, at: stamp(saved?.updated_at), runId: saved?.run_id || '', revision: saved?.definition_revision ?? agent?.revision ?? '',
      recommendationText: listText(data?.recommendation || data?.recommendations || agent?.recommendation || agent?.recommendations) || report.recommendation };
    // With a published output the server says which families that output fills; before one, the events say it.
    const out = outputOf(data), told = out && Array.isArray(data.families) ? data.families.filter(item => item && typeof item === 'object') : [];
    const set = FAMILY_SET.every(item => told.some(entry => entry.id === item.id))
      ? FAMILY_SET.map(item => { const entry = told.find(one => one.id === item.id); return { id: item.id, name: item.name, needs: item.needs, available: entry.available === true, blocks: Array.isArray(entry.blocks) ? entry.blocks : [] }; })
      : familySet(signals(incidents));
    const picked = set.find(item => item.id === options.family && item.available);
    const kind = picked ? picked.id : family(agent, data), profile = data?.profile || data?.profiles?.[0] || null;
    const shown = out ? shownOutput(out, presented(data), kind, set, period) : null, events = shown ? shown.rows : incidents;
    // The heading is the label a saved presentation gives. Until one is saved the agent keeps its own full title.
    const label = shown ? plain(shown.row.label) : '', named = agent?.title || agent?.name || '';
    const view = {
      family: kind, familyName: FAMILY_SET.find(item => item.id === kind).name, families: set, locked: isLocked(agent, options.locked), loaded: !!data,
      title: (finite(data?.presentation?.revision) > 0 && label) || named || label || adapter.agentLabel || 'Agent', description: plain(agent?.description), period, now, paused: agent?.status === 'paused',
      events, summary, recommendation: summary.recommendationText, // A list counts the rows it shows. `total` is the output's own count of its rows, beside the list that binds them.
      count: shown ? events.length : finite(data?.event_count) ?? events.length, total: shown && shown.rail?.bind?.rows === 'rows' ? finite(shown.canvas.total_rows) : null, more: !shown && !!data?.more_available,
      profile: plain(profile?.name) || options.profileName || '', cadence: cadence(agent), out: shown,
      needs: shown ? shown.needs : agent?.execution && agent.execution.ready === false && !events.length && !summary.text ? (plain(agent.execution.notice) || 'Needs its input') : '',
      updatedAt: Math.max(summary.at || 0, incidents[0]?.at || 0, events[0]?.at || 0) || stamp(agent?.updated_at) || null,
      revision: [agent?.id || '', summary.revision, summary.runId, finite(data?.event_count) ?? incidents.length, incidents[0]?.id || '', Array.isArray(data?.memos) ? data.memos.length : '',
        ...(out ? [`output-${out.revision}`, `presentation-${finite(data.presentation?.revision) ?? 0}`] : finite(data?.presentation?.revision) ? [`presentation-${data.presentation.revision}`] : [])].join(':'),
    };
    if (shown) {
      if (shown.block('line')) view.metric = outputMetric(shown.value('line', 'series'), events, period);
      adapter.output?.(view, shown);
      const pinned = shown.value('key_points', 'items');
      view.points = (Array.isArray(pinned) ? pinned : []).map(plain).map(inertText).filter(Boolean).slice(0, 5);
      if (kind === 'live') {
        const comments = shown.value('timeline', 'comments');
        view.memos = memosOf({ memos: Array.isArray(comments) ? comments.filter(item => item && typeof item === 'object') : [] }, summary).map(memo => ({ ...memo, inert: true }));
        view.today = events.filter(event => event.at != null && event.at >= startOfDay(now)).length;
        view.feed = [...events.map(event => ({ ...event, kind: 'entry' })), ...view.memos].sort((a, b) => (b.at ?? -Infinity) - (a.at ?? -Infinity) || (a.kind === 'memo' ? -1 : 1));
      }
      return view;
    }
    if (kind === 'metric') view.metric = metric(events, period, now);
    adapter.events?.(view);
    if (kind === 'live') {
      view.memos = memosOf(data, summary); view.today = events.filter(event => event.at != null && event.at >= startOfDay(now)).length;
      const pinned = Array.isArray(data?.key_points) ? data.key_points.map(plain).filter(Boolean) : [];
      view.points = (pinned.length ? pinned : view.memos.find(memo => memo.points.length)?.points || []).slice(0, 5);
      view.feed = [...events.map(event => ({ ...event, kind: 'entry' })), ...view.memos].sort((a, b) => (b.at ?? -Infinity) - (a.at ?? -Infinity) || (a.kind === 'memo' ? -1 : 1));
    }
    return view;
  }

  /* ---------- marks: the glowing line, the hover-play thumbnail ---------- */
  function curve(points) {
    const n = points.length;
    if (!n) return '';
    if (n === 1) return `M${points[0][0]},${points[0][1]}`;
    const dx = [], slope = [], tangent = [];
    for (let i = 0; i < n - 1; i++) { dx[i] = points[i + 1][0] - points[i][0]; slope[i] = (points[i + 1][1] - points[i][1]) / dx[i]; }
    tangent[0] = slope[0]; tangent[n - 1] = slope[n - 2];
    for (let i = 1; i < n - 1; i++) tangent[i] = slope[i - 1] * slope[i] <= 0 ? 0 : 3 * (dx[i - 1] + dx[i]) / ((2 * dx[i] + dx[i - 1]) / slope[i - 1] + (dx[i] + 2 * dx[i - 1]) / slope[i]);
    let d = `M${points[0][0]},${points[0][1]}`;
    for (let i = 0; i < n - 1; i++) d += `C${points[i][0] + dx[i] / 3},${points[i][1] + tangent[i] * dx[i] / 3} ${points[i + 1][0] - dx[i] / 3},${points[i + 1][1] - tangent[i + 1] * dx[i] / 3} ${points[i + 1][0]},${points[i + 1][1]}`;
    return d;
  }
  function layout(values, box) {
    const top = Math.max(1, ...values) * 1.12, n = values.length;
    return values.map((value, index) => [n === 1 ? box.w / 2 : box.left + index * ((box.w - box.left - box.right) / (n - 1)), box.h - box.bottom - (value / top) * (box.h - box.top - box.bottom)]);
  }
  function lineMark(values, box, className) {
    const id = `tc-light-${++serial}`, placed = layout(values, box), d = curve(placed);
    const light = svg('linearGradient', { id, gradientUnits: 'userSpaceOnUse', x1: 0, y1: 0, x2: box.w, y2: 0 }, svg('stop', { offset: '0', class: 'tc-stop-warm' }), svg('stop', { offset: '1', class: 'tc-stop-cool' }));
    const art = svg('svg', { class: className, viewBox: `0 0 ${box.w} ${box.h}`, preserveAspectRatio: 'none', 'aria-hidden': 'true', focusable: 'false' },
      svg('defs', {}, light), svg('path', { class: 'tc-line-glow', d, stroke: `url(#${id})` }), svg('path', { class: 'tc-line', d }));
    return { art, placed };
  }
  /* RocotoCharts (display-charts.js) draws the mark when the page carries it; the built-in mark draws otherwise. */
  function kitMark(kind, points, opts) {
    const charts = window.RocotoCharts;
    if (!charts || typeof charts[kind] !== 'function') return null;
    const host = el('div', { class: `tc-kit-mark tc-kit-${kind}` });
    try { return charts[kind](host, points, opts) ? host : null; } catch { return null; }
  }
  /* The canvas chart: a no-axis line over the period. Arrow keys and the pointer read each point; Enter opens its events. */
  function lineChart({ title, points, selected, onSelect, read }) {
    let index = selected ?? points.length - 1;
    const said = value => read ? read(value) : String(value);
    const kit = kitMark('line', points.map(point => ({ value: point.value, label: point.name })), { size: 'large', hover: true, surface: 'paper', label: `${title} over time`, onFocus: at => { if (at != null) index = at; }, ...(read ? { format: value => read(value) } : {}) });
    const box = { w: 1000, h: 300, left: 44, right: 44, top: 30, bottom: 18 }, values = points.map(point => point.value);
    const placed = layout(values, box), share = at => points.length > 1 ? at / (points.length - 1) : .5;
    // Each tick stands under its point: the kit's large line runs from 4px to 16px inside its box, the built-in one from 4.4% to 95.6%.
    const under = at => kit ? `calc(4px + (100% - 20px) * ${share(at)})` : `${placed[at][0] / box.w * 100}%`;
    const ticks = el('div', { class: 'tc-chart-ticks', 'aria-hidden': 'true' }, points.map((point, at) => point.tick ? el('span', { class: at === points.length - 1 ? 'tc-chart-tick-now' : '', style: `left:${under(at)}`, text: point.tick }) : null));
    if (kit) {
      const pick = () => onSelect?.(index === selected ? null : index), stage = kit.querySelector('[tabindex]');
      if (stage) stage.dataset.focus = 'chart';
      kit.addEventListener('click', pick);
      kit.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); pick(); } });
      return el('div', { class: 'tc-chart tc-chart-kit' }, el('div', { class: 'tc-chart-plot' }, kit), ticks);
    }
    const { art } = lineMark(values, box, 'tc-chart-art');
    const dots = placed.map(([x, y], at) => el('span', { class: `tc-chart-dot${at === placed.length - 1 ? ' tc-chart-dot-now' : ''}${selected === at ? ' tc-chart-dot-chosen' : ''}`, style: `left:${x / box.w * 100}%;top:${y / box.h * 100}%` }));
    const cursor = el('span', { class: 'tc-chart-cursor', hidden: true }), chip = el('span', { class: 'tc-chart-chip', hidden: true });
    const plot = el('div', { class: 'tc-chart-plot' }, el('div', { class: 'tc-chart-reveal' }, art, dots), cursor, chip);
    const chart = el('div', { class: 'tc-chart', role: 'slider', tabindex: '0', data: { focus: 'chart' }, 'aria-label': `${title} over time`, 'aria-valuemin': '0', 'aria-valuemax': String(points.length - 1), 'aria-orientation': 'horizontal' }, plot, ticks);
    const tell = () => { chart.setAttribute('aria-valuenow', String(index)); chart.setAttribute('aria-valuetext', `${said(points[index].value)}, ${points[index].name}`); };
    function show(next) {
      index = Math.min(points.length - 1, Math.max(0, next)); tell();
      const [x, y] = placed[index], left = x / box.w * 100;
      cursor.hidden = chip.hidden = false; cursor.style.left = `${left}%`; cursor.style.setProperty('--tc-cursor-y', `${y / box.h * 100}%`);
      chip.replaceChildren(el('strong', { text: said(points[index].value) }), el('span', { text: points[index].name }));
      chip.style.left = `${Math.min(86, Math.max(14, left))}%`; chip.style.top = `${y / box.h * 100}%`;
    }
    const rest = () => { cursor.hidden = chip.hidden = true; };
    const nearest = event => { const frame = plot.getBoundingClientRect(), x = (event.clientX - frame.left) / frame.width * box.w; let best = 0; placed.forEach(([px], at) => { if (Math.abs(px - x) < Math.abs(placed[best][0] - x)) best = at; }); return best; };
    plot.addEventListener('pointermove', event => show(nearest(event)));
    plot.addEventListener('pointerleave', () => { if (document.activeElement !== chart) rest(); });
    plot.addEventListener('click', event => { const picked = nearest(event); show(picked); onSelect?.(picked === selected ? null : picked); });
    chart.addEventListener('focus', () => { if (chart.matches(':focus-visible')) show(index); });
    chart.addEventListener('blur', rest);
    chart.addEventListener('keydown', event => {
      const step = { ArrowLeft: -1, ArrowDown: -1, ArrowRight: 1, ArrowUp: 1 }[event.key];
      if (step) { event.preventDefault(); show(index + step); }
      else if (event.key === 'Home') { event.preventDefault(); show(0); }
      else if (event.key === 'End') { event.preventDefault(); show(points.length - 1); }
      else if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onSelect?.(index === selected ? null : index); }
    });
    tell();
    return chart;
  }
  function spark(values) { return kitMark('spark', values, { size: 'spark', surface: 'paper' }) || lineMark(values, { w: 240, h: 72, left: 4, right: 8, top: 10, bottom: 8 }, 'tc-spark').art; }

  let playing = null;
  function stopPreview() { if (!playing) return; const { frame, video } = playing; playing = null; video.pause(); video.removeAttribute('src'); video.load(); video.remove(); frame.classList.remove('tc-thumb-playing'); frame.querySelector('.tc-thumb-play')?.setAttribute('aria-pressed', 'false'); }
  function startPreview(frame, item) {
    if (!item.media || playing?.frame === frame) return;
    stopPreview();
    const from = item.start ?? 0, to = item.end != null && item.end > from ? Math.min(item.end, from + 12) : from + 8;
    const video = el('video', { class: 'tc-thumb-video', muted: true, playsinline: true, preload: 'auto', tabindex: '-1', 'aria-hidden': 'true' });
    video.muted = true; video.src = item.media;
    video.addEventListener('loadedmetadata', () => { try { video.currentTime = from; } catch { /* the first frame plays */ } });
    video.addEventListener('timeupdate', () => { if (video.currentTime >= to) video.currentTime = from; });
    video.addEventListener('playing', () => frame.classList.add('tc-thumb-playing'));
    frame.append(video); playing = { frame, video };
    frame.querySelector('.tc-thumb-play')?.setAttribute('aria-pressed', 'true');
    video.play?.().catch(() => {});
  }
  /* A thumbnail plays its own interval while the pointer or the focus rests on its row. Touch screens get a play control. */
  function thumb(item, host) {
    const frame = el('span', { class: 'tc-thumb' + (item.thumb ? '' : ' tc-thumb-open') });
    if (item.thumb) { const image = el('img', { src: item.thumb, alt: '', loading: 'lazy', decoding: 'async' }); image.addEventListener('error', () => { image.remove(); frame.classList.add('tc-thumb-open'); }); frame.append(image); }
    if (item.length != null) frame.append(el('span', { class: 'tc-thumb-length', text: clipLength(item.length) }));
    if (item.media) {
      const play = el('button', { type: 'button', class: 'tc-thumb-play', 'aria-label': 'Play preview', 'aria-pressed': 'false' }, icon('play'));
      play.addEventListener('click', event => { event.preventDefault(); event.stopPropagation(); if (playing?.frame === frame) stopPreview(); else startPreview(frame, item); });
      frame.append(play);
      const begin = () => { if (!reducedMotion() && !hoverless()) startPreview(frame, item); }, end = () => { if (playing?.frame === frame && !hoverless()) stopPreview(); };
      host.addEventListener('pointerenter', begin); host.addEventListener('pointerleave', end);
      host.addEventListener('focusin', event => { if (event.target.matches?.(':focus-visible')) begin(); }); host.addEventListener('focusout', end);
    }
    return frame;
  }

  /* ---------- shared pieces ---------- */
  function occurrenceRow(item, context, options = {}) {
    const row = el('li', { class: 'tc-occurrence', data: { occurrenceId: item.id } }), focusKey = `occurrence-${item.id}`;
    const copy = el('span', { class: 'tc-occurrence-copy' }, el('strong', { text: item.text }), el('span', { class: 'tc-time', text: item.at != null ? when(item.at, context.now) : item.timeLabel }));
    const body = item.href ? el('a', { class: 'tc-occurrence-open', href: item.href, data: { focus: focusKey } }) : el('div', { class: 'tc-occurrence-open' });
    if (item.href) body.addEventListener('click', event => { if (context.onOpenEvidence && !event.metaKey && !event.ctrlKey && !event.shiftKey) { event.preventDefault(); stopPreview(); context.onOpenEvidence(item); } });
    if (!options.bare) body.append(thumb(item, row));
    body.append(copy); row.append(body);
    return row;
  }
  function occurrenceList(items, context) {
    if (!items.length) return el('div', { class: 'tc-waiting' }, [0, 1, 2].map(() => el('span', { class: 'tc-waiting-row', 'aria-hidden': 'true' }, el('i'), el('b'))), el('p', { text: context.waiting || 'The first occurrence lands here.' }));
    return el('ol', { class: 'tc-occurrences' }, items.map(item => occurrenceRow(item, context)));
  }
  function rail(title, count, body, extra) {
    const id = `tc-rail-${++serial}`;
    return el('aside', { class: 'cr-detail-grid tc-rail tc-glass', 'aria-labelledby': id },
      el('header', { class: 'tc-rail-head' }, el('h2', { id, text: title }), count == null ? null : el('span', { class: 'tc-count', text: String(count) }), extra),
      el('div', { class: 'tc-rail-scroll', tabindex: '0', role: 'group', 'aria-labelledby': id, data: { focus: 'rail' } }, body));
  }
  function facts(view) {
    const rows = [['Updated', view.updatedAt ? `${dayName(view.updatedAt, view.now)} ${clock(view.updatedAt)}` : ''], ['Runs', view.cadence], ['Watches', view.profile]].filter(([, value]) => value);
    return el('dl', { class: 'tc-updated tc-glass' }, rows.map(([label, value]) => el('div', {}, el('dt', { text: label }), el('dd', { text: value }))));
  }
  function periodToggle(current, onChange) {
    const group = el('div', { class: 'tc-period', role: 'radiogroup', 'aria-label': 'Period' });
    const keys = Object.keys(PERIODS);
    keys.forEach(key => group.append(el('button', { type: 'button', role: 'radio', 'aria-checked': String(key === current), tabindex: key === current ? '0' : '-1', data: { period: key, focus: `period-${key}` }, text: PERIODS[key].label, onclick: () => onChange(key) })));
    group.addEventListener('keydown', event => {
      const step = { ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1 }[event.key]; if (!step) return;
      event.preventDefault(); onChange(keys[(keys.indexOf(current) + step + keys.length) % keys.length]);
    });
    return group;
  }
  function recommendation(text, inert = false) {
    if (!text) return null;
    return el('section', { class: 'tc-recommendation' }, el('h2', { text: 'Recommendation' }), rich(text, 'tc-rich', inert));
  }

  /* ---------- the five canvases ---------- */
  function metricCanvas(view, ui, context) {
    const data = view.metric, bucket = ui.bucket != null ? data.points[ui.bucket] : null;
    const shown = bucket ? data.events.filter(event => event.at >= bucket.start && event.at < bucket.end) : data.events;
    const figure = el('p', { class: 'tc-figure' }, el('span', { class: 'tc-numeral', text: String(data.total) }), el('em', { text: PERIODS[data.period].unit }));
    const instrument = el('section', { class: 'tc-instrument tc-glass', 'aria-label': view.title },
      el('div', { class: 'tc-instrument-top' }, el('div', {}, figure, data.delta ? el('p', { class: 'tc-delta', text: data.delta }) : null), periodToggle(data.period, context.setPeriod)),
      lineChart({ title: view.title, points: data.points, selected: ui.bucket, onSelect: context.setBucket }));
    const main = el('div', { class: 'tc-main' }, instrument, view.summary.text ? rich(view.summary.text, 'tc-rich tc-summary') : null, recommendation(view.recommendation));
    const whole = bucket ? el('button', { type: 'button', class: 'tc-text-button', data: { focus: 'whole' }, text: PERIODS[data.period].whole, onclick: () => context.setBucket(null) }) : null;
    return { main, rail: rail(bucket ? bucket.name : 'Events', shown.length, occurrenceList(shown, { ...context, waiting: 'The first event lands here.' }), whole) };
  }
  function occurrenceCanvas(view, ui, context) {
    const lead = el('div', { class: 'tc-lead' }, el('p', { class: 'tc-figure' }, el('span', { class: 'tc-numeral', text: view.more ? `${view.count}+` : String(view.count) }), el('em', { text: view.count === 1 ? 'occurrence' : 'occurrences' })), facts(view));
    const main = el('div', { class: 'tc-main' }, lead, view.summary.text ? rich(view.summary.text, 'tc-rich tc-summary') : null,
      view.summary.points.length ? rich(view.summary.points.map(point => `- ${point}`).join('\n'), 'tc-rich tc-summary') : null, recommendation(view.recommendation));
    return { main, rail: rail('Occurrences', null, occurrenceList(view.events, context)) };
  }
  function feedEntry(item, context, fresh) {
    if (item.kind === 'memo') {
      return el('li', { class: 'tc-feed-memo' + (fresh ? ' tc-arrived' : ''), data: { feedId: item.id } }, el('span', { class: 'tc-time', text: item.at != null ? clock(item.at) : '' }),
        el('article', { class: 'tc-memo tc-glass' }, el('h3', { text: 'Memo' }), item.body ? rich(item.body, 'tc-rich', !!item.inert) : null, item.recommendation ? el('div', { class: 'tc-memo-advice' }, el('h4', { text: 'Recommendation' }), rich(item.recommendation, 'tc-rich', !!item.inert)) : null));
    }
    const row = el('li', { class: 'tc-feed-entry' + (fresh ? ' tc-arrived' : ''), data: { feedId: item.id } }, el('span', { class: 'tc-time', text: item.at != null ? clock(item.at) : item.timeLabel }));
    const body = item.href ? el('a', { class: 'tc-feed-open', href: item.href, data: { focus: `feed-${item.id}` } }) : el('div', { class: 'tc-feed-open' });
    if (item.href) body.addEventListener('click', event => { if (context.onOpenEvidence && !event.metaKey && !event.ctrlKey && !event.shiftKey) { event.preventDefault(); stopPreview(); context.onOpenEvidence(item); } });
    body.append(el('p', { text: item.text })); if (item.thumb || item.media) body.append(thumb(item, row));
    row.append(body);
    return row;
  }
  function feedList(items, context, fresh) {
    const list = el('ol', { class: 'tc-feed' });
    let day = '';
    for (const item of items) {
      const name = item.at != null ? dayName(item.at, context.now) : '';
      if (name && name !== day) { day = name; list.append(el('li', { class: 'tc-feed-day' }, el('h2', { text: name }))); }
      list.append(feedEntry(item, context, fresh?.has(item.id)));
    }
    return list;
  }
  const keyPoints = (points, inert) => points.length ? el('section', { class: 'tc-points tc-glass', 'aria-label': 'Key points' }, el('h2', { text: 'Key points' }), el('ul', {}, points.map(point => el('li', {}, inline(point, inert))))) : null;
  function liveRail(view, ui, context) {
    const held = new Set(ui.held || []), visible = view.feed.filter(item => !held.has(item.id));
    const more = held.size ? el('button', { type: 'button', class: 'tc-new-updates', data: { focus: 'new-updates' }, text: held.size === 1 ? '1 new update' : `${held.size} new updates`, onclick: () => context.revealHeld() }) : null;
    const page = visible.slice(0, ui.shown);
    const feed = page.length ? feedList(page, context, ui.fresh) : el('p', { class: 'tc-quiet', text: 'The first update lands here.' });
    const earlier = visible.length > page.length ? el('button', { type: 'button', class: 'tc-feed-more', data: { focus: 'earlier' }, text: 'Earlier updates', onclick: () => context.showEarlier(true) }) : null;
    return el('section', { class: 'cr-detail-grid tc-live', 'aria-label': 'Live updates' },
      el('header', { class: 'tc-live-head' }, el('h2', {}, el('span', { class: 'tc-live-dot', 'aria-hidden': 'true' }), 'Live updates'), view.updatedAt ? el('span', { class: 'tc-time', text: `Updated ${when(view.updatedAt, view.now)}` }) : null),
      more, feed, earlier);
  }
  function liveCanvas(view, ui, context) {
    return { main: el('div', { class: 'tc-main' }, keyPoints(view.points, false)), rail: liveRail(view, ui, context) };
  }
  const CANVASES = { metric: metricCanvas, occurrences: occurrenceCanvas, live: liveCanvas };

  /* ---------- a canvas drawn from a presentation: the kit's blocks, in the order the presentation names ----------
   * Every value comes from the display's published output. The number, its share, the period switch and the facts
   * share the first row; a line that follows them joins their panel. The family's list stands in the rail. */
  const LEAD = new Set(['figure', 'gauge', 'period', 'facts']);
  const RAIL_TITLE = { metric: 'Events', occurrences: 'Occurrences', live: 'Events' };
  function blockCanvas(view, ui, context) {
    const o = view.out, shown = written(o.row), waits = o.row.state === 'needs' || shown.value === null;
    const text = (name, field) => inertText(plain(o.value(name, field, true))).trim();
    const said = value => { const one = written(o.row, value); return one.unit === '%' || one.unit === 's' ? `${one.text}${one.unit}` : [one.text, one.unit].filter(Boolean).join(' '); };

    const draw = {
      figure: () => el('div', { class: 'tc-figure-box' },
        waits ? el('p', { class: 'tc-figure tc-figure-waits' }, el('span', { text: plain(o.row.needs) || 'Needs a source' }))
          : el('p', { class: 'tc-figure' }, el('span', { class: 'tc-numeral', text: shown.text }), shown.unit ? el('em', { text: shown.unit }) : null),
        o.label ? el('p', { class: 'tc-delta', text: o.label }) : null),
      gauge: block => {
        const share = finite(o.value('gauge', 'fraction')); if (share === null) return null;
        const part = Math.max(0, Math.min(1, share)), mark = kitMark(block.chart === 'arc' ? 'arc' : 'ring', part, { surface: 'paper', label: `${Math.round(part * 1000) / 10}% of the whole` });
        return mark ? el('div', { class: 'tc-gauge' }, mark) : null;
      },
      period: () => o.periods ? periodToggle(view.period, context.setPeriod) : null,
      facts: () => facts(view),
      line: () => view.metric && view.metric.points.length > 1 ? lineChart({ title: view.title, points: view.metric.points, selected: ui.bucket, onSelect: context.setBucket, read: said }) : null,
      bars: block => {
        const bands = (Array.isArray(o.value('bars', 'values')) ? o.value('bars', 'values') : []).filter(band => band && typeof band === 'object' && finite(band.value) !== null).map(band => ({ label: plain(band.label ?? band.band), value: finite(band.value) }));
        const mark = bands.length ? kitMark('bars', bands, { size: 'large', highlight: 'max', surface: 'paper', label: `${view.title} by band` }) : null;
        return mark ? el('section', { class: 'tc-bars tc-glass' }, plain(block.title) ? el('h2', { text: plain(block.title) }) : null, el('div', { class: 'tc-bars-plot' }, mark)) : null;
      },
      summary: () => { const body = text('summary', 'text'); return body ? rich(body, 'tc-rich tc-summary', true) : null; },
      key_points: () => keyPoints(view.points || [], true),
      recommendation: () => recommendation(text('recommendation', 'text'), true),
      ...adapter.blocks?.(view, ui, context),
    };
    const nodes = [];
    for (const block of o.blocks) { if (block === o.rail || !draw[block.block]) continue; const node = draw[block.block](block); if (node) nodes.push([block.block, node]); }
    const part = name => nodes.find(([key]) => key === name)?.[1] || null;
    const lineAt = nodes.findIndex(([name]) => name === 'line'), joined = lineAt > 0 && nodes.slice(0, lineAt).every(([name]) => LEAD.has(name));
    const left = [part('figure'), part('gauge')].filter(Boolean), right = [part('period'), part('facts')].filter(Boolean);
    const lead = left.length || right.length ? [left.length ? el('div', { class: 'tc-lead-figure' }, left) : null, right] : null;
    const main = el('div', { class: 'tc-main' });
    let led = false;
    for (const [name, node] of nodes) {
      if (LEAD.has(name)) {
        if (led) continue; led = true;
        put(main, joined ? el('section', { class: 'tc-instrument tc-glass', 'aria-label': view.title }, el('div', { class: 'tc-instrument-top' }, lead), part('line')) : el('div', { class: 'tc-lead' + (left.length ? '' : ' tc-lead-end') }, lead));
      } else if (name === 'line') { if (!joined) put(main, el('section', { class: 'tc-instrument tc-instrument-chart tc-glass', 'aria-label': view.title }, node)); }
      else put(main, node);
    }
    if (!o.rail) return { main, rail: null };
    if (o.rail.block === 'timeline') return { main, rail: liveRail(view, ui, context) };
    // The list follows the chart point or the selected family’s declared selection.
    const bucket = ui.bucket != null && view.metric ? view.metric.points[ui.bucket] || null : null;
    const selection = adapter.selection?.(view, ui, context);
    const rows = bucket ? view.events.filter(event => event.at != null && event.at >= bucket.start && event.at < bucket.end) : selection?.rows || view.events;
    const chosen = bucket || selection?.chosen;
    const whole = !chosen ? null : el('button', { type: 'button', class: 'tc-text-button', data: { focus: 'whole' },
      text: bucket ? (o.periods ? PERIODS[view.period].whole : 'Show the whole period') : selection.resetLabel,
      onclick: () => (bucket ? context.setBucket(null) : selection.reset()) });
    const waiting = adapter.waiting?.[view.family] || 'The first event lands here.';
    // The list holds the newest rows of an output: beside a longer count it says how many of them it shows.
    const counted = view.total !== null && view.total > view.count ? `${grouped(view.count)} of ${grouped(view.total)}` : grouped(view.count);
    return { main, rail: rail(chosen ? chosen.name : plain(o.rail.title) || adapter.railTitles?.[view.family] || RAIL_TITLE[view.family], chosen ? grouped(rows.length) : counted, occurrenceList(rows, { ...context, waiting }), whole) };
  }

  /* A canvas whose first read has yet to answer: its heading over a resting figure. */
  function restingCanvas() {
    return { main: el('div', { class: 'tc-main' }, el('p', { class: 'tc-figure tc-figure-rest', 'aria-hidden': 'true' }, el('span'))), rail: null };
  }

  /* mount: the full canvas of one display. update() repaints from new records and keeps scroll, focus, the period
   * and a selection. The phone slot is built once and stays in place across every repaint. A locked canvas (a Core
   * display) carries neither the chat control nor the canvas control, and has no phone slot. */
  function mount(options = {}) {
    const root = el('div', { class: 'tc-canvas' }), frame = el('div', { class: 'tc-frame' });
    if (adapter.rootClass) root.classList.add(adapter.rootClass);
    const phoneSlot = options.phone === false || isLocked(options.agent, options.locked) ? null : el('div', { class: 'tc-phone', data: { displayPhoneSlot: '' } });
    if (phoneSlot && adapter.phoneClass) { phoneSlot.classList.add(adapter.phoneClass); phoneSlot.dataset[adapter.phoneAttribute] = ''; }
    const ui = { bucket: null, ...(adapter.initialSelection || {}), held: [], fresh: null, seen: null, shown: PAGE, picking: false };
    const parts = { head: null, main: null, rail: null };
    let input = { agent: options.agent, observations: options.observations ?? null, period: options.period || 'week', profileName: options.profileName || '', family: options.family || '', notice: '' };
    let view = null, painted = '', headKey = '', watcher = null;
    const scroller = () => options.scroller?.() || root.closest('[data-display-scroll]') || root.parentElement;
    const focusOn = key => root.querySelector(`[data-focus="${CSS.escape(key)}"]`)?.focus({ preventScroll: true });
    const context = {
      get now() { return view.now; },
      onOpenEvidence: options.onOpenEvidence || null,
      setPeriod(period) { ui.bucket = null; input.period = period; paint(true, `period-${period}`); options.onPeriod?.(period); },
      setBucket(index) { ui.bucket = index; paint(true, 'chart'); },
      setSelection(name, key, prefix = name) { const from = ui[name]; ui[name] = key; paint(true, `${prefix}-${key || from}`); },
      revealHeld() { const first = ui.held[0]; ui.fresh = new Set(ui.held); ui.shown += ui.held.length; ui.held = []; paint(true, `feed-${first}`); root.querySelector('.tc-live')?.scrollIntoView({ block: 'start', behavior: reducedMotion() ? 'auto' : 'smooth' }); },
      // The timeline runs on: the next set is drawn when the reader reaches the end, or presses the control.
      showEarlier(byHand) { const held = byHand || document.activeElement?.dataset?.focus === 'earlier', before = root.querySelectorAll('.tc-feed-open[data-focus]').length; ui.shown += PAGE; paint(true); if (held) (root.querySelectorAll('.tc-feed-open[data-focus]')[before] || root.querySelector('.tc-feed-more'))?.focus({ preventScroll: true }); },
    };
    const outside = event => { if (!root.querySelector('.tc-pick')?.contains(event.target)) closePicker(false); };
    function openPicker() { ui.picking = true; document.addEventListener('pointerdown', outside, true); paint(true, `family-${view.family}`); }
    function closePicker(keepFocus) { if (!ui.picking) return; ui.picking = false; document.removeEventListener('pointerdown', outside, true); paint(true, keepFocus ? 'canvas' : ''); }
    function pick(id) {
      ui.picking = false; document.removeEventListener('pointerdown', outside, true);
      if (id !== view.family) { input.family = id; input.notice = ''; Object.assign(ui, { bucket: null, ...(adapter.initialSelection || {}), held: [], seen: null, known: false, shown: PAGE }); options.onFamily?.(id); }
      paint(true, 'canvas');
    }
    /* The canvas control: the set of families, each with the data it needs. */
    function picker() {
      const menu = !ui.picking ? null : el('div', { class: 'tc-pick-menu', role: 'menu', 'aria-label': 'Canvas' }, view.families.map(item =>
        el('button', { type: 'button', role: 'menuitemradio', 'aria-checked': String(item.id === view.family), 'aria-disabled': item.available ? null : 'true', tabindex: item.id === view.family ? '0' : '-1', data: { focus: `family-${item.id}`, family: item.id }, onclick: () => { if (item.available) pick(item.id); } },
          el('span', { class: 'tc-pick-name', text: item.name }), el('span', { class: 'tc-pick-needs', text: item.needs }), item.id === view.family ? icon('check') : null)));
      const wrap = el('div', { class: 'tc-pick' }, el('button', { type: 'button', class: 'tc-control tc-pick-open', 'aria-haspopup': 'menu', 'aria-expanded': String(ui.picking), 'aria-label': `Canvas: ${view.familyName}`, data: { focus: 'canvas' }, onclick: () => (ui.picking ? closePicker(true) : openPicker()) },
        icon('canvas'), el('span', { text: view.familyName }), icon('chevron')), menu);
      wrap.addEventListener('keydown', event => {
        if (!ui.picking) return;
        const items = [...wrap.querySelectorAll('[role="menuitemradio"]')], at = items.indexOf(document.activeElement);
        const to = { ArrowDown: at + 1, ArrowUp: at - 1, Home: 0, End: items.length - 1 }[event.key];
        if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closePicker(true); }
        else if (to !== undefined) { event.preventDefault(); items[(to + items.length) % items.length].focus(); }
      });
      wrap.addEventListener('focusout', () => setTimeout(() => { if (wrap.isConnected && ui.picking && !wrap.contains(document.activeElement)) closePicker(false); }, 0));
      return wrap;
    }
    function head() {
      const title = view.title;
      const controls = view.locked ? null : el('div', { class: 'tc-controls' },
        el('button', { type: 'button', class: 'tc-control tc-chat', data: { crAction: 'open-agent-chat', focus: 'chat' }, 'aria-label': `Chat with ${title}` }, icon('chat'), el('span', { text: 'Chat' })), picker());
      return el('header', { class: 'cr-detail-heading tc-head' },
        el('button', { type: 'button', class: 'cr-back-button tc-back', data: { crAction: 'back-home', focus: 'back' }, text: '← Home' }),
        el('div', { class: 'tc-title' }, el('h1', { text: title }), view.paused ? el('span', { class: 'tc-tag', text: 'Paused' }) : null, controls),
        view.description ? el('p', { class: 'tc-standfirst', text: view.description }) : null,
        view.needs ? el('p', { class: 'tc-needs', text: view.needs }) : null,
        input.notice ? el('p', { class: 'tc-needs tc-notice', role: 'status', text: input.notice }) : null);
    }
    function place(name, node) {
      if (!node) { parts[name]?.remove(); parts[name] = null; return; }
      if (parts[name]) parts[name].replaceWith(node); else frame.append(node);
      parts[name] = node;
    }
    function watchEarlier() {
      watcher?.disconnect(); watcher = null;
      const more = root.querySelector('.tc-feed-more');
      if (!more || typeof IntersectionObserver !== 'function') return;
      watcher = new IntersectionObserver(entries => { if (entries.some(entry => entry.isIntersecting)) context.showEarlier(false); });
      watcher.observe(more);
    }
    function paint(force, focusKey) {
      const now = options.now ?? Date.now();
      view = model(input.agent, input.observations, { period: input.period, now, profileName: input.profileName, family: input.family, locked: options.locked });
      // New live entries wait behind a control while the reader is further down the feed.
      if (view.family === 'live') {
        const ids = view.feed.map(item => item.id);
        if (ui.seen && ui.known) {
          const arrived = ids.filter(id => !ui.seen.has(id) && !ui.held.includes(id));
          const feed = root.querySelector('.tc-feed'), area = scroller();
          const reading = feed && area && feed.getBoundingClientRect().top < area.getBoundingClientRect().top - 48;
          if (arrived.length) { if (reading) ui.held = [...ui.held, ...arrived]; else { ui.fresh = new Set(arrived); ui.shown += arrived.length; } }
        }
        ui.seen = new Set(ids); ui.held = ui.held.filter(id => ids.includes(id)); ui.known = view.loaded;
      }
      const key = [view.revision, view.period, view.family, ui.bucket, JSON.stringify(adapter.selectionKey?.(ui) || []), ui.held.length, ui.shown, ui.picking, view.loaded, view.paused, input.notice].join('|');
      if (!force && key === painted) return;
      painted = key;
      const area = scroller(), areaTop = area?.scrollTop || 0, railTop = root.querySelector('.tc-rail-scroll')?.scrollTop || 0;
      const active = document.activeElement, own = active && root.contains(active) && !phoneSlot?.contains(active);
      const wanted = focusKey || (own ? active.dataset.focus || '' : '');
      stopPreview();
      root.dataset.ownerAgentId = input.agent?.id || ''; root.dataset.family = view.family; root.dataset.revision = view.revision; root.dataset.period = view.period; root.dataset.locked = String(view.locked);
      // The output revision this canvas draws: the same one its tile on Home names.
      if (view.out) root.dataset.output = String(view.out.revision); else delete root.dataset.output;
      root.setAttribute('aria-busy', String(!view.loaded));
      const nextHead = [view.title, view.description, view.needs, view.family, ui.picking, view.locked, view.paused, view.families.map(item => Number(item.available)).join(''), input.notice].join('|');
      if (nextHead !== headKey) { headKey = nextHead; place('head', head()); }
      // A published output is drawn through its presentation's blocks; before one, the family draws the agent's events.
      // Until a read has answered, the canvas rests: a number here always comes from the display's records.
      const made = !view.loaded ? restingCanvas() : (view.out ? blockCanvas : adapter.canvases?.[view.family] || CANVASES[view.family] || occurrenceCanvas)(view, ui, context);
      place('main', made.main);
      if (phoneSlot && !phoneSlot.isConnected) frame.append(phoneSlot);
      place('rail', made.rail); root.dataset.rail = String(!!made.rail);
      if (ui.fresh && !reducedMotion()) setTimeout(() => { root.querySelectorAll('.tc-arrived').forEach(node => node.classList.remove('tc-arrived')); }, 900);
      ui.fresh = null;
      const list = root.querySelector('.tc-rail-scroll'); if (list) list.scrollTop = railTop;
      if (area) area.scrollTop = areaTop;
      if (wanted) focusOn(wanted);
      watchEarlier();
    }
    root.append(frame); paint(true);
    if (!reducedMotion()) { root.classList.add('tc-entering'); setTimeout(() => root.classList.remove('tc-entering'), 900); }
    return {
      element: root, phoneSlot, get agentId() { return input.agent?.id; }, get family() { return view.family; }, get revision() { return view.revision; }, get locked() { return view.locked; },
      get output() { return view.out ? view.out.revision : null; },
      update(next = {}) { input = { ...input, ...next }; paint(false); },
      destroy() { stopPreview(); watcher?.disconnect(); document.removeEventListener('pointerdown', outside, true); root.remove(); },
    };
  }

  /* tile: the compact projection of the same model, for Home. */
  function tile(options = {}) {
    const agent = options.agent || {}, view = model(agent, options.observations ?? null, { period: options.period || 'week', now: options.now ?? Date.now(), profileName: options.profileName || '', family: options.family || '', locked: options.locked });
    const card = el('article', { class: 'tc-tile tc-glass' + (options.arrived ? ' tc-arrived' : ''), data: { agentId: agent.id || '', ownerAgentId: agent.id || '', family: view.family, revision: view.revision, period: view.period }, 'aria-busy': String(!view.loaded) });
    if (adapter.tileClass) card.classList.add(adapter.tileClass);
    const open = el('button', { type: 'button', class: 'tc-tile-open', data: { displayAction: 'open-agent', agentId: agent.id || '' }, onclick: () => options.onOpenAgent?.(agent.id) }, el('span', { class: 'tc-tile-title', text: view.title }));
    if (adapter.openAttribute) open.dataset[adapter.openAttribute] = 'open-agent';
    const figure = (value, unit) => el('span', { class: 'tc-tile-figure' }, el('span', { class: 'tc-tile-numeral', text: String(value) }), unit ? el('em', { text: unit }) : null);
    const line = (text, time) => text ? el('span', { class: 'tc-tile-line' }, el('span', { text }), time ? el('span', { class: 'tc-time', text: time }) : null) : null;
    if (!view.loaded) put(open, el('span', { class: 'tc-tile-loading', 'aria-hidden': 'true' }));
    else if (view.out) {
      const o = view.out, shown = written(o.row), values = (Array.isArray(o.row.series) ? o.row.series : []).map(point => finite(point?.value) ?? 0), mark = o.row.mark;
      const share = finite(o.row.fraction) ?? (o.row.kind === 'rate' && shown.value !== null ? (o.row.unit === '%' ? shown.value / 100 : shown.value) : 0);
      const art = o.tileWaits ? kitMark('ghost', mark, { surface: 'paper' }) : mark === 'ring' || mark === 'arc' ? kitMark(mark, Math.max(0, Math.min(1, share)), { surface: 'paper' }) : mark === 'bars' ? kitMark('bars', values, { size: 'tile', surface: 'paper' }) : values.length > 1 ? spark(values) : null;
      card.dataset.output = String(o.revision);
      put(open, art ? el('span', { class: 'tc-tile-mark' }, art) : null, o.row.state === 'needs' || shown.value === null ? el('span', { class: 'tc-tile-needs', text: plain(o.row.needs) || 'Needs a source' }) : figure(shown.text, shown.unit), line(o.label));
    } else if (view.needs) put(open, el('span', { class: 'tc-tile-needs', text: view.needs }));
    else if (view.family === 'metric') {
      put(open, el('span', { class: 'tc-tile-mark' }, spark(view.metric.points.map(point => point.value))), figure(view.metric.total, PERIODS[view.period].unit), line(view.metric.delta));
    } else if (view.family === 'live') {
      const latest = view.feed.find(item => item.kind === 'entry') || null;
      put(open, el('span', { class: 'tc-tile-mark' }, el('span', { class: 'tc-live-dot', 'aria-hidden': 'true' })),
        figure(view.today, 'today'), latest ? line(latest.text, latest.at != null ? when(latest.at, view.now) : latest.timeLabel) : null);
    } else if (adapter.tile?.(view, open, figure, line)) {
      // The selected vertical drew its own family from this same view.
    } else {
      const latest = view.events[0];
      const first = (view.summary.text.match(/^[^.!?\n]+[.!?]?/) || [''])[0].trim();
      put(open, figure(view.more ? `${view.count}+` : view.count), latest ? line(latest.text, latest.at != null ? when(latest.at, view.now) : latest.timeLabel) : line(first));
    }
    card.append(open);
    return { element: card, family: view.family, revision: view.revision, output: view.out ? view.out.revision : null };
  }

  /* A host-admitted display always names the actual agent that maintains it. */
  function displayInput(display) {
    if (!display || typeof display !== 'object' || ![display.display_id, display.owner_agent_id].every(value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/.test(value))) {
      throw new TypeError('Give the display its registered identifier and owning agent.');
    }
    const output = display.output || display;
    if (!output.tile || !output.canvas || !display.presentation) throw new TypeError('Give the display its published output and presentation.');
    return { agent: { ...(display.agent || {}), id: display.owner_agent_id, title: output.tile.label || display.title || 'Display' },
      observations: { output, presentation: display.presentation, families: display.families || [] } };
  }
  function mountDisplay(options = {}) {
    const view = mount({ ...options, ...displayInput(options.display) });
    const update = view.update;
    view.update = (next = {}) => update({ ...next, ...(next.display ? displayInput(next.display) : {}) });
    return view;
  }
  function tileDisplay(options = {}) { return tile({ ...options, ...displayInput(options.display) }); }

  const families = (agent, observations, options) => model(agent, observations, options).families;
  adapter = extend({el, plain, finite, stamp, when, span, humanize, ownUrl, rich, recommendation, periodToggle, rail, occurrenceList, metric, put, grouped, PERIODS}) || {};
  FAMILY_SET = adapter.families || BASE_FAMILIES; FAMILIES = FAMILY_SET.map(item => item.id);
  return { FAMILIES, families, PERIODS: Object.fromEntries(Object.entries(PERIODS).map(([key, value]) => [key, value.label])), family, model, mount, tile, mountDisplay, tileDisplay };
  }
  window.RocotoDisplayCanvas = Object.freeze({ ...create(), create });
})();
