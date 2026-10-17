/**
 * avatar/src/main/pin-preload.js
 * Minimal bridge for the Free-mode PIN dialog.
 */

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('kiraPin', {
  info: () => ipcRenderer.invoke('pin:info'),
  submit: (pin) => ipcRenderer.invoke('pin:submit', String(pin || '')),
  cancel: () => ipcRenderer.send('pin:cancel')
});
