(() => {
  'use strict';

  // Render saved context without turning a citation into a verification claim.
  function render(data, { node, icon, safeUrl, sourceIcon }) {
    const profile = data.profile || {}, provenance = data.provenance || {};
    const card = node('section', 'business-summary');
    card.setAttribute('aria-label', 'Business summary');
    const heading = node('div', 'business-summary-heading');
    const mark = node('span', 'business-summary-mark');
    mark.append(icon('globe'));
    const identity = node('div', 'business-summary-identity');
    identity.append(node('h3', '', profile.name || 'Your business'));
    if (profile.type) identity.append(node('p', 'business-summary-type', profile.type));
    heading.append(mark, identity);
    card.append(heading);

    const status = provenance.epistemic_status === 'operator_provided' ? 'Provided by operator'
      : provenance.epistemic_status === 'agent_researched_unconfirmed' ? 'Research draft' : '';
    if (status) card.append(node('span', 'business-summary-status', status));
    if (profile.description) card.append(node('p', 'business-summary-description', profile.description));
    if (!profile.name && !profile.description) card.append(node('p', 'workspace-help', 'Business details appear here as you add them.'));

    function reference(value) {
      const href = safeUrl(value);
      if (!href) return null;
      const url = new URL(href);
      return url.username || url.password ? null : url;
    }
    function link(href, label, symbol, className = 'business-summary-action') {
      const anchor = node('a', className);
      anchor.href = href; anchor.target = '_blank'; anchor.rel = 'noopener noreferrer';
      anchor.append(symbol === 'globe' ? sourceIcon?.(href) || icon(symbol) : icon(symbol), node('span', '', label), icon('arrow'));
      return anchor;
    }
    const website = profile.website ? reference(profile.website) : null;
    if (website) {
      const row = node('div', 'business-summary-website');
      row.append(node('span', 'business-summary-label', 'Website'),
        link(website.href, website.hostname.replace(/^www\./, ''), 'globe'));
      card.append(row);
    }
    if (profile.location) {
      const location = node('div', 'business-summary-location');
      location.append(node('span', 'business-summary-label', 'Location'), node('p', '', profile.location));
      // A company can have several sites. This is a search, never an inferred site pin.
      if (profile.name) {
        const map = new URL('https://www.google.com/maps/search/');
        map.searchParams.set('api', '1'); map.searchParams.set('query', profile.name);
        location.append(link(map.href, 'Search Google Maps', 'search'));
      }
      card.append(location);
    }

    const references = [...new Set([profile.website, ...(profile.source_urls || [])].filter(Boolean))]
      .map(reference).filter(Boolean);
    if (references.length) {
      const sources = node('details', 'business-summary-sources');
      const summary = node('summary');
      summary.append(icon('document'), node('span', '', `${references.length} ${references.length === 1 ? 'source' : 'sources'}`), icon('chevron'));
      sources.append(summary);
      const list = node('ul');
      for (const url of references) {
        const item = node('li');
        item.append(link(url.href, url.hostname.replace(/^www\./, '') + (url.pathname === '/' ? '' : url.pathname), 'globe', 'source-reference'));
        list.append(item);
      }
      sources.append(list); card.append(sources);
    }
    const recorded = provenance.recorded_at && new Date(provenance.recorded_at);
    if (recorded && Number.isFinite(recorded.getTime())) {
      const stamp = node('time', 'business-summary-updated', `Profile updated ${recorded.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })}`);
      stamp.dateTime = recorded.toISOString(); card.append(stamp);
    }
    return card;
  }

  // Stats owns the surrounding page and its edit/research controls. Reuse the
  // inline summary's safe links and provenance, with a separate profile layout.
  function renderProfile(data, helpers) {
    const { node } = helpers, profile = data.profile || {};
    const card = render(data, helpers);
    card.classList.add('business-profile');
    card.setAttribute('aria-label', 'Business profile');
    const cover = node('div', 'business-profile-cover'); cover.setAttribute('aria-hidden', 'true');
    if (/^\/api\/profile-covers\/cover-[a-f0-9]{32}$/.test(data.cover?.url || '') && data.cover?.target === 'business') {
      const artwork = node('img', 'business-profile-cover-image'); artwork.alt = ''; artwork.src = data.cover.url;
      artwork.addEventListener('error', () => artwork.remove(), { once: true }); cover.append(artwork);
    }
    const heading = card.querySelector('.business-summary-heading');
    const identity = card.querySelector('.business-summary-identity');
    const mark = card.querySelector('.business-summary-mark');
    mark.classList.add('business-profile-avatar');
    mark.textContent = (profile.name || 'Business').trim().split(/\s+/).slice(0, 2)
      .map(word => Array.from(word)[0] || '').join('').toLocaleUpperCase();
    mark.setAttribute('aria-hidden', 'true');
    const status = card.querySelector('.business-summary-status');
    if (status) identity.append(status);
    const actions = node('div', 'business-profile-actions');
    heading.append(actions);

    const contact = node('div', 'business-profile-contact');
    for (const selector of ['.business-summary-website', '.business-summary-location']) {
      const row = card.querySelector(selector);
      if (row) contact.append(row);
    }
    const content = node('div', 'business-profile-content');
    const description = card.querySelector('.business-summary-description');
    if (description) content.append(description);
    const empty = card.querySelector('.workspace-help');
    if (empty) content.append(empty);
    function addList(title, values, className) {
      const items = (Array.isArray(values) ? values : []).filter(value => typeof value === 'string' && value.trim()).slice(0, 32);
      if (!items.length) return;
      const section = node('section', `business-profile-section ${className}`);
      section.append(node('h4', '', title));
      const list = node('ul');
      for (const value of items) list.append(node('li', '', value));
      section.append(list); content.append(section);
    }
    addList('Operations', profile.operations, 'business-profile-operations');
    addList('Assumptions', profile.assumptions, 'business-profile-assumptions');

    const provenance = node('div', 'business-profile-provenance');
    for (const selector of ['.business-summary-sources', '.business-summary-updated']) {
      const item = card.querySelector(selector);
      if (item) provenance.append(item);
    }
    card.replaceChildren(cover, heading, contact, content, provenance);
    return card;
  }

  window.RocotoBusinessSummary = Object.freeze({ render, renderProfile });
})();
