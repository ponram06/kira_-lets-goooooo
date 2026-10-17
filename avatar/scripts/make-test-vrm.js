/**
 * avatar/scripts/make-test-vrm.js
 * Generates a tiny, original VRM 1.0 test model (a bone skeleton with one
 * small triangle and two empty expressions). Used only by the smoke test, so
 * no third-party model is ever downloaded or bundled.
 *
 *   node scripts/make-test-vrm.js <out.vrm>
 */

'use strict';

const fs = require('fs');

const BONES = [
  // name, parent, local translation (metres)
  ['hips', null, [0, 0.9, 0]], ['spine', 'hips', [0, 0.1, 0]], ['chest', 'spine', [0, 0.15, 0]],
  ['neck', 'chest', [0, 0.2, 0]], ['head', 'neck', [0, 0.1, 0]],
  ['leftUpperArm', 'chest', [0.15, 0.15, 0]], ['leftLowerArm', 'leftUpperArm', [0.25, 0, 0]], ['leftHand', 'leftLowerArm', [0.22, 0, 0]],
  ['rightUpperArm', 'chest', [-0.15, 0.15, 0]], ['rightLowerArm', 'rightUpperArm', [-0.25, 0, 0]], ['rightHand', 'rightLowerArm', [-0.22, 0, 0]],
  ['leftUpperLeg', 'hips', [0.1, -0.05, 0]], ['leftLowerLeg', 'leftUpperLeg', [0, -0.4, 0]], ['leftFoot', 'leftLowerLeg', [0, -0.4, 0]],
  ['rightUpperLeg', 'hips', [-0.1, -0.05, 0]], ['rightLowerLeg', 'rightUpperLeg', [0, -0.4, 0]], ['rightFoot', 'rightLowerLeg', [0, -0.4, 0]]
];

function buildVrm() {
  // One triangle near the head height so the renderer has something to draw
  const positions = new Float32Array([-0.1, 1.35, 0, 0.1, 1.35, 0, 0, 1.6, 0]);
  const bin = Buffer.from(positions.buffer);

  const nodes = BONES.map(([name, , t]) => ({ name, translation: t, children: [] }));
  const index = Object.fromEntries(BONES.map(([n], i) => [n, i]));
  BONES.forEach(([n, parent]) => { if (parent) nodes[index[parent]].children.push(index[n]); });
  nodes.forEach((n) => { if (!n.children.length) delete n.children; });
  const meshNode = nodes.length;
  nodes.push({ name: 'face', mesh: 0 });

  const humanBones = Object.fromEntries(BONES.map(([n], i) => [n, { node: i }]));
  const gltf = {
    asset: { version: '2.0', generator: 'kira make-test-vrm' },
    extensionsUsed: ['VRMC_vrm'],
    scene: 0,
    scenes: [{ nodes: [0, meshNode] }],
    nodes,
    meshes: [{ primitives: [{ attributes: { POSITION: 0 }, material: 0 }] }],
    materials: [{ pbrMetallicRoughness: { baseColorFactor: [0.85, 0.1, 0.15, 1] }, doubleSided: true }],
    accessors: [{ bufferView: 0, componentType: 5126, count: 3, type: 'VEC3', min: [-0.1, 1.35, 0], max: [0.1, 1.6, 0] }],
    bufferViews: [{ buffer: 0, byteOffset: 0, byteLength: bin.length }],
    buffers: [{ byteLength: bin.length }],
    extensions: {
      VRMC_vrm: {
        specVersion: '1.0',
        meta: {
          name: 'Kira test model', version: '1', authors: ['Kira project (generated)'],
          licenseUrl: 'https://vrm.dev/licenses/1.0/', avatarPermission: 'onlyAuthor',
          commercialUsage: 'personalNonProfit', allowRedistribution: false, modification: 'prohibited'
        },
        humanoid: { humanBones },
        expressions: { preset: { happy: {}, aa: {}, blink: {} } }
      }
    }
  };

  const pad = (buf, byte) => Buffer.concat([buf, Buffer.alloc((4 - (buf.length % 4)) % 4, byte)]);
  const json = pad(Buffer.from(JSON.stringify(gltf)), 0x20);
  const data = pad(bin, 0);
  const header = Buffer.alloc(12);
  header.write('glTF', 0, 'latin1');
  header.writeUInt32LE(2, 4);
  header.writeUInt32LE(12 + 8 + json.length + 8 + data.length, 8);
  const chunk = (buf, type) => {
    const h = Buffer.alloc(8);
    h.writeUInt32LE(buf.length, 0);
    h.write(type, 4, 'latin1');
    return Buffer.concat([h, buf]);
  };
  return Buffer.concat([header, chunk(json, 'JSON'), chunk(data, 'BIN\0')]);
}

if (require.main === module) {
  const out = process.argv[2];
  if (!out) { console.error('usage: node scripts/make-test-vrm.js <out.vrm>'); process.exit(2); }
  fs.writeFileSync(out, buildVrm());
  console.log(`wrote ${out}`);
}

module.exports = { buildVrm };
