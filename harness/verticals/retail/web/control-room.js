/* The shell's Home: the location view, and the canvas a tracker tile opens.
 * Home shows the location view only. Its tiles live inside that view (web/control-room/); a tile of a tracker the
 * shop made opens that tracker's canvas here. A tracker with a Tracking Agent is read from the host; a tracker
 * whose records ride with the request (a Core Tracker, or a tracker that stands by itself) opens locked.
 * A canvas draws what the harness saved: the tracker's presentation and its published output arrive with
 * GET /api/agents/{id}/observations. A canvas picked here is saved with the tracker through
 * POST /api/agents/{id}/presentation, the same save as the harness's save_tracker_presentation. The canvas then
 * draws what is saved: a pick the server refuses returns to the saved canvas under the server's sentence.
 *   from the location view (same origin)   postMessage { type: 'rocoto:home-open-tracker', agent_id, period }
 *                                          postMessage { type: 'rocoto:home-open-tracker', tracker: { key, title, family, observations }, period }
 *                                          postMessage { type: 'rocoto:home-new-tracker' } · { type: 'rocoto:period', period }
 *   to the location view                   postMessage { type: 'rocoto:period', period } · { type: 'rocoto:home-focus-tile', key }
 *   on this window                         CustomEvent 'rocoto:home-open-tracker' { agentId } · 'rocoto:period' { period } */
