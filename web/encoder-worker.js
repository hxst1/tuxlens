// TuxLens encoder worker: camera frames → hardware H.264 (WebCodecs) → WebSocket.
//
// Latency rules:
//  * latencyMode "realtime", no B-frames (Baseline, or Main/High with reordering off in WebKit).
//  * framerate in the config must match the camera: WebKit cuts the bitrate to 1/3 when the real
//    frame rate falls below half of the configured one.
//  * Never queue: if the encoder or the socket is behind, the frame is dropped *before* encoding,
//    which keeps the reference chain intact (no keyframe needed).
'use strict';

const HEADER_BYTES = 24;
const FLAG_KEY = 1;

let ws = null;
let wsUrl = null;
let encoder = null;
let encoderKey = '';
let cfg = { width: 1280, height: 720, fps: 30, bitrate: 6000000, codec: 'baseline', keyframe_interval_s: 3 };
let trackInfo = { width: 0, height: 0, fps: 30 };
let reader = null;
let readerGen = 0;
let seq = 0;
let forceKey = true;
let lastKeyAt = 0;
let reconnectTimer = null;
const captureTimes = new Map();

const stats = { read: 0, encoded: 0, droppedBusy: 0, droppedOffline: 0, keyframes: 0, bytes: 0,
                encodeMsSum: 0, maxBuffered: 0, errors: 0 };

const now = () => performance.timeOrigin + performance.now();

function log(message) { postMessage({ type: 'log', message }); }

// ---------------------------------------------------------------- codec selection
function avcCodecString(profile, width, height, fps) {
  const mbps = Math.ceil(width / 16) * Math.ceil(height / 16) * fps;
  // H.264 levels by macroblocks/second: 3.1, 3.2, 4.0, 4.2, 5.1
  const level = mbps <= 108000 ? '1F' : mbps <= 216000 ? '20' : mbps <= 245760 ? '28'
              : mbps <= 522240 ? '2A' : '33';
  const prefix = { baseline: '42E0', main: '4D40', high: '6400' }[profile] || '42E0';
  return `avc1.${prefix}${level}`;
}

async function encoderConfig(width, height, fps) {
  const profiles = [cfg.codec, 'baseline'].filter((p, i, a) => a.indexOf(p) === i);
  for (const profile of profiles) {
    const config = {
      codec: avcCodecString(profile, width, height, fps),
      width, height,
      framerate: fps,
      bitrate: cfg.bitrate,
      bitrateMode: 'constant',
      latencyMode: 'realtime',
      hardwareAcceleration: 'prefer-hardware',
      avc: { format: 'annexb' },
    };
    try {
      const support = await VideoEncoder.isConfigSupported(config);
      if (support.supported) return { config, profile };
    } catch (e) { /* try next */ }
  }
  throw new Error(`no supported H.264 config for ${width}x${height}@${fps}`);
}

async function ensureEncoder(width, height) {
  const fps = Math.round(trackInfo.fps || cfg.fps || 30);
  const key = `${width}x${height}@${fps}/${cfg.bitrate}/${cfg.codec}`;
  if (encoder && encoder.state === 'configured' && key === encoderKey) return;
  const { config, profile } = await encoderConfig(width, height, fps);
  if (!encoder || encoder.state === 'closed') {
    encoder = new VideoEncoder({ output: onChunk, error: onEncoderError });
  }
  encoder.configure(config);
  encoderKey = key;
  forceKey = true;
  postMessage({ type: 'encoder', codec: config.codec, profile, width, height, fps, bitrate: cfg.bitrate });
}

function onEncoderError(e) {
  stats.errors++;
  log(`encoder error: ${e && e.message}`);
  try { encoder && encoder.close(); } catch (_) {}
  encoder = null;
  encoderKey = '';
  forceKey = true;
}

