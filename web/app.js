// TuxLens phone page: picks the camera, hands frames to the encoder worker and obeys the PC.
'use strict';

const $ = (id) => document.getElementById(id);
const lang = (navigator.language || 'en').slice(0, 2);
const T = {
  es: { connecting: 'Conectando con el PC…', connected: 'Abriendo la cámara…', live: 'En directo', offline: 'Sin conexión con el PC',
        offlineHelp: 'Comprueba que el PC está encendido y en la misma Wi-Fi.',
        standby: 'En pausa', standbyHelp: 'El PC está usando otra cámara.',
        cameraoff: 'Cámara apagada', cameraoffHelp: 'Se ha apagado desde el PC.',
        ready: 'Lista', readyHelp: 'Se encenderá sola cuando una app use la cámara.',
        show: 'Ver imagen', hide: 'Ocultar imagen', fps: 'fps',
        start: 'Empezar', denied: 'Safari no tiene permiso para la cámara', unpaired: 'Este móvil no está emparejado',
        pair: 'Emparejar con el PC', approve: 'Acepta la solicitud en tu PC…', rejected: 'El PC rechazó la solicitud',
        unsupported: 'Este navegador no soporta WebCodecs. Actualiza iOS.', tapWake: 'Toca para mantener la pantalla encendida',
        front: 'Cámara frontal', back: 'Cámara principal', wide: 'Gran angular', tele: 'Teleobjetivo', dim: 'Toca para volver',
        preview: 'Vista previa', black: 'Pantalla negra', replaced: 'Otro dispositivo tomó el control',
        orient: 'Activar giro automático', micTap: 'Toca para activar el micrófono' },
  en: { connecting: 'Connecting to the PC…', connected: 'Opening the camera…', live: 'Live', offline: 'No connection to the PC',
        offlineHelp: 'Check that the PC is on and on the same Wi-Fi.',
        standby: 'Paused', standbyHelp: 'The PC is using another camera.',
        cameraoff: 'Camera off', cameraoffHelp: 'It was turned off from the PC.',
        ready: 'Ready', readyHelp: 'It turns on by itself when an app uses the camera.',
        show: 'Show picture', hide: 'Hide picture', fps: 'fps',
        start: 'Start', denied: 'Safari has no camera permission', unpaired: 'This phone is not paired',
        pair: 'Pair with the PC', approve: 'Accept the request on your PC…', rejected: 'The PC rejected the request',
        unsupported: 'This browser lacks WebCodecs. Update iOS.', tapWake: 'Tap to keep the screen on',
        front: 'Front camera', back: 'Main camera', wide: 'Ultra wide', tele: 'Telephoto', dim: 'Tap to come back',
        preview: 'Preview', black: 'Black screen', replaced: 'Another device took over',
        orient: 'Enable auto-rotate', micTap: 'Tap to enable the microphone' },
};
const t = (k) => (T[lang] || T.en)[k] || T.en[k];

// ---------------------------------------------------------------- pairing token
const hashParams = new URLSearchParams(location.hash.slice(1));
if (hashParams.get('t')) {
  localStorage.setItem('tuxlens.token', hashParams.get('t'));
  // replaceState before the camera starts: pushState/hash changes later can mute the track (WebKit bug).
  history.replaceState(null, '', location.pathname);
}
let token = localStorage.getItem('tuxlens.token');
if (token) {
  // Lets the PC hand the token to a home-screen copy of this page (see /manifest.webmanifest).
  document.cookie = `tuxlens_t=${encodeURIComponent(token)}; Path=/; Max-Age=31536000; Secure; SameSite=Strict`;
}
const standalone = window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
const wsBase = `wss://${location.host}`;

// ---------------------------------------------------------------- state
const worker = new Worker('encoder-worker.js');
let control = null;
let config = null;            // last config from the PC
let stream = null;
let track = null;
let cameras = [];             // [{deviceId, label, kind}]
let currentKind = null;
let useFallback = false;
let fallbackVideo = null;
let wakeLock = null;
let starting = false;
let encoderInfo = null;
let lastStats = null;
let cameraInfo = null;

function setStatus(key, detail) {
  document.body.dataset.state = key;
  $('status-text').textContent = t(key);
  $('detail').textContent = detail ?? ({ offline: t('offlineHelp'), standby: t('standbyHelp'),
                                         cameraoff: t('cameraoffHelp'), ready: t('readyHelp') }[key] || '');
  $('status').textContent = `${t(key)}. ${$('detail').textContent}`;
  if (key === 'live') armDim(); else undim();
}

