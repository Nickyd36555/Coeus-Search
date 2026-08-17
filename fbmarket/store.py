"""SQLite record of everything we've already seen, so you're pinged once."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .models import Listing

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    search      TEXT NOT NULL,
    listing_id  TEXT NOT NULL,
    title       TEXT,
    price       INTEGER,
    url         TEXT,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    notified    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (search, listing_id)
);
CREATE INDEX IF NOT EXISTS idx_seen_first ON seen (first_seen);
"""


class Store:
    """Dedupe state keyed by (search name, listing id)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def is_empty(self, search: str) -> bool:
        """True when this search has never recorded a listing (first run)."""
        row = self.conn.execute(
            "SELECT 1 FROM seen WHERE search = ? LIMIT 1", (search,)
        ).fetchone()
        return row is None

    def get(self, search: str, listing_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM seen WHERE search = ? AND listing_id = ?",
            (search, listing_id),
        ).fetchone()

    def select_new(
        self, search: str, listings: list[Listing], notify_price_drops: bool = False
    ) -> tuple[list[Listing], list[tuple[Listing, int]]]:
        """Split scraped listings into brand-new ones and price drops.

        Returns ``(new_listings, [(listing, old_price), ...])``. Nothing is
        written here — call :meth:`record` once you've actually notified, so a
        crash mid-notification doesn't silently swallow a listing.
        """
        new: list[Listing] = []
        drops: list[tuple[Listing, int]] = []
        for listing in listings:
            row = self.get(search, listing.id)
            if row is None:
                new.append(listing)
            elif (
                notify_price_drops
                and listing.price is not None
                and row["price"] is not None
                and listing.price < row["price"]
            ):
                drops.append((listing, int(row["price"])))
        return new, drops

    def record(self, search: str, listings: list[Listing], notified: bool = True) -> None:
        """Insert or refresh listings, marking whether they were notified."""
        now = time.time()
        rows = [
            (search, l.id, l.title, l.price, l.url, now, now, int(notified))
            for l in listings
        ]
        self.conn.executemany(
            """
            INSERT INTO seen
                (search, listing_id, title, price, url, first_seen, last_seen, notified)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(search, listing_id) DO UPDATE SET
                title = excluded.title,
                price = excluded.price,
                url = excluded.url,
                last_seen = excluded.last_seen,
                notified = seen.notified | excluded.notified
            """,
            rows,
        )
        self.conn.commit()

    def prune(self, older_than_days: int) -> int:
        """Drop rows not seen in a while to keep the DB small."""
        if older_than_days <= 0:
            return 0
        cutoff = time.time() - older_than_days * 86400
        cur = self.conn.execute("DELETE FROM seen WHERE last_seen < ?", (cutoff,))
        self.conn.commit()
        return cur.rowcount

    def stats(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT search, COUNT(*) AS total, MAX(last_seen) AS last_seen
            FROM seen GROUP BY search ORDER BY search
            """
        ).fetchall()
