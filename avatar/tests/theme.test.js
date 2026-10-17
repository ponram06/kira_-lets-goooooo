/**
 * avatar/tests/theme.test.js
 * M9 themes and mode indicator classes, and that the CSS defines them.
 */

const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const { containerClasses, apply } = require('../src/shared/theme');

function fakeEl() {
  const set = new Set();
  return { set, classList: { add: (c) => set.add(c), remove: (c) => set.delete(c) } };
}

describe('themes and mode glow', () => {
  it('maps theme and mode to classes', () => {
    assert.deepEqual(containerClasses('gotham', 'free').add, ['theme-gotham', 'mode-free']);
    assert.deepEqual(containerClasses('bogus', 'weird').add, ['theme-notebook', 'mode-restricted']);
  });

  it('switching replaces the old classes', () => {
    const el = fakeEl();
    apply(el, 'notebook', 'restricted');
    apply(el, 'gotham', 'free');
    assert.deepEqual([...el.set].sort(), ['mode-free', 'theme-gotham']);
  });

  it('CSS has both themes and blue/red mode glows', () => {
    const css = fs.readFileSync(path.join(__dirname, '../src/renderer/style.css'), 'utf8');
    for (const sel of ['.theme-notebook', '.theme-gotham', '.mode-restricted', '.mode-free']) assert.ok(css.includes(sel), sel);
    assert.match(css, /mode-restricted[^}]*rgba\(59, 130, 246/);   // blue
    assert.match(css, /mode-free[^}]*rgba\(239, 68, 68/);          // red
  });
});
