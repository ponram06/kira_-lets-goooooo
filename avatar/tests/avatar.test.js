/**
 * avatar/tests/avatar.test.js
 * Comprehensive unit test suite using Node.js built-in node:test and node:assert.
 * Verifies all pure shared logic without requiring Electron.
 */

const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const zlib = require('zlib');

const {
  VALID_STATES,
  normalizeState,
  computeAnimation
} = require('../src/shared/state-machine');

const {
  smoothLoudness,
  isMouthOpen
} = require('../src/shared/loudness');

const {
  ALLOWED_COMMANDS,
  createStopMessage,
  isValidCommand,
  isValidTokenString
} = require('../src/shared/schema');

const {
  clampToBounds,
  calculateDefaultPosition,
  findDisplayForWindow
} = require('../src/shared/bounds');

const {
  MAX_BUBBLE_LENGTH,
  sanitizeBubbleText
} = require('../src/shared/bubble');

const {
  buildHitMask,
  isHit,
  isMaskNonEmpty
} = require('../src/shared/hitmask');

describe('State Machine & Animation', () => {
  it('normalizes valid and invalid states', () => {
    assert.equal(normalizeState('IDLE'), 'idle');
    assert.equal(normalizeState('Listening'), 'listening');
    assert.equal(normalizeState('thinking'), 'thinking');
    assert.equal(normalizeState('WORKING'), 'working');
    assert.equal(normalizeState('Speaking'), 'speaking');
    assert.equal(normalizeState('error'), 'error');
    assert.equal(normalizeState('offline'), 'offline');
    assert.equal(normalizeState('unknown_foo'), 'idle');
    assert.equal(normalizeState(null), 'idle');
    assert.equal(normalizeState(undefined), 'idle');
  });

  it('computes idle breathing animation values', () => {
    const anim0 = computeAnimation('idle', 0.0);
    assert.equal(anim0.isDimmed, false);
    assert.equal(anim0.isRedFlash, false);
    assert.ok(anim0.scaleY >= 0.98 && anim0.scaleY <= 1.02);

    const anim1 = computeAnimation('idle', 1.0);
    assert.ok(anim1.scaleY >= 0.98 && anim1.scaleY <= 1.02);
  });

  it('computes listening forward tilt and brighter eye glow', () => {
    const anim = computeAnimation('listening', 0.0);
    assert.equal(anim.tiltDeg, 2.5);
    assert.equal(anim.eyeGlow, 1.6);
  });

  it('computes thinking pulsing apple glow', () => {
    const anim = computeAnimation('thinking', 0.5);
    assert.ok(anim.appleGlow >= 0.6 && anim.appleGlow <= 1.6);
  });

  it('computes working rapid apple pulse and sway', () => {
    const anim = computeAnimation('working', 0.25);
    assert.notEqual(anim.offsetX, 0.0);
    assert.ok(anim.appleGlow >= 0.7);
  });

  it('computes speaking bob based on smoothed loudness', () => {
    const quiet = computeAnimation('speaking', 0.0, 0.0);
    const loud = computeAnimation('speaking', 0.0, 0.8);
    assert.equal(quiet.offsetY, 0.0);
    assert.ok(loud.offsetY < 0.0, 'Mouth bob should move upward on loud speech');
    assert.ok(loud.scaleY > quiet.scaleY);
  });

  it('computes error red flash and offline dimming', () => {
    const err = computeAnimation('error', 0.0);
    assert.equal(err.isRedFlash, true);

    const off = computeAnimation('offline', 0.0);
    assert.equal(off.isDimmed, true);
    assert.ok(off.eyeGlow < 0.5);
  });
});

