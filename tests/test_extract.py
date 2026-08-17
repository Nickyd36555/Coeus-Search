import json

from fbmarket.extract import extract_listings
from fbmarket.models import Listing, parse_mileage, parse_price, parse_year


def _page(payload) -> str:
    return (
        "<html><body><script type=\"application/json\" data-sjs>"
        + json.dumps(payload)
        + "</script></body></html>"
    )


LISTING_NODE = {
    "__typename": "GroupCommerceProductItem",
    "id": "1234567890",
    "marketplace_listing_title": "2016 Toyota Tacoma TRD Off Road",
    "listing_price": {
        "amount": "24500",
        "formatted_amount": "$24,500",
        "currency": "USD",
    },
    "primary_listing_photo": {"image": {"uri": "https://example.com/a.jpg"}},
    "location": {
        "reverse_geocode": {
            "city": "Salt Lake City",
            "state": "UT",
            "city_page": {"display_name": "Salt Lake City, UT"},
        }
    },
    "custom_sub_titles_with_rendering_flags": [{"subtitle": "82K miles"}],
    "is_sold": False,
}


def test_extracts_listing_from_embedded_json():
    html = _page({"require": [["ScheduledServerJS", "handle", None, [{"__bbox": {
        "result": {"data": {"marketplace_search": {"feed_units": {
            "edges": [{"node": {"listing": LISTING_NODE}}]
        }}}}
    }}]]]})

    listings = extract_listings(html, "tacomas")
    assert len(listings) == 1

    listing = listings[0]
    assert listing.id == "1234567890"
    assert listing.title == "2016 Toyota Tacoma TRD Off Road"
    assert listing.price == 24500
    assert listing.price_text == "$24,500"
    assert listing.location == "Salt Lake City, UT"
    assert listing.mileage == 82000
    assert listing.year == 2016
    assert listing.image_url == "https://example.com/a.jpg"
    assert listing.search == "tacomas"
    assert listing.url == "https://www.facebook.com/marketplace/item/1234567890/"


def test_deduplicates_repeated_nodes():
    # Facebook repeats the same listing across several payload sections.
    html = _page([{"a": LISTING_NODE}, {"b": LISTING_NODE}, {"c": [LISTING_NODE]}])
    assert len(extract_listings(html)) == 1


def test_ignores_non_listing_objects():
    html = _page({"id": "999", "name": "Some Page", "nested": {"id": "1", "text": "hi"}})
    assert extract_listings(html) == []


def test_handles_malformed_json_blocks():
    html = (
        '<script type="application/json">{"listing_price": broken</script>'
        + _page({"node": LISTING_NODE})
    )
    assert len(extract_listings(html)) == 1


def test_dom_fallback_when_json_shape_changes():
    html = """
    <div><a href="/marketplace/item/5556667778/?ref=search">
      <span>$9,750</span><span>2011 Honda Civic EX</span>
    </a></div>
    """
    listings = extract_listings(html, "civics")
    assert len(listings) == 1
    assert listings[0].id == "5556667778"
    assert listings[0].price == 9750
    assert listings[0].year == 2011


def test_free_and_missing_prices():
    node = dict(LISTING_NODE, id="1", listing_price={"formatted_amount": "Free"})
    assert extract_listings(_page(node))[0].price == 0

    node = dict(LISTING_NODE, id="2")
    node.pop("listing_price")
    assert extract_listings(_page(node))[0].price is None


def test_parse_price_variants():
    assert parse_price("$12,995") == 12995
    assert parse_price(8000) == 8000
    assert parse_price("8000") == 8000
    assert parse_price("Free") == 0
    assert parse_price("") is None
    assert parse_price(None) is None


def test_parse_mileage_variants():
    assert parse_mileage("82K miles") == 82000
    assert parse_mileage("120,000 miles") == 120000
    assert parse_mileage("45k mi") == 45000
    assert parse_mileage("100,000 km") == 62137  # converted to miles
    assert parse_mileage("no numbers here") is None


def test_parse_year_ignores_model_names():
    assert parse_year("2016 Toyota Tacoma") == 2016
    assert parse_year("Toyota 4Runner SR5") is None
    assert parse_year("1998 Mazda Miata") == 1998


def test_listing_text_and_html_render():
    listing = Listing(id="7", title="2016 Tacoma", price=24500, location="SLC, UT",
                      subtitles=["82K miles"])
    text = listing.to_text()
    assert "2016 Tacoma" in text
    assert "$24,500" in text
    assert "82,000 mi" in text
    assert "marketplace/item/7" in text
    assert "<a href" in listing.to_html()
