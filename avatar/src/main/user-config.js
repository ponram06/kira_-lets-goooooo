/**
 * avatar/src/main/user-config.js
 * Avatar UI preferences in the per-user Kira folder (never in git):
 *   %KIRA_USER_DIR%\avatar.json  or  %APPDATA%\Kira\avatar.json
 *
 * The backend owns persona and mic mute (settings.json in the same folder);
 * this file holds window and UI preferences only. The permission mode is
 * never stored anywhere: Kira always starts Restricted.
 * Does NOT import electron (the caller passes the folder in).
 */

const fs = require('fs');
const path = require('path');
const { SIZE_NAMES } = require('../shared/sizes');

const FILE_NAME = 'avatar.json';

const DEFAULTS = Object.freeze({
  position: null,             // {x, y} or null → default bottom-right
  size: 'medium',             // small | medium | large
  locked: false,              // lock position (no dragging)
  feedVisible: true,          // action feed panel
  lowPower: false,            // M8: 2D avatar only, lower frame rate
  startWithWindows: false,    // login item (default off)
  showHideHotkey: 'Ctrl+Alt+H'
});

/** Accelerator strings like "Ctrl+Alt+H" / "CommandOrControl+Shift+F12". */
const HOTKEY_RE = /^((CommandOrControl|CmdOrCtrl|Control|Ctrl|Alt|Shift|Super)\+){1,3}([A-Z0-9]|F([1-9]|1[0-9]|2[0-4]))$/i;

function isValidHotkey(value) {
  return typeof value === 'string' && value.length <= 40 && HOTKEY_RE.test(value);
}

/** Resolve the per-user Kira folder. */
function userDir(appDataDir, env = process.env) {
  if (env.KIRA_USER_DIR) return env.KIRA_USER_DIR;
  return path.join(appDataDir, 'Kira');
}

/** Keep only known keys with valid values; anything else falls back to defaults. */
function sanitize(raw) {
  const out = { ...DEFAULTS };
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return out;

  const pos = raw.position;
  if (pos && Number.isFinite(pos.x) && Number.isFinite(pos.y)) {
    out.position = { x: Math.round(pos.x), y: Math.round(pos.y) };
  }
  if (SIZE_NAMES.includes(raw.size)) out.size = raw.size;
  if (typeof raw.locked === 'boolean') out.locked = raw.locked;
  if (typeof raw.feedVisible === 'boolean') out.feedVisible = raw.feedVisible;
  if (typeof raw.lowPower === 'boolean') out.lowPower = raw.lowPower;
  if (typeof raw.startWithWindows === 'boolean') out.startWithWindows = raw.startWithWindows;
  if (isValidHotkey(raw.showHideHotkey)) out.showHideHotkey = raw.showHideHotkey;
  return out;
}

/** Load avatar.json (missing or corrupt → defaults). */
function loadUserConfig(dir, fsImpl = fs) {
  try {
    const text = fsImpl.readFileSync(path.join(dir, FILE_NAME), 'utf8');
    return sanitize(JSON.parse(text));
  } catch {
    return { ...DEFAULTS };
  }
}

/** Save atomically (write temp file, then rename). Returns the stored object. */
function saveUserConfig(dir, data, fsImpl = fs) {
  const clean = sanitize(data);
  try {
    fsImpl.mkdirSync(dir, { recursive: true });
    const target = path.join(dir, FILE_NAME);
    const tmp = `${target}.${process.pid}.tmp`;
    fsImpl.writeFileSync(tmp, JSON.stringify(clean, null, 2), 'utf8');
    fsImpl.renameSync(tmp, target);
  } catch {
    // Non-critical: preferences simply are not remembered this time
  }
  return clean;
}

module.exports = { DEFAULTS, FILE_NAME, isValidHotkey, userDir, sanitize, loadUserConfig, saveUserConfig };
