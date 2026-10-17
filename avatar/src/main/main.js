/**
 * avatar/src/main/main.js
 * Electron Main Process for the Kira Avatar Window.
 *
 * Responsibilities
 * - transparent, always-on-top, click-through avatar window
 * - tray icon + avatar menu (hover button / right-click), see menu.js
 * - starts / stops the Python backend (backend-process.js)
 * - Settings window and Free-mode PIN dialog
 * - remembers position, size, lock, feed visibility, start-with-Windows and
 *   the show/hide hotkey in the per-user avatar.json (user-config.js)
 *
 * Security:
 * - contextIsolation: true, nodeIntegration: false, sandbox: true
 * - strictly no navigation or child window opening
 * - focusable: false + showInactive() to prevent stealing keyboard focus
 * - token managed entirely in main; renderer never sees credentials
 * - the Free-mode PIN only passes through main to the authenticated socket
 */

const {
  app,
  BrowserWindow,
  Tray,
  Menu,
  ipcMain,
  screen,
  nativeImage,
  protocol,
  net,
  globalShortcut,
  Notification
} = require('electron');
const path = require('path');
const fs = require('fs');

const { KiraWsClient } = require('./ws-client');
const { BackendProcess, defaultPythonPath } = require('./backend-process');
const { buildTrayTemplate, buildAvatarTemplate } = require('./menu');
const { loadUserConfig, saveUserConfig, userDir, isValidHotkey } = require('./user-config');
const { runPyCli } = require('./py-cli');
const { SettingsWindow } = require('./settings-window');
const { PinDialog } = require('./pin-dialog');
const { clampToBounds, findDisplayForWindow, resolveStartPosition } = require('../shared/bounds');
const { scaleFor, scaledWindowSize } = require('../shared/sizes');
const { buildHitMask, isMaskNonEmpty } = require('../shared/hitmask');

// Disable all touch/trackpad pinch-to-zoom gestures at Chromium engine level
app.commandLine.appendSwitch('disable-pinch');
app.commandLine.appendSwitch('disable-features', 'TouchpadPinch,TouchpadAndWheelScrollLatching');

// Register custom scheme as standard, secure, and supportFetchAPI before app is ready
protocol.registerSchemesAsPrivileged([
  {
    scheme: 'app',
    privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: false }
  }
]);

// Single Instance Lock
const gotTheLock = app.requestSingleInstanceLock();
if (!gotTheLock) {
  app.quit();
  process.exit(0);
}

const AVATAR_ROOT = path.resolve(__dirname, '../..');
const PROJECT_ROOT = path.resolve(AVATAR_ROOT, '..');

// Global references
let mainWindow = null;
let tray = null;
let wsClient = null;
let backend = null;
let settingsWindow = null;
let pinDialog = null;
let isDragging = false;
let isQuitting = false;
let config = {};
let userCfg = null;
let userCfgDir = '';
let registeredHotkey = null;
let characterHitMask = null;
let characterImageMeta = { width: 0, height: 0, isPlaceholder: false };

// Live state mirrored from the backend (snapshot + events)
const live = {
  connected: false,
  persona: 'kira',
  personas: ['kira', 'ryuk'],
  micMuted: false,
  sleeping: false,
  mode: 'restricted',
  freeAvailable: false,
  freeTimeout: 600
};

/**
 * Loads configuration from avatar.config.json.
 */
function loadConfig() {
  const configPath = path.join(AVATAR_ROOT, 'avatar.config.json');
  try {
    if (fs.existsSync(configPath)) {
      config = JSON.parse(fs.readFileSync(configPath, 'utf8'));
      return;
    }
  } catch (err) {
    console.warn('Failed to load avatar.config.json, using defaults');
  }

  // Fallback defaults
  config = {
    window: { width: 380, height: 310, margin_right: 20, margin_bottom: 20 },
    alpha_threshold: 20,
    greeting: 'Hey... Need anything?',
    character: { x: 165, y: 60, width: 205, height: 220 },
    bubble: { x: 10, y: 35, max_width: 170, fade_delay_ms: 3000 },
    connection: {
      url: 'ws://127.0.0.1:8765',
      token_file: '../.kira/token',
      reconnect_min_ms: 1000,
      reconnect_max_ms: 10000
    },
    backend: { autostart: true, python: null, shutdown_timeout_ms: 5000 }
  };
}

/**
 * Resolves character PNG asset (ryuk.png or placeholder.png) and builds alpha hit mask.
 */
