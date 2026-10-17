/**
 * avatar/src/main/preload.js
 * Secure context bridge exposing only whitelisted IPC methods.
 * Node integration is disabled; credentials are never exposed to renderer.
 */

const { contextBridge, ipcRenderer, webFrame } = require('electron');

// Hard lock visual zoom limits at Chromium webFrame level
try {
  webFrame.setVisualZoomLevelLimits(1, 1);
  webFrame.setZoomFactor(1.0);
} catch {
  // WebFrame locked
}

contextBridge.exposeInMainWorld('kiraAvatar', {
  getConfig: () => ipcRenderer.invoke('avatar:get-config'),

  // M8: returns { ok, bytes?: ArrayBuffer, reason? } for the character's .vrm
  loadModel: (profile) => ipcRenderer.invoke('avatar:load-model', {
    asset: profile && typeof profile.asset === 'string' ? profile.asset : null
  }),

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
  },

  // Opens the native avatar menu (hover control button or right-click)
  openMenu: () => {
    ipcRenderer.send('avatar:open-menu');
  },

  // UI state from main: { scale, feedVisible, locked, sleeping, micMuted, connected }
  onUi: (callback) => {
    if (typeof callback !== 'function') return () => {};
    const subscription = (_event, data) => callback(data);
    ipcRenderer.on('avatar:ui', subscription);
    return () => {
      ipcRenderer.removeListener('avatar:ui', subscription);
    };
  },

  // M2 Chat box controls
  onOpenChat: (callback) => {
    if (typeof callback !== 'function') return () => {};
    const subscription = () => callback();
    ipcRenderer.on('avatar:open-chat', subscription);
    return () => ipcRenderer.removeListener('avatar:open-chat', subscription);
  },

  onBlurChat: (callback) => {
    if (typeof callback !== 'function') return () => {};
    const subscription = () => callback();
    ipcRenderer.on('avatar:blur-chat', subscription);
    return () => ipcRenderer.removeListener('avatar:blur-chat', subscription);
  },

  closeChat: () => {
    ipcRenderer.send('avatar:close-chat');
  },

  sendText: (text) => {
    ipcRenderer.send('avatar:send-text', { text });
  }
});
