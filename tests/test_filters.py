from fbmarket.filters import Filter
from fbmarket.models import Listing


def car(**kwargs) -> Listing:
    base = dict(
        id="1",
        title="2016 Toyota Tacoma TRD Off Road",
        price=24500,
        location="Salt Lake City, UT",
        subtitles=["82K miles"],
    )
    base.update(kwargs)
    return Listing(**base)


def test_passes_when_everything_matches():
    criteria = Filter(
        {"min_price": 8000, "max_price": 25000, "min_year": 2014,
         "max_mileage": 150000, "include_any": ["tacoma"], "exclude": ["salvage"]}
    )
    assert criteria.reject_reason(car()) is None


def test_price_bounds():
    assert Filter({"max_price": 20000}).reject_reason(car()) == "price 24500 > 20000"
    assert Filter({"min_price": 30000}).reject_reason(car()) == "price 24500 < 30000"
    assert Filter({"min_price": 20000, "max_price": 30000}).reject_reason(car()) is None


def test_year_and_mileage_bounds():
    assert "year" in Filter({"min_year": 2018}).reject_reason(car())
    assert "mileage" in Filter({"max_mileage": 50000}).reject_reason(car())
    assert Filter({"max_mileage": 90000}).reject_reason(car()) is None


def test_missing_mileage_does_not_reject():
    # Facebook often omits mileage in the grid; don't drop a good car over it.
    listing = car(subtitles=[])
    assert Filter({"max_mileage": 100000}).reject_reason(listing) is None


def test_missing_price_rejects_only_when_price_is_constrained():
    listing = car(price=None, price_text="")
    assert Filter({"max_price": 25000}).reject_reason(listing) == "no price"
    assert Filter({"include_any": ["tacoma"]}).reject_reason(listing) is None


def test_exclude_keywords_are_case_insensitive():
    listing = car(title="2016 Tacoma SALVAGE title")
    assert Filter({"exclude": ["salvage"]}).reject_reason(listing) == "excluded keyword: salvage"


def test_exclude_matches_subtitles_and_location():
    listing = car(subtitles=["82K miles", "Rebuilt title"])
    assert "rebuilt" in Filter({"exclude": ["rebuilt"]}).reject_reason(listing)


def test_include_all_requires_every_keyword():
    criteria = Filter({"include_all": ["tacoma", "trd"]})
    assert criteria.reject_reason(car()) is None
    assert "missing keyword" in criteria.reject_reason(car(title="2016 Toyota Tacoma SR5"))


def test_include_any_needs_one_hit():
    criteria = Filter({"include_any": ["tacoma", "frontier"]})
    assert criteria.reject_reason(car()) is None
    assert criteria.reject_reason(car(title="2016 Ford Ranger")) == (
        "missing all include_any keywords"
    )


def test_sold_listings_are_skipped_by_default():
    assert Filter({}).reject_reason(car(is_sold=True)) == "sold"
    assert Filter({"skip_sold": False}).reject_reason(car(is_sold=True)) is None


def test_title_regex():
    criteria = Filter({"title_regex": r"tacoma|tundra"})
    assert criteria.reject_reason(car()) is None
    assert criteria.reject_reason(car(title="2016 Honda Civic")) == "title_regex did not match"


def test_empty_filter_accepts_everything():
    assert Filter({}).matches(car())
    assert Filter(None).matches(car())
