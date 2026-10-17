/**
 * avatar/tests/vrm.test.js
 * M8 3D avatar without a GPU: character validation, renderer choice and
 * fallback, animation mapping, expression blending, lip sync, FPS guard,
 * pixel hit testing, VrmCore with a mocked three.js/WebGL, model file
 * checks, and the CSP hash of the import map.
 */

const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');

const { validateCharacter, chooseRenderer } = require('../src/shared/characters');
const Anim = require('../src/shared/avatar-anim');
const Expr = require('../src/shared/expressions');
const { FpsMonitor, renderSettings, frameDue, PixelHit } = require('../src/shared/render-guard');
const { VrmCore } = require('../src/renderer/vrm-core');
const { AvatarView } = require('../src/renderer/avatar-view');
const { readModel, resolveModelPath, safeAppPath } = require('../src/main/model-file');

// ── mocked three.js ─────────────────────────────────────────────────────

function fakeThree(log = []) {
  class Vec { constructor(x = 0, y = 0, z = 0) { this.x = x; this.y = y; this.z = z; } set(x, y, z) { this.x = x; this.y = y; this.z = z; return this; } setScalar(v) { this.x = this.y = this.z = v; } }
  class Obj {
    constructor() { this.children = []; this.position = new Vec(); this.rotation = new Vec(); this.scale = new Vec(1, 1, 1); }
    add(o) { this.children.push(o); }
    traverse(fn) { fn(this); for (const c of this.children) c.traverse ? c.traverse(fn) : fn(c); }
  }
  return {
    log,
    Vector3: Vec,
    Scene: Obj,
    DirectionalLight: class extends Obj {},
    AmbientLight: class extends Obj {},
    PerspectiveCamera: class extends Obj { lookAt() {} updateProjectionMatrix() { log.push('projection'); } },
    WebGLRenderer: class {
      constructor(opts) { this.opts = opts; log.push(['renderer', opts]); }
      setPixelRatio(r) { log.push(['pixelRatio', r]); }
      setSize() {}
      setClearColor() {}
      render() { log.push('render'); }
      getContext() { return { RGBA: 1, UNSIGNED_BYTE: 2, readPixels: (x, y, w, h, f, t, px) => { px[3] = 200; } }; }
      dispose() { log.push('renderer.dispose'); }
      forceContextLoss() { log.push('contextLoss'); }
    },
    Obj
  };
}

function fakeVrm(T, names = ['happy', 'aa', 'ih', 'ou', 'ee', 'oh', 'blink']) {
  const bones = {};
  const bone = (n) => (bones[n] = bones[n] || Object.assign(new T.Obj(), { getWorldPosition: (v) => v.set(0, 1.45, 0) }));
  const values = {};
  const geometry = { dispose: () => T.log.push('geometry.dispose') };
  const tex = { isTexture: true, dispose: () => T.log.push('texture.dispose') };
  const material = { map: tex, dispose: () => T.log.push('material.dispose') };
  const scene = new T.Obj();
  const mesh = new T.Obj();
  mesh.geometry = geometry;
  mesh.material = material;
  scene.add(mesh);
  return {
    scene, values, bones,
    humanoid: { getNormalizedBoneNode: bone },
    expressionManager: { expressions: names.map((expressionName) => ({ expressionName })), setValue: (n, v) => { values[n] = v; } },
    update: () => T.log.push('vrm.update')
  };
}

function fakeDeps(T, vrm, extra = {}) {
  const rafs = [];
  return {
    THREE: T,
    GLTFLoader: class { register(fn) { this.plugin = fn({}); } async parseAsync() { if (extra.loadError) throw new Error('bad'); return { scene: vrm.scene, userData: { vrm } }; } },
    VRMLoaderPlugin: class {},
    VRMUtils: { rotateVRM0: () => T.log.push('rotateVRM0'), deepDispose: () => T.log.push('deepDispose') },
    raf: (fn) => { rafs.push(fn); return rafs.length; },
    caf: () => T.log.push('caf'),
    now: extra.now || (() => 0),
    devicePixelRatio: 2,
    rng: () => 0.5,
    rafs
  };
}

