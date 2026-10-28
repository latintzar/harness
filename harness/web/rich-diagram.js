(() => {
  'use strict';
  let used = false;
  window.addEventListener('message', async event => {
    const value = event.data;
    if (used || event.source !== parent || event.origin !== new URL(location.href).origin || value?.kind !== 'rocoto-diagram-render' ||
        typeof value.token !== 'string' || !/^[a-f0-9-]{36}$/.test(value.token) || typeof value.source !== 'string') return;
    used = true;
    const reply = (status, height = 0) => parent.postMessage({ kind: 'rocoto-diagram', token: value.token, status, height }, event.origin);
    if (value.source.length > 12000 || value.source.split('\n').length > 240 || /%%\s*\{|^\s*---/m.test(value.source)) { reply('failed'); return; }
    try {
      const palette = value.theme;
      if (!palette || !['paper', 'ink', 'surface', 'border', 'link', 'muted'].every(name => typeof palette[name] === 'string' && /^#[a-f0-9]{6}$/i.test(palette[name]))) { reply('failed'); return; }
      for (const [name, color] of Object.entries(palette)) if (['paper', 'ink', 'surface', 'border', 'link', 'muted'].includes(name)) document.documentElement.style.setProperty('--' + name, color);
      mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'base', fontFamily: 'Arial, sans-serif', htmlLabels: false,
        themeVariables: { background: palette.paper, primaryColor: palette.surface, primaryTextColor: palette.ink, primaryBorderColor: palette.border, lineColor: palette.muted, textColor: palette.ink, secondaryColor: palette.paper, tertiaryColor: palette.surface, edgeLabelBackground: palette.paper },
        maxTextSize: 12000, maxEdges: 100, suppressErrorRendering: true, flowchart: { htmlLabels: false },
        secure: ['secure', 'securityLevel', 'startOnLoad', 'maxTextSize', 'maxEdges', 'suppressErrorRendering', 'htmlLabels', 'fontFamily', 'theme', 'themeVariables', 'themeCSS', 'dompurifyConfig'] });
      const { svg } = await mermaid.render('rocoto-diagram-svg', value.source);
      const target = document.querySelector('#diagram'); target.innerHTML = svg;
      // Strict mode disables interactions. The additional removal also keeps
      // generated SVG resources inert under this page's network-denying CSP.
      target.querySelectorAll('a,image,foreignObject,script').forEach(el => el.remove());
      target.querySelectorAll('*').forEach(el => { for (const attr of [...el.attributes]) if (/^on/i.test(attr.name) || /^(?:xlink:)?href$/i.test(attr.name)) el.removeAttribute(attr.name); });
      reply('ready', Math.ceil(target.getBoundingClientRect().height));
    } catch { reply('failed'); }
  });
})();
