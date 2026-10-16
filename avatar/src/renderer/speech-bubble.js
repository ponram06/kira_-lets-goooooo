/**
 * avatar/src/renderer/speech-bubble.js
 * Speech bubble controller managing auto-fadeout, sanitization, and click dismissal.
 */

class SpeechBubble {
  /**
   * @param {HTMLElement} bubbleElement
   * @param {HTMLElement} textElement
   * @param {number} fadeDelayMs
   */
  constructor(bubbleElement, textElement, fadeDelayMs = 3000) {
    this.bubbleElement = bubbleElement;
    this.textElement = textElement;
    this.fadeDelayMs = fadeDelayMs;
    this.fadeTimeout = null;

    // Click to dismiss immediately
    this.bubbleElement.addEventListener('click', () => {
      this.hide();
    });
  }

  /**
   * Displays speech text in the bubble.
   * @param {string} text
   */
  say(text) {
    if (!text || typeof text !== 'string') return;
    const clean = this._sanitize(text);
    if (!clean) return;

    // Plain text only - NEVER innerHTML
    this.textElement.textContent = clean;

    this.bubbleElement.classList.remove('hidden');
    this.bubbleElement.classList.add('visible');

    this._resetTimer();
  }

  hide() {
    this.bubbleElement.classList.remove('visible');
    this.bubbleElement.classList.add('hidden');
    if (this.fadeTimeout) {
      clearTimeout(this.fadeTimeout);
      this.fadeTimeout = null;
    }
  }

  _resetTimer() {
    if (this.fadeTimeout) {
      clearTimeout(this.fadeTimeout);
    }
    this.fadeTimeout = setTimeout(() => {
      this.hide();
    }, this.fadeDelayMs);
  }

  _sanitize(text) {
    let str = String(text);
    str = str.replace(/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/g, '');
    str = str.replace(/\r\n/g, '\n').replace(/\r/g, '\n').trim();
    if (str.length > 140) {
      return str.slice(0, 139) + '…';
    }
    return str;
  }
}

window.SpeechBubble = SpeechBubble;