function loadCharacterAssetAndMask() {
  const assetsDir = path.join(AVATAR_ROOT, 'assets');
  const ryukPath = path.join(assetsDir, 'ryuk.png');
  const placeholderPath = path.join(assetsDir, 'placeholder.png');

  let targetPath = placeholderPath;
  let isPlaceholder = true;
  if (fs.existsSync(ryukPath)) {
    targetPath = ryukPath;
    isPlaceholder = false;
  }

  try {
    const img = nativeImage.createFromPath(targetPath);
    const size = img.getSize();
    const mask = buildHitMask(img.toBitmap(), size.width, size.height, config.alpha_threshold || 20);

    const mouthOpenPath = path.join(assetsDir, 'ryuk_mouth_open.png');
    let mouthOpenDataUrl = null;
    if (fs.existsSync(mouthOpenPath)) {
      try {
        mouthOpenDataUrl = nativeImage.createFromPath(mouthOpenPath).toDataURL();
      } catch {}
    }

    characterHitMask = mask;
    characterImageMeta = {
      width: size.width,
      height: size.height,
      isPlaceholder,
      assetPath: targetPath,
      dataUrl: img.toDataURL(),
      mouthOpenDataUrl,
      maskLength: mask.length,
      isNonEmpty: isMaskNonEmpty(mask)
    };
  } catch (err) {
    console.error('Failed to build character hit mask');
  }
}

// ── user preferences ────────────────────────────────────────────────────────

function saveUserCfg() {
  userCfg = saveUserConfig(userCfgDir, userCfg);
}

function currentWindowSize() {
  return scaledWindowSize(config.window || {}, userCfg.size);
}

/** Pushes UI state (scale, feed, lock, sleep) to the avatar renderer. */
function sendUiState() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  mainWindow.webContents.send('avatar:ui', {
    scale: scaleFor(userCfg.size),
    feedVisible: userCfg.feedVisible,
    locked: userCfg.locked,
    sleeping: live.sleeping,
    micMuted: live.micMuted,
    connected: live.connected
  });
}

function menuState() {
  return {
    visible: Boolean(mainWindow && mainWindow.isVisible()),
    micMuted: live.micMuted,
    persona: live.persona,
    personas: live.personas,
    mode: live.mode,
    freeAvailable: live.freeAvailable,
    sleeping: live.sleeping,
    size: userCfg.size,
    locked: userCfg.locked,
    feedVisible: userCfg.feedVisible,
    connected: live.connected
  };
}

// ── window ──────────────────────────────────────────────────────────────────

/**
 * Creates the transparent, always-on-top, non-focus-stealing BrowserWindow.
 */
function createWindow() {
  const { width: winWidth, height: winHeight } = currentWindowSize();
  const primary = screen.getPrimaryDisplay();
  const initialPos = resolveStartPosition(
    userCfg.position, winWidth, winHeight, screen.getAllDisplays(), primary.workArea,
    { right: config.window?.margin_right ?? 20, bottom: config.window?.margin_bottom ?? 20 }
  );

  mainWindow = new BrowserWindow({
    x: initialPos.x,
    y: initialPos.y,
    width: winWidth,
    height: winHeight,
    minWidth: winWidth,
    maxWidth: winWidth,
    minHeight: winHeight,
    maxHeight: winHeight,
    useContentSize: true,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    skipTaskbar: true,
    resizable: false,
    focusable: false, // NEVER steals focus from active editor
    show: false,      // Will be shown via showInactive()
    hasShadow: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      navigateOnDragDrop: false
    }
  });

  // Click-through everywhere except the character and the control button
  mainWindow.setIgnoreMouseEvents(true, { forward: true });

  mainWindow.webContents.on('will-navigate', (event) => event.preventDefault());
  mainWindow.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));

  // Strictly disable all webContents zoom (prevents clicking or gestures from zooming)
  try {
    mainWindow.webContents.setVisualZoomLevelLimits(1, 1);
    mainWindow.webContents.setZoomFactor(1.0);
  } catch {}
  mainWindow.webContents.on('zoom-changed', () => {
    mainWindow.webContents.setZoomLevel(0);
  });

  mainWindow.loadFile(path.join(__dirname, '../renderer/index.html'));
  mainWindow.once('ready-to-show', () => {
    mainWindow.showInactive();
    sendUiState();
  });
  mainWindow.on('show', rebuildTray);
  mainWindow.on('hide', rebuildTray);
  mainWindow.on('closed', () => { mainWindow = null; });
}

