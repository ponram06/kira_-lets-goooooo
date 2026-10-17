/**
 * avatar/tests/highlight.test.js
 * M6 target highlight: DPI conversion, bounds, duration, window options.
 */

const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const { highlightBounds, durationMs, showHighlight, RING } = require('../src/main/highlight-overlay');

describe('highlight overlay', () => {
  it('centres the ring on the point, converting physical px to DIP', () => {
    const toDip = (p) => ({ x: p.x / 1.5, y: p.y / 1.5 });     // 150 % scaling
    assert.deepEqual(highlightBounds({ x: 1500, y: 300 }, toDip),
      { x: 1000 - RING / 2, y: 200 - RING / 2, width: RING, height: RING });
  });

  it('supports negative coordinates (monitor left of primary)', () => {
    const b = highlightBounds({ x: -500, y: 10 }, null);
    assert.equal(b.x, -500 - RING / 2);
  });

  it('rejects malformed messages', () => {
    assert.equal(highlightBounds({ x: 'a', y: 1 }), null);
    assert.equal(highlightBounds(null), null);
  });

  it('clamps the duration', () => {
    assert.equal(durationMs({ ms: 10 }), 100);
    assert.equal(durationMs({ ms: 99999 }), 3000);
    assert.equal(durationMs({}), 700);
  });

  it('creates a click-through, non-focusable window that closes itself', () => {
    const made = [];
    let timer = null;
    class FakeWin {
      constructor(opts) { this.opts = opts; this.calls = []; this.closed = false; made.push(this); }
      setIgnoreMouseEvents(v) { this.calls.push(['ignore', v]); }
      setAlwaysOnTop(v, level) { this.calls.push(['top', v, level]); }
      loadURL(u) { this.url = u; }
      once() {}
      isDestroyed() { return this.closed; }
      close() { this.closed = true; }
    }
    showHighlight({ x: 10, y: 10, ms: 500 }, { BrowserWindow: FakeWin, screen: null, setTimer: (fn, ms) => { timer = [fn, ms]; } });
    const w = made[0];
    assert.equal(w.opts.focusable, false);
    assert.equal(w.opts.webPreferences.javascript, false);
    assert.deepEqual(w.calls[0], ['ignore', true]);
    assert.equal(timer[1], 500);
    timer[0]();
    assert.equal(w.closed, true);
  });
});
