/**
 * avatar/src/shared/bounds.js
 * Multi-monitor bounds clamping and default positioning.
 * Does NOT import electron.
 */

/**
 * Clamps coordinates to keep the window fully inside the display's work area.
 * @param {number} x - Target window X.
 * @param {number} y - Target window Y.
 * @param {number} width - Window width.
 * @param {number} height - Window height.
 * @param {{ x: number, y: number, width: number, height: number }} workArea - Display work area bounds.
 * @returns {{ x: number, y: number }}
 */
function clampToBounds(x, y, width, height, workArea) {
  if (!workArea) return { x: Math.round(x), y: Math.round(y) };

  const minX = workArea.x;
  const maxX = Math.max(workArea.x, workArea.x + workArea.width - width);
  const clampedX = Math.round(Math.max(minX, Math.min(x, maxX)));

  const minY = workArea.y;
  const maxY = Math.max(workArea.y, workArea.y + workArea.height - height);
  const clampedY = Math.round(Math.max(minY, Math.min(y, maxY)));

  return { x: clampedX, y: clampedY };
}

/**
 * Calculates the default bottom-right position above the taskbar.
 * @param {{ x: number, y: number, width: number, height: number }} workArea
 * @param {number} width
 * @param {number} height
 * @param {number} marginRight
 * @param {number} marginBottom
 * @returns {{ x: number, y: number }}
 */
function calculateDefaultPosition(workArea, width, height, marginRight = 20, marginBottom = 20) {
  const targetX = workArea.x + workArea.width - width - marginRight;
  const targetY = workArea.y + workArea.height - height - marginBottom;
  return clampToBounds(targetX, targetY, width, height, workArea);
}

/**
 * Finds which display contains the center point of the window.
 * @param {number} x
 * @param {number} y
 * @param {number} width
 * @param {number} height
 * @param {Array<{ workArea: { x: number, y: number, width: number, height: number } }>} displays
 * @returns {any} The display object or null
 */
function findDisplayForWindow(x, y, width, height, displays) {
  if (!Array.isArray(displays) || displays.length === 0) return null;

  const centerX = x + width / 2;
  const centerY = y + height / 2;

  // Check if center is directly inside any display work area
  for (const d of displays) {
    const wa = d.workArea || d.bounds;
    if (wa &&
        centerX >= wa.x && centerX < wa.x + wa.width &&
        centerY >= wa.y && centerY < wa.y + wa.height) {
      return d;
    }
  }

  // Fallback to closest display center
  let closest = displays[0];
  let minDistanceSq = Infinity;
  for (const d of displays) {
    const wa = d.workArea || d.bounds;
    if (!wa) continue;
    const dcX = wa.x + wa.width / 2;
    const dcY = wa.y + wa.height / 2;
    const distSq = (centerX - dcX) ** 2 + (centerY - dcY) ** 2;
    if (distSq < minDistanceSq) {
      minDistanceSq = distSq;
      closest = d;
    }
  }

  return closest;
}

/**
 * True if at least a usable part of the window (minVisible px in each
 * direction) overlaps some display's work area.
 */
function isOnAnyDisplay(x, y, width, height, displays, minVisible = 40) {
  if (!Array.isArray(displays)) return false;
  return displays.some((d) => {
    const wa = d.workArea || d.bounds;
    if (!wa) return false;
    const overlapX = Math.min(x + width, wa.x + wa.width) - Math.max(x, wa.x);
    const overlapY = Math.min(y + height, wa.y + wa.height) - Math.max(y, wa.y);
    return overlapX >= Math.min(minVisible, width) && overlapY >= Math.min(minVisible, height);
  });
}

/**
 * Start position on launch: the saved position if it is still on a
 * connected screen (clamped inside it), otherwise the default bottom-right
 * spot on the primary display (e.g. after a monitor was unplugged).
 * @param {{x:number,y:number}|null} saved
 */
function resolveStartPosition(saved, width, height, displays, primaryWorkArea, margins = {}) {
  const fallback = calculateDefaultPosition(
    primaryWorkArea, width, height, margins.right ?? 20, margins.bottom ?? 20
  );
  if (!saved || typeof saved.x !== 'number' || typeof saved.y !== 'number' ||
      !Number.isFinite(saved.x) || !Number.isFinite(saved.y)) {
    return fallback;
  }
  if (!isOnAnyDisplay(saved.x, saved.y, width, height, displays)) {
    return fallback;
  }
  const disp = findDisplayForWindow(saved.x, saved.y, width, height, displays);
  return clampToBounds(saved.x, saved.y, width, height, disp ? (disp.workArea || disp.bounds) : primaryWorkArea);
}

module.exports = {
  clampToBounds,
  calculateDefaultPosition,
  findDisplayForWindow,
  isOnAnyDisplay,
  resolveStartPosition
};
