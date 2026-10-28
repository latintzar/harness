/* Retail's Home plugs into the shared workspace tab host. */
(() => {
  'use strict';
  window.RocotoWorkspaceTabs.register('retail', {home:{mount(root,context) {
    const node=context.node;
    let controlRoom=null;
  function mountLocation(slot, config) {
    let disposed = false;
    let snapshotTimer = null;
    const showState = (title, copy, action = false) => { if (disposed) return; slot.replaceChildren(); const body = node('div', 'cr-location-slot-copy'); body.append(node('strong', '', title), node('span', '', copy)); if (action) { const button = node('button', 'cr-secondary-button', 'Refresh location'); button.type = 'button'; button.addEventListener('click', () => controlRoom?.refresh()); body.append(button); } slot.append(body); };
    showState('Loading location', 'Preparing your cameras and saved visits.');
    // The page opens on the status alone. The location snapshot is the heavier read: it starts once the viewer
    // has loaded and asked for its own data, and reaches the viewer when it is ready.
    const readSnapshot = () => fetch('/api/retail/location', { credentials: 'same-origin', cache: 'no-store' }).then(async response => response.ok ? response.json() : null).catch(() => null);
    fetch('/api/retail/viewer', { credentials: 'same-origin', cache: 'no-store' }).then(async response => response.ok ? response.json() : null).then((status) => {
      if (disposed) return;
      if (!status?.ready) { showState('Location viewer', status?.message || 'Connect location services to open map, weather and building context.', true); return; }
      if (status.location) window.dispatchEvent(new CustomEvent('rocoto:location-shell', { detail: status.location }));
      const href = typeof status.bundle_url === 'string' && status.bundle_url ? status.bundle_url : '/control-room/';
      const url = new URL(href, location.origin); url.searchParams.set('embedded', '1'); url.hash = 'view=location';
      const frame = document.createElement('iframe'); frame.className = 'cr-location-frame'; frame.title = `${config?.name || 'Store'} location overview`;
      const loaded = new Promise((resolve) => frame.addEventListener('load', resolve, { once: true }));
      // The viewer retains this snapshot until its data components are ready.
      loaded.then(() => new Promise((resolve) => { snapshotTimer = setTimeout(resolve, 1200); })).then(readSnapshot).then((snapshot) => {
        if (disposed || !snapshot || typeof snapshot !== 'object') return;
        if (snapshot.locations?.[0]) window.dispatchEvent(new CustomEvent('rocoto:location-shell', { detail: snapshot.locations[0] }));
        frame.contentWindow?.postMessage({ type: 'rocoto:location-snapshot', snapshot }, location.origin);
      });
      frame.src = url.href; slot.replaceChildren(frame);
    }).catch(() => showState('Location viewer', 'Connect location services to open map, weather and building context.', true));
    return () => { disposed = true; if (snapshotTimer) clearTimeout(snapshotTimer); snapshotTimer = null; slot.replaceChildren(); };
  }

    controlRoom=window.RocotoControlRoom.create({root,location:window.RocotoControlRoomConfig?.location,
      onNewChat:context.createAgent,onOpenChat:context.openChat,onOpenChatList:context.openChatList,
      onOpenAgentChat:context.openAgent,onOpenOrganization:detail=>context.openOrganization(detail?.purpose),
      onOpenLocation:config=>window.dispatchEvent(new CustomEvent('rocoto:open-location',{detail:config})),mountLocation});
    const visible=value=>root.querySelectorAll('.cr-location-frame').forEach(frame=>frame.contentWindow?.postMessage(
      {type:value?'rocoto:player-resume':'rocoto:player-suspend'},location.origin));
    return {refresh(){controlRoom.refresh();},show(){controlRoom.show();visible(true);},hide(){visible(false);},unmount(){controlRoom.destroy();}};
  }}});
})();
