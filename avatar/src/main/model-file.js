/**
 * avatar/src/main/model-file.js
 * Reads the character's .vrm file for the renderer (M8). The renderer is
 * sandboxed and cannot touch the disk; main validates the path and hands
 * over the bytes. Relative paths resolve from the project folder.
 */

'use strict';

const path = require('path');

const MAX_BYTES = 200 * 1024 * 1024;

/** Resolve *asset* to an absolute .vrm path. Returns { ok, file?, reason? }. */
function resolveModelPath(asset, projectRoot) {
  if (typeof asset !== 'string' || !asset.trim()) return { ok: false, reason: 'No 3D model file is set for this character.' };
  const raw = asset.trim();
  if (raw.includes('\0') || /^\\\\/.test(raw) || /^[a-z]+:\/\//i.test(raw)) {
    return { ok: false, reason: 'The 3D model path is not a local file.' };
  }
  const file = path.resolve(projectRoot, raw);
  if (path.extname(file).toLowerCase() !== '.vrm') return { ok: false, reason: 'The 3D model must be a .vrm file.' };
  return { ok: true, file };
}

/** Read the model. *fsMod* is injectable for tests. Returns { ok, bytes?, reason? }. */
function readModel(asset, projectRoot, fsMod = require('fs')) {
  const r = resolveModelPath(asset, projectRoot);
  if (!r.ok) return r;
  let st;
  try {
    st = fsMod.statSync(r.file);
  } catch {
    return { ok: false, reason: `3D model file not found (${path.basename(r.file)}).` };
  }
  if (!st.isFile()) return { ok: false, reason: 'The 3D model path is not a file.' };
  if (st.size > MAX_BYTES) return { ok: false, reason: 'The 3D model file is larger than 200 MB.' };
  if (st.size < 12) return { ok: false, reason: 'The 3D model file is empty or damaged.' };
  const buf = fsMod.readFileSync(r.file);
  if (buf.toString('latin1', 0, 4) !== 'glTF') return { ok: false, reason: 'The 3D model file is not a valid VRM (glTF binary).' };
  // Hand over an exact ArrayBuffer copy (structured-clone friendly)
  const bytes = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
  return { ok: true, bytes };
}

/** app:// protocol: map a URL path inside *root*, or null if it escapes. */
function safeAppPath(root, urlPath) {
  let decoded;
  try {
    decoded = decodeURIComponent(urlPath || '');
  } catch {
    return null;
  }
  if (decoded.includes('\0')) return null;
  const full = path.resolve(root, '.' + path.sep + decoded.replace(/^[/\\]+/, ''));
  const rel = path.relative(root, full);
  if (rel.startsWith('..') || path.isAbsolute(rel)) return null;
  return full;
}

module.exports = { resolveModelPath, readModel, safeAppPath, MAX_BYTES };
