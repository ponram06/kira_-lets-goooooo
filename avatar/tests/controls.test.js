/**
 * avatar/tests/controls.test.js
 * M1 app controls: menus, user config, start position, sizes, action feed,
 * backend start/stop sequence, settings CLI runner and protocol helpers.
 * Headless: no Electron, no real processes, no network.
 */

const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const os = require('os');
const fs = require('fs');
const path = require('path');
const { EventEmitter } = require('events');
const { PassThrough } = require('stream');

const { buildTrayTemplate, buildAvatarTemplate } = require('../src/main/menu');
const { DEFAULTS, loadUserConfig, saveUserConfig, sanitize, userDir, isValidHotkey } = require('../src/main/user-config');
const { resolveStartPosition, isOnAnyDisplay } = require('../src/shared/bounds');
const { scaledWindowSize, scaleFor } = require('../src/shared/sizes');
const { formatFeedEntry, pushFeed } = require('../src/shared/feed');
const { BackendProcess } = require('../src/main/backend-process');
const { runPyCli } = require('../src/main/py-cli');
const { createCommand, isValidCommand } = require('../src/shared/schema');

// ── helpers ──────────────────────────────────────────────────────────────────

function recorder() {
  const calls = [];
  const handler = { get: (_t, name) => (...args) => calls.push([name, ...args]) };
  return { calls, actions: new Proxy({}, handler) };
}

const baseState = {
  visible: true, micMuted: false, persona: 'kira', personas: ['kira', 'ryuk'],
  mode: 'restricted', freeAvailable: true, sleeping: false, size: 'medium',
  locked: false, feedVisible: true, connected: true
};

function labels(template) {
  return template.filter((i) => i.type !== 'separator').map((i) => i.label);
}

function find(template, prefix) {
  return template.find((i) => i.label && i.label.startsWith(prefix));
}

// ── menus ────────────────────────────────────────────────────────────────────

describe('Tray menu', () => {
  it('has show/hide, mic, persona, settings, stop and quit', () => {
    const { actions } = recorder();
    const l = labels(buildTrayTemplate(baseState, actions));
    assert.deepEqual(l, ['Hide avatar', 'Mute microphone', 'Persona', 'Settings…', 'Stop current task', 'Quit Kira']);
  });

  it('toggles labels from state and fires actions', () => {
    const { calls, actions } = recorder();
    const t = buildTrayTemplate({ ...baseState, visible: false, micMuted: true }, actions);
    assert.equal(t[0].label, 'Show avatar');
    find(t, 'Unmute').click();
    find(t, 'Persona').submenu[1].click();
    find(t, 'Stop').click();
    find(t, 'Quit').click();
    assert.deepEqual(calls, [['setMicMuted', false], ['setPersona', 'ryuk'], ['stopTask'], ['quit']]);
  });

  it('disables backend items while offline but keeps quit and settings', () => {
    const { actions } = recorder();
    const t = buildTrayTemplate({ ...baseState, connected: false }, actions);
    assert.equal(find(t, 'Mute').enabled, false);
    assert.equal(find(t, 'Stop').enabled, false);
    assert.equal(find(t, 'Quit').enabled, undefined);
    assert.equal(find(t, 'Settings').enabled, undefined);
  });
});

