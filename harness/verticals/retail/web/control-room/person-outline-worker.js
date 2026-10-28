/* Official MediaPipe CPU inference, isolated from the player thread. */
'use strict';
let segmenter = null;

// Build the edge where the CPU mask already lives. The player thread receives a
// ready texture instead of scanning the full confidence map on every refresh.
function edgeTexture(values, size, rect) {
  const [rx, ry, rw, rh] = rect || [0, 0, size, size];
  const foreground = new Uint8Array(size * size);
  const pixels = new Uint8ClampedArray(size * size * 4);
  let foregroundPixels = 0;
  for (let y = 0; y < size; y++) {
    const sy = Math.max(0, Math.min(size - 1, Math.floor(ry + y * rh / size)));
    for (let x = 0; x < size; x++) {
      const sx = Math.max(0, Math.min(size - 1, Math.floor(rx + x * rw / size)));
      const inside = values[sy * size + sx] > .5;
      foreground[y * size + x] = inside ? 1 : 0;
      if (inside) foregroundPixels++;
    }
  }
  for (let y = 1; y < size - 1; y++) for (let x = 1; x < size - 1; x++) {
    const i = y * size + x;
    if (!foreground[i] || (foreground[i-1] && foreground[i+1] && foreground[i-size] && foreground[i+size])) continue;
    pixels.set([255, 218, 68, 225], i * 4);
  }
  return {pixels, foregroundPixels};
}

self.onmessage = async function (event) {
  const message = event.data || {};
  try {
    if (message.kind === 'init') {
      const assets = message.assets;
      for (const value of Object.values(assets)) {
        if (new URL(value, self.location.href).origin !== self.location.origin) {
          throw new Error('Person outline assets must use this app origin.');
        }
      }
      const vision = await import(assets.moduleUrl);
      const files = await vision.FilesetResolver.forVisionTasks(assets.wasmRoot);
      segmenter = await vision.ImageSegmenter.createFromOptions(files, {
        baseOptions: { modelAssetPath: assets.modelAssetPath, delegate: 'CPU' },
        runningMode: 'IMAGE', outputConfidenceMasks: true, outputCategoryMask: false
      });
      self.postMessage({ kind: 'ready' });
    } else if (message.kind === 'frame') {
      if (!segmenter) throw new Error('Person outline is still loading.');
      const started = performance.now();
      const canvas = new OffscreenCanvas(message.size, message.size);
      if (message.sourceFrame) {
        const c = message.cropBox, r = message.contentRect;
        canvas.getContext('2d').drawImage(message.bitmap, c.x, c.y, c.x2-c.x, c.y2-c.y, r[0], r[1], r[2], r[3]);
      } else canvas.getContext('2d').drawImage(message.bitmap, 0, 0);
      message.bitmap.close();
      const result = segmenter.segment(canvas);
      try {
        const mask = result.confidenceMasks.at(-1);
        if (mask.width !== message.size || mask.height !== message.size) {
          throw new Error('Unexpected person mask dimensions.');
        }
        const edge = edgeTexture(mask.getAsFloat32Array(), message.size, message.contentRect);
        self.postMessage({ kind: 'mask', seq: message.seq, personId: message.personId,
          time: message.time, cropBox: message.cropBox, sourceWidth: message.sourceWidth,
          sourceHeight: message.sourceHeight, contentRect: message.contentRect,
          edgePixels: edge.pixels.buffer, foregroundPixels: edge.foregroundPixels, workMs: performance.now() - started }, [edge.pixels.buffer]);
      } finally { result.close(); }
    }
  } catch (error) {
    if (message.bitmap) message.bitmap.close();
    self.postMessage({ kind: 'error', message: String(error) });
  }
};
