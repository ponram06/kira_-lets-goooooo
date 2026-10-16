/**
 * avatar/src/main/main.js
 * Electron Main Process for the Kira Avatar Window.
 *
 * Security:
 * - contextIsolation: true, nodeIntegration: false, sandbox: true
 * - strictly no navigation or child window opening
 * - focusable: false + showInactive() to prevent stealing keyboard focus
 * - token managed entirely in main; renderer never sees credentials
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
  net
} = require('electron');
const path = require('path');
const fs = require('fs');

const { KiraWsClient } = require('./ws-client');
const {
  clampToBounds,
  calculateDefaultPosition,
  findDisplayForWindow
} = require('../shared/bounds');
const { buildHitMask, isMaskNonEmpty } = require('../shared/hitmask');

// Register custom scheme as standard, secure, and supportFetchAPI before app is ready
protocol.registerSchemesAsPrivileged([
  {
    scheme: 'app',
    privileges: {
      standard: true,
      secure: true,
      supportFetchAPI: true,
      corsEnabled: false
    }
  }
]);

// Single Instance Lock
const gotTheLock = app.requestSingleInstanceLock();
if (!gotTheLock) {
  app.quit();
  process.exit(0);
}

// Global references
let mainWindow = null;
let tray = null;
let wsClient = null;
let isDragging = false;
let isPositionLocked = false;
let config = {};
let characterHitMask = null;
let characterImageMeta = { width: 0, height: 0, isPlaceholder: false };

/**
 * Loads configuration from avatar.config.json.
 */
function loadConfig() {
  const configPath = path.resolve(__dirname, '../../avatar.config.json');
  try {
    if (fs.existsSync(configPath)) {
      config = JSON.parse(fs.readFileSync(configPath, 'utf8'));
      return;
    }
  } catch (err) {
    console.warn('Failed to load avatar.config.json, using defaults', err);
  }

  // Fallback defaults
  config = {
    window: { width: 480, height: 420, margin_right: 20, margin_bottom: 20 },
    alpha_threshold: 20,
    greeting: 'Hey... Need anything?',
    glow: {
      eye: { x: 228, y: 140, radius: 14, color: 'rgba(255, 60, 60, 0.6)' },
      apple: { x: 240, y: 270, radius: 28, color: 'rgba(255, 20, 20, 0.85)' }
    },
    character: { x: 160, y: 80, width: 300, height: 320 },
    bubble: { x: 10, y: 40, max_width: 220, fade_delay_ms: 3000 },
    connection: {
      url: 'ws://127.0.0.1:8765',
      token_file: '../.kira/token',
      reconnect_min_ms: 1000,
      reconnect_max_ms: 10000
    }
  };
}

/**
 * Resolves character PNG asset (ryuk.png or placeholder.png) and builds alpha hit mask.
 */
function loadCharacterAssetAndMask() {
  const assetsDir = path.resolve(__dirname, '../../assets');
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
    const bitmap = img.toBitmap();
    const mask = buildHitMask(bitmap, size.width, size.height, config.alpha_threshold || 20);

    characterHitMask = mask;
    characterImageMeta = {
      width: size.width,
      height: size.height,
      isPlaceholder,
      assetPath: targetPath,
      maskLength: mask.length,
      isNonEmpty: isMaskNonEmpty(mask)
    };
  } catch (err) {
    console.error('Failed to build character hit mask:', err);
  }
}

/**
 * Path to saved window bounds JSON file.
 */
function getBoundsFilePath() {
  return path.join(app.getPath('userData'), 'avatar-bounds.json');
}

/**
 * Saves window position to disk.
 */
function saveBounds(bounds) {
  try {
    fs.writeFileSync(getBoundsFilePath(), JSON.stringify(bounds, null, 2), 'utf8');
  } catch {
    // Non-critical persistence error
  }
}

/**
 * Restores window position or calculates default bottom-right.
 */
function restoreOrCalculateBounds(winWidth, winHeight) {
  const primaryDisplay = screen.getPrimaryDisplay();
  const workArea = primaryDisplay.workArea;

  const defaultPos = calculateDefaultPosition(
    workArea,
    winWidth,
    winHeight,
    config.window?.margin_right || 20,
    config.window?.margin_bottom || 20
  );

  const boundsFile = getBoundsFilePath();
  if (fs.existsSync(boundsFile)) {
    try {
      const saved = JSON.parse(fs.readFileSync(boundsFile, 'utf8'));
      if (typeof saved.x === 'number' && typeof saved.y === 'number') {
        const allDisplays = screen.getAllDisplays();
        const disp = findDisplayForWindow(saved.x, saved.y, winWidth, winHeight, allDisplays) || primaryDisplay;
        return clampToBounds(saved.x, saved.y, winWidth, winHeight, disp.workArea);
      }
    } catch {
      // Fall through to defaultPos
    }
  }

  return defaultPos;
}

/**
 * Creates the transparent, always-on-top, non-focus-stealing BrowserWindow.
 */