function fakeCanvas() {
  const listeners = {};
  return {
    width: 380, height: 310, listeners,
    addEventListener: (n, f) => { listeners[n] = f; },
    removeEventListener: (n) => { delete listeners[n]; }
  };
}

// ── characters ──────────────────────────────────────────────────────────

describe('character profiles', () => {
  it('validates and fills defaults', () => {
    const { profile, errors } = validateCharacter({ id: 'kira', display_name: 'Kira', renderer: 'vrm', asset: 'models/kira.vrm', theme: 'gotham' });
    assert.deepEqual(errors, []);
    assert.equal(profile.renderer, 'vrm');
    assert.equal(profile.framing, 'bust');
  });

  it('repairs bad values', () => {
    const { profile, errors } = validateCharacter({ id: 'BAD ID!', renderer: 'webgpu', asset: 'x.exe', theme: 'pink', framing: 'feet' });
    assert.equal(profile.renderer, 'sprite');
    assert.equal(profile.asset, null);
    assert.equal(profile.theme, 'notebook');
    assert.ok(errors.includes('renderer') && errors.includes('asset') && errors.includes('id'));
  });

  it('chooses the sprite with a clear notice when 3D cannot run', () => {
    const p = validateCharacter({ id: 'k', renderer: 'vrm', asset: 'a.vrm' }).profile;
    assert.equal(chooseRenderer(p, {}).renderer, 'vrm');
    for (const env of [{ lowPower: true }, { modelExists: false }, { webgl: false }, { failedBefore: true }]) {
      const c = chooseRenderer(p, env);
      assert.equal(c.renderer, 'sprite');
      assert.ok(c.notice.length > 10);
    }
    assert.equal(chooseRenderer(validateCharacter({ id: 'r', renderer: 'vrm' }).profile, {}).renderer, 'sprite');
  });
});

// ── animation / expressions / lip sync ─────────────────────────────────

describe('animation mapping', () => {
  it('maps every state to a pose, unknown to idle', () => {
    for (const s of ['idle', 'listening', 'thinking', 'speaking', 'error', 'working']) assert.ok(Anim.stateToPose(s));
    assert.equal(Anim.stateToPose('weird'), Anim.POSES.idle);
  });

  it('blends poses smoothly and converges', () => {
    let pose = { ...Anim.POSES.idle };
    for (let i = 0; i < 200; i++) pose = Anim.blendPose(pose, Anim.POSES.thinking, 1 / 30);
    assert.ok(Math.abs(pose.headYaw - Anim.POSES.thinking.headYaw) < 1e-3);
    const one = Anim.blendPose(Anim.POSES.idle, Anim.POSES.thinking, 1 / 30);
    assert.ok(Math.abs(one.headYaw) < Math.abs(Anim.POSES.thinking.headYaw));   // no snapping
  });

  it('idle breathing and sway are small and periodic', () => {
    const a = Anim.idleMotion(0);
    const b = Anim.idleMotion(4);
    assert.ok(Math.abs(a.breath - b.breath) < 1e-9);
    assert.ok(Math.abs(Anim.idleMotion(1).breath) <= 0.012 + 1e-9);
  });

  it('blinks with gaps of 2-6 s', () => {
    const bl = new Anim.Blinker(() => 0.5, 0);   // first blink at 4 s
    assert.equal(bl.value(3.9), 0);
    assert.ok(bl.value(4.07) > 0.5);
    assert.equal(bl.value(4.2), 0);
    assert.ok(bl.next > 4.2);
  });

  it('look-at is clamped and smoothed', () => {
    const t = Anim.lookTarget(5, -5);
    assert.equal(t.yaw, Anim.LOOK_LIMIT.yaw);
    const s = Anim.smoothLook({ yaw: 0, pitch: 0 }, t, 1 / 30);
    assert.ok(s.yaw > 0 && s.yaw < t.yaw);
  });

  it('gestures for task events', () => {
    assert.equal(Anim.gestureFor('tool_start').name, 'nod');
    assert.equal(Anim.gestureFor('tool_done').name, 'tilt');
    assert.equal(Anim.gestureFor('tool_error').name, 'shake');
    assert.equal(Anim.gestureFor('loudness'), null);
    assert.deepEqual(Anim.gestureOffset('nod', 2), { headPitch: 0, headYaw: 0, headRoll: 0 });
  });
});