// ---------------------------------------------------------------- screen dimming
// The phone is usually turned away while it films: once live and untouched, fade almost to black.
let dimTimer = 0;
function undim() {
  document.body.classList.remove('dimmed');
  clearTimeout(dimTimer);
}
function armDim() {
  undim();
  if ($('preview').classList.contains('on')) return;
  dimTimer = setTimeout(() => {
    if (document.body.dataset.state === 'live') document.body.classList.add('dimmed');
  }, 20000);
}
document.addEventListener('pointerdown', () => {
  const wasDimmed = document.body.classList.contains('dimmed');
  if (document.body.dataset.state === 'live') armDim(); else undim();
  return wasDimmed;
}, { capture: true });

function send(obj) {
  if (control && control.readyState === WebSocket.OPEN) control.send(JSON.stringify(obj));
}

function log(message) { console.log(message); send({ type: 'log', message }); }

// ---------------------------------------------------------------- cameras
function classify(label) {
  const l = label.toLowerCase();
  if (/front|delanter|frontal|avant|vorder/.test(l)) return 'front';
  if (/dual|triple|doble/.test(l)) return 'virtual';   // switches lenses on its own: avoid
  if (/ultra/.test(l)) return 'wide';
  if (/tele/.test(l)) return 'tele';
  if (/back|rear|trasera|arrière|rück|posterior/.test(l)) return 'back';
  return 'other';
}

async function listCameras() {
  const devices = await navigator.mediaDevices.enumerateDevices();
  cameras = devices.filter((d) => d.kind === 'videoinput' && d.label)
    .map((d) => ({ deviceId: d.deviceId, label: d.label, kind: classify(d.label) }));
  return cameras;
}

function pickCamera(kind) {
  const exact = cameras.find((c) => c.kind === kind);
  if (exact) return exact;
  if (kind !== 'front') return cameras.find((c) => c.kind === 'back') || cameras.find((c) => c.kind !== 'front');
  return null;
}

function constraintsFor(cam, strictFps = true) {
  const c = config || { width: 1280, height: 720, fps: 30 };
  // Upright phone: only a strip of the sensor is used for a 16:9 picture, so capture at 1080p
  // to keep that strip sharp (the strip is cropped before encoding, see encoder-worker.js).
  const upright = screenAngle() % 180 === 0;
  const sizes = { '720p': [1280, 720], '1080p': [1920, 1080], '4k': [3840, 2160] };
  let [w, h] = sizes[c.capture] || (upright ? [Math.max(1920, c.width), Math.max(1080, c.height)] : [c.width, c.height]);
  // Safari ignores an "ideal" 60 fps on some lenses; a minimum forces the 60 fps format.
  const frameRate = strictFps && c.fps >= 50 ? { ideal: c.fps, min: 50 } : { ideal: c.fps };
  const video = { width: { ideal: w }, height: { ideal: h }, frameRate };
  if (cam) video.deviceId = { exact: cam.deviceId };
  else video.facingMode = currentKind === 'front' ? 'user' : 'environment';
  return { audio: false, video };
}

async function startCamera(kind) {
  if (starting) return;
  starting = true;
  try {
    worker.postMessage({ type: 'stop' });
    if (track) track.stop();        // iPhone allows a single active camera
    track = null;
    if (!cameras.length) {
      // Labels are only exposed after a first grant.
      const probe = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
      probe.getTracks().forEach((tr) => tr.stop());
      await listCameras();
    }
    currentKind = kind;
    const cam = pickCamera(kind);
    try {
      stream = await navigator.mediaDevices.getUserMedia(constraintsFor(cam));
    } catch (e) {
      if (e.name !== 'OverconstrainedError') throw e;
      log(`${cam ? cam.label : kind}: ${config?.fps} fps not available, falling back`);
      stream = await navigator.mediaDevices.getUserMedia(constraintsFor(cam, false));
    }
    track = stream.getVideoTracks()[0];
    track.addEventListener('ended', () => { if (document.visibilityState === 'visible') startCamera(currentKind); });
    await applyTrackControls();
    const wanted = config?.fps || 30;
    if ((track.getSettings().frameRate || 0) < wanted - 5) {
      // getUserMedia may pick a 30 fps format even when the lens can do 60: ask again explicitly.
      try {
        await track.applyConstraints({ ...constraintsFor(cam, false).video, frameRate: { exact: wanted } });
      } catch (e) { log(`${wanted} fps rejected: ${e.name} ${e.constraint || ''}`); }
      const st = track.getSettings();
      log(`camera now ${st.width}x${st.height}@${st.frameRate}`);
    }
    const s = track.getSettings();
    const info = { width: s.width, height: s.height, fps: Math.round(s.frameRate || config?.fps || 30) };
    if (useFallback) startFallback(info);
    else {
      const clone = track.clone();
      worker.postMessage({ type: 'track', track: clone, info }, [clone]);
    }
    if ($('preview').classList.contains('on')) $('preview-video').srcObject = stream;
    reportState(cam);
    lastSentOrientation = '';
    sendOrientation();
    cameraInfo = { kind, fps: info.fps };
    $('camera').textContent = `${t(kind) || kind}, ${info.fps} ${t('fps')}`;
  } catch (e) {
    if (e.name === 'NotAllowedError') setStatus('offline', t('denied'));
    log(`camera error: ${e.name}: ${e.message}`);
    $('start').hidden = false;
  } finally {
    starting = false;
  }
}

