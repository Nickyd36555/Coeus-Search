"""Build Facebook Marketplace search URLs from config."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

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


def build_search_url(search: dict[str, Any]) -> str:
    """Return the Marketplace URL for one configured search.

    A literal ``url:`` in the config always wins — paste a URL straight from
    your browser when you want a filter this builder does not model.
    """
    explicit = search.get("url")
    if explicit:
        return _force_sort(str(explicit), search.get("sort_by", DEFAULT_SORT))

    location = str(search.get("location", "") or "").strip("/")
    if not location:
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
