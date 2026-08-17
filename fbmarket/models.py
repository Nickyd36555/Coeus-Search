"""Normalized listing model shared by the scraper, filters and notifiers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any

ITEM_URL = "https://www.facebook.com/marketplace/item/{id}/"

# "2016 Toyota Tacoma TRD" -> 2016.  Restricted to plausible model years so we
# don't pick the "4" out of "4Runner" or a price out of the title.
_YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")

# "80K miles", "80,000 miles", "80k mi", "12 000 km"
_MILEAGE_RE = re.compile(
    r"(\d[\d,.\s]*)\s*(k)?\s*(?:miles|mile|mi\b|km\b|kilometers)",
    re.IGNORECASE,
)


@dataclass
class Listing:
    """A single Marketplace listing, normalized across extraction strategies."""

    id: str
    title: str
    search: str = ""
    price: int | None = None           # whole currency units, e.g. 12995
    currency: str = "USD"
    price_text: str = ""               # as Facebook rendered it, e.g. "$12,995"
    location: str = ""
    mileage: int | None = None         # miles (km inputs are converted)
    year: int | None = None
    image_url: str = ""
    subtitles: list[str] = field(default_factory=list)
    url: str = ""
    is_sold: bool = False

    def __post_init__(self) -> None:
        if not self.url:
            self.url = ITEM_URL.format(id=self.id)
        if self.year is None:
            self.year = parse_year(self.title)
        if self.mileage is None:
            self.mileage = parse_mileage(" ".join([*self.subtitles, self.title]))
        if not self.price_text and self.price is not None:
            self.price_text = f"${self.price:,}"

    @property
    def haystack(self) -> str:
        """Everything a keyword filter should be allowed to match against."""
        return " ".join([self.title, *self.subtitles, self.location]).lower()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_text(self) -> str:
        bits = [self.title]
        if self.price_text:
            bits.append(self.price_text)
        details = []
        if self.mileage is not None:
            details.append(f"{self.mileage:,} mi")
        if self.location:
            details.append(self.location)
        if details:
            bits.append(" · ".join(details))
        bits.append(self.url)
        return "\n".join(bits)

    def to_html(self) -> str:
        details = []
        if self.mileage is not None:
            details.append(f"{self.mileage:,} mi")
        if self.location:
            details.append(_esc(self.location))
        subtitle = " &middot; ".join(details)
        img = (
            f'<a href="{_esc(self.url)}">'
            f'<img src="{_esc(self.image_url)}" alt="" '
            f'style="max-width:320px;border-radius:8px;display:block;margin:8px 0"></a>'
            if self.image_url
            else ""
        )
        return (
            '<div style="font-family:system-ui,sans-serif;margin:0 0 24px">'
            f'<a href="{_esc(self.url)}" style="font-size:16px;font-weight:600;'
            f'text-decoration:none;color:#1877f2">{_esc(self.title)}</a>'
            f'<div style="font-size:18px;font-weight:700;margin:4px 0">'
            f"{_esc(self.price_text or 'Price not listed')}</div>"
            f'<div style="color:#606770;font-size:13px">{subtitle}</div>'
            f"{img}"
            "</div>"
        )


def _esc(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def parse_year(text: str) -> int | None:
    """Pull a model year out of a listing title."""
    match = _YEAR_RE.search(text or "")
    return int(match.group(1)) if match else None


def parse_price(value: Any) -> int | None:
    """Parse a price from a number or a rendered string like '$12,995'."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    if text.lower() in {"free", "$0", "0"}:
        return 0
    digits = re.sub(r"[^\d.]", "", text.split("-")[0])
    if not digits:
        return None
    try:
        return int(float(digits))
    except ValueError:
        return None


def parse_mileage(text: str) -> int | None:
    """Parse mileage from strings like '80K miles' or '120,000 km' (-> miles)."""
    match = _MILEAGE_RE.search(text or "")
    if not match:
        return None
    raw = re.sub(r"[,\s]", "", match.group(1)).rstrip(".")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if match.group(2):  # the "K" in "80K miles"
        value *= 1000
    if match.group(0).lower().rstrip().endswith(("km", "kilometers")):
        value *= 0.621371
    return int(round(value))
