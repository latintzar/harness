/* Atelier native navigation. MIT © 2026 Peter Neyra; Lucide icons ISC. */
(() => {
  'use strict';
  const script = document.currentScript;
  const project = script?.dataset.project;
  if (!project || document.querySelector('atelier-native-nav')) return;
  const catalog = __CATALOG__;
  const icons = __ICONS__;
  const version = '__VERSION__';
  const current = catalog.find(item => item.id === project);
  if (!current) return;

  // Atelier serves one immutable asset bundle; a copied project uses its adjacent files.
  const atelierHost = location.hostname === 'atelierneyra.com' || location.hostname.endsWith('.atelierneyra.com');
  const assetBase = script.dataset.assetBase
    ? new URL(script.dataset.assetBase.replace(/\/?$/, '/'), script.src)
    : atelierHost ? new URL(`/assets/native-nav/releases/${version}/`, location.origin) : new URL('./', script.src);
  const asset = path => new URL(path, assetBase).href;
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const host = document.createElement('atelier-native-nav');
  host.dataset.version = version;
  const shadow = host.attachShadow({mode:'open'});
  const cards = catalog.filter(item => item.id !== project && item.entry).map(item =>
    `<a class="card" href="${escape(item.entry)}"><img src="${escape(item.thumbnail)}" alt="" loading="lazy" decoding="async"><span class="copy"><span class="title">${escape(item.name)}</span><span class="summary">${escape(item.summary)}</span></span></a>`
  ).join('');
  shadow.innerHTML = `<link rel="stylesheet" href="${escape(asset('atelier-nav.css'))}">
    <header class="bar">
      <a class="brand" href="https://atelierneyra.com/" aria-label="Atelier Neyra"><span class="brand-mark">${__MARK__}</span></a>
      <span class="name" title="${escape(current.name)}">${escape(current.name)}</span>
      <div class="actions"><button class="icon menu" type="button" aria-label="Projects" aria-expanded="false" aria-controls="atelier-project-dialog">${icons.grid}</button><a class="icon docs" href="${escape(current.docs)}" target="_blank" rel="noopener noreferrer" aria-label="Documentation">${icons.book}</a></div>
    </header>
    <dialog class="panel overlay" id="atelier-project-dialog" aria-labelledby="atelier-project-title" hidden>
      <div class="panel-head"><h2 id="atelier-project-title">Projects</h2><a class="experiences" href="https://atelierneyra.com/web-experiences/">Web Experiences</a><button class="icon close" type="button" aria-label="Close projects" autofocus>${icons.close}</button></div>
      <div class="grid">${cards}</div>
    </dialog>`;

  // A distinct family keeps the app's own typography unchanged across the shadow boundary.
  if (typeof FontFace === 'function' && document.fonts) {
    const font = new FontFace('Atelier Navigation Archivo', `url("${asset('atelier-nav-fonts/archivo-variable.woff2')}")`, {weight:'100 900', display:'swap'});
    document.fonts.add(font);
    font.load().then(() => {host.dataset.font = 'ready';}).catch(() => {host.dataset.font = 'fallback';});
  }
  const menu = shadow.querySelector('.menu');
  const dialog = shadow.querySelector('dialog');
  const close = shadow.querySelector('.close');
  let scrollState;
  const restore = () => {
    if (dialog.hidden && !scrollState) return;
    dialog.hidden = true;
    menu.setAttribute('aria-expanded','false');
    if (scrollState) {
      if (document.documentElement.style.getPropertyValue('overflow') === 'hidden') {
        if (scrollState.value) document.documentElement.style.setProperty('overflow',scrollState.value,scrollState.priority);
        else document.documentElement.style.removeProperty('overflow');
      }
      scrollState = undefined;
    }
    menu.focus({preventScroll:true});
  };
  const hide = () => {if (dialog.open) dialog.close();restore();};
  const show = () => {
    if (dialog.open) return;
    scrollState = {value:document.documentElement.style.getPropertyValue('overflow'),priority:document.documentElement.style.getPropertyPriority('overflow')};
    document.documentElement.style.setProperty('overflow','hidden');
    dialog.hidden = false;
    dialog.showModal();
    menu.setAttribute('aria-expanded','true');
    close.focus({preventScroll:true});
  };
  menu.addEventListener('click',() => dialog.open ? hide() : show());
  close.addEventListener('click',hide);
  dialog.addEventListener('close',restore);
  dialog.addEventListener('cancel',event => {event.preventDefault();hide();});
  dialog.addEventListener('click',event => {
    if (event.target !== dialog) return;
    const rect = dialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) hide();
  });
  dialog.addEventListener('keydown',event => {
    event.stopPropagation();
    if (event.key !== 'Tab') return;
    const focus = [...dialog.querySelectorAll('button:not([disabled]),a[href]')].filter(element => element.getClientRects().length);
    const first = focus[0], last = focus[focus.length - 1];
    if (event.shiftKey && shadow.activeElement === first) {event.preventDefault();last.focus();}
    else if (!event.shiftKey && shadow.activeElement === last) {event.preventDefault();first.focus();}
  });
  document.documentElement.style.setProperty('--atelier-nav-height','56px');
  document.body.prepend(host);
})();
