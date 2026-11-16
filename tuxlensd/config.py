"""Paths, persistent settings and the pairing token.

Nothing machine-specific lives in the repository: everything is created on first run under the
XDG directories of the user running the daemon.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP = "tuxlens"
REPO_ROOT = Path(__file__).resolve().parent.parent


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / APP
STATE_DIR = _xdg("XDG_STATE_HOME", ".local/state") / APP
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/{APP}-{os.getuid()}") / APP

CONTROL_SOCKET = RUNTIME_DIR / "control.sock"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
TOKEN_FILE = CONFIG_DIR / "token"
CERT_DIR = CONFIG_DIR / "certs"
USER_THEMES_DIR = CONFIG_DIR / "themes"

DATA_DIRS = [REPO_ROOT, Path("/usr/share") / APP]


def data_path(*parts: str) -> Path:
    """Locate a bundled file both from a git checkout and from a system install."""
    for base in DATA_DIRS:
        candidate = base.joinpath(*parts)
        if candidate.exists():
            return candidate
    return DATA_DIRS[0].joinpath(*parts)


@dataclass
class Settings:
    # Network
    https_port: int = 7790
    setup_port: int = 7791
    # Virtual camera
    device: str = "/dev/video10"
    width: int = 1280
    height: int = 720
    fps: int = 30
    # Privacy: master switch, and only open the real camera while an app is using TuxLens
    camera_enabled: bool = True
    on_demand: bool = True
    # Source: "phone" or a local V4L2 camera id (/dev/v4l/by-id/… or /dev/videoN)
    source: str = "phone"
    # Phone camera
    camera: str = "back"          # front | back | wide | tele
    zoom: float = 1.0
    torch: bool = False
    mic: bool = False             # phone microphone as the "TuxLens" virtual source
    mic_gain_db: float = 0.0      # applied on the PC (−12 … +18 dB)
    mic_mute: bool = False
    mic_echo: bool = True         # iOS processing: echo cancellation / noise suppression / auto gain
    mic_noise: bool = True
    mic_agc: bool = True
    capture: str = "auto"         # auto | 720p | 1080p | 4k (auto = 1080p upright, output size otherwise)
    bitrate: int = 6_000_000
    codec: str = "baseline"       # baseline | main | high
    keyframe_interval_s: float = 3.0
    # Crop to 16:9 on the phone before encoding. Off by default: Safari 27.2 ignores visibleRect
    # and the OffscreenCanvas copy costs ~15 ms per frame on the phone (measured), more than the
    # PC saves by decoding fewer pixels.
    phone_crop: bool = False
    rotate: int = 0               # 0/90/180/270, extra rotation on top of the automatic one
    mirror: bool = False
    # Framing
    framing: bool = False         # auto-framing (follows the person, like Center Stage)
    framing_zoom: float = 1.6     # how far it may zoom in to follow
    framing_speed: float = 0.4    # 0 calm … 1 quick
    digital_zoom: float = 1.0     # fixed crop zoom when framing is off
    # Effects
    effect: str = "none"          # none | blur | image
    blur_strength: float = 0.6
    background_image: str = ""
    filter: str = "none"          # none | mono | warm | cool | vivid
    brightness: float = 0.0
    contrast: float = 0.0
    # Look
    theme: str = "niebla"
    waybar_signal: int = 0        # if set, `pkill -RTMIN+N waybar` on state changes (module "signal": N)
    language: str = "auto"        # auto (follow the locale) | es | en …: panel and "camera paused" frame
    extra: dict = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Settings":
        try:
            raw = json.loads(SETTINGS_FILE.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            raw = {}
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False))
        tmp.replace(SETTINGS_FILE)

    def update(self, changes: dict) -> dict:
        """Apply known keys with type coercion; returns what actually changed."""
        changed = {}
        for f in fields(self):
            if f.name not in changes:
                continue
            current = getattr(self, f.name)
            value = changes[f.name]
            try:
                if isinstance(current, bool):
                    value = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "on", "yes")
                elif isinstance(current, int):
                    value = int(value)
                elif isinstance(current, float):
                    value = float(value)
                elif isinstance(current, str):
                    value = str(value)
            except (TypeError, ValueError):
                continue
            if value != current:
                setattr(self, f.name, value)
                changed[f.name] = value
        return changed


_token_lock = threading.Lock()


def pairing_token(regenerate: bool = False) -> str:
    with _token_lock:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if not regenerate and TOKEN_FILE.exists():
            token = TOKEN_FILE.read_text().strip()
            if token:
                return token
        token = secrets.token_urlsafe(24)
        TOKEN_FILE.write_text(token + "\n")
        TOKEN_FILE.chmod(0o600)
        return token