describe('Avatar menu', () => {
  it('contains every M1 and M2 control', () => {
    const { actions } = recorder();
    const l = labels(buildAvatarTemplate(baseState, actions));
    assert.deepEqual(l, ['Mute microphone', 'Chat…', 'Sleep', 'Persona', 'Mode', 'Size', 'Lock position',
      'Show action feed', 'Low-power mode (2D avatar)', 'Settings…', 'Stop task', 'Quit Kira']);
  });

  it('Chat item opens chat when idle and is disabled when working', () => {
    const { calls, actions } = recorder();
    const idleMenu = buildAvatarTemplate({ ...baseState, state: 'idle' }, actions);
    const chatItem = find(idleMenu, 'Chat');
    assert.ok(chatItem);
    assert.equal(chatItem.enabled, true);
    chatItem.click();
    assert.deepEqual(calls, [['openChat']]);

    const workingMenu = buildAvatarTemplate({ ...baseState, state: 'working' }, actions);
    assert.equal(find(workingMenu, 'Chat').enabled, false);
  });

  it('sleep and mute are separate actions', () => {
    const { calls, actions } = recorder();
    const t = buildAvatarTemplate(baseState, actions);
    find(t, 'Sleep').click();
    find(t, 'Mute').click();
    assert.deepEqual(calls, [['setSleeping', true], ['setMicMuted', true]]);
    const asleep = buildAvatarTemplate({ ...baseState, sleeping: true }, actions);
    assert.ok(find(asleep, 'Wake'));
  });

  it('Free goes through the PIN dialog, Restricted is immediate', () => {
    const { calls, actions } = recorder();
    const mode = find(buildAvatarTemplate(baseState, actions), 'Mode').submenu;
    mode[1].click();
    mode[0].click();
    assert.deepEqual(calls, [['requestFree'], ['setRestricted']]);
    assert.match(mode[1].label, /PIN/);
  });

  it('Free is disabled when no PIN is configured or already free', () => {
    const { actions } = recorder();
    let mode = find(buildAvatarTemplate({ ...baseState, freeAvailable: false }, actions), 'Mode').submenu;
    assert.equal(mode[1].enabled, false);
    assert.match(mode[1].label, /Settings/);
    mode = find(buildAvatarTemplate({ ...baseState, mode: 'free' }, actions), 'Mode').submenu;
    assert.equal(mode[1].enabled, false);
    assert.equal(mode[1].checked, true);
  });

  it('size, lock and feed items reflect and change state', () => {
    const { calls, actions } = recorder();
    const t = buildAvatarTemplate({ ...baseState, locked: true, feedVisible: false, size: 'large' }, actions);
    const size = find(t, 'Size').submenu;
    assert.deepEqual(size.map((s) => s.label), ['Small', 'Medium', 'Large']);
    assert.equal(size[2].checked, true);
    size[0].click();
    find(t, 'Lock').click();
    find(t, 'Show action feed').click();
    assert.deepEqual(calls, [['setSize', 'small'], ['setLocked', false], ['setFeedVisible', true]]);
  });
});

// ── user config ──────────────────────────────────────────────────────────────

describe('User config (avatar.json)', () => {
  const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), 'kira-ucfg-'));

  it('defaults when missing or corrupt', () => {
    const dir = tmp();
    assert.deepEqual(loadUserConfig(dir), { ...DEFAULTS });
    fs.writeFileSync(path.join(dir, 'avatar.json'), '{broken');
    assert.deepEqual(loadUserConfig(dir), { ...DEFAULTS });
  });

  it('round-trips valid values and drops unknown ones (never a mode)', () => {
    const dir = tmp();
    saveUserConfig(dir, { position: { x: 10.4, y: 20 }, size: 'large', locked: true, feedVisible: false,
      lowPower: true, startWithWindows: true, showHideHotkey: 'Ctrl+Shift+K', mode: 'free', persona: 'ryuk' });
    const cfg = loadUserConfig(dir);
    assert.deepEqual(cfg, { position: { x: 10, y: 20 }, size: 'large', locked: true, feedVisible: false,
      lowPower: true, startWithWindows: true, showHideHotkey: 'Ctrl+Shift+K' });
    assert.equal('mode' in cfg, false);
  });

  it('start-with-Windows defaults to off', () => {
    assert.equal(DEFAULTS.startWithWindows, false);
  });

  it('rejects bad values', () => {
    const cfg = sanitize({ size: 'huge', position: { x: 'a', y: 1 }, showHideHotkey: 'rm -rf', locked: 'yes' });
    assert.deepEqual(cfg, { ...DEFAULTS });
  });

  it('validates hotkeys', () => {
    assert.equal(isValidHotkey('Ctrl+Alt+H'), true);
    assert.equal(isValidHotkey('CommandOrControl+Shift+F12'), true);
    assert.equal(isValidHotkey('H'), false);
    assert.equal(isValidHotkey('Ctrl+Alt+Delete;calc'), false);
  });

  it('folder comes from KIRA_USER_DIR or AppData\\Kira', () => {
    assert.equal(userDir('C:/AppData', { KIRA_USER_DIR: 'X:/u' }), 'X:/u');
    assert.equal(userDir('C:/AppData', {}), path.join('C:/AppData', 'Kira'));
  });
});

