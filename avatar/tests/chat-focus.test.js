/**
 * avatar/tests/chat-focus.test.js
 * Chat box keyboard focus: focusable only while open, never while working.
 */

const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const { ChatFocus, BLUR_GRACE_MS } = require('../src/main/chat-focus');

function fakeWindow({ focusLands = true } = {}) {
  const calls = [];
  let focused = false;
  return {
    calls,
    isDestroyed: () => false,
    isVisible: () => true,
    isFocused: () => focused,
    setFocusable: (v) => calls.push(['setFocusable', v]),
    setIgnoreMouseEvents: (v, o) => calls.push(['ignoreMouse', v, o]),
    show: () => calls.push(['show']),
    focus: () => { calls.push(['focus']); if (focusLands) focused = true; },
    webContents: {
      send: (ch) => calls.push(['send', ch]),
      focus: () => calls.push(['wcFocus'])
    }
  };
}

function controller(win, t = { v: 1000 }) {
  const timers = [];
  const cf = new ChatFocus(() => win, { now: () => t.v, setTimer: (fn) => timers.push(fn) });
  return { cf, timers, t };
}

describe('ChatFocus', () => {
  it('open makes the window focusable, interactive and focused', () => {
    const win = fakeWindow();
    const { cf } = controller(win);
    assert.equal(cf.open('idle'), true);
    const names = win.calls.map((c) => c[0]);
    assert.deepEqual(win.calls[0], ['setFocusable', true]);
    assert.deepEqual(win.calls[1], ['ignoreMouse', false, undefined]);
    assert.ok(names.includes('focus') && names.includes('wcFocus'));
    assert.ok(win.calls.some((c) => c[0] === 'send' && c[1] === 'avatar:open-chat'));
  });

  it('refuses to open while the assistant is working', () => {
    const win = fakeWindow();
    const { cf } = controller(win);
    assert.equal(cf.open('working'), false);
    assert.equal(win.calls.length, 0);
    assert.equal(cf.isOpen, false);
  });

  it('retries focus once if it did not land', () => {
    const win = fakeWindow({ focusLands: false });
    const { cf, timers } = controller(win);
    cf.open('idle');
    const before = win.calls.filter((c) => c[0] === 'focus').length;
    timers.forEach((fn) => fn());
    assert.equal(win.calls.filter((c) => c[0] === 'focus').length, before + 1);
  });

  it('ignores the transient blur right after opening, closes on a real blur', () => {
    const win = fakeWindow();
    const { cf, t } = controller(win);
    cf.open('idle');
    t.v += 50;
    assert.equal(cf.onBlur(), 'refocused');
    assert.equal(cf.isOpen, true);
    t.v += BLUR_GRACE_MS + 1;
    assert.equal(cf.onBlur(), 'closed');
    assert.equal(cf.isOpen, false);
    assert.deepEqual(win.calls.filter((c) => c[0] === 'setFocusable').pop(), ['setFocusable', false]);
  });

  it('close restores click-through and non-focusable', () => {
    const win = fakeWindow();
    const { cf } = controller(win);
    cf.open('idle');
    cf.close();
    const tail = win.calls.slice(-3);
    assert.deepEqual(tail[0], ['setFocusable', false]);
    assert.deepEqual(tail[1], ['ignoreMouse', true, { forward: true }]);
    assert.deepEqual(tail[2], ['send', 'avatar:blur-chat']);
  });

  it('a task starting closes the chat so VS Code keeps focus', () => {
    const win = fakeWindow();
    const { cf } = controller(win);
    cf.open('idle');
    assert.equal(cf.onState('working'), true);
    assert.equal(cf.isOpen, false);
    assert.equal(cf.onState('working'), false);
  });

  it('blur while closed only keeps the window non-focusable', () => {
    const win = fakeWindow();
    const { cf } = controller(win);
    assert.equal(cf.onBlur(), 'ignored');
    assert.deepEqual(win.calls, [['setFocusable', false]]);
  });

  it('copes with a missing window', () => {
    const cf = new ChatFocus(() => null);
    assert.equal(cf.open('idle'), false);
    cf.close();
    assert.equal(cf.onBlur(), 'ignored');
  });
});
