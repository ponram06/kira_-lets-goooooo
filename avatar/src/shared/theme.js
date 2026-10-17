/**
 * avatar/src/shared/theme.js
 * M9: CSS classes for the character theme and the mode indicator.
 * Themes are original CSS styling (no third-party artwork):
 *   notebook — black page, red ink action feed
 *   gotham   — dark slate with amber accents
 * Mode glow: blue = Restricted, red = Free (independent of the renderer).
 */

(function (root) {
  const THEMES = ['notebook', 'gotham'];
  const MODES = ['restricted', 'free'];

  /** Class list for #container given theme and mode (bad values → safe defaults). */
  function containerClasses(theme, mode) {
    const t = THEMES.includes(theme) ? theme : 'notebook';
    const m = mode === 'free' ? 'free' : 'restricted';
    return { add: [`theme-${t}`, `mode-${m}`], remove: [...THEMES.filter((x) => x !== t).map((x) => `theme-${x}`), ...MODES.filter((x) => x !== m).map((x) => `mode-${x}`)] };
  }

  function apply(el, theme, mode) {
    const c = containerClasses(theme, mode);
    for (const r of c.remove) el.classList.remove(r);
    for (const a of c.add) el.classList.add(a);
    return c;
  }

  const api = { THEMES, containerClasses, apply };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.KiraTheme = api;
})(typeof window !== 'undefined' ? window : globalThis);
