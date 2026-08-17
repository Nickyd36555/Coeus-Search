"""Notification channels."""

from __future__ import annotations

import logging
from typing import Any

from ..models import Listing
from .base import Notifier
from .channels import REGISTRY

log = logging.getLogger(__name__)

__all__ = ["Notifier", "REGISTRY", "build_notifiers", "Dispatcher"]


def build_notifiers(specs: list[dict[str, Any]] | None) -> list[Notifier]:
    """Instantiate every enabled channel from the ``notify.channels`` config."""
    notifiers: list[Notifier] = []
    for spec in specs or []:
        type_name = str(spec.get("type", "")).lower().strip()
        if not type_name:
            raise ValueError("each notify channel needs a 'type'")
        if type_name not in REGISTRY:
            raise ValueError(
                f"unknown notifier type {type_name!r}; "
                f"available: {', '.join(sorted(REGISTRY))}"
            )
        notifier = REGISTRY[type_name]({k: v for k, v in spec.items() if k != "type"})
        if notifier.enabled:
            notifiers.append(notifier)
        else:
            log.debug("notifier %s is disabled", type_name)
    return notifiers


class Dispatcher:
    """Fans one batch out to every channel, isolating channel failures."""

    def __init__(self, notifiers: list[Notifier]) -> None:
        self.notifiers = notifiers

    def __bool__(self) -> bool:
        return bool(self.notifiers)

    def send(self, subject: str, listings: list[Listing], note: str = "") -> bool:
        """Return True if at least one channel accepted the batch.

        A dead channel must not stop the others, and must not let the run
        record the listing as delivered when nothing went out.
        """
        if not listings:
            return True
        delivered = False
        for notifier in self.notifiers:
            try:
                notifier.send(subject, listings, note)
                delivered = True
                log.info("notified %d via %s", len(listings), notifier.type_name)
            except Exception as exc:  # noqa: BLE001 - one bad channel isn't fatal
                log.error("notifier %s failed: %s", notifier.type_name, exc)
        return delivered
