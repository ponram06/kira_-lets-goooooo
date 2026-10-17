/**
 * avatar/src/shared/characters.js
 * Character profile validation (M8). Profiles come from config.yaml through
 * the backend snapshot / persona_change events. Unknown or bad values fall
 * back to safe defaults; a bad profile can never break the avatar.
 * Works in Node (tests) and in the renderer (window.KiraCharacters).
 */

(function (root) {
  const RENDERERS = ['sprite', 'vrm'];
  const THEMES = ['notebook', 'gotham'];
  const FRAMINGS = ['bust', 'full'];
  const ID_RE = /^[a-z0-9_-]{1,32}$/;

  const DEFAULT_PROFILE = Object.freeze({
    id: 'kira', displayName: 'Kira', renderer: 'sprite', asset: null,
    voice: '', theme: 'notebook', framing: 'bust'
  });

  function str(v, max) {
    return typeof v === 'string' ? v.trim().slice(0, max) : '';
  }

  /** Returns { profile, errors } — profile is always usable. */
  function validateCharacter(raw) {
    const errors = [];
    const r = raw && typeof raw === 'object' ? raw : {};
    const id = str(r.id, 32).toLowerCase();
    if (!ID_RE.test(id)) errors.push('id');
    let renderer = str(r.renderer, 10).toLowerCase() || 'sprite';
    if (!RENDERERS.includes(renderer)) { errors.push('renderer'); renderer = 'sprite'; }
    let asset = str(r.asset, 1024) || null;
    if (asset && !/\.vrm$/i.test(asset)) { errors.push('asset'); asset = null; }
    if (renderer === 'vrm' && !asset) errors.push('asset_missing');
    let theme = str(r.theme, 16).toLowerCase() || 'notebook';
    if (!THEMES.includes(theme)) { errors.push('theme'); theme = 'notebook'; }
    let framing = str(r.framing, 8).toLowerCase() || 'bust';
    if (!FRAMINGS.includes(framing)) { errors.push('framing'); framing = 'bust'; }
    const profile = {
      id: ID_RE.test(id) ? id : DEFAULT_PROFILE.id,
      displayName: str(r.display_name || r.displayName, 40) || (ID_RE.test(id) ? id : DEFAULT_PROFILE.displayName),
      renderer, asset, voice: str(r.voice, 80), theme, framing
    };
    return { profile, errors };
  }

  /** Which renderer to use right now, and why (for the on-screen notice). */
  function chooseRenderer(profile, env) {
    const e = env || {};
    if (!profile || profile.renderer !== 'vrm') return { renderer: 'sprite', notice: null };
    if (e.lowPower) return { renderer: 'sprite', notice: 'Low-power mode: using the 2D avatar.' };
    if (!profile.asset) return { renderer: 'sprite', notice: 'No 3D model file is set for this character. Using the 2D avatar.' };
    if (e.modelExists === false) return { renderer: 'sprite', notice: '3D model file not found. Using the 2D avatar.' };
    if (e.webgl === false) return { renderer: 'sprite', notice: 'WebGL is not available. Using the 2D avatar.' };
    if (e.failedBefore) return { renderer: 'sprite', notice: 'The 3D avatar failed earlier. Using the 2D avatar.' };
    return { renderer: 'vrm', notice: null };
  }

  const api = { validateCharacter, chooseRenderer, DEFAULT_PROFILE, RENDERERS, THEMES, FRAMINGS };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.KiraCharacters = api;
})(typeof window !== 'undefined' ? window : globalThis);
