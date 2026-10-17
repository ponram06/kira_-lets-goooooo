/**
 * avatar/src/main/settings-preload.js
 * Bridge for the Settings window (only these calls are exposed).
 */

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('kiraSettings', {
  get: () => ipcRenderer.invoke('settings:get'),
  listMics: () => ipcRenderer.invoke('settings:list-mics'),
  save: (data) => ipcRenderer.invoke('settings:save', data),
  testKey: (provider, key) => ipcRenderer.invoke('settings:test-key', { provider, key }),
  doctor: () => ipcRenderer.invoke('settings:doctor'),
  restartBackend: () => ipcRenderer.invoke('settings:restart-backend')
});
