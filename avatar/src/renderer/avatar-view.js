/**
 * avatar/src/renderer/avatar-view.js
 * Owns the active avatar renderer (2D sprite or 3D VRM) and switches between
 * them live (M8). The sprite always exists and is the fallback: if the model
 * is missing, WebGL fails, loading throws, or the 3D avatar stays below
 * 20 fps for 10 s, the view switches to the sprite and reports a notice.
 * Also answers "is there a visible pixel here?" for click-through.
 */

(function (root) {
  const isNode = typeof module !== 'undefined' && module.exports;
  const Chars = isNode ? require('../shared/characters') : root.KiraCharacters;

  class AvatarView {
    /**
     * @param {{sprite, spriteCanvas, vrmCanvas, loadModel, importVrm, notice, settings}} deps
     *   loadModel(profile) → Promise<{ok, bytes?, reason?}>  (main process reads the file)
     *   importVrm() → Promise<{createVrmRenderer, webglAvailable}>
     */
    constructor(deps) {
      this.d = deps;
      this.sprite = deps.sprite;
      this.active = deps.sprite;
      this.kind = 'sprite';
      this.vrm = null;
      this.profile = null;
      this.failedIds = new Set();
      this.last = { state: 'idle', mode: 'restricted', sleeping: false, loudness: 0, emotion: null };
      this.switching = null;
      this._show('sprite');
    }

    _show(kind) {
      const { spriteCanvas, vrmCanvas } = this.d;
      if (spriteCanvas && spriteCanvas.style) spriteCanvas.style.display = kind === 'sprite' ? '' : 'none';
      if (vrmCanvas && vrmCanvas.style) vrmCanvas.style.display = kind === 'vrm' ? 'block' : 'none';
    }

    _replay(r) {
      r.setState(this.last.state);
      r.setMode(this.last.mode);
      r.setSleeping(this.last.sleeping);
      if (r.setEmotion) r.setEmotion(this.last.emotion);
    }

    _useSprite(notice) {
      if (this.vrm) { try { this.vrm.dispose(); } catch { /* already gone */ } }
      this.vrm = null;
      this.active = this.sprite;
      this.kind = 'sprite';
      this._replay(this.sprite);
      this.sprite.start();
      this._show('sprite');
      if (notice && this.d.notice) this.d.notice(notice);
    }

    /** Switch to the character *rawProfile* (validated). Resolves to the renderer kind used. */
    async setCharacter(rawProfile) {
      const { profile } = Chars.validateCharacter(rawProfile);
      this.profile = profile;
      const run = (async () => {
        const settings = (this.d.settings && this.d.settings()) || {};
        let choice = Chars.chooseRenderer(profile, { lowPower: settings.lowPower, failedBefore: this.failedIds.has(profile.id) });
        if (choice.renderer === 'sprite') { this._useSprite(this.kind === 'vrm' || profile.renderer === 'vrm' ? choice.notice : null); return 'sprite'; }

        let mod;
        try { mod = await this.d.importVrm(); } catch { mod = null; }
        if (!mod || (mod.webglAvailable && !mod.webglAvailable())) {
          choice = Chars.chooseRenderer(profile, { webgl: false });
          this._useSprite(choice.notice);
          return 'sprite';
        }
        const file = await this.d.loadModel(profile);
        if (!file || !file.ok) {
          this._useSprite(file && file.reason ? `${file.reason} Using the 2D avatar.` : Chars.chooseRenderer(profile, { modelExists: false }).notice);
          return 'sprite';
        }
        let core;
        try {
          core = await mod.createVrmRenderer(this.d.vrmCanvas, file.bytes, {
            ...settings, framing: profile.framing,
            onFallback: (reason) => this._onVrmFailure(profile.id, reason)
          });
        } catch {
          this.failedIds.add(profile.id);
          this._useSprite('The 3D model could not be loaded. Using the 2D avatar.');
          return 'sprite';
        }
        if (this.profile !== profile) { core.dispose(); return this.kind; }   // switched again meanwhile
        if (this.vrm) this.vrm.dispose();
        this.sprite.stop();
        this.vrm = core;
        this.active = core;
        this.kind = 'vrm';
        this._replay(core);
        core.start();
        this._show('vrm');
        return 'vrm';
      })();
      this.switching = run;
      return run;
    }

    _onVrmFailure(id, reason) {
      this.failedIds.add(id);
      const why = reason === 'slow' ? 'The 3D avatar was too slow on this PC'
        : reason === 'context_lost' ? 'The graphics context was lost' : 'The 3D avatar failed';
      this._useSprite(`${why}. Switched to the 2D avatar.`);
    }

    // ── delegated renderer interface ────────────────────────────────────
    setState(s) { this.last.state = s; this.active.setState(s); }
    setMode(m) { this.last.mode = m; this.active.setMode(m); }
    setSleeping(v) { this.last.sleeping = v; this.active.setSleeping(v); }
    setLoudness(v) { this.last.loudness = v; this.active.setLoudness(v); }
    setEmotion(e) { this.last.emotion = e; if (this.active.setEmotion) this.active.setEmotion(e); }
    setVisible(v) { if (this.active.setVisible) this.active.setVisible(v); }
    setLook(nx, ny) { if (this.active.setLook) this.active.setLook(nx, ny); }
    gestureEvent(type) { if (this.active.gestureEvent) this.active.gestureEvent(type); }

    /** Alpha (0..255) under canvas point (x, y), or null when unknown. */
    alphaAt(x, y) {
      if (this.kind === 'vrm' && this.vrm) {
        this.vrm.requestHit(x, y);       // read after the next frame
        return this.vrm.hitAlpha;
      }
      const c = this.d.spriteCanvas;
      try {
        const ctx = c.getContext('2d');
        return ctx.getImageData(Math.floor(x), Math.floor(y), 1, 1).data[3];
      } catch {
        return null;
      }
    }

    dispose() {
      if (this.vrm) this.vrm.dispose();
      this.vrm = null;
      this.sprite.stop();
    }
  }

  const api = { AvatarView };
  if (isNode) module.exports = api;
  else root.KiraAvatarView = api;
})(typeof window !== 'undefined' ? window : globalThis);
