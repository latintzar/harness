/* Display existing scoped records; application callbacks own every account action. */
(() => {
  'use strict';
  let sequence = 0;
  const nodeDefault = (tag, className = '', text) => { const el = document.createElement(tag); el.className = className; if (text !== undefined) el.textContent = text; return el; };
  const kinds = [['plugins', 'Plugins'], ['apps', 'Apps'], ['mcps', 'MCPs'], ['marketplaces', 'Marketplace']];
  const states = { connected: 'Connected', disconnected: 'Account setup', setup_required: 'Setup required', cleanup_pending: 'Removal pending', installed: 'Installed', awaiting_authorization: 'Awaiting authorisation', failed: 'Review setup', connecting: 'Connecting' };

  function render(data = {}, { node = nodeDefault, icon, logo, onOpen, onBrowse, onAdd } = {}) {
    const root = node('section', 'plugin-settings'), id = `plugin-settings-${++sequence}`;
    const heading = node('header', 'plugin-settings-heading'), title = node('div');
    title.append(node('h1', '', 'Plugins'), node('p', '', 'Manage plugins, skills, and MCPs'));
    const actions = node('div', 'plugin-settings-heading-actions');
    function action(label, callback, primary = false) {
      const button = node('button', primary ? 'plugin-settings-primary' : 'plugin-settings-secondary', label); button.type = 'button';
      button.disabled = typeof callback !== 'function'; button.addEventListener('click', () => callback?.()); return button;
    }
    actions.append(action('Browse directory', onBrowse));
    const add = action('Add', onAdd || onBrowse, true); add.disabled ||= data.can_manage !== true;
    if (icon) add.append(icon('plus')); actions.append(add); heading.append(title, actions);
    const tools = node('div', 'plugin-settings-tools'), tabs = node('div', 'plugin-settings-tabs'); tabs.setAttribute('role', 'tablist'); tabs.setAttribute('aria-label', 'Plugin types');
    const label = node('label', 'plugin-settings-search'); if (icon) label.append(icon('search'));
    const search = node('input'); search.type = 'search'; search.placeholder = 'Search plugins'; search.autocomplete = 'off'; search.setAttribute('aria-label', 'Search plugins'); label.append(search);
    const panel = node('div', 'plugin-settings-list'); panel.id = id + '-panel'; panel.setAttribute('role', 'tabpanel');
    const collections = Object.fromEntries(kinds.map(([kind]) => [kind, (Array.isArray(data[kind]) ? data[kind] : []).filter(entry => entry && typeof entry.id === 'string').slice(0, 500)]));
    let selected = 'plugins'; const buttons = new Map();
    function show(kind, focus = false) {
      selected = kind;
      for (const [key, button] of buttons) { button.setAttribute('aria-selected', String(key === kind)); button.tabIndex = key === kind ? 0 : -1; }
      panel.setAttribute('aria-labelledby', id + '-' + kind); panel.replaceChildren();
      const query = search.value.trim().toLocaleLowerCase();
      const rows = collections[kind].filter(entry => [entry.name, entry.id, entry.description, entry.developer].filter(Boolean).join(' ').toLocaleLowerCase().includes(query));
      if (data.error) { const error = node('p', 'plugin-settings-message', data.error); error.setAttribute('role', 'status'); panel.append(error); }
      if (data.loading && !rows.length) panel.append(node('p', 'plugin-settings-message', 'Loading plugins…'));
      else if (!rows.length) panel.append(node('p', 'plugin-settings-message', query ? 'Try another plugin name.' : ({plugins: 'Your installed plugins appear here.', apps: 'Your connected apps appear here.', mcps: 'Your MCP connections appear here.', marketplaces: 'Browse the directory to discover plugins.'})[kind]));
      for (const entry of rows) {
        const row = node('div', 'plugin-settings-row'); row.dataset.pluginId = entry.id;
        const main = node('button', 'plugin-settings-entry'); main.type = 'button'; main.disabled = typeof onOpen !== 'function';
        const mark = node('span', 'plugin-settings-logo');
        if (logo) { const image = logo(entry); if (image) mark.append(image); }
        if (!mark.childNodes.length) mark.textContent = (entry.name || entry.id).slice(0, 1).toLocaleUpperCase();
        const copy = node('span', 'plugin-settings-copy'); copy.append(node('strong', '', entry.name || entry.id));
        const description = entry.description || entry.developer || (entry.version ? `Version ${entry.version}` : ''); if (description) copy.append(node('span', '', description));
        main.append(mark, copy); main.addEventListener('click', () => onOpen?.(entry.id, kind));
        const detail = node('div', 'plugin-settings-row-actions');
        const status = entry.cleanup_pending ? 'Removal pending' : entry.installed === true ? 'Installed' : states[entry.status] || '';
        if (status) { const state = node('span', 'plugin-settings-state', status); state.title = status; detail.append(state); }
        const manage = action(data.can_manage === true ? 'Manage' : 'View', () => onOpen?.(entry.id, kind)); manage.disabled = typeof onOpen !== 'function'; manage.setAttribute('aria-label', `${data.can_manage === true ? 'Manage' : 'View'} ${entry.name || entry.id}`); detail.append(manage);
        row.append(main, detail); panel.append(row);
      }
      if (focus) buttons.get(kind).focus();
    }
    for (const [kind, name] of kinds) {
      const button = node('button', 'plugin-settings-tab'); button.type = 'button'; button.id = id + '-' + kind; button.setAttribute('role', 'tab'); button.setAttribute('aria-controls', panel.id);
      button.append(node('span', '', name), node('span', 'plugin-settings-count', String(collections[kind].length)));
      button.addEventListener('click', () => show(kind)); button.addEventListener('keydown', event => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return; event.preventDefault();
        const at = kinds.findIndex(([key]) => key === selected), next = event.key === 'Home' ? 0 : event.key === 'End' ? kinds.length - 1 : (at + (event.key === 'ArrowRight' ? 1 : -1) + kinds.length) % kinds.length;
        show(kinds[next][0], true);
      }); buttons.set(kind, button); tabs.append(button);
    }
    search.addEventListener('input', () => show(selected)); search.addEventListener('search', () => show(selected));
    tools.append(tabs, label); root.append(heading, tools, panel); show(selected); return root;
  }
  window.RocotoPluginSettings = Object.freeze({ render });
})();
