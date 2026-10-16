/**
 * avatar/src/shared/state-machine.js
 * Pure state machine and animation parameter calculations.
 * Does NOT import electron.
 */

const VALID_STATES = Object.freeze([
  'idle',
  'listening',
  'thinking',
  'working',
  'speaking',
  'error',
  'offline'
]);

/**
 * Normalizes an incoming backend state string.
 * @param {string} state
 * @returns {string}
 */
function normalizeState(state) {
  if (typeof state !== 'string') return 'idle';
  const s = state.trim().toLowerCase();
  return VALID_STATES.includes(s) ? s : 'idle';
}

/**
 * Computes rendering parameters for the given state and clock time.
 * @param {string} state - The normalized state.
 * @param {number} timeSec - Running time in seconds.
 * @param {number} smoothedLoudness - Value between 0.0 and 1.0.
 * @returns {{ scaleX: number, scaleY: number, offsetY: number, offsetX: number, tiltDeg: number, eyeGlow: number, appleGlow: number, isDimmed: boolean, isRedFlash: boolean }}
 */
function computeAnimation(state, timeSec, smoothedLoudness = 0.0) {
  const normState = normalizeState(state);

  let scaleX = 1.0;
  let scaleY = 1.0;
  let offsetX = 0.0;
  let offsetY = 0.0;
  let tiltDeg = 0.0;
  let eyeGlow = 1.0;
  let appleGlow = 1.0;
  let isDimmed = false;
  let isRedFlash = false;

  switch (normState) {
    case 'idle': {
      // Slow breathing: tiny scale and vertical movement (period ~4s)
      const breath = Math.sin(timeSec * Math.PI * 0.5);
      scaleY = 1.0 + breath * 0.015;
      offsetY = breath * 2.0;
      eyeGlow = 0.8 + 0.2 * breath;
      appleGlow = 1.0;
      break;
    }
    case 'listening': {
      // Lean slightly forward, eyes glow brighter
      tiltDeg = 2.5;
      offsetY = 3.0;
      eyeGlow = 1.6;
      appleGlow = 1.0;
      break;
    }
    case 'thinking': {
      // Apple glow pulses slowly (period ~2s)
      const pulse = 0.5 + 0.5 * Math.sin(timeSec * Math.PI);
      appleGlow = 0.7 + pulse * 0.8;
      eyeGlow = 1.0;
      offsetY = Math.sin(timeSec * Math.PI * 0.5) * 1.5;
      break;
    }
    case 'working': {
      // Faster apple pulse and small side-to-side sway
      const fastPulse = 0.5 + 0.5 * Math.sin(timeSec * Math.PI * 2.5);
      appleGlow = 0.8 + fastPulse * 1.0;
      offsetX = Math.sin(timeSec * Math.PI * 2.0) * 3.0;
      offsetY = Math.cos(timeSec * Math.PI * 2.0) * 1.5;
      eyeGlow = 1.2;
      break;
    }
    case 'speaking': {
      // Bob and glow follow loudness
      const bob = Math.min(1.0, Math.max(0.0, smoothedLoudness));
      offsetY = (-bob * 6.0) || 0.0;
      scaleY = 1.0 + bob * 0.04;
      eyeGlow = 1.0 + bob * 0.5;
      appleGlow = 1.0 + bob * 0.6;
      break;
    }
    case 'error': {
      // Brief red flash
      isRedFlash = true;
      eyeGlow = 1.8;
      appleGlow = 1.8;
      offsetX = (Math.random() - 0.5) * 4.0;
      break;
    }
    case 'offline': {
      // Dimmed and desaturated
      isDimmed = true;
      eyeGlow = 0.2;
      appleGlow = 0.2;
      break;
    }
  }

  return {
    scaleX,
    scaleY,
    offsetX,
    offsetY,
    tiltDeg,
    eyeGlow,
    appleGlow,
    isDimmed,
    isRedFlash
  };
}

module.exports = {
  VALID_STATES,
  normalizeState,
  computeAnimation
};
