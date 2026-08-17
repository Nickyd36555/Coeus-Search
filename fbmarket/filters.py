"""Client-side filtering.

Facebook's own filters are coarse and it happily returns listings that ignore
them, so every search re-checks its constraints locally. This is also where you
express things Marketplace has no filter for at all, like "no salvage titles".
"""

from __future__ import annotations

import re
from typing import Any

from .models import Listing


class Filter:
    """Post-scrape constraints for one search."""

    def __init__(self, spec: dict[str, Any] | None = None) -> None:
        spec = spec or {}
        self.min_price = _as_int(spec.get("min_price"))
        self.max_price = _as_int(spec.get("max_price"))
        self.min_year = _as_int(spec.get("min_year"))
        self.max_year = _as_int(spec.get("max_year"))
        self.min_mileage = _as_int(spec.get("min_mileage"))
        self.max_mileage = _as_int(spec.get("max_mileage"))
        self.require_price = bool(spec.get("require_price", False))
        self.skip_sold = bool(spec.get("skip_sold", True))

        # Any one of these must appear (empty = no constraint).
        self.include_any = _as_list(spec.get("include_any") or spec.get("keywords"))
        # All of these must appear.
        self.include_all = _as_list(spec.get("include_all"))
        # None of these may appear.
        self.exclude = _as_list(spec.get("exclude"))

        pattern = spec.get("title_regex")
        self.title_regex = re.compile(pattern, re.IGNORECASE) if pattern else None

    def reject_reason(self, listing: Listing) -> str | None:
        """Return why a listing fails, or None if it passes."""
        if self.skip_sold and listing.is_sold:
            return "sold"

        price = listing.price
        if price is None:
            if self.require_price or self.min_price is not None or self.max_price is not None:
                return "no price"
        else:
            if self.min_price is not None and price < self.min_price:
                return f"price {price} < {self.min_price}"
            if self.max_price is not None and price > self.max_price:
                return f"price {price} > {self.max_price}"

        year = listing.year
        if year is None:
            if self.min_year is not None or self.max_year is not None:
                return "no year"
        else:
            if self.min_year is not None and year < self.min_year:
                return f"year {year} < {self.min_year}"
            if self.max_year is not None and year > self.max_year:
                return f"year {year} > {self.max_year}"

        mileage = listing.mileage
        if mileage is None:
            # Mileage is often absent from the search grid. Don't drop an
            # otherwise-matching car over a field Facebook simply didn't render.
            pass
        else:
            if self.min_mileage is not None and mileage < self.min_mileage:
                return f"mileage {mileage} < {self.min_mileage}"
            if self.max_mileage is not None and mileage > self.max_mileage:
                return f"mileage {mileage} > {self.max_mileage}"

        text = listing.haystack
        if self.include_any and not any(word in text for word in self.include_any):
            return "missing all include_any keywords"
        missing = [word for word in self.include_all if word not in text]
        if missing:
            return f"missing keyword(s): {', '.join(missing)}"
        hit = next((word for word in self.exclude if word in text), None)
        if hit:
            return f"excluded keyword: {hit}"

        if self.title_regex and not self.title_regex.search(listing.title):
            return "title_regex did not match"

        return None

    def matches(self, listing: Listing) -> bool:
        return self.reject_reason(listing) is None


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _as_list(value: Any) -> list[str]:
    if not value:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    return [str(item).strip().lower() for item in items if str(item).strip()]
