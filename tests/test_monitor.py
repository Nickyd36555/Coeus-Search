"""End-to-end cycle behaviour with a fake scraper — no browser, no network."""

import pytest

from fbmarket import monitor as monitor_module
from fbmarket.models import Listing
from fbmarket.monitor import Monitor


class FakeScraper:
    """Stands in for the Playwright scraper; returns queued pages of results."""

    pages: list[list[Listing]] = []

    def __init__(self, settings=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def pause_between_searches(self, low=0, high=0):
        pass

    def fetch(self, search):
        return FakeScraper.pages.pop(0) if FakeScraper.pages else []


class RecordingNotifier:
    type_name = "recording"

    def __init__(self, config=None):
        self.enabled = True
        self.batches: list[tuple[str, list[Listing]]] = []
        self.fail = False

    def send(self, subject, listings, note=""):
        if self.fail:
            raise RuntimeError("channel down")
        self.batches.append((subject, list(listings)))

    @property
    def notified_ids(self):
        return [l.id for _, batch in self.batches for l in batch]


def car(listing_id: str, price: int = 20000, title: str = "2016 Toyota Tacoma") -> Listing:
    return Listing(id=listing_id, title=title, price=price, subtitles=["80K miles"])


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor_module, "Scraper", FakeScraper)
    FakeScraper.pages = []

    def build(**overrides):
        config = {
            "database": str(tmp_path / "db.sqlite3"),
            "poll": {"between_searches_seconds": [0, 0]},
            "browser": {},
            "notify": {"channels": []},
            "searches": [
                {
                    "name": "tacomas",
                    "location": "slc",
                    "query": "tacoma",
                    "enabled": True,
                    "filters": {"max_price": 25000, "exclude": ["salvage"]},
                }
            ],
        }
        config.update(overrides)
        mon = Monitor(config)
        notifier = RecordingNotifier()
        mon.dispatcher.notifiers = [notifier]
        return mon, notifier

    return build


def test_first_run_records_silently(setup):
    FakeScraper.pages = [[car("1"), car("2")]]
    mon, notifier = setup()

    result = mon.run_once()

    assert notifier.batches == []          # no startup spam
    assert result.matched == 2
    assert not mon.store.is_empty("tacomas")
    mon.close()


def test_first_run_notifies_when_opted_in(setup):
    FakeScraper.pages = [[car("1")]]
    mon, notifier = setup(notify_on_first_run=True)

    mon.run_once()

    assert notifier.notified_ids == ["1"]
    mon.close()


def test_only_new_listings_are_notified(setup):
    FakeScraper.pages = [[car("1"), car("2")]]
    mon, notifier = setup()
    mon.run_once()                                    # first run: silent baseline

    FakeScraper.pages = [[car("3"), car("1"), car("2")]]
    result = mon.run_once()

    assert notifier.notified_ids == ["3"]
    assert result.new == 1
    assert "New on Marketplace" in notifier.batches[0][0]
    mon.close()


def test_repeat_listings_never_re_notify(setup):
    FakeScraper.pages = [[car("1")]]
    mon, notifier = setup()
    mon.run_once()

    for _ in range(3):
        FakeScraper.pages = [[car("1")]]
        mon.run_once()

    assert notifier.batches == []
    mon.close()


def test_filters_apply_before_notifying(setup):
    FakeScraper.pages = [[car("1")]]
    mon, notifier = setup()
    mon.run_once()

    FakeScraper.pages = [[
        car("2", price=99000),                            # over max_price
        car("3", title="2016 Tacoma salvage title"),      # excluded keyword
        car("4"),                                         # keeper
    ]]
    result = mon.run_once()

    assert notifier.notified_ids == ["4"]
    assert result.scraped == 3 and result.matched == 1
    mon.close()


def test_price_drop_notification(setup):
    FakeScraper.pages = [[car("1", price=24000)]]
    mon, notifier = setup(notify_price_drops=True)
    mon.run_once()

    FakeScraper.pages = [[car("1", price=21000)]]
    result = mon.run_once()

    assert result.price_drops == 1
    subject, listings = notifier.batches[0]
    assert "Price drop" in subject
    assert listings[0].id == "1"
    mon.close()


def test_failed_delivery_retries_next_cycle(setup):
    FakeScraper.pages = [[car("1")]]
    mon, notifier = setup()
    mon.run_once()                                    # baseline

    notifier.fail = True
    FakeScraper.pages = [[car("2")]]
    mon.run_once()
    assert notifier.batches == []                     # nothing got through

    notifier.fail = False
    FakeScraper.pages = [[car("2")]]
    mon.run_once()
    assert notifier.notified_ids == ["2"]             # redelivered, not lost
    mon.close()


def test_notification_cap_limits_a_burst(setup):
    FakeScraper.pages = [[car("0")]]
    mon, notifier = setup(max_notifications_per_cycle=2)
    mon.run_once()

    FakeScraper.pages = [[car(str(i)) for i in range(1, 11)]]
    mon.run_once()

    assert len(notifier.notified_ids) == 2
    # The rest are recorded, so the next cycle stays quiet instead of catching up.
    FakeScraper.pages = [[car(str(i)) for i in range(1, 11)]]
    mon.run_once()
    assert len(notifier.notified_ids) == 2
    mon.close()


def test_scraper_failure_does_not_kill_the_cycle(setup, monkeypatch):
    mon, notifier = setup()

    def boom(self, search):
        raise RuntimeError("navigation timeout")

    monkeypatch.setattr(FakeScraper, "fetch", boom)
    result = mon.run_once()

    assert result.errors == 1
    assert result.new == 0
    mon.close()
