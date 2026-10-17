/**
 * avatar/src/main/backend-process.js
 * Starts and stops the Python backend.
 *
 * - ensureStarted(): if something already listens on the backend port (you
 *   ran run.bat yourself), leave it alone; otherwise spawn
 *   `python -m backend.main --headless` with output appended to logs/backend.log.
 * - stop(): ask the backend to shut down (WebSocket `shutdown` command), wait
 *   up to timeoutMs, then kill the whole process tree (taskkill /T /F) so no
 *   stray processes remain. A backend we did not start is only asked.
 *
 * spawn, killTree, probePort and fs are injectable for tests.
 */

const childProcess = require('child_process');
const fs = require('fs');
const net = require('net');
const path = require('path');

/** Resolves true if a TCP server accepts connections on host:port. */
function defaultProbePort(host, port, timeoutMs = 500) {
  return new Promise((resolve) => {
    const sock = net.connect({ host, port });
    const done = (ok) => { sock.destroy(); resolve(ok); };
    sock.setTimeout(timeoutMs, () => done(false));
    sock.once('connect', () => done(true));
    sock.once('error', () => done(false));
  });
}

/** Kill a process and all its children (Windows). */
function defaultKillTree(pid) {
  return new Promise((resolve) => {
    const killer = childProcess.spawn('taskkill', ['/PID', String(pid), '/T', '/F'], {
      windowsHide: true,
      stdio: 'ignore'
    });
    killer.once('exit', () => resolve());
    killer.once('error', () => resolve());
  });
}

class BackendProcess {
  /**
   * @param {object} o
   * @param {string} o.python    - python executable (absolute, from the project .venv)
   * @param {string} o.cwd       - project root
   * @param {string} o.logPath   - logs/backend.log
   * @param {string} o.host
   * @param {number} o.port
   * @param {number} [o.timeoutMs=5000]
   */
  constructor(o) {
    this.python = o.python;
    this.cwd = o.cwd;
    this.logPath = o.logPath;
    this.host = o.host || '127.0.0.1';
    this.port = o.port || 8765;
    this.timeoutMs = o.timeoutMs ?? 5000;
    this.args = o.args || ['-m', 'backend.main', '--headless'];
    this.spawn = o.spawn || childProcess.spawn;
    this.killTree = o.killTree || defaultKillTree;
    this.probePort = o.probePort || defaultProbePort;
    this.fs = o.fs || fs;
    this.onExit = o.onExit || (() => {});

    this.child = null;
    this.external = false;
    this._logStream = null;
  }

  /** True while a backend we spawned is running. */
  isOwned() {
    return Boolean(this.child && this.child.exitCode === null && !this.child.killed);
  }

  /** @returns {Promise<'external'|'started'|'missing-python'>} */
  async ensureStarted() {
    if (this.isOwned()) return 'started';
    if (await this.probePort(this.host, this.port)) {
      this.external = true;
      return 'external';
    }
    this.external = false;
    if (!this.python || !this.fs.existsSync(this.python)) {
      return 'missing-python';
    }

    try {
      this.fs.mkdirSync(path.dirname(this.logPath), { recursive: true });
      this._logStream = this.fs.createWriteStream(this.logPath, { flags: 'a' });
    } catch {
      this._logStream = null;
    }

    const child = this.spawn(this.python, this.args, {
      cwd: this.cwd,
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' }
    });
    this.child = child;
    if (this._logStream) {
      child.stdout?.pipe(this._logStream, { end: false });
      child.stderr?.pipe(this._logStream, { end: false });
    }
    child.once('exit', (code) => {
      if (this.child === child) this.child = null;
      this._closeLog();
      this.onExit(code);
    });
    child.once('error', () => {
      if (this.child === child) this.child = null;
      this._closeLog();
    });
    return 'started';
  }

  /**
   * Clean shutdown. sendShutdown() should send the WebSocket `shutdown`
   * command and return true if it was sent.
   * @param {() => boolean} sendShutdown
   * @returns {Promise<'not-running'|'external-asked'|'exited'|'killed'>}
   */
  async stop(sendShutdown) {
    if (!this.isOwned()) {
      if (this.external) {
        try { sendShutdown(); } catch { /* ignore */ }
        return 'external-asked';
      }
      return 'not-running';
    }
    const child = this.child;
    let sent = false;
    try { sent = Boolean(sendShutdown()); } catch { sent = false; }

    const exited = sent ? await this._waitForExit(child, this.timeoutMs) : false;
    if (exited) return 'exited';
    await this.killTree(child.pid);
    await this._waitForExit(child, 2000);
    return 'killed';
  }

  async restart(sendShutdown) {
    await this.stop(sendShutdown);
    // Give the OS a moment to release the port before probing again
    await new Promise((r) => setTimeout(r, 300));
    return this.ensureStarted();
  }

  _waitForExit(child, ms) {
    if (child.exitCode !== null) return Promise.resolve(true);
    return new Promise((resolve) => {
      const timer = setTimeout(() => resolve(false), ms);
      child.once('exit', () => { clearTimeout(timer); resolve(true); });
    });
  }

  _closeLog() {
    if (this._logStream) {
      try { this._logStream.end(); } catch { /* ignore */ }
      this._logStream = null;
    }
  }
}

/** Project .venv python path on Windows (relative to the project root). */
function defaultPythonPath(projectRoot, configured) {
  if (configured) return path.resolve(projectRoot, configured);
  return path.join(projectRoot, '.venv', 'Scripts', 'python.exe');
}

module.exports = { BackendProcess, defaultProbePort, defaultKillTree, defaultPythonPath };
