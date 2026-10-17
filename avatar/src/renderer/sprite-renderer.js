/**
 * avatar/src/renderer/sprite-renderer.js
 * 2D Canvas sprite renderer with breathing, glow overlays, and 30 FPS cap.
 */

class SpriteRenderer {
  /**
   * @param {HTMLCanvasElement} canvas
   * @param {object} config
   * @param {object} characterMeta
   */
  constructor(canvas, config, characterMeta) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.config = config || {};
    this.characterMeta = characterMeta || {};

    this.charConfig = this.config.character || { x: 160, y: 80, width: 300, height: 320 };
    this.glowConfig = this.config.glow || {
      eye: { x: 310, y: 180, radius: 14, color: 'rgba(255, 60, 60, 0.65)' },
      apple: { x: 310, y: 320, radius: 28, color: 'rgba(255, 20, 20, 0.85)' }
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
    this.baseImage.onload = () => {
      this.imagesLoaded = true;
    };
    this.baseImage.onerror = () => {
      // If dataUrl failed or path failed, try relative path to placeholder
      if (!this.baseImage.src.includes('placeholder.png')) {
        this.baseImage.src = '../../assets/placeholder.png';
      }
    };

    // Load from data URL passed by main process (zero network/path failure)
    if (this.characterMeta?.dataUrl) {
      this.baseImage.src = this.characterMeta.dataUrl;
    } else {
      this.baseImage.src = '../../assets/placeholder.png';
    }

    if (this.characterMeta?.mouthOpenDataUrl) {
      this.mouthOpenImage.onload = () => {
        this.hasMouthOpenImage = true;
      };
      this.mouthOpenImage.src = this.characterMeta.mouthOpenDataUrl;
    } else {
      this.mouthOpenImage.onload = () => {
        this.hasMouthOpenImage = true;
      };
      this.mouthOpenImage.onerror = () => {
        this.hasMouthOpenImage = false;
      };
      this.mouthOpenImage.src = '../../assets/ryuk_mouth_open.png';
    }
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

  /** Sleep: render at 5 FPS instead of 30 to save CPU (the CSS dims the avatar). */
  setSleeping(sleeping) {
    this.fpsInterval = 1000 / (sleeping ? 5 : 30);
  }

  setLoudness(val) {
    this.rawLoudness = Math.min(1.0, Math.max(0.0, Number(val) || 0.0));
  }

  start() {
    const loop = (timestamp) => {
      requestAnimationFrame(loop);

      if (document.hidden) return;   // window hidden: skip drawing
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

    // Smooth loudness using exponential moving average
    this.smoothedLoudness += 0.35 * (this.rawLoudness - this.smoothedLoudness);

    const timeSec = (timestamp - this.startTime) / 1000.0;
    const isErrorFlash = timestamp < this.errorFlashUntil || this.state === 'error';
    const isOffline = this.state === 'offline';

    // 1. Compute animation transforms
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

    if (this.imagesLoaded) {
      ctx.drawImage(imgToDraw, drawX, drawY, this.charConfig.width, this.charConfig.height);
    } else {
      // Instant vector fallback while image loads or if file missing
      this._drawVectorFallback(ctx, drawX, drawY, this.charConfig.width, this.charConfig.height);
    }

    ctx.restore();

    // 3. Dynamic Apple Tint: Green in Restricted mode, natural Red in Free mode
    if (this.mode === 'restricted' && !isOffline) {
      const ax = this.glowConfig.apple.x + offsetX;
      const ay = this.glowConfig.apple.y + offsetY;
      const ar = (this.glowConfig.apple.radius || 18) * 1.15;

      ctx.save();
      ctx.beginPath();
      ctx.arc(ax, ay, ar, 0, Math.PI * 2);
      ctx.clip();

      // Transform red pixels to emerald green while preserving shading, specular highlights, and hand contours
      ctx.globalCompositeOperation = 'hue';
      ctx.fillStyle = '#22c55e';
      ctx.fillRect(ax - ar, ay - ar, ar * 2, ar * 2);

      ctx.globalCompositeOperation = 'color';
      ctx.fillStyle = 'rgba(34, 197, 94, 0.7)';
      ctx.fillRect(ax - ar, ay - ar, ar * 2, ar * 2);

      ctx.restore();
    }

    // 4. Draw Glow Overlays (Eye and Apple radial gradients)
    if (!isOffline) {
      this._drawRadialGlow(
        ctx,
        this.glowConfig.eye.x + offsetX,
        this.glowConfig.eye.y + offsetY,
        this.glowConfig.eye.radius * eyeGlow,
        this.glowConfig.eye.color
      );

      const appleGlowColor = this.mode === 'free'
        ? (this.glowConfig.apple.color || 'rgba(255, 30, 30, 0.85)')
        : (this.glowConfig.apple.restricted_color || 'rgba(34, 197, 94, 0.85)');

      this._drawRadialGlow(
        ctx,
        this.glowConfig.apple.x + offsetX,
        this.glowConfig.apple.y + offsetY,
        this.glowConfig.apple.radius * appleGlow,
        appleGlowColor
      );
    }
  }

  _drawVectorFallback(ctx, x, y, width, height) {
    ctx.save();
    const cx = x + width / 2;
    const cy = y + height / 2;

    // Dark winged character silhouette
    ctx.fillStyle = '#262933';
    ctx.strokeStyle = '#64748b';
    ctx.lineWidth = 2;

    // Wings
    ctx.beginPath();
    ctx.moveTo(cx - 20, cy + 10);
    ctx.lineTo(cx - 85, cy - 60);
    ctx.lineTo(cx - 40, cy + 50);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();

    ctx.beginPath();
    ctx.moveTo(cx + 20, cy + 10);
    ctx.lineTo(cx + 85, cy - 60);
    ctx.lineTo(cx + 40, cy + 50);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();

    // Body
    ctx.beginPath();
    ctx.ellipse(cx, cy + 30, 48, 65, 0, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();

    // Head
    ctx.beginPath();
    ctx.arc(cx, cy - 45, 30, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();

    // Red eyes
    ctx.fillStyle = '#ef4444';
    ctx.beginPath();
    ctx.arc(cx - 9, cy - 47, 4, 0, Math.PI * 2);
    ctx.arc(cx + 9, cy - 47, 4, 0, Math.PI * 2);
    ctx.fill();

    // Apple in hands: Green in Restricted mode, Red in Free mode
    ctx.fillStyle = this.mode === 'free' ? '#dc2626' : '#16a34a';
    ctx.beginPath();
    ctx.arc(cx, cy + 70, 18, 0, Math.PI * 2);
    ctx.fill();

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
