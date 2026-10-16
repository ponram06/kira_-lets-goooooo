/**
 * avatar/src/shared/hitmask.js
 * Alpha hit-mask generation and testing from bitmap buffers.
 * Does NOT import electron.
 */

/**
 * Builds a 1-bit per pixel hit-mask (stored in Uint8Array) from a 32-bit bitmap buffer.
 * In raw 32-bit bitmaps (RGBA or BGRA), alpha is stored in the 4th byte (offset + 3).
 * @param {Buffer | Uint8Array} bitmapBuffer - Raw 32-bit pixels (width * height * 4 bytes).
 * @param {number} width - Image width.
 * @param {number} height - Image height.
 * @param {number} alphaThreshold - Threshold (0-255) below which pixel is considered transparent.
 * @returns {Uint8Array} Array of length width * height, containing 1 (hit) or 0 (miss).
 */
function buildHitMask(bitmapBuffer, width, height, alphaThreshold = 20) {
  if (!bitmapBuffer || width <= 0 || height <= 0) {
    return new Uint8Array(0);
  }

  const totalPixels = width * height;
  const mask = new Uint8Array(totalPixels);
  const threshold = Math.max(1, Math.min(255, alphaThreshold));

  for (let i = 0; i < totalPixels; i++) {
    const byteOffset = i * 4;
    if (byteOffset + 3 < bitmapBuffer.length) {
      const alpha = bitmapBuffer[byteOffset + 3];
      if (alpha >= threshold) {
        mask[i] = 1;
      }
    }
  }

  return mask;
}

/**
 * Tests whether a point (x, y) relative to the image hits a non-transparent pixel.
 * @param {Uint8Array} mask - Precomputed hit mask.
 * @param {number} width - Image width.
 * @param {number} height - Image height.
 * @param {number} x - Pixel X coordinate.
 * @param {number} y - Pixel Y coordinate.
 * @returns {boolean} True if point hits non-transparent pixel.
 */
function isHit(mask, width, height, x, y) {
  if (!mask || mask.length === 0) return false;
  const ix = Math.floor(x);
  const iy = Math.floor(y);
  if (ix < 0 || ix >= width || iy < 0 || iy >= height) {
    return false;
  }
  return mask[iy * width + ix] === 1;
}

/**
 * Verifies that the hit mask is valid and not entirely transparent.
 * @param {Uint8Array} mask
 * @returns {boolean} True if mask has at least one solid pixel.
 */
function isMaskNonEmpty(mask) {
  if (!mask || mask.length === 0) return false;
  for (let i = 0; i < mask.length; i++) {
    if (mask[i] === 1) return true;
  }
  return false;
}

module.exports = {
  buildHitMask,
  isHit,
  isMaskNonEmpty
};
