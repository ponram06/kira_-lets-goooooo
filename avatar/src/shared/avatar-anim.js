/**
 * avatar/src/shared/avatar-anim.js
 * Procedural animation for the 3D avatar (M8), GPU-free so it can be tested:
 * state → pose targets, idle breathing/sway, blinking, smoothed look-at and
 * small gestures. Angles are radians. Renderer-agnostic.
 */

(function (root) {
  // Pose targets per assistant state (bone rotations, radians)
  const POSES = Object.freeze({
    idle:      { headPitch: 0.00, headYaw: 0.00, spineLean: 0.00, armDown: 1.20, energy: 1.0 },
    listening: { headPitch: -0.06, headYaw: 0.10, spineLean: 0.04, armDown: 1.15, energy: 1.0 },
    thinking:  { headPitch: 0.10, headYaw: -0.18, spineLean: -0.02, armDown: 1.10, energy: 0.8 },
    working:   { headPitch: 0.14, headYaw: 0.00, spineLean: 0.06, armDown: 1.05, energy: 0.9 },
    speaking:  { headPitch: -0.02, headYaw: 0.00, spineLean: 0.02, armDown: 1.15, energy: 1.2 },
    error:     { headPitch: 0.18, headYaw: 0.00, spineLean: -0.06, armDown: 1.25, energy: 0.6 },
    sleeping:  { headPitch: 0.30, headYaw: 0.00, spineLean: 0.08, armDown: 1.25, energy: 0.3 }
  });

  /** Emotion implied by a state when the brain sends none. */
  const STATE_EMOTION = Object.freeze({ error: 'sad', speaking: 'neutral', idle: 'neutral' });

  function stateToPose(state) {
    return POSES[state] || POSES.idle;
  }

  /** Move *current* toward *target* with exponential smoothing (frame-rate independent). */
  function approach(current, target, dt, rate) {
    const k = 1 - Math.exp(-Math.max(0, rate) * Math.max(0, dt));
    return current + (target - current) * k;
  }

  function blendPose(current, target, dt, rate = 6) {
    const out = {};
    for (const key of Object.keys(target)) {
      const c = typeof current?.[key] === 'number' ? current[key] : target[key];
      out[key] = approach(c, target[key], dt, rate);
    }
    return out;
  }

  /** Breathing (chest scale offset) and sway (hip roll) at time t seconds. */
  function idleMotion(t, energy = 1) {
    const e = Math.max(0, energy);
    return {
      breath: 0.012 * e * Math.sin((2 * Math.PI * t) / 4.0),
      sway: 0.025 * e * Math.sin((2 * Math.PI * t) / 7.3),
      nod: 0.01 * e * Math.sin((2 * Math.PI * t) / 5.1 + 1.3)
    };
  }

  /** Blink scheduler: random gaps of 2–6 s, 150 ms blinks. rng injectable. */
  class Blinker {
    constructor(rng = Math.random, startT = 0) {
      this.rng = rng;
      this.next = startT + this._gap();
      this.closeFor = 0.15;
    }
    _gap() { return 2 + 4 * this.rng(); }
    /** Eye-closed weight 0..1 at time t (seconds). */
    value(t) {
      if (t < this.next) return 0;
      const p = (t - this.next) / this.closeFor;
      if (p >= 1) { this.next = t + this._gap(); return 0; }
      return Math.sin(Math.PI * p);
    }
  }

  const LOOK_LIMIT = { yaw: 0.6, pitch: 0.35 };

  /** Target head angles that look at a cursor position (normalised -1..1). */
  function lookTarget(nx, ny) {
    const cx = Math.max(-1, Math.min(1, Number(nx) || 0));
    const cy = Math.max(-1, Math.min(1, Number(ny) || 0));
    return { yaw: cx * LOOK_LIMIT.yaw, pitch: cy * LOOK_LIMIT.pitch };
  }

  function smoothLook(current, target, dt, rate = 4) {
    return { yaw: approach(current.yaw, target.yaw, dt, rate), pitch: approach(current.pitch, target.pitch, dt, rate) };
  }

  // Small gestures on task start / finish
  const GESTURES = Object.freeze({
    task_start: { name: 'nod', duration: 0.6 },
    task_done: { name: 'tilt', duration: 0.9 },
    error: { name: 'shake', duration: 0.8 }
  });

  function gestureFor(eventType) {
    if (eventType === 'step_start' || eventType === 'tool_start') return GESTURES.task_start;
    if (eventType === 'tool_done' || eventType === 'reply') return GESTURES.task_done;
    if (eventType === 'tool_error' || eventType === 'error') return GESTURES.error;
    return null;
  }

  /** Gesture offsets at progress p in [0, 1]; zero outside. */
  function gestureOffset(name, p) {
    if (!(p >= 0 && p <= 1)) return { headPitch: 0, headYaw: 0, headRoll: 0 };
    const env = Math.sin(Math.PI * p);
    if (name === 'nod') return { headPitch: 0.18 * env * Math.sin(2 * Math.PI * p), headYaw: 0, headRoll: 0 };
    if (name === 'tilt') return { headPitch: 0, headYaw: 0, headRoll: 0.15 * env };
    if (name === 'shake') return { headPitch: 0, headYaw: 0.2 * env * Math.sin(4 * Math.PI * p), headRoll: 0 };
    return { headPitch: 0, headYaw: 0, headRoll: 0 };
  }

  const api = { POSES, STATE_EMOTION, stateToPose, approach, blendPose, idleMotion, Blinker,
    lookTarget, smoothLook, gestureFor, gestureOffset, LOOK_LIMIT };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.KiraAnim = api;
})(typeof window !== 'undefined' ? window : globalThis);
