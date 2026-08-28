"""Read the curated lists the stats panel tracks progress against.

Each list page names its films with a `data-item-slug` attribute. Diary entries
carry the same slug, so list progress is a set intersection and needs no title
matching and no TMDB lookup.

Pages hold a hundred films each. Results are cached per list so a weekly run
re-reads a list only when its cache is missing or stale.
"""

from __future__ import annotations

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
    CURATED_LISTS,
    LISTS_CACHE_DIR,
    REQUEST_DELAY,
    REQUEST_TIMEOUT,
    USER_AGENT,
    ensure_dirs,
)

# Every film on a list page carries its slug and its display name as two
# attributes of the same element. FILM_ELEMENT_PATTERN finds that element, so the
# two are always read together and a film can only ever be paired with its own
# name.
#
# Reading each attribute across the whole page and pairing the two result lists
# by position gives the same answer on healthy markup and a silently wrong one
# the moment a single element carries one attribute without the other: from that
# film on, every entry takes its name from the next film along, and the result is
# the right size, so no size check can catch it.
FILM_ELEMENT_PATTERN = re.compile(r'<[a-zA-Z][^>]*\bdata-item-slug="[^"]*"[^>]*>')
SLUG_PATTERN = re.compile(r'data-item-slug="([^"]+)"')
NAME_PATTERN = re.compile(r'data-item-name="([^"]+)"')
MAX_PAGES = 20  # 2,000 films, above the largest list tracked

# How much smaller than its cached copy a refreshed list may come back.
#
# A curated list gains or loses a handful of films between weekly runs. It does
# not lose a tenth of itself. A refresh that returns less than this share of what
# the cache already holds is treated as a broken read rather than as an edit,
# because from here the two look identical: Letterboxd answers 200 either way,
# and markup this file no longer matches simply yields nothing.
MINIMUM_SHARE_OF_CACHED_FILMS = 0.9


class PageMarkupError(RuntimeError):
    """Raised when a page's film attributes cannot be paired element by element.

    This is an error and not a short read on purpose. A page whose two attributes
    no longer sit on the same element still yields films, and each of those films
    carries another film's name. That result is the right size, so the size check
    below passes it, and the wrong names reach the cache unnoticed.
    """


def list_page_url(path: str, page: int) -> str:
    """Return the URL for one page of a list."""
    return f"{BASE_URL}{path}" if page == 1 else f"{BASE_URL}{path}page/{page}/"


def fetch_list_page(url: str, client: httpx.Client) -> str:
    """Download one page of a list."""
    response = client.get(url)
    response.raise_for_status()
    return response.text


def read_page(html: str, source: str) -> list[dict[str, str]]:
    """Pull the films out of one list page, one element at a time.

    `source` is the URL the markup came from, so a refusal can name the page to
    open.

    A film is read only from an element that carries both its slug and its name.
    When the page holds more of either attribute than it holds elements carrying
    both, the markup no longer pairs a film with its own name, and this raises
    rather than returning the films it could pair.
    """
    films: list[dict[str, str]] = []
    for element in FILM_ELEMENT_PATTERN.findall(html):
        slug = SLUG_PATTERN.search(element)
        name = NAME_PATTERN.search(element)
        if slug is None or name is None:
            # Counted, then refused below. Skipping it silently would just make
            # the read short, which reads as an edit rather than as broken markup.
            continue
        films.append({"slug": slug.group(1), "name": name.group(1)})

    slug_count = len(SLUG_PATTERN.findall(html))
    name_count = len(NAME_PATTERN.findall(html))
    if len(films) != slug_count or len(films) != name_count:
        raise PageMarkupError(
            f"the film markup at {source} changed: the page carries {slug_count} film slugs "
            f"and {name_count} film names, but {len(films)} elements carry both, so pairing "
            f"those would give films the wrong names"
        )

    return films


