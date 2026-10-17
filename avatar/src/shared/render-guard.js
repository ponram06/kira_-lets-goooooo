/**
 * avatar/src/shared/render-guard.js
 * Performance guard and render settings for the 3D avatar (M8):
 * - FpsMonitor: fall back to the sprite when the average frame rate stays
 *   below 20 fps for 10 seconds (both configurable);
 * - renderSettings: FPS cap, pixel-ratio cap, antialiasing, low power;
 * - frameDue: FPS limiter; PixelHit: click-through transitions from alpha.
 */

(function (root) {
  class FpsMonitor {
    constructor({ minFps = 20, windowSec = 10 } = {}) {
      this.minFps = minFps;
      this.windowMs = windowSec * 1000;
      this.frames = [];          // timestamps (ms)
      this.belowSince = null;
    }
    /** Record a frame at *now* (ms). Returns true when the sprite fallback should trigger. */
    frame(now) {
      this.frames.push(now);
      const cutoff = now - 1000;
      while (this.frames.length && this.frames[0] < cutoff) this.frames.shift();
      const fps = this.frames.length;                 // frames in the last second
      if (fps < this.minFps) {
        if (this.belowSince === null) this.belowSince = now;
        return now - this.belowSince >= this.windowMs;
      }
      this.belowSince = null;
      return false;
    }
    /** Pause (hidden / asleep): low fps then is intentional, not a failure. */
    reset() { this.frames = []; this.belowSince = null; }
  }

  function renderSettings(opts, devicePixelRatio) {
    const o = opts || {};
    const low = Boolean(o.lowPower);
    const fpsCap = low ? 15 : Math.max(10, Math.min(60, Number(o.fpsCap) || 30));
    const prCap = low ? 1 : Math.max(0.5, Math.min(2, Number(o.pixelRatioCap) || 1.25));
    return {
      fpsCap,
      pixelRatio: Math.min(Number(devicePixelRatio) || 1, prCap),
      antialias: low ? false : Boolean(o.antialias),
      lookAt: low ? false : o.lookAt !== false,
      sleepingFps: 5
    };
  }

  /** True when a new frame should be drawn at *now* given the last drawn time. */
  function frameDue(now, last, fps) {
    return now - last >= 1000 / Math.max(1, fps) - 0.5;
  }

  /**
   * Turns per-mousemove alpha samples into click-through changes. Only
   * reports a change (so IPC is sent only on transitions).
   */
  class PixelHit {
    constructor(threshold = 20) { this.threshold = threshold; this.capturing = false; }
    /** alpha 0..255 (or null = unknown). Returns 'capture' | 'release' | null. */
    update(alpha, overUi) {
      if ((alpha === null || alpha === undefined) && !overUi) return null;   // unknown: keep state
      const want = Boolean(overUi) || (typeof alpha === 'number' && alpha >= this.threshold);
      if (want === this.capturing) return null;
      this.capturing = want;
      return want ? 'capture' : 'release';
    }
  }

  const api = { FpsMonitor, renderSettings, frameDue, PixelHit };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.KiraRenderGuard = api;
})(typeof window !== 'undefined' ? window : globalThis);
