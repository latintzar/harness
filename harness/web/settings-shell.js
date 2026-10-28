/* Existing settings navigation and local filtering; section actions stay with app.js. */
(() => {
  'use strict';
  const mounted = new WeakMap();
  const groups = [
    { label: 'Account', items: [
      ['profile', 'Profile', 'profile'],
      ['notifications', 'Notifications', 'bell'],
    ] },
    { label: 'Workspace', items: [
      ['appearance', 'Appearance', 'sun'],
      ['archives', 'Archived chats', 'archive'],
    ] },
    { label: 'Organization', items: [
      ['business', 'Your Business', 'building'],
      ['stats', 'Stats', 'chart'],
      ['team', 'Team', 'people'],
      ['messaging', 'Messaging', 'chat'],
      ['usage', 'Usage', 'chart'],
      ['billing', 'Billing', 'card'],
    ] },
    { label: 'Integrations', items: [
      ['plugins', 'Plugins', 'at'],
      ['mcp', 'Rocoto MCP', 'link'],
    ] },
  ];

  function mount({ node, icon }) {
    if (!node) throw new TypeError('Choose the settings navigation element.');
    if (mounted.has(node)) return mounted.get(node);
    const doc = node.ownerDocument;
    const make = (tag, className, text) => {
      const element = doc.createElement(tag);
      element.className = className;
      if (text) element.textContent = text;
      return element;
    };
    const heading = make('h2', 'settings-shell-title', 'Settings');
    const searchLabel = make('label', 'settings-shell-search');
    if (icon) searchLabel.append(icon('search'));
    const search = make('input', 'settings-shell-input');
    search.type = 'search';
    search.id = 'settings-search';
    search.placeholder = 'Search';
    search.autocomplete = 'off';
    search.spellcheck = false;
    search.setAttribute('aria-label', 'Search settings');
    searchLabel.append(search);
    const sections = make('div', 'settings-shell-groups');
    const contributed = [...node.querySelectorAll('[data-settings-group]')];
    const entries = groups.map(({ label, items }, groupIndex) => {
      items = [...items];
      for (const option of contributed.filter(item => item.dataset.settingsGroup === label)) {
        const entry = [option.id.replace(/^settings-/, ''), option.textContent.trim(), option.dataset.settingsIcon];
        const after = items.findIndex(([key]) => key === option.dataset.settingsAfter);
        items.splice(after < 0 ? items.length : after + 1, 0, entry);
      }
      const section = make('section', 'settings-shell-group');
      const title = make('h3', 'settings-shell-group-title', label);
      title.id = `settings-group-${groupIndex}`;
      section.setAttribute('aria-labelledby', title.id);
      const options = make('div', 'settings-shell-options');
      const buttons = items.map(([key, text, symbol]) => {
        // Reparent existing nodes so saved selection and installed listeners survive.
        const button = doc.getElementById(`settings-${key}`) || make('button', '', text);
        button.id = `settings-${key}`;
        button.type = 'button';
        button.classList.add('settings-shell-option');
        if (!button.querySelector('svg') && icon) button.prepend(icon(symbol));
        options.append(button);
        return { button, text };
      });
      section.append(title, options);
      sections.append(section);
      return { section, buttons };
    });
    const empty = make('p', 'settings-shell-empty', 'Try another setting.');
    empty.setAttribute('role', 'status');
    empty.hidden = true;
    search.setAttribute('aria-controls', 'settings-sections');
    sections.id = 'settings-sections';
    node.classList.add('settings-shell');
    node.replaceChildren(heading, searchLabel, sections, empty);

    function filter() {
      const query = search.value.trim().toLocaleLowerCase();
      let total = 0;
      for (const { section, buttons } of entries) {
        let visible = 0;
        for (const { button, text } of buttons) {
          button.hidden = button.dataset.settingsAvailable === 'false' || !text.toLocaleLowerCase().includes(query);
          if (!button.hidden) visible++;
        }
        section.hidden = visible === 0;
        total += visible;
      }
      empty.hidden = total > 0;
    }
    search.addEventListener('input', filter);
    search.addEventListener('search', filter);
    let active = false;
    let entry = 0;
    let pendingFocus = null;
    let focusQueued = false;
    const controller = {
      sync(next) {
        filter();
        next = Boolean(next);
        node.hidden = !next;
        if (next !== active) {
          active = next;
          ++entry;
          pendingFocus = active ? { entry, priorFocus: doc.activeElement } : null;
        }
        if (!pendingFocus || focusQueued) return;
        focusQueued = true;
        queueMicrotask(() => {
          focusQueued = false;
          if (!pendingFocus || !active || pendingFocus.entry !== entry) return;
          if (doc.activeElement !== pendingFocus.priorFocus || doc.querySelector('dialog[open]')) {
            pendingFocus = null;
            return;
          }
          // Session restoration can render this entry before its workspace is visible.
          // Retain only that initial focus request until a later visible sync.
          if (node.hidden || !node.isConnected || !search.getClientRects().length
              || doc.defaultView.getComputedStyle(search).visibility !== 'visible') return;
          search.focus({ preventScroll: true });
          pendingFocus = null;
        });
      },
    };
    mounted.set(node, controller);
    return controller;
  }

  window.RocotoSettingsShell = Object.freeze({ mount });
})();