function onChunk(chunk) {
  const tCap = captureTimes.get(chunk.timestamp);
  captureTimes.delete(chunk.timestamp);
  const key = chunk.type === 'key';
  const buf = new ArrayBuffer(HEADER_BYTES + chunk.byteLength);
  const view = new DataView(buf);
  view.setUint8(0, 1);
  view.setUint8(1, key ? FLAG_KEY : 0);
  view.setUint16(2, 0, true);
  view.setUint32(4, seq++ >>> 0, true);
  view.setFloat64(8, tCap || now(), true);
  view.setFloat64(16, now(), true);
  chunk.copyTo(new Uint8Array(buf, HEADER_BYTES));
  stats.encoded++;
  stats.bytes += chunk.byteLength;
  if (key) stats.keyframes++;
  if (tCap) stats.encodeMsSum += now() - tCap;
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(buf);
    if (ws.bufferedAmount > stats.maxBuffered) stats.maxBuffered = ws.bufferedAmount;
  }
}

// ---------------------------------------------------------------- frames in
let lastOrientation = '';
let rotation = 0;          // clockwise rotation the PC will apply (sent by the page)
// Safari 27.2 ignores visibleRect when encoding (it squeezes the whole frame into the crop size),
// so the strip is copied with a GPU blit into an OffscreenCanvas instead. The PC can turn this
// off with the phone_crop setting, and then crops on its side.
let cropDisabled = false;
let cropCanvas = null;
let cropCtx = null;

function cropWithCanvas(frame, rect) {
  if (!cropCanvas || cropCanvas.width !== rect.width || cropCanvas.height !== rect.height) {
    cropCanvas = new OffscreenCanvas(rect.width, rect.height);
    cropCtx = cropCanvas.getContext('2d', { alpha: false, desynchronized: true });
  }
  cropCtx.drawImage(frame, rect.x, rect.y, rect.width, rect.height, 0, 0, rect.width, rect.height);
  return new VideoFrame(cropCanvas, { timestamp: frame.timestamp });
}

// Crop to the output aspect *before* encoding, in sensor coordinates: with the phone upright the
// sensor frame is landscape and only a vertical strip of it ends up in a 16:9 picture, so
// encoding the whole frame would waste two thirds of the work and bandwidth.
function cropRect(fw, fh) {
  if (cropDisabled) return null;
  const aspect = (cfg.width || 16) / (cfg.height || 9);
  const align = (v) => Math.max(16, Math.round(v / 16) * 16);
  if (rotation === 90 || rotation === 270) {
    const cw = align(fh / aspect);
    return cw < fw - 8 ? { x: ((fw - cw) / 2) & ~1, y: 0, width: cw, height: fh } : null;
  }
  const ch = align(fw / aspect);
  return ch < fh - 8 ? { x: 0, y: ((fh - ch) / 2) & ~1, width: fw, height: ch } : null;
}

async function handleFrame(frame) {
  stats.read++;
  const tRead = now();
  // Safari hands over sensor-oriented frames plus display metadata the encoder ignores:
  // forward it so the PC can rotate/mirror.
  const orientation = `${frame.rotation || 0}/${frame.flip ? 1 : 0}`;
  if (orientation !== lastOrientation) {
    lastOrientation = orientation;
    postMessage({ type: 'orientation', rotation: frame.rotation || 0, flip: !!frame.flip });
  }
  let cropped = null;
  try {
    if (!ws || ws.readyState !== WebSocket.OPEN) { stats.droppedOffline++; forceKey = true; return; }
    const perFrame = cfg.bitrate / 8 / Math.max(1, trackInfo.fps || cfg.fps);
    const limit = Math.max(48 * 1024, perFrame * 1.5);
    if ((encoder && encoder.encodeQueueSize > 1) || ws.bufferedAmount > limit) {
      stats.droppedBusy++;
      return;
    }
    const rect = cropRect(frame.displayWidth, frame.displayHeight);
    if (rect) {
      try { cropped = cropWithCanvas(frame, rect); }
      catch (e) { cropDisabled = true; log(`cropping unsupported (${e.message}), sending full frames`); }
    }
    const src = cropped || frame;
    await ensureEncoder(src.displayWidth & ~1, src.displayHeight & ~1);
    const due = tRead - lastKeyAt > cfg.keyframe_interval_s * 1000;
    const keyFrame = forceKey || due;
    if (keyFrame) { forceKey = false; lastKeyAt = tRead; }
    captureTimes.set(src.timestamp, tRead);
    if (captureTimes.size > 120) captureTimes.delete(captureTimes.keys().next().value);
    encoder.encode(src, { keyFrame });
  } catch (e) {
    if (cropped && !cropDisabled) { cropDisabled = true; log(`encoding cropped frames failed (${e.message}), disabling crop`); }
    onEncoderError(e);
  } finally {
    if (cropped) cropped.close();
    frame.close();
  }
}

