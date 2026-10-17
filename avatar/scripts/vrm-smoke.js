/**
 * avatar/scripts/vrm-smoke.js
 * Electron smoke test for the real 3D path (M8): loads the real renderer
 * page (index.html, import map, three.js, three-vrm) in a HIDDEN window with
 * the real preload, feeds it a generated test VRM and a sprite fallback case,
 * and prints PASS/FAIL. No backend, no network, nothing shown on screen.
 *
 *   npx electron scripts/vrm-smoke.js
 */

'use strict';

const { app, BrowserWindow, ipcMain } = require('electron');
const path = require('path');
const { buildVrm } = require('./make-test-vrm');

const bytes = buildVrm();
const model = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
const errors = [];
const timeout = setTimeout(() => finish('FAIL: timeout'), 30000);

function finish(result) {
  clearTimeout(timeout);
  console.log(result);
  if (errors.length) console.log('console errors:\n  ' + errors.join('\n  '));
  app.exit(result.startsWith('PASS') ? 0 : 1);
}

app.whenReady().then(async () => {
  const win = new BrowserWindow({
    show: false, width: 380, height: 310, transparent: true,
    webPreferences: {
      preload: path.join(__dirname, '../src/main/preload.js'),
      contextIsolation: true, nodeIntegration: false, sandbox: true, backgroundThrottling: false
    }
  });
  win.webContents.on('console-message', (e) => {
    const level = e.level ?? e.params?.level;
    const msg = e.message ?? e.params?.message;
    if (level === 'error' || level === 3) errors.push(String(msg).slice(0, 300));
  });
  ipcMain.handle('avatar:get-config', () => ({
    config: { window: { width: 380, height: 310 } },
    characterMeta: {}, hitMask: [], ui: { lowPower: false },
    character: { id: 'kira', display_name: 'Kira', renderer: 'vrm', asset: 'models/test.vrm' }
  }));
  ipcMain.handle('avatar:load-model', () => ({ ok: true, bytes: model }));

  await win.loadFile(path.join(__dirname, '../src/renderer/index.html'));
  const read = () => win.webContents.executeJavaScript(
    "({ vrm: document.getElementById('vrm-canvas').style.display, sprite: document.getElementById('avatar-canvas').style.display, bubble: document.getElementById('bubble-text').textContent })");
  for (let i = 0; i < 40; i++) {
    const s = await read();
    if (s.vrm === 'block' && s.sprite === 'none') return finish('PASS: VRM loaded through the real import map, three.js and three-vrm');
    if (/2D avatar/.test(s.bubble)) return finish(`FAIL: fell back to sprite: ${s.bubble}`);
    await new Promise((r) => setTimeout(r, 500));
  }
  finish('FAIL: VRM renderer never became active');
});