def fetch_list(path: str, client: httpx.Client) -> list[dict[str, str]]:
    """Walk a list's pages until one comes back with no film it has not seen."""
    films: list[dict[str, str]] = []
    seen: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        url = list_page_url(path, page)
        page_films = read_page(fetch_list_page(url, client), url)

        fresh = [film for film in page_films if film["slug"] not in seen]
        if not fresh:
            break

        for film in fresh:
            seen.add(film["slug"])
        films.extend(fresh)

        time.sleep(REQUEST_DELAY)

    return films


def cache_path(list_id: str) -> Path:
    return LISTS_CACHE_DIR / f"{list_id}.json"


def cached_film_count(list_id: str) -> int:
    """Return how many films the cache on disk holds for a list, or 0 for none."""
    target = cache_path(list_id)
    if not target.exists():
        return 0
    try:
        return len(json.loads(target.read_text())["films"])
    except (OSError, ValueError, KeyError, TypeError):
        # A cache that cannot be read is no evidence about the refresh, and it
        # holds no films worth protecting either.
        return 0


def refusal_reason(
    fetched_count: int,
    cached_count: int,
    allow_shrink: bool,
    forced: bool,
) -> str | None:
    """Say why a fetched list must not overwrite its cache, or None when it may.

    Fetching a list and writing it are separate decisions on purpose. Letterboxd
    answers 200 with no films both when a page really is empty and when the
    markup this file parses has changed, so the size of the result is the only
    signal available that a read went wrong. Writing the smaller result would
    replace real membership with an empty list, and the next commit would publish
    that as progress of nothing out of nothing.

    The size check needs something to measure against, and the cache is it. A
    list with no cached copy has nothing, so under --force the check is refused
    rather than skipped: skipping it would let the one run nobody watches accept
    whatever the page happened to return, which is the failure this whole
    function exists to stop. Creating a cache for a newly tracked list is a job
    for a person, who runs this script without --force and looks at the count.
    """
    if fetched_count == 0:
        return (
            "the refresh found no films at all, so either the page markup changed, "
            "the request was blocked, or the list was emptied"
        )

    if cached_count == 0 and forced:
        return (
            f"the refresh found {fetched_count} films and there is no cached copy to check "
            f"that size against, so an unattended run cannot tell a full read from a broken one"
        )

    if allow_shrink or cached_count == 0:
        return None

    smallest_accepted = cached_count * MINIMUM_SHARE_OF_CACHED_FILMS
    if fetched_count < smallest_accepted:
        percent = round(MINIMUM_SHARE_OF_CACHED_FILMS * 100)
        return (
            f"the refresh found {fetched_count} films where the cache already holds "
            f"{cached_count}, below the {percent} percent floor a real edit stays above"
        )

    return None


