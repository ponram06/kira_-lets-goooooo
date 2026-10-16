/**
 * avatar/src/shared/loudness.js
 * Loudness smoothing and mouth open threshold determination.
 * Does NOT import electron.
 */

/**
 * Exponential moving average (EMA) smoothing for loudness values.
 * @param {number} current - Currently smoothed value (0.0 to 1.0).
 * @param {number} target - New raw loudness reading (0.0 to 1.0).
 * @param {number} alpha - Smoothing factor between 0.0 and 1.0. Higher = faster response.
 * @returns {number}
 */
function smoothLoudness(current, target, alpha = 0.35) {
  const clampedTarget = Math.min(1.0, Math.max(0.0, Number(target) || 0.0));
  const clampedCurrent = Math.min(1.0, Math.max(0.0, Number(current) || 0.0));
  const a = Math.min(1.0, Math.max(0.01, Number(alpha) || 0.35));
  return clampedCurrent + a * (clampedTarget - clampedCurrent);
}

/**
 * Check whether loudness exceeds the threshold to display the mouth-open sprite.
 * @param {number} loudness - Smoothed loudness value.
 * @param {number} threshold - Threshold value (default 0.25).
 * @returns {boolean}
 */
function isMouthOpen(loudness, threshold = 0.25) {
  const val = Number(loudness) || 0.0;
  return val >= threshold;
}

module.exports = {
  smoothLoudness,
  isMouthOpen
};
