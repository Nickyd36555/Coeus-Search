"""The polling loop: scrape -> filter -> dedupe -> notify -> record."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any

from .config import channel_specs, enabled_searches
from .filters import Filter
from .models import Listing
from .notifiers import Dispatcher, build_notifiers
from .scraper import LoginRequired, Scraper
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class CycleResult:
    scraped: int = 0
    matched: int = 0
    new: int = 0
    price_drops: int = 0
    errors: int = 0

    def __str__(self) -> str:
        return (
            f"{self.scraped} scraped, {self.matched} matched filters, "
            f"{self.new} new, {self.price_drops} price drop(s), {self.errors} error(s)"
        )


class Monitor:
    """Runs configured searches once (:meth:`run_once`) or forever (:meth:`watch`)."""

    def __init__(self, config: dict[str, Any], dry_run: bool = False) -> None:
        self.config = config
        self.dry_run = dry_run
        self.store = Store(config.get("database", "fbmarket.sqlite3"))

        specs = channel_specs(config)
        if dry_run:
            specs = [{"type": "console"}]
        self.dispatcher = Dispatcher(build_notifiers(specs))
        if not self.dispatcher:
            log.warning(
                "no notification channels are enabled — matches will only be logged"
            )

    def close(self) -> None:
        self.store.close()

    # -- one cycle ---------------------------------------------------------

    def run_once(self) -> CycleResult:
        result = CycleResult()
        searches = enabled_searches(self.config)
        if not searches:
            log.warning("no enabled searches in config")
            return result

        low, high = _pair(
            self.config.get("poll", {}).get("between_searches_seconds", [6, 16])
        )

        with Scraper(self.config.get("browser", {})) as scraper:
            for index, search in enumerate(searches):
                if index:
                    scraper.pause_between_searches(low, high)
                try:
                    self._run_search(scraper, search, result)
                except LoginRequired as exc:
                    result.errors += 1
                    log.error("[%s] %s", search["name"], exc)
                except Exception as exc:  # noqa: BLE001 - one search must not kill the cycle
                    result.errors += 1
                    log.exception("[%s] failed: %s", search["name"], exc)

        pruned = self.store.prune(int(self.config.get("prune_after_days", 45)))
        if pruned:
            log.debug("pruned %d stale row(s)", pruned)
        log.info("cycle complete: %s", result)
        return result

    def _run_search(
        self, scraper: Scraper, search: dict[str, Any], result: CycleResult
    ) -> None:
        name = search["name"]
        listings = scraper.fetch(search)
        result.scraped += len(listings)

        criteria = Filter(search.get("filters") or search)
        matched: list[Listing] = []
        for listing in listings:
            reason = criteria.reject_reason(listing)
            if reason is None:
                matched.append(listing)
            else:
                log.debug("[%s] skip %s (%s): %s", name, listing.id, reason, listing.title)
        result.matched += len(matched)

        if not matched:
            log.info("[%s] nothing matched this cycle", name)
            return

        first_run = self.store.is_empty(name)
        notify_drops = bool(
            search.get("notify_price_drops", self.config.get("notify_price_drops", True))
        )
        new, drops = self.store.select_new(name, matched, notify_price_drops=notify_drops)
        result.new += len(new)
        result.price_drops += len(drops)

        if first_run and not self.config.get("notify_on_first_run", False):
            # Every current listing is "new" the first time a search runs.
            # Record them quietly so you only get pinged about what appears next.
            self.store.record(name, matched, notified=False)
            log.info(
                "[%s] first run: recorded %d existing listing(s) without notifying "
                "(set notify_on_first_run: true to change this)",
                name,
                len(matched),
            )
            return

        if new:
            self._deliver(name, new, subject=f"New on Marketplace — {name}")
        for listing, old_price in drops:
            note = f"Price drop: was ${old_price:,}, now {listing.price_text}"
            self._deliver(name, [listing], subject=f"Price drop — {name}", note=note)

        # Refresh last_seen / price for everything we saw, notified or not.
        already = {l.id for l in new}
        rest = [l for l in matched if l.id not in already]
        if rest:
            self.store.record(name, rest, notified=False)

        if not new and not drops:
            log.info("[%s] %d match(es), nothing new", name, len(matched))

    def _deliver(
        self, name: str, listings: list[Listing], subject: str, note: str = ""
    ) -> None:
        cap = int(self.config.get("max_notifications_per_cycle", 15))
        batch, overflow = listings, []
        if cap > 0 and len(listings) > cap:
            batch, overflow = listings[:cap], listings[cap:]
            log.warning(
                "[%s] %d new listings exceeds max_notifications_per_cycle=%d; "
                "notifying the first %d and recording the rest silently",
                name,
                len(listings),
                cap,
                cap,
            )

        for listing in batch:
            log.info("[%s] NEW %s %s %s", name, listing.price_text, listing.title, listing.url)

        delivered = self.dispatcher.send(subject, batch, note) if self.dispatcher else False
        if not self.dispatcher:
            delivered = True  # nothing to deliver to; don't retry forever

        # Only mark as done if a channel accepted it, so a transient outage
        # re-notifies next cycle instead of losing the listing.
        if delivered:
            self.store.record(name, batch, notified=True)
        else:
            log.error("[%s] no channel accepted the batch; will retry next cycle", name)
        if overflow:
            self.store.record(name, overflow, notified=False)

    # -- forever -----------------------------------------------------------

    def watch(self) -> None:
        poll = self.config.get("poll", {})
        interval = int(poll.get("interval_seconds", 600))
        jitter = int(poll.get("jitter_seconds", 120))
        log.info(
            "watching %d search(es) every ~%ds — Ctrl+C to stop",
            len(enabled_searches(self.config)),
            interval,
        )
        while True:
            started = time.time()
            try:
                self.run_once()
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the watcher alive
                log.exception("cycle failed: %s", exc)
            # Jitter keeps the request pattern from looking metronomic.
            delay = max(30.0, interval + random.uniform(-jitter, jitter))
            elapsed = time.time() - started
            sleep_for = max(15.0, delay - elapsed)
            log.info("next check in %.0fs", sleep_for)
            time.sleep(sleep_for)


def _pair(value: Any) -> tuple[float, float]:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return float(value[0]), float(value[1])
    number = float(value or 0)
    return number, number