describe('expressions and lip sync', () => {
  it('parses emotion tags and strips them', () => {
    assert.deepEqual(Expr.parseEmotionTag('[emotion:happy] Done!'), { emotion: 'happy', text: 'Done!' });
    assert.deepEqual(Expr.parseEmotionTag('[emotion:evil] hi'), { emotion: null, text: 'hi' });
  });

  it('resolves VRM 1.0 and 0.x names', () => {
    assert.equal(Expr.resolveName('happy', ['happy']), 'happy');
    assert.equal(Expr.resolveName('happy', ['Joy', 'A']), 'Joy');
    assert.equal(Expr.resolveName('aa', ['A']), 'A');
    assert.equal(Expr.resolveName('relaxed', new Set(['fun'])), 'fun');
    assert.equal(Expr.resolveName('sad', []), null);
  });

  it('blends toward the target emotion without exceeding 1 in total', () => {
    let w = {};
    for (let i = 0; i < 60; i++) w = Expr.blendEmotions(w, 'happy', 1 / 30);
    assert.ok(w.happy > 0.95);
    for (let i = 0; i < 10; i++) w = Expr.blendEmotions(w, 'sad', 1 / 30);
    const sum = Object.values(w).reduce((a, b) => a + b, 0);
    assert.ok(sum <= 1 + 1e-9 && w.sad > 0 && w.happy < 0.95);
  });

  it('lip sync opens with loudness and closes in silence', () => {
    assert.deepEqual(Expr.lipSync(0, 1), { aa: 0, ih: 0, ou: 0, ee: 0, oh: 0 });
    const v = Expr.lipSync(0.5, 0.05);
    assert.ok(Math.max(...Object.values(v)) > 0.5);
    const shapes = new Set();
    for (let t = 0; t < 1.2; t += 0.12) {
      const w = Expr.lipSync(0.6, t + 0.01);
      shapes.add(Object.keys(w).reduce((a, b) => (w[a] >= w[b] ? a : b)));
    }
    assert.ok(shapes.size >= 3, 'vowels should vary');
  });
});

// ── performance guard ───────────────────────────────────────────────────

describe('render guard', () => {
  it('falls back only after 10 s below 20 fps', () => {
    const m = new FpsMonitor({ minFps: 20, windowSec: 10 });
    let t = 0;
    let fired = false;
    for (; t < 9000; t += 100) fired = m.frame(t) || fired;   // 10 fps for 9 s
    assert.equal(fired, false);
    for (; t < 12000; t += 100) fired = m.frame(t) || fired;
    assert.equal(fired, true);
  });

  it('a good frame rate resets the timer', () => {
    const m = new FpsMonitor();
    let t = 0;
    for (; t < 8000; t += 100) m.frame(t);
    for (; t < 10000; t += 30) m.frame(t);   // ~33 fps
    let fired = false;
    for (; t < 17000; t += 100) fired = m.frame(t) || fired;
    assert.equal(fired, false);
  });

  it('settings: 30 fps, AA off, pixel ratio capped; low power is lighter', () => {
    assert.deepEqual(renderSettings({}, 3), { fpsCap: 30, pixelRatio: 1.25, antialias: false, lookAt: true, sleepingFps: 5 });
    const low = renderSettings({ lowPower: true, antialias: true, fpsCap: 60 }, 2);
    assert.equal(low.fpsCap, 15);
    assert.equal(low.pixelRatio, 1);
    assert.equal(low.antialias, false);
    assert.equal(low.lookAt, false);
  });

  it('frame limiter', () => {
    assert.equal(frameDue(10, 0, 30), false);
    assert.equal(frameDue(34, 0, 30), true);
  });

  it('pixel hit reports transitions only and ignores unknown samples', () => {
    const h = new PixelHit(20);
    assert.equal(h.update(0, false), null);
    assert.equal(h.update(255, false), 'capture');
    assert.equal(h.update(200, false), null);
    assert.equal(h.update(null, false), null);
    assert.equal(h.update(5, false), 'release');
    assert.equal(h.update(0, true), 'capture');
  });
});

