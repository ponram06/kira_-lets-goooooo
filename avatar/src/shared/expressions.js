/**
 * avatar/src/shared/expressions.js
 * Emotion tags → VRM expressions with smooth blending, and lip sync from
 * loudness to the five mouth shapes (M8). Supports VRM 1.0 and 0.x names.
 */

(function (root) {
  const EMOTIONS = ['happy', 'angry', 'sad', 'surprised', 'relaxed'];
  const VISEMES = ['aa', 'ih', 'ou', 'ee', 'oh'];

  // Canonical (VRM 1.0) name → aliases used by VRM 0.x models / exporters
  const ALIASES = Object.freeze({
    happy: ['happy', 'joy', 'Joy', 'HAPPY'],
    angry: ['angry', 'Angry', 'ANGRY'],
    sad: ['sad', 'sorrow', 'Sorrow', 'SAD'],
    surprised: ['surprised', 'Surprised', 'surprise'],
    relaxed: ['relaxed', 'fun', 'Fun', 'RELAXED'],
    aa: ['aa', 'a', 'A'],
    ih: ['ih', 'i', 'I'],
    ou: ['ou', 'u', 'U'],
    ee: ['ee', 'e', 'E'],
    oh: ['oh', 'o', 'O'],
    blink: ['blink', 'Blink', 'BLINK']
  });

  /** First alias the model actually has (availableNames: array or Set), or null. */
  function resolveName(canonical, availableNames) {
    const have = availableNames instanceof Set ? availableNames : new Set(availableNames || []);
    for (const n of ALIASES[canonical] || [canonical]) if (have.has(n)) return n;
    return null;
  }

  /** "[emotion:happy] Done!" → { emotion: 'happy', text: 'Done!' }. Unknown tags are removed. */
  function parseEmotionTag(text) {
    if (typeof text !== 'string') return { emotion: null, text: '' };
    let emotion = null;
    const clean = text.replace(/\[(?:emotion|mood):\s*([a-z]+)\]/gi, (_m, e) => {
      const k = e.toLowerCase();
      if (!emotion && (EMOTIONS.includes(k) || k === 'neutral')) emotion = k;
      return '';
    }).replace(/\s{2,}/g, ' ').trim();
    return { emotion, text: clean };
  }

  /**
   * One blending step: every emotion weight moves toward 1 for the target and
   * 0 for the rest; weights stay in [0,1] and never sum above 1.
   */
  function blendEmotions(weights, target, dt, rate = 5) {
    const k = 1 - Math.exp(-rate * Math.max(0, dt));
    const out = {};
    let sum = 0;
    for (const e of EMOTIONS) {
      const cur = Math.min(1, Math.max(0, Number(weights?.[e]) || 0));
      const goal = e === target ? 1 : 0;
      out[e] = cur + (goal - cur) * k;
      sum += out[e];
    }
    if (sum > 1) for (const e of EMOTIONS) out[e] /= sum;
    return out;
  }

  /**
   * Lip sync: loudness (0..1, already smoothed) → viseme weights.
   * The open amount follows loudness; the vowel changes about every 120 ms
   * in a fixed pseudo-random order so speech looks natural, not robotic.
   */
  function lipSync(loudness, t, gain = 1.4) {
    const open = Math.min(1, Math.max(0, (Number(loudness) || 0) * gain));
    const out = { aa: 0, ih: 0, ou: 0, ee: 0, oh: 0 };
    if (open < 0.04) return out;
    const slot = Math.floor((Number(t) || 0) / 0.12);
    const order = [0, 3, 1, 4, 0, 2, 3, 0, 4, 1];
    const v = VISEMES[order[((slot % order.length) + order.length) % order.length]];
    out[v] = open;
    if (v !== 'aa') out.aa = open * 0.3;   // keep the jaw moving
    return out;
  }

  const api = { EMOTIONS, VISEMES, ALIASES, resolveName, parseEmotionTag, blendEmotions, lipSync };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.KiraExpressions = api;
})(typeof window !== 'undefined' ? window : globalThis);