function createWindow() {
  loadConfig();
  loadCharacterAssetAndMask();

  const winWidth = config.window?.width || 480;
  const winHeight = config.window?.height || 420;
  const initialPos = restoreOrCalculateBounds(winWidth, winHeight);

  mainWindow = new BrowserWindow({
    x: initialPos.x,
    y: initialPos.y,
    width: winWidth,
    height: winHeight,
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

  // Enable click-through initially
  mainWindow.setIgnoreMouseEvents(true, { forward: true });

  // Strictly block external navigation and new windows
  mainWindow.webContents.on('will-navigate', (event) => {
    event.preventDefault();
  });

  mainWindow.webContents.setWindowOpenHandler(() => {
    return { action: 'deny' };
  });

  mainWindow.loadFile(path.join(__dirname, '../renderer/index.html'));

  mainWindow.once('ready-to-show', () => {
    // Show without activating/stealing focus from VS Code or terminal
    mainWindow.showInactive();
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

/**
 * Sets up system tray icon and context menu.
 */
function createTray() {
  const iconPath = path.resolve(__dirname, '../../assets/icon.png');
  const trayImage = nativeImage.createFromPath(iconPath);
  tray = new Tray(trayImage);
  tray.setToolTip('Kira Desktop Assistant');

  const contextMenu = Menu.buildFromTemplate([
    {
      label: 'Show / Hide',
      click: () => {
        if (!mainWindow) return;
        if (mainWindow.isVisible()) {
          mainWindow.hide();
        } else {
          mainWindow.showInactive();
        }
      }
    },
    {
      label: 'Lock Position',
      type: 'checkbox',
      checked: isPositionLocked,
      click: (item) => {
        isPositionLocked = item.checked;
      }
    },
    {
      label: 'Stop (Kill Switch)',
      click: () => {
        if (wsClient) {
          wsClient.sendStop();
        }
      }
    },
    { type: 'separator' },
    {
      label: 'Quit',
      click: () => {
        if (wsClient) wsClient.destroy();
        app.quit();
      }
    }
  ]);

  tray.setContextMenu(contextMenu);
}

/**
 * Initializes IPC handlers.
 */
function setupIpcHandlers() {
  ipcMain.handle('avatar:get-config', (event) => {
    // Verify caller frame
    if (event.sender.id !== mainWindow?.webContents.id) {
      throw new Error('Unauthorized IPC sender');
    }

    return {
      config,
      characterMeta: {
        width: characterImageMeta.width,
        height: characterImageMeta.height,
        isPlaceholder: characterImageMeta.isPlaceholder
      },
      hitMask: characterHitMask ? Array.from(characterHitMask) : []
    };
  });

  ipcMain.on('avatar:drag-start', (event) => {
    if (event.sender.id !== mainWindow?.webContents.id || isPositionLocked) return;
    isDragging = true;
    if (mainWindow) {
      mainWindow.setIgnoreMouseEvents(false);
    }
  });

  ipcMain.on('avatar:drag-move', (event, delta) => {
    if (event.sender.id !== mainWindow?.webContents.id || isPositionLocked || !isDragging) return;
    if (!mainWindow || !delta) return;

    const [currentX, currentY] = mainWindow.getPosition();
    const newX = currentX + (Number(delta.dx) || 0);
    const newY = currentY + (Number(delta.dy) || 0);
    mainWindow.setPosition(Math.round(newX), Math.round(newY));
  });

  ipcMain.on('avatar:drag-end', (event) => {
    if (event.sender.id !== mainWindow?.webContents.id || isPositionLocked) return;
    isDragging = false;

    if (!mainWindow) return;

    const [currentX, currentY] = mainWindow.getPosition();
    const [width, height] = mainWindow.getSize();
    const allDisplays = screen.getAllDisplays();
    const currentDisplay = findDisplayForWindow(currentX, currentY, width, height, allDisplays) || screen.getPrimaryDisplay();

    const clamped = clampToBounds(currentX, currentY, width, height, currentDisplay.workArea);
    mainWindow.setPosition(clamped.x, clamped.y);
    saveBounds(clamped);

    mainWindow.setIgnoreMouseEvents(true, { forward: true });
  });

  ipcMain.on('avatar:set-ignore-mouse-events', (event, { ignore, forward }) => {
    if (event.sender.id !== mainWindow?.webContents.id) return;
    if (isDragging) return; // Keep receiving events while dragging

    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.setIgnoreMouseEvents(Boolean(ignore), { forward: Boolean(forward) });
    }
  });

  ipcMain.on('avatar:stop', (event) => {
    if (event.sender.id !== mainWindow?.webContents.id) return;
    if (wsClient) {
      wsClient.sendStop();
    }
  });
}

/**
 * Initializes WebSocket client connecting to Kira backend.
 */
function initWsClient() {
  const tokenRelPath = config.connection?.token_file || '../.kira/token';
  const tokenAbsPath = path.resolve(__dirname, '../../', tokenRelPath);

  wsClient = new KiraWsClient({
    url: config.connection?.url || 'ws://127.0.0.1:8765',
    tokenPath: tokenAbsPath,
    reconnectMinMs: config.connection?.reconnect_min_ms || 1000,
    reconnectMaxMs: config.connection?.reconnect_max_ms || 10000,
    onEvent: (type, payload) => {
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('avatar:event', { type, payload });
      }
    }
  });

  wsClient.connect();
}

app.whenReady().then(() => {
  // Protocol handler for app:// if needed
  protocol.handle('app', (request) => {
    const url = new URL(request.url);
    const safePath = path.normalize(path.join(__dirname, '../..', url.pathname));
    return net.fetch(`file://${safePath}`);
  });

  createWindow();
  createTray();
  setupIpcHandlers();
  initWsClient();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on('window-all-closed', (event) => {
  // Never quit when window closes; avatar lives in tray
  event.preventDefault();
});

module.exports = {
  loadConfig,
  loadCharacterAssetAndMask,
  restoreOrCalculateBounds
};