// ── VrmCore with mocked WebGL ───────────────────────────────────────────

describe('VrmCore (mocked three.js)', () => {
  async function make(opts = {}, extra = {}) {
    const T = fakeThree();
    const vrm = fakeVrm(T, extra.names);
    let now = 0;
    const deps = fakeDeps(T, vrm, { ...extra, now: () => now });
    const core = new VrmCore(fakeCanvas(), deps, opts).init();
    await core.load(new ArrayBuffer(8));
    return { core, T, vrm, deps, tick: (ms) => { now += ms; return core.frame(now); } };
  }

  it('creates a low-power, transparent renderer with AA off and capped pixel ratio', async () => {
    const { T } = await make();
    const opts = T.log.find((e) => e[0] === 'renderer')[1];
    assert.equal(opts.alpha, true);
    assert.equal(opts.antialias, false);
    assert.equal(opts.powerPreference, 'low-power');
    assert.deepEqual(T.log.find((e) => e[0] === 'pixelRatio'), ['pixelRatio', 1.25]);
    assert.ok(T.log.includes('rotateVRM0'));
  });

  it('caps the frame rate at 30 fps', async () => {
    const { core, tick } = await make();
    let drawn = 0;
    for (let i = 0; i < 60; i++) if (tick(1000 / 60)) drawn++;   // 1 s of 60 Hz rAF
    assert.ok(drawn >= 29 && drawn <= 31, `drew ${drawn}`);
    core.dispose();
  });

  it('pauses rendering when hidden and slows to 5 fps asleep', async () => {
    const { core, tick } = await make();
    core.setVisible(false);
    assert.equal(tick(100), false);
    core.setVisible(true);
    core.setSleeping(true);
    let drawn = 0;
    for (let i = 0; i < 60; i++) if (tick(1000 / 60)) drawn++;
    assert.ok(drawn <= 6, `drew ${drawn} while asleep`);
  });

  it('drives lip sync and emotions through the expression manager', async () => {
    const { core, vrm, tick } = await make();
    core.setState('speaking');
    core.setLoudness(0.7);
    core.setEmotion('happy');
    for (let i = 0; i < 30; i++) tick(34);
    const mouth = Math.max(vrm.values.aa, vrm.values.ih, vrm.values.ou, vrm.values.ee, vrm.values.oh);
    assert.ok(mouth > 0.3);
    assert.ok(vrm.values.happy > 0.5);
    core.setState('idle');
    tick(34);
    assert.equal(vrm.values.ih + vrm.values.ou + vrm.values.ee + vrm.values.oh + vrm.values.aa, 0);
  });

  it('supports VRM 0.x expression names', async () => {
    const { core, vrm, tick } = await make({}, { names: ['Joy', 'A', 'I', 'U', 'E', 'O', 'Blink'] });
    core.setState('speaking');
    core.setLoudness(1);
    core.setEmotion('happy');
    for (let i = 0; i < 30; i++) tick(34);
    assert.ok(vrm.values.Joy > 0.5);
    assert.ok(['A', 'I', 'U', 'E', 'O'].some((n) => vrm.values[n] > 0));
  });

  it('applies the state pose to the head bone', async () => {
    const { core, vrm, tick } = await make();
    core.setState('thinking');
    for (let i = 0; i < 90; i++) tick(34);
    assert.ok(vrm.bones.head.rotation.y < -0.1);
  });

  it('reads alpha under the cursor after a frame', async () => {
    const { core, tick } = await make();
    core.requestHit(100, 100);
    tick(34);
    assert.equal(core.hitAlpha, 200);
  });

  it('falls back when slow, and on context loss', async () => {
    let reason = null;
    const { core, tick } = await make({ onFallback: (r) => { reason = r; } });
    for (let i = 0; i < 130; i++) tick(100);       // 10 fps for 13 s
    assert.equal(reason, 'slow');
    assert.equal(core.failed, true);

    let r2 = null;
    const b = await make({ onFallback: (r) => { r2 = r; } });
    b.core.canvas.listeners.webglcontextlost({ preventDefault() {} });
    assert.equal(r2, 'context_lost');
  });

  it('disposes geometry, materials, textures, listeners and the context', async () => {
    const { core, T } = await make();
    core.start();
    core.dispose();
    for (const k of ['geometry.dispose', 'material.dispose', 'texture.dispose', 'deepDispose', 'renderer.dispose', 'contextLoss', 'caf']) {
      assert.ok(T.log.includes(k), `missing ${k}`);
    }
    assert.equal(core.canvas.listeners.webglcontextlost, undefined);
    assert.equal(core.frame(1e9), false);
  });

  it('rejects a glTF that is not a VRM', async () => {
    const T = fakeThree();
    const deps = fakeDeps(T, fakeVrm(T));
    deps.GLTFLoader = class { register() {} async parseAsync() { return { scene: {}, userData: {} }; } };
    const core = new VrmCore(fakeCanvas(), deps, {}).init();
    await assert.rejects(core.load(new ArrayBuffer(4)), /not a VRM/);
  });
});

