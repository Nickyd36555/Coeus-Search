from urllib.parse import parse_qs, urlparse

import pytest

from fbmarket.config import ConfigError, validate
from fbmarket.models import Listing
from fbmarket.store import Store
from fbmarket.urls import build_search_url


def listing(listing_id: str, price: int = 20000) -> Listing:
    return Listing(id=listing_id, title=f"2016 Tacoma {listing_id}", price=price)


# -- URL building ----------------------------------------------------------


def test_builds_vehicle_search_url():
    url = build_search_url(
        {
            "name": "t",
            "location": "saltlakecity",
            "query": "Toyota Tacoma",
            "min_price": 8000,
            "max_price": 25000,
            "min_year": 2014,
            "max_mileage": 150000,
            "radius_km": 100,
            "transmission": "automatic",
        }
    )
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    assert parsed.path == "/marketplace/saltlakecity/vehicles"
    assert params["query"] == ["Toyota Tacoma"]
    assert params["minPrice"] == ["8000"]
    assert params["maxPrice"] == ["25000"]
    assert params["minYear"] == ["2014"]
    assert params["maxMileage"] == ["150000"]
    assert params["radius"] == ["100"]
    assert params["transmissionType"] == ["automatic"]
    # Newest-first is what makes "ping me when it appears" work.
    assert params["sortBy"] == ["creation_time_descend"]


def test_list_params_repeat():
    url = build_search_url(
        {"location": "slc", "make": ["toyota", "nissan"], "car_type": "car-truck"}
    )
    params = parse_qs(urlparse(url).query)
    assert params["make"] == ["toyota", "nissan"]
    assert params["carType"] == ["car-truck"]


def test_explicit_url_wins_and_still_sorts_newest():
    url = build_search_url(
        {"url": "https://www.facebook.com/marketplace/slc/search?query=miata"}
    )
    assert url.startswith("https://www.facebook.com/marketplace/slc/search?query=miata")
    assert "sortBy=creation_time_descend" in url


def test_explicit_url_keeps_its_own_sort():
    url = build_search_url(
        {"url": "https://www.facebook.com/marketplace/slc/search?sortBy=price_ascend"}
    )
    assert url.count("sortBy") == 1
    assert "price_ascend" in url


def test_search_without_location_or_url_is_an_error():
    with pytest.raises(ValueError):
        build_search_url({"name": "broken"})


# -- config validation -----------------------------------------------------


def test_duplicate_search_names_rejected():
    config = {"searches": [{"name": "a", "location": "x"}, {"name": "a", "location": "y"}]}
    with pytest.raises(ConfigError, match="duplicate search name"):
        validate(config)


def test_config_needs_a_search():
    with pytest.raises(ConfigError):
        validate({"searches": []})


def test_channel_needs_a_type():
    config = {"searches": [{"name": "a", "location": "x"}], "notify": {"channels": [{}]}}
    with pytest.raises(ConfigError, match="needs a 'type'"):
        validate(config)


# -- dedupe store ----------------------------------------------------------


def test_new_listings_reported_once(tmp_path):
    with Store(tmp_path / "db.sqlite3") as store:
        batch = [listing("1"), listing("2")]

        new, drops = store.select_new("cars", batch)
        assert {l.id for l in new} == {"1", "2"}
        assert drops == []

        store.record("cars", new)

        # Same listings on the next cycle -> nothing new.
        new, drops = store.select_new("cars", batch)
        assert new == [] and drops == []

        # A third listing appears.
        new, _ = store.select_new("cars", [*batch, listing("3")])
        assert [l.id for l in new] == ["3"]


def test_searches_have_independent_history(tmp_path):
    with Store(tmp_path / "db.sqlite3") as store:
        store.record("tacomas", [listing("1")])
        new, _ = store.select_new("4runners", [listing("1")])
        assert [l.id for l in new] == ["1"]


def test_price_drops_detected_only_when_enabled(tmp_path):
    with Store(tmp_path / "db.sqlite3") as store:
        store.record("cars", [listing("1", price=25000)])

        _, drops = store.select_new("cars", [listing("1", price=22000)])
        assert drops == []  # disabled by default

        _, drops = store.select_new(
            "cars", [listing("1", price=22000)], notify_price_drops=True
        )
        assert len(drops) == 1
        assert drops[0][1] == 25000
        assert drops[0][0].price == 22000

        # A price increase is not a drop.
        _, drops = store.select_new(
            "cars", [listing("1", price=27000)], notify_price_drops=True
        )
        assert drops == []


def test_is_empty_tracks_first_run(tmp_path):
    with Store(tmp_path / "db.sqlite3") as store:
        assert store.is_empty("cars")
        store.record("cars", [listing("1")], notified=False)
        assert not store.is_empty("cars")


def test_notified_flag_is_sticky(tmp_path):
    with Store(tmp_path / "db.sqlite3") as store:
        store.record("cars", [listing("1")], notified=True)
        store.record("cars", [listing("1")], notified=False)
        assert store.get("cars", "1")["notified"] == 1


def test_prune_drops_stale_rows(tmp_path):
    with Store(tmp_path / "db.sqlite3") as store:
        store.record("cars", [listing("1")])
        store.conn.execute("UPDATE seen SET last_seen = 0")
        store.conn.commit()
        assert store.prune(30) == 1
        assert store.is_empty("cars")


def test_marketplace_home_page_url_is_rejected():
    """facebook.com/marketplace/ loads fine but returns a generic near-you
    feed — sofas, phones, houses — ignoring every filter."""
    for url in (
        "https://www.facebook.com/marketplace/",
        "https://www.facebook.com/marketplace",
        "https://facebook.com/marketplace/?sortBy=creation_time_descend",
    ):
        with pytest.raises(ValueError, match="home page"):
            build_search_url({"name": "bad", "url": url})


def test_non_marketplace_urls_are_rejected():
    with pytest.raises(ValueError, match="not a Facebook URL"):
        build_search_url({"name": "bad", "url": "https://example.com/x"})
    with pytest.raises(ValueError, match="not a Marketplace URL"):
        build_search_url({"name": "bad", "url": "https://www.facebook.com/groups/1"})


def test_real_search_urls_are_accepted():
    for url in (
        "https://www.facebook.com/marketplace/portland/vehicles?query=camaro",
        "https://www.facebook.com/marketplace/search?query=miata",
        "https://www.facebook.com/marketplace/category/vehicles",
        "https://www.facebook.com/marketplace/109470862423276/vehicles",
    ):
        assert build_search_url({"name": "ok", "url": url}).startswith(url)