async function applyTrackControls() {
  if (!track || !config) return;
  const caps = track.getCapabilities ? track.getCapabilities() : {};
  const advanced = {};
  if (caps.zoom) advanced.zoom = Math.min(caps.zoom.max, Math.max(caps.zoom.min, config.zoom || 1));
  if (caps.torch) advanced.torch = !!config.torch;
  if (Object.keys(advanced).length) {
    try { await track.applyConstraints({ advanced: [advanced] }); } catch (e) { log(`constraints: ${e.message}`); }
  }
}

function reportState(cam) {
  if (!track) return;
  const s = track.getSettings();
  const caps = track.getCapabilities ? track.getCapabilities() : {};
  send({
    type: 'state', camera: currentKind, label: cam ? cam.label : null,
    width: s.width, height: s.height, fps: s.frameRate,
    zoom: caps.zoom ? { min: caps.zoom.min, max: caps.zoom.max, value: s.zoom } : null,
    torch: !!caps.torch, fallback: useFallback, encoder: encoderInfo,
    caps: { frameRate: caps.frameRate, width: caps.width, height: caps.height },
    available: [...new Set(cameras.map((c) => c.kind))].filter((k) => k !== 'virtual' && k !== 'other'),
  });
}

// Fallback when MediaStreamTrackProcessor is missing in workers: one VideoFrame per video frame.
function startFallback(info) {
  if (!fallbackVideo) {
    fallbackVideo = document.createElement('video');
    fallbackVideo.muted = true;
    fallbackVideo.playsInline = true;
  }
  fallbackVideo.srcObject = stream;
  fallbackVideo.play().catch(() => {});
  const pump = () => {
    if (!track || track.readyState !== 'live' || fallbackVideo.srcObject !== stream) return;
    try {
      const frame = new VideoFrame(fallbackVideo);
      worker.postMessage({ type: 'frame', frame, info }, [frame]);
    } catch (_) { /* not ready yet */ }
    fallbackVideo.requestVideoFrameCallback(pump);
  };
  fallbackVideo.requestVideoFrameCallback(pump);
}

// ---------------------------------------------------------------- config from the PC
async function applyConfig(next) {
  const prev = config;
  config = next;
  worker.postMessage({ type: 'config', config: next });
  if (next.active === false) {
    // The PC is using another camera: free the phone camera (and battery), keep the mic going.
    if (track) { worker.postMessage({ type: 'stop' }); track.stop(); track = null; stream = null; }
    $('preview-video').srcObject = null;
    setStatus({ off: 'cameraoff', idle: 'ready' }[next.reason] || 'standby');
    $('camera').textContent = '';
    if (next.mic && !micStream) startMic();
    else if (!next.mic && micStream) stopMic();
    return;
  }
  const needRestart = !prev || !track || prev.camera !== next.camera || prev.width !== next.width
    || prev.height !== next.height || prev.fps !== next.fps || prev.capture !== next.capture;
  if (needRestart) await startCamera(next.camera);
  else if (prev.zoom !== next.zoom || prev.torch !== next.torch) {
    await applyTrackControls();
    reportState(pickCamera(currentKind));
  }
  const processingChanged = prev && (prev.mic_echo !== next.mic_echo || prev.mic_noise !== next.mic_noise
    || prev.mic_agc !== next.mic_agc);
  if (next.mic && micStream && processingChanged) { stopMic(); startMic(); }
  else if (next.mic && !micStream) startMic();
  else if (!next.mic && micStream) stopMic();
}

