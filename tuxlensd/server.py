"""Network side of the daemon.

* HTTPS (settings.https_port): the phone page, /ws/control (JSON) and /ws/video (binary H.264).
* HTTP  (settings.setup_port): pairing page + CA download, nothing else.
* Unix socket ($XDG_RUNTIME_DIR/tuxlens/control.sock): local API for the panel, waybar and CLI.

Video message layout (little endian, 24-byte header + Annex-B access unit):
    u8 version=1 | u8 flags (bit0 = keyframe) | u16 reserved | u32 seq | f64 t_capture | f64 t_send
Timestamps are the phone's epoch milliseconds; ClockSync maps them onto the PC clock.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import json
import logging
import secrets
import socket
import ssl
import struct
from dataclasses import asdict
from pathlib import Path

from aiohttp import WSMsgType, web

from . import __version__, certs, themes
from .config import CONTROL_SOCKET, RUNTIME_DIR, Settings, data_path, pairing_token
from .audio import PhoneMic
from .decoder import H264Decoder
from .pairing import Pairing
from . import sources as local_sources
from .pipeline import DecodedFrame, OutputThread
from .stats import ClockSync, StageStats, now_ms

log = logging.getLogger("tuxlens.server")

HEADER = struct.Struct("<BBHIdd")
FLAG_KEY = 1
TCP_QUICKACK = getattr(socket, "TCP_QUICKACK", 12)

# Settings the phone needs to know about (the rest is PC-side only).
PHONE_KEYS = ("camera", "zoom", "torch", "width", "height", "fps", "bitrate", "codec", "keyframe_interval_s",
              "phone_crop", "capture", "mic", "mic_echo", "mic_noise", "mic_agc")
DEVICE_KEYS = ("device", "width", "height", "fps")


def _is_private(request: web.Request) -> bool:
    peer = request.remote or ""
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback


class Daemon:
    def __init__(self) -> None:
        self.settings = Settings.load()
        self.stats = StageStats()
        self.clock = ClockSync()
        self.theme = themes.load(self.settings.theme)
        self.output = OutputThread(self.settings, self.stats, lambda: self.theme)
        self.decoder = H264Decoder()
        self.control_ws: web.WebSocketResponse | None = None
        self.video_ws: web.WebSocketResponse | None = None
        self.phone: dict = {}
        self.waiting_key = True
        self.last_seq: int | None = None
        self._ping_task: asyncio.Task | None = None
        self.pairing = Pairing(self.language)
        self.mic = PhoneMic()
        self.local: local_sources.LocalCamera | None = None
        self.camera_active = False          # is the real camera (phone or local) running now?
        self.consumers = 0                  # apps holding the virtual camera open
        self.preview_until = 0.0            # the panel preview counts as a viewer
        self._cameras_cache: tuple[float, list] = (0.0, [])
        self.audio_ws: web.WebSocketResponse | None = None

    # -- helpers --------------------------------------------------------------------------------
    def language(self) -> str:
        from .placeholder import system_language
        lang = self.settings.language
        return system_language() if not lang or lang == "auto" else lang[:2]

    def token_ok(self, request: web.Request) -> bool:
        given = request.query.get("t", "")
        ok = bool(given) and secrets.compare_digest(given, pairing_token())
        if not ok:
            log.warning("rejected %s from %s: %s token", request.path, request.remote,
                        "missing" if not given else "wrong")
        return ok

    def urls(self) -> dict:
        host = certs.local_hostname()
        s = self.settings
        token = pairing_token()
        ips = certs.lan_addresses()
        return {
            "host": host,
            "ips": ips,
            "setup": f"http://{host}:{s.setup_port}/#t={token}",
            "setup_ip": f"http://{ips[0]}:{s.setup_port}/#t={token}" if ips else None,
            "app": f"https://{host}:{s.https_port}/#t={token}",
        }

    def phone_config(self) -> dict:
        s = self.settings
        # active=False: another source is in use, the phone turns its camera off (mic may stay on).
        active = s.source == local_sources.PHONE and self.camera_active
        if active:
            reason = None
        elif not s.camera_enabled:
            reason = "off"
        elif s.source != local_sources.PHONE:
            reason = "other-source"
        else:
            reason = "idle"                  # on demand: waiting for an app to use the camera
        return {"type": "config", "active": active, "reason": reason,
                **{k: getattr(s, k) for k in PHONE_KEYS}}

    def wants_camera(self) -> bool:
        import time as _time
        s = self.settings
        if not s.camera_enabled:
            return False
        return not s.on_demand or self.consumers > 0 or _time.monotonic() < self.preview_until

    async def update_demand(self) -> None:
        """Open or release the real camera according to the privacy switch and who is watching."""
        active = self.wants_camera()
        off = not self.settings.camera_enabled
        if off != self.output.camera_off:
            self.output.camera_off = off
            self.output.refresh_placeholder()
        if active == self.camera_active:
            return
        self.camera_active = active
        log.info("camera %s (enabled=%s, viewers=%d)", "on" if active else "released",
                 self.settings.camera_enabled, self.consumers)
        self.apply_source()
        await self.send_control(self.phone_config())

    # -- sources ---------------------------------------------------------------------------------
    def cameras(self) -> list[dict]:
        import time as _time
        stamp, cached = self._cameras_cache
        if _time.monotonic() - stamp > 5:
            cached = local_sources.list_cameras(exclude=self.settings.device)
            self._cameras_cache = (_time.monotonic(), cached)
        return cached

    def source_list(self) -> list[dict]:
        connected = self.control_ws is not None and not self.control_ws.closed
        phone_name = self.phone.get("name") or "iPhone"
        return [{"id": local_sources.PHONE, "name": phone_name, "kind": "phone", "connected": connected},
                *self.cameras()]

    def local_device(self) -> str | None:
        if self.settings.source == local_sources.PHONE:
            return None
        return os.path.realpath(self.settings.source)

    def apply_source(self) -> None:
        """Start/stop the local camera reader according to settings.source."""
        wanted = None if self.settings.source == local_sources.PHONE else self.settings.source
        if not self.camera_active:
            wanted = None                      # released: privacy switch off, or nobody watching
        if self.local is not None and (wanted is None or self.local.camera_id != wanted):
            self.local.stop()
            self.local = None
        if wanted is not None and self.local is None:
            s = self.settings
            capture_h = 1080 if s.capture == "1080p" else 720
            capture_w = capture_h * 16 // 9
            self.output.framer.reset()
            self.local = local_sources.LocalCamera(wanted, capture_w, capture_h, min(s.fps, 30), self._on_local_frame)
            self.local.start()
            log.info("source: local camera %s", wanted)
        elif wanted is None:
            log.info("source: phone")

    def _on_local_frame(self, frame, n: int, t_recv: float, t_dec: float) -> None:
        self.stats.count("frames_in")
        # No capture timestamp from V4L2 here: count from when the frame left the driver.
        self.output.mailbox.put(DecodedFrame(frame, n, t_recv, t_recv, t_dec))

    async def send_control(self, payload: dict) -> None:
        ws = self.control_ws
        if ws is not None and not ws.closed:
            await ws.send_str(json.dumps(payload))

    async def request_keyframe(self, reason: str) -> None:
        self.stats.count("keyframe_requests")
        ws = self.video_ws
        if ws is not None and not ws.closed:
            log.debug("keyframe requested: %s", reason)
            await ws.send_str('{"type":"key"}')

    async def apply_settings(self, changes: dict) -> dict:
        changed = self.settings.update(changes)
        if not changed:
            return changed
        self.settings.save()
        if "theme" in changed:
            self.theme = themes.load(self.settings.theme)
            self.output.refresh_placeholder()
        if "extra" in changed or "language" in changed:
            self.output.refresh_placeholder()
        if any(k in changed for k in DEVICE_KEYS):
            self.output.request_reconfigure()
        if "source" in changed or (self.local is not None and any(k in changed for k in ("capture", "fps"))):
            if self.local is not None and "source" not in changed:
                self.local.stop()
                self.local = None
            self.apply_source()
            await self.send_control(self.phone_config())
        if "camera_enabled" in changed or "on_demand" in changed:
            await self.update_demand()
        if "mic" in changed:
            if self.settings.mic:
                await asyncio.to_thread(self.mic.start, self.language())
            else:
                await asyncio.to_thread(self.mic.stop)
        if any(k in changed for k in PHONE_KEYS):
            await self.send_control(self.phone_config())
        log.info("settings changed: %s", changed)
        return changed

    def status(self) -> dict:
        connected = self.control_ws is not None and not self.control_ws.closed
        streaming = self.video_ws is not None and not self.video_ws.closed
        return {
            "version": __version__,
            "state": self.output.state if not self.output.error else "error",
            "error": self.output.error,
            "phone": {"connected": connected, "streaming": streaming, **self.phone},
            "clock": {"offset_ms": self.clock.offset_ms, "rtt_ms": self.clock.rtt_ms},
            "settings": asdict(self.settings),
            "stats": self.stats.snapshot(),
            "decoder_lag": self.decoder.lag,
            "source": getattr(getattr(self.output, "geometry", None), "source", ""),
            "themes": themes.available(),
            "pair_requests": self.pairing.pending(),
            "sources": self.source_list(),
            "camera": {"enabled": self.settings.camera_enabled, "active": self.camera_active,
                       "on_demand": self.settings.on_demand, "viewers": self.consumers},
            "local": {"id": self.local.camera_id, "mode": self.local.mode, "error": self.local.error}
                     if self.local is not None else None,
            "mic": {**self.mic.stats(), "streaming": self.audio_ws is not None and not self.audio_ws.closed},
            "effects_error": self.output.effects.error,
        }

    # -- phone: pairing by approval ------------------------------------------------------------
    async def pair_check(self, request: web.Request) -> web.Response:
        if not _is_private(request):
            raise web.HTTPForbidden()
        given = request.query.get("t", "")
        ok = bool(given) and secrets.compare_digest(given, pairing_token())
        return web.json_response({"paired": ok})

    async def pair_request(self, request: web.Request) -> web.Response:
        if not _is_private(request):
            raise web.HTTPForbidden()
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        req = self.pairing.create(str(body.get("name") or "iPhone"), request.remote or "?")
        if req is None:
            raise web.HTTPTooManyRequests()
        return web.json_response({"id": req.id})

    async def pair_wait(self, request: web.Request) -> web.Response:
        if not _is_private(request):
            raise web.HTTPForbidden()
        result = await self.pairing.wait(request.query.get("id", ""), timeout=25)
        if result is None:
            return web.json_response({"status": "pending"})
        if result == "":
            return web.json_response({"status": "rejected"})
        return web.json_response({"status": "approved", "token": result})

    # -- phone: control ------------------------------------------------------------------------
    async def ws_control(self, request: web.Request) -> web.StreamResponse:
        if not (_is_private(request) and self.token_ok(request)):
            raise web.HTTPForbidden()
        ws = web.WebSocketResponse(heartbeat=10, max_msg_size=1 << 20)
        await ws.prepare(request)
        if self.control_ws is not None and not self.control_ws.closed:
            await self.control_ws.close(code=4000, message=b"replaced by a new phone connection")
        self.control_ws = ws
        self.clock.reset()
        self.phone = {"address": request.remote}
        log.info("phone connected from %s", request.remote)
        await ws.send_str(json.dumps(self.phone_config()))
        if self._ping_task:
            self._ping_task.cancel()
        self._ping_task = asyncio.create_task(self._ping_loop(ws))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                await self._on_control(data)
        finally:
            if self.control_ws is ws:
                self.control_ws = None
                self.phone = {}
                log.info("phone disconnected")
        return ws

    async def _on_control(self, data: dict) -> None:
        kind = data.get("type")
        if kind == "pong":
            self.clock.add(float(data["t"]), float(data["tp"]), now_ms())
        elif kind == "hello":
            self.phone.update({k: data.get(k) for k in ("name", "cameras", "userAgent", "encoders", "standalone")})
        elif kind == "state":
            self.phone.update({k: v for k, v in data.items() if k != "type"})
            if "rotation" in data:
                self.output.phone_rotation = int(data.get("rotation") or 0) % 360
                self.output.phone_flip = bool(data.get("flip"))
                log.info("phone orientation: rotation %s, flip %s",
                         self.output.phone_rotation, self.output.phone_flip)
        elif kind == "stats":
            self.stats.phone = {k: v for k, v in data.items() if k != "type"}
        elif kind == "log":
            log.info("phone: %s", str(data.get("message"))[:500])

    async def _ping_loop(self, ws: web.WebSocketResponse) -> None:
        try:
            for i in range(10_000_000):
                if ws.closed:
                    return
                await ws.send_str(json.dumps({"type": "ping", "t": now_ms()}))
                await asyncio.sleep(0.25 if i < 8 else 2.0)
        except (ConnectionResetError, asyncio.CancelledError):
            return

    # -- phone: video --------------------------------------------------------------------------
    async def ws_video(self, request: web.Request) -> web.StreamResponse:
        if not (_is_private(request) and self.token_ok(request)):
            raise web.HTTPForbidden()
        ws = web.WebSocketResponse(heartbeat=10, max_msg_size=16 << 20, compress=False)
        await ws.prepare(request)
        if self.video_ws is not None and not self.video_ws.closed:
            await self.video_ws.close(code=4000, message=b"replaced")
        self.video_ws = ws
        sock = request.transport.get_extra_info("socket") if request.transport else None
        if sock is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                sock = None
        self.decoder.reset()
        self.waiting_key = True
        self.last_seq = None
        self.stats.reset()
        log.info("video stream started")
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    if sock is not None:
                        try:  # Linux re-enables delayed ACKs after each read
                            sock.setsockopt(socket.IPPROTO_TCP, TCP_QUICKACK, 1)
                        except OSError:
                            pass
                    await self._on_video(msg.data)
        finally:
            if self.video_ws is ws:
                self.video_ws = None
            log.info("video stream stopped")
        return ws

    async def _on_video(self, data: bytes) -> None:
        t_recv = now_ms()
        if len(data) <= HEADER.size:
            return
        version, flags, _, seq, t_capture, _t_send = HEADER.unpack_from(data)
        if version != 1:
            return
        key = bool(flags & FLAG_KEY)
        if self.settings.source != local_sources.PHONE or not self.camera_active:
            return                         # another source, or camera released: phone should be paused
        self.stats.count("frames_in")
        if self.last_seq is not None and seq != self.last_seq + 1 and not key:
            self.waiting_key = True
        self.last_seq = seq
        if self.waiting_key and not key:
            self.stats.count("skipped_stale")
            await self.request_keyframe("waiting for keyframe")
            return
        self.waiting_key = False
        try:
            frames = self.decoder.decode(memoryview(data)[HEADER.size:].tobytes())
        except Exception as exc:  # av.error.InvalidDataError and friends
            self.stats.count("decode_errors")
            log.warning("decode error: %s", exc)
            self.decoder.reset()
            self.waiting_key = True
            await self.request_keyframe("decode error")
            return
        t_dec = now_ms()
        t_cap_pc = self.clock.to_pc(t_capture)
        for frame in frames:
            self.output.mailbox.put(DecodedFrame(frame, seq, t_cap_pc, t_recv, t_dec))

    # -- phone: microphone ---------------------------------------------------------------------
    async def ws_audio(self, request: web.Request) -> web.StreamResponse:
        if not (_is_private(request) and self.token_ok(request)):
            raise web.HTTPForbidden()
        ws = web.WebSocketResponse(heartbeat=10, max_msg_size=1 << 16, compress=False)
        await ws.prepare(request)
        if self.audio_ws is not None and not self.audio_ws.closed:
            await self.audio_ws.close(code=4000, message=b"replaced")
        self.audio_ws = ws
        sock = request.transport.get_extra_info("socket") if request.transport else None
        log.info("phone microphone streaming")
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    if sock is not None:
                        try:
                            sock.setsockopt(socket.IPPROTO_TCP, TCP_QUICKACK, 1)
                        except OSError:
                            sock = None
                    self.mic.feed(msg.data, self.settings.mic_gain_db, self.settings.mic_mute)
        finally:
            if self.audio_ws is ws:
                self.audio_ws = None
            log.info("phone microphone stopped")
        return ws

    # -- local API -----------------------------------------------------------------------------
    async def api_status(self, request: web.Request) -> web.Response:
        return web.json_response(self.status())

    async def api_settings(self, request: web.Request) -> web.Response:
        changes = await request.json()
        changed = await self.apply_settings(changes if isinstance(changes, dict) else {})
        return web.json_response({"changed": changed, "settings": asdict(self.settings)})

    async def api_keyframe(self, request: web.Request) -> web.Response:
        await self.request_keyframe("api")
        return web.json_response({"ok": True})

    async def api_pair_decide(self, request: web.Request) -> web.Response:
        body = await request.json()
        ok = self.pairing.decide(str(body.get("id", "")), bool(body.get("approve")))
        return web.json_response({"ok": ok, "pending": self.pairing.pending()})

    async def api_camera_controls(self, request: web.Request) -> web.Response:
        device = self.local_device()
        if device is None:
            return web.json_response({"controls": []})
        if request.method == "POST":
            body = await request.json()
            for name, value in (body or {}).items():
                await asyncio.to_thread(local_sources.set_control, device, str(name), value)
        controls = await asyncio.to_thread(local_sources.read_controls, device)
        return web.json_response({"device": device, "controls": controls})

    async def api_mic_level(self, request: web.Request) -> web.Response:
        return web.json_response({"level_db": round(self.mic.level_db, 1), "active": self.mic.active,
                                  "streaming": self.audio_ws is not None and not self.audio_ws.closed,
                                  "clipping": self.mic.clipping})

    async def api_reload_phone(self, request: web.Request) -> web.Response:
        await self.send_control({"type": "reload"})
        return web.json_response({"ok": self.control_ws is not None})

    async def api_pair(self, request: web.Request) -> web.Response:
        if request.method == "POST":
            pairing_token(regenerate=True)
            for ws in (self.control_ws, self.video_ws):
                if ws is not None and not ws.closed:
                    await ws.close(code=4001, message=b"pairing reset")
        return web.json_response(self.urls())

    async def api_preview(self, request: web.Request) -> web.Response:
        """Small RGB thumbnail of what the virtual camera is showing (raw bytes)."""
        import av  # local import: only the panel uses this
        import time as _time
        self.preview_until = _time.monotonic() + 3.0      # an open panel keeps the camera on
        if not self.camera_active and self.wants_camera():
            await self.update_demand()
        width = max(64, min(960, int(request.query.get("w", "480")))) & ~1
        with self.output.preview_lock:
            frame = self.output.preview
        if frame is None or self.output.state == "idle":
            return web.Response(status=204)
        src_h = frame.shape[0] * 2 // 3
        src_w = frame.shape[1]
        height = (width * src_h // src_w) & ~1
        vf = av.VideoFrame.from_ndarray(frame, format="yuv420p")
        rgb = vf.reformat(width=width, height=height, format="rgb24").to_ndarray()
        return web.Response(body=rgb.tobytes(), content_type="application/octet-stream",
                            headers={"X-Width": str(width), "X-Height": str(height)})

    # -- static --------------------------------------------------------------------------------
    async def theme_css(self, request: web.Request) -> web.Response:
        return web.Response(text=themes.css_variables(self.theme), content_type="text/css",
                            headers={"Cache-Control": "no-cache"})

    async def manifest(self, request: web.Request) -> web.Response:
        """Web app manifest. A home-screen web app on iOS gets its own storage, separate from
        Safari, so it would lose the pairing token: the already-paired phone (proven by its cookie)
        gets a start_url that carries the token in the fragment."""
        if not _is_private(request):
            raise web.HTTPForbidden()
        start = "/"
        cookie = request.cookies.get("tuxlens_t", "")
        if cookie and secrets.compare_digest(cookie, pairing_token()):
            start = f"/#t={cookie}"
        c = self.theme["colors"]
        body = {
            "name": "TuxLens", "short_name": "TuxLens", "start_url": start, "scope": "/",
            "display": "standalone", "orientation": "any",
            "background_color": c["background"], "theme_color": c["background"],
            "icons": [{"src": f"/icon-{s}.png", "sizes": f"{s}x{s}", "type": "image/png", "purpose": "any"}
                      for s in (180, 192, 512)],
        }
        return web.json_response(body, content_type="application/manifest+json",
                                 headers={"Cache-Control": "no-store"})

    async def ca_cert(self, request: web.Request) -> web.Response:
        if not _is_private(request):
            raise web.HTTPForbidden()
        return web.Response(body=certs.ca_der(), content_type="application/x-x509-ca-cert",
                            headers={"Content-Disposition": 'attachment; filename="tuxlens-ca.crt"'})

    def _static(self, name: str):
        path: Path = data_path("web", name)

        async def handler(request: web.Request) -> web.FileResponse:
            if not _is_private(request):
                raise web.HTTPForbidden()
            return web.FileResponse(path, headers={"Cache-Control": "no-cache"})
        return handler

    # -- apps ----------------------------------------------------------------------------------
    def https_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self._static("index.html"))
        for name in ("app.js", "encoder-worker.js", "mic-worklet.js", "style.css", "icon.svg",
                     "icon-180.png", "icon-192.png", "icon-512.png"):
            app.router.add_get(f"/{name}", self._static(name))
        app.router.add_get("/manifest.webmanifest", self.manifest)
        app.router.add_get("/pair/check", self.pair_check)
        app.router.add_post("/pair/request", self.pair_request)
        app.router.add_get("/pair/wait", self.pair_wait)
        app.router.add_get("/theme.css", self.theme_css)
        app.router.add_get("/ca.crt", self.ca_cert)
        app.router.add_get("/ws/control", self.ws_control)
        app.router.add_get("/ws/video", self.ws_video)
        app.router.add_get("/ws/audio", self.ws_audio)
        return app

    def setup_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self._static("setup.html"))
        app.router.add_get("/style.css", self._static("style.css"))
        app.router.add_get("/icon.svg", self._static("icon.svg"))
        app.router.add_get("/theme.css", self.theme_css)
        app.router.add_get("/ca.crt", self.ca_cert)
        return app

    def control_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/status", self.api_status)
        app.router.add_post("/settings", self.api_settings)
        app.router.add_post("/keyframe", self.api_keyframe)
        app.router.add_post("/phone/reload", self.api_reload_phone)
        app.router.add_get("/mic/level", self.api_mic_level)
        app.router.add_get("/camera/controls", self.api_camera_controls)
        app.router.add_post("/camera/controls", self.api_camera_controls)
        app.router.add_post("/pair/decide", self.api_pair_decide)
        app.router.add_get("/pair", self.api_pair)
        app.router.add_post("/pair", self.api_pair)
        app.router.add_get("/preview", self.api_preview)
        app.router.add_get("/theme.css", self.theme_css)
        return app


async def watch_viewers(daemon: Daemon) -> None:
    """Once a second: who holds the virtual camera open? Drives the on-demand camera."""
    while True:
        try:
            daemon.consumers = await asyncio.to_thread(local_sources.count_consumers, daemon.settings.device)
            await daemon.update_demand()
        except Exception:
            log.exception("viewer check failed")
        await asyncio.sleep(1.0)


async def watch_state(daemon: Daemon) -> None:
    """Nudge the status bar when the visible state changes, so its module needs no polling."""
    last = None
    while True:
        await asyncio.sleep(0.5)
        connected = daemon.control_ws is not None and not daemon.control_ws.closed
        state = "error" if daemon.output.error else daemon.output.state
        current = ("live" if state == "holding" else state, connected, daemon.settings.theme,
                   daemon.settings.camera_enabled, daemon.camera_active,
                   daemon.settings.camera, daemon.settings.language, len(daemon.pairing.pending()))
        if current != last and daemon.settings.waybar_signal > 0:
            proc = await asyncio.create_subprocess_exec(
                "pkill", f"-RTMIN+{daemon.settings.waybar_signal}", "-x", "waybar",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc.wait()
        last = current


async def serve() -> None:
    daemon = Daemon()
    daemon.output.start()
    asyncio.create_task(watch_state(daemon))
    asyncio.create_task(watch_viewers(daemon))
    if daemon.settings.mic:
        await asyncio.to_thread(daemon.mic.start, daemon.language())
    daemon.apply_source()

    cert, key = certs.ensure_server_cert()
    ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ssl_ctx.load_cert_chain(cert, key)

    runners = []
    s = daemon.settings
    for app, kind in ((daemon.https_app(), "https"), (daemon.setup_app(), "setup"), (daemon.control_app(), "unix")):
        runner = web.AppRunner(app, access_log=None, handle_signals=False, shutdown_timeout=1.0)
        await runner.setup()
        runners.append(runner)
        if kind == "https":
            await web.TCPSite(runner, "0.0.0.0", s.https_port, ssl_context=ssl_ctx).start()
        elif kind == "setup":
            await web.TCPSite(runner, "0.0.0.0", s.setup_port).start()
        else:
            RUNTIME_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
            CONTROL_SOCKET.unlink(missing_ok=True)
            await web.UnixSite(runner, str(CONTROL_SOCKET)).start()
            CONTROL_SOCKET.chmod(0o600)

    urls = daemon.urls()
    log.info("TuxLens %s ready — pair the phone at %s", __version__, urls["setup"].split("#")[0])

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    import signal
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    log.info("shutting down")
    daemon.output.stop()
    for ws in (daemon.control_ws, daemon.video_ws, daemon.audio_ws):
        if ws is not None and not ws.closed:
            await ws.close(code=1001, message=b"daemon stopping")
    daemon.mic.stop()
    if daemon.local is not None:
        daemon.local.stop()
    for runner in runners:
        await runner.cleanup()
    CONTROL_SOCKET.unlink(missing_ok=True)
