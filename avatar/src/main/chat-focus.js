/**
 * chat-focus.js
 * Keyboard focus for the avatar chat box.
 *
 * The avatar window is created with focusable:false and shown inactive so it
 * never steals focus from VS Code. To type into the chat box it must become
 * focusable for a short time. On Windows, setFocusable(true) + focus() on a
 * window that was shown inactive does not always activate it, and the blur
 * that follows a closing context menu could close the chat at once. This
 * controller:
 *   - refuses to open while the assistant is working (typing into VS Code),
 *   - makes the window focusable and mouse-interactive, shows and focuses it,
 *     focuses the web contents, and retries once if focus did not land,
 *   - ignores blur events during a short grace period after opening,
 *   - closes the chat (focusable:false, click-through) on send, Escape, real
 *     blur, or as soon as a task starts.
 */

'use strict';

const BLUR_GRACE_MS = 400;
const RETRY_MS = 60;

class ChatFocus {
  /**
   * @param {() => any} getWindow   returns the BrowserWindow (or null)
   * @param {{now?: () => number, setTimer?: Function}} [opts]
   */
  constructor(getWindow, opts = {}) {
    this.getWindow = getWindow;
    this.now = opts.now || Date.now;
    this.setTimer = opts.setTimer || setTimeout;
    this.isOpen = false;
    this.openedAt = 0;
  }

  _win() {
    const w = this.getWindow();
    return w && !(w.isDestroyed && w.isDestroyed()) ? w : null;
  }

  /** Open the chat box. Returns false if refused (working or no window). */
  open(state) {
    const win = this._win();
    if (!win || state === 'working') return false;
    this.isOpen = true;
    this.openedAt = this.now();
    win.setFocusable(true);
    win.setIgnoreMouseEvents(false);
    win.webContents.send('avatar:open-chat');
    this._focus(win);
    this.setTimer(() => {
      const w = this._win();
      if (this.isOpen && w && !w.isFocused()) this._focus(w);
    }, RETRY_MS);
    return true;
  }

  _focus(win) {
    if (win.isVisible && !win.isVisible()) win.show();
    win.focus();
    win.webContents.focus();
  }

  /** Close the chat box and make the window non-focusable again. */
  close() {
    const win = this._win();
    const wasOpen = this.isOpen;
    this.isOpen = false;
    if (!win) return;
    win.setFocusable(false);
    win.setIgnoreMouseEvents(true, { forward: true });
    if (wasOpen) win.webContents.send('avatar:blur-chat');
  }

  /** Window blur: close unless it is the transient blur right after opening. */
  onBlur() {
    if (!this.isOpen) {
      const win = this._win();
      if (win) win.setFocusable(false);
      return 'ignored';
    }
    if (this.now() - this.openedAt < BLUR_GRACE_MS) {
      const win = this._win();
      if (win) this._focus(win);
      return 'refocused';
    }
    this.close();
    return 'closed';
  }

  /** Assistant state changed: a task starting closes the chat at once. */
  onState(state) {
    if (state === 'working' && this.isOpen) {
      this.close();
      return true;
    }
    return false;
  }
}

module.exports = { ChatFocus, BLUR_GRACE_MS, RETRY_MS };
