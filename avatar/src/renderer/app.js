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
  let isLocked = Boolean(configData.ui?.locked);

  // Prevent all browser zooming (Ctrl+wheel, touch pinch, dblclick, Ctrl+/- shortcuts)
  window.addEventListener('wheel', (e) => {
    if (e.ctrlKey) e.preventDefault();
  }, { passive: false });

  window.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && (e.key === '+' || e.key === '-' || e.key === '=' || e.key === '0')) {
      e.preventDefault();
    }
  });

  window.addEventListener('dblclick', (e) => {
    e.preventDefault();
  });

  dragHandle.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    if (isLocked || e.button !== 0) return;
    isDragging = true;
    lastScreenX = e.screenX;
    lastScreenY = e.screenY;
    dragHandle.setPointerCapture(e.pointerId);
    window.kiraAvatar.startDrag();
  });

  dragHandle.addEventListener('pointermove', (e) => {
    e.preventDefault();
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

  // 5. Control button + click-through
  // Only the character (drag handle), the control button and the bubble take
  // the mouse; everything else in the window passes clicks through.
  const container = document.getElementById('container');
  const controlBtn = document.getElementById('control-btn');
  const feedElem = document.getElementById('action-feed');
  const badgesElem = document.getElementById('status-badges');
  const interactiveElements = [dragHandle, bubbleElem, controlBtn];
  const HIDE_BUTTON_MS = 2000;
  let hideBtnTimer = null;

  const showControlButton = () => {
    clearTimeout(hideBtnTimer);
    controlBtn.classList.add('visible');
  };
  const scheduleHideButton = () => {
    clearTimeout(hideBtnTimer);
    hideBtnTimer = setTimeout(() => controlBtn.classList.remove('visible'), HIDE_BUTTON_MS);
  };

  for (const elem of interactiveElements) {
    if (!elem) continue;
    elem.addEventListener('pointerenter', () => {
      window.kiraAvatar.setIgnoreMouseEvents(false);
      if (elem !== bubbleElem) showControlButton();
    });
    elem.addEventListener('pointerleave', (e) => {
      if (!isDragging && !interactiveElements.some(el => el.contains(e.relatedTarget))) {
        window.kiraAvatar.setIgnoreMouseEvents(true, true);
        scheduleHideButton();
      }
    });
  }

  // 6. Menus: control button click or right-click on the character
  controlBtn.addEventListener('click', (e) => {
    e.preventDefault();
    window.kiraAvatar.openMenu();
  });
  dragHandle.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    window.kiraAvatar.openMenu();
  });

  // UI state from main: size, feed, lock, sleep, mic, connection
  let feed = [];
  const renderFeed = () => {
    feedElem.replaceChildren(...feed.map((entry) => {
      const li = document.createElement('li');
      li.className = entry.kind;
      li.textContent = entry.text;      // textContent only: never HTML
      return li;
    }));
  };

  const renderBadges = (ui) => {
    const badges = [];
    if (ui.connected === false) badges.push(['offline', 'warn']);
    if (ui.micMuted) badges.push(['mic off', 'warn']);
    if (ui.sleeping) badges.push(['zZ', '']);
    badgesElem.replaceChildren(...badges.map(([text, cls]) => {
      const span = document.createElement('span');
      span.className = `badge ${cls}`.trim();
      span.textContent = text;
      return span;
    }));
  };

  const applyUi = (ui) => {
    if (!ui || typeof ui !== 'object') return;
    if (typeof ui.scale === 'number' && ui.scale > 0) {
      container.style.transform = `scale(${ui.scale})`;
    }
    if (typeof ui.feedVisible === 'boolean') feedElem.classList.toggle('hidden', !ui.feedVisible);
    if (typeof ui.locked === 'boolean') {
      isLocked = ui.locked;
      dragHandle.classList.toggle('locked', isLocked);
    }
    if (typeof ui.sleeping === 'boolean') {
      container.classList.toggle('sleeping', ui.sleeping);
      renderer.setSleeping(ui.sleeping);
      if (ui.sleeping) bubble.hide();
    }
    renderBadges(ui);
  };
  applyUi(configData.ui);
  window.kiraAvatar.onUi(applyUi);

  // 7. Subscribe to WebSocket events forwarded from main process
  window.kiraAvatar.onEvent((eventData) => {
    if (!eventData || typeof eventData !== 'object') return;
    const { type, payload } = eventData;

    const entry = window.KiraFeed.formatFeedEntry(type, payload);
    if (entry) {
      feed = window.KiraFeed.pushFeed(feed, entry);
      renderFeed();
    }

    switch (type) {
      case 'snapshot':
        if (payload?.state) {
          renderer.setState(payload.state);
        }
        if (payload?.mode) {
          renderer.setMode(payload.mode);
        }
        break;

      case 'state':
        if (payload?.state) {
          renderer.setState(payload.state);
        }
        break;

      case 'speech':
        renderer.setState('speaking');
        if (payload?.text) {
          bubble.say(payload.text);
        }
        break;

      case 'loudness': {
        const val = typeof payload?.value === 'number'
          ? payload.value
          : (typeof payload?.level === 'number' ? payload.level : null);
        if (val !== null) {
          renderer.setLoudness(val);
        }
        break;
      }

      case 'mode':
      case 'mode_change':
        if (payload?.mode) {
          renderer.setMode(payload.mode);
        }
        break;

      case 'step_start':
      case 'tool_start':
        renderer.setState('working');
        break;

      case 'step_done':
      case 'tool_done':
        renderer.setState('idle');
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

