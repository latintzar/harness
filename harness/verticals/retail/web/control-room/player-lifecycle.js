/* Suspend background decoding without turning a return to the tab into a seek. */
(function (root) {
  'use strict';
  function create(options) {
    const doc = options.document || document, target = options.window || window, video = options.video;
    let resume = null, disposed = false, externalSuspension = false;
    function hide() {
      if (disposed) return;
      if (!resume) resume = { playing: !video.paused && !video.ended, context: options.context() };
      options.onSuspend();
      video.pause();
    }
    function show() {
      if (disposed || doc.hidden || externalSuspension) return;
      const previous = resume; resume = null;
      if (!options.isActive()) return;
      options.onResume();
      // A changed recording or closed view cancels the old playback intention.
      if (previous?.playing && previous.context === options.context()) video.play().catch(() => {});
    }
    function visibility() { if (doc.hidden) hide(); else show(); }
    function play() { if (doc.hidden || externalSuspension) video.pause(); }
    doc.addEventListener('visibilitychange', visibility);
    target.addEventListener('pagehide', hide);
    target.addEventListener('pageshow', show);
    video.addEventListener('play', play);
    if (doc.hidden) hide();
    return { setSuspended(value) {
      value = Boolean(value); if (value === externalSuspension) return;
      externalSuspension = value; if (value) hide(); else show();
    }, destroy() {
      disposed = true; resume = null;
      doc.removeEventListener('visibilitychange', visibility);
      target.removeEventListener('pagehide', hide); target.removeEventListener('pageshow', show);
      video.removeEventListener('play', play);
    } };
  }
  root.PlayerLifecycle = { create };
})(window);