def refresh_all(force: bool = False, allow_shrink: bool = False) -> dict[str, Any]:
    """Fetch every curated list, reusing the cache unless force is set.

    A cache is overwritten only when the refresh comes back a plausible size. A
    list that fails or is refused keeps the copy already on disk, and the summary
    records why, so the caller can end the run in failure rather than in silence.
    """
    ensure_dirs()
    summary: dict[str, Any] = {}

    with httpx.Client(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        for list_id, title, path in CURATED_LISTS:
            target = cache_path(list_id)

            if target.exists() and not force:
                films = json.loads(target.read_text())["films"]
                summary[list_id] = {"title": title, "count": len(films), "cached": True}
                continue

            try:
                films = fetch_list(path, client)
            except httpx.HTTPError as error:
                # A list that moved, was deleted, or answered 403 must not stop
                # the other fifteen from being read. It is still recorded as a
                # failure: a list nobody can read is a list whose progress figures
                # have quietly stopped moving.
                kept = cached_film_count(list_id)
                print(f"  {list_id}: could not read the list ({error}). Kept the cached {kept} films.")
                summary[list_id] = {"title": title, "count": kept, "error": str(error)}
                continue
            except PageMarkupError as error:
                # Recorded as a refusal rather than as an error, because the page
                # answered normally. What it answered with cannot be trusted, and
                # the advice for it is the markup advice `report_failures` prints.
                kept = cached_film_count(list_id)
                kept_note = f"Kept the cached {kept} films." if kept else "There is no cache to keep."
                print(f"  {list_id}: refused the refresh because {error}. {kept_note}")
                summary[list_id] = {"title": title, "count": kept, "refused": str(error)}
                continue

            kept = cached_film_count(list_id)
            reason = refusal_reason(len(films), kept, allow_shrink, forced=force)
            if reason is not None:
                kept_note = f"Kept the cached {kept} films." if kept else "There is no cache to keep."
                print(f"  {list_id}: refused the refresh because {reason}. {kept_note}")
                summary[list_id] = {"title": title, "count": kept, "refused": reason}
                continue

            target.write_text(
                json.dumps({"title": title, "path": path, "films": films}, indent=2, ensure_ascii=False)
            )
            summary[list_id] = {"title": title, "count": len(films), "cached": False}
            print(f"  {list_id}: {len(films)} films")

    return summary


def load_all() -> dict[str, dict[str, Any]]:
    """Read every cached list."""
    lists: dict[str, dict[str, Any]] = {}
    for list_id, title, _ in CURATED_LISTS:
        target = cache_path(list_id)
        if target.exists():
            lists[list_id] = json.loads(target.read_text())
    return lists


def report_failures(summary: dict[str, Any]) -> int:
    """Print what went wrong and what to do about it, and return the exit code.

    Returns 0 when every list was either refreshed or deliberately served from
    cache, and 1 when at least one list was refused or could not be read.
    """
    refused = [list_id for list_id, result in summary.items() if "refused" in result]
    unreadable = [list_id for list_id, result in summary.items() if "error" in result]

    if not refused and not unreadable:
        return 0

    if refused:
        print(
            f"Refused {len(refused)} of {len(CURATED_LISTS)} list refreshes, so those caches were "
            f"left exactly as they were: {', '.join(refused)}.",
            file=sys.stderr,
        )
        print(
            "A refusal means the page answered but the result could not be trusted: far fewer "
            "films than the cache already holds, or a page whose films no longer carry their "
            "slug and their name on the same element. Both nearly always mean Letterboxd "
            "changed the list markup.",
            file=sys.stderr,
        )
        print(
            "Do this: open one of those list pages and check whether its films still carry "
            "data-item-slug and data-item-name on the same element. If they do not, fix "
            "FILM_ELEMENT_PATTERN, SLUG_PATTERN and NAME_PATTERN in scripts/fetch_lists.py. If "
            "the lists really did shrink that much, re-run this script with --allow-shrink to "
            "accept the smaller result.",
            file=sys.stderr,
        )
        print(
            "A list refused for having no cached copy is a different case, and --allow-shrink "
            "does not cover it. Run this script once without --force to create that cache, check "
            "the film count looks right for the list, and commit the file under data/cache/lists.",
            file=sys.stderr,
        )

    if unreadable:
        print(
            f"Could not read {len(unreadable)} lists, so those caches were left exactly as they "
            f"were: {', '.join(unreadable)}.",
            file=sys.stderr,
        )
        print(
            "Do this: open each one's path from CURATED_LISTS in scripts/lib/config.py in a "
            "browser. A list that moved or was deleted needs its path corrected there. A list that "
            "answers 403 needs the run repeated later.",
            file=sys.stderr,
        )

    return 1


if __name__ == "__main__":
    force = "--force" in sys.argv
    # --allow-shrink is for a human who has checked the pages and knows the lists
    # really did lose that many films. It must never appear in the weekly
    # workflow, where nobody is watching to make that judgement.
    allow_shrink = "--allow-shrink" in sys.argv

    results = refresh_all(force=force, allow_shrink=allow_shrink)
    print(json.dumps(results, indent=2, ensure_ascii=False))
    sys.exit(report_failures(results))
