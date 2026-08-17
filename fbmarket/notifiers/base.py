"""Notifier interface and the shared message-building helpers."""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from ..models import Listing

log = logging.getLogger(__name__)

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class Notifier:
    """One delivery channel. Subclasses implement :meth:`send`."""

    type_name = "base"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = {k: resolve_env(v) for k, v in config.items()}
        self.enabled = bool(self.config.get("enabled", True))

    def send(self, subject: str, listings: list[Listing], note: str = "") -> None:
        raise NotImplementedError

    def _require(self, *keys: str) -> list[Any]:
        missing = [k for k in keys if not self.config.get(k)]
        if missing:
            raise ValueError(
                f"{self.type_name} notifier is missing config: {', '.join(missing)}"
            )
        return [self.config[k] for k in keys]


def resolve_env(value: Any) -> Any:
    """Expand ``${VAR}`` in config strings so secrets stay out of the file."""
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [resolve_env(v) for v in value]
    if isinstance(value, dict):
        return {k: resolve_env(v) for k, v in value.items()}
    return value


def plain_body(listings: list[Listing], note: str = "") -> str:
    parts = [note] if note else []
    parts.extend(listing.to_text() for listing in listings)
    return "\n\n".join(parts)


def html_body(subject: str, listings: list[Listing], note: str = "") -> str:
    cards = "".join(listing.to_html() for listing in listings)
    intro = f"<p>{note}</p>" if note else ""
    return (
        '<div style="font-family:system-ui,sans-serif">'
        f"<h2 style='font-size:18px'>{subject}</h2>{intro}{cards}"
        "</div>"
    )
