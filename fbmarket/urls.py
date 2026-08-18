"""Build Facebook Marketplace search URLs from config."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode, urlparse

BASE = "https://www.facebook.com/marketplace"

# config key -> Marketplace query parameter
PARAM_MAP = {
    "query": "query",
    "min_price": "minPrice",
    "max_price": "maxPrice",
    "min_year": "minYear",
    "max_year": "maxYear",
    "min_mileage": "minMileage",
    "max_mileage": "maxMileage",
    "transmission": "transmissionType",
    "vehicle_condition": "vehicleCondition",
    "days_since_listed": "daysSinceListed",
    "radius_km": "radius",
    "exact": "exact",
    "sort_by": "sortBy",
    "delivery_method": "deliveryMethod",
}

# Multi-select filters: config may give a string or a list of strings.
LIST_PARAMS = {
    "car_type": "carType",
    "make": "make",
    "model": "model",
}

# Newest-first. Without this you get Facebook's relevance ranking, which buries
# brand new listings and makes "tell me the moment one appears" unreliable.
DEFAULT_SORT = "creation_time_descend"


def build_search_urls(search: dict[str, Any]) -> list[str]:
    """Return every URL this search covers — one per configured location.

    Marketplace has no nationwide search for local goods: results are always
    anchored to a city plus a radius. Covering a wider area therefore means
    querying several cities and merging the results, which is what a list of
    ``locations`` does. Deduplication happens under the single search name, so
    a car listed in the overlap between two cities is still reported once.
    """
    locations = search.get("locations")
    if not locations:
        return [build_search_url(search)]
    if isinstance(locations, str):
        locations = [p.strip() for p in locations.split(",") if p.strip()]

    urls: list[str] = []
    for location in locations:
        variant = {k: v for k, v in search.items() if k != "locations"}
        variant["location"] = location
        urls.append(build_search_url(variant))
    return urls


def build_search_url(search: dict[str, Any]) -> str:
    """Return the Marketplace URL for one configured search.

    A literal ``url:`` in the config always wins — paste a URL straight from
    your browser when you want a filter this builder does not model.
    """
    explicit = search.get("url")
    if explicit:
        url = validate_explicit_url(str(explicit))
        return _force_sort(url, search.get("sort_by", DEFAULT_SORT))

    location = str(search.get("location", "") or "").strip("/")
    if not location:
        first = search.get("locations")
        if isinstance(first, str):
            first = [p.strip() for p in first.split(",") if p.strip()]
        if first:
            variant = {k: v for k, v in search.items() if k != "locations"}
            variant["location"] = first[0]
            return build_search_url(variant)
        raise ValueError(
            f"search {search.get('name', '<unnamed>')!r} needs either 'url' or 'location'"
        )

    category = str(search.get("category", "vehicles") or "vehicles").strip("/")

    params: list[tuple[str, str]] = []
    for key, param in PARAM_MAP.items():
        value = search.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            value = "true" if value else "false"
        params.append((param, str(value)))

    for key, param in LIST_PARAMS.items():
        value = search.get(key)
        if not value:
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        params.extend((param, str(item)) for item in values if item)

    for param, value in (search.get("extra_params") or {}).items():
        params.append((str(param), str(value)))

    if not any(param == "sortBy" for param, _ in params):
        params.append(("sortBy", DEFAULT_SORT))

    return f"{BASE}/{location}/{category}?{urlencode(params)}"


def _force_sort(url: str, sort_by: str) -> str:
    """Ensure a hand-pasted URL still sorts newest-first."""
    if not sort_by or "sortBy=" in url:
        return url
    joiner = "&" if "?" in url else "?"
    return f"{url}{joiner}sortBy={sort_by}"


def validate_explicit_url(url: str) -> str:
    """Reject URLs that are not actually a Marketplace search.

    The Marketplace home page (facebook.com/marketplace/) is a valid URL and
    loads fine, but it returns a generic "near you" feed of whatever is listed
    locally — sofas, phones, houses — rather than search results. Silently
    monitoring it produces a stream of alerts that ignore every filter, so it
    is caught here instead.
    """
    url = url.strip()
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https") or "facebook.com" not in parsed.netloc:
        raise ValueError(
            f"{url!r} is not a Facebook URL. Paste the address bar contents from "
            "a Marketplace search, or clear the field and use the location and "
            "query boxes instead."
        )

    parts = [p for p in parsed.path.split("/") if p]
    if not parts or parts[0] != "marketplace":
        raise ValueError(
            f"{url!r} is not a Marketplace URL — it should start with "
            "facebook.com/marketplace/"
        )

    if len(parts) < 2:
        raise ValueError(
            "that is the Marketplace home page, not a search — it returns "
            "whatever is listed near you, ignoring every filter. Browse to your "
            "search on Facebook first, then copy the address bar, which should "
            "look like facebook.com/marketplace/portland/vehicles?query=camaro "
            "— or clear the URL box and fill in the location and car instead."
        )

    return url