/** Applies a new size preset: resize around the current position, keep on screen. */
function applySize(size) {
  userCfg.size = size;
  saveUserCfg();
  if (!mainWindow) return;
  const { width, height } = currentWindowSize();
  const [x, y] = mainWindow.getPosition();
  const disp = findDisplayForWindow(x, y, width, height, screen.getAllDisplays()) || screen.getPrimaryDisplay();
  const pos = clampToBounds(x, y, width, height, disp.workArea);
  mainWindow.setMinimumSize(width, height);
  mainWindow.setMaximumSize(width, height);
  mainWindow.setBounds({ x: pos.x, y: pos.y, width, height });
  sendUiState();
}

function toggleVisible() {
  if (!mainWindow) return;
  if (mainWindow.isVisible()) mainWindow.hide();
  else mainWindow.showInactive();
  rebuildTray();
}

// ── tray and menus ──────────────────────────────────────────────────────────

const menuActions = {
  toggleVisible,
  setMicMuted: (muted) => wsClient && wsClient.request('set_mic_muted', { muted }),
  setPersona: (persona) => wsClient && wsClient.request('set_persona', { persona }),
  openSettings: () => settingsWindow.open(),
  stopTask: () => wsClient && wsClient.sendStop(),
  quit: () => app.quit(),
  setSleeping: (sleeping) => {
    // Dim immediately; the backend confirms with a sleep_change event
    live.sleeping = sleeping;
    sendUiState();
    if (wsClient) wsClient.request('set_sleep', { sleeping });
  },
  setRestricted: () => wsClient && wsClient.send('restricted_mode'),
  requestFree: () => pinDialog.open(),
  setSize: (size) => applySize(size),
  setLocked: (locked) => { userCfg.locked = locked; saveUserCfg(); sendUiState(); rebuildTray(); },
  setFeedVisible: (visible) => { userCfg.feedVisible = visible; saveUserCfg(); sendUiState(); }
};

function rebuildTray() {
  if (!tray) return;
  tray.setContextMenu(Menu.buildFromTemplate(buildTrayTemplate(menuState(), menuActions)));
  const mode = live.mode === 'free' ? 'FREE mode' : 'Restricted';
  tray.setToolTip(`Kira — ${live.connected ? mode : 'backend offline'}`);
}

function createTray() {
  tray = new Tray(nativeImage.createFromPath(path.join(AVATAR_ROOT, 'assets', 'icon.png')));
  tray.on('click', toggleVisible);
  rebuildTray();
}

function popupAvatarMenu() {
  if (!mainWindow) return;
  Menu.buildFromTemplate(buildAvatarTemplate(menuState(), menuActions)).popup({ window: mainWindow });
}

// ── hotkey and login item ───────────────────────────────────────────────────

function registerShowHideHotkey(accelerator) {
  if (registeredHotkey) {
    globalShortcut.unregister(registeredHotkey);
    registeredHotkey = null;
  }
  if (!isValidHotkey(accelerator)) return false;
  let ok = false;
  try {
    ok = globalShortcut.register(accelerator, toggleVisible);
  } catch {
    ok = false;
  }
  if (ok) {
    registeredHotkey = accelerator;
  } else if (Notification.isSupported()) {
    new Notification({ title: 'Kira', body: `Hotkey ${accelerator} is in use by another app. Pick another in Settings.` }).show();
  }
  return ok;
}

function applyLoginItem(enabled) {
  try {
    // In development the app is started as `electron .`, so pass the app path
    const args = app.isPackaged ? [] : [AVATAR_ROOT];
    app.setLoginItemSettings({ openAtLogin: Boolean(enabled), path: process.execPath, args });
  } catch {
    // Not fatal
  }
}

function setAvatarPrefs(prefs) {
  const errors = [];
  if (typeof prefs.startWithWindows === 'boolean' && prefs.startWithWindows !== userCfg.startWithWindows) {
    userCfg.startWithWindows = prefs.startWithWindows;
    applyLoginItem(userCfg.startWithWindows);
  }
  if (prefs.showHideHotkey !== undefined && prefs.showHideHotkey !== userCfg.showHideHotkey) {
    if (!isValidHotkey(prefs.showHideHotkey)) {
      errors.push('Show/hide hotkey: use a form like Ctrl+Alt+H');
    } else if (!registerShowHideHotkey(prefs.showHideHotkey)) {
      errors.push('Show/hide hotkey is already used by another app');
      registerShowHideHotkey(userCfg.showHideHotkey);
    } else {
      userCfg.showHideHotkey = prefs.showHideHotkey;
    }
  }
  saveUserCfg();
  return { ok: errors.length === 0, errors };
}

