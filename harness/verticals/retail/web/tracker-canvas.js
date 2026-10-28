/* Retail display adapter: observation vocabulary, shelf states and product ranking. */
(() => {
  'use strict';
  window.RocotoTrackerCanvas = window.RocotoDisplayCanvas.create(shared => {
  const { el, plain, finite, stamp, when, span, humanize, ownUrl, rich, recommendation, periodToggle, rail, occurrenceList, metric, put, grouped, PERIODS } = shared;
  const STATE_TYPES = [
    ['empty', /(^|_)(empty|emptied|out_of_stock|stockout|gap)(_|$)/],
    ['replenished', /(^|_)(replenish|replenished|restock|restocked|refill|refilled)(_|$)/],
    ['stocked', /(^|_)(stocked|available|full)(_|$)/],
  ];
  const STATE_LABEL = { empty: 'Empty', replenished: 'Replenished', stocked: 'Stocked' };
  const RANK_TYPES = [
    ['bought', /(^|_)(purchase|purchased|bought|sale|sold|checkout)(_|$)/],
    ['returned', /(^|_)(put_back|putback|returned|return|replaced)(_|$)/],
    ['picked', /(^|_)(pick_up|picked_up|pickup|picked|interest|handled|touched)(_|$)/],
  ];
  const FAMILY_SET = [
    { id: 'metric', name: 'Chart', needs: 'Needs a number over time.', has: found => found.timed > 0 },
    { id: 'live', name: 'Timeline', needs: 'Needs timed events.', has: found => found.timed > 0 },
    { id: 'occurrences', name: 'Occurrences', needs: 'Needs instances with a thumbnail.', has: found => found.thumbs > 0 },
    { id: 'state', name: 'Shelves', needs: 'Needs shelf states.', has: found => found.states > 0 },
    { id: 'ranking', name: 'Products', needs: 'Needs events that name a product.', has: found => found.products > 0 },
  ];

  function stateOf(type) { const found = STATE_TYPES.find(([, pattern]) => pattern.test(type)); return found ? found[0] : ''; }
  function shelves(events, now) {
    const byShelf = new Map();
    for (const event of [...events].reverse()) {
      const state = stateOf(event.type); if (!state) continue;
      const key = event.subjectId || event.subject || 'shelf';
      if (!byShelf.has(key)) byShelf.set(key, { key, name: event.subject || humanize(key), history: [] });
      const shelf = byShelf.get(key); if (event.subject) shelf.name = event.subject;
      shelf.history.push({ state, at: event.at, event });
    }
    const order = { empty: 0, replenished: 1, stocked: 2 };
    const list = [...byShelf.values()].map(shelf => {
      const latest = shelf.history[shelf.history.length - 1];
      const lastEmpty = [...shelf.history].reverse().find(item => item.state === 'empty');
      const facts = [];
      if (latest.state === 'empty') facts.push({ label: 'Empty since', value: when(latest.at, now), extra: latest.at != null ? span(now - latest.at) : '' });
      if (latest.state === 'replenished') {
        if (lastEmpty) facts.push({ label: 'Empty since', value: when(lastEmpty.at, now), extra: '' });
        facts.push({ label: 'Replenished', value: when(latest.at, now), extra: lastEmpty && latest.at != null && lastEmpty.at != null ? `after ${span(latest.at - lastEmpty.at)}` : '' });
      }
      if (latest.state === 'stocked') facts.push({ label: 'Stocked since', value: when(latest.at, now), extra: '' });
      return { key: shelf.key, name: shelf.name, state: latest.state, since: latest.at, facts, history: [...shelf.history].reverse() };
    }).sort((a, b) => order[a.state] - order[b.state] || (b.since || 0) - (a.since || 0));
    const counts = { empty: 0, replenished: 0, stocked: 0 };
    for (const shelf of list) counts[shelf.state] += 1;
    const changes = list.flatMap(shelf => shelf.history.map(item => ({ ...item.event, shelf: shelf.key, state: item.state, text: `${shelf.name} · ${STATE_LABEL[item.state]}` }))).sort((a, b) => (b.at || 0) - (a.at || 0));
    return { list, counts, changes };
  }
  function ranking(events) {
    const items = new Map();
    for (const event of events) {
      const key = event.subjectId || event.subject; if (!key) continue;
      if (!items.has(key)) items.set(key, { key, name: event.subject || humanize(key), thumb: '', picked: 0, returned: 0, bought: 0, total: 0, events: [] });
      const item = items.get(key), kind = (RANK_TYPES.find(([, pattern]) => pattern.test(event.type)) || ['picked'])[0];
      item[kind] += 1; item.total += 1; item.events.push(event);
      if (!item.thumb && event.thumb) item.thumb = event.thumb;
      if (event.subject) item.name = event.subject;
    }
    return [...items.values()].sort((a, b) => b.total - a.total || a.name.localeCompare(b.name));
  }
  function outputShelves(list, events, now) {
    const order = { empty: 0, replenished: 1, stocked: 2 };
    const made = (Array.isArray(list) ? list : []).filter(shelf => shelf && typeof shelf === 'object').map((shelf, index) => {
      const since = stamp(shelf.empty_since), back = shelf.state === 'empty' ? null : stamp(shelf.replenished_at);
      const state = shelf.state === 'empty' ? 'empty' : shelf.state === 'replenished' || back != null ? 'replenished' : 'stocked', facts = [];
      if (state === 'empty' && since != null) facts.push({ label: 'Empty since', value: when(since, now), extra: span(now - since) });
      if (state === 'replenished') {
        if (since != null) facts.push({ label: 'Empty since', value: when(since, now), extra: '' });
        if (back != null) facts.push({ label: 'Replenished', value: when(back, now), extra: since != null && back > since ? `after ${span(back - since)}` : '' });
      }
      return { key: String(shelf.shelf ?? shelf.id ?? `shelf-${index}`), name: plain(shelf.label) || 'Shelf', state, since: state === 'empty' ? since : back ?? since, facts };
    }).sort((a, b) => order[a.state] - order[b.state] || (b.since || 0) - (a.since || 0));
    const counts = { empty: 0, replenished: 0, stocked: 0 };
    for (const shelf of made) counts[shelf.state] += 1;
    return { list: made, counts, changes: events.map(event => ({ ...event, shelf: event.subjectId })) };
  }
  function outputRanking(items, events) {
    return (Array.isArray(items) ? items : []).filter(item => item && typeof item === 'object' && plain(item.product)).map(item => {
      const name = plain(item.product), parts = [['picked', 'picked up'], ['returned', 'put back'], ['bought', 'bought']].filter(([key]) => finite(item[key]) !== null);
      return { key: name, name, thumb: ownUrl(item.thumb), picked: finite(item.picked) ?? 0, returned: finite(item.returned) ?? 0, bought: finite(item.bought) ?? 0,
        total: finite(item.value) ?? 0, parts, events: events.filter(event => event.subject === name) };
    });
  }
  function stateCanvas(view, ui, context) {
    const data = view.shelves, chosen = data.list.find(shelf => shelf.key === ui.shelf) || null;
    const counts = el('dl', { class: 'tc-counts' }, ['empty', 'replenished', 'stocked'].map(state => el('div', { data: { state } }, el('dt', { text: STATE_LABEL[state] }), el('dd', { class: 'tc-numeral', text: String(data.counts[state]) }))));
    const cards = el('ul', { class: 'tc-shelves' }, data.list.map(shelf => el('li', {},
      el('button', { type: 'button', class: 'tc-shelf tc-glass', 'aria-pressed': String(chosen?.key === shelf.key), data: { state: shelf.state, shelf: shelf.key, focus: `shelf-${shelf.key}` }, onclick: () => context.setSelection('shelf', chosen?.key === shelf.key ? null : shelf.key) },
        el('span', { class: 'tc-shelf-name', text: shelf.name }), el('span', { class: 'tc-state-mark', 'aria-hidden': 'true' }),
        el('strong', { class: 'tc-shelf-state', text: STATE_LABEL[shelf.state] }),
        el('span', { class: 'tc-shelf-facts' }, shelf.facts.map(fact => el('span', {}, el('span', { text: fact.label }), el('b', { text: fact.value }), fact.extra ? el('i', { text: fact.extra }) : null)))))));
    const main = el('div', { class: 'tc-main' }, counts, data.list.length ? cards : el('p', { class: 'tc-quiet', text: 'The first shelf reading lands here.' }),
      view.summary.text ? rich(view.summary.text, 'tc-rich tc-summary') : null, recommendation(view.recommendation));
    const changes = chosen ? data.changes.filter(change => change.shelf === chosen.key) : data.changes;
    const whole = chosen ? el('button', { type: 'button', class: 'tc-text-button', data: { focus: 'whole' }, text: 'Show every shelf', onclick: () => context.setSelection('shelf', null) }) : null;
    return { main, rail: rail(chosen ? chosen.name : 'History', changes.length, occurrenceList(changes, { ...context, waiting: 'The first shelf change lands here.' }), whole) };
  }
  function rankingCanvas(view, ui, context) {
    const items = view.ranking, chosen = items.find(item => item.key === ui.product) || null;
    const list = items.length ? el('ol', { class: 'tc-ranking' }, items.map((item, index) => el('li', {},
      el('button', { type: 'button', class: 'tc-rank tc-glass', 'aria-pressed': String(chosen?.key === item.key), data: { focus: `product-${item.key}` }, onclick: () => context.setSelection('product', chosen?.key === item.key ? null : item.key) },
        el('span', { class: 'tc-rank-place', text: String(index + 1) }), el('span', { class: 'tc-thumb tc-thumb-small' + (item.thumb ? '' : ' tc-thumb-open') }, item.thumb ? el('img', { src: item.thumb, alt: '', loading: 'lazy' }) : null),
        el('span', { class: 'tc-rank-name', text: item.name }),
        el('span', { class: 'tc-rank-split' }, [['picked', 'picked up'], ['returned', 'put back'], ['bought', 'bought']].map(([key, label]) => el('span', {}, el('b', { text: String(item[key]) }), ` ${label}`))),
        el('strong', { class: 'tc-rank-total', text: String(item.total) }))))) : el('p', { class: 'tc-quiet', text: 'The first product lands here.' });
    const main = el('div', { class: 'tc-main' }, el('div', { class: 'tc-ranking-top' }, periodToggle(view.period, context.setPeriod)), list, view.summary.text ? rich(view.summary.text, 'tc-rich tc-summary') : null, recommendation(view.recommendation));
    const shown = chosen ? chosen.events : items.flatMap(item => item.events).sort((a, b) => (b.at || 0) - (a.at || 0));
    const whole = chosen ? el('button', { type: 'button', class: 'tc-text-button', data: { focus: 'whole' }, text: 'Show every product', onclick: () => context.setSelection('product', null) }) : null;
    return { main, rail: rail(chosen ? chosen.name : 'Sessions', shown.length, occurrenceList(shown, { ...context, waiting: 'The first session lands here.' }), whole) };
  }
  return {
    families: FAMILY_SET, agentLabel: 'Tracking Agent',
    rootClass: 'cr-detail', phoneClass: 'cr-phone-slot', phoneAttribute: 'crPhoneSlot', tileClass: 'cr-agent-card', openAttribute: 'crAction',
    initialSelection: { shelf: null, product: null }, selectionKey: ui => [ui.shelf, ui.product],
    occurrence: raw => ({ subject: plain(raw.object_description) || plain(raw.product), subjectId: String(raw.item_id || raw.object_ref || raw.shelf || '') }),
    family(agent, observations) {
    const types = new Set();
    for (const profile of observations?.profiles || (observations?.profile ? [observations.profile] : [])) for (const entry of profile?.vocabulary || []) if (entry?.type) types.add(entry.type);
    for (const event of observations?.incidents || []) if (event?.type) types.add(event.type);
    const known = name => [...types].some(type => STATE_TYPES.find(([state]) => state === name)[1].test(type));
    if (known('empty') && (known('replenished') || known('stocked'))) return 'state';
    },
    signals: events => ({ states: events.filter(event => stateOf(event.type)).length, products: events.filter(event => (event.subject || event.subjectId) && !stateOf(event.type)).length }),
    output(view, shown) {
      if (shown.block('state_cards') || shown.block('state_counts')) view.shelves = outputShelves(shown.value('state_cards', 'shelves') ?? shown.value('state_counts', 'shelves'), view.events, view.now);
      if (shown.block('ranking')) view.ranking = outputRanking(shown.value('ranking', 'items'), view.events);
    },
    events(view) {
      if (view.family === 'state') view.shelves = shelves(view.events, view.now);
      if (view.family === 'ranking') view.ranking = ranking(metric(view.events, view.period, view.now).events);
    },
    canvases: { state: stateCanvas, ranking: rankingCanvas },
    railTitles: { state: 'History', ranking: 'Sessions' },
    waiting: { state: 'The first shelf change lands here.', ranking: 'The first session lands here.' },
    blocks(view, ui, context) {
      const shelfList = view.shelves, chosenShelf = shelfList?.list.find(shelf => shelf.key === ui.shelf) || null;
      const ranked = view.ranking || [], chosenProduct = ranked.find(item => item.key === ui.product) || null;
      return {
      state_counts: () => shelfList ? el('dl', { class: 'tc-counts' }, ['empty', 'replenished', 'stocked'].map(state => el('div', { data: { state } }, el('dt', { text: STATE_LABEL[state] }), el('dd', { class: 'tc-numeral', text: String(shelfList.counts[state]) })))) : null,
      state_cards: () => !shelfList ? null : shelfList.list.length ? el('ul', { class: 'tc-shelves' }, shelfList.list.map(shelf => el('li', {},
        el('button', { type: 'button', class: 'tc-shelf tc-glass', 'aria-pressed': String(chosenShelf?.key === shelf.key), data: { state: shelf.state, shelf: shelf.key, focus: `shelf-${shelf.key}` }, onclick: () => context.setSelection('shelf', chosenShelf?.key === shelf.key ? null : shelf.key) },
          el('span', { class: 'tc-shelf-name', text: shelf.name }), el('span', { class: 'tc-state-mark', 'aria-hidden': 'true' }),
          el('strong', { class: 'tc-shelf-state', text: STATE_LABEL[shelf.state] }),
          el('span', { class: 'tc-shelf-facts' }, shelf.facts.map(fact => el('span', {}, el('span', { text: fact.label }), el('b', { text: fact.value }), fact.extra ? el('i', { text: fact.extra }) : null))))))) : el('p', { class: 'tc-quiet', text: 'The first shelf reading lands here.' }),
      ranking: () => ranked.length ? el('ol', { class: 'tc-ranking' }, ranked.map((item, index) => el('li', {},
        el('button', { type: 'button', class: 'tc-rank tc-glass', 'aria-pressed': String(chosenProduct?.key === item.key), data: { focus: `product-${item.key}` }, onclick: () => context.setSelection('product', chosenProduct?.key === item.key ? null : item.key) },
          el('span', { class: 'tc-rank-place', text: String(index + 1) }), el('span', { class: 'tc-thumb tc-thumb-small' + (item.thumb ? '' : ' tc-thumb-open') }, item.thumb ? el('img', { src: item.thumb, alt: '', loading: 'lazy' }) : null),
          el('span', { class: 'tc-rank-name', text: item.name }),
          el('span', { class: 'tc-rank-split' }, item.parts.map(([key, label]) => el('span', {}, el('b', { text: String(item[key]) }), ` ${label}`))),
          el('strong', { class: 'tc-rank-total', text: new Intl.NumberFormat('en', { maximumFractionDigits: 1 }).format(item.total) }))))) : el('p', { class: 'tc-quiet', text: 'The first product lands here.' }),
      };
    },
    selection(view, ui, context) {
      const chosenShelf = view.shelves?.list.find(shelf => shelf.key === ui.shelf), chosenProduct = view.ranking?.find(item => item.key === ui.product);
      if (!chosenShelf && !chosenProduct) return null;
      return { chosen: chosenShelf || chosenProduct, rows: chosenShelf ? view.events.filter(event => event.subjectId === chosenShelf.key) : chosenProduct.events,
        resetLabel: chosenShelf ? 'Show every shelf' : 'Show every product', reset: () => chosenShelf ? context.setSelection('shelf', null) : context.setSelection('product', null) };
    },
    tile(view, open, figure, line) {
    if (view.family === 'state') {
      const latest = view.shelves.changes[0];
      put(open, el('span', { class: 'tc-tile-mark tc-tile-shelves', 'aria-hidden': 'true' }, view.shelves.list.slice(0, 12).map(shelf => el('i', { data: { state: shelf.state } }))),
        figure(view.shelves.counts.empty, 'empty'), line(latest ? latest.text : '', latest ? when(latest.at, view.now) : ''));

      return true;
    }
    if (view.family === 'ranking') {
      put(open, el('ol', { class: 'tc-tile-top' }, view.ranking.slice(0, 3).map(item => el('li', {}, el('span', { text: item.name }), el('b', { text: String(item.total) })))), view.ranking.length ? null : figure(0, PERIODS[view.period].unit));

      return true;
    }
    return false;
    },
  };
  });
})();