// ── start position and sizes ─────────────────────────────────────────────────

describe('Start position', () => {
  const primary = { workArea: { x: 0, y: 0, width: 1920, height: 1040 } };
  const second = { workArea: { x: 1920, y: 0, width: 1280, height: 1024 } };

  it('restores a saved on-screen position', () => {
    assert.deepEqual(resolveStartPosition({ x: 2000, y: 100 }, 380, 310, [primary, second], primary.workArea), { x: 2000, y: 100 });
  });

  it('resets to default bottom-right when the saved position is off-screen', () => {
    const def = { x: 1920 - 380 - 20, y: 1040 - 310 - 20 };
    assert.deepEqual(resolveStartPosition({ x: 2500, y: 100 }, 380, 310, [primary], primary.workArea), def);
    assert.deepEqual(resolveStartPosition({ x: -5000, y: -5000 }, 380, 310, [primary], primary.workArea), def);
    assert.deepEqual(resolveStartPosition(null, 380, 310, [primary], primary.workArea), def);
  });

  it('clamps a partly visible window back inside', () => {
    const pos = resolveStartPosition({ x: 1800, y: 900 }, 380, 310, [primary], primary.workArea);
    assert.deepEqual(pos, { x: 1540, y: 730 });
    assert.equal(isOnAnyDisplay(1900, 1030, 380, 310, [primary]), false);
  });
});

describe('Sizes', () => {
  it('scales the window', () => {
    assert.deepEqual(scaledWindowSize({ width: 380, height: 310 }, 'small'), { width: 304, height: 248 });
    assert.deepEqual(scaledWindowSize({ width: 380, height: 310 }, 'large'), { width: 475, height: 388 });
    assert.equal(scaleFor('nonsense'), 1.0);
  });
});

// ── action feed ──────────────────────────────────────────────────────────────

describe('Action feed', () => {
  it('formats tool, ask, block, countdown and error events', () => {
    assert.equal(formatFeedEntry('tool_start', { tool: 'create_file' }).kind, 'start');
    assert.equal(formatFeedEntry('tool_done', { tool: 'create_file' }).kind, 'done');
    assert.equal(formatFeedEntry('policy_ask', { prompt: 'Delete?' }).kind, 'ask');
    assert.equal(formatFeedEntry('policy_decision', { decision: 'block', tool: 'x' }).kind, 'block');
    assert.equal(formatFeedEntry('policy_decision', { decision: 'countdown', tool: 'x' }).kind, 'countdown');
    assert.equal(formatFeedEntry('policy_decision', { decision: 'allow' }), null);
    assert.equal(formatFeedEntry('tool_error', { summary: 'failed' }).kind, 'error');
    assert.equal(formatFeedEntry('loudness', {}), null);
  });

  it('clips long text and strips control characters', () => {
    const e = formatFeedEntry('tool_error', { summary: 'a\u0000b' + 'x'.repeat(200) });
    assert.ok(e.text.length <= 60);
    assert.equal(e.text.includes('\u0000'), false);
  });

  it('keeps only the last 4 entries', () => {
    let list = [];
    for (let i = 0; i < 6; i++) list = pushFeed(list, { kind: 'start', text: String(i) });
    assert.deepEqual(list.map((e) => e.text), ['2', '3', '4', '5']);
  });
});

// ── backend process ──────────────────────────────────────────────────────────

function fakeChild({ exitOnShutdown = true } = {}) {
  const child = new EventEmitter();
  child.pid = 4242;
  child.exitCode = null;
  child.killed = false;
  child.stdout = new PassThrough();
  child.stderr = new PassThrough();
  child.exit = (code = 0) => { child.exitCode = code; child.emit('exit', code); };
  child.exitOnShutdown = exitOnShutdown;
  return child;
}