// ── backend link ────────────────────────────────────────────────────────────

/** Mirrors backend events into `live` and refreshes menus / renderer. */
function trackBackendEvent(type, payload) {
  let changed = true;
  switch (type) {
    case 'auth_ok':
      live.connected = true;
      break;
    case 'snapshot':
      live.connected = true;
      if (payload.mode) live.mode = payload.mode;
      if (payload.persona) live.persona = payload.persona;
      if (Array.isArray(payload.personas)) live.personas = payload.personas;
      if (typeof payload.mic_muted === 'boolean') live.micMuted = payload.mic_muted;
      if (typeof payload.sleeping === 'boolean') live.sleeping = payload.sleeping;
      if (typeof payload.free_mode_available === 'boolean') live.freeAvailable = payload.free_mode_available;
      if (typeof payload.free_timeout === 'number') live.freeTimeout = payload.free_timeout;
      break;
    case 'mode_change':
      live.mode = payload.mode === 'free' ? 'free' : 'restricted';
      break;
    case 'persona_change':
      if (payload.persona) live.persona = payload.persona;
      break;
    case 'mic_mute':
      live.micMuted = Boolean(payload.muted);
      break;
    case 'sleep_change':
      live.sleeping = Boolean(payload.sleeping);
      break;
    case 'state':
      if (payload.state === 'offline') live.connected = false;
      else changed = false;
      break;
    default:
      changed = false;
  }
  if (changed) {
    rebuildTray();
    sendUiState();
  }
  // After (re)connecting, ask for a full status so menus are accurate
  if (type === 'auth_ok' && wsClient) {
    wsClient.request('get_status', {}).then((ack) => {
      if (ack && ack.snapshot) trackBackendEvent('snapshot', ack.snapshot);
    });
  }
}

function initWsClient() {
  const tokenAbsPath = path.resolve(AVATAR_ROOT, config.connection?.token_file || '../.kira/token');
  wsClient = new KiraWsClient({
    url: config.connection?.url || 'ws://127.0.0.1:8765',
    tokenPath: tokenAbsPath,
    reconnectMinMs: config.connection?.reconnect_min_ms || 1000,
    reconnectMaxMs: config.connection?.reconnect_max_ms || 10000,
    onEvent: (type, payload) => {
      trackBackendEvent(type, payload || {});
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('avatar:event', { type, payload });
      }
    }
  });
  wsClient.connect();
}

function initBackend() {
  const url = new URL(config.connection?.url || 'ws://127.0.0.1:8765');
  const bcfg = config.backend || {};
  backend = new BackendProcess({
    python: defaultPythonPath(PROJECT_ROOT, bcfg.python),
    cwd: PROJECT_ROOT,
    logPath: path.join(PROJECT_ROOT, 'logs', 'backend.log'),
    host: url.hostname,
    port: Number(url.port) || 8765,
    timeoutMs: bcfg.shutdown_timeout_ms ?? 5000
  });
  if (bcfg.autostart !== false) {
    backend.ensureStarted().then((result) => {
      if (result === 'missing-python' && Notification.isSupported()) {
        new Notification({
          title: 'Kira',
          body: 'Python .venv not found. See README "Quick Setup", then restart Kira.'
        }).show();
      }
    });
  }
}

function pyCli(action, payload) {
  return runPyCli({ python: defaultPythonPath(PROJECT_ROOT, config.backend?.python), cwd: PROJECT_ROOT }, action, payload);
}

// ── IPC from the avatar renderer ────────────────────────────────────────────

