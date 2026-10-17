/**
 * avatar/src/renderer/vrm-core.js
 * 3D VRM avatar renderer (M8). three.js, GLTFLoader and three-vrm are
 * injected (see vrm-boot.js), so the whole class runs in Node tests with a
 * mocked WebGL. Same interface as SpriteRenderer plus alpha hit testing.
 *
 * Resource rules: FPS cap (default 30, 5 while asleep, 0 while hidden),
 * antialiasing off by default, pixel-ratio cap, 'low-power' GPU preference,
 * full disposal of geometry, materials, textures, listeners and the context.
 */

(function (root) {
  const isNode = typeof module !== 'undefined' && module.exports;
  const Anim = isNode ? require('../shared/avatar-anim') : root.KiraAnim;
  const Expr = isNode ? require('../shared/expressions') : root.KiraExpressions;
  const Guard = isNode ? require('../shared/render-guard') : root.KiraRenderGuard;

  class VrmCore {
    /**
     * @param {HTMLCanvasElement} canvas
     * @param {{THREE, GLTFLoader, VRMLoaderPlugin, VRMUtils, raf?, caf?, now?, devicePixelRatio?}} deps
     * @param {{fpsCap?, pixelRatioCap?, antialias?, lowPower?, framing?, minFps?, fallbackSec?, onFallback?}} opts
     */
    constructor(canvas, deps, opts = {}) {
      this.canvas = canvas;
      this.d = deps;
      this.T = deps.THREE;
      this.opts = opts;
      this.settings = Guard.renderSettings(opts, deps.devicePixelRatio);
      this.raf = deps.raf || ((fn) => root.requestAnimationFrame(fn));
      this.caf = deps.caf || ((id) => root.cancelAnimationFrame(id));
      this.now = deps.now || (() => root.performance.now());
      this.onFallback = opts.onFallback || (() => {});
      this.fps = new Guard.FpsMonitor({ minFps: opts.minFps || 20, windowSec: opts.fallbackSec || 10 });
      this.blinker = new Anim.Blinker(deps.rng || Math.random, 0);

      this.state = 'idle';
      this.mode = 'restricted';
      this.sleeping = false;
      this.visible = true;
      this.loudness = 0;
      this.emotion = null;
      this.emotionWeights = {};
      this.pose = { ...Anim.POSES.idle };
      this.look = { yaw: 0, pitch: 0 };
      this.lookGoal = { yaw: 0, pitch: 0 };
      this.gesture = null;           // { name, start, duration }
      this.hitRequest = null;
      this.hitAlpha = null;
      this.vrm = null;
      this.expressionNames = new Set();
      this.running = false;
      this.rafId = null;
      this.lastFrame = 0;
      this.t0 = this.now();
      this.failed = false;
      this._onLost = (e) => { if (e && e.preventDefault) e.preventDefault(); this._fail('context_lost'); };
    }

    init() {
      const T = this.T;
      this.renderer = new T.WebGLRenderer({
        canvas: this.canvas, alpha: true, antialias: this.settings.antialias,
        powerPreference: 'low-power', premultipliedAlpha: true, preserveDrawingBuffer: false
      });
      this.renderer.setPixelRatio(this.settings.pixelRatio);
      this.renderer.setSize(this.canvas.width, this.canvas.height, false);
      this.renderer.setClearColor(0x000000, 0);
      this.scene = new T.Scene();
      this.camera = new T.PerspectiveCamera(30, this.canvas.width / this.canvas.height, 0.1, 20);
      const light = new T.DirectionalLight(0xffffff, 2.2);
      light.position.set(1, 1.5, 2);
      this.scene.add(light);
      this.scene.add(new T.AmbientLight(0xffffff, 0.8));
      if (this.canvas.addEventListener) this.canvas.addEventListener('webglcontextlost', this._onLost, false);
      return this;
    }

    async load(bytes) {
      const loader = new this.d.GLTFLoader();
      loader.register((parser) => new this.d.VRMLoaderPlugin(parser));
      const gltf = await loader.parseAsync(bytes, '');
      const vrm = gltf && gltf.userData && gltf.userData.vrm;
      if (!vrm) throw new Error('The file is not a VRM model.');
      const U = this.d.VRMUtils || {};
      if (U.removeUnnecessaryVertices) U.removeUnnecessaryVertices(gltf.scene);
      if (U.combineSkeletons) U.combineSkeletons(gltf.scene);
      if (U.rotateVRM0) U.rotateVRM0(vrm);       // VRM 0.x faces the other way
      vrm.scene.traverse((o) => { o.frustumCulled = false; });
      this.vrm = vrm;
      this.scene.add(vrm.scene);
      const em = vrm.expressionManager;
      this.expressionNames = new Set((em && em.expressions ? em.expressions : []).map((e) => e.expressionName));
      this._frameCamera();
      return this;
    }

    _bone(name) {
      const h = this.vrm && this.vrm.humanoid;
      return h && h.getNormalizedBoneNode ? h.getNormalizedBoneNode(name) : null;
    }

    _frameCamera() {
      const T = this.T;
      const head = this._bone('head');
      const p = new T.Vector3(0, 1.4, 0);
      if (head && head.getWorldPosition) { this.vrm.update(0); head.getWorldPosition(p); }
      if (this.opts.framing === 'full') {
        const h = Math.max(0.5, p.y * 1.12);
        this.camera.position.set(0, h * 0.52, h * 2.2);
        this.camera.lookAt(0, h * 0.5, 0);
      } else {
        this.camera.position.set(0, p.y - 0.02, 0.95);
        this.camera.lookAt(0, p.y - 0.12, 0);
      }
      this.camera.updateProjectionMatrix();
    }

    // ── public interface (same as SpriteRenderer) ──────────────────────
    setState(s) {
      if (typeof s !== 'string') return;
      this.state = s;
      if (!this.emotion && Anim.STATE_EMOTION[s]) this.emotionTarget = Anim.STATE_EMOTION[s];
    }
    setMode(m) { this.mode = m === 'free' ? 'free' : 'restricted'; }
    setLoudness(v) { this.loudness = Math.min(1, Math.max(0, Number(v) || 0)); }
    setSleeping(s) { this.sleeping = Boolean(s); this.fps.reset(); }
    setVisible(v) { this.visible = Boolean(v); this.fps.reset(); }
    setEmotion(e) { this.emotion = Expr.EMOTIONS.includes(e) ? e : null; this.emotionTarget = this.emotion; }
    setLook(nx, ny) { if (this.settings.lookAt) this.lookGoal = Anim.lookTarget(nx, ny); }
    gestureEvent(type) {
      const g = Anim.gestureFor(type);
      if (g) this.gesture = { ...g, start: (this.now() - this.t0) / 1000 };
    }
    /** Ask for the alpha under (x, y) in canvas pixels; read after the next frame via hitAlpha. */
    requestHit(x, y) { this.hitRequest = { x: Math.round(x), y: Math.round(y) }; }

    start() {
      if (this.running) return;
      this.running = true;
      const loop = () => {
        if (!this.running) return;
        this.frame(this.now());
        this.rafId = this.raf(loop);
      };
      this.rafId = this.raf(loop);
    }

    stop() {
      this.running = false;
      if (this.rafId !== null) this.caf(this.rafId);
      this.rafId = null;
    }

    /** Draw one frame if due. Returns true if a frame was drawn. */
    frame(now) {
      if (this.failed || !this.vrm || !this.visible) return false;
      const cap = this.sleeping ? this.settings.sleepingFps : this.settings.fpsCap;
      if (this.lastFrame && !Guard.frameDue(now, this.lastFrame, cap)) return false;
      const dt = this.lastFrame ? Math.min(0.1, (now - this.lastFrame) / 1000) : 1 / cap;
      this.lastFrame = now;
      const t = (now - this.t0) / 1000;
      this._animate(t, dt);
      this.vrm.update(dt);
      this.renderer.render(this.scene, this.camera);
      this._readHit();
      if (!this.sleeping && this.fps.frame(now)) this._fail('slow');
      return true;
    }

    _animate(t, dt) {
      const target = Anim.stateToPose(this.sleeping ? 'sleeping' : this.state);
      this.pose = Anim.blendPose(this.pose, target, dt);
      const idle = Anim.idleMotion(t, this.pose.energy);
      this.look = Anim.smoothLook(this.look, this.lookGoal, dt);
      let g = { headPitch: 0, headYaw: 0, headRoll: 0 };
      if (this.gesture) {
        const p = (t - this.gesture.start) / this.gesture.duration;
        if (p > 1) this.gesture = null;
        else g = Anim.gestureOffset(this.gesture.name, p);
      }
      const head = this._bone('head');
      if (head) {
        head.rotation.x = this.pose.headPitch + idle.nod + g.headPitch + this.look.pitch * 0.6;
        head.rotation.y = this.pose.headYaw + g.headYaw + this.look.yaw * 0.6;
        head.rotation.z = g.headRoll;
      }
      const spine = this._bone('spine');
      if (spine) spine.rotation.x = this.pose.spineLean;
      const chest = this._bone('chest') || this._bone('upperChest');
      if (chest && chest.scale) chest.scale.setScalar(1 + idle.breath);
      const hips = this._bone('hips');
      if (hips) hips.rotation.z = idle.sway;
      const la = this._bone('leftUpperArm');
      const ra = this._bone('rightUpperArm');
      if (la) la.rotation.z = -this.pose.armDown;
      if (ra) ra.rotation.z = this.pose.armDown;

      const em = this.vrm.expressionManager;
      if (!em) return;
      this.emotionWeights = Expr.blendEmotions(this.emotionWeights, this.emotionTarget || null, dt);
      for (const e of Expr.EMOTIONS) {
        const n = Expr.resolveName(e, this.expressionNames);
        if (n) em.setValue(n, this.emotionWeights[e]);
      }
      const speaking = this.state === 'speaking';
      const vis = Expr.lipSync(speaking ? this.loudness : 0, t);
      for (const v of Expr.VISEMES) {
        const n = Expr.resolveName(v, this.expressionNames);
        if (n) em.setValue(n, vis[v]);
      }
      const blink = Expr.resolveName('blink', this.expressionNames);
      if (blink) em.setValue(blink, this.sleeping ? 1 : this.blinker.value(t));
    }

    _readHit() {
      if (!this.hitRequest) return;
      const { x, y } = this.hitRequest;
      this.hitRequest = null;
      try {
        const gl = this.renderer.getContext();
        const pr = this.settings.pixelRatio;
        const px = new Uint8Array(4);
        const glY = Math.round((this.canvas.height - y) * pr) - 1;
        gl.readPixels(Math.round(x * pr), glY, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px);
        this.hitAlpha = px[3];
      } catch {
        this.hitAlpha = null;
      }
    }

    _fail(reason) {
      if (this.failed) return;
      this.failed = true;
      this.stop();
      this.onFallback(reason);
    }

    dispose() {
      this.stop();
      if (this.canvas.removeEventListener) this.canvas.removeEventListener('webglcontextlost', this._onLost, false);
      if (this.scene) {
        this.scene.traverse((o) => {
          if (o.geometry && o.geometry.dispose) o.geometry.dispose();
          const mats = Array.isArray(o.material) ? o.material : (o.material ? [o.material] : []);
          for (const m of mats) {
            for (const key of Object.keys(m)) {
              const v = m[key];
              if (v && v.isTexture && v.dispose) v.dispose();
            }
            if (m.dispose) m.dispose();
          }
        });
      }
      const U = this.d.VRMUtils || {};
      if (this.vrm && U.deepDispose) U.deepDispose(this.vrm.scene);
      if (this.renderer) {
        this.renderer.dispose();
        if (this.renderer.forceContextLoss) this.renderer.forceContextLoss();
      }
      this.vrm = null;
      this.scene = null;
      this.renderer = null;
    }
  }

  const api = { VrmCore };
  if (isNode) module.exports = api;
  else root.KiraVrmCore = api;
})(typeof window !== 'undefined' ? window : globalThis);