function fakeFs() {
  return {
    existsSync: () => true,
    mkdirSync: () => {},
    createWriteStream: () => new PassThrough()
  };
}

describe('Backend process', () => {
  it('does not start a second backend when one is already listening', async () => {
    let spawned = 0;
    const bp = new BackendProcess({ python: 'py', cwd: '.', logPath: 'l', probePort: async () => true,
      spawn: () => { spawned++; return fakeChild(); }, fs: fakeFs() });
    assert.equal(await bp.ensureStarted(), 'external');
    assert.equal(spawned, 0);
  });

  it('spawns headless backend with hidden window and piped output', async () => {
    let args = null;
    const bp = new BackendProcess({ python: 'py', cwd: 'root', logPath: 'logs/backend.log', probePort: async () => false,
      spawn: (cmd, a, opts) => { args = { cmd, a, opts }; return fakeChild(); }, fs: fakeFs() });
    assert.equal(await bp.ensureStarted(), 'started');
    assert.deepEqual(args.a, ['-m', 'backend.main', '--headless']);
    assert.equal(args.opts.windowsHide, true);
    assert.equal(args.opts.cwd, 'root');
    assert.equal(bp.isOwned(), true);
  });

  it('reports missing python instead of spawning', async () => {
    const bp = new BackendProcess({ python: 'nope', cwd: '.', logPath: 'l', probePort: async () => false,
      spawn: () => { throw new Error('should not spawn'); }, fs: { ...fakeFs(), existsSync: () => false } });
    assert.equal(await bp.ensureStarted(), 'missing-python');
  });

  it('quit: shutdown command, clean exit, no kill', async () => {
    const child = fakeChild();
    let killed = false;
    const bp = new BackendProcess({ python: 'py', cwd: '.', logPath: 'l', probePort: async () => false,
      spawn: () => child, killTree: async () => { killed = true; }, fs: fakeFs(), timeoutMs: 500 });
    await bp.ensureStarted();
    const result = await bp.stop(() => { setTimeout(() => child.exit(0), 20); return true; });
    assert.equal(result, 'exited');
    assert.equal(killed, false);
  });

  it('quit: kills the process tree when the backend does not exit in time', async () => {
    const child = fakeChild();
    let killedPid = null;
    const bp = new BackendProcess({ python: 'py', cwd: '.', logPath: 'l', probePort: async () => false,
      spawn: () => child, fs: fakeFs(), timeoutMs: 50,
      killTree: async (pid) => { killedPid = pid; child.exit(1); } });
    await bp.ensureStarted();
    assert.equal(await bp.stop(() => true), 'killed');
    assert.equal(killedPid, 4242);
  });

  it('quit: kills immediately if the shutdown command could not be sent', async () => {
    const child = fakeChild();
    let killed = false;
    const bp = new BackendProcess({ python: 'py', cwd: '.', logPath: 'l', probePort: async () => false,
      spawn: () => child, fs: fakeFs(), timeoutMs: 5000,
      killTree: async () => { killed = true; child.exit(1); } });
    await bp.ensureStarted();
    const t0 = Date.now();
    assert.equal(await bp.stop(() => false), 'killed');
    assert.ok(killed && Date.now() - t0 < 1000);
  });

  it('quit: an external backend is only asked to shut down', async () => {
    let asked = false;
    const bp = new BackendProcess({ python: 'py', cwd: '.', logPath: 'l', probePort: async () => true,
      spawn: () => fakeChild(), killTree: async () => { throw new Error('must not kill'); }, fs: fakeFs() });
    await bp.ensureStarted();
    assert.equal(await bp.stop(() => { asked = true; return true; }), 'external-asked');
    assert.equal(asked, true);
  });
});

// ── settings CLI runner ──────────────────────────────────────────────────────

