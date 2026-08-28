"""Read the member's watchlist from their public Letterboxd pages.

The watchlist is the one part of the account the RSS feed does not carry, and it
changes constantly, so a single export snapshot would go stale within weeks.

Membership comes from here. The date a film was added does not: these pages never
state it, and only the export does. So this reader keeps every `added_date` it has
already stored and stamps only genuinely new films, marking those as estimates.

These pages are public, need no sign-in, and sit behind no bot challenge. The
reader is deliberately a polite one: it identifies itself honestly, waits between
pages, stops as soon as a page repeats what it has already seen, and gives up
quietly rather than hammering the site when something goes wrong.
"""

from __future__ import annotations

import datetime
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.config import (
    BASE_URL,
    HISTORY_FILE,
    LETTERBOXD_USER,
    REQUEST_DELAY,
    REQUEST_TIMEOUT,
    USER_AGENT,
    ensure_dirs,
)

SLUG_PATTERN = re.compile(r'data-item-slug="([^"]+)"')
NAME_PATTERN = re.compile(r'data-item-name="([^"]+)"')
TOTAL_PATTERN = re.compile(r'data-num-entries="(\d+)"')
TITLE_YEAR_PATTERN = re.compile(r"^(.*)\s+\((\d{4})\)$")

# Letterboxd shows 28 films per watchlist page. A ceiling of 200 pages covers a
# 5,600-film watchlist, far above any real one, and stops a broken selector from
# looping forever.
MAX_PAGES = 200


def page_url(username: str, page: int) -> str:
    """Return the URL for one page of a member's watchlist."""
    if page == 1:
        return f"{BASE_URL}/{username}/watchlist/"
    return f"{BASE_URL}/{username}/watchlist/page/{page}/"


def split_title_and_year(display_name: str) -> tuple[str, int | None]:
    """Split "Film Name (1999)" into its title and year.

    A name without a trailing year keeps the whole string as the title.
    """
    match = TITLE_YEAR_PATTERN.match(display_name.strip())
    if not match:
        return display_name.strip(), None
    return match.group(1).strip(), int(match.group(2))


def read_page(html: str) -> list[dict[str, Any]]:
    """Pull the films out of one watchlist page."""
    films = []
    for slug, display_name in zip(SLUG_PATTERN.findall(html), NAME_PATTERN.findall(html)):
        title, year = split_title_and_year(display_name)
        films.append({"slug": slug, "title": title, "year": year})
    return films


def declared_total(html: str) -> int | None:
    """Return the watchlist size Letterboxd states on the page, when it says one.

    This is how the run knows whether it read everything, rather than assuming a
    short page meant the end.
    """
    match = TOTAL_PATTERN.search(html)
    return int(match.group(1)) if match else None


def fetch_watchlist(username: str = LETTERBOXD_USER) -> tuple[list[dict[str, Any]], int | None]:
    """Walk every page of the watchlist.

    Returns the films found and the total the site declared, so the caller can
    tell a complete read from a truncated one.
    """
    films: list[dict[str, Any]] = []
    seen: set[str] = set()
    total: int | None = None

    with httpx.Client(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            response = client.get(page_url(username, page))
            if response.status_code == 404:
                break
            response.raise_for_status()

            if total is None:
                total = declared_total(response.text)

            fresh = [film for film in read_page(response.text) if film["slug"] not in seen]
            if not fresh:
                break

            for film in fresh:
                seen.add(film["slug"])
            films.extend(fresh)

            if total is not None and len(films) >= total:
                break

            time.sleep(REQUEST_DELAY)

    return films, total


def merge_watchlist(
    stored: list[dict[str, Any]],
    found: list[dict[str, Any]],
    today: str,
) -> tuple[list[dict[str, Any]], int, int]:
    """Combine what the site shows now with the dates already known.

    Membership is replaced wholesale, because a film removed on the site must
    disappear here too. Dates are not: a film already stored keeps the date it
    had, whether that came from the export or from an earlier weekly read.
    Restamping every film each week would reset all of their ages and make the
    watchlist age statistics meaningless.

    Returns the merged watchlist, how many films are new, and how many were
    dropped since the last read.
    """
    known = {film["slug"]: film for film in stored}
    merged: list[dict[str, Any]] = []
    added = 0

    for film in found:
        previous = known.get(film["slug"])
        # A stored film only keeps its date if it actually has one. An entry left
        # dateless by an earlier run would otherwise stay dateless forever and be
        # excluded from the watchlist age statistics for good.
        if previous is not None and previous.get("added_date"):
            merged.append(
                {
                    **film,
                    "added_date": previous["added_date"],
                    "added_date_estimated": previous.get("added_date_estimated", True),
                }
            )
            continue

        # First sighting. This is an upper bound on the real date, not the date
        # itself, so it is flagged as an estimate.
        merged.append({**film, "added_date": today, "added_date_estimated": True})
        added += 1

    removed = len(set(known) - {film["slug"] for film in found})
    return merged, added, removed


def save_to_history(films: list[dict[str, Any]], total: int | None) -> tuple[int, int]:
    """Store the watchlist, keeping the dates already known for films seen before."""
    ensure_dirs()
    if HISTORY_FILE.exists():
        history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    else:
        history = {"username": LETTERBOXD_USER, "entry_count": 0, "entries": [], "watchlist": []}

    today = datetime.date.today().isoformat()
    merged, added, removed = merge_watchlist(history.get("watchlist", []), films, today)

    history["watchlist"] = merged
    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")

    if total is not None and len(films) != total:
        print(
            f"Warning: the site says the watchlist holds {total} films but only {len(films)} "
            f"were read. The stored watchlist is incomplete, so nothing was removed on the "
            f"strength of a partial read. Re-run this script, and if the gap persists the "
            f"page markup has probably changed.",
            file=sys.stderr,
        )

    return added, removed


def main() -> None:
    films, total = fetch_watchlist()
    added, removed = save_to_history(films, total)
    stated = total if total is not None else "unstated"
    print(f"Read {len(films)} watchlist films (the site states {stated}).")
    print(f"New since the last read: {added}. Removed since the last read: {removed}.")
    print(f"Stored in {HISTORY_FILE}.")


if __name__ == "__main__":
    main()