// ---------------------------------------------------------------- microphone
// Raw 48 kHz mono PCM in 10 ms chunks over its own socket (no codec delay; ~770 kbit/s).
let micStream = null;
let micCtx = null;
let micNode = null;
let micWs = null;
let micSeq = 0;

async function startMic() {
  if (micStream) return;
  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: config?.mic_echo ?? true, noiseSuppression: config?.mic_noise ?? true,
               autoGainControl: config?.mic_agc ?? true, channelCount: 1 },
      video: false,
    });
    micCtx = new AudioContext({ sampleRate: 48000, latencyHint: 'interactive' });
    await micCtx.audioWorklet.addModule('mic-worklet.js');
    const source = micCtx.createMediaStreamSource(micStream);
    micNode = new AudioWorkletNode(micCtx, 'mic-chunker');
    const silent = micCtx.createGain();
    silent.gain.value = 0;                     // keeps the graph pulled without playing anything
    source.connect(micNode).connect(silent).connect(micCtx.destination);
    micNode.port.onmessage = (ev) => sendAudio(ev.data);
    if (micCtx.state !== 'running') await micCtx.resume().catch(() => {});
    $('mic-hint').hidden = micCtx.state === 'running';
    connectMic();
    const ms = micStream.getAudioTracks()[0].getSettings();
    send({ type: 'state', mic: true, micRate: micCtx.sampleRate, micState: micCtx.state,
           micProcessing: { echo: ms.echoCancellation, noise: ms.noiseSuppression, agc: ms.autoGainControl } });
  } catch (e) {
    log(`microphone error: ${e.name}: ${e.message}`);
    stopMic();
  }
}

function stopMic() {
  if (micWs) { micWs.onclose = null; try { micWs.close(); } catch (_) {} micWs = null; }
  if (micCtx) { micCtx.close().catch(() => {}); micCtx = null; }
  if (micStream) { micStream.getTracks().forEach((tr) => tr.stop()); micStream = null; }
  micNode = null;
  $('mic-hint').hidden = true;
  send({ type: 'state', mic: false });
}

function connectMic() {
  if (!micStream || !token) return;
  micWs = new WebSocket(`${wsBase}/ws/audio?t=${encodeURIComponent(token)}`);
  micWs.binaryType = 'arraybuffer';
  micWs.onclose = () => { if (micStream) setTimeout(connectMic, 1000); };
}

function sendAudio(pcm) {
  if (!micWs || micWs.readyState !== WebSocket.OPEN) return;
  if (micWs.bufferedAmount > 9600) return;        // >100 ms queued: drop rather than lag
  const buf = new ArrayBuffer(12 + pcm.byteLength);
  const view = new DataView(buf);
  view.setUint32(0, micSeq++ >>> 0, true);
  view.setFloat64(4, performance.timeOrigin + performance.now(), true);
  new Uint8Array(buf, 12).set(new Uint8Array(pcm));
  micWs.send(buf);
}

$('mic-hint').addEventListener('click', async () => {
  if (micCtx) await micCtx.resume().catch(() => {});
  $('mic-hint').hidden = !micCtx || micCtx.state === 'running';
});

// ---------------------------------------------------------------- control socket
// ---------------------------------------------------------------- pairing by approval
function saveToken(value) {
  token = value;
  localStorage.setItem('tuxlens.token', value);
  document.cookie = `tuxlens_t=${encodeURIComponent(value)}; Path=/; Max-Age=31536000; Secure; SameSite=Strict`;
}

async function tokenIsValid() {
  if (!token) return false;
  try {
    const r = await fetch(`/pair/check?t=${encodeURIComponent(token)}`, { cache: 'no-store' });
    return (await r.json()).paired === true;
  } catch (_) {
    return true;   // PC unreachable: keep the token and let the socket retry
  }
}

function showPairing(message) {
  setStatus('unpaired', message || '');
  $('pair').hidden = false;
  $('pair').disabled = false;
  $('pair').textContent = t('pair');
}

