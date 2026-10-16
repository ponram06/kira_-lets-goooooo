/**
 * avatar/src/renderer/app.js
 * Application entry point for the Electron renderer process.
 */

(async function init() {
  if (!window.kiraAvatar) {
    console.error('KiraAvatar IPC bridge not found in window');
    return;
  }

  // 1. Fetch initial configuration from main process
  let configData = {};
  try {
    configData = await window.kiraAvatar.getConfig();
  } catch (err) {
    console.warn('Failed to get config from main process:', err);
  }

  const config = configData.config || {};
  const greeting = config.greeting || 'Hey... Need anything?';
  const fadeDelayMs = config.bubble?.fade_delay_ms || 3000;

  // 2. Setup canvas & renderer
  const canvas = document.getElementById('avatar-canvas');
  if (config.window) {
    canvas.width = config.window.width || 480;
    canvas.height = config.window.height || 420;
  }
  const renderer = new window.SpriteRenderer(canvas, config, configData.characterMeta);
  renderer.start();

  // 3. Setup speech bubble
  const bubbleElem = document.getElementById('speech-bubble');
  const textElem = document.getElementById('bubble-text');
  const bubble = new window.SpeechBubble(bubbleElem, textElem, fadeDelayMs);

  // Show greeting initially
  bubble.say(greeting);

  // 4. Setup Dragging
  const dragHandle = document.getElementById('drag-handle');
  let isDragging = false;
  let lastScreenX = 0;
  let lastScreenY = 0;
  let isLocked = false;

  dragHandle.addEventListener('pointerdown', (e) => {
    if (isLocked || e.button !== 0) return;
    isDragging = true;
    lastScreenX = e.screenX;
    lastScreenY = e.screenY;
    dragHandle.setPointerCapture(e.pointerId);
    window.kiraAvatar.startDrag();
  });

  dragHandle.addEventListener('pointermove', (e) => {
    if (!isDragging) return;
    const dx = e.screenX - lastScreenX;
    const dy = e.screenY - lastScreenY;
    lastScreenX = e.screenX;
    lastScreenY = e.screenY;
    window.kiraAvatar.moveDrag({ dx, dy });
  });

  const stopDrag = (e) => {
    if (!isDragging) return;
    isDragging = false;
    try {
      dragHandle.releasePointerCapture(e.pointerId);
    } catch {
      // Ignored
    }
    window.kiraAvatar.endDrag();
  };

  dragHandle.addEventListener('pointerup', stopDrag);
  dragHandle.addEventListener('pointercancel', stopDrag);

  // 5. Setup Click-Through (setIgnoreMouseEvents)
  // When mouse enters interactive elements, disable ignoreMouseEvents
  const interactiveElements = [
    dragHandle,
    bubbleElem,
    document.getElementById('hover-menu-container')
  ];

  for (const elem of interactiveElements) {
    if (!elem) continue;
    elem.addEventListener('pointerenter', () => {
      window.kiraAvatar.setIgnoreMouseEvents(false);
    });
    elem.addEventListener('pointerleave', (e) => {
      if (!isDragging && !interactiveElements.some(el => el.contains(e.relatedTarget))) {
        window.kiraAvatar.setIgnoreMouseEvents(true, true);
      }
    });
  }

  // 6. Hover Menu Controls
  const btnStop = document.getElementById('btn-stop');
  if (btnStop) {
    btnStop.addEventListener('click', () => {
      window.kiraAvatar.sendStop();
    });
  }

  const btnLock = document.getElementById('btn-lock');
  if (btnLock) {
    btnLock.addEventListener('click', () => {
      isLocked = !isLocked;
      btnLock.textContent = isLocked ? 'Unlock Position' : 'Lock Position';
      dragHandle.style.cursor = isLocked ? 'default' : 'grab';
    });
  }

  const btnSleep = document.getElementById('btn-sleep');
  if (btnSleep) {
    btnSleep.addEventListener('click', () => {
      bubble.hide();
      renderer.setState('offline');
    });
  }

  // 7. Subscribe to WebSocket events forwarded from main process
  window.kiraAvatar.onEvent((eventData) => {
    if (!eventData || typeof eventData !== 'object') return;
    const { type, payload } = eventData;

    switch (type) {
      case 'state':
        if (payload?.state) {
          renderer.setState(payload.state);
        }
        break;

      case 'speech':
        if (payload?.text) {
          bubble.say(payload.text);
        }
        break;

      case 'loudness':
        if (typeof payload?.value === 'number') {
          renderer.setLoudness(payload.value);
        }
        break;

      case 'mode':
        if (payload?.mode) {
          renderer.setMode(payload.mode);
        }
        break;

      case 'error':
        renderer.setState('error');
        if (payload?.message) {
          bubble.say(`Error: ${payload.message}`);
        }
        break;
    }
  });
})();

