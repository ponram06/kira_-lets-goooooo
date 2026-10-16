/**
 * avatar/src/main/preload.js
 * Secure context bridge exposing only whitelisted IPC methods.
 * Node integration is disabled; credentials are never exposed to renderer.
 */

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('kiraAvatar', {
  getConfig: () => ipcRenderer.invoke('avatar:get-config'),

  onEvent: (callback) => {
    if (typeof callback !== 'function') return () => {};
    const subscription = (_event, data) => callback(data);
    ipcRenderer.on('avatar:event', subscription);
    return () => {
      ipcRenderer.removeListener('avatar:event', subscription);
    };
  },

  startDrag: () => {
    ipcRenderer.send('avatar:drag-start');
  },

  moveDrag: (delta) => {
    if (delta && typeof delta.dx === 'number' && typeof delta.dy === 'number') {
      ipcRenderer.send('avatar:drag-move', { dx: delta.dx, dy: delta.dy });
    }
  },

  endDrag: () => {
    ipcRenderer.send('avatar:drag-end');
  },

  setIgnoreMouseEvents: (ignore, forward = true) => {
    ipcRenderer.send('avatar:set-ignore-mouse-events', {
      ignore: Boolean(ignore),
      forward: Boolean(forward)
    });
  },

  sendStop: () => {
    ipcRenderer.send('avatar:stop');
  }
});