(() => {
  'use strict';

  const DEFAULT_LOCATION = { name: 'Vitosha Boulevard 85', city: 'Sofia', address: '85 Vitosha Boulevard, Sofia' };
  const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
  const textValue = (value) => typeof value === 'string' ? value : value && typeof value === 'object' ? value.text || value.summary || value.description || '' : '';
  const dateLabel = (value) => {
    if (!value) return 'Awaiting first update';
    const date = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value);
    return Number.isFinite(date.getTime()) ? `Updated ${date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })}` : 'Awaiting first update';
  };
  const safeImage = (value) => {
    if (typeof value !== 'string' || !value.trim()) return '';
    try { const url = new URL(value, location.href); return ['http:', 'https:'].includes(url.protocol) ? url.href : ''; } catch { return ''; }
  };

  function create(options = {}) {
    const root = options.root;
    if (!root) throw new Error('ControlRoom requires a root element');
    const config = { ...DEFAULT_LOCATION, ...(window.RocotoControlRoomConfig?.location || {}), ...(options.location || {}) };
    const state = { view: 'home', selectedId: null, agents: [], profiles: [], observations: null, core: null, error: '', generation: 0, period: 'week' };
    // One reading per tracker: its canvas opens on the records its tile already showed. `unread` holds the trackers
    // whose first read has yet to answer: their canvas rests under the read's own sentence and asks again.
    const observed = new Map(), carried = new Set(), unread = new Set();
    const canvases = () => window.RocotoTrackerCanvas || null;
    let locationCleanup = null, phone = null, canvas = null, legacy = null, liveTimer = null;
    let homeEl = null, homeError = '', homeStale = false, homeScroll = 0, pendingOpen = null;

    const query = (selector) => root.querySelector(selector);
    const locationFrame = () => root.querySelector('.cr-location-frame');
    const tellFrame = (message) => { try { locationFrame()?.contentWindow?.postMessage(message, location.origin); } catch { /* the view reads it on its next load */ } };
    async function read(path) {
      const response = await fetch(path, { credentials: 'same-origin', cache: 'no-store' });
      if (!response.ok) throw new Error(response.status === 401 || response.status === 403 ? 'Open your workspace to review the control room.' : 'The control room is reconnecting.');
      return response.json();
    }
    const syncShellLocation = (event) => {
      const next = event.detail;
      if (!next || typeof next !== 'object') return;
      Object.assign(config, next);
      const frame = locationFrame();
      if (frame) frame.title = `${config.name || 'Store'} location overview`;
    };
    window.addEventListener('rocoto:location-shell', syncShellLocation);

    function profileName(agent) {
      const id = agent?.observation_profile_id || agent?.profile_id || agent?.observation_profile || agent?.profile_ids?.[0];
      const profile = state.profiles.find(item => item.id === id);
      return profile?.name || profile?.title || agent?.profile_name || '';
    }
    const isCore = (agent) => !!(agent && (agent.core || agent.agent_kind === 'core'));
    function agentById(id) { return state.core && state.core.id === id ? state.core : state.agents.find(agent => agent.id === id); }
    function agentFrequency(agent) {
      if (!agent || agent.mode === 'ad_hoc') return 'Runs when requested';
      const seconds = Number(agent.interval_seconds);
      if (!Number.isFinite(seconds)) return 'Scheduled';
      if (seconds % 86400 === 0) return `Every ${seconds / 86400}d`;
      if (seconds % 3600 === 0) return `Every ${seconds / 3600}h`;
      if (seconds % 60 === 0) return `Every ${seconds / 60}m`;
      return `Every ${seconds}s`;
    }
    // A canvas a person picks is saved with the tracker: its presentation's next revision, through the route the
    // harness's own tool shares. The canvas shows the pick while the save is on its way (`pending`). Once the
    // server has answered, the canvas draws what is saved: the pick, or the canvas as it was under the server's
    // sentence. A presentation someone saved in between (409) is read and the pick is sent once more.
    const pending = new Map();
    const RECONNECTING = 'The control room is reconnecting. Pick the canvas again in a moment.';
    const revisionOf = (id) => Number(observed.get(id)?.presentation?.revision) || 0;
    async function savePick(id, family) {
      pending.set(id, family);
      let notice = '', status = 0;
      try {
        for (let attempt = 0; attempt < 2; attempt++) {
          const sent = revisionOf(id);
          const response = await fetch(`/api/agents/${encodeURIComponent(id)}/presentation`, { method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', 'X-Rocoto-Page': '1' }, body: JSON.stringify({ expected_revision: sent, canvas: { family } }) });
          const answer = await response.json().catch(() => ({}));
          status = response.status; notice = '';
          if (response.ok) {
            // The saved presentation is the tracker's own from here on, whatever the next read does.
            if (answer.presentation && typeof answer.presentation === 'object') observed.set(id, { ...(observed.get(id) || {}), presentation: answer.presentation });
            break;
          }
          notice = typeof answer.detail === 'string' && answer.detail ? answer.detail : RECONNECTING;
          if (status !== 409 || attempt) break;
          await loadObservations(id);
          if (revisionOf(id) === sent) break;
        }
      } catch { notice = RECONNECTING; status = 0; }
      if (pending.get(id) === family) pending.delete(id);
      if (status >= 200 && status < 300) { if (state.selectedId === id) state.observations = observed.get(id) ?? null; await loadObservations(id); }
      if (canvas && canvas.agentId === id) { canvas.update({ observations: state.selectedId === id ? state.observations : observed.get(id) ?? null, family: pending.get(id) || '', notice }); watch(); }
      return status;
    }
    // Earlier versions kept the pick in this browser. It moves to the tracker once. The browser's copy goes when
    // the tracker holds the pick or the kit has refused it (400); an answer about the session, the service or a
    // tracker still to get its location keeps the copy for the next visit.
    const formerKey = (id) => `rocoto.canvas.family.${id}`;
    async function carryFormerPick(id) {
      let former = '';
      try { former = localStorage.getItem(formerKey(id)) || ''; } catch { return; }
      if (!former) return;
      const forget = () => { try { localStorage.removeItem(formerKey(id)); } catch { /* the next visit carries it */ } };
      // A presentation someone already saved is the tracker's own, and a name outside the set has no canvas.
      if (!canvases()?.FAMILIES.includes(former) || revisionOf(id) > 0) { forget(); return; }
      if (canvas && canvas.agentId === id) canvas.update({ family: former });
      const status = await savePick(id, former);
      if (status >= 200 && status < 300 || status === 400) forget();
    }

    function home() {
      return `<section class="cr-location-view" aria-label="Location overview"><div class="cr-location-slot" data-cr-location-slot><div class="cr-location-slot-copy"><strong>Location overview</strong><span>Loading the saved location view.</span></div></div></section>
        ${state.error ? `<p class="cr-error" role="alert">${escapeHtml(state.error)}</p>` : ''}`;
    }
    function buildHome() {
      if (locationCleanup) { locationCleanup(); locationCleanup = null; }
      homeEl?.remove();
      homeEl = document.createElement('div'); homeEl.className = 'cr-shell cr-home'; homeEl.innerHTML = home();
      homeError = state.error; homeStale = false;
      root.prepend(homeEl);
      const slot = homeEl.querySelector('[data-cr-location-slot]');
      if (slot && options.mountLocation) locationCleanup = options.mountLocation(slot, config) || null;
    }

    // The plain tracker view the shell keeps when the canvas module is away.
    function incidentCard(incident, index) {
      const image = safeImage(incident.thumbnail_url || incident.thumbnail || incident.image || incident.poster);
      const copy = textValue(incident.description || incident.summary || incident.label || incident.event || `Incident ${index + 1}`);
      return `<article class="cr-incident"><div class="cr-incident-media">${image ? `<img src="${escapeHtml(image)}" alt="${escapeHtml(copy)}" loading="lazy">` : '<span class="cr-incident-placeholder" aria-hidden="true">Evidence</span>'}</div><div><strong>${escapeHtml(copy)}</strong><small>${escapeHtml(incident.time_label || incident.timestamp || incident.time || 'Source timestamp recorded')}</small></div></article>`;
    }
    function legacyDetail(agent) {
      const summary = textValue(state.observations?.summary || agent.summary || agent.description || 'The tracker is ready for its next committed observation.');
      const incidents = Array.isArray(state.observations?.incidents) ? state.observations.incidents : [];
      return `<button type="button" class="cr-back-button" data-cr-action="back-home">← Home</button><header class="cr-detail-heading"><div><h1>${escapeHtml(agent.title || agent.name || 'Tracker Agent')}</h1><p>${escapeHtml(profileName(agent) || 'Observation profile')} · ${escapeHtml(agentFrequency(agent))}</p></div>${isCore(agent) ? '' : `<button type="button" class="cr-agent-pill" data-cr-action="open-agent-chat"><span class="cr-agent-pill-dot"></span><span><strong>Agent conversation</strong><small>${escapeHtml(dateLabel(agent.updated_at || agent.updated || agent.last_updated_at))}</small></span><span aria-hidden="true">›</span></button>`}</header>${agent.agent_kind === 'tracker' ? '<div class="cr-phone-slot" data-cr-phone-slot></div>' : ''}<div class="cr-detail-grid"><article class="cr-summary-card"><div class="cr-rich-summary">${escapeHtml(summary)}</div>${agent.recommendation || agent.recommendations ? `<section class="cr-recommendation"><h2>Recommendation</h2><p>${escapeHtml(textValue(agent.recommendation || agent.recommendations))}</p></section>` : ''}${isCore(agent) ? '' : '<button type="button" class="cr-primary-button" data-cr-action="open-agent-chat">Open agent conversation</button>'}</article><aside class="cr-incidents"><div class="cr-section-heading"><h2>Recent incidents</h2><span class="cr-count">${incidents.length}</span></div>${incidents.length ? incidents.slice(0, 6).map(incidentCard).join('') : '<div class="cr-empty-card"><strong>Evidence will appear here</strong><span>Committed tracker events will bring source intervals into this panel.</span></div>'}</aside></div>`;
    }
    // Alerts on your phone: one block per saved Tracking Agent, kept across this canvas's own refreshes.
    function syncPhone(focused) {
      const agent = state.view === 'tracker' ? agentById(state.selectedId) : null, slot = query('[data-cr-phone-slot]');
      const wanted = agent?.agent_kind === 'tracker' && slot && window.RocotoMessaging?.mountPhone ? agent : null;
      if (phone && (!wanted || phone.agentId !== wanted.id)) { phone.destroy(); phone = null; }
      if (!wanted) return;
      if (!phone) phone = window.RocotoMessaging.mountPhone({ agentId: wanted.id, agentTitle: wanted.title || wanted.name || '' });
      else phone.setTitle(wanted.title || wanted.name || '');
      if (phone.element.parentNode !== slot) slot.append(phone.element);
      if (focused?.isConnected) focused.focus({ preventScroll: true });
    }

    // One period for the location view and every canvas.
    function applyPeriod(period, source) {
      if (typeof period !== 'string' || period === state.period || (canvases() && !canvases().PERIODS[period])) return;
      state.period = period;
      if (source !== 'frame') tellFrame({ type: 'rocoto:period', period });
      if (source !== 'window') window.dispatchEvent(new CustomEvent('rocoto:period', { detail: { period } }));
      if (state.view === 'tracker') render();
    }
    const syncPeriod = (event) => applyPeriod(event.detail?.period || event.detail, 'window');
    window.addEventListener('rocoto:period', syncPeriod);

    function closeDetail() {
      if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
      if (canvas) { canvas.destroy(); canvas = null; }
      if (legacy) { legacy.remove(); legacy = null; }
    }
    // The canvas stays mounted across its own refreshes, so scroll, focus, the period and a selection hold.
    function mountCanvas(agent) {
      const input = { agent, observations: state.observations ?? observed.get(agent.id) ?? agent.observations ?? null, period: state.period, profileName: profileName(agent) };
      if (canvas && canvas.agentId === agent.id && root.contains(canvas.element)) { canvas.update(input); return; }
      closeDetail();
      canvas = canvases().mount({ ...input, locked: isCore(agent), phone: agent.agent_kind === 'tracker', family: isCore(agent) ? '' : pending.get(agent.id) || '',
        onPeriod: (period) => applyPeriod(period, 'canvas'), onFamily: (family) => { if (isCore(agent)) return; savePick(agent.id, family); watch(); }, onOpenEvidence: options.onOpenEvidence || null });
      root.append(canvas.element);
    }
    function mountLegacy(agent) {
      if (canvas) { canvas.destroy(); canvas = null; }
      if (!legacy) { legacy = document.createElement('div'); legacy.className = 'cr-shell cr-detail'; root.append(legacy); }
      legacy.innerHTML = legacyDetail(agent);
    }
    // An open canvas reads again by itself: a timeline for its new events, every canvas of a tracker whose
    // output is published, since its trackers run by themselves, and a canvas whose first read has yet to answer.
    // The canvas keeps its period, scroll and focus.
    function watch() {
      const wanted = state.view === 'tracker' && !!canvas && (canvas.family === 'live' || canvas.output != null || unread.has(state.selectedId)) && !isCore(agentById(state.selectedId));
      if (!wanted && liveTimer) { clearInterval(liveTimer); liveTimer = null; }
      if (wanted && !liveTimer) liveTimer = setInterval(() => { if (!document.hidden && state.selectedId) loadObservations(state.selectedId); }, Math.max(1000, Number(window.RocotoControlRoomConfig?.liveRefreshMs) || 15000));
    }
    function render() {
      const focused = phone?.element.contains(document.activeElement) ? document.activeElement : null;
      const agent = state.view === 'tracker' ? agentById(state.selectedId) : null;
      if (agent) {
        // Home stays mounted behind the canvas, so the location view keeps its place for the way back.
        if (homeEl && !homeEl.hidden) { homeScroll = root.scrollTop; homeEl.hidden = true; tellFrame({ type: 'rocoto:player-suspend' }); }
        if (canvases()) mountCanvas(agent); else mountLegacy(agent);
        root.hidden = false; syncPhone(focused); watch();
        return;
      }
      state.view = 'home'; state.selectedId = null;
      closeDetail();
      if (!homeEl || !homeEl.isConnected || homeStale || homeError !== state.error) buildHome();
      else if (homeEl.hidden) { homeEl.hidden = false; root.scrollTop = homeScroll; tellFrame({ type: 'rocoto:player-resume' }); }
      root.hidden = false;
      syncPhone(focused);
    }
    function openTracker(id) {
      const agent = agentById(id);
      if (!agent) { pendingOpen = id; load(); return; }
      pendingOpen = null; state.selectedId = id; state.view = 'tracker'; state.observations = observed.get(id) ?? agent.observations ?? null;
      render(); root.scrollTop = 0; query('.tc-back, .cr-back-button')?.focus({ preventScroll: true });
      if (!isCore(agent)) loadObservations(id);
    }
    // A tracker whose records ride with the request from the location view: a Core Tracker, or a tracker of the
    // company's own that stands by itself, which sends the row and the canvas its tile drew. Its canvas is locked.
    function openCore(tracker) {
      const key = String(tracker.key || tracker.id || '').slice(0, 80);
      if (!key) return;
      const given = tracker.observations && typeof tracker.observations === 'object' ? tracker.observations : { incidents: Array.isArray(tracker.events) ? tracker.events : [], summary: tracker.summary || null };
      state.core = { id: `core:${key}`, key, title: String(tracker.title || tracker.label || 'Tracker').slice(0, 100), description: textValue(tracker.description), core: true, agent_kind: 'core',
        mode: tracker.mode || 'ongoing', canvas: { family: tracker.family || tracker.canvas?.family || '' }, observations: given };
      openTracker(state.core.id);
    }
    function openFrom(detail) {
      if (!detail || typeof detail !== 'object') return;
      // The canvas opens on the period its tile showed.
      if (typeof detail.period === 'string') applyPeriod(detail.period, 'frame');
      const id = detail.agent_id || detail.agentId;
      if (typeof id === 'string' && id) openTracker(id);
      else if (detail.tracker && typeof detail.tracker === 'object') openCore(detail.tracker);
    }
    const fromWindow = (event) => openFrom(event.detail);
    function fromFrame(event) {
      const frame = locationFrame(), data = event.data;
      if (!frame || event.source !== frame.contentWindow || event.origin !== location.origin || !data || typeof data !== 'object') return;
      if (data.type === 'rocoto:home-new-tracker') options.onNewChat?.('tracker');
      else if (data.type === 'rocoto:home-open-tracker') openFrom(data);
      else if (data.type === 'rocoto:open-organization-chat') options.onOpenOrganization?.(data);
      else if (data.type === 'rocoto:period') applyPeriod(data.period, 'frame');
    }
    window.addEventListener('message', fromFrame);
    window.addEventListener('rocoto:home-open-tracker', fromWindow);

    async function loadObservations(id) {
      const generation = state.generation;
      try {
        const data = await read(`/api/agents/${encodeURIComponent(id)}/observations`);
        if (generation !== state.generation) return;
        const first = unread.delete(id);
        observed.set(id, data); state.error = '';
        if (state.selectedId !== id) return;
        state.observations = data;
        render();
        if (first && canvas && canvas.agentId === id) canvas.update({ notice: '' });
        if (!carried.has(id)) { carried.add(id); carryFormerPick(id); }
      } catch (error) {
        if (generation !== state.generation || state.selectedId !== id) return;
        state.error = error.message;
        // A canvas draws numbers from a read that answered. Its first read still open, it rests under the read's own sentence.
        if (!observed.has(id)) { unread.add(id); if (canvas && canvas.agentId === id) { canvas.update({ notice: error.message }); watch(); } }
        if (!canvas) render();
      }
    }
    async function load() {
      const generation = ++state.generation;
      state.error = '';
      try {
        const results = await Promise.allSettled([read('/api/retail/monitoring/profiles'), read('/api/agents')]);
        if (generation !== state.generation) return;
        const profiles = results[0].status === 'fulfilled' ? results[0].value : {};
        const agentData = results[1].status === 'fulfilled' ? results[1].value : {};
        state.profiles = Array.isArray(profiles) ? profiles : profiles.profiles || profiles.observation_profiles || [];
        state.agents = Array.isArray(agentData) ? agentData : agentData.agents || [];
        for (const id of [...observed.keys()]) if (!state.agents.some(agent => agent.id === id)) observed.delete(id);
        const failures = results.filter(result => result.status === 'rejected');
        if (failures.length && !state.agents.length) state.error = failures[0].reason?.message || 'The control room is reconnecting.';
      } finally {
        if (generation === state.generation) {
          render();
          if (pendingOpen) { const wanted = pendingOpen; pendingOpen = null; if (agentById(wanted)) openTracker(wanted); }
          else if (state.view === 'tracker' && state.selectedId && !isCore(agentById(state.selectedId))) loadObservations(state.selectedId);
        }
      }
    }
    function goHome() {
      const from = agentById(state.selectedId);
      state.view = 'home'; state.selectedId = null; state.observations = null; state.core = null;
      render();
      if (from) { tellFrame({ type: 'rocoto:home-focus-tile', key: isCore(from) ? from.key : `agent:${from.id}` }); (locationFrame() || query('[data-cr-action="new-chat"]'))?.focus({ preventScroll: true }); }
    }
    function handle(event) {
      const button = event.target.closest('[data-cr-action]');
      if (!button || !root.contains(button)) return;
      const action = button.dataset.crAction;
      if (action === 'back-home') { goHome(); return; }
      if (action === 'open-agent') { openTracker(button.dataset.agentId); return; }
      if (action === 'open-agent-chat') { const agent = agentById(state.selectedId); if (agent && !isCore(agent)) options.onOpenAgentChat?.(agent); return; }
      if (action === 'new-chat') options.onNewChat?.(button.dataset.agentKind);
    }
    root.addEventListener('click', handle);
    return {
      show() { state.view = 'home'; state.selectedId = null; state.core = null; render(); load(); },
      // Home in the shell's rail, and the location view's own retry, both land on a fresh Home.
      refresh() { state.view = 'home'; state.selectedId = null; state.core = null; state.observations = null; homeStale = true; render(); return load(); },
      openTracker,
      destroy() {
        state.generation += 1; closeDetail();
        if (phone) { phone.destroy(); phone = null; }
        if (locationCleanup) locationCleanup();
        window.removeEventListener('rocoto:location-shell', syncShellLocation); window.removeEventListener('rocoto:period', syncPeriod);
        window.removeEventListener('message', fromFrame); window.removeEventListener('rocoto:home-open-tracker', fromWindow);
        root.removeEventListener('click', handle); root.replaceChildren(); homeEl = null;
      }
    };
  }
  window.RocotoControlRoom = { create };
})();
