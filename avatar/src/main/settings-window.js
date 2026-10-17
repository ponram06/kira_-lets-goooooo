/**
 * avatar/src/main/settings-window.js
 * Settings window. Closing it only hides it (avatar and backend keep running).
 *
 * Backend settings (keys, PIN, models, mic, voice, push-to-talk key, persona,
 * Free-mode timeout) are read and written by `python -m backend.settings_cli`
 * (payload over stdin). Avatar preferences (start with Windows, show/hide
 * hotkey) are applied here. Secrets are never logged.
 */

const { BrowserWindow, ipcMain } = require('electron');
const path = require('path');

class SettingsWindow {
  /**
   * @param {object} deps
   * @param {(action: string, payload?: object) => Promise<object>} deps.runCli
   * @param {() => object} deps.getAvatarPrefs           → { startWithWindows, showHideHotkey }
   * @param {(prefs: object) => object} deps.setAvatarPrefs → { ok, errors? }
   * @param {() => Promise<object>} deps.reloadBackend   → ws reload_settings
   * @param {() => Promise<string>} deps.restartBackend
   * @param {() => boolean} deps.isQuitting
   */
  constructor(deps) {
    this.deps = deps;
    this.win = null;
    this._registerIpc();
  }

  open() {
    if (this.win && !this.win.isDestroyed()) {
      this.win.show();
      this.win.focus();
      return;
    }
    this.win = new BrowserWindow({
      width: 640,
      height: 760,
      minWidth: 520,
      minHeight: 500,
      title: 'Kira Settings',
      autoHideMenuBar: true,
      show: false,
      webPreferences: {
        preload: path.join(__dirname, 'settings-preload.js'),
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        spellcheck: false
      }
    });
    this.win.setMenu(null);
    this.win.webContents.on('will-navigate', (e) => e.preventDefault());
    this.win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    this.win.loadFile(path.join(__dirname, '../renderer/settings.html'));
    this.win.once('ready-to-show', () => this.win && this.win.show());

    // Closing only hides; real close happens on quit
    this.win.on('close', (e) => {
      if (!this.deps.isQuitting()) {
        e.preventDefault();
        this.win.hide();
      }
    });
    this.win.on('closed', () => { this.win = null; });
  }

  destroy() {
    if (this.win && !this.win.isDestroyed()) this.win.destroy();
    this.win = null;
  }

  _guard(event) {
    if (!this.win || this.win.isDestroyed() || event.sender.id !== this.win.webContents.id) {
      throw new Error('Unauthorized IPC sender');
    }
  }

  _registerIpc() {
    ipcMain.handle('settings:get', async (event) => {
      this._guard(event);
      const backend = await this.deps.runCli('get');
      return { backend, avatar: this.deps.getAvatarPrefs() };
    });

    ipcMain.handle('settings:list-mics', async (event) => {
      this._guard(event);
      return this.deps.runCli('list-mics');
    });

    ipcMain.handle('settings:save', async (event, data) => {
      this._guard(event);
      const payload = data && typeof data === 'object' ? data : {};
      const errors = [];
      let needsRestart = false;

      if (payload.backend && typeof payload.backend === 'object') {
        const res = await this.deps.runCli('set', payload.backend);
        if (!res.ok) errors.push(...(res.errors || ['could not save']));
        needsRestart = Boolean(res.needs_restart);
      }
      if (payload.avatar && typeof payload.avatar === 'object') {
        const res = this.deps.setAvatarPrefs(payload.avatar);
        if (!res.ok) errors.push(...(res.errors || []));
      }
      if (!errors.length) {
        await this.deps.reloadBackend();
      }
      return { ok: errors.length === 0, errors, needsRestart };
    });

    ipcMain.handle('settings:test-key', async (event, data) => {
      this._guard(event);
      const provider = data && typeof data.provider === 'string' ? data.provider : '';
      const key = data && typeof data.key === 'string' ? data.key : '';
      return this.deps.runCli('test-key', key ? { provider, key } : { provider });
    });

    ipcMain.handle('settings:doctor', async (event) => {
      this._guard(event);
      return this.deps.runCli('doctor');
    });

    ipcMain.handle('settings:restart-backend', async (event) => {
      this._guard(event);
      return { status: await this.deps.restartBackend() };
    });
  }
}

module.exports = { SettingsWindow };