function fakeCliSpawn(reply, capture) {
  return (cmd, args) => {
    const child = new EventEmitter();
    child.stdout = new PassThrough();
    child.stderr = new PassThrough();
    child.stdin = new PassThrough();
    let input = '';
    child.stdin.on('data', (d) => { input += d; });
    child.stdin.on('finish', () => {
      capture.args = args;
      capture.input = input;
      child.stdout.end(reply);
      setImmediate(() => child.emit('close', 0));
    });
    child.kill = () => {};
    return child;
  };
}

describe('Settings CLI runner', () => {
  it('sends secrets via stdin only, never argv', async () => {
    const cap = {};
    const res = await runPyCli({ python: 'py', cwd: '.', spawn: fakeCliSpawn('{"ok": true}\n', cap) },
      'set', { keys: { GROQ_API_KEY: 'gsk_secret_value' }, pin: '0000' });
    assert.deepEqual(res, { ok: true });
    assert.deepEqual(cap.args, ['-m', 'backend.settings_cli', 'set']);
    assert.equal(cap.args.join(' ').includes('gsk_secret_value'), false);
    assert.equal(cap.args.join(' ').includes('0000'), false);
    assert.equal(JSON.parse(cap.input).keys.GROQ_API_KEY, 'gsk_secret_value');
  });

  it('rejects unknown actions without spawning', async () => {
    const res = await runPyCli({ python: 'py', cwd: '.', spawn: () => { throw new Error('no'); } }, 'rm', {});
    assert.equal(res.ok, false);
  });

  it('handles a bad reply', async () => {
    const res = await runPyCli({ python: 'py', cwd: '.', spawn: fakeCliSpawn('Traceback...', {}) }, 'get', {});
    assert.equal(res.ok, false);
  });
});

// ── protocol ─────────────────────────────────────────────────────────────────

describe('Control command schema', () => {
  it('accepts the M1 commands with valid fields', () => {
    assert.equal(isValidCommand(createCommand('free_mode_request', { pin: '0000' }, 'r1')), true);
    assert.equal(isValidCommand(createCommand('set_persona', { persona: 'ryuk' })), true);
    assert.equal(isValidCommand(createCommand('set_mic_muted', { muted: true })), true);
    assert.equal(isValidCommand(createCommand('set_sleep', { sleeping: false })), true);
    assert.equal(isValidCommand(createCommand('shutdown')), true);
    assert.equal(isValidCommand(createCommand('get_status')), true);
  });

  it('rejects malformed control commands', () => {
    assert.equal(isValidCommand({ command: 'free_mode_request', pin: '12' }), false);
    assert.equal(isValidCommand({ command: 'free_mode_request', pin: 'abcd' }), false);
    assert.equal(isValidCommand({ command: 'free_mode_request' }), false);
    assert.equal(isValidCommand({ command: 'set_mic_muted', muted: 'yes' }), false);
    assert.equal(isValidCommand({ command: 'set_sleep' }), false);
    assert.equal(isValidCommand({ command: 'set_persona', persona: ' ' }), false);
    assert.equal(isValidCommand({ command: 'free' }), false);
  });

  it('request id is attached and command cannot be overridden by fields', () => {
    const msg = createCommand('get_status', { command: 'shutdown' }, 'r9');
    assert.equal(msg.command, 'get_status');
    assert.equal(msg.request_id, 'r9');
  });

  it('accepts valid user_text command within 2000 chars and rejects invalid', () => {
    assert.equal(isValidCommand(createCommand('user_text', { text: 'Hello Kira' }, 'r10')), true);
    assert.equal(isValidCommand(createCommand('user_text', { text: 'a'.repeat(2000) })), true);
    // Rejects empty, missing, whitespace-only, non-string, or > 2000 chars
    assert.equal(isValidCommand({ command: 'user_text', text: '' }), false);
    assert.equal(isValidCommand({ command: 'user_text', text: '   ' }), false);
    assert.equal(isValidCommand({ command: 'user_text' }), false);
    assert.equal(isValidCommand({ command: 'user_text', text: 12345 }), false);
    assert.equal(isValidCommand({ command: 'user_text', text: 'a'.repeat(2001) }), false);
  });
});
