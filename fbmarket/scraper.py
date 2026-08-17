"""Drive a real browser over Marketplace search pages."""

from __future__ import annotations

import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Any

from .extract import extract_listings
from .models import Listing
from .urls import build_search_url

log = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

LOGIN_MARKERS = (
    "You must log in to continue",
    "Log in to Facebook",
    "log_in_to_continue",
)


class LoginRequired(RuntimeError):
    """Facebook served a login wall instead of results."""


class Scraper:
    """Opens one browser and reuses it across every search in a cycle."""

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        settings = settings or {}
        self.headless = bool(settings.get("headless", True))
        self.timeout_ms = int(settings.get("timeout_ms", 45000))
        self.locale = str(settings.get("locale", "en-US"))
        self.user_agent = str(settings.get("user_agent") or DEFAULT_UA)
        self.scrolls = int(settings.get("scrolls", 3))
        self.scroll_pause_ms = int(settings.get("scroll_pause_ms", 1500))
        self.settle_ms = int(settings.get("settle_ms", 4000))
        self.block_media = bool(settings.get("block_media", True))
        # Point at an existing Chrome/Chromium if you'd rather not have
        # Playwright download its own build.
        executable = settings.get("executable_path")
        self.executable_path = str(Path(str(executable)).expanduser()) if executable else None
        # Explicit proxy wins; otherwise honour the usual environment vars so the
        # browser goes out the same way the rest of the machine does.
        self.proxy = settings.get("proxy") or os.environ.get("HTTPS_PROXY") or os.environ.get(
            "https_proxy"
        )
        state = settings.get("storage_state") or "~/.config/fbmarket/state.json"
        self.storage_state = Path(str(state)).expanduser()
        debug_dir = settings.get("debug_dump_dir")
        self.debug_dir = Path(str(debug_dir)).expanduser() if debug_dir else None

        self._pw = None
        self._browser = None
        self._context = None

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "Scraper":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def start(self, headless: bool | None = None) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        launch_args: dict[str, Any] = {
            "headless": self.headless if headless is None else headless,
            "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        }
        if self.executable_path:
            launch_args["executable_path"] = self.executable_path
        if self.proxy:
            launch_args["proxy"] = (
                {"server": self.proxy} if isinstance(self.proxy, str) else dict(self.proxy)
            )
        self._browser = self._pw.chromium.launch(**launch_args)
        context_args: dict[str, Any] = {
            "user_agent": self.user_agent,
            "locale": self.locale,
            "viewport": {"width": 1400, "height": 1000},
        }
        if self.storage_state.exists():
            context_args["storage_state"] = str(self.storage_state)
            log.debug("using saved session %s", self.storage_state)
        self._context = self._browser.new_context(**context_args)
        self._context.set_default_timeout(self.timeout_ms)
        # Hide the most obvious automation tell.
        self._context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        if self.block_media:
            self._context.route(
                "**/*",
                lambda route: (
                    route.abort()
                    if route.request.resource_type in {"image", "media", "font"}
                    else route.continue_()
                ),
            )

    def stop(self) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer:
                    closer.close()
            except Exception:  # noqa: BLE001 - teardown must not mask real errors
                pass
        if self._pw:
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
        self._pw = self._browser = self._context = None

    def save_session(self) -> None:
        if self._context:
            self.storage_state.parent.mkdir(parents=True, exist_ok=True)
            self._context.storage_state(path=str(self.storage_state))
            log.info("saved session to %s", self.storage_state)

    # -- scraping ----------------------------------------------------------

    def fetch(self, search: dict[str, Any]) -> list[Listing]:
        """Load one search and return every listing on the results page."""
        if not self._context:
            raise RuntimeError("Scraper.start() must be called first")

        url = build_search_url(search)
        name = str(search.get("name") or search.get("query") or "search")
        log.info("[%s] %s", name, url)

        page = self._context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            self._dismiss_cookie_banner(page)
            page.wait_for_timeout(self.settle_ms)

            for _ in range(max(0, self.scrolls)):
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(self.scroll_pause_ms)

            html = page.content()
        finally:
            page.close()

        listings = extract_listings(html, name)
        if not listings:
            if any(marker in html for marker in LOGIN_MARKERS):
                raise LoginRequired(
                    "Facebook returned a login wall. Run `fbmarket login` once to "
                    "save a session, then try again."
                )
            dump = self._dump_html(name, html)
            if dump:
                log.warning(
                    "[%s] no listings found; saved the page to %s "
                    "(inspect it with `fbmarket parse %s`)",
                    name,
                    dump,
                    dump,
                )
        log.info("[%s] %d listing(s) on page", name, len(listings))
        return listings

    def _dump_html(self, name: str, html: str) -> Path | None:
        """Save a page that yielded nothing, so failures are diagnosable."""
        if not self.debug_dir:
            return None
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-") or "search"
            path = self.debug_dir / f"{safe}-{int(time.time())}.html"
            path.write_text(html, encoding="utf-8")
            return path
        except OSError as exc:
            log.warning("could not write debug HTML: %s", exc)
            return None

    def _dismiss_cookie_banner(self, page: Any) -> None:
        """Best-effort: the consent dialog covers results in some regions."""
        for selector in (
            '[aria-label="Allow all cookies"]',
            '[aria-label="Decline optional cookies"]',
            'div[role="button"]:has-text("Allow all cookies")',
        ):
            try:
                button = page.locator(selector).first
                if button.is_visible(timeout=1200):
                    button.click(timeout=2500)
                    page.wait_for_timeout(800)
                    return
            except Exception:  # noqa: BLE001 - banner is optional
                continue

    def pause_between_searches(self, low: float = 6.0, high: float = 16.0) -> None:
        """Randomized gap so a cycle doesn't look like a burst of requests."""
        time.sleep(random.uniform(low, high))


def interactive_login(settings: dict[str, Any] | None = None) -> Path:
    """Open a visible browser, wait for a manual login, and save the session."""
    scraper = Scraper({**(settings or {}), "block_media": False})
    scraper.start(headless=False)
    try:
        page = scraper._context.new_page()  # noqa: SLF001 - internal helper
        page.goto("https://www.facebook.com/login", wait_until="domcontentloaded")
        print(
            "\nA browser window is open.\n"
            "  1. Log in to Facebook (complete any 2FA).\n"
            "  2. Browse to Marketplace once to confirm you're through.\n"
            "  3. Come back here and press Enter.\n"
        )
        input("Press Enter once you are logged in... ")
        scraper.save_session()
        return scraper.storage_state
    finally:
        scraper.stop()