// ── AvatarView: switching and automatic fallback ────────────────────────

function fakeSprite() {
  const s = { calls: [], started: 0, stopped: 0 };
  for (const m of ['setState', 'setMode', 'setSleeping', 'setLoudness']) s[m] = (v) => s.calls.push([m, v]);
  s.start = () => { s.started++; };
  s.stop = () => { s.stopped++; };
  return s;
}

function fakeCore() {
  const c = { calls: [], disposed: false, started: false, hitAlpha: 77 };
  for (const m of ['setState', 'setMode', 'setSleeping', 'setLoudness', 'setEmotion', 'setVisible', 'setLook', 'gestureEvent']) c[m] = (v) => c.calls.push([m, v]);
  c.start = () => { c.started = true; };
  c.dispose = () => { c.disposed = true; };
  c.requestHit = () => {};
  return c;
}

function view({ loadModel, importVrm, settings } = {}) {
  const notices = [];
  const canvas = (d) => ({ style: { display: d } });
  let fallbackCb = null;
  const core = fakeCore();
  const v = new AvatarView({
    sprite: fakeSprite(),
    spriteCanvas: canvas(''), vrmCanvas: canvas('none'),
    loadModel: loadModel || (async () => ({ ok: true, bytes: new ArrayBuffer(8) })),
    importVrm: importVrm || (async () => ({ webglAvailable: () => true, createVrmRenderer: async (_c, _b, o) => { fallbackCb = o.onFallback; return core; } })),
    notice: (t) => notices.push(t),
    settings: settings || (() => ({}))
  });
  return { v, notices, core, fallback: (r) => fallbackCb(r) };
}

const VRM_CHAR = { id: 'kira', renderer: 'vrm', asset: 'models/kira.vrm' };

describe('AvatarView', () => {
  it('switches to VRM live and replays the current state', async () => {
    const { v, core } = view();
    v.setState('thinking');
    v.setMode('free');
    assert.equal(await v.setCharacter(VRM_CHAR), 'vrm');
    assert.ok(core.started);
    assert.deepEqual(core.calls.slice(0, 2), [['setState', 'thinking'], ['setMode', 'free']]);
    assert.equal(v.d.vrmCanvas.style.display, 'block');
    assert.equal(v.d.spriteCanvas.style.display, 'none');
    assert.ok(v.sprite.stopped >= 1);
  });

  it('missing model file → sprite with a clear notice', async () => {
    const { v, notices } = view({ loadModel: async () => ({ ok: false, reason: '3D model file not found (kira.vrm).' }) });
    assert.equal(await v.setCharacter(VRM_CHAR), 'sprite');
    assert.match(notices[0], /not found.*2D avatar/);
  });

  it('WebGL unavailable → sprite', async () => {
    const { v, notices } = view({ importVrm: async () => ({ webglAvailable: () => false }) });
    assert.equal(await v.setCharacter(VRM_CHAR), 'sprite');
    assert.match(notices[0], /WebGL/);
  });

  it('model load error → sprite, and not retried for that character', async () => {
    let tries = 0;
    const { v, notices } = view({ importVrm: async () => ({ webglAvailable: () => true, createVrmRenderer: async () => { tries++; throw new Error('x'); } }) });
    assert.equal(await v.setCharacter(VRM_CHAR), 'sprite');
    assert.equal(await v.setCharacter({ ...VRM_CHAR, asset: 'models/other.vrm' }), 'sprite');
    assert.equal(tries, 1);
    assert.ok(notices.length >= 1);
  });

  it('slow 3D at runtime → automatic switch to the sprite with a notice', async () => {
    const { v, notices, core, fallback } = view();
    await v.setCharacter(VRM_CHAR);
    fallback('slow');
    assert.equal(v.kind, 'sprite');
    assert.ok(core.disposed);
    assert.match(notices.pop(), /too slow/);
  });

  it('low power keeps the sprite', async () => {
    const { v } = view({ settings: () => ({ lowPower: true }) });
    assert.equal(await v.setCharacter(VRM_CHAR), 'sprite');
  });

  it('20 character switches leave exactly one live renderer', async () => {
    const cores = [];
    const { v } = view({ importVrm: async () => ({ webglAvailable: () => true, createVrmRenderer: async () => { const c = fakeCore(); cores.push(c); return c; } }) });
    for (let i = 0; i < 20; i++) {
      await v.setCharacter(i % 2 ? { id: 'ryuk', renderer: 'sprite' } : { ...VRM_CHAR, id: `k${i}` });
    }
    assert.equal(cores.filter((c) => !c.disposed).length, 0);   // ended on the sprite
    assert.equal(v.kind, 'sprite');
  });

  it('alpha comes from the active renderer', async () => {
    const { v } = view();
    await v.setCharacter(VRM_CHAR);
    assert.equal(v.alphaAt(1, 1), 77);
  });
});

