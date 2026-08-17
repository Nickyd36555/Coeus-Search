"""Command line interface."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from . import __version__
from .config import ConfigError, channel_specs, enabled_searches, load_config
from .models import Listing
from .monitor import Monitor
from .notifiers import Dispatcher, build_notifiers
from .scraper import interactive_login
from .store import Store
from .urls import build_search_url


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fbmarket",
        description="Monitor Facebook Marketplace searches and get pinged on new listings.",
    )
    parser.add_argument("--config", "-c", help="path to config.yaml")
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version=f"fbmarket {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    once = sub.add_parser("once", help="run every search a single time")
    once.add_argument(
        "--dry-run",
        action="store_true",
        help="print matches to the console instead of notifying (and still record them)",
    )

    watch = sub.add_parser("watch", help="poll forever on the configured interval")
    watch.add_argument("--interval", type=int, help="override interval_seconds")

    sub.add_parser("login", help="open a browser to log in once and save the session")
    sub.add_parser("check", help="validate the config and print the search URLs")
    sub.add_parser("test-notify", help="send a sample listing through every channel")
    sub.add_parser("status", help="show what the database has recorded per search")

    reset = sub.add_parser(
        "reset", help="forget seen listings so the next run treats them as new"
    )
    reset.add_argument("--search", help="only reset this search (default: all)")

    web = sub.add_parser("web", help="open the browser UI to manage searches")
    web.add_argument("--host", default="127.0.0.1", help="bind address")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument(
        "--token",
        help="require this token to access the UI (mandatory for non-local hosts)",
    )

    parse = sub.add_parser(
        "parse",
        help="run the extractor over a saved HTML page (offline debugging)",
    )
    parse.add_argument("path", help="HTML file saved from a Marketplace results page")
    parse.add_argument("--search", help="apply this search's filters to the results")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    try:
        return _dispatch(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nstopped.")
        return 130


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "login":
        config = _try_load(args.config)
        path = interactive_login((config or {}).get("browser", {}))
        print(f"session saved to {path}")
        return 0

    if args.command == "parse":
        return _cmd_parse(_try_load(args.config), args.path, args.search)

    if args.command == "web":
        try:
            from .web import run_server
        except ImportError as exc:
            print(f"the web UI needs Flask: pip install 'fbmarket[web]'  ({exc})",
                  file=sys.stderr)
            return 2
        load_config(args.config)  # fail fast on a broken config
        run_server(args.config, host=args.host, port=args.port, token=args.token)
        return 0

    config = load_config(args.config)

    if args.command == "check":
        return _cmd_check(config)
    if args.command == "test-notify":
        return _cmd_test_notify(config)
    if args.command == "status":
        return _cmd_status(config)
    if args.command == "reset":
        return _cmd_reset(config, args.search)

    if args.command == "watch" and args.interval:
        config.setdefault("poll", {})["interval_seconds"] = args.interval

    monitor = Monitor(config, dry_run=getattr(args, "dry_run", False))
    try:
        if args.command == "watch":
            monitor.watch()
            return 0
        result = monitor.run_once()
        return 1 if result.errors and not result.new else 0
    finally:
        monitor.close()


def _cmd_check(config: dict) -> int:
    searches = config.get("searches", [])
    active = enabled_searches(config)
    print(f"config: {config.get('_path')}")
    print(f"database: {config.get('database')}")
    print(f"searches: {len(active)} enabled / {len(searches)} total\n")
    for search in searches:
        flag = " " if search.get("enabled", True) else "-"
        print(f"[{flag}] {search['name']}\n    {build_search_url(search)}")
    channels = channel_specs(config)
    enabled = [c for c in channels if c.get("enabled", True)]
    print(f"\nnotification channels: {', '.join(c['type'] for c in enabled) or 'NONE'}")
    if not enabled:
        print("  warning: nothing is configured to ping you.")
    return 0


def _cmd_test_notify(config: dict) -> int:
    dispatcher = Dispatcher(build_notifiers(channel_specs(config)))
    if not dispatcher:
        print("no enabled notification channels to test.", file=sys.stderr)
        return 1
    sample = Listing(
        id="0",
        title="2018 Toyota Tacoma TRD Off Road (test listing)",
        search="test",
        price=24500,
        price_text="$24,500",
        location="Salt Lake City, UT",
        subtitles=["68K miles"],
        url="https://www.facebook.com/marketplace/",
    )
    ok = dispatcher.send("fbmarket test", [sample], "If you can read this, alerts work.")
    print("sent." if ok else "every channel failed — see the errors above.")
    return 0 if ok else 1


def _cmd_parse(config: dict | None, path: str, search_name: str | None) -> int:
    """Extract listings from a saved page — proves whether scraping or
    filtering is at fault without touching the network."""
    from pathlib import Path

    from .extract import extract_listings
    from .filters import Filter

    html = Path(path).expanduser().read_text(encoding="utf-8", errors="replace")
    listings = extract_listings(html, search_name or "parse")
    print(f"extracted {len(listings)} listing(s) from {path}")
    if not listings:
        print(
            "\nNothing matched the known payload shapes. Facebook may have changed "
            "its data format, or the page was a login/consent wall."
        )
        return 1

    criteria = None
    if search_name and config:
        spec = next(
            (s for s in config.get("searches", []) if s.get("name") == search_name), None
        )
        if spec is None:
            print(f"no search named {search_name!r} in the config", file=sys.stderr)
            return 2
        criteria = Filter(spec.get("filters") or spec)

    for listing in listings:
        verdict = ""
        if criteria:
            reason = criteria.reject_reason(listing)
            verdict = "  [PASS]" if reason is None else f"  [skip: {reason}]"
        print(f"\n{listing.to_text()}{verdict}")
    return 0


def _cmd_status(config: dict) -> int:
    with Store(config.get("database", "fbmarket.sqlite3")) as store:
        rows = store.stats()
        if not rows:
            print("nothing recorded yet — run `fbmarket once`.")
            return 0
        print(f"{'search':<32} {'seen':>6}  last seen")
        for row in rows:
            when = datetime.fromtimestamp(row["last_seen"], timezone.utc).astimezone()
            print(f"{row['search']:<32} {row['total']:>6}  {when:%Y-%m-%d %H:%M}")
    return 0


def _cmd_reset(config: dict, search: str | None) -> int:
    with Store(config.get("database", "fbmarket.sqlite3")) as store:
        if search:
            cur = store.conn.execute("DELETE FROM seen WHERE search = ?", (search,))
        else:
            cur = store.conn.execute("DELETE FROM seen")
        store.conn.commit()
        print(f"cleared {cur.rowcount} row(s){f' for {search!r}' if search else ''}.")
    return 0


def _try_load(path: str | None) -> dict | None:
    try:
        return load_config(path)
    except ConfigError:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