async function requestPairing() {
  $('pair').disabled = true;
  setStatus('connecting', t('approve'));
  try {
    const r = await fetch('/pair/request', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                              body: JSON.stringify({ name: navigator.platform || 'Phone' }) });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const { id } = await r.json();
    for (;;) {
      const w = await (await fetch(`/pair/wait?id=${encodeURIComponent(id)}`, { cache: 'no-store' })).json();
      if (w.status === 'approved') {
        saveToken(w.token);
        $('pair').hidden = true;
        connectControl();
        return;
      }
      if (w.status === 'rejected') { showPairing(t('rejected')); return; }
    }
  } catch (e) {
    showPairing(`${t('unpaired')} (${e.message})`);
  }
}

$('pair').addEventListener('click', () => requestPairing());

let everOpened = false;

function connectControl() {
  if (!token) { showPairing(); return; }
  setStatus('connecting');
  control = new WebSocket(`${wsBase}/ws/control?t=${encodeURIComponent(token)}`);
  control.onopen = async () => {
    everOpened = true;
    setStatus('connected');
    lastSentOrientation = '';
    if (!cameras.length) { try { await listCameras(); } catch (_) {} }
    worker.postMessage({ type: 'start', url: `${wsBase}/ws/video?t=${encodeURIComponent(token)}`, config: config || {} });
    send({ type: 'hello', userAgent: navigator.userAgent, name: navigator.platform, standalone,
           cameras: cameras.map((c) => ({ kind: c.kind, label: c.label })) });
  };
  control.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    if (msg.type === 'ping') send({ type: 'pong', t: msg.t, tp: performance.timeOrigin + performance.now() });
    else if (msg.type === 'config') applyConfig(msg);
    else if (msg.type === 'reload') { if (track) track.stop(); location.reload(); }
  };
  control.onclose = async (ev) => {
    if (ev.code === 4000) { setStatus('offline', t('replaced')); return; }
    // A rejected handshake looks like any network error: ask the PC whether the token still works.
    if (ev.code === 4001 || !(await tokenIsValid())) { showPairing(); return; }
    setStatus('offline');
    setTimeout(connectControl, everOpened ? 1500 : 3000);
  };
}

worker.onmessage = (ev) => {
  const msg = ev.data;
  if (msg.type === 'capabilities') {
    if (!msg.encoder) { setStatus('offline', t('unsupported')); return; }
    if (!msg.mstp && !useFallback) {
      useFallback = true;
      log('MediaStreamTrackProcessor missing in worker: using VideoFrame fallback');
      if (track) startCamera(currentKind);
    }
  } else if (msg.type === 'video') {
    if (msg.state === 'open') setStatus('live');
    else if (control && control.readyState === WebSocket.OPEN) setStatus('connected');
  } else if (msg.type === 'encoder') {
    encoderInfo = msg;
    reportState(pickCamera(currentKind));
  } else if (msg.type === 'stats') {
    lastStats = msg.stats;
    send({ type: 'stats', ...msg.stats });
    const s = msg.stats;
    // Technical figures stay out of the way: tap the lens to show them.
    $('stats').textContent = `${Math.round(s.encoded / 2)} ${t('fps')} enviados, ${(s.kbps / 1000).toFixed(1)} Mbps, `
      + `codificar ${s.encodeMs} ms` + (s.droppedBusy ? `, ${s.droppedBusy} descartados` : '');
  } else if (msg.type === 'orientation') {
    frameRotation = msg.rotation;
    sendOrientation();
  } else if (msg.type === 'log') {
    log(msg.message);
  }
};

// ---------------------------------------------------------------- orientation
// Safari (27.x) hands over frames in sensor orientation (landscape) and leaves VideoFrame.rotation
// at 0, so the clockwise rotation the PC must apply is derived from the UI orientation:
//   back camera:  portrait → 90, landscape (home/USB right) → 0, other landscape → 180
//   front camera: portrait → 90, landscapes swapped compared to the back camera.
let frameRotation = 0;
let lastSentOrientation = '';

// Physical orientation from gravity, so the phone can rest in any position (rotation lock on,
// upside down…). Angle = counter-clockwise device rotation, same convention as screen.orientation.
let physicalAngle = null;
let motionAsked = false;

