/**
 * highlight-overlay.js
 * M6: shows a short red ring where screen control is about to act, so the
 * user sees the target before the click. A tiny transparent, click-through,
 * non-focusable window is placed over the point and closes by itself.
 * The backend sends physical pixels; Electron windows use DIPs, so the point
 * is converted with screen.screenToDipPoint on Windows.
 */

'use strict';

const RING = 64;

/** Window bounds (DIP) centred on a physical point. Pure, for tests. */
function highlightBounds(msg, toDip, size = RING) {
  if (!msg || typeof msg.x !== 'number' || typeof msg.y !== 'number') return null;
  const { x, y } = msg;
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  const p = toDip ? toDip({ x: Math.round(x), y: Math.round(y) }) : { x, y };
  return { x: Math.round(p.x - size / 2), y: Math.round(p.y - size / 2), width: size, height: size };
}

function durationMs(msg) {
  const ms = Number(msg && msg.ms);
  return Number.isFinite(ms) ? Math.max(100, Math.min(3000, ms)) : 700;
}

const PAGE = 'data:text/html;charset=utf-8,' + encodeURIComponent(
  '<!doctype html><html><body style="margin:0;background:transparent;overflow:hidden">' +
  '<div style="box-sizing:border-box;width:100vw;height:100vh;border:4px solid #e0102f;' +
  'border-radius:50%;box-shadow:0 0 12px #e0102f"></div></body></html>'
);

/** Show the ring. *BrowserWindow* and *screen* are injected (Electron in prod). */
function showHighlight(msg, { BrowserWindow, screen, setTimer = setTimeout }) {
  const toDip = screen && process.platform === 'win32' && screen.screenToDipPoint
    ? (p) => screen.screenToDipPoint(p) : null;
  const bounds = highlightBounds(msg, toDip);
  if (!bounds) return null;
  const win = new BrowserWindow({
    ...bounds, frame: false, transparent: true, alwaysOnTop: true, focusable: false,
    skipTaskbar: true, resizable: false, show: false, hasShadow: false,
    webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false, javascript: false }
  });
  win.setIgnoreMouseEvents(true);
  win.setAlwaysOnTop(true, 'screen-saver');
  win.loadURL(PAGE);
  win.once('ready-to-show', () => win.showInactive());
  setTimer(() => { if (!win.isDestroyed()) win.close(); }, durationMs(msg));
  return win;
}

module.exports = { highlightBounds, durationMs, showHighlight, RING };
