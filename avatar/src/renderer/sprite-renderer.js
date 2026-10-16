/**
 * avatar/src/renderer/sprite-renderer.js
 * 2D Canvas sprite renderer with breathing, glow overlays, and 30 FPS cap.
 */

// Import pure calculations (bundled or accessible in renderer script context)
// In browser renderer context without bundler, we can define the pure functions or import them.

class SpriteRenderer {
  /**
   * @param {HTMLCanvasElement} canvas
   * @param {object} config
   */
  constructor(canvas, config) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.config = config || {};

    this.charConfig = this.config.character || { x: 160, y: 80, width: 300, height: 320 };
    this.glowConfig = this.config.glow || {
      eye: { x: 228, y: 140, radius: 14, color: 'rgba(255, 60, 60, 0.6)' },
      apple: { x: 240, y: 270, radius: 28, color: 'rgba(255, 20, 20, 0.85)' }
    };

    this.state = 'idle';
    this.mode = 'restricted'; // 'restricted' (blue) or 'free' (red)
    this.rawLoudness = 0.0;
    this.smoothedLoudness = 0.0;
    this.lastFrameTime = 0;
    this.fpsInterval = 1000 / 30; // 30 FPS cap
    this.startTime = performance.now();

    this.baseImage = new Image();
    this.mouthOpenImage = new Image();
    this.hasMouthOpenImage = false;
    this.imagesLoaded = false;
    this.errorFlashUntil = 0;

