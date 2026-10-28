/* Read-only visualisation of the host's person/company activity projection. */
(() => {
  'use strict';
  const DAY = 86400000, MODES = ['daily', 'weekly', 'cumulative'];
  const defaultNode = (tag, className = '', text) => { const el = document.createElement(tag); el.className = className; if (text !== undefined) el.textContent = text; return el; };
  const count = value => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null;
  const full = value => count(value) === null ? '—' : value.toLocaleString('en');
  const compact = value => count(value) === null ? '—' : new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 }).format(value);
  const date = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(Date.parse(value + 'T00:00:00Z')) ? Date.parse(value + 'T00:00:00Z') : null;
  const iso = value => new Date(value).toISOString().slice(0, 10);

  function render(data, { node = defaultNode, icon, scope = 'person', mode = 'daily', loading = false, error = '', onMode, onAgent } = {}) {
    const root = node('section', 'profile-settings');
    const selectedScope = ['person', 'organisation'].includes(data?.scope) ? data.scope : scope === 'organisation' ? 'organisation' : 'person';
    root.dataset.scope = selectedScope;
    const title = selectedScope === 'organisation' ? 'Organization profile' : 'Profile';
    root.append(node('h1', 'profile-settings-title', title));
    if (error || !data) { const message = node('p', 'profile-settings-message', error || (loading ? 'Loading profile activity…' : 'Your profile activity appears here.')); message.setAttribute('role', 'status'); root.append(message); return root; }
    const identity = node('header', 'profile-identity'), name = typeof data.identity?.name === 'string' ? data.identity.name : title;
    const avatar = node('div', 'profile-avatar', name.trim().split(/\s+/).filter(Boolean).slice(0, 2).map(part => [...part][0]).join('').toLocaleUpperCase()); avatar.setAttribute('aria-hidden', 'true');
    identity.append(avatar, node('h2', '', name), node('p', '', selectedScope === 'organisation' ? 'Organization activity' : 'Personal activity')); root.append(identity);
    const totals = data.totals || {}, stats = node('dl', 'profile-stats');
    for (const [field, label] of [['tokens', 'Tokens'], ['agent_runs', 'Agent runs'], ['agent_runs_completed', 'Completed runs'], ['chats', 'Chats'], ['alerts_sent', 'Alerts sent']]) {
      const item = node('div'), value = node('dd', '', compact(totals[field])); value.title = full(totals[field]); item.append(value, node('dt', '', label));
      if (field === 'alerts_sent' && count(totals[field]) === null) item.title = 'Delivery tracking is being prepared.';
      stats.append(item);
    }
    root.append(stats);
    const from = date(data.start_date), until = date(data.end_date), validRange = from !== null && until !== null && until >= from && until - from < 366 * DAY;
    if (!validRange) { root.append(node('p', 'profile-settings-message', 'Refresh your activity period.')); return root; }
    const period = node('p', 'profile-period', `${data.start_date} – ${data.end_date} · ${data.timezone === 'UTC' ? 'UTC' : 'Recorded time'}`); root.append(period);
    const rows = new Map((Array.isArray(data.daily) ? data.daily : []).slice(0, 366).filter(row => date(row?.date) !== null).map(row => [row.date, row]));
    const tokens = node('section', 'profile-activity'), tokenHead = node('div', 'profile-activity-heading'); tokenHead.append(node('h3', '', 'Token activity'));
    const controls = node('div', 'profile-mode-controls'); controls.setAttribute('role', 'group'); controls.setAttribute('aria-label', 'Activity aggregation');
    const tokenChart = node('div'), agentSection = node('section', 'profile-activity'), agentChart = node('div');
    agentSection.append(node('h3', '', 'Agent activity'), agentChart);
    let selectedMode = MODES.includes(mode) ? mode : 'daily'; const buttons = new Map();

    function chart(target, metric, label) {
      target.replaceChildren();
      const calendar = node('div', 'profile-calendar'), labels = node('div', 'profile-day-labels'); labels.setAttribute('aria-hidden', 'true');
      ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].forEach(day => labels.append(node('span', '', day)));
      const viewport = node('div', 'profile-calendar-scroll'); viewport.tabIndex = 0; viewport.setAttribute('aria-label', `${label} by day and week`);
      const grid = node('div', 'profile-heatmap'); grid.setAttribute('role', 'img'); grid.setAttribute('aria-label', `${label}, ${selectedMode}, ${data.start_date} to ${data.end_date}. Detailed daily values follow.`);
      const monthLabels = node('div', 'profile-month-labels'); monthLabels.setAttribute('aria-hidden', 'true');
      const start = from - ((new Date(from).getUTCDay() + 6) % 7) * DAY, weeks = Math.ceil((until - start + DAY) / (7 * DAY));
      grid.style.setProperty('--profile-weeks', String(weeks)); monthLabels.style.setProperty('--profile-weeks', String(weeks));
      const values = []; let cumulative = 0, cumulativeKnown = true, cumulativeStarted = false;
      for (let time = from; time <= until; time += DAY) {
        const row = rows.get(iso(time)), raw = count(row?.[metric]); let value = raw;
        if (selectedMode === 'weekly') {
          const monday = time - ((new Date(time).getUTCDay() + 6) % 7) * DAY;
          const week = (Array.isArray(data.weekly) ? data.weekly : []).find(item => date(item.start_date) !== null && date(item.end_date) !== null && (date(item.start_date) === monday || date(item.start_date) <= time && date(item.end_date) >= time));
          value = count(week?.[metric]);
        } else if (selectedMode === 'cumulative') { if (raw === null) { if (cumulativeStarted) cumulativeKnown = false; } else { cumulativeStarted = true; cumulative += raw; } value = raw !== null && cumulativeKnown && Number.isSafeInteger(cumulative) ? cumulative : null; }
        values.push({ time, raw, value });
      }
      const max = Math.max(1, ...values.map(item => item.value || 0)), byDate = new Map(values.map(item => [item.time, item]));
      let priorMonth = '';
      for (let week = 0; week < weeks; week++) {
        const weekStart = start + week * 7 * DAY, month = new Date(Math.max(from, weekStart)).toLocaleDateString('en', { month: 'short', timeZone: 'UTC' });
        const monthLabel = node('span', '', month !== priorMonth ? month : ''); monthLabel.style.gridColumn = String(week + 1); monthLabels.append(monthLabel); priorMonth = month;
        for (let day = 0; day < 7; day++) {
          const time = weekStart + day * DAY, item = byDate.get(time), cell = node('span', 'profile-day'); cell.setAttribute('aria-hidden', 'true');
          if (!item) cell.dataset.level = 'padding';
          else { cell.dataset.date = iso(time); cell.dataset.level = item.value === null ? 'unknown' : item.value === 0 ? '0' : String(Math.min(4, Math.max(1, Math.ceil(item.value / max * 4)))); cell.title = `${iso(time)} · ${item.value === null ? 'Awaiting records' : full(item.value) + ' ' + label.toLocaleLowerCase()}${selectedMode === 'weekly' ? ' · week total' : selectedMode === 'cumulative' ? ' · running period total' : ''}`; }
          grid.append(cell);
        }
      }
      viewport.append(grid, monthLabels); calendar.append(labels, viewport); target.append(calendar);
      const note = node('p', 'profile-chart-note', selectedMode === 'weekly' ? 'Each column shows its week total.' : selectedMode === 'cumulative' ? 'Running totals for the selected period.' : 'Each square represents one day.');
      const available = data.metrics?.[metric]?.available;
      if (available === false) note.textContent = metric === 'tokens' ? 'Token activity appears as attributed usage is recorded.' : 'Agent activity appears as runs are recorded.';
      target.append(note);
      if (metric === 'tokens' && selectedScope === 'person' && data.metrics?.tokens?.coverage === 'attributed_api_receipts') {
        const since = typeof data.metrics.tokens.since === 'string' ? data.metrics.tokens.since.slice(0, 10) : '';
        target.append(node('p', 'profile-chart-note profile-token-coverage', date(since) !== null ? `Personal token records from ${since}.` : 'Personal tokens cover attributed usage.'));
      }
      const detail = node('details', 'profile-daily-details'); detail.append(node('summary', '', `View daily ${label.toLocaleLowerCase()}`));
      const list = node('dl'); for (const item of values) { const pair = node('div'); pair.append(node('dt', '', iso(item.time)), node('dd', '', full(item.raw))); list.append(pair); } detail.append(list); target.append(detail);
      queueMicrotask(() => { if (viewport.isConnected) viewport.scrollLeft = viewport.scrollWidth; });
    }
    function update(value) {
      selectedMode = value; for (const [key, button] of buttons) button.setAttribute('aria-pressed', String(key === value));
      chart(tokenChart, 'tokens', 'Tokens'); chart(agentChart, 'agent_runs', 'Agent runs');
    }
    for (const key of MODES) { const button = node('button', '', key[0].toUpperCase() + key.slice(1)); button.type = 'button'; button.addEventListener('click', () => { update(key); onMode?.(key); }); buttons.set(key, button); controls.append(button); }
    tokenHead.append(controls); tokens.append(tokenHead, tokenChart); root.append(tokens, agentSection); update(selectedMode);
    const recent = node('section', 'profile-agents'); recent.append(node('h3', '', 'Agents'));
    const agents = (Array.isArray(data.agents) ? data.agents : []).slice(0, 100);
    if (!agents.length) recent.append(node('p', 'profile-chart-note', 'Your agents appear here as they run.'));
    for (const agent of agents) {
      const row = node('div', 'profile-agent-row'), name = typeof onAgent === 'function' && typeof agent.id === 'string' ? node('button', 'profile-agent-name') : node('span', 'profile-agent-name');
      if (name.tagName === 'BUTTON') { name.type = 'button'; name.addEventListener('click', () => onAgent(agent.id)); }
      if (icon) name.append(icon('agent')); name.append(node('span', '', agent.title || 'Agent'));
      row.append(name, node('span', 'profile-agent-count', `${full(agent.runs)} runs`)); recent.append(row);
    }
    root.append(recent); return root;
  }
  window.RocotoProfileSettings = Object.freeze({ render });
})();
