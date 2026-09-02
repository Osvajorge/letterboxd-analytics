"""Read the public Letterboxd RSS feed and return its diary entries.

The feed needs no authentication. Each entry already carries the watched date,
the rating, the rewatch flag, and the TMDB id, so nothing has to be matched by
title afterwards.

The feed is a rolling window of roughly the last fifty entries. It keeps an
existing history current; it cannot build one. Use backfill.py for that.
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from typing import Any

import httpx

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from lib.config import BASE_URL, LETTERBOXD_USER, REQUEST_TIMEOUT, USER_AGENT
from lib.safe_http import read_text

NAMESPACES = {
    "letterboxd": "https://letterboxd.com",
    "tmdb": "https://themoviedb.org",
}

FILM_SLUG_PATTERN = re.compile(r"/film/([^/]+)/?")


def fetch_feed(username: str = LETTERBOXD_USER) -> str:
    """Download the raw RSS document for one member."""
    url = f"{BASE_URL}/{username}/rss/"
    with httpx.Client(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        return read_text(client, url)


def _text(item: ET.Element, path: str) -> str | None:
    node = item.find(path, NAMESPACES)
    if node is None or node.text is None:
        return None
    return node.text.strip() or None


class UnsafeFeed(RuntimeError):
    """Raised when the feed carries markup a real Letterboxd feed never does."""


# Everything the XML prolog may legally hold before the root element, other than
# a document type declaration: whitespace, processing instructions, comments.
PROLOG_FILLER_PATTERN = re.compile(r"\s+|<\?.*?\?>|<!--.*?-->", re.DOTALL)


def refuse_document_type_declaration(xml_text: str) -> None:
    """Stop before parsing a feed that declares its own XML entities.

    An entity is declared inside a document type declaration and nowhere else,
    and expanding one is how a 10 KB document becomes 10 MB of text: four nested
    entities multiply by a thousand per level. Python's parser already refuses an
    entity that points at a file or a URL, so expansion is the whole risk, and a
    genuine RSS feed carries no document type declaration at all.

    Only the prolog is examined. A declaration is legal nowhere else, so the
    words "<!DOCTYPE" inside a film title or a review are just text and are left
    alone.
    """
    position = 1 if xml_text.startswith("\ufeff") else 0
    while True:
        filler = PROLOG_FILLER_PATTERN.match(xml_text, position)
        if filler is None:
            break
        position = filler.end()

    # Every comment was skipped above, so the only other thing that can open
    # with "<!" here is the declaration this refuses.
    if xml_text.startswith("<!", position):
        raise UnsafeFeed(
            "The feed opens with a document type declaration. A Letterboxd feed "
            "never carries one, and it is how a small feed expands into enough "
            "text to exhaust this machine. Nothing was read from it."
        )


def parse_feed(xml_text: str) -> list[dict[str, Any]]:
    """Turn the RSS document into diary entries.

    The feed mixes diary entries with list publications. Only items that carry a
    watched date are diary entries, so everything else is dropped.
    """
    refuse_document_type_declaration(xml_text)

    root = ET.fromstring(xml_text)
    entries: list[dict[str, Any]] = []

    for item in root.iter("item"):
        watched_date = _text(item, "letterboxd:watchedDate")
        if watched_date is None:
            continue

        link = _text(item, "link") or ""
        slug_match = FILM_SLUG_PATTERN.search(link)
        rating = _text(item, "letterboxd:memberRating")
        tmdb_id = _text(item, "tmdb:movieId")
        film_year = _text(item, "letterboxd:filmYear")

        entries.append(
            {
                "guid": _text(item, "guid"),
                "slug": slug_match.group(1) if slug_match else None,
                "title": _text(item, "letterboxd:filmTitle"),
                "year": int(film_year) if film_year and film_year.isdigit() else None,
                "watched_date": watched_date,
                "rating": float(rating) if rating else None,
                "rewatch": _text(item, "letterboxd:rewatch") == "Yes",
                "liked": _text(item, "letterboxd:memberLike") == "Yes",
                "tmdb_id": int(tmdb_id) if tmdb_id and tmdb_id.isdigit() else None,
                "source": "rss",
            }
        )

    return entries


def main() -> None:
    import json

    entries = parse_feed(fetch_feed())
    print(json.dumps(entries, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
