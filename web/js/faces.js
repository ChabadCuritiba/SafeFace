// Face detection + recognition that runs entirely in the browser (face-api / TensorFlow.js).
// Models are served from /models on the same site - no external services.
import * as faceapi from "../vendor/face-api.esm.js";

let ready = null;

/** Load the AI models once. Resolves to the TF backend in use ("webgl" is fast, "cpu" is slow). */
export function loadModels(onProgress = () => {}) {
  if (ready) return ready;
  ready = (async () => {
    onProgress("Starting AI engine…");
    try { await faceapi.tf.setBackend("webgl"); } catch { /* falls back to cpu */ }
    await faceapi.tf.ready();
    const nets = [
      ["tinyFaceDetector", "face detector"],
      ["faceLandmark68Net", "landmark model"],
      ["faceRecognitionNet", "recognition model"],
      ["ssdMobilenetv1", "photo face detector"],
    ];
    for (const [net, label] of nets) {
      onProgress(`Loading ${label}…`);
      await faceapi.nets[net].loadFromUri("models");
    }
    return faceapi.tf.getBackend();
  })();
  ready.catch(() => { ready = null; });
  return ready;
}

function toFaces(results) {
  return results.map((r) => ({
    box: { x: r.detection.box.x, y: r.detection.box.y, width: r.detection.box.width, height: r.detection.box.height },
    score: r.detection.score,
    descriptor: r.descriptor, // Float32Array(128)
  }));
}

/** Fast detector for live video. */
export async function detectLive(input, inputSize = 416) {
  const opts = new faceapi.TinyFaceDetectorOptions({ inputSize, scoreThreshold: 0.5 });
  return toFaces(await faceapi.detectAllFaces(input, opts).withFaceLandmarks().withFaceDescriptors());
}

/** Accurate detector for photos and enrolment. */
export async function detectPhoto(input) {
  const opts = new faceapi.SsdMobilenetv1Options({ minConfidence: 0.4, maxResults: 50 });
  return toFaces(await faceapi.detectAllFaces(input, opts).withFaceLandmarks().withFaceDescriptors());
}

function distance(a, b) {
  let s = 0;
  for (let i = 0; i < a.length; i++) { const d = a[i] - b[i]; s += d * d; }
  return Math.sqrt(s);
}

/**
 * Compare a face against every enrolled face.
 * `threshold` is the maximum distance that counts as the same person (0.5 default; lower = stricter).
 * Returns { personId, name, distance, confidence } where confidence is 0–1 (0.5 at the threshold).
 */
export function matchFace(descriptor, knownFaces, threshold) {
  let best = null, bestD = Infinity;
  for (const f of knownFaces) {
    if (!f.descriptor || f.descriptor.length !== descriptor.length) continue;
    const d = distance(descriptor, f.descriptor);
    if (d < bestD) { bestD = d; best = f; }
  }
  const confidence = Math.max(0, Math.min(1, 1 - bestD / (2 * threshold)));
  if (best && bestD <= threshold) return { personId: best.personId, name: best.name, distance: bestD, confidence };
  return { personId: null, name: "Unknown", distance: bestD, confidence: best ? confidence : 0 };
}

/** Crop a face (with margin) out of a video/image/canvas into a small JPEG data URL. */
export function cropToDataUrl(source, box, size = 160, margin = 0.35) {
  const sw = source.videoWidth || source.naturalWidth || source.width;
  const sh = source.videoHeight || source.naturalHeight || source.height;
  const mx = box.width * margin, my = box.height * margin;
  const x = Math.max(0, box.x - mx), y = Math.max(0, box.y - my);
  const w = Math.min(sw - x, box.width + 2 * mx), h = Math.min(sh - y, box.height + 2 * my);
  const scale = Math.min(1, size / Math.max(w, h));
  const c = document.createElement("canvas");
  c.width = Math.max(1, Math.round(w * scale)); c.height = Math.max(1, Math.round(h * scale));
  c.getContext("2d").drawImage(source, x, y, w, h, 0, 0, c.width, c.height);
  return c.toDataURL("image/jpeg", 0.82);
}

/** Round a descriptor so it stores compactly (128 numbers, 4 decimals is plenty). */
export const packDescriptor = (d) => Array.from(d, (v) => Math.round(v * 1e4) / 1e4);