// ── model file & protocol ───────────────────────────────────────────────

describe('model file', () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'kira-vrm-'));
  const good = path.join(tmp, 'ok.vrm');
  fs.writeFileSync(good, Buffer.concat([Buffer.from('glTF'), Buffer.alloc(20)]));
  const bad = path.join(tmp, 'bad.vrm');
  fs.writeFileSync(bad, Buffer.alloc(30));

  it('reads a valid glTF-binary .vrm as an ArrayBuffer', () => {
    const r = readModel('ok.vrm', tmp);
    assert.equal(r.ok, true);
    assert.ok(r.bytes instanceof ArrayBuffer);
    assert.equal(r.bytes.byteLength, 24);
  });

  it('refuses missing, wrong type, non-glTF, remote and UNC paths', () => {
    assert.match(readModel('missing.vrm', tmp).reason, /not found/);
    assert.match(readModel('bad.vrm', tmp).reason, /not a valid/);
    assert.equal(resolveModelPath('model.exe', tmp).ok, false);
    assert.equal(resolveModelPath('https://x/y.vrm', tmp).ok, false);
    assert.equal(resolveModelPath('\\\\server\\share\\m.vrm', tmp).ok, false);
    assert.equal(resolveModelPath(null, tmp).ok, false);
  });

  it('app:// paths cannot escape the avatar folder', () => {
    const root = path.join(tmp, 'avatar');
    assert.equal(safeAppPath(root, '/assets/icon.png'), path.join(root, 'assets', 'icon.png'));
    assert.equal(safeAppPath(root, '/../secret.txt'), null);
    assert.equal(safeAppPath(root, '/%2e%2e/%2e%2e/x'), null);
    assert.equal(safeAppPath(root, '/..%5c..%5cx'), null);
    assert.equal(safeAppPath(root, '/%00'), null);
  });
});

describe('index.html', () => {
  it('pins the import map with a CSP hash and keeps the 3D modules local', () => {
    const html = fs.readFileSync(path.join(__dirname, '../src/renderer/index.html'), 'utf8');
    const map = html.match(/<script type="importmap">([\s\S]*?)<\/script>/)[1];
    const hash = crypto.createHash('sha256').update(map).digest('base64');
    assert.ok(html.includes(`'sha256-${hash}'`), 'CSP hash does not match the import map');
    assert.ok(!/https?:/.test(map), 'import map must not load from the network');
    for (const p of Object.values(JSON.parse(map).imports)) {
      const target = path.join(__dirname, '../src/renderer', p);
      assert.ok(p.endsWith('/') ? fs.existsSync(target) : fs.statSync(target).isFile(), `missing ${p}`);
    }
  });
});
