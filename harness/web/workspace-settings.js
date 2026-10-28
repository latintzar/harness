/* Personal browser preferences and truthful organisation administration views. */
(() => {
  'use strict';
  const defaults = Object.freeze({ uiSize: 14, codeSize: 13, motion: 'system', desktopNotifications: false });
  function normalize(value = {}) {
    return { uiSize: Number.isInteger(value.uiSize) && value.uiSize >= 12 && value.uiSize <= 20 ? value.uiSize : 14,
      codeSize: Number.isInteger(value.codeSize) && value.codeSize >= 11 && value.codeSize <= 20 ? value.codeSize : 13,
      motion: ['system', 'reduce'].includes(value.motion) ? value.motion : 'system', desktopNotifications: value.desktopNotifications === true };
  }
  function key(identity) { return typeof identity === 'string' && identity.length > 0 && identity.length <= 300 ? 'rocoto-settings-v1:' + identity : null; }
  function read(identity) { try { return normalize(JSON.parse(localStorage.getItem(key(identity))) || {}); } catch { return normalize(); } }
  function write(identity, preferences) {
    const storageKey = key(identity), value = normalize(preferences);
    if (!storageKey) return false;
    try { localStorage.setItem(storageKey, JSON.stringify(value)); return true; } catch { return false; }
  }
  function apply(preferences = defaults) {
    const value = normalize(preferences);
    document.documentElement.style.setProperty('--font-size-ui', value.uiSize + 'px');
    document.documentElement.style.setProperty('--font-size-code', value.codeSize + 'px');
    document.documentElement.dataset.reduceMotion = String(value.motion === 'reduce');
  }
  function permission() { return 'Notification' in window && window.isSecureContext ? Notification.permission : 'unavailable'; }
  async function requestPermission() { return permission() === 'unavailable' ? 'unavailable' : Notification.requestPermission(); }
  function notify(preferences, title, onOpen) {
    if (!normalize(preferences).desktopNotifications || permission() !== 'granted' || !document.hidden) return false;
    try {
      const notification = new Notification('Rocoto', { body: 'Your task is finished.', tag: typeof title === 'string' ? title.slice(0, 120) : 'rocoto-task' });
      notification.onclick = () => { window.focus(); notification.close(); onOpen?.(); };
      return true;
    } catch { return false; }
  }

  function render(view, data = {}, { node, icon, onChange, onPermission, onUsage, onMember } = {}) {
    const root = node('section', 'workspace-settings-page');
    const titles = { appearance: 'Appearance', notifications: 'Notifications', team: 'Team', billing: 'Billing' };
    root.append(node('h1', '', titles[view] || 'Settings'));
    const button = (label, action, primary = false, symbol = 'settings') => { const el = node('button', primary ? 'agent-primary' : 'agent-secondary', label); el.type = 'button'; if (icon) el.prepend(icon(symbol)); el.addEventListener('click', action); return el; };
    const card = () => node('section', 'workspace-settings-card');
    const row = (label, description, control) => {
      const el = node('div', 'workspace-settings-row'), text = node('div', 'workspace-settings-copy'); text.append(node('span', '', label));
      if (description) text.append(node('small', '', description)); el.append(text, control); return el;
    };
    if (view === 'appearance') {
      const preferences = normalize(data.preferences);
      root.append(node('h2', '', 'Visual style'));
      const mode = card(), choice = node('div', 'workspace-light-mode'); choice.append(icon('sun'), node('span', '', 'Light'), icon('check'));
      mode.append(row('Mode', '', choice)); root.append(mode);
      const theme = card(); theme.append(row('Theme', '', node('span', 'workspace-setting-value', 'Rocoto')), row('Font', 'Uses your device’s system font.', node('span', 'workspace-setting-value', 'System'))); root.append(theme);
      root.append(node('h2', '', 'Font and motion'));
      const advanced = card();
      for (const [field, label, description, min] of [['uiSize', 'UI font size', 'Base size across the workspace.', 12], ['codeSize', 'Code font size', 'Code blocks and source previews.', 11]]) {
        const control = node('label', 'workspace-number-control'), input = node('input'); input.type = 'number'; input.min = String(min); input.max = '20'; input.step = '1'; input.value = String(preferences[field]); input.setAttribute('aria-label', label);
        input.addEventListener('change', () => { const next = Number(input.value); if (!Number.isInteger(next) || next < min || next > 20) { input.value = String(preferences[field]); return; } preferences[field] = next; onChange?.({ ...preferences }); }); control.append(input, node('span', '', 'px')); advanced.append(row(label, description, control));
      }
      const motion = node('select'); motion.setAttribute('aria-label', 'Reduce motion');
      for (const [value, label] of [['system', 'Match system'], ['reduce', 'Reduce']]) { const option = node('option', '', label); option.value = value; motion.append(option); } motion.value = preferences.motion;
      motion.addEventListener('change', () => { preferences.motion = motion.value; onChange?.({ ...preferences }); }); advanced.append(row('Reduce motion', 'Animation preference for this browser.', motion)); root.append(advanced);
      root.append(button('Reset to default', () => onChange?.({ ...defaults, desktopNotifications: preferences.desktopNotifications }), false, 'refresh'));
      root.append(node('p', 'workspace-settings-note', 'Saved for your account in this browser.'));
    } else if (view === 'notifications') {
      const preferences = normalize(data.preferences), state = permission(), group = card();
      const control = node('input'); control.type = 'checkbox'; control.setAttribute('role', 'switch'); control.setAttribute('aria-label', 'Desktop task completion'); control.checked = preferences.desktopNotifications && state === 'granted'; control.disabled = state !== 'granted';
      control.addEventListener('change', () => onChange?.({ ...preferences, desktopNotifications: control.checked }));
      group.append(row('Desktop task completion', 'Notify you when an active task finishes while this tab is in the background.', control)); root.append(group);
      if (state === 'default') root.append(button('Enable desktop notifications', () => onPermission?.(), true, 'bell'));
      const note = { default: 'Allow notifications in your browser to enable this preference.', denied: 'Allow notifications for this site in your browser settings.', unavailable: 'Desktop notifications require a supported browser on HTTPS.', granted: 'Notifications show a generic completion message. Open Rocoto to read the result.' }[state];
      root.append(node('p', 'workspace-settings-note', note));
      root.append(node('h2', '', 'Alerts on your phone'), node('p', 'workspace-settings-note', 'Each Tracking Agent sends its alerts to its participants’ verified phones. Open a Tracking Agent’s info to manage its participants, channels and switch.'));
    } else if (view === 'team') {
      root.append(node('p', 'workspace-settings-note', 'People and their access to this organization.'));
      if (data.error || data.loading) { root.append(node('p', 'workspace-settings-note', data.error || 'Loading team…')); return root; }
      const list = node('div', 'workspace-team-list');
      for (const person of data.members || []) {
        const item = node('button', 'workspace-team-row'), name = person.display_name || 'Team member', avatar = node('span', 'workspace-team-avatar', name.trim().split(/\s+/).slice(0, 2).map(part => [...part][0]).join('').toLocaleUpperCase()); avatar.setAttribute('aria-hidden', 'true');
        const copy = node('div', 'workspace-settings-copy'); copy.append(node('strong', '', name));
        const role = { owner: 'Organization admin', operator: 'Member', observer: 'Read-only member' }[person.role] || 'Member';
        item.type='button';item.addEventListener('click',()=>onMember?.(person));item.append(avatar, copy, node('span', 'workspace-team-role', role)); list.append(item);
      }
      if (!list.childNodes.length) list.append(node('p', 'workspace-settings-note', 'Your organization’s members appear here.'));
      root.append(list);
    } else if (view === 'billing') {
      const group = card(); group.append(row('Customer payment setup', 'Subscriptions, purchased credits and automatic reload.', node('span', 'workspace-setting-value', 'Being prepared'))); root.append(group);
      root.append(node('p', 'workspace-settings-note', data.billing?.status === 'prepaid_api' ? 'This preview uses prepaid provider funding. Usage shows its measured spend and remaining funds.' : 'Usage shows the current account’s available funding and limits.'));
      if (onUsage) root.append(button('View usage', onUsage, false, 'chart'));
    }
    return root;
  }
  window.RocotoWorkspaceSettings = Object.freeze({ defaults, read, write, apply, permission, requestPermission, notify, render });
})();
