/* Atelier native navigation. MIT © 2026 Peter Neyra; Lucide icons ISC. */
(() => {
  'use strict';
  const script = document.currentScript;
  const project = script?.dataset.project;
  if (!project || document.querySelector('atelier-native-nav')) return;
  const catalog = [{"id":"ai-native-erp","name":"Rocoto","summary":"AI-native ERP for food distributors","site":"https://soyrocoto.com","repo":"https://github.com/latintzar/ai-native-erp","docs":"https://docs.atelierneyra.com/ai-native-erp/overview/","entry":"https://preview.atelierneyra.com/apps/erp/app.html","thumbnail":"https://demo.soyrocoto.com/open-source/sites/studio/assets/product-motion-v4/ai-native-erp-1200.webp"},{"id":"agent-workspace","name":"Harness","summary":"A Codex-based agent harness with generative UI","site":"https://atelierneyra.com/harness","repo":"https://github.com/latintzar/harness","docs":"https://docs.atelierneyra.com/agent-workspace/overview/","entry":"https://preview.atelierneyra.com/apps/agent-workspace/","thumbnail":"https://atelierneyra.com/assets/product-motion-v5/agent-workspace-0e3fe195346c-1200.webp"},{"id":"manufacturer-order-desk","name":"Manufacturer Order Desk","summary":"An AI-native order desk for construction materials manufacturers working across web and email","site":"https://atelierneyra.com/manufacturer-order-desk","repo":"https://github.com/latintzar/manufacturer-order-desk","docs":"https://docs.atelierneyra.com/manufacturer-order-desk/overview/","entry":"https://preview.atelierneyra.com/apps/manufacturer-order-desk/","thumbnail":"https://demo.soyrocoto.com/open-source/sites/studio/assets/product-motion-v4/manufacturer-order-desk-1200.webp"},{"id":"video","name":"Video Intelligence","summary":"CCTV feeds with Cosmos-powered alerts for real-time business intelligence","site":"https://atelierneyra.com/video","repo":"https://github.com/latintzar/camera-video-search","docs":"https://docs.atelierneyra.com/video/overview/","entry":"https://preview.atelierneyra.com/apps/video/control-room/#view=location&period=year","thumbnail":"https://atelierneyra.com/assets/product-motion-v5/video-bfd81a0d96af-1200.webp"},{"id":"shop-workspace","name":"Rocoto Peru","summary":"Mobile AI-native ERP matching bodegas\u2019 Yape/Plin payments to WhatsApp orders","site":"https://atelierneyra.com/peru","repo":"https://github.com/latintzar/whatsapp-shop-workspace","docs":"https://docs.atelierneyra.com/shop-workspace/overview/","entry":"https://preview.atelierneyra.com/apps/peru/bodega-pay-later.html#/prep","thumbnail":"https://atelierneyra.com/assets/product-motion-v5/shop-workspace-0f44723d1911-1200.webp"},{"id":"disdat","name":"DisDat.dev","summary":"A Chrome extension for recording feedback for coding agents","site":"https://disdat.dev","repo":"https://github.com/latintzar/disdat","docs":"https://docs.atelierneyra.com/disdat/overview/","entry":"https://github.com/latintzar/disdat/releases/latest/download/disdat-extension.zip","thumbnail":"https://demo.soyrocoto.com/open-source/sites/studio/assets/product-motion-v4/disdat-1200.webp"},{"id":"cancha","name":"Cancha","summary":"An Android TV app to watch free Latin American TV","site":"https://atelierneyra.com/cancha","repo":"https://github.com/latintzar/cancha-tv","docs":"https://docs.atelierneyra.com/cancha/overview/","entry":"https://preview.atelierneyra.com/apps/cancha/watch","thumbnail":"https://atelierneyra.com/assets/product-motion-v5/cancha-a346548642fd-1200.webp"},{"id":"renovation-marketplace","name":"Leo.casa","summary":"Renovation quotes, project chat and milestone payments","site":"https://leo.casa","repo":"https://github.com/latintzar/renovation-marketplace","docs":"https://docs.atelierneyra.com/renovation-marketplace/overview/","entry":"https://preview.atelierneyra.com/apps/renovation-marketplace/?local_role=contractor","thumbnail":"https://atelierneyra.com/assets/product-motion-v5/renovation-marketplace-c423bf0a3793-1200.webp"},{"id":"route-planner","name":"Farm-to-restaurant route planner","summary":"VROOM-based routing for farm-to-table deliveries","site":"https://atelierneyra.com/route-planner","repo":"https://github.com/latintzar/farm-to-restaurant-route-planner","docs":"https://docs.atelierneyra.com/route-planner/overview/","entry":"https://atelierneyra.com/preview/route-planner/","thumbnail":"https://demo.soyrocoto.com/open-source/sites/studio/assets/product-motion-v4/route-planner-1200.webp"},{"id":"vitrina","name":"Vitrina Local","summary":"Turn recipe Reels & TikToks into shopping baskets from local grocers","site":"https://atelierneyra.com/vitrina","repo":"https://github.com/latintzar/vitrina-local","docs":"https://docs.atelierneyra.com/vitrina/overview/","entry":"https://preview.atelierneyra.com/apps/vitrina-local/","thumbnail":"https://demo.soyrocoto.com/open-source/sites/studio/assets/product-motion-v4/vitrina-1200.webp"},{"id":"social-recipes","name":"Social Recipes","summary":"An Instagram agent runtime that turns Reels into recipes","site":"https://atelierneyra.com/social-recipes","repo":"https://github.com/latintzar/social-recipes-kit","docs":"https://docs.atelierneyra.com/social-recipes/overview/","entry":"https://github.com/latintzar/social-recipes-kit","thumbnail":"https://demo.soyrocoto.com/open-source/sites/studio/assets/product-motion-v4/social-recipes-1200.webp"},{"id":"hermes-caller","name":"Hermes Caller","summary":"A Twilio dialler with diarised transcripts and Hermes debriefs","site":"https://atelierneyra.com/hermes-caller","repo":"https://github.com/latintzar/hermes-caller","docs":"https://docs.atelierneyra.com/hermes-caller/overview/","entry":"https://github.com/latintzar/hermes-caller/releases/download/v5.8-preview/hermes-caller-v5.8-debug.apk","thumbnail":"https://atelierneyra.com/assets/product-motion-v5/hermes-caller-2f04d2b4f42a-1200.webp"},{"id":"leo-estimates","name":"Leo Estimate","summary":"Room-by-room renovation estimates from a property listing","site":"https://atelierneyra.com/renovation-estimates","repo":"https://github.com/latintzar/renovation-estimates","docs":"https://docs.atelierneyra.com/leo-estimates/overview/","entry":"https://preview.atelierneyra.com/apps/estimates/listings","thumbnail":"https://demo.soyrocoto.com/open-source/sites/studio/assets/product-motion-v4/leo-estimates-1200.webp"},{"id":"home-matching","name":"Home Matching","summary":"AI-native property scanner across platforms based on the person's lifestyle","site":"https://atelierneyra.com/home-matching","repo":"https://github.com/latintzar/home-matching","docs":"https://docs.atelierneyra.com/home-matching/overview/","entry":"https://preview.atelierneyra.com/apps/home-matching/dashboard","thumbnail":"https://demo.soyrocoto.com/open-source/sites/studio/assets/product-motion-v4/home-matching-1200.webp"},{"id":"retail-location-similarity","name":"Retail Location Similarity","summary":"Geospatial embeddings to find similar places","site":"https://atelierneyra.com/retail-location-similarity","repo":"https://github.com/latintzar/retail-location-similarity","docs":"https://docs.atelierneyra.com/retail-location-similarity/overview/","entry":"https://github.com/latintzar/retail-location-similarity","thumbnail":"https://atelierneyra.com/assets/product-motion-v5/retail-location-similarity-b30d6ebfb434-1200.webp"},{"id":"geospatial-agent","name":"Geospatial Agent","summary":"An AI agent for querying and exploring maps through chat","site":"https://atelierneyra.com/geospatial-agent","repo":"https://github.com/latintzar/geospatial-agent","docs":"https://docs.atelierneyra.com/geospatial-agent/overview/","entry":"https://preview.atelierneyra.com/apps/geospatial-agent/","thumbnail":"https://demo.soyrocoto.com/open-source/sites/studio/assets/product-motion-v4/geospatial-agent-1200.webp"},{"id":"leo-vibe-maps","name":"Leo Vibe Maps","summary":"An Airbnb map extension that highlights neighbourhoods by vibe","site":"https://atelierneyra.com/vibe-maps","repo":"https://github.com/latintzar/leo-vibe-maps","docs":"https://docs.atelierneyra.com/leo-vibe-maps/overview/","entry":"https://preview.atelierneyra.com/apps/vibe-maps/","thumbnail":"https://demo.soyrocoto.com/open-source/sites/studio/assets/product-motion-v4/leo-vibe-maps-1200.webp"},{"id":"tactile-glove","name":"Artisan Glove","summary":"A sensing-glove design for contact, vibration and video.","site":"https://atelierneyra.com/hardware/tactile-glove","repo":"https://github.com/latintzar/tactile-glove","docs":"https://docs.atelierneyra.com/tactile-glove/overview/","entry":"https://atelierneyra.com/hardware/tactile-glove/viewer/","thumbnail":"https://atelierneyra.com/hardware/tactile-glove/media/glove-concept.webp"},{"id":"messier-57","name":"Messier \u201957","summary":"An electric drum with an acoustic feel","site":"https://atelierneyra.com/hardware/messier-57","repo":"https://github.com/latintzar/messier57-instrument","docs":"https://docs.atelierneyra.com/messier-57/overview/","entry":"https://atelierneyra.com/hardware/messier-57?play=1","thumbnail":"https://atelierneyra.com/hardware/messier-57/media/92b6541e_6.webp"}];
  const icons = {"grid": "<!-- Lucide 1.34.0, ISC license; original icon geometry. -->\n<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"24\" height=\"24\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"#000\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><rect width=\"7\" height=\"7\" x=\"3\" y=\"3\" rx=\"1\" /><rect width=\"7\" height=\"7\" x=\"14\" y=\"3\" rx=\"1\" /><rect width=\"7\" height=\"7\" x=\"14\" y=\"14\" rx=\"1\" /><rect width=\"7\" height=\"7\" x=\"3\" y=\"14\" rx=\"1\" /></svg>\n", "book": "<!-- Lucide 1.34.0, ISC license; original icon geometry. -->\n<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"24\" height=\"24\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"#000\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M12 5v16\" /><path d=\"M20.001 19A2 2 0 0022 17V5a2 2 0 00-1.999-2L16 3.002A5 5 0 0012 5a5 5 0 00-4-2H4a2 2 0 00-2 2v12a2 2 0 001.999 2H8a5 5 0 014 2 5 5 0 014-2z\" /></svg>\n", "close": "<!-- Lucide 1.34.0, ISC license; original icon geometry. -->\n<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"24\" height=\"24\" viewBox=\"0 0 24 24\" fill=\"none\" stroke=\"#000\" stroke-width=\"2\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M18 6 6 18\" /><path d=\"m6 6 12 12\" /></svg>\n"};
  const version = 'v2-ea6222b5ad29f532';
  const current = catalog.find(item => item.id === project);
  if (!current) return;

  // Official Atelier pages use the commissioned direct asset origin. A copied
  // project keeps its adjacent files or an explicit data-asset-base override.
  const studioAssets = new URL('https://demo.soyrocoto.com/open-source/sites/studio/');
  const atelierHost = location.hostname === 'atelierneyra.com' || location.hostname.endsWith('.atelierneyra.com');
  const assetBase = script.dataset.assetBase
    ? new URL(script.dataset.assetBase.replace(/\/?$/, '/'), script.src)
    : atelierHost ? new URL(`assets/native-nav/releases/${version}/`, studioAssets) : new URL('./', script.src);
  const asset = path => new URL(path, assetBase).href;
  // Only image resources move; chooser destinations retain their own hosts.
  const thumbnail = value => {
    try {
      const url = new URL(value, 'https://atelierneyra.com/');
      if (url.origin === 'https://atelierneyra.com' && /\.(?:avif|gif|jpe?g|png|svg|webp)$/i.test(url.pathname)) {
        return new URL(url.pathname.replace(/^\/+/, ''), studioAssets).href + url.search + url.hash;
      }
    } catch {}
    return value;
  };
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const host = document.createElement('atelier-native-nav');
  host.dataset.version = version;
  const shadow = host.attachShadow({mode:'open'});
  const cards = catalog.filter(item => item.id !== project && item.entry).map(item =>
    `<a class="card" href="${escape(item.entry)}"><img src="${escape(thumbnail(item.thumbnail))}" alt="" loading="lazy" decoding="async"><span class="copy"><span class="title">${escape(item.name)}</span><span class="summary">${escape(item.summary)}</span></span></a>`
  ).join('');
  shadow.innerHTML = `<link rel="stylesheet" href="${escape(asset('atelier-nav.css'))}">
    <header class="bar">
      <a class="brand" href="https://atelierneyra.com/" aria-label="Atelier Neyra"><span class="brand-mark">${"<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 24 24\" fill=\"currentColor\"><!-- Standalone outline of Atelier's existing homepage U+2731 motif; new export, not a recovered logo master. DejaVu Sans glyph, Bitstream Vera/DejaVu licence. --><metadata>Copyright: Copyright (c) 2003 by Bitstream, Inc. All Rights Reserved.\n Bitstream Vera is a trademark of Bitstream, Inc.\n DejaVu changes are in public domain.\nLicense: bitstream-vera\n Permission is hereby granted, free of charge, to any person obtaining a copy\n of the fonts accompanying this license (\"Fonts\") and associated\n documentation files (the \"Font Software\"), to reproduce and distribute the\n Font Software, including without limitation the rights to use, copy, merge,\n publish, distribute, and/or sell copies of the Font Software, and to permit\n persons to whom the Font Software is furnished to do so, subject to the\n following conditions:\n\n The above copyright and trademark notices and this permission notice shall\n be included in all copies of one or more of the Font Software typefaces.\n\n The Font Software may be modified, altered, or added to, and in particular\n the designs of glyphs or characters in the Fonts may be modified and\n additional glyphs or characters may be added to the Fonts, only if the fonts\n are renamed to names not containing either the words \"Bitstream\" or the word\n \"Vera\".\n\n This License becomes null and void to the extent applicable to Fonts or Font\n Software that has been modified and is distributed under the \"Bitstream\n Vera\" names.\n\n The Font Software may be sold as part of a larger software package but no\n copy of one or more of the Font Software typefaces may be sold by itself.\n\n THE FONT SOFTWARE IS PROVIDED \"AS IS\", WITHOUT WARRANTY OF ANY KIND, EXPRESS\n OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY WARRANTIES OF MERCHANTABILITY,\n FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT OF COPYRIGHT, PATENT,\n TRADEMARK, OR OTHER RIGHT. IN NO EVENT SHALL BITSTREAM OR THE GNOME\n FOUNDATION BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, INCLUDING\n ANY GENERAL, SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL DAMAGES,\n WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF\n THE USE OR INABILITY TO USE THE FONT SOFTWARE OR FROM OTHER DEALINGS IN THE\n FONT SOFTWARE.\n\n Except as contained in this notice, the names of Gnome, the Gnome\n Foundation, and Bitstream Inc., shall not be used in advertising or\n otherwise to promote the sale, use or other dealings in this Font Software\n without prior written authorization from the Gnome Foundation or Bitstream\n Inc., respectively. For further information, contact: fonts at gnome dot\n org.\n</metadata><path transform=\"matrix(0.01339585 0 0 -0.01339585 0.50636303 22.00000000)\" d=\"M698 1024Q698 1024 698 1493H1018V1023Q1018 1023 1424 1258L1584 981L1178 747Q1178 747 1584 512L1424 235L1018 470Q1018 470 1018 0H698V470Q698 470 292 235L132 512L537 747Q537 747 132 981L292 1258Z\"/></svg>\n"}</span></a>
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
