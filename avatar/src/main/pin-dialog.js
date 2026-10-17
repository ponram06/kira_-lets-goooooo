/**
 * avatar/src/main/pin-dialog.js
 * Free-mode confirmation dialog: explains what Free mode does and asks for
 * the PIN in a masked field. The PIN goes renderer → main (IPC) → backend
 * (`free_mode_request` over the token-authenticated WebSocket) → ModeManager.
 * It is never logged, stored, or put in events; only the result status
 * comes back.
 */

const { BrowserWindow, ipcMain } = require('electron');
const path = require('path');

const PIN_RE = /^\d{4,12}$/;

class PinDialog {
  /**
   * @param {{ request: (cmd: string, fields: object) => Promise<object>, getFreeTimeout: () => number }} deps
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
      width: 380,
      height: 300,
      resizable: false,
      minimizable: false,
      maximizable: false,
      alwaysOnTop: true,
      title: 'Enable Free mode',
      autoHideMenuBar: true,
      show: false,
      webPreferences: {
        preload: path.join(__dirname, 'pin-preload.js'),
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        spellcheck: false
      }
    });
    this.win.setMenu(null);
    this.win.webContents.on('will-navigate', (e) => e.preventDefault());
    this.win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    this.win.loadFile(path.join(__dirname, '../renderer/pin.html'));
    this.win.once('ready-to-show', () => this.win && this.win.show());
    this.win.on('closed', () => { this.win = null; });
  }

  close() {
    if (this.win && !this.win.isDestroyed()) this.win.close();
  }

  _isOwnSender(event) {
    return Boolean(this.win && !this.win.isDestroyed() && event.sender.id === this.win.webContents.id);
  }

  _registerIpc() {
    ipcMain.handle('pin:info', (event) => {
      if (!this._isOwnSender(event)) throw new Error('Unauthorized IPC sender');
      return { freeTimeout: this.deps.getFreeTimeout() };
    });

    ipcMain.handle('pin:submit', async (event, pin) => {
      if (!this._isOwnSender(event)) throw new Error('Unauthorized IPC sender');
      if (typeof pin !== 'string' || !PIN_RE.test(pin)) {
        return { status: 'invalid' };
      }
      const ack = await this.deps.request('free_mode_request', { pin });
      pin = null;
      const result = { status: ack.status || 'error' };
      if (typeof ack.lockout_seconds === 'number') result.lockoutSeconds = ack.lockout_seconds;
      if (result.status === 'ok') setTimeout(() => this.close(), 600);
      return result;
    });

    ipcMain.on('pin:cancel', (event) => {
      if (this._isOwnSender(event)) this.close();
    });
  }
}

module.exports = { PinDialog, PIN_RE };
