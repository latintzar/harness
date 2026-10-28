/* Host-owned business graph. Callbacks carry revisions; the host owns authorization. */
(() => {
  'use strict';
  const make = (tag, className = '', text) => { const el = document.createElement(tag); el.className = className; if (text !== undefined) el.textContent = text; return el; };
  const list = value => Array.isArray(value) ? value : [];
  const names = (rows, ids) => list(ids).map(id => list(rows).find(row => row.id === id)?.name || id).join(', ');
  const roles = { location_admin: 'Location admin', function_admin: 'Function admin', colleague: 'Colleague', alert_recipient: 'Alert recipient' };
  let serial = 0;
  function render({ data = null, loading = false, error = '', onSave, onRetry } = {}, { node = make } = {}) {
    const root = node('section', 'business-scope'); root.setAttribute('aria-label', 'Locations and access');
    let graph = data, activeDialog = null;
    const button = (label, action, primary = false) => { const el = node('button', primary ? 'agent-primary' : 'agent-secondary', label); el.type = 'button'; el.addEventListener('click', action); return el; };
    const can = kind => graph?.can_manage === true && list(graph.can_manage_kinds).includes(kind) && typeof onSave === 'function';
    function draw() {
      root.replaceChildren();
      const header = node('header', 'business-scope-heading'); header.append(node('h2', '', 'Locations and access'), node('p', '', 'Locations, rooms, cameras and the people who use them.')); root.append(header);
      if (error || loading || !graph) {
        const status = node('div', 'business-scope-status'); status.setAttribute('role', 'status'); status.append(node('p', '', error || (loading ? 'Loading locations…' : 'Your business locations appear here.')));
        if (error && onRetry) status.append(button('Retry', onRetry)); root.append(status); return;
      }
      const locations = node('section', 'business-scope-group'), locationHead = node('div', 'business-scope-group-heading'); locationHead.append(node('h3', '', 'Locations'));
      if (can('locations')) locationHead.append(button('Add location', event => edit('locations', null, event.currentTarget)));
      locations.append(locationHead);
      if (!list(graph.locations).length) locations.append(node('p', 'business-scope-empty', 'Add your first location to organize rooms, cameras and team access.'));
      for (const location of list(graph.locations)) {
        const card = node('article', 'business-location'), head = node('div', 'business-scope-row'), copy = node('div', 'business-scope-copy'); copy.append(node('h4', '', location.name), node('p', '', location.address || 'Address to add')); head.append(copy);
        if (can('locations')) head.append(button('Edit', event => edit('locations', location, event.currentTarget))); card.append(head);
        for (const [kind, title] of [['rooms', 'Rooms'], ['cameras', 'Cameras']]) {
          const group = node('div', 'business-location-section'), groupHead = node('div', 'business-scope-group-heading'); groupHead.append(node('h5', '', title));
          if (can(kind)) groupHead.append(button('Add ' + (kind === 'rooms' ? 'room' : 'camera'), event => edit(kind, { location_id: location.id }, event.currentTarget)));
          group.append(groupHead);
          const rows = list(graph[kind]).filter(row => row.location_id === location.id);
          if (!rows.length) group.append(node('p', 'business-scope-empty', kind === 'rooms' ? 'Rooms at this location appear here.' : 'Cameras at this location appear here.'));
          for (const row of rows) {
            const item = node('div', 'business-scope-row'), text = node('div', 'business-scope-copy'); text.append(node('strong', '', row.name));
            const detail = kind === 'rooms' ? names(graph.functions, row.function_ids) : names(graph.rooms, row.room_ids);
            if (detail) text.append(node('p', '', detail));
            if (kind === 'cameras') text.append(node('small', '', 'View revision ' + row.view_revision));
            item.append(text); if (can(kind)) { const action = button('Edit', event => edit(kind, row, event.currentTarget)); action.setAttribute('aria-label', 'Edit ' + row.name); item.append(action); } group.append(item);
          }
          card.append(group);
        }
        locations.append(card);
      }
      root.append(locations);
      const functions = node('section', 'business-scope-group'), functionHead = node('div', 'business-scope-group-heading'); functionHead.append(node('h3', '', 'Functions'));
      if (can('functions')) functionHead.append(button('Add function', event => edit('functions', null, event.currentTarget))); functions.append(functionHead);
      if (!list(graph.functions).length) functions.append(node('p', 'business-scope-empty', 'Create functions such as cooking, customer service or storage, then assign them to rooms.'));
      for (const row of list(graph.functions)) { const item = node('div', 'business-scope-row'); item.append(node('strong', '', row.name)); if (can('functions')) { const action = button('Edit', event => edit('functions', row, event.currentTarget)); action.setAttribute('aria-label', 'Edit function ' + row.name); item.append(action); } functions.append(item); }
      root.append(functions);
      const members = node('section', 'business-scope-group'); members.append(node('h3', '', 'Member access'), node('p', 'business-scope-note', 'Account roles and location access are managed separately.'));
      for (const row of list(graph.members)) {
        const item = node('div', 'business-scope-row'), text = node('div', 'business-scope-copy'); text.append(node('strong', '', row.display_name || row.member_id));
        const owner = row.account_role === 'owner'; text.append(node('p', '', owner ? 'Organization admin · All locations' : roles[row.role] || 'Location access to assign'));
        if (!owner) { text.append(node('small', '', list(row.location_ids).length ? names(graph.locations, row.location_ids) : graph.configured === false ? 'Current organization access' : row.role ? 'Location access revoked' : 'Location access to assign')); if (list(row.function_ids).length) text.append(node('small', '', names(graph.functions, row.function_ids))); }
        item.append(text); if (!owner && can('members') && row.member_id !== graph.effective?.principal_id) { const action = button('Manage access', event => edit('members', row, event.currentTarget)); action.setAttribute('aria-label', 'Manage access for ' + (row.display_name || row.member_id)); item.append(action); } members.append(item);
      }
      if (!list(graph.members).length) members.append(node('p', 'business-scope-empty', 'Your organization’s members appear here.')); root.append(members);
    }
    function edit(kind, original, opener) {
      if (!can(kind) || activeDialog) return;
      const value = original || {}, expectedRevision = graph.revision, isMember = kind === 'members', singular = { locations: 'location', functions: 'function', rooms: 'room', cameras: 'camera', members: 'member access' }[kind];
      const dialog = node('dialog', 'business-scope-dialog'); activeDialog = dialog;
      const form = node('form'), title = node('h2', '', isMember ? 'Member access' : (value.id ? 'Edit ' : 'Add ') + singular), titleId = 'business-scope-title-' + (++serial); title.id = titleId; dialog.setAttribute('aria-labelledby', titleId); form.append(title);
      const controls = {}, id = suffix => 'business-scope-' + serial + '-' + suffix;
      function input(name, label, initial, required = false) {
        const group = node('label', 'business-scope-field'), control = node('input'); control.id = id(name); control.name = name; control.value = initial || ''; control.required = required; control.maxLength = name === 'address' ? 500 : 160; group.append(node('span', '', label), control); form.append(group); controls[name] = control; return control;
      }
      function select(name, label, rows, initial) {
        const group = node('label', 'business-scope-field'), control = node('select'); control.name = name; control.setAttribute('aria-label', label);
        for (const row of rows) { const option = node('option', '', row.name); option.value = row.id; control.append(option); }
        control.value = initial || rows[0]?.id || ''; control.required = true; group.append(node('span', '', label), control); form.append(group); controls[name] = control; return control;
      }
      function checks(name, label, rows, selected, emptyText) {
        const group = node('fieldset', 'business-scope-options'); group.append(node('legend', '', label)); const chosen = new Set(list(selected));
        controls[name] = () => [...group.querySelectorAll('input:checked')].map(control => control.value);
        function refresh(nextRows) {
          group.querySelectorAll('label,p').forEach(el => el.remove());
          if (!nextRows.length) group.append(node('p', 'business-scope-note', emptyText));
          for (const row of nextRows) { const option = node('label'), control = node('input'); control.type = 'checkbox'; control.name = name; control.value = row.id; control.checked = chosen.has(row.id); control.addEventListener('change', () => { if (control.checked) chosen.add(row.id); else chosen.delete(row.id); }); option.append(control, node('span', '', row.name)); group.append(option); }
        }
        refresh(rows); form.append(group); return refresh;
      }
      if (isMember) {
        form.append(node('p', 'business-scope-member-name', value.display_name || value.member_id));
        const allowedRoles = graph.effective?.role === 'location_admin' ? ['function_admin', 'colleague', 'alert_recipient'] : graph.effective?.role === 'function_admin' ? ['colleague', 'alert_recipient'] : Object.keys(roles);
        select('role', 'Scope role', allowedRoles.map(key => ({ id: key, name: roles[key] })), allowedRoles.includes(value.role) ? value.role : 'colleague');
        checks('location_ids', 'Locations', list(graph.locations), value.location_ids, 'Add a location to assign access.');
        checks('function_ids', 'Functions', list(graph.functions), value.function_ids, 'Add a function to refine access.');
        form.append(node('p', 'business-scope-note', 'Selected locations set access. Select functions to narrow it. Clear every location to revoke scoped access. Function admins need a function assignment.'));
      } else {
        input('name', 'Name', value.name, true);
        if (kind === 'locations') input('address', 'Address', value.address);
        if (kind === 'rooms' || kind === 'cameras') {
          const location = select('location_id', 'Location', list(graph.locations), value.location_id);
          if (kind === 'rooms') checks('function_ids', 'Functions', list(graph.functions), value.function_ids, 'Create a function to assign it to this room.');
          else { const refresh = checks('room_ids', 'Visible rooms', list(graph.rooms).filter(row => row.location_id === location.value), value.room_ids, 'Create a room at this location to assign the camera view.'); location.addEventListener('change', () => refresh(list(graph.rooms).filter(row => row.location_id === location.value))); }
        }
      }
      const status = node('p', 'business-scope-form-status'); status.setAttribute('role', 'status'); form.append(status);
      const actions = node('div', 'business-scope-dialog-actions'), cancel = button('Cancel', () => dialog.close()), save = node('button', 'agent-primary', 'Save changes'); save.type = 'submit'; actions.append(cancel, save); form.append(actions); dialog.append(form); root.append(dialog);
      let pending = false, refreshAfterClose = false;
      dialog.addEventListener('cancel', event => { if (pending) event.preventDefault(); });
      dialog.addEventListener('close', () => { activeDialog = null; dialog.remove(); if (refreshAfterClose) draw(); if (opener?.isConnected) opener.focus(); }, { once: true });
      form.addEventListener('submit', async event => {
        event.preventDefault(); if (pending || !form.reportValidity()) return;
        const next = isMember ? { member_id: value.member_id } : value.id ? { id: value.id } : {};
        for (const [key, control] of Object.entries(controls)) next[key] = typeof control === 'function' ? control() : control.value.trim();
        if (isMember && next.role === 'function_admin' && !next.function_ids.length) { status.textContent = 'Choose a function for this function admin.'; return; }
        if (kind === 'cameras') next.source_ids = [...list(value.source_ids)];
        pending = true; [...form.elements].forEach(control => { control.disabled = true; }); status.textContent = 'Saving changes…';
        try {
          const refreshed = await onSave(kind, next, expectedRevision);
          if (!root.isConnected || !dialog.isConnected) return;
          if (refreshed && typeof refreshed === 'object' && Number.isInteger(refreshed.revision)) graph = refreshed;
          dialog.close(); draw();
        } catch (failure) { if (failure?.scopeGraph) { graph = failure.scopeGraph; refreshAfterClose = true; } if (dialog.isConnected) status.textContent = failure?.message || 'Refresh the locations and try saving again.'; }
        finally { pending = false; if (dialog.isConnected) [...form.elements].forEach(control => { control.disabled = false; }); }
      });
      dialog.showModal();
    }
    draw(); return root;
  }
  function renderSelector({ data = null, locations = [], loading = false, error = '', busy = false, readOnly = false, onSave, onOpen, onRetry } = {}, { node = make } = {}) {
    const root = node('div', 'chat-location-selector'), trigger = node('button', 'chat-location-trigger'); trigger.type = 'button'; trigger.setAttribute('aria-haspopup', 'dialog'); trigger.setAttribute('aria-expanded', 'false');
    const panel = node('div', 'chat-location-popover'); panel.id = 'chat-locations-' + (++serial); panel.setAttribute('popover', 'auto'); panel.setAttribute('role', 'dialog'); panel.setAttribute('aria-label', 'Chat locations'); trigger.setAttribute('aria-controls', panel.id); trigger.popoverTargetElement = panel;
    let current = data, available = list(locations), allMode = true, selected = new Set(), pending = false, epoch = 0, watching = null;
    const controls = node('div'), status = node('p', 'chat-location-status'), form = node('form'), title = node('h2', '', 'Chat locations'); status.setAttribute('role', 'status');
    const apply = node('button', 'agent-primary', 'Apply locations'); apply.type = 'submit'; const cancel = node('button', 'agent-secondary', 'Cancel'); cancel.type = 'button'; cancel.addEventListener('click', () => panel.hidePopover());
    const actions = node('div', 'chat-location-actions'); actions.append(cancel, apply); form.append(title, controls, status, actions); panel.append(form); root.append(trigger, panel);
    function isAll() { return current?.selected_location_ids === null || !Object.hasOwn(current || {}, 'selected_location_ids') && available.length === list(current?.location_ids).length && available.every(row => list(current?.location_ids).includes(row.id)); }
    function label() {
      const chosen = list(current?.location_ids); trigger.textContent = loading ? 'Loading locations…' : error ? 'Locations' : isAll() ? 'All locations' : chosen.length === 1 ? available.find(row => row.id === chosen[0])?.name || '1 location' : chosen.length + ' locations';
      trigger.setAttribute('aria-label', 'Chat locations: ' + trigger.textContent); trigger.disabled = loading || busy || readOnly || !current && !error; trigger.title = busy ? 'Available when this response finishes.' : readOnly ? 'Current chat locations' : 'Choose locations for this chat';
    }
    function drawChoices() {
      controls.replaceChildren(); selected = new Set(list(current?.location_ids)); allMode = isAll();
      const allLabel = node('label', 'chat-location-option'), all = node('input'); all.type = 'checkbox'; all.checked = allMode; allLabel.append(all, node('span', '', 'All permitted locations')); controls.append(allLabel);
      const rows = node('fieldset', 'chat-location-options'); rows.append(node('legend', '', 'Locations')); const boxes = [];
      for (const location of available) { const option = node('label', 'chat-location-option'), check = node('input'); check.type = 'checkbox'; check.value = location.id; check.checked = selected.has(location.id); option.append(check, node('span', '', location.name)); boxes.push(check); rows.append(option); check.addEventListener('change', () => { allMode = false; all.checked = false; check.checked ? selected.add(location.id) : selected.delete(location.id); apply.disabled = pending || !selected.size; }); }
      all.addEventListener('change', () => { allMode = all.checked; selected = new Set(allMode ? available.map(row => row.id) : []); boxes.forEach(check => { check.checked = selected.has(check.value); }); apply.disabled = pending || !allMode && !selected.size; });
      controls.append(rows); apply.disabled = pending || !current?.can_query || !available.length || !allMode && !selected.size;
      if (!available.length) status.textContent = 'Your permitted locations appear here after they are assigned.';
    }
    function position() {
      const rect = trigger.getBoundingClientRect(), width = Math.min(320, innerWidth - 32); panel.style.width = width + 'px'; panel.style.left = Math.max(16, Math.min(rect.left, innerWidth - width - 16)) + 'px'; panel.style.top = Math.max(16, rect.top - Math.min(panel.scrollHeight, innerHeight - 32) - 8) + 'px';
    }
    function stopWatching() { watching?.abort(); watching = null; }
    panel.addEventListener('beforetoggle', event => { if (event.newState === 'open') { pending = Boolean(onOpen); controls.replaceChildren(); apply.disabled = true; status.textContent = onOpen ? 'Loading current locations…' : ''; } else { epoch++; stopWatching(); } });
    panel.addEventListener('toggle', async event => {
      const open = event.newState === 'open'; trigger.setAttribute('aria-expanded', String(open)); if (!open) { epoch++; stopWatching(); return; }
      const requestEpoch = ++epoch; status.textContent = error || ''; controls.replaceChildren(); apply.disabled = true; position();
      stopWatching(); watching = new AbortController(); window.addEventListener('resize', position, { signal: watching.signal });
      if (onOpen) {
        pending = true; status.textContent = 'Loading current locations…';
        try { const fresh = await onOpen(); if (!root.isConnected || requestEpoch !== epoch || !panel.matches(':popover-open')) return; current = fresh.data; available = list(fresh.locations); error = ''; status.textContent = ''; }
        catch (failure) { if (root.isConnected && requestEpoch === epoch) { error = failure?.message || 'Refresh the locations to try again.'; status.textContent = error; } }
        finally { if (requestEpoch === epoch) pending = false; }
      }
      if (!root.isConnected || requestEpoch !== epoch || !panel.matches(':popover-open')) return;
      if (error) { if (onRetry) { const retry = node('button', 'agent-secondary', 'Refresh locations'); retry.type = 'button'; retry.addEventListener('click', async () => { panel.hidePopover(); await onRetry(); }); controls.append(retry); } }
      else drawChoices(); position();
    });
    form.addEventListener('submit', async event => {
      event.preventDefault(); if (pending || busy || readOnly || !current?.can_query || !allMode && !selected.size || typeof onSave !== 'function') return;
      const requestEpoch = epoch; pending = true; [...form.elements].forEach(el => { el.disabled = true; }); status.textContent = 'Updating chat locations…';
      try { const saved = await onSave(allMode ? null : [...selected], current.revision, current.selection_revision); if (!root.isConnected || requestEpoch !== epoch) return; current = saved; error = ''; label(); panel.hidePopover(); trigger.focus(); }
      catch (failure) { if (root.isConnected && requestEpoch === epoch) status.textContent = failure?.message || 'Refresh the locations and try again.'; }
      finally { pending = false; if (root.isConnected) [...form.elements].forEach(el => { el.disabled = false; }); }
    });
    // The host replaces this node on navigation. A pending response stays attached
    // to that original node and never updates another chat or account.
    const cleanup = new MutationObserver(() => { if (!root.isConnected) { epoch++; stopWatching(); cleanup.disconnect(); } }); queueMicrotask(() => { if (root.isConnected) cleanup.observe(document.body, { childList: true, subtree: true }); });
    label(); return root;
  }
  window.RocotoRetailScope = Object.freeze({ render, renderSelector });
  window.RocotoBusinessScope=window.RocotoRetailScope; // Retained browser integration name.
})();