    this._initImages();
  }

  _initImages() {
    let loadedCount = 0;
    const checkReady = () => {
      loadedCount++;
      if (loadedCount >= 1) {
        this.imagesLoaded = true;
      }
    };

    // Load base character image
    this.baseImage.onload = checkReady;
    this.baseImage.onerror = () => {
      // Fallback to placeholder
      this.baseImage.src = '../assets/placeholder.png';
    };

    // Try ryuk.png first, fallback handled in onerror
    this.baseImage.src = '../assets/ryuk.png';

    // Optional mouth open image
    this.mouthOpenImage.onload = () => {
      this.hasMouthOpenImage = true;
    };
    this.mouthOpenImage.onerror = () => {
      this.hasMouthOpenImage = false;
    };
    this.mouthOpenImage.src = '../assets/ryuk_mouth_open.png';
  }

  setState(newState) {
    const s = String(newState || 'idle').toLowerCase();
    this.state = s;
    if (s === 'error') {
      this.errorFlashUntil = performance.now() + 1500;
    }
  }

  setMode(newMode) {
    const m = String(newMode || 'restricted').toLowerCase();
    this.mode = m === 'free' ? 'free' : 'restricted';
  }

  setLoudness(val) {
    this.rawLoudness = Math.min(1.0, Math.max(0.0, Number(val) || 0.0));
  }

  start() {
    const loop = (timestamp) => {
      requestAnimationFrame(loop);

      const elapsed = timestamp - this.lastFrameTime;
      if (elapsed < this.fpsInterval) return;

      this.lastFrameTime = timestamp - (elapsed % this.fpsInterval);
      this.render(timestamp);
    };

    requestAnimationFrame(loop);
  }

  render(timestamp) {
    const ctx = this.ctx;
    const w = this.canvas.width;
    const h = this.canvas.height;

    ctx.clearRect(0, 0, w, h);

    if (!this.imagesLoaded) return;

    // Smooth loudness using exponential moving average
    this.smoothedLoudness += 0.35 * (this.rawLoudness - this.smoothedLoudness);

    const timeSec = (timestamp - this.startTime) / 1000.0;
    const isErrorFlash = timestamp < this.errorFlashUntil || this.state === 'error';
    const isOffline = this.state === 'offline';

    // 1. Draw mode indicator ring at bottom right of the canvas
    this._drawModeIndicator(ctx, w - 24, h - 24, 8);

    // 2. Compute animation transforms
    let scaleX = 1.0;
    let scaleY = 1.0;
    let offsetX = 0.0;
    let offsetY = 0.0;
    let tiltDeg = 0.0;
    let eyeGlow = 1.0;
    let appleGlow = 1.0;

    switch (this.state) {
      case 'idle': {
        const breath = Math.sin(timeSec * Math.PI * 0.5);
        scaleY = 1.0 + breath * 0.015;
        offsetY = breath * 2.0;
        eyeGlow = 0.8 + 0.2 * breath;
        break;
      }
      case 'listening': {
        tiltDeg = 2.5;
        offsetY = 3.0;
        eyeGlow = 1.6;
        break;
      }
      case 'thinking': {
        const pulse = 0.5 + 0.5 * Math.sin(timeSec * Math.PI);
        appleGlow = 0.7 + pulse * 0.8;
        offsetY = Math.sin(timeSec * Math.PI * 0.5) * 1.5;
        break;
      }
      case 'working': {
        const fastPulse = 0.5 + 0.5 * Math.sin(timeSec * Math.PI * 2.5);
        appleGlow = 0.8 + fastPulse * 1.0;
        offsetX = Math.sin(timeSec * Math.PI * 2.0) * 3.0;
        offsetY = Math.cos(timeSec * Math.PI * 2.0) * 1.5;
        eyeGlow = 1.2;
        break;
      }
      case 'speaking': {
        const bob = this.smoothedLoudness;
        offsetY = -bob * 6.0;
        scaleY = 1.0 + bob * 0.04;
        eyeGlow = 1.0 + bob * 0.5;
        appleGlow = 1.0 + bob * 0.6;
        break;
      }
      case 'error': {
        eyeGlow = 1.8;
        appleGlow = 1.8;
        offsetX = (Math.random() - 0.5) * 3.0;
        break;
      }
      case 'offline': {
        eyeGlow = 0.15;
        appleGlow = 0.15;
        break;
      }
    }

    // 3. Draw Character Sprite
    ctx.save();

    if (isOffline) {
      ctx.filter = 'grayscale(80%) opacity(60%)';
    } else if (isErrorFlash) {
      ctx.filter = 'drop-shadow(0 0 10px rgba(239, 68, 68, 0.9))';
    }

    const cx = this.charConfig.x + this.charConfig.width / 2 + offsetX;
    const cy = this.charConfig.y + this.charConfig.height + offsetY;

    ctx.translate(cx, cy);
    if (tiltDeg !== 0) {
      ctx.rotate((tiltDeg * Math.PI) / 180);
    }
    ctx.scale(scaleX, scaleY);

    const drawX = -this.charConfig.width / 2;
    const drawY = -this.charConfig.height;

    // Sprite selection: check if speaking with mouth open
    const useMouthOpen = this.hasMouthOpenImage &&
      this.state === 'speaking' &&
      this.smoothedLoudness > 0.25;

    const imgToDraw = useMouthOpen ? this.mouthOpenImage : this.baseImage;
    ctx.drawImage(imgToDraw, drawX, drawY, this.charConfig.width, this.charConfig.height);

    ctx.restore();

    // 4. Draw Glow Overlays (Eye and Apple radial gradients)
    if (!isOffline) {
      this._drawRadialGlow(
        ctx,
        this.glowConfig.eye.x + offsetX,
        this.glowConfig.eye.y + offsetY,
        this.glowConfig.eye.radius * eyeGlow,
        this.glowConfig.eye.color
      );

      this._drawRadialGlow(
        ctx,
        this.glowConfig.apple.x + offsetX,
        this.glowConfig.apple.y + offsetY,
        this.glowConfig.apple.radius * appleGlow,
        this.glowConfig.apple.color
      );
    }
  }

  _drawModeIndicator(ctx, x, y, radius) {
    ctx.save();
    // Blue for Restricted mode, Red for Free mode
    const color = this.mode === 'free' ? '#ef4444' : '#3b82f6';
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.shadowColor = color;
    ctx.shadowBlur = 6;
    ctx.fill();

    // Subtle dark border
    ctx.lineWidth = 1.5;
    ctx.strokeStyle = '#1e293b';
    ctx.stroke();
    ctx.restore();
  }

  _drawRadialGlow(ctx, x, y, radius, colorStr) {
    if (radius <= 0) return;
    ctx.save();
    const grad = ctx.createRadialGradient(x, y, 0, x, y, radius);
    grad.addColorStop(0, colorStr);
    grad.addColorStop(1, 'rgba(0, 0, 0, 0)');
    ctx.fillStyle = grad;
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
  }
}

window.SpriteRenderer = SpriteRenderer;
