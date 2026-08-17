"""Load, validate and normalize config.yaml."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from .notifiers.base import resolve_env
from .urls import build_search_url

DEFAULT_PATHS = (
    "config.yaml",
    "config.yml",
    "~/.config/fbmarket/config.yaml",
)

DEFAULTS: dict[str, Any] = {
    "poll": {
        "interval_seconds": 600,
        "jitter_seconds": 120,
        "between_searches_seconds": [6, 16],
    },
    "browser": {
        "headless": True,
        "storage_state": "~/.config/fbmarket/state.json",
        "timeout_ms": 45000,
        "locale": "en-US",
        "scrolls": 3,
        "scroll_pause_ms": 1500,
        "settle_ms": 4000,
        "block_media": True,
    },
    "database": "fbmarket.sqlite3",
    "prune_after_days": 45,
    "notify_on_first_run": False,
    "notify_price_drops": True,
    "max_notifications_per_cycle": 15,
    "notify": {"channels": []},
    "searches": [],
}


class ConfigError(Exception):
    """Config is missing or malformed."""


def find_config(path: str | None = None) -> Path:
    if path:
        resolved = Path(path).expanduser()
        if not resolved.exists():
            raise ConfigError(f"config file not found: {resolved}")
        return resolved
    env_path = os.environ.get("FBMARKET_CONFIG")
    if env_path:
        return find_config(env_path)
    for candidate in DEFAULT_PATHS:
        resolved = Path(candidate).expanduser()
        if resolved.exists():
            return resolved
    raise ConfigError(
        "no config found. Copy config.example.yaml to config.yaml and edit it, "
        "or pass --config /path/to/config.yaml"
    )


def load_dotenv(path: str | Path = ".env") -> int:
    """Load ``KEY=value`` pairs from a .env file into the environment.

    Keeps secrets out of config.yaml without adding a dependency. Existing
    environment variables win, so a real export always overrides the file.
    """
    env_path = Path(path).expanduser()
    if not env_path.exists():
        return 0
    loaded = 0
    # utf-8-sig strips the byte-order mark Notepad and PowerShell's Out-File
    # write. Without it the first key parses as "﻿KEY" and never resolves.
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def load_config(path: str | None = None) -> dict[str, Any]:
    config_path = find_config(path)
    # Look for .env beside the config first, then in the working directory.
    load_dotenv(config_path.parent / ".env")
    load_dotenv(".env")
    # utf-8-sig so a Notepad-saved config.yaml (which carries a BOM) parses.
    with open(config_path, "r", encoding="utf-8-sig") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path} must contain a YAML mapping at the top level")

    config = _merge(DEFAULTS, raw)
    config["_path"] = str(config_path)
    validate(config)
    return config


def save_config(config: dict[str, Any], path: str | Path | None = None) -> Path:
    """Write the config back to disk (used by the web UI).

    Note: PyYAML does not preserve comments, so the explanatory comments in
    config.example.yaml are lost once the UI saves. A timestamped backup of the
    previous file is kept beside it.
    """
    target = Path(path or config.get("_path") or "config.yaml").expanduser()
    payload = {k: v for k, v in config.items() if not k.startswith("_")}
    validate(payload)

    if target.exists():
        backup = target.with_suffix(target.suffix + f".bak")
        backup.write_text(target.read_text(encoding="utf-8-sig"), encoding="utf-8")

    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        yaml.safe_dump(payload, handle, sort_keys=False, allow_unicode=True)
    tmp.replace(target)  # atomic, so a crash can't truncate your config
    return target


def validate(config: dict[str, Any]) -> None:
    searches = config.get("searches") or []
    if not isinstance(searches, list) or not searches:
        raise ConfigError("config needs at least one entry under 'searches'")

    names: set[str] = set()
    for index, search in enumerate(searches):
        if not isinstance(search, dict):
            raise ConfigError(f"searches[{index}] must be a mapping")
        name = str(search.get("name") or f"search-{index + 1}")
        if name in names:
            raise ConfigError(
                f"duplicate search name {name!r} — names key the dedupe database, "
                "so they must be unique"
            )
        names.add(name)
        search["name"] = name
        if search.get("enabled") is None:
            search["enabled"] = True
        try:
            build_search_url(search)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc

    channels = (config.get("notify") or {}).get("channels") or []
    if not isinstance(channels, list):
        raise ConfigError("notify.channels must be a list")
    for index, channel in enumerate(channels):
        if not isinstance(channel, dict) or not channel.get("type"):
            raise ConfigError(f"notify.channels[{index}] needs a 'type'")
        if not channel.get("enabled", True):
            continue
        # ntfy topics are public to anyone who guesses them — refuse to ship
        # alerts to the example placeholder.
        if channel.get("type") == "ntfy" and "CHANGE-THIS" in str(channel.get("topic", "")):
            raise ConfigError(
                "the ntfy topic is still the example placeholder. Pick your own "
                "unguessable topic name — anyone who knows it can read your alerts."
            )


def enabled_searches(config: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in config.get("searches", []) if s.get("enabled", True)]


def channel_specs(config: dict[str, Any]) -> list[dict[str, Any]]:
    return list((config.get("notify") or {}).get("channels") or [])


def search_setting(config: dict[str, Any], search: dict[str, Any], key: str) -> Any:
    """Per-search override, falling back to the global value."""
    value = search.get(key)
    return config.get(key) if value is None else value


def resolve_secrets(config: dict[str, Any]) -> dict[str, Any]:
    """Expand ``${ENV_VAR}`` references throughout the config."""
    return resolve_env(config)


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out
