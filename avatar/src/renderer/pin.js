/**
 * avatar/src/renderer/pin.js
 * Free-mode PIN dialog. The PIN is read from a masked field, sent once to
 * the main process, and the field is cleared right away. It is never logged.
 */

(async function init() {
  const form = document.getElementById('pin-form');
  const input = document.getElementById('pin');
  const message = document.getElementById('message');
  const enableBtn = document.getElementById('enable');

  try {
    const info = await window.kiraPin.info();
    const minutes = Math.round((info.freeTimeout || 600) / 60);
    document.getElementById('timeout-note').textContent =
      `Free mode switches back to Restricted after ${minutes} minute${minutes === 1 ? '' : 's'}, on restart, or when you say "restricted mode".`;
  } catch { /* keep default text */ }

  const MESSAGES = {
    ok: 'Free mode enabled.',
    wrong_pin: 'Wrong PIN.',
    locked: 'Too many wrong PINs. Try again later.',
    disabled: 'Free mode is disabled: set a PIN in Settings first.',
    offline: 'Kira backend is not connected.',
    timeout: 'No answer from Kira backend.',
    invalid: 'Enter 4 to 12 digits.'
  };

  document.getElementById('cancel').addEventListener('click', () => window.kiraPin.cancel());
  window.addEventListener('keydown', (e) => { if (e.key === 'Escape') window.kiraPin.cancel(); });

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const pin = input.value;
    input.value = '';                       // clear immediately
    if (!/^\d{4,12}$/.test(pin)) {
      message.textContent = MESSAGES.invalid;
      return;
    }
    enableBtn.disabled = true;
    const result = await window.kiraPin.submit(pin);
    let text = MESSAGES[result.status] || 'Could not enable Free mode.';
    if (result.status === 'locked' && result.lockoutSeconds) {
      text = `Too many wrong PINs. Try again in ${result.lockoutSeconds} s.`;
    }
    message.textContent = text;
    message.className = result.status === 'ok' ? 'ok' : 'error';
    enableBtn.disabled = result.status === 'ok';
    input.focus();
  });
})();