describe('Loudness Smoothing & Mouth Threshold', () => {
  it('smooths loudness towards target using EMA', () => {
    let current = 0.0;
    current = smoothLoudness(current, 1.0, 0.5);
    assert.equal(current, 0.5);
    current = smoothLoudness(current, 1.0, 0.5);
    assert.equal(current, 0.75);
  });

  it('clamps loudness between 0.0 and 1.0', () => {
    assert.equal(smoothLoudness(0.0, -0.5, 1.0), 0.0);
    assert.equal(smoothLoudness(0.0, 2.5, 1.0), 1.0);
  });

  it('evaluates mouth open threshold correctly', () => {
    assert.equal(isMouthOpen(0.0, 0.25), false);
    assert.equal(isMouthOpen(0.24, 0.25), false);
    assert.equal(isMouthOpen(0.25, 0.25), true);
    assert.equal(isMouthOpen(0.8, 0.25), true);
  });
});

describe('Schema & Backend Protocol Compliance', () => {
  it('creates exact stop message accepted by backend/server.py', () => {
    const stopMsg = createStopMessage();
    const parsed = JSON.parse(stopMsg);
    assert.deepEqual(parsed, { command: 'stop' });
  });

  it('validates allowed commands matching backend server schema', () => {
    assert.equal(isValidCommand({ command: 'stop' }), true);
    assert.equal(isValidCommand('{"command": "stop"}'), true);
    assert.equal(isValidCommand({ command: 'restricted mode' }), true);
    assert.equal(isValidCommand({ command: 'confirm', question_id: 'q123', value: 'yes' }), true);
    assert.equal(isValidCommand({ command: 'confirm', question_id: 'q123', value: 'no' }), true);
  });

  it('rejects unauthorized or invalid commands', () => {
    // Malformed JSON
    assert.equal(isValidCommand('{bad_json}'), false);
    // Arbitrary unwhitelisted commands
    assert.equal(isValidCommand({ command: 'execute_shell' }), false);
    assert.equal(isValidCommand({ command: 'free' }), false);
    // Incomplete confirm
    assert.equal(isValidCommand({ command: 'confirm', question_id: 'q123', value: 'maybe' }), false);
    assert.equal(isValidCommand({ command: 'confirm' }), false);
    assert.equal(isValidCommand(null), false);
    assert.equal(isValidCommand([]), false);
  });

  it('validates token strings', () => {
    assert.equal(isValidTokenString('secret-token-123'), true);
    assert.equal(isValidTokenString(''), false);
    assert.equal(isValidTokenString('   '), false);
    assert.equal(isValidTokenString(null), false);
  });
});

describe('Bounds & Multi-Monitor Positioning', () => {
  const display1 = { x: 0, y: 0, width: 1920, height: 1040 };
  const display2 = { x: 1920, y: 0, width: 1920, height: 1080 };

  it('clamps coordinates inside display work area', () => {
    const winWidth = 480;
    const winHeight = 420;

    // Normal inside
    assert.deepEqual(clampToBounds(500, 300, winWidth, winHeight, display1), { x: 500, y: 300 });

    // Escaping left / top
    assert.deepEqual(clampToBounds(-50, -100, winWidth, winHeight, display1), { x: 0, y: 0 });

    // Escaping right / bottom
    assert.deepEqual(clampToBounds(2000, 1200, winWidth, winHeight, display1), {
      x: 1920 - 480,
      y: 1040 - 420
    });
  });

  it('calculates default bottom-right position above taskbar', () => {
    const pos = calculateDefaultPosition(display1, 480, 420, 20, 20);
    assert.equal(pos.x, 1920 - 480 - 20);
    assert.equal(pos.y, 1040 - 420 - 20);
  });

  it('finds display containing window center', () => {
    const displays = [{ workArea: display1 }, { workArea: display2 }];

    const dispOnFirst = findDisplayForWindow(100, 100, 480, 420, displays);
    assert.deepEqual(dispOnFirst.workArea, display1);

    const dispOnSecond = findDisplayForWindow(2100, 100, 480, 420, displays);
    assert.deepEqual(dispOnSecond.workArea, display2);
  });
});

