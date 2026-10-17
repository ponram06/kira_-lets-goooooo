/**
 * avatar/src/shared/feed.js
 * Action feed: turns backend events into short lines (tool start, done, ask,
 * block, countdown, error, kill switch) and keeps only the latest few.
 * Loaded by the renderer with a <script> tag (window.KiraFeed) and by
 * node:test with require(). Does NOT import electron.
 */

(function (root) {
  const MAX_ENTRIES = 4;
  const MAX_TEXT = 60;

  function clip(text) {
    const s = String(text ?? '').replace(/[\x00-\x1F\x7F]/g, ' ').trim();
    return s.length > MAX_TEXT ? s.slice(0, MAX_TEXT - 1) + '…' : s;
  }

  /**
   * @param {string} type - event type from the backend
   * @param {object} p    - payload
   * @returns {{ kind: string, text: string } | null}
   */
  function formatFeedEntry(type, p) {
    p = p || {};
    switch (type) {
      case 'tool_start': return { kind: 'start', text: clip(`▶ ${p.tool || 'tool'}`) };
      case 'tool_done': return { kind: 'done', text: clip(`✓ ${p.tool || 'tool'}`) };
      case 'tool_error': return { kind: 'error', text: clip(`✗ ${p.summary || 'error'}`) };
      case 'policy_ask': return { kind: 'ask', text: clip(`? ${p.prompt || 'needs confirmation'}`) };
      case 'policy_decision':
        if (p.decision === 'block') return { kind: 'block', text: clip(`⛔ blocked: ${p.tool || ''}`) };
        if (p.decision === 'countdown') return { kind: 'countdown', text: clip(`⏱ 3 s countdown: ${p.tool || ''}`) };
        return null;
      case 'kill_switch': return { kind: 'block', text: 'Stopped (kill switch)' };
      case 'mode_change': return { kind: 'mode', text: p.mode === 'free' ? 'Mode: FREE' : 'Mode: Restricted' };
      case 'free_mode_denied': return { kind: 'block', text: clip(`Free mode refused (${String(p.reason || '').replace('_', ' ')})`) };
      case 'error': return { kind: 'error', text: clip(`✗ ${p.message || 'error'}`) };
      default: return null;
    }
  }

  /** Returns a new list with *entry* appended, keeping the last MAX_ENTRIES. */
  function pushFeed(list, entry, max = MAX_ENTRIES) {
    if (!entry) return list;
    return [...list, entry].slice(-max);
  }

  const api = { MAX_ENTRIES, formatFeedEntry, pushFeed };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.KiraFeed = api;
})(typeof window !== 'undefined' ? window : globalThis);
