"""Turn a Marketplace results page into :class:`Listing` objects.

Facebook ships the search results as JSON inside ``<script type="application/json">``
tags and then hydrates the DOM from them. We read that JSON rather than the
rendered markup: the CSS class names are obfuscated and rotate constantly, but
the data keys (``marketplace_listing_title``, ``listing_price``, ...) have been
stable for years. A DOM scan runs as a fallback when the JSON shape changes.

Everything here is pure text/JSON handling, so it is unit-testable without a
browser or a network connection.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator

from .models import Listing, parse_price

_SCRIPT_RE = re.compile(
    r'<script[^>]*type="application/json"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_ITEM_HREF_RE = re.compile(r'href="(/marketplace/item/(\d+)/?[^"]*)"')
_TAG_RE = re.compile(r"<[^>]+>")

_TITLE_KEYS = ("marketplace_listing_title", "custom_title")
_MAX_DEPTH = 60

# Container keys whose contents are genuine search results.
_RESULTS_CONTAINER_KEYS = (
    "marketplace_search",
    "search_results",
    "feed_units",
    "marketplace_feed_stories",
)
# ...unless the key also says these, which mark recommendation rails.
_EXCLUDE_CONTAINER_KEYS = (
    "related",
    "recommend",
    "suggested",
    "similar",
    "you_may",
    "recently_viewed",
    "saved",
    "sponsored",
)


def extract_listings(html: str, search_name: str = "") -> list[Listing]:
    """Extract every listing on a results page, de-duplicated by listing id."""
    listings: dict[str, Listing] = {}

    fallback: dict[str, Listing] = {}
    for blob in _json_blobs(html):
        for node, in_results in _walk_listing_nodes(blob):
            listing = _node_to_listing(node, search_name)
            if not listing:
                continue
            target = listings if in_results else fallback
            target.setdefault(listing.id, listing)

    if listings:
        # Anything outside the results containers is a recommendation rail,
        # not something the search asked for.
        return list(listings.values())
    listings = fallback

    if not listings:
        for listing in _from_dom(html, search_name):
            listings.setdefault(listing.id, listing)

    return list(listings.values())


def _json_blobs(html: str) -> Iterator[Any]:
    for raw in _SCRIPT_RE.findall(html or ""):
        raw = raw.strip()
        if not raw or not any(key in raw for key in ("marketplace_listing_title", "listing_price")):
            continue
        try:
            yield json.loads(raw)
        except (ValueError, RecursionError):
            continue


def _walk_listing_nodes(
    node: Any, depth: int = 0, in_results: bool = False
) -> Iterator[tuple[dict, bool]]:
    """Yield ``(listing_node, is_in_search_results)`` for every listing found.

    A results page carries more than the results: Facebook also embeds
    "suggested for you" rails and a generic browse feed, all using the same
    listing shape. Collecting everything indiscriminately means a search for a
    car reports bookshelves and hot tubs. The flag records whether a node was
    reached through a search-results container so the caller can prefer those.
    """
    if depth > _MAX_DEPTH:
        return
    if isinstance(node, dict):
        if node.get("id") and any(node.get(key) for key in _TITLE_KEYS):
            yield node, in_results
        for key, value in node.items():
            if isinstance(value, (dict, list)):
                yield from _walk_listing_nodes(
                    value, depth + 1, in_results or _is_results_key(key)
                )
    elif isinstance(node, list):
        for value in node:
            if isinstance(value, (dict, list)):
                yield from _walk_listing_nodes(value, depth + 1, in_results)


def _is_results_key(key: Any) -> bool:
    """True for the container keys that hold actual search results."""
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    if any(marker in lowered for marker in _EXCLUDE_CONTAINER_KEYS):
        return False
    return any(marker in lowered for marker in _RESULTS_CONTAINER_KEYS)


def _node_to_listing(node: dict, search_name: str) -> Listing | None:
    listing_id = str(node.get("id") or "").strip()
    if not listing_id.isdigit():
        return None

    title = ""
    for key in _TITLE_KEYS:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            title = value.strip()
            break
        if isinstance(value, dict) and isinstance(value.get("text"), str):
            title = value["text"].strip()
            break
    if not title:
        return None

    price_node = node.get("listing_price") or {}
    price_text = ""
    price = None
    currency = "USD"
    if isinstance(price_node, dict):
        price_text = str(price_node.get("formatted_amount") or "")
        price = parse_price(price_node.get("amount"))
        if price is None:
            price = parse_price(price_text)
        currency = str(price_node.get("currency") or "USD")
    elif price_node:
        price_text = str(price_node)
        price = parse_price(price_text)

    return Listing(
        id=listing_id,
        title=title,
        search=search_name,
        price=price,
        currency=currency,
        price_text=price_text,
        location=_location(node),
        image_url=_image(node),
        subtitles=_subtitles(node),
        is_sold=bool(node.get("is_sold") or node.get("is_pending")),
    )


def _location(node: dict) -> str:
    location = node.get("location")
    if isinstance(location, dict):
        geo = location.get("reverse_geocode")
        if isinstance(geo, dict):
            page = geo.get("city_page")
            if isinstance(page, dict) and page.get("display_name"):
                return str(page["display_name"])
            city, state = geo.get("city"), geo.get("state")
            if city:
                return f"{city}, {state}" if state else str(city)
    text = node.get("location_text")
    if isinstance(text, dict) and text.get("text"):
        return str(text["text"])
    if isinstance(text, str):
        return text
    return ""


def _image(node: dict) -> str:
    photo = node.get("primary_listing_photo")
    if isinstance(photo, dict):
        for key in ("image", "listing_image", "photo_image_medium"):
            image = photo.get(key)
            if isinstance(image, dict) and image.get("uri"):
                return str(image["uri"])
    photos = node.get("listing_photos")
    if isinstance(photos, list):
        for entry in photos:
            if isinstance(entry, dict):
                image = entry.get("image")
                if isinstance(image, dict) and image.get("uri"):
                    return str(image["uri"])
    return ""


def _subtitles(node: dict) -> list[str]:
    out: list[str] = []
    raw = node.get("custom_sub_titles_with_rendering_flags")
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict) and entry.get("subtitle"):
                out.append(str(entry["subtitle"]))
            elif isinstance(entry, str):
                out.append(entry)
    for key in ("custom_sub_title", "subtitle", "vehicle_odometer_data"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            out.append(value.strip())
        elif isinstance(value, dict):
            for sub_key in ("text", "subtitle", "value"):
                if isinstance(value.get(sub_key), str):
                    out.append(value[sub_key])
                    break
    return out


def _from_dom(html: str, search_name: str) -> list[Listing]:
    """Last resort: pull ids out of anchors and scrape nearby text.

    Yields thinner listings than the JSON path (often title + price only), but
    it keeps the monitor alive if Facebook reshapes its payload.
    """
    out: list[Listing] = []
    seen: set[str] = set()
    for match in _ITEM_HREF_RE.finditer(html or ""):
        listing_id = match.group(2)
        if listing_id in seen:
            continue
        seen.add(listing_id)

        chunk = html[match.end() : match.end() + 1500]
        end = chunk.find("/marketplace/item/")
        if end != -1:
            chunk = chunk[:end]
        text = [t.strip() for t in _TAG_RE.sub("\n", chunk).split("\n") if t.strip()]
        price_text = next((t for t in text if re.match(r"^[\$£€]\s?[\d,]+", t)), "")
        title = next(
            (t for t in text if t != price_text and len(t) > 3 and not t.startswith("$")),
            "",
        )
        if not title:
            continue
        out.append(
            Listing(
                id=listing_id,
                title=title,
                search=search_name,
                price=parse_price(price_text),
                price_text=price_text,
            )
        )
    return out
