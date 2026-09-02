"""Read the member's watchlist from their public Letterboxd pages.

The watchlist is the one part of the account the RSS feed does not carry, and it
changes constantly, so a single export snapshot would go stale within weeks.

Membership comes from here. The date a film was added does not: these pages never
state it, and only the export does. So this reader keeps every `added_date` it has
already stored and stamps only genuinely new films, marking those as estimates.

Reading the pages and storing what they gave is deliberately one decision each.
A short read is refused rather than written, because writing one throws away the
real added dates the export supplied and nothing can put them back. See
`refusal_reason` for the whole argument, and `PageMarkupError` for the one way a
read goes wrong that no size check could ever catch.

These pages are public, need no sign-in, and sit behind no bot challenge. The
reader is deliberately a polite one: it identifies itself honestly and waits
between pages. It stops walking when a page comes back with no films it has not
already seen, which is how the site marks the end of the watchlist. It never
retries: anything that goes wrong mid-walk ends the run without storing
anything, because a walk that stopped early is exactly the read that must not
be written.
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
from lib.safe_http import read_text

# Every film on a watchlist page carries its slug and its display name as two
# attributes of the same element. FILM_ELEMENT_PATTERN finds that element, so the
# two are always read together and a film can only ever be paired with its own
# title.
#
# Reading each attribute across the whole page and pairing the two result lists
# by position gives the same answer on healthy markup and a silently wrong one
# the moment a single element carries one attribute without the other: from that
# film on, every entry takes its title and year from the next film along, and the
# result is the right size, so no size check can catch it.
FILM_ELEMENT_PATTERN = re.compile(r'<[a-zA-Z][^>]*\bdata-item-slug="[^"]*"[^>]*>')
SLUG_PATTERN = re.compile(r'data-item-slug="([^"]+)"')
NAME_PATTERN = re.compile(r'data-item-name="([^"]+)"')
TOTAL_PATTERN = re.compile(r'data-num-entries="(\d+)"')
TITLE_YEAR_PATTERN = re.compile(r"^(.*)\s+\((\d{4})\)$")

# Letterboxd shows 28 films per watchlist page.
FILMS_PER_PAGE = 28

# A ceiling of 200 pages covers a 5,600-film watchlist, far above any real one,
# and stops a broken selector from looping forever.
MAX_PAGES = 200

# How much smaller than the watchlist already stored a read may come back before
# it is treated as a broken read rather than as real removals.
#
# A watchlist gains and loses a handful of films in a week. It does not lose a
# tenth of itself. A read below this share of what is already stored is refused,
# because from here a markup change looks exactly like a mass removal: the pages
# answer 200 either way, and markup these patterns no longer match simply yields
# nothing.
#
# The total the pages themselves state is checked far more tightly, because it is
# an exact number rather than an estimate. See `refusal_reason`.
MINIMUM_SHARE_OF_STORED_FILMS = 0.9


class PageMarkupError(RuntimeError):
    """Raised when a page's film attributes cannot be paired element by element.

    This is an error and not a short read on purpose. A page whose two attributes
    no longer sit on the same element still yields films, and each of those films
    carries another film's title and year. That result is the right size, so every
    size check downstream passes it, and the wrong titles reach `data/history.json`
    unnoticed.
    """


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


def read_page(html: str, source: str) -> list[dict[str, Any]]:
    """Pull the films out of one watchlist page, one element at a time.

    `source` is the URL the markup came from, so a refusal can name the page to
    open.

    A film is read only from an element that carries both its slug and its
    display name. When the page holds more of either attribute than it holds
    elements carrying both, the markup no longer pairs a film with its own title,
    and this raises rather than returning the films it could pair. Returning them
    would hand back a full-sized page of films wearing each other's titles.
    """
    films: list[dict[str, Any]] = []
    for element in FILM_ELEMENT_PATTERN.findall(html):
        slug = SLUG_PATTERN.search(element)
        name = NAME_PATTERN.search(element)
        if slug is None or name is None:
            # Counted, then refused below. Skipping it silently would just make
            # the read short, which reads as removals rather than as broken markup.
            continue
        title, year = split_title_and_year(name.group(1))
        films.append({"slug": slug.group(1), "title": title, "year": year})

    slug_count = len(SLUG_PATTERN.findall(html))
    name_count = len(NAME_PATTERN.findall(html))
    if len(films) != slug_count or len(films) != name_count:
        raise PageMarkupError(
            f"Stopped reading the watchlist at {source} because its film markup changed: "
            f"the page carries {slug_count} film slugs and {name_count} film names, but "
            f"{len(films)} elements carry both. Pairing those would give films the wrong "
            f"titles, so nothing was read.\n"
            f"Do this: open that page and check whether every film still carries "
            f"data-item-slug and data-item-name on the same element. If it does not, fix "
            f"FILM_ELEMENT_PATTERN, SLUG_PATTERN and NAME_PATTERN in scripts/fetch_watchlist.py."
        )

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
            url = page_url(username, page)
            # There is deliberately no branch that treats a 404 as the end of the
            # watchlist. Letterboxd answers 200 with no films past the last page,
            # checked on page 39 and page 100 of a 38-page watchlist, so the walk
            # ends on an empty page and this branch would never fire on a healthy
            # read. All it could ever do is turn one failed page mid-walk into a
            # short read, which the caller would then have to catch.
            #
            # read_text raises on an error status exactly as raise_for_status
            # did, and additionally refuses a page too large to be a real one.
            html = read_text(client, url)

            if total is None:
                total = declared_total(html)

            fresh = [film for film in read_page(html, url) if film["slug"] not in seen]
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


def refusal_reason(
    read_count: int,
    declared_count: int | None,
    stored_count: int,
    allow_shrink: bool,
    allow_first_watchlist: bool,
) -> str | None:
    """Say why a watchlist read must not be stored, or None when it may.

    Reading the watchlist and storing it are separate decisions on purpose.
    Letterboxd answers 200 whether or not its pages still carry the attributes
    the films are read from, so a markup change and a watchlist the member
    emptied look identical from here: both come back as no films.

    Storing the smaller result costs more than a wrong number for one week. The
    stored watchlist is the only place the export's real `added_date` values
    live. The pages never state them, so every film dropped here and seen again
    next week returns stamped with next week's date and flagged as an estimate.
    The real dates cannot be recovered without downloading a fresh export, and
    the export only ever states them for films still on the watchlist. That is
    why an unattended run refuses rather than writes.

    `allow_shrink` is for a person who has looked at the pages and knows the
    watchlist really did lose that many films. It deliberately does not cover a
    read that fell short of the total the site itself states: the page and the
    parser disagree there, which is a read that stopped early, not a judgement
    anyone can make from outside. On an empty read it counts only alongside a
    page that states a watchlist of zero, so clearing the stored watchlist takes
    the site and a person saying the same thing.

    `allow_first_watchlist` is for a person creating a watchlist where none is
    stored yet. Nothing stored means nothing to measure a read against, the same
    hole `fetch_lists.py` refuses to fall into under `--force`, and it is also
    the one run that stamps every film with an estimated date. Neither belongs in
    the weekly workflow, so an unflagged run with nothing stored refuses.
    """
    # An empty read is stored only on two independent confirmations: the pages
    # state a watchlist of zero, and a person passed the override. Either alone
    # is what a markup change looks like.
    emptied_on_purpose = declared_count == 0 and allow_shrink
    if read_count == 0 and not emptied_on_purpose:
        return (
            "the read found no films at all, so either the page markup changed, "
            "the request was blocked, or the watchlist was emptied"
        )

    # The pages state an exact number, not an estimate, so the only gap worth
    # tolerating is films moving on or off the watchlist while the walk was
    # running. One page of them is already generous. Anything larger is a walk
    # that stopped early, and storing it drops the export's real added date for
    # every film that fell out.
    if declared_count is not None and read_count < declared_count - FILMS_PER_PAGE:
        missing = declared_count - read_count
        return (
            f"the read found {read_count} films where the watchlist pages state "
            f"{declared_count}, a gap of {missing} films where at most {FILMS_PER_PAGE}, one "
            f"page, can be films moving while the pages were walked, so the read stopped "
            f"early instead of finishing"
        )

    if stored_count == 0:
        if allow_first_watchlist:
            return None
        return (
            f"no watchlist is stored yet, so there is nothing to check the size of this read "
            f"against, and the weekly workflow must not create the first one: it would stamp "
            f"all {read_count} films with today's date as an estimate, and only the export "
            f"carries the real ones"
        )

    if allow_shrink:
        return None

    if read_count < stored_count * MINIMUM_SHARE_OF_STORED_FILMS:
        percent = round(MINIMUM_SHARE_OF_STORED_FILMS * 100)
        return (
            f"the read found {read_count} films where {stored_count} are already stored, "
            f"below the {percent} percent floor a week of real removals stays above"
        )

    return None


def report_refusal(reason: str, stored_count: int) -> None:
    """Print why nothing was stored and what to do about it."""
    print(
        f"Refused to store this watchlist read because {reason}.",
        file=sys.stderr,
    )
    if stored_count:
        print(
            f"The {stored_count} films already in {HISTORY_FILE} were left exactly as they were, "
            f"with the added dates they carry.",
            file=sys.stderr,
        )
    else:
        print(f"Nothing was written to {HISTORY_FILE}.", file=sys.stderr)
    if stored_count == 0:
        print(
            "Do this: run scripts/backfill.py against your export first. It writes the "
            "watchlist with the real date each film was added, which the pages never state "
            "and this reader then keeps.",
            file=sys.stderr,
        )
        print(
            "Only if you have no export and accept that every watchlist age will be a "
            "guess, run this script by hand with --allow-first-watchlist, and check the "
            "film count it prints against the total the pages state. That flag must never "
            "be added to the weekly workflow, where nobody is watching to make that "
            "judgement.",
            file=sys.stderr,
        )
        return

    print(
        "Do this: open https://letterboxd.com/"
        f"{LETTERBOXD_USER}/watchlist/ and check whether its films still carry a "
        "data-item-slug attribute. If they do not, fix FILM_ELEMENT_PATTERN, SLUG_PATTERN "
        "and NAME_PATTERN in scripts/fetch_watchlist.py. If the pages look normal, run this "
        "script again, because a page that failed mid-walk gives the same short read.",
        file=sys.stderr,
    )
    print(
        "Only if you have checked the pages and the watchlist really did lose that many "
        "films, re-run with --allow-shrink to store the smaller result. Doing that drops "
        "the export's real added dates for every film no longer listed. A read of no films "
        "at all is stored only when the pages themselves state a watchlist of zero, so on "
        "an empty read the override alone changes nothing. Neither does --allow-shrink "
        "cover a read that fell short of the total the pages state: that is a walk that "
        "stopped early, not a removal anyone can confirm from outside.",
        file=sys.stderr,
    )


def load_history() -> dict[str, Any]:
    """Read data/history.json, or return the empty shape when there is none yet."""
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    return {"username": LETTERBOXD_USER, "entry_count": 0, "entries": [], "watchlist": []}


def save_to_history(
    history: dict[str, Any],
    films: list[dict[str, Any]],
    today: str,
) -> tuple[int, int]:
    """Store the watchlist, keeping the dates already known for films seen before."""
    ensure_dirs()
    merged, added, removed = merge_watchlist(history.get("watchlist", []), films, today)

    history["watchlist"] = merged
    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")
    return added, removed


def main(argv: list[str] | None = None) -> int:
    """Read the watchlist and store it, unless the read looks broken.

    Returns 0 when the watchlist was stored, and 1 when it was refused or could
    not be read. The weekly workflow runs this before the build and the commit,
    so a non-zero exit stops the job while the published site still holds last
    week's real numbers.
    """
    arguments = sys.argv[1:] if argv is None else argv
    # Both flags are for a human who has looked at the pages and can make a
    # judgement about what they show. Neither must ever appear in the weekly
    # workflow, where nobody is watching to make one.
    #
    # --allow-shrink says the watchlist really did lose that many films.
    # --allow-first-watchlist says it is fine to create a watchlist from nothing,
    # accepting that every added date in it will be an estimate.
    allow_shrink = "--allow-shrink" in arguments
    allow_first_watchlist = "--allow-first-watchlist" in arguments

    # Read the stored history before the fetch rather than after. The gate
    # compares the read against what is already stored, so both have to be the
    # same snapshot, and a history file that cannot be read should fail in a
    # second rather than after 46 seconds of polite requests.
    history = load_history()
    stored_count = len(history.get("watchlist", []))

    try:
        films, total = fetch_watchlist()
    except httpx.HTTPError as error:
        print(f"Could not read the watchlist pages ({error}).", file=sys.stderr)
        print(
            f"Nothing was stored, so the {stored_count} films already in {HISTORY_FILE} still "
            f"hold the added dates they had.",
            file=sys.stderr,
        )
        print(
            "Do this: run this script again. A page that answers 403 or times out usually "
            "answers normally later. If it keeps failing, open "
            f"https://letterboxd.com/{LETTERBOXD_USER}/watchlist/ in a browser to see what "
            "the site is returning.",
            file=sys.stderr,
        )
        return 1
    except PageMarkupError as error:
        print(str(error), file=sys.stderr)
        print(
            f"Nothing was stored, so the {stored_count} films already in {HISTORY_FILE} still "
            f"hold the added dates they had.",
            file=sys.stderr,
        )
        return 1

    stated = total if total is not None else "unstated"
    print(f"Read {len(films)} watchlist films (the site states {stated}).")

    reason = refusal_reason(len(films), total, stored_count, allow_shrink, allow_first_watchlist)
    if reason is not None:
        report_refusal(reason, stored_count)
        return 1

    today = datetime.date.today().isoformat()
    added, removed = save_to_history(history, films, today)
    print(f"New since the last read: {added}. Removed since the last read: {removed}.")
    print(f"Stored in {HISTORY_FILE}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
