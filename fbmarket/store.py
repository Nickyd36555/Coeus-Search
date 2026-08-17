"""SQLite record of everything we've already seen, so you're pinged once."""

from __future__ import annotations

import sqlite3
import threading
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

# Added after the first release; applied to existing databases on open.
MIGRATIONS = {
    "location": "ALTER TABLE seen ADD COLUMN location TEXT",
    "image_url": "ALTER TABLE seen ADD COLUMN image_url TEXT",
    "price_text": "ALTER TABLE seen ADD COLUMN price_text TEXT",
    "mileage": "ALTER TABLE seen ADD COLUMN mileage INTEGER",
}


class Store:
    """Dedupe state keyed by (search name, listing id)."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The web UI touches the store from request threads and the background
        # monitor thread, so serialize access ourselves.
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        existing = {row["name"] for row in self.conn.execute("PRAGMA table_info(seen)")}
        for column, statement in MIGRATIONS.items():
            if column not in existing:
                self.conn.execute(statement)

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
            (
                search, l.id, l.title, l.price, l.url, now, now, int(notified),
                l.location, l.image_url, l.price_text, l.mileage,
            )
            for l in listings
        ]
        with self._lock:
            self.conn.executemany(
                """
                INSERT INTO seen
                    (search, listing_id, title, price, url, first_seen, last_seen,
                     notified, location, image_url, price_text, mileage)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(search, listing_id) DO UPDATE SET
                    title = excluded.title,
                    price = excluded.price,
                    url = excluded.url,
                    last_seen = excluded.last_seen,
                    location = excluded.location,
                    image_url = excluded.image_url,
                    price_text = excluded.price_text,
                    mileage = excluded.mileage,
                    notified = seen.notified | excluded.notified
                """,
                rows,
            )
            self.conn.commit()

    def recent(self, limit: int = 50, search: str | None = None) -> list[sqlite3.Row]:
        """Most recently discovered listings, newest first — the results feed."""
        query = "SELECT * FROM seen"
        params: list = []
        if search:
            query += " WHERE search = ?"
            params.append(search)
        query += " ORDER BY first_seen DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            return self.conn.execute(query, params).fetchall()

    def delete_search(self, search: str) -> int:
        with self._lock:
            cur = self.conn.execute("DELETE FROM seen WHERE search = ?", (search,))
            self.conn.commit()
            return cur.rowcount

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