function onMotion(ev) {
  const g = ev.accelerationIncludingGravity;
  if (!g || g.x == null) return;
  if (Math.abs(g.z) > 8.5) return;              // lying flat: keep the last orientation
  // iOS reports the gravity vector pointing down in device coordinates (upright: y ≈ -9.8).
  const deg = (Math.atan2(g.x, -g.y) * 180 / Math.PI + 360) % 360;
  const nearest = (Math.round(deg / 90) * 90) % 360;
  const off = Math.min(Math.abs(deg - nearest), 360 - Math.abs(deg - nearest));
  if (off > 30 && physicalAngle !== null) return;  // hysteresis between quadrants
  if (nearest !== physicalAngle) {
    const firstReading = physicalAngle === null;
    physicalAngle = nearest;
    if (firstReading) log(`gravity x=${g.x.toFixed(1)} y=${g.y.toFixed(1)} z=${g.z.toFixed(1)} → ${nearest}°`);
    sendOrientation();
  }
}

async function enableMotion() {
  if (motionAsked || typeof DeviceMotionEvent === 'undefined') return;
  motionAsked = true;
  try {
    if (typeof DeviceMotionEvent.requestPermission === 'function') {
      const answer = await DeviceMotionEvent.requestPermission();   // needs a tap on iOS
      if (answer !== 'granted') { log('motion permission denied: using screen orientation'); return; }
    }
    window.addEventListener('devicemotion', onMotion);
  } catch (e) {
    motionAsked = false;   // not called from a tap yet: retry on the next one
  }
}

function screenAngle() {
  if (physicalAngle !== null) return physicalAngle;
  if (screen.orientation && typeof screen.orientation.angle === 'number') return screen.orientation.angle;
  return typeof window.orientation === 'number' ? (window.orientation + 360) % 360 : 0;
}

function sendOrientation() {
  let rotation = frameRotation;
  if (!rotation) {
    const angle = screenAngle();
    rotation = currentKind === 'front' ? (90 + angle) % 360 : (450 - angle) % 360;
  }
  const key = `${rotation}`;
  const needsTap = typeof DeviceMotionEvent !== 'undefined' && typeof DeviceMotionEvent.requestPermission === 'function';
  $('orient-hint').hidden = physicalAngle !== null || !needsTap;
  if (key === lastSentOrientation) return;
  lastSentOrientation = key;
  worker.postMessage({ type: 'rotation', rotation });
  send({ type: 'state', rotation, flip: false, screenAngle: screenAngle() });
}

if (screen.orientation) screen.orientation.addEventListener('change', sendOrientation);
window.addEventListener('orientationchange', sendOrientation);

// ---------------------------------------------------------------- screen
async function keepAwake() {
  try {
    if ('wakeLock' in navigator && !wakeLock) {
      wakeLock = await navigator.wakeLock.request('screen');
      wakeLock.addEventListener('release', () => { wakeLock = null; });
      $('wake-hint').hidden = true;
    }
  } catch (_) {
    $('wake-hint').hidden = false;
  }
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible') return;
  keepAwake();
  if (!track || track.readyState === 'ended') startCamera(currentKind || config?.camera || 'back');
});

document.addEventListener('click', () => { keepAwake(); enableMotion(); }, { capture: true });
$('start').addEventListener('click', () => { $('start').hidden = true; startCamera(config?.camera || 'back'); });
$('toggle-preview').addEventListener('click', () => {
  const on = $('preview').classList.toggle('on');
  $('preview-video').srcObject = on ? stream : null;
  $('toggle-preview').textContent = on ? t('hide') : t('show');
  if (document.body.dataset.state === 'live') armDim();
});
$('lens').addEventListener('click', () => { $('stats').hidden = !$('stats').hidden; });
$('host').textContent = /^[\d.]+$|:/.test(location.hostname) ? location.hostname : location.hostname.split('.')[0];
$('toggle-black').addEventListener('click', () => { $('black').hidden = false; });
$('black').addEventListener('click', () => { $('black').hidden = true; });

$('orient-hint').textContent = t('orient');
$('mic-hint').textContent = t('micTap');
$('toggle-preview').textContent = t('show');
$('toggle-black').textContent = t('black');
$('start').textContent = t('start');
$('wake-hint').textContent = t('tapWake');
$('black-hint').textContent = t('dim');

const demo = hashParams.get('demo');   // screenshots: #demo=live|connecting|offline|unpaired
if (demo) {
  if (demo === 'unpaired') showPairing();
  else setStatus(demo);
  if (demo === 'live') $('camera').textContent = `${t('wide')}, 60 ${t('fps')}`;
} else if (typeof VideoEncoder === 'undefined') setStatus('offline', t('unsupported'));
else { connectControl(); keepAwake(); }
