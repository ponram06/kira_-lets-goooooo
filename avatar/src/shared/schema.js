/**
 * avatar/src/shared/schema.js
 * Message formatting and validation against backend/server.py protocol.
 * Does NOT import electron.
 */

const ALLOWED_COMMANDS = Object.freeze([
  'stop',
  'confirm',
  'restricted mode',
  'mode'
]);

/**
 * Creates the kill switch stop command accepted by backend/server.py.
 * @returns {string} JSON string
 */
function createStopMessage() {
  return JSON.stringify({ command: 'stop' });
}

/**
 * Validates whether an outgoing or incoming command matches the backend schema.
 * @param {any} message - Raw message object or JSON string.
 * @returns {boolean}
 */
function isValidCommand(message) {
  let parsed = message;
  if (typeof message === 'string') {
    try {
      parsed = JSON.parse(message);
    } catch {
      return false;
    }
  }

  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return false;
  }

  const cmd = parsed.command;
  if (typeof cmd !== 'string') {
    return false;
  }

  const lowerCmd = cmd.trim().toLowerCase();
  if (!ALLOWED_COMMANDS.includes(lowerCmd)) {
    return false;
  }

  if (lowerCmd === 'confirm') {
    if (typeof parsed.question_id !== 'string' || !parsed.question_id.trim()) {
      return false;
    }
    if (typeof parsed.value !== 'string' || !['yes', 'no'].includes(parsed.value.trim().toLowerCase())) {
      return false;
    }
  }

  return true;
}

/**
 * Validates authentication token payload.
 * @param {string} token
 * @returns {boolean}
 */
function isValidTokenString(token) {
  return typeof token === 'string' && token.trim().length > 0;
}

module.exports = {
  ALLOWED_COMMANDS,
  createStopMessage,
  isValidCommand,
  isValidTokenString
};
