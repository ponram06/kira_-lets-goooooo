/**
 * avatar/src/shared/sizes.js
 * Avatar size presets. The whole window (canvas, drag area, button, bubble)
 * is scaled with one CSS transform, so only the window size changes here.
 * Does NOT import electron.
 */

const SIZE_SCALES = Object.freeze({ small: 0.8, medium: 1.0, large: 1.25 });
const SIZE_NAMES = Object.freeze(Object.keys(SIZE_SCALES));

/** Returns the scale for a size name (unknown → medium). */
function scaleFor(size) {
  return Object.prototype.hasOwnProperty.call(SIZE_SCALES, size) ? SIZE_SCALES[size] : 1.0;
}

/**
 * Window size for a base size and a size name.
 * @param {{ width: number, height: number }} base
 * @param {string} size
 */
function scaledWindowSize(base, size) {
  const s = scaleFor(size);
  return {
    width: Math.round((base.width || 380) * s),
    height: Math.round((base.height || 310) * s)
  };
}

module.exports = { SIZE_SCALES, SIZE_NAMES, scaleFor, scaledWindowSize };