async function readTrack(track) {
  const gen = ++readerGen;
  if (reader) { try { await reader.cancel(); } catch (_) {} }
  const processor = new MediaStreamTrackProcessor({ track });
  reader = processor.readable.getReader();
  forceKey = true;
  while (gen === readerGen) {
    let result;
    try { result = await reader.read(); } catch (e) { log(`track read failed: ${e.message}`); break; }
    if (result.done) break;
    if (gen !== readerGen) { result.value.close(); break; }
    await handleFrame(result.value);
  }
}

// ---------------------------------------------------------------- socket
function connect() {
  clearTimeout(reconnectTimer);
  if (!wsUrl) return;
  try { ws && ws.close(); } catch (_) {}
  ws = new WebSocket(wsUrl);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => { forceKey = true; postMessage({ type: 'video', state: 'open' }); };
  ws.onmessage = (ev) => {
    if (typeof ev.data !== 'string') return;
    try { if (JSON.parse(ev.data).type === 'key') forceKey = true; } catch (_) {}
  };
  ws.onclose = (ev) => {
    postMessage({ type: 'video', state: 'closed', code: ev.code });
    if (ev.code !== 4000 && ev.code !== 4001) reconnectTimer = setTimeout(connect, 1000);
  };
  ws.onerror = () => {};
}

setInterval(() => {
  const encoded = stats.encoded || 1;
  postMessage({ type: 'stats', stats: {
    read: stats.read, encoded: stats.encoded, droppedBusy: stats.droppedBusy,
    droppedOffline: stats.droppedOffline, keyframes: stats.keyframes, errors: stats.errors,
    kbps: Math.round(stats.bytes * 8 / 2000), encodeMs: +(stats.encodeMsSum / encoded).toFixed(1),
    maxBuffered: stats.maxBuffered,
  } });
  stats.read = stats.encoded = stats.droppedBusy = stats.droppedOffline = stats.keyframes = 0;
  stats.bytes = stats.encodeMsSum = stats.maxBuffered = 0;
}, 2000);

onmessage = async (ev) => {
  const msg = ev.data;
  switch (msg.type) {
    case 'start':
      wsUrl = msg.url;
      Object.assign(cfg, msg.config || {});
      connect();
      postMessage({ type: 'capabilities', mstp: typeof MediaStreamTrackProcessor !== 'undefined',
                    encoder: typeof VideoEncoder !== 'undefined' });
      break;
    case 'config': {
      const before = `${cfg.bitrate}/${cfg.codec}`;
      Object.assign(cfg, msg.config || {});
      if (typeof cfg.phone_crop === 'boolean') cropDisabled = !cfg.phone_crop;
      if (`${cfg.bitrate}/${cfg.codec}` !== before) encoderKey = '';
      break;
    }
    case 'track':
      trackInfo = msg.info || trackInfo;
      readTrack(msg.track);
      break;
    case 'frame':   // fallback path: frames posted from the main thread
      trackInfo = msg.info || trackInfo;
      await handleFrame(msg.frame);
      break;
    case 'stop':
      readerGen++;
      if (reader) { try { await reader.cancel(); } catch (_) {} reader = null; }
      break;
    case 'key':
      forceKey = true;
      break;
    case 'rotation':
      if (msg.rotation !== rotation) { rotation = msg.rotation; encoderKey = ''; }
      break;
  }
};
