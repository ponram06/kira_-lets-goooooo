/**
 * avatar/src/main/ws-client.js
 * Electron Main Process WebSocket Client connecting to Kira backend.
 * Reads token from disk on every attempt; renderer NEVER sees the token.
 */

const fs = require('fs');
const path = require('path');
const WebSocket = require('ws');
const { createStopMessage, createCommand, isValidCommand } = require('../shared/schema');

class KiraWsClient {
  /**
   * @param {object} options
   * @param {string} options.url - WebSocket URL (ws://127.0.0.1:8765)
   * @param {string} options.tokenPath - Absolute or relative path to .kira/token
   * @param {number} options.reconnectMinMs - Minimum reconnect delay
   * @param {number} options.reconnectMaxMs - Maximum reconnect delay
   * @param {function(string, any): void} options.onEvent - Event dispatcher callback
   */
  constructor(options = {}) {
    this.url = options.url || 'ws://127.0.0.1:8765';
    this.tokenPath = options.tokenPath
      ? path.resolve(options.tokenPath)
      : path.resolve(__dirname, '../../../.kira/token');
    this.reconnectMinMs = options.reconnectMinMs || 1000;
    this.reconnectMaxMs = options.reconnectMaxMs || 10000;
    this.onEvent = options.onEvent || (() => {});

    this.ws = null;
    this.currentDelay = this.reconnectMinMs;
    this.reconnectTimer = null;
    this.isDestroyed = false;
    this.authenticated = false;
    this.pending = new Map();   // request_id → { resolve, timer }
    this._nextId = 1;
  }

  /**
   * Reads token from the token file on disk.
   * Returns null if file does not exist or cannot be read.
   * @returns {string | null}
   */
  readToken() {
    try {
      if (fs.existsSync(this.tokenPath)) {
        const token = fs.readFileSync(this.tokenPath, 'utf8').trim();
        return token || null;
      }
    } catch {
      // Token not available or file locked
    }
    return null;
  }

  /**
   * Starts connection attempt loop.
   */
  connect() {
    if (this.isDestroyed) return;
    this._clearReconnectTimer();

    const token = this.readToken();
    if (!token) {
      this._emitOffline();
      this._scheduleReconnect();
      return;
    }

    try {
      this.ws = new WebSocket(this.url);
    } catch {
      this._emitOffline();
      this._scheduleReconnect();
      return;
    }

    this.ws.on('open', () => {
      if (this.isDestroyed) return;
      // Send token immediately as first message as required by backend/server.py
      try {
        this.ws.send(JSON.stringify({ type: 'auth', token }));
        this.authenticated = true;
        this.currentDelay = this.reconnectMinMs; // Reset backoff upon successful connection
      } catch {
        this._cleanupSocket();
        this._scheduleReconnect();
      }
    });

    this.ws.on('message', (data) => {
      if (this.isDestroyed) return;
      this._handleMessage(data);
    });

    this.ws.on('close', () => {
      this._handleDisconnect();
    });

    this.ws.on('error', () => {
      this._handleDisconnect();
    });
  }

  /**
   * Handles incoming message from backend.
   * @param {Buffer | string} data
   */
  _handleMessage(data) {
    let payload;
    try {
      payload = JSON.parse(data.toString('utf8'));
    } catch {
      return; // Ignore malformed frames
    }

    if (!payload || typeof payload !== 'object') return;

    // Replies to request(): resolve the matching promise, do not forward
    if (payload.type === 'command_ack' && payload.request_id !== undefined) {
      const entry = this.pending.get(String(payload.request_id));
      if (entry) {
        clearTimeout(entry.timer);
        this.pending.delete(String(payload.request_id));
        entry.resolve(payload);
        return;
      }
    }

    const type = payload.type || payload.event;
    if (type) {
      this.onEvent(type, payload);
    } else if (payload.state) {
      this.onEvent('state', payload);
    }
  }

  /**
   * Handles socket disconnection or error.
   */
  _handleDisconnect() {
    this._failPending();
    this._cleanupSocket();
    this._emitOffline();
    this._scheduleReconnect();
  }

  /**
   * Sends the Stop command to set the backend kill switch.
   */
  sendStop() {
    if (this.ws && this.ws.readyState === WebSocket.OPEN && this.authenticated) {
      try {
        this.ws.send(createStopMessage());
        return true;
      } catch {
        return false;
      }
    }
    return false;
  }

  /**
   * Sends a validated control command and resolves with the backend's ack
   * (or {status: 'offline' | 'timeout' | 'invalid'}). Payload contents are
   * never logged.
   * @param {string} command
   * @param {object} fields
   * @param {number} timeoutMs
   * @returns {Promise<object>}
   */
  request(command, fields = {}, timeoutMs = 5000) {
    const id = `r${this._nextId++}`;
    const msg = createCommand(command, fields, id);
    if (!isValidCommand(msg)) return Promise.resolve({ status: 'invalid' });
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN || !this.authenticated) {
      return Promise.resolve({ status: 'offline' });
    }
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        resolve({ status: 'timeout' });
      }, timeoutMs);
      this.pending.set(id, { resolve, timer });
      try {
        this.ws.send(JSON.stringify(msg));
      } catch {
        clearTimeout(timer);
        this.pending.delete(id);
        resolve({ status: 'offline' });
      }
    });
  }

  /** Fire-and-forget command (used for shutdown during quit). */
  send(command, fields = {}) {
    const msg = createCommand(command, fields);
    if (!isValidCommand(msg)) return false;
    if (this.ws && this.ws.readyState === WebSocket.OPEN && this.authenticated) {
      try {
        this.ws.send(JSON.stringify(msg));
        return true;
      } catch {
        return false;
      }
    }
    return false;
  }

  isConnected() {
    return Boolean(this.ws && this.ws.readyState === WebSocket.OPEN && this.authenticated);
  }

  _failPending() {
    for (const [id, entry] of this.pending) {
      clearTimeout(entry.timer);
      entry.resolve({ status: 'offline' });
      this.pending.delete(id);
    }
  }

  _emitOffline() {
    this.authenticated = false;
    this.onEvent('state', { type: 'state', state: 'offline' });
  }

  _cleanupSocket() {
    if (this.ws) {
      try {
        this.ws.removeAllListeners();
        this.ws.close();
      } catch {
        // Ignored
      }
      this.ws = null;
    }
  }

  _clearReconnectTimer() {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  _scheduleReconnect() {
    if (this.isDestroyed || this.reconnectTimer) return;

    // Exponential backoff with jitter
    const jitter = Math.floor(Math.random() * 250);
    const delay = Math.min(this.reconnectMaxMs, this.currentDelay) + jitter;
    this.currentDelay = Math.min(this.reconnectMaxMs, Math.round(this.currentDelay * 1.5));

    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  /**
   * Cleanly closes connection and stops timers.
   */
  destroy() {
    this.isDestroyed = true;
    this._failPending();
    this._clearReconnectTimer();
    this._cleanupSocket();
  }
}

module.exports = { KiraWsClient };
