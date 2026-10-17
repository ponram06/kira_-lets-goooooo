/**
 * avatar/src/renderer/vrm-boot.js
 * ES module: imports three.js + three-vrm (pinned, local node_modules, via the
 * import map in index.html) and builds a VrmCore. Loaded on demand with
 * import() only when a character uses the vrm renderer, so the sprite-only
 * setup never pays for three.js.
 */

import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { VRMLoaderPlugin, VRMUtils } from '@pixiv/three-vrm';

export function webglAvailable() {
  try {
    const c = document.createElement('canvas');
    return Boolean(c.getContext('webgl2') || c.getContext('webgl'));
  } catch {
    return false;
  }
}

/** Create, init and load a VRM renderer on *canvas* from model *bytes* (ArrayBuffer). */
export async function createVrmRenderer(canvas, bytes, opts) {
  const core = new window.KiraVrmCore.VrmCore(canvas, {
    THREE, GLTFLoader, VRMLoaderPlugin, VRMUtils, devicePixelRatio: window.devicePixelRatio
  }, opts);
  core.init();
  try {
    await core.load(bytes);
  } catch (err) {
    core.dispose();
    throw err;
  }
  return core;
}
