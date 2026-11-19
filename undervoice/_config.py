"""Settings the window remembers between runs, kept in config.toml."""

from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

Settings = dict[str, dict[str, Any]]

DEFAULTS: Settings = {
    "updater": {"enabled": True, "channel": "stable", "check_interval_h": 24, "last_check": 0},
}


def folder() -> Path:
    """Where config.toml lives: next to the exe for a portable copy (a file named `portable`
    beside it), otherwise the per-user config folder."""
    if getattr(sys, "frozen", False):
        here = Path(sys.executable).parent
        if (here / "portable").exists():
            return here
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "undervoice"


def defaults() -> Settings:
    return {section: dict(values) for section, values in DEFAULTS.items()}


def load(path: Path | None = None) -> Settings:
    """Saved settings over the defaults. Raises tomllib.TOMLDecodeError for a broken file."""
    path = path or folder() / "config.toml"
    merged = defaults()
    try:
        with open(path, "rb") as f:
            saved = tomllib.load(f)
    except FileNotFoundError:
        return merged
    for section, values in saved.items():
        if isinstance(values, dict):
            merged.setdefault(section, {}).update(values)
    return merged


def _value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return repr(v)
    # TOML basic strings use the same escapes JSON does for everything json.dumps emits
    return json.dumps(str(v))


def save(settings: Settings, path: Path | None = None) -> None:
    path = path or folder() / "config.toml"
    lines = []
    for section, values in settings.items():
        lines.append(f"[{section}]")
        lines += [f"{key} = {_value(v)}" for key, v in values.items()]
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    # write then rename, so a crash mid-write never leaves half a config
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(path)
