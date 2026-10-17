/**
 * avatar/src/main/py-cli.js
 * Runs `python -m backend.settings_cli <action>` for the Settings window.
 * The payload (which may hold API keys or the PIN) goes through stdin only —
 * never argv, never logged. The reply is one JSON object on stdout.
 */

const childProcess = require('child_process');

const ALLOWED_ACTIONS = Object.freeze(['get', 'set', 'test-key', 'doctor', 'list-mics']);

/**
 * @param {{ python: string, cwd: string, spawn?: Function, timeoutMs?: number }} opts
 * @param {string} action
 * @param {object} payload
 * @returns {Promise<object>} parsed reply; {ok:false, errors:[...]} on failure
 */
function runPyCli(opts, action, payload = {}) {
  if (!ALLOWED_ACTIONS.includes(action)) {
    return Promise.resolve({ ok: false, errors: ['unknown action'] });
  }
  const spawn = opts.spawn || childProcess.spawn;
  const timeoutMs = opts.timeoutMs ?? 30000;

  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(opts.python, ['-m', 'backend.settings_cli', action], {
        cwd: opts.cwd,
        windowsHide: true,
        stdio: ['pipe', 'pipe', 'pipe'],
        env: { ...process.env, PYTHONIOENCODING: 'utf-8' }
      });
    } catch {
      resolve({ ok: false, errors: ['could not start Python'] });
      return;
    }

    let out = '';
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };
    const timer = setTimeout(() => {
      try { child.kill(); } catch { /* ignore */ }
      finish({ ok: false, errors: ['timed out'] });
    }, timeoutMs);

    child.stdout.on('data', (chunk) => {
      if (out.length < 1024 * 1024) out += chunk.toString('utf8');
    });
    child.stderr.on('data', () => { /* ignored: may contain tracebacks, never shown */ });
    child.on('error', () => finish({ ok: false, errors: ['could not start Python'] }));
    child.on('close', () => {
      const line = out.trim().split(/\r?\n/).pop() || '';
      try {
        const parsed = JSON.parse(line);
        finish(parsed && typeof parsed === 'object' ? parsed : { ok: false, errors: ['bad reply'] });
      } catch {
        finish({ ok: false, errors: ['bad reply from settings helper'] });
      }
    });

    child.stdin.end(JSON.stringify(payload || {}));
  });
}

module.exports = { runPyCli, ALLOWED_ACTIONS };