describe('Speech Bubble Formatting & Sanitization', () => {
  it('enforces maximum length of 140 chars with ellipsis', () => {
    const shortText = 'Hello World!';
    assert.equal(sanitizeBubbleText(shortText), shortText);

    const longText = 'A'.repeat(200);
    const sanitized = sanitizeBubbleText(longText);
    assert.equal(sanitized.length, 140);
    assert.ok(sanitized.endsWith('…'));
  });

  it('strips non-printable control characters', () => {
    const dirty = 'Hello\x00\x07\x1BWorld!';
    assert.equal(sanitizeBubbleText(dirty), 'HelloWorld!');
  });

  it('handles null and undefined gracefully', () => {
    assert.equal(sanitizeBubbleText(null), '');
    assert.equal(sanitizeBubbleText(undefined), '');
  });
});

describe('Hit Mask & Alpha Mask Verification', () => {
  it('builds hitmask from RGBA bitmap and detects hits correctly', () => {
    const width = 4;
    const height = 4;
    // 4x4 RGBA bitmap = 64 bytes
    const buf = Buffer.alloc(width * height * 4);

    // Pixel (1, 1) solid red (alpha = 255 at offset (1*4 + 1)*4 + 3 = 23)
    const p1Offset = (1 * 4 + 1) * 4;
    buf[p1Offset] = 255;
    buf[p1Offset + 3] = 255;

    // Pixel (2, 2) semi-transparent (alpha = 10, below threshold 20)
    const p2Offset = (2 * 4 + 2) * 4;
    buf[p2Offset + 3] = 10;

    const mask = buildHitMask(buf, width, height, 20);
    assert.equal(mask.length, 16);
    assert.equal(isMaskNonEmpty(mask), true);

    assert.equal(isHit(mask, width, height, 1, 1), true);
    assert.equal(isHit(mask, width, height, 0, 0), false);
    assert.equal(isHit(mask, width, height, 2, 2), false); // Below threshold
    assert.equal(isHit(mask, width, height, -1, 0), false); // Out of bounds
    assert.equal(isHit(mask, width, height, 5, 5), false);  // Out of bounds
  });

  it('verifies generated placeholder asset exists and mask is not all-transparent', () => {
    const placeholderPath = path.resolve(__dirname, '../assets/placeholder.png');
    assert.ok(fs.existsSync(placeholderPath), 'placeholder.png asset must exist');

    const fileBuf = fs.readFileSync(placeholderPath);
    assert.ok(fileBuf.length > 50, 'placeholder.png must have data');
    assert.equal(fileBuf.slice(0, 8).toString('hex'), '89504e470d0a1a0a');

    // Parse IDAT chunk from PNG to verify raw decompressed scanlines contain non-zero alpha
    let offset = 8;
    const idatChunks = [];
    let width = 0;
    let height = 0;

    while (offset < fileBuf.length) {
      const length = fileBuf.readUInt32BE(offset);
      const type = fileBuf.slice(offset + 4, offset + 8).toString('ascii');
      const data = fileBuf.slice(offset + 8, offset + 8 + length);
      if (type === 'IHDR') {
        width = data.readUInt32BE(0);
        height = data.readUInt32BE(4);
      } else if (type === 'IDAT') {
        idatChunks.push(data);
      }
      offset += 12 + length;
    }

    assert.ok(width > 0 && height > 0, 'IHDR width and height must be parsed');
    const decompressed = zlib.inflateSync(Buffer.concat(idatChunks));

    // Convert filter-byte scanlines to contiguous RGBA buffer
    const rawRgba = Buffer.alloc(width * height * 4);
    const rowStride = 1 + width * 4;
    for (let y = 0; y < height; y++) {
      const srcOffset = y * rowStride + 1;
      const dstOffset = y * width * 4;
      decompressed.copy(rawRgba, dstOffset, srcOffset, srcOffset + width * 4);
    }

    const mask = buildHitMask(rawRgba, width, height, 20);
    assert.equal(mask.length, width * height);
    assert.equal(isMaskNonEmpty(mask), true, 'Placeholder mask must NOT be all-transparent');
  });
});
