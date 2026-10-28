(() => {
  'use strict';
  const loaded = new Map(), configured = new WeakSet();
  const LANGUAGES = new Set('bash sh shell c cpp c++ csharp cs css diff go ini java javascript js json kotlin less lua makefile markdown md objectivec perl php plaintext text python py r ruby rb rust rs scss sql swift typescript ts vbnet wasm xml html yaml yml'.split(' '));
  const DISPLAY_LANGUAGES = { javascript: 'JavaScript', js: 'JavaScript', typescript: 'TypeScript', ts: 'TypeScript', python: 'Python', py: 'Python', bash: 'Bash', sh: 'Shell', shell: 'Shell', c: 'C', cpp: 'C++', 'c++': 'C++', cs: 'C#', csharp: 'C#', css: 'CSS', html: 'HTML', xml: 'XML', json: 'JSON', sql: 'SQL', yaml: 'YAML', yml: 'YAML', md: 'Markdown', markdown: 'Markdown', objectivec: 'Objective-C', plaintext: 'Plain text', text: 'Plain text', vbnet: 'Visual Basic', wasm: 'WebAssembly' };
  const MATH = new Set(['math', 'latex', 'tex']);
  const MAX_CODE = 65536, MAX_MATH = 4000, MAX_DIAGRAM = 12000, MAX_BLOCKS = 48;
  const esc = value => value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  const defaultNode = (tag, className, text) => { const el = document.createElement(tag); if (className) el.className = className; if (text !== undefined) el.textContent = text; return el; };

  // Math is tokenized before Markdown can consume its backslash delimiters. The
  // extension emits escaped code only; the normal Markdown sanitizer still runs.
  function configure(marked) {
    if (!marked || configured.has(marked)) return;
    configured.add(marked);
    marked.use({ extensions: [{ name: 'rocotoBlockMath', level: 'block',
      start(src) { return src.search(/(?:^|\n)(?:\$\$|\\\[)/); },
      tokenizer(src) {
        const match = /^(?:\$\$([\s\S]*?)\$\$|\\\[([\s\S]*?)\\\])(?:\n|$)/.exec(src);
        if (match) return { type: 'rocotoBlockMath', raw: match[0], text: match[1] ?? match[2] };
      },
      renderer(token) { return `<pre><code class="language-math">${esc(token.text)}</code></pre>`; }
    }, { name: 'rocotoInlineMath', level: 'inline',
      start(src) { const index = src.indexOf('\\('); return index < 0 ? undefined : index; },
      tokenizer(src) { const match = /^\\\(([^\n]*?)\\\)/.exec(src); if (match) return { type: 'rocotoInlineMath', raw: match[0], text: match[1] }; },
      renderer(token) { return `<code class="language-math">${esc(token.text)}</code>`; }
    }] });
  }

  function script(path, global) {
    if (window[global]) return Promise.resolve(window[global]);
    if (!loaded.has(path)) loaded.set(path, new Promise(resolve => {
      const el = document.createElement('script'); el.src = path; el.async = true;
      const finish = () => { clearTimeout(timer); el.onload = el.onerror = null; resolve(window[global] || null); };
      const timer = setTimeout(finish, 8000); el.onload = el.onerror = finish; document.head.append(el);
    }));
    return loaded.get(path);
  }
  function mathAssets() {
    const href = '/static/vendor/katex-0.19.0/katex.min.css';
    if (!document.querySelector('link[data-rich-math]')) { const css = document.createElement('link'); css.rel = 'stylesheet'; css.href = href; css.dataset.richMath = '1'; document.head.append(css); }
    return script('/static/vendor/katex-0.19.0/katex.min.js', 'katex');
  }
  function language(code) { const match = /^language-([a-z0-9_+-]{1,32})$/i.exec(code.className); return match ? match[1].toLowerCase() : ''; }

  function button(node, icon, label, symbol, action) {
    const el = node('button', 'rich-action'); el.type = 'button'; el.setAttribute('aria-label', label); el.title = label;
    if (icon) { el.append(icon(symbol)); el.classList.add('rich-icon-action'); }
    el.append(node('span', 'rich-action-label', label)); el.addEventListener('click', action); return el;
  }
  function copyButton(node, icon, label, text) {
    let timer;
    const el = button(node, icon, label, 'copy', async () => {
      try { await navigator.clipboard.writeText(text()); el.lastChild.textContent = 'Copied'; el.title = 'Copied'; if (icon) el.firstChild.replaceWith(icon('check')); }
      catch { el.lastChild.textContent = 'Copy again'; el.title = 'Copy again'; }
      clearTimeout(timer); timer = setTimeout(() => { el.lastChild.textContent = label; el.title = label; if (icon) el.firstChild.replaceWith(icon('copy')); }, 1800);
    }); el.lastChild.setAttribute('aria-live', 'polite'); return el;
  }
  function toolbar(node, label) { const bar = node('div', 'rich-toolbar'); bar.append(node('span', 'rich-language', label)); return bar; }
  function actions(node, label) { const bar = node('div', 'rich-actions'); bar.setAttribute('role', 'group'); bar.setAttribute('aria-label', label); return bar; }
  function displayLanguage(lang) { return DISPLAY_LANGUAGES[lang] || (lang ? lang[0].toUpperCase() + lang.slice(1) : 'Code'); }
  function expand(node, icon, title, build, opener) {
    const dialog = node('dialog', 'rich-dialog'); dialog.setAttribute('aria-label', title);
    const head = node('div', 'rich-dialog-heading'); head.append(node('strong', '', title));
    const close = button(node, icon, 'Close expanded view', 'close', () => dialog.close()); head.append(close);
    const body = node('div', 'rich-dialog-content'); build(body); dialog.append(head, body); document.body.append(dialog);
    dialog.addEventListener('close', () => { dialog.remove(); if (opener.isConnected) opener.focus(); }, { once: true });
    dialog.addEventListener('click', event => { if (event.target === dialog) { const r = dialog.getBoundingClientRect(); if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) dialog.close(); } });
    dialog.showModal(); close.focus();
  }
  function tableText(table) {
    return [...table.rows].map(row => [...row.cells].map(cell => {
      const value = cell.textContent; return /[\t\n\r"]/.test(value) ? '"' + value.replace(/"/g, '""') + '"' : value;
    }).join('\t')).join('\n');
  }

  function allowedDiagram(source) {
    // Per-diagram config can alter CSS, resource loading and renderer behavior.
    // Treat those diagrams as literal source; app configuration owns the renderer.
    return source.length <= MAX_DIAGRAM && !/%%\s*\{|^\s*---/m.test(source) && source.split('\n').length <= 240;
  }
  function diagram(source, target, node) {
    return new Promise(resolve => {
      const iframe = node('iframe', 'rich-diagram'); iframe.title = 'Diagram'; iframe.setAttribute('sandbox', 'allow-scripts');
      iframe.referrerPolicy = 'no-referrer'; iframe.src = '/static/harness/rich-diagram.html'; iframe.setAttribute('aria-busy', 'true');
      const token = crypto.randomUUID(), theme = {};
      const styles = getComputedStyle(document.documentElement);
      for (const name of ['paper', 'ink', 'surface', 'border', 'link', 'muted']) theme[name] = styles.getPropertyValue('--' + name).trim();
      let finished = false;
      const done = success => { if (finished) return; finished = true; clearTimeout(timer); window.removeEventListener('message', receive); iframe.removeAttribute('aria-busy'); if (!success) iframe.remove(); resolve(success); };
      const receive = event => {
        if (event.source !== iframe.contentWindow || event.origin !== 'null' || event.data?.kind !== 'rocoto-diagram' || event.data?.token !== token) return;
        if (!target.isConnected) { done(false); return; }
        if (event.data.status === 'ready') { iframe.style.height = `${Math.min(1400, Math.max(80, Number(event.data.height) || 200))}px`; done(true); }
        else if (event.data.status === 'failed') done(false);
      };
      const timer = setTimeout(() => done(false), 10000); window.addEventListener('message', receive);
      iframe.addEventListener('load', () => { if (!finished && target.isConnected) iframe.contentWindow.postMessage({ kind: 'rocoto-diagram-render', token, source, theme }, '*'); else done(false); }, { once: true });
      target.append(iframe);
    });
  }

  async function enrich(root, { node = defaultNode, icon = null, pending = false } = {}) {
    if (!root || !window.DOMPurify) return;
    const tasks = []; let diagrams = 0;
    for (const table of [...root.querySelectorAll('table')].slice(0, MAX_BLOCKS)) {
      if (table.closest('.rich-table')) continue;
      const viewport = table.parentElement?.classList.contains('markdown-table') ? table.parentElement : node('div', 'markdown-table');
      const block = node('section', 'rich-table'), bar = actions(node, 'Table actions');
      (viewport.contains(table) ? viewport : table).before(block); block.append(bar, viewport); if (!viewport.contains(table)) viewport.append(table);
      bar.append(copyButton(node, icon, 'Copy table', () => tableText(table)));
      const open = button(node, icon, 'Expand table', 'fullscreen', () => expand(node, icon, 'Table', body => { const wrap = node('div', 'markdown-table'); wrap.append(table.cloneNode(true)); body.append(wrap); }, open)); bar.append(open);
    }
    for (const code of [...root.querySelectorAll('code')].slice(0, MAX_BLOCKS)) {
      if (code.dataset.richProcessed) continue;
      const lang = language(code), source = code.textContent, pre = code.parentElement?.tagName === 'PRE' ? code.parentElement : null;
      if (!pre && !MATH.has(lang)) continue;
      code.dataset.richProcessed = '1';
      if (MATH.has(lang) && source.length <= MAX_MATH) {
        tasks.push((async () => {
          const katex = await mathAssets(); if (!katex || !root.contains(code)) return;
          const rendered = node('span', pre ? 'rich-math-block' : 'rich-math');
          try { katex.render(source, rendered, { displayMode: Boolean(pre), throwOnError: true, trust: false, strict: 'ignore', maxSize: 10, maxExpand: 500, macros: {}, globalGroup: false }); }
          catch { return; }
          if (pre) { const block = node('section', 'rich-equation'), bar = toolbar(node, 'Math'); bar.append(copyButton(node, icon, 'Copy math', () => source)); pre.replaceWith(block); block.append(bar, rendered); }
          else code.replaceWith(rendered);
        })()); continue;
      }
      if (!pre) continue;
      const block = node('section', lang === 'mermaid' ? 'rich-code rich-diagram-block' : 'rich-code'), bar = lang === 'mermaid' ? actions(node, 'Diagram actions') : toolbar(node, displayLanguage(lang));
      pre.before(block); block.append(bar, pre); bar.append(copyButton(node, icon, lang === 'mermaid' ? 'Copy diagram' : 'Copy code', () => source));
      if (lang === 'mermaid') {
        if (!pending && diagrams < 4 && allowedDiagram(source)) {
          diagrams++; const preview = node('div', 'rich-diagram-preview'); block.append(preview);
          tasks.push(diagram(source, preview, node).then(success => {
            if (!success || !root.contains(block)) return;
            pre.hidden = true;
            const toggle = button(node, icon, 'View source', 'terminal', () => { pre.hidden = !pre.hidden; preview.hidden = !pre.hidden; const label = pre.hidden ? 'View source' : 'View diagram'; toggle.lastChild.textContent = label; toggle.setAttribute('aria-label', label); toggle.title = label; }); bar.append(toggle);
            const open = button(node, icon, 'Expand diagram', 'fullscreen', () => expand(node, icon, 'Diagram', body => { diagram(source, body, node); }, open)); bar.append(open);
          }));
        }
      } else if (LANGUAGES.has(lang) && source.length <= MAX_CODE) {
        tasks.push((async () => { const hljs = await script('/static/vendor/highlight-11.12.0/highlight.min.js', 'hljs'); if (!hljs || !root.contains(code) || !hljs.getLanguage(lang)) return;
          try { const html = hljs.highlight(source, { language: lang, ignoreIllegals: true }).value;
            code.replaceChildren(DOMPurify.sanitize(html, { RETURN_DOM_FRAGMENT: true, ALLOWED_TAGS: ['span'], ALLOWED_ATTR: ['class'] })); code.classList.add('hljs');
          } catch { /* Literal source remains available. */ }
        })());
      }
    }
    await Promise.allSettled(tasks);
  }
  window.RocotoRichContent = Object.freeze({ configure, enrich });
})();
