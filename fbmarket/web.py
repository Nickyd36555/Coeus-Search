"""Local web UI: define searches in a form, test them, watch results arrive.

Runs on 127.0.0.1 by default. If you bind it to a public interface you must set
an access token (``--token``), otherwise anyone who finds the port can read your
searches and drive the scraper.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from typing import Any

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from .config import channel_specs, load_config, save_config
from .filters import Filter
from .models import Listing
from .monitor import Monitor
from .notifiers import Dispatcher, build_notifiers
from .scraper import LoginRequired, Scraper
from .store import Store
from .urls import build_search_url

log = logging.getLogger(__name__)

# Fields the form collects, and how to coerce them out of the POST body.
INT_FIELDS = ("min_price", "max_price", "min_year", "max_year", "max_mileage", "radius_km")
TEXT_FIELDS = ("name", "location", "query", "transmission", "days_since_listed", "url")
LIST_FIELDS = ("include_any", "include_all", "exclude")


class MonitorThread:
    """Runs the polling loop in the background so the UI stays responsive."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last_result: str = ""
        self.last_run: float | None = None
        self.last_error: str = ""

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, config: dict[str, Any]) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, args=(config,), daemon=True, name="fbmarket-monitor"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self, config: dict[str, Any]) -> None:
        interval = int(config.get("poll", {}).get("interval_seconds", 600))
        monitor = Monitor(config)
        try:
            while not self._stop.is_set():
                try:
                    result = monitor.run_once()
                    self.last_result = str(result)
                    self.last_error = ""
                except Exception as exc:  # noqa: BLE001 - keep the thread alive
                    self.last_error = str(exc)
                    log.exception("background cycle failed")
                self.last_run = time.time()
                # Wake promptly on stop instead of sleeping the whole interval.
                self._stop.wait(interval)
        finally:
            monitor.close()


MONITOR = MonitorThread()


