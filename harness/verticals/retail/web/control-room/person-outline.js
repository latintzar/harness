/*
 * Optional selected-person silhouette overlay.
 *
 * The tracker owns identity and the box. This module only turns that box into
 * a real MediaPipe person mask. It never draws a geometric stand-in.
 * The pinned official MediaPipe assets load lazily from this app. Segmentation
 * runs on the browser CPU in a dedicated same-origin worker; identity stays
 * with the tracker. A failed engine stays visible and can be retried.
 */
(function (root) {
  'use strict';

  var assetRoot = new URL('vendor/mediapipe-0.10.22-rc.20250304/', document.currentScript.src);
  var DEFAULT_ASSETS = {
    moduleUrl: new URL('vision_bundle.mjs', assetRoot).href,
    wasmRoot: new URL('wasm', assetRoot).href,
    modelAssetPath: new URL('selfie_segmenter.tflite', assetRoot).href
  };
  var WORKER_URL = new URL('person-outline-worker.js', document.currentScript.src).href;
  var MASK_SIZE = 256;
  var UPDATE_MS = 180; // 5.5 Hz mask refresh; the box can still move every frame.
  var OVERLAP_LIMIT = 0.12;

  function finite(value, fallback) {
    return Number.isFinite(Number(value)) ? Number(value) : fallback;
  }

  function normalizeBox(value) {
    if (Array.isArray(value) && value.length >= 4) {
      return { x: finite(value[0], 0), y: finite(value[1], 0),
        x2: finite(value[2], 0), y2: finite(value[3], 0) };
    }
    if (!value || typeof value !== 'object') return null;
    var x = value.x != null ? value.x : value.left;
    var y = value.y != null ? value.y : value.top;
    var x2 = value.x2 != null ? value.x2 : (value.right != null ? value.right : Number(x) + Number(value.width || 0));
    var y2 = value.y2 != null ? value.y2 : (value.bottom != null ? value.bottom : Number(y) + Number(value.height || 0));
    x = finite(x, 0); y = finite(y, 0); x2 = finite(x2, x); y2 = finite(y2, y);
    return { x: Math.min(x, x2), y: Math.min(y, y2), x2: Math.max(x, x2), y2: Math.max(y, y2) };
  }

  function area(box) {
    return Math.max(0, box.x2 - box.x) * Math.max(0, box.y2 - box.y);
  }

  function intersection(a, b) {
    var x = Math.max(0, Math.min(a.x2, b.x2) - Math.max(a.x, b.x));
    var y = Math.max(0, Math.min(a.y2, b.y2) - Math.max(a.y, b.y));
    return x * y;
  }

  function create(options) {
    options = options || {};
    var canvas = options.canvas;
    var video = options.video;
    if (!canvas || !video) throw new Error('PersonOutline.create requires canvas and video');
    var ctx = canvas.getContext('2d');
    var assets = Object.assign({}, DEFAULT_ASSETS, options.assets || {});
    var enabled = options.enabled === true;
    var suspended = false;
    var destroyed = false;
    var worker = null;
    var startupTimeout = null;
    var engine = 'starting';

    var current = null;
    var cached = null;
    var seq = 0;
    var lastSubmit = -Infinity;
    var inFlight = null;
    var refreshMs = UPDATE_MS;
    var elapsedAverage = 0;
    var submitted = 0, completed = 0, accepted = 0, discarded = 0;
    var lastFrameKey = null;
    var queuedFrame = null;

    function clear() {
      if (ctx) ctx.clearRect(0, 0, canvas.width, canvas.height);
      cached = null;
      queuedFrame = null;
      lastFrameKey = null;
      seq += 1; // invalidate any in-flight worker result
      current = null;
    }

    function notify() {
      if (typeof options.onStatus === 'function') options.onStatus(status());
    }

    function fail() {
      clearTimeout(startupTimeout);
      if (worker) worker.terminate();
      worker = null; inFlight = null;
      engine = 'failed'; queuedFrame = null; clear(); notify();
    }

    function ensureCanvas(width, height) {
      if (!width || !height) return;
      if (canvas.width !== width) canvas.width = width;
      if (canvas.height !== height) canvas.height = height;
    }

    function buildEdgeTexture(message) {
      var pixels = new Uint8ClampedArray(message.edgePixels);
      if (pixels.length !== MASK_SIZE * MASK_SIZE * 4) throw new Error('Invalid outline texture');
      var edge = document.createElement('canvas');
      edge.width = MASK_SIZE; edge.height = MASK_SIZE;
      edge.getContext('2d').putImageData(new ImageData(pixels, MASK_SIZE, MASK_SIZE), 0, 0);
      return edge;
    }

    function drawMask(mask, box, width, height) {
      if (!mask || !mask.edgeCanvas || !box || !width || !height) return;
      ensureCanvas(width, height);
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      var w = box.x2 - box.x, h = box.y2 - box.y;
      ctx.save();
      ctx.globalCompositeOperation = 'source-over';
      ctx.globalAlpha = 0.48;
      ctx.filter = 'blur(3px)';
      ctx.drawImage(mask.edgeCanvas, box.x, box.y, w, h);
      ctx.filter = 'none';
      ctx.globalAlpha = 0.92;
      ctx.drawImage(mask.edgeCanvas, box.x, box.y, w, h);
      ctx.restore();
    }

    function significantOverlap(box, others) {
      var targetArea = area(box);
      if (!targetArea) return true;
      return (others || []).map(normalizeBox).filter(Boolean).some(function (other) {
        return intersection(box, other) / targetArea >= OVERLAP_LIMIT;
      });
    }

    function acceptMask(message) {
      // Clearing the overlay invalidates attribution, not the outstanding work.
      // Release the sole worker slot even when that result is now obsolete.
      if (!inFlight || message.seq !== inFlight.seq) return;
      var elapsed = Math.max(0, performance.now() - inFlight.started);
      inFlight = null; completed += 1;
      elapsedAverage = elapsedAverage ? elapsedAverage * 0.7 + elapsed * 0.3 : elapsed;
      refreshMs = Math.min(1500, Math.max(UPDATE_MS, Math.ceil(elapsedAverage * 2)));
      if (destroyed || suspended || document.hidden || !current || message.seq !== current.seq || message.personId !== current.personId ||
          current.suppressed || (current.time != null && message.time != null && Math.abs(Number(current.time) - Number(message.time)) > 0.25)) {
        discarded += 1; return;
      }
      try {
        cached = { edgeCanvas: buildEdgeTexture(message), time: message.time, personId: message.personId,
          sourceWidth: message.sourceWidth, sourceHeight: message.sourceHeight, cropBox: message.cropBox };
        accepted += 1;
        drawMask(cached, cached.cropBox, current.sourceWidth, current.sourceHeight);
      } catch (error) { fail(); }
    }

    function onWorkerMessage(event) {
      var message = event.data || {};
      if (message.kind === 'ready') {
        clearTimeout(startupTimeout); engine = 'worker-ready'; notify(); flushQueued(); return;
      }
      if (message.kind === 'mask') { acceptMask(message); return; }
      if (message.kind === 'error') fail();
    }

    function startWorker() {
      if (destroyed || suspended || document.hidden || !enabled || worker) return;
      engine = 'worker-loading'; notify();
      if (typeof Worker === 'undefined' || typeof OffscreenCanvas === 'undefined') { fail(); return; }
      try {
        // A classic worker supports the official runtime's importScripts loader.
        // A static URL needs neither blob: workers nor external asset origins.
        worker = new Worker(WORKER_URL);
        var activeWorker = worker;
        worker.onmessage = function (event) { if (worker === activeWorker) onWorkerMessage(event); };
        worker.onerror = function () { if (worker === activeWorker) fail(); };
        worker.postMessage({ kind: 'init', assets: assets });
        startupTimeout = setTimeout(fail, 30000);
      } catch (e) { fail(); }
    }

    function flushQueued() {
      if (!queuedFrame || engine !== 'worker-ready') return;
      var frame = queuedFrame;
      queuedFrame = null;
      submit(frame.box, frame.sourceWidth, frame.sourceHeight, frame.personId, frame.time);
    }

    function submit(box, sourceWidth, sourceHeight, personId, time) {
      if (destroyed || suspended || document.hidden || !enabled) return;
      if (engine !== 'worker-ready') {
        queuedFrame = { box: box, sourceWidth: sourceWidth, sourceHeight: sourceHeight, personId: personId, time: time };
        return;
      }
      var frameKey = personId + ':' + time;
      if (inFlight || frameKey === lastFrameKey || performance.now() - lastSubmit < refreshMs || !video || video.readyState < 2 || !video.videoWidth) return;
      lastSubmit = performance.now(); lastFrameKey = frameKey;
      var padX = (box.x2 - box.x) * 0.08, padY = (box.y2 - box.y) * 0.05;
      var crop = { x: Math.max(0, box.x - padX), y: Math.max(0, box.y - padY), x2: Math.min(sourceWidth, box.x2 + padX), y2: Math.min(sourceHeight, box.y2 + padY) };
      var sw = crop.x2 - crop.x, sh = crop.y2 - crop.y;
      var scale = Math.min(MASK_SIZE / sw, MASK_SIZE / sh), dw = sw * scale, dh = sh * scale;
      var dx = (MASK_SIZE - dw) / 2, dy = (MASK_SIZE - dh) / 2;
      var request = { seq: ++seq, personId: personId, time: time, cropBox: crop, sourceWidth: sourceWidth, sourceHeight: sourceHeight, contentRect: [dx, dy, dw, dh] };
      inFlight = { seq: request.seq, started: lastSubmit }; submitted += 1;
      if (current) current.seq = request.seq;
      try {
        var bitmap, sourceFrame = typeof VideoFrame !== 'undefined';
        if (sourceFrame) {
          // Transfer the decoded frame; crop/letterbox work belongs in the worker.
          bitmap = new VideoFrame(video, { timestamp: Math.round(Number(time || 0) * 1000000) });
        } else {
          var target = new OffscreenCanvas(MASK_SIZE, MASK_SIZE);
          target.getContext('2d').drawImage(video, crop.x, crop.y, sw, sh, dx, dy, dw, dh);
          bitmap = target.transferToImageBitmap();
        }
        worker.postMessage({ kind: 'frame', seq: request.seq, personId: personId, time: time, cropBox: crop, sourceWidth: sourceWidth, sourceHeight: sourceHeight, bitmap: bitmap, sourceFrame: sourceFrame, size: MASK_SIZE, contentRect: request.contentRect }, [bitmap]);
      } catch (error) { if (bitmap) bitmap.close(); fail(); }
    }

    function update(frame) {
      if (destroyed || !enabled || suspended || document.hidden) return;
      frame = frame || {};
      var sourceWidth = finite(frame.sourceWidth, video.videoWidth || canvas.width);
      var sourceHeight = finite(frame.sourceHeight, video.videoHeight || canvas.height);
      var box = normalizeBox(frame.box);
      if (!box || box.x2 <= box.x || box.y2 <= box.y || !sourceWidth || !sourceHeight) { clear(); return; }
      box.x = Math.max(0, Math.min(sourceWidth, box.x)); box.y = Math.max(0, Math.min(sourceHeight, box.y));
      box.x2 = Math.max(box.x, Math.min(sourceWidth, box.x2)); box.y2 = Math.max(box.y, Math.min(sourceHeight, box.y2));
      if (box.x2 <= box.x || box.y2 <= box.y) { clear(); return; }
      var personId = frame.personId == null ? '' : String(frame.personId);
      if (current && frame.time != null && current.time != null && Number(frame.time) + 0.05 < Number(current.time)) clear();
      if (!current || current.personId !== personId || current.sourceWidth !== sourceWidth || current.sourceHeight !== sourceHeight) { clear(); }
      current = { box: box, personId: personId, sourceWidth: sourceWidth, sourceHeight: sourceHeight, time: frame.time, seq: seq };
      ensureCanvas(sourceWidth, sourceHeight);
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      if (significantOverlap(box, frame.otherBoxes)) { current.suppressed = true; cached = null; return; }
      if (cached && cached.personId === personId && cached.sourceWidth === sourceWidth && cached.sourceHeight === sourceHeight &&
          (frame.time == null || cached.time == null || Math.abs(Number(frame.time) - Number(cached.time)) <= 0.25)) {
        drawMask(cached, cached.cropBox, sourceWidth, sourceHeight);
      } else if (cached) {
        cached = null;
      }
      submit(box, sourceWidth, sourceHeight, personId, frame.time);
    }

    function status() { return { enabled: enabled, engine: engine, suspended: suspended || document.hidden, pending: Boolean(inFlight),
      hasMask: Boolean(cached), maskTime: cached ? cached.time : null, refreshMs: refreshMs,
      averageWorkMs: elapsedAverage, submitted: submitted, completed: completed, accepted: accepted, discarded: discarded }; }
    function stopEngine(nextState) {
      clearTimeout(startupTimeout);
      if (worker) worker.terminate();
      worker = null; inFlight = null; engine = nextState; clear();
    }
    function startEngine() { if (!worker && !suspended && !document.hidden) startWorker(); }
    function setSuspended(value) {
      value = Boolean(value); if (suspended === value) return;
      suspended = value;
      if (suspended) stopEngine('suspended'); else if (enabled) startEngine();
      notify();
    }
    function setEnabled(value) {
      enabled = Boolean(value);
      if (enabled) startEngine(); else stopEngine('disabled');
      notify();
    }
    function onVisibility() {
      if (document.hidden) { stopEngine('suspended'); notify(); }
      else if (!suspended && enabled) startEngine();
    }
    function destroy() {
      destroyed = true; document.removeEventListener('visibilitychange', onVisibility);
      stopEngine('destroyed');
    }
    document.addEventListener('visibilitychange', onVisibility);

    if (enabled) startEngine();
    return { update: update, clear: clear, setEnabled: setEnabled, setSuspended: setSuspended, destroy: destroy, status: status };
  }

  root.PersonOutline = { create: create };
})(window);
