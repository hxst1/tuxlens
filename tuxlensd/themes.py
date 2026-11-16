"""Theme loading.

A theme is a small JSON file with design tokens (colors, glass, radius, fonts). The same tokens
feed every surface: the GTK control panel, the phone web page, the waybar icons and the
"waiting for phone" frame the virtual camera shows. Users drop their own themes in
~/.config/tuxlens/themes/<name>.json (they win over bundled ones with the same name) and can add
~/.config/tuxlens/extra.css for panel tweaks that tokens can't express.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from .config import CONFIG_DIR, USER_THEMES_DIR, data_path

DEFAULT_THEME = "niebla"

# Fallback for any token a theme omits, so partial user themes still work.
BASE_TOKENS: dict = {
    "name": "Base",
    "dark": True,
    "colors": {
        "background": "#141516",
        "surface": "#1a1b1d",
        "surface_alt": "#26282a",
        "text": "#e4e6e7",
        "text_secondary": "#c8ccd0",
        "muted": "#8a8f93",
        "accent": "#8b98a6",
        "on_accent": "#141516",
        "danger": "#b8656a",
        "success": "#a9b9a5",
    },
    "glass": {"rgb": "20, 21, 22", "alpha": 0.55, "blur": 20, "border": "rgba(255, 255, 255, 0.09)"},
    "radius": 18,
    "font": "Inter",
    "mono_font": "JetBrainsMono Nerd Font",
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def theme_dirs() -> list[Path]:
    return [USER_THEMES_DIR, data_path("themes")]


def available() -> list[str]:
    names: set[str] = set()
    for d in theme_dirs():
        if d.is_dir():
            names.update(p.stem for p in d.glob("*.json"))
    return sorted(names)


def load(name: str | None) -> dict:
    name = name or DEFAULT_THEME
    for d in theme_dirs():
        path = d / f"{name}.json"
        if path.is_file():
            try:
                tokens = json.loads(path.read_text())
            except json.JSONDecodeError:
                break
            theme = _merge(BASE_TOKENS, tokens)
            theme["id"] = name
            return theme
    if name != DEFAULT_THEME:
        return load(DEFAULT_THEME)
    theme = copy.deepcopy(BASE_TOKENS)
    theme["id"] = DEFAULT_THEME
    return theme


def css_variables(theme: dict) -> str:
    """CSS custom properties for the phone page (served as /theme.css)."""
    c = theme["colors"]
    g = theme["glass"]
    lines = [f"  --{k.replace('_', '-')}: {v};" for k, v in c.items()]
    lines += [
        f"  --glass: rgba({g['rgb']}, {g['alpha']});",
        f"  --glass-blur: {g['blur']}px;",
        f"  --glass-border: {g['border']};",
        f"  --radius: {theme['radius']}px;",
        f"  --font: '{theme['font']}', -apple-system, system-ui, sans-serif;",
        f"  --mono: '{theme['mono_font']}', ui-monospace, monospace;",
        f"  color-scheme: {'dark' if theme.get('dark', True) else 'light'};",
    ]
    return ":root {\n" + "\n".join(lines) + "\n}\n"


def extra_css() -> str:
    path = CONFIG_DIR / "extra.css"
    try:
        return path.read_text()
    except OSError:
        return ""


def hex_to_rgb(color: str) -> tuple[float, float, float]:
    color = color.lstrip("#")
    if len(color) == 3:
        color = "".join(ch * 2 for ch in color)
    return tuple(int(color[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]
