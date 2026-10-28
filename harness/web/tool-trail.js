/* Completed native activity. Live tool rendering remains owned by app.js. */
(() => {
  'use strict';
  const categories = [
    { names: ['edit_files'], label: 'Edited files', icon: 'edit' },
    { names: ['read_files', 'read_file', 'read_file_pages'], label: 'Read files', icon: 'document' },
    { names: ['list_files'], label: 'Listed files', icon: 'folder' },
    { names: ['search_files'], label: 'Searched files', icon: 'search' },
    { names: ['run_command'], label: 'Ran commands', single: 'Ran a command', icon: 'terminal' },
    { names: ['web_search'], label: 'Searched the web', icon: 'globe' },
    { names: ['open_web_page'], label: 'Browsed the web', icon: 'globe' },
    { names: ['find_on_page'], label: 'Searched a web page', icon: 'search' },
  ];
  const lifecycle = /^(compact_context|spawn_agent|delegate_investigation|plan_investigation)$/;
  const category = tool => tool.status === 'complete' || tool.success === true
    ? categories.find(item => item.names.includes(tool.name)) : null;
  const text = (value, limit = 2048) => typeof value === 'string' ? value.slice(0, limit) : '';
  const inspectionPreviewPath = /^\/api\/inspection-previews\/[a-f0-9]{32}\/[0-3]\.jpg$/;
  function normalizeFramePreviews(value) {
    if(!Array.isArray(value)||value.length>4)return [];
    const seen=new Set(),frames=[];
    for(const item of value){
      if(!item||typeof item!=='object'||Array.isArray(item)||typeof item.url!=='string'||!inspectionPreviewPath.test(item.url)||seen.has(item.url))continue;
      if(typeof item.time!=='number'||!Number.isFinite(item.time)||item.time<0||item.time>1e12||typeof item.clip_id!=='string'||!/^[A-Za-z0-9_.:-]{1,256}$/.test(item.clip_id)||['.','..'].includes(item.clip_id)||typeof item.sha256!=='string'||!/^[a-f0-9]{64}$/.test(item.sha256))continue;
      if(item.basis!=null&&(typeof item.basis!=='string'||!Array.from(item.basis).length||Array.from(item.basis).length>128||Array.from(item.basis).some(char=>{const code=char.codePointAt(0);return code<32||code===127||(code>=0xD800&&code<=0xDFFF);})))continue;
      if(['width','height'].some(key=>item[key]!=null&&(!Number.isSafeInteger(item[key])||item[key]<1||item[key]>65535)))continue;
      const frame={url:item.url,time:item.time,clip_id:item.clip_id,sha256:item.sha256};
      for(const key of ['basis','width','height'])if(item[key]!=null)frame[key]=item[key];
      frames.push(frame);seen.add(item.url);
    }
    return frames;
  }
  function argumentsOf(tool) {
    if (tool.arguments && typeof tool.arguments === 'object' && !Array.isArray(tool.arguments)) return tool.arguments;
    if (typeof tool.arguments === 'string' && tool.arguments.length <= 65536) {
      try { const value = JSON.parse(tool.arguments); return value && typeof value === 'object' && !Array.isArray(value) ? value : {}; } catch {}
    }
    return {};
  }
  function references(tool, args) {
    const found = [];
    const add = (kind, id, path) => {
      id = text(id, 512) || null;
      path = text(path) || null;
      if ((id || path) && !found.some(item => item.kind === kind && item.id === id && item.path === path)) found.push({ tool, kind, id, path });
    };
    if (args.file_id) add('file', args.file_id, null);
    if (args.clip_id || args.asset_id) add('source', args.clip_id || args.asset_id, null);
    if (/^(read_files|read_file|read_file_pages|edit_files|search_files|list_files)$/.test(tool.name)) {
      add('file', null, args.path);
      for (const path of (Array.isArray(args.paths) ? args.paths : []).slice(0, 8)) add('file', null, path);
      for (const action of (Array.isArray(args.actions) ? args.actions : []).slice(0, 8)) {
        if (action && typeof action === 'object') add('file', null, action.path);
      }
    }
    return found.slice(0, 8);
  }
  function safeThumbnail(value, doc) {
    if (!text(value) || /[\\\s\x00-\x1f\x7f]/.test(value)) return null;
    try {
      const base = new URL(doc.baseURI), url = new URL(value, base);
      return /^(https?:)$/.test(url.protocol) && url.origin === base.origin && !url.username && !url.password
        && (/^\/(thumbnails|attachments|api\/files|api\/assets)\//.test(url.pathname) || inspectionPreviewPath.test(value) && !url.search && !url.hash) ? url.href : null;
    } catch { return null; }
  }
  function measure(viewport) {
    viewport.parentElement.classList.toggle('tool-trail-overflow', viewport.clientHeight > 0
      && viewport.scrollTop + viewport.clientHeight < viewport.scrollHeight - 1);
  }
  function refresh(root) {
    for (const viewport of root.querySelectorAll('.tool-trail-viewport')) measure(viewport);
  }
  // One document-level listener also catches an outer restored "Worked" disclosure.
  document.addEventListener('toggle', event => {
    if (event.target instanceof Element && event.target.matches('details')) {
      requestAnimationFrame(() => {
        refresh(event.target);
        const viewport = event.target.closest('.tool-trail-viewport');
        if (viewport) measure(viewport);
      });
    }
  }, true);
  window.addEventListener('resize', () => refresh(document));

  function render(tools, { pending = false, renderTool, icon, resolveReference, resolveMedia } = {}) {
    if (typeof renderTool !== 'function' || typeof icon !== 'function') throw new TypeError('Provide the existing tool renderer and icons.');
    const doc = document;
    const make = (tag, className, value) => {
      const element = doc.createElement(tag);
      element.className = className;
      if (value) element.textContent = value;
      return element;
    };
    const list = make('div', 'tool-list completed-activity tool-trail');
    const ownedReference = ref => {
      let resolved;
      try { resolved = resolveReference?.(ref); } catch {}
      if (resolved && text(resolved.label, 512) && typeof resolved.open === 'function') {
        const button = make('button', 'tool-trail-reference', text(resolved.label, 512));
        button.type = 'button';
        button.title = ref.path || resolved.label;
        button.addEventListener('click', event => { event.preventDefault(); event.stopPropagation(); resolved.open(button); });
        return button;
      }
      if (!ref.path) return null;
      const label = ref.path.split(/[\\/]/).filter(Boolean).at(-1) || ref.path;
      const value = make('span', 'tool-trail-path', label);
      value.title = ref.path;
      return value;
    };
    function individual(tool, index) {
      const args = argumentsOf(tool);
      const command = tool.name === 'run_command' ? text(args.command, 8192) : '';
      const options = { index };
      if (command) options.label = `Ran ${command.replace(/\s+/g, ' ')}`;
      const action = Array.isArray(args.actions) ? args.actions.find(item => item && item.type === 'search') : null;
      const query = text(args.query || args.pattern || action?.query, 2048);
      if (query && /^(search_files|web_search|find_on_page)$/.test(tool.name)) options.label = `Searched for ${query.replace(/\s+/g, ' ')}`;
      const detail = renderTool(tool, options);
      detail.classList.add('tool-trail-item');
      const heading = detail.querySelector(':scope > summary');
      if (heading) {
        const label = heading.querySelector('span');
        if (label) label.classList.add('tool-trail-label');
        if (command) heading.title = command;
        else if (query) heading.title = query;
        const refs = references(tool, args).map(ownedReference).filter(Boolean);
        if (label && refs.length) {
          const prefix = /^(read_files|read_file|read_file_pages)$/.test(tool.name) ? 'Read '
            : tool.name === 'edit_files' ? 'Edited ' : null;
          if (prefix) label.replaceChildren(doc.createTextNode(prefix));
          else label.append(doc.createTextNode(' · '));
          refs.forEach((ref, position) => { if (position) label.append(doc.createTextNode(', ')); label.append(ref); });
        }
      }
      let media;
      try { media = resolveMedia?.(tool); } catch {}
      if (Array.isArray(media)) {
        const row = make('div', 'tool-trail-media tool-media-strip');
        const frames=/^(?:mcp__rocoto__)?inspect_clip$/.test(tool.name)?normalizeFramePreviews(tool.frame_previews):[],sampled=new Set(frames.map(frame=>new URL(frame.url,doc.baseURI).href));
        row.setAttribute('aria-label', sampled.size ? 'Inspected frames' : 'Tool source previews');
        for (const item of media.slice(0, 12)) {
          const url = item && safeThumbnail(item.thumbnailUrl, doc);
          if (!url || (inspectionPreviewPath.test(new URL(url).pathname)&&!sampled.has(url))) continue;
          const card = make(typeof item.open === 'function' ? 'button' : 'figure', 'tool-trail-media-item');
          const image = make('img', '');
          image.src = url;
          image.alt = text(item.label, 512) || 'Source preview';
          image.loading = 'lazy';
          image.referrerPolicy = 'no-referrer';
          if(sampled.has(url)){row.classList.add('tool-trail-inspection-frames');image.style.objectFit='contain';card.title=image.alt;}
          image.addEventListener('load', () => { const viewport = detail.closest('.tool-trail-viewport'); if (viewport) measure(viewport); });
          card.append(image);
          if (item.label) card.append(make('span', '', text(item.caption || item.label, 512)));
          if (typeof item.open === 'function') { card.type = 'button'; card.setAttribute('aria-label',image.alt);card.addEventListener('click', () => item.open(card)); }
          row.append(card);
        }
        if (row.childElementCount) detail.insertBefore(row, heading?.nextSibling || null);
      }
      return detail;
    }
    function group(items) {
      if (items.length === 1) return individual(items[0].tool, items[0].index);
      const detail = make('details', 'tool-trail-group');
      const summary = make('summary', 'activity-row');
      const used = categories.filter(item => items.some(entry => category(entry.tool) === item));
      const labels = used.map(item => {
        const count = items.filter(entry => category(entry.tool) === item).length;
        return count === 1 && item.single ? item.single : item.label;
      });
      const label = labels.map((value, index) => index ? value[0].toLowerCase() + value.slice(1) : value).join(', ');
      const anchor = items[0];
      detail.dataset.detailKey = `tool-group:${anchor.tool.id || `${anchor.index}:${anchor.tool.name}`}`;
      summary.append(icon(used[0].icon), make('span', 'tool-trail-label', label), icon('chevron'));
      summary.setAttribute('aria-label', `${label} · Details`);
      const wrap = make('div', 'tool-trail-scroll-wrap');
      const viewport = make('div', 'tool-trail-viewport');
      viewport.tabIndex = 0;
      viewport.setAttribute('role', 'region');
      viewport.setAttribute('aria-label', 'Completed tool details');
      for (const { tool, index } of items) viewport.append(individual(tool, index));
      viewport.addEventListener('scroll', () => measure(viewport), { passive: true });
      wrap.append(viewport);
      detail.append(summary, wrap);
      return detail;
    }
    let buffered = [];
    const flush = () => { if (buffered.length) list.append(group(buffered)); buffered = []; };
    for (const [index, tool] of (Array.isArray(tools) ? tools : []).entries()) {
      if (!tool || typeof tool !== 'object') continue;
      if (!pending && category(tool)) buffered.push({ tool, index });
      else {
        flush();
        const detail = individual(tool, index);
        detail.hidden = pending && !lifecycle.test(tool.name) && !detail.querySelector('.tool-trail-inspection-frames');
        list.append(detail);
      }
    }
    flush();
    list.hidden = ![...list.children].some(child => !child.hidden);
    requestAnimationFrame(() => refresh(list));
    return list;
  }
  window.RocotoToolTrail = Object.freeze({ render, normalizeFramePreviews });
})();