def create_app(config_path: str | None = None, token: str | None = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = secrets.token_hex(16)
    app.config["CONFIG_PATH"] = config_path
    app.config["ACCESS_TOKEN"] = token

    def current_config() -> dict[str, Any]:
        return load_config(app.config["CONFIG_PATH"])

    def store_for(config: dict[str, Any]) -> Store:
        return Store(config.get("database", "fbmarket.sqlite3"))

    # -- auth ------------------------------------------------------------

    @app.before_request
    def require_token():
        expected = app.config["ACCESS_TOKEN"]
        if not expected or request.endpoint == "login" or session.get("authed"):
            return None
        return redirect(url_for("login", next=request.path))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        expected = app.config["ACCESS_TOKEN"]
        if not expected:
            return redirect(url_for("index"))
        if request.method == "POST":
            supplied = request.form.get("token", "")
            # Constant-time compare so the token can't be guessed by timing.
            if secrets.compare_digest(supplied, expected):
                session["authed"] = True
                return redirect(request.args.get("next") or url_for("index"))
            flash("Incorrect token.", "error")
        return render_template("login.html")

    # -- pages -----------------------------------------------------------

    @app.route("/")
    def index():
        config = current_config()
        with store_for(config) as store:
            recent = store.recent(limit=40)
            counts = {row["search"]: row["total"] for row in store.stats()}
        channels = [c for c in channel_specs(config) if c.get("enabled", True)]
        # Surfacing the resolved URL makes a misconfigured search obvious: a
        # bare /marketplace/ URL returns the generic near-you feed, which looks
        # like the scraper is broken rather than the search being wrong.
        urls = {}
        for s in config.get("searches", []):
            try:
                urls[s.get("name")] = build_search_url(s)
            except ValueError as exc:
                urls[s.get("name")] = f"INVALID — {exc}"
        return render_template(
            "index.html",
            urls=urls,
            searches=config.get("searches", []),
            counts=counts,
            recent=recent,
            channels=channels,
            monitor=MONITOR,
            interval=config.get("poll", {}).get("interval_seconds", 600),
            config_path=config.get("_path"),
        )

    @app.route("/search/new", methods=["GET", "POST"])
    def new_search():
        if request.method == "POST":
            return _save(None)
        return render_template("edit.html", search={}, is_new=True)

    @app.route("/search/<name>/edit", methods=["GET", "POST"])
    def edit_search(name: str):
        config = current_config()
        search = _find(config, name)
        if search is None:
            abort(404)
        if request.method == "POST":
            return _save(name)
        return render_template("edit.html", search=_to_form(search), is_new=False)

    def _save(original_name: str | None):
        config = current_config()
        try:
            spec = _from_form(request.form)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(request.path)

        searches = config.setdefault("searches", [])
        existing = _find(config, original_name) if original_name else None

        clash = _find(config, spec["name"])
        if clash is not None and clash is not existing:
            flash(f"A search named {spec['name']!r} already exists.", "error")
            return redirect(request.path)

        if existing is None:
            searches.append(spec)
        else:
            # Renaming changes the dedupe key, so carry the history across.
            if original_name and original_name != spec["name"]:
                with store_for(config) as store:
                    store.conn.execute(
                        "UPDATE seen SET search = ? WHERE search = ?",
                        (spec["name"], original_name),
                    )
                    store.conn.commit()
            existing.clear()
            existing.update(spec)

        try:
            save_config(config)
        except Exception as exc:  # noqa: BLE001 - surface bad config in the UI
            flash(f"Could not save: {exc}", "error")
            return redirect(request.path)
        flash(f"Saved {spec['name']!r}.", "ok")
        return redirect(url_for("index"))

    @app.route("/search/<name>/toggle", methods=["POST"])
    def toggle_search(name: str):
        config = current_config()
        search = _find(config, name)
        if search is None:
            abort(404)
        search["enabled"] = not search.get("enabled", True)
        save_config(config)
        return redirect(url_for("index"))

    @app.route("/search/<name>/delete", methods=["POST"])
    def delete_search(name: str):
        config = current_config()
        config["searches"] = [
            s for s in config.get("searches", []) if s.get("name") != name
        ]
        if not config["searches"]:
            flash("Cannot delete the last search — add another one first.", "error")
            return redirect(url_for("index"))
        save_config(config)
        with store_for(config) as store:
            store.delete_search(name)
        flash(f"Deleted {name!r}.", "ok")
        return redirect(url_for("index"))

    @app.route("/search/<name>/reset", methods=["POST"])
    def reset_search(name: str):
        config = current_config()
        with store_for(config) as store:
            removed = store.delete_search(name)
        flash(f"Forgot {removed} listing(s) for {name!r}; they'll alert again.", "ok")
        return redirect(url_for("index"))

    @app.route("/search/<name>/test")
    def test_search(name: str):
        """Scrape once and show what passes, and why anything else was rejected."""
        config = current_config()
        search = _find(config, name)
        if search is None:
            abort(404)

        rows: list[dict[str, Any]] = []
        error = ""
        try:
            with Scraper(config.get("browser", {})) as scraper:
                listings = scraper.fetch(search)
            criteria = Filter(search.get("filters") or search)
            for listing in listings:
                reason = criteria.reject_reason(listing)
                rows.append({"listing": listing, "reason": reason})
            rows.sort(key=lambda r: r["reason"] is not None)
        except LoginRequired as exc:
            error = f"{exc}"
        except Exception as exc:  # noqa: BLE001 - report scrape failures in the UI
            error = f"{type(exc).__name__}: {exc}"

        return render_template(
            "test.html",
            search=search,
            rows=rows,
            error=error,
            url=build_search_url(search),
            passed=sum(1 for r in rows if r["reason"] is None),
        )

    @app.route("/monitor/<action>", methods=["POST"])
    def monitor_control(action: str):
        config = current_config()
        if action == "start":
            MONITOR.start(config)
            flash("Monitoring started.", "ok")
        elif action == "stop":
            MONITOR.stop()
            flash("Monitoring will stop after the current cycle.", "ok")
        elif action == "once":
            monitor = Monitor(config)
            try:
                flash(f"Ran once: {monitor.run_once()}", "ok")
            finally:
                monitor.close()
        else:
            abort(404)
        return redirect(url_for("index"))

    @app.route("/notify/test", methods=["POST"])
    def notify_test():
        config = current_config()
        dispatcher = Dispatcher(build_notifiers(channel_specs(config)))
        if not dispatcher:
            flash("No notification channels are enabled.", "error")
            return redirect(url_for("index"))
        sample = Listing(
            id="0",
            title="2018 Toyota Tacoma TRD Off Road (test listing)",
            price=24500,
            price_text="$24,500",
            location="Salt Lake City, UT",
            subtitles=["68K miles"],
            url="https://www.facebook.com/marketplace/",
        )
        ok = dispatcher.send("fbmarket test", [sample], "Alerts are working.")
        flash("Test alert sent." if ok else "Every channel failed.", "ok" if ok else "error")
        return redirect(url_for("index"))

    return app


# -- form <-> config translation -----------------------------------------


def _find(config: dict[str, Any], name: str | None) -> dict[str, Any] | None:
    if not name:
        return None
    return next((s for s in config.get("searches", []) if s.get("name") == name), None)


def _to_form(search: dict[str, Any]) -> dict[str, Any]:
    """Flatten a config entry into the flat shape the form expects."""
    form = dict(search)
    filters = search.get("filters") or {}
    for key in LIST_FIELDS:
        value = filters.get(key) or []
        form[key] = ", ".join(value) if isinstance(value, (list, tuple)) else str(value)
    for key in ("min_price", "max_price", "min_year", "max_year", "max_mileage"):
        form.setdefault(key, filters.get(key))
    return form


def _from_form(form: Any) -> dict[str, Any]:
    """Build a config entry from the POSTed form, validating as we go."""
    spec: dict[str, Any] = {}

    name = (form.get("name") or "").strip()
    if not name:
        raise ValueError("Give the search a name.")
    spec["name"] = name

    for key in TEXT_FIELDS:
        if key == "name":
            continue
        value = (form.get(key) or "").strip()
        if value:
            spec[key] = value

    for key in INT_FIELDS:
        raw = (form.get(key) or "").strip().replace(",", "").replace("$", "")
        if not raw:
            continue
        try:
            spec[key] = int(float(raw))
        except ValueError:
            raise ValueError(f"{key.replace('_', ' ')} must be a number (got {raw!r}).")

    if not spec.get("url") and not spec.get("location"):
        raise ValueError("Set a location (or paste a full Marketplace URL).")

    if spec.get("min_price") and spec.get("max_price"):
        if spec["min_price"] > spec["max_price"]:
            raise ValueError("Minimum price is higher than maximum price.")
    if spec.get("min_year") and spec.get("max_year"):
        if spec["min_year"] > spec["max_year"]:
            raise ValueError("Earliest year is later than latest year.")

    spec["category"] = (form.get("category") or "vehicles").strip()
    spec["enabled"] = form.get("enabled") == "on"

    # Mirror the server-side bounds into local filters, which is what actually
    # enforces them — Marketplace returns results that ignore its own filters.
    filters: dict[str, Any] = {}
    for key in ("min_price", "max_price", "min_year", "max_year", "max_mileage"):
        if spec.get(key) is not None:
            filters[key] = spec[key]
    for key in LIST_FIELDS:
        words = [w.strip() for w in (form.get(key) or "").split(",") if w.strip()]
        if words:
            filters[key] = words
    filters["skip_sold"] = True
    spec["filters"] = filters

    return spec


def run_server(
    config_path: str | None,
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str | None = None,
) -> None:
    app = create_app(config_path, token)
    if host not in {"127.0.0.1", "localhost"} and not token:
        raise SystemExit(
            "Refusing to listen on a public interface without --token.\n"
            "Anyone who reached the port could read your searches and drive the "
            "scraper. Re-run with --token YOUR_SECRET."
        )
    print(f"\n  fbmarket UI  ->  http://{host}:{port}\n")
    app.run(host=host, port=port, threaded=True)
