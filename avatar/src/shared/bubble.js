/**
 * avatar/src/shared/bubble.js
 * Speech bubble text formatting, truncation, and sanitization.
 * Does NOT import electron.
 */

const MAX_BUBBLE_LENGTH = 140;

/**
 * Sanitizes and truncates text for the speech bubble.
 * Enforces text-only output, removes dangerous control characters,
 * and limits length to MAX_BUBBLE_LENGTH with ellipsis.
 * @param {any} text - Input text.
 * @param {number} maxLength - Maximum allowable characters.
 * @returns {string} Sanitized string.
 */
function sanitizeBubbleText(text, maxLength = MAX_BUBBLE_LENGTH) {
  if (text === null || text === undefined) {
    return '';
  }

  let str = String(text);

  // Strip null bytes and non-printable control characters (except newline & tab)
  str = str.replace(/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/g, '');

  // Normalize excessive internal whitespace
  str = str.replace(/\r\n/g, '\n').replace(/\r/g, '\n').trim();

  if (str.length > maxLength) {
    // Truncate at maxLength - 1 and append ellipsis
    return str.slice(0, maxLength - 1) + '…';
  }

  return str;
}

module.exports = {
  MAX_BUBBLE_LENGTH,
  sanitizeBubbleText
};