function setupIpcHandlers() {
  const fromAvatar = (event) => event.sender.id === mainWindow?.webContents.id;

  ipcMain.handle('avatar:get-config', (event) => {
    if (!fromAvatar(event)) throw new Error('Unauthorized IPC sender');
    return {
      config,
      characterMeta: {
        width: characterImageMeta.width,
        height: characterImageMeta.height,
        isPlaceholder: characterImageMeta.isPlaceholder,
        dataUrl: characterImageMeta.dataUrl,
        mouthOpenDataUrl: characterImageMeta.mouthOpenDataUrl
      },
      hitMask: characterHitMask ? Array.from(characterHitMask) : [],
      ui: { scale: scaleFor(userCfg.size), feedVisible: userCfg.feedVisible, locked: userCfg.locked }
    };
  });

  ipcMain.on('avatar:open-menu', (event) => {
    if (fromAvatar(event)) popupAvatarMenu();
  });

  ipcMain.on('avatar:drag-start', (event) => {
    if (!fromAvatar(event) || userCfg.locked) return;
    isDragging = true;
    if (mainWindow) mainWindow.setIgnoreMouseEvents(false);
  });

  ipcMain.on('avatar:drag-move', (event, delta) => {
    if (!fromAvatar(event) || userCfg.locked || !isDragging || !mainWindow || !delta) return;
    const [currentX, currentY] = mainWindow.getPosition();
    const { width, height } = currentWindowSize();
    mainWindow.setBounds({
      x: Math.round(currentX + (Number(delta.dx) || 0)),
      y: Math.round(currentY + (Number(delta.dy) || 0)),
      width,
      height
    });
  });

  ipcMain.on('avatar:drag-end', (event) => {
    if (!fromAvatar(event)) return;
    const wasDragging = isDragging;
    isDragging = false;
    if (!mainWindow || !wasDragging) return;

    const [currentX, currentY] = mainWindow.getPosition();
    const { width, height } = currentWindowSize();
    const disp = findDisplayForWindow(currentX, currentY, width, height, screen.getAllDisplays()) || screen.getPrimaryDisplay();
    const clamped = clampToBounds(currentX, currentY, width, height, disp.workArea);
    mainWindow.setBounds({ x: clamped.x, y: clamped.y, width, height });
    userCfg.position = clamped;
    saveUserCfg();
    mainWindow.setIgnoreMouseEvents(true, { forward: true });
  });

  ipcMain.on('avatar:set-ignore-mouse-events', (event, { ignore, forward }) => {
    if (!fromAvatar(event) || isDragging) return;
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.setIgnoreMouseEvents(Boolean(ignore), { forward: Boolean(forward) });
    }
  });

  ipcMain.on('avatar:stop', (event) => {
    if (fromAvatar(event) && wsClient) wsClient.sendStop();
  });
}

// ── lifecycle ───────────────────────────────────────────────────────────────

app.whenReady().then(() => {
  protocol.handle('app', (request) => {
    const url = new URL(request.url);
    const safePath = path.normalize(path.join(AVATAR_ROOT, url.pathname));
    return net.fetch(`file://${safePath}`);
  });

  loadConfig();
  loadCharacterAssetAndMask();
  userCfgDir = userDir(app.getPath('appData'));
  userCfg = loadUserConfig(userCfgDir);

  settingsWindow = new SettingsWindow({
    runCli: pyCli,
    getAvatarPrefs: () => ({ startWithWindows: userCfg.startWithWindows, showHideHotkey: userCfg.showHideHotkey }),
    setAvatarPrefs,
    reloadBackend: () => (wsClient ? wsClient.request('reload_settings', {}) : Promise.resolve({ status: 'offline' })),
    restartBackend: async () => backend.restart(() => wsClient.send('shutdown')),
    isQuitting: () => isQuitting
  });
  pinDialog = new PinDialog({
    request: (cmd, fields) => (wsClient ? wsClient.request(cmd, fields, 8000) : Promise.resolve({ status: 'offline' })),
    getFreeTimeout: () => live.freeTimeout
  });

  createWindow();
  createTray();
  setupIpcHandlers();
  initBackend();
  initWsClient();
  registerShowHideHotkey(userCfg.showHideHotkey);
  applyLoginItem(userCfg.startWithWindows);

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', (event) => {
  // Never quit when windows close; the avatar lives in the tray
  event.preventDefault();
});

// Quit: stop the backend cleanly (shutdown command → wait → kill tree), then exit
app.on('before-quit', (event) => {
  if (isQuitting) return;
  isQuitting = true;
  event.preventDefault();
  globalShortcut.unregisterAll();
  const sendShutdown = () => (wsClient ? wsClient.send('shutdown') : false);
  const stopping = backend ? backend.stop(sendShutdown) : Promise.resolve('not-running');
  stopping.finally(() => {
    if (wsClient) wsClient.destroy();
    if (settingsWindow) settingsWindow.destroy();
    if (tray) tray.destroy();
    app.exit(0);
  });
});

module.exports = { loadConfig, loadCharacterAssetAndMask };
