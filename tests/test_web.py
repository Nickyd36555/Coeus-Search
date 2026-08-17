"""Web UI behaviour, driven through Flask's test client."""

import pytest

pytest.importorskip("flask")

import yaml

from fbmarket.config import load_config
from fbmarket.store import Store
from fbmarket.web import _from_form, create_app


BASE_CONFIG = {
    "database": "test.sqlite3",
    "notify": {"channels": [{"type": "console"}]},
    "searches": [
        {
            "name": "Tacomas",
            "location": "saltlakecity",
            "query": "Toyota Tacoma",
            "max_price": 25000,
            "enabled": True,
            "filters": {"max_price": 25000, "exclude": ["salvage"]},
        }
    ],
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = dict(BASE_CONFIG)
    config["database"] = str(tmp_path / "test.sqlite3")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    app = create_app(str(path))
    app.config["TESTING"] = True
    client = app.test_client()
    client.config_path = str(path)
    return client


def read_config(client):
    return load_config(client.config_path)


def test_index_lists_searches(client):
    page = client.get("/").get_data(as_text=True)
    assert "Tacomas" in page
    assert "Toyota Tacoma" in page


def test_create_a_search_through_the_form(client):
    response = client.post(
        "/search/new",
        data={
            "name": "4Runners",
            "query": "Toyota 4Runner",
            "location": "provo",
            "min_price": "10000",
            "max_price": "30000",
            "min_year": "2015",
            "max_mileage": "120000",
            "exclude": "salvage, rebuilt, parts only",
            "include_any": "4runner",
            "enabled": "on",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200

    config = read_config(client)
    saved = next(s for s in config["searches"] if s["name"] == "4Runners")
    assert saved["query"] == "Toyota 4Runner"
    assert saved["max_price"] == 30000
    assert saved["enabled"] is True
    # Bounds must be mirrored into filters — that is what actually enforces them.
    assert saved["filters"]["max_price"] == 30000
    assert saved["filters"]["min_year"] == 2015
    assert saved["filters"]["exclude"] == ["salvage", "rebuilt", "parts only"]


def test_editing_preserves_other_searches(client):
    client.post(
        "/search/new",
        data={"name": "Second", "location": "provo", "enabled": "on"},
        follow_redirects=True,
    )
    client.post(
        "/search/Tacomas/edit",
        data={"name": "Tacomas", "location": "ogden", "max_price": "20000", "enabled": "on"},
        follow_redirects=True,
    )
    config = read_config(client)
    names = {s["name"] for s in config["searches"]}
    assert names == {"Tacomas", "Second"}
    tacomas = next(s for s in config["searches"] if s["name"] == "Tacomas")
    assert tacomas["location"] == "ogden"
    assert tacomas["max_price"] == 20000


def test_renaming_carries_seen_history_across(client):
    config = read_config(client)
    with Store(config["database"]) as store:
        from fbmarket.models import Listing

        store.record("Tacomas", [Listing(id="1", title="2016 Tacoma")])

    client.post(
        "/search/Tacomas/edit",
        data={"name": "Tacoma hunt", "location": "saltlakecity", "enabled": "on"},
        follow_redirects=True,
    )

    with Store(config["database"]) as store:
        # Without the migration the renamed search would re-alert on every
        # car it had already reported.
        assert store.is_empty("Tacomas")
        assert not store.is_empty("Tacoma hunt")


def test_duplicate_names_are_rejected(client):
    response = client.post(
        "/search/new",
        data={"name": "Tacomas", "location": "provo", "enabled": "on"},
        follow_redirects=True,
    )
    assert "already exists" in response.get_data(as_text=True)
    assert len(read_config(client)["searches"]) == 1


def test_invalid_input_is_reported_not_saved(client):
    response = client.post(
        "/search/new",
        data={"name": "Bad", "location": "provo", "max_price": "twenty grand"},
        follow_redirects=True,
    )
    assert "must be a number" in response.get_data(as_text=True)
    assert len(read_config(client)["searches"]) == 1


def test_search_without_location_or_url_is_rejected(client):
    response = client.post(
        "/search/new", data={"name": "Nowhere"}, follow_redirects=True
    )
    assert "Set a location" in response.get_data(as_text=True)


def test_min_above_max_is_rejected(client):
    response = client.post(
        "/search/new",
        data={"name": "Backwards", "location": "x", "min_price": "30000",
              "max_price": "10000"},
        follow_redirects=True,
    )
    assert "higher than maximum" in response.get_data(as_text=True)


def test_toggle_pauses_a_search(client):
    client.post("/search/Tacomas/toggle", follow_redirects=True)
    assert read_config(client)["searches"][0]["enabled"] is False
    client.post("/search/Tacomas/toggle", follow_redirects=True)
    assert read_config(client)["searches"][0]["enabled"] is True


def test_cannot_delete_the_last_search(client):
    response = client.post("/search/Tacomas/delete", follow_redirects=True)
    assert "Cannot delete the last search" in response.get_data(as_text=True)
    assert len(read_config(client)["searches"]) == 1


def test_delete_removes_search_and_its_history(client):
    client.post(
        "/search/new",
        data={"name": "Second", "location": "provo", "enabled": "on"},
        follow_redirects=True,
    )
    client.post("/search/Tacomas/delete", follow_redirects=True)
    config = read_config(client)
    assert [s["name"] for s in config["searches"]] == ["Second"]


def test_editing_an_unknown_search_404s(client):
    assert client.get("/search/nope/edit").status_code == 404


# -- auth ------------------------------------------------------------------


def test_no_token_means_no_login_required(client):
    assert client.get("/").status_code == 200


def test_token_gates_every_page(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = dict(BASE_CONFIG, database=str(tmp_path / "t.sqlite3"))
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    app = create_app(str(path), token="s3cret")
    app.config["TESTING"] = True
    client = app.test_client()

    assert client.get("/").status_code == 302              # bounced to login
    assert client.post("/search/Tacomas/toggle").status_code == 302

    client.post("/login", data={"token": "wrong"})
    assert client.get("/").status_code == 302

    client.post("/login", data={"token": "s3cret"})
    assert client.get("/").status_code == 200


def test_public_bind_without_token_refuses_to_start(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(BASE_CONFIG), encoding="utf-8")
    from fbmarket.web import run_server

    with pytest.raises(SystemExit, match="--token"):
        run_server(str(path), host="0.0.0.0", port=8765, token=None)


# -- form translation ------------------------------------------------------


def test_form_parsing_strips_currency_formatting():
    spec = _from_form({"name": "x", "location": "y", "max_price": "$25,000"})
    assert spec["max_price"] == 25000


def test_form_parsing_splits_keyword_lists():
    spec = _from_form(
        {"name": "x", "location": "y", "exclude": " salvage , rebuilt ,, parts "}
    )
    assert spec["filters"]["exclude"] == ["salvage", "rebuilt", "parts"]


def test_url_only_search_needs_no_location():
    spec = _from_form({"name": "x", "url": "https://facebook.com/marketplace/x"})
    assert spec["url"].endswith("/marketplace/x")
