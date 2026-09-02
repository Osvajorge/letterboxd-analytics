"""Read each film's TMDB id from its own Letterboxd page.

Letterboxd sources its film data from TMDB and states the id it uses on every
film page:

    <body ... data-tmdb-id="848685" data-tmdb-type="movie">

That is the authoritative answer to "which TMDB record is this film", and it
replaces guessing. Searching TMDB by title and year cannot tell "Ghost in the
Shell" 1995 from the 2017 remake, or "The Beasts" from "Fantastic Beasts", and on
this account it merged six pairs of distinct films into three.

The type matters as much as the id. A film Letterboxd files as television is not
in TMDB's movie endpoint at all, so knowing that up front turns twenty five
mysterious failures into twenty five correctly skipped records.

Results go to `data/tmdb-ids.json` and are committed. A film's TMDB id does not
change, so this runs once per new film and never again.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.config import BASE_URL, DATA, HISTORY_FILE, REQUEST_TIMEOUT, USER_AGENT, ensure_dirs
from lib.safe_http import ResponseTooLarge, read_text_and_status

TMDB_IDS_FILE = DATA / "tmdb-ids.json"

TMDB_ID_PATTERN = re.compile(r'data-tmdb-id="(\d+)"')
TMDB_TYPE_PATTERN = re.compile(r'data-tmdb-type="([a-z]+)"')

DELAY_BETWEEN_REQUESTS = 0.25

# A page that answers but names no id is a real answer: Letterboxd has no TMDB
# record for that film. A request that never succeeded is not, and is left out of
# the file so the next run tries again.
NO_TMDB_RECORD = {"tmdb_id": None, "tmdb_type": None}


def load_known() -> dict[str, dict]:
    """Read the ids resolved by earlier runs."""
    if not TMDB_IDS_FILE.exists():
        return {}
    return json.loads(TMDB_IDS_FILE.read_text(encoding="utf-8"))


def save_known(known: dict[str, dict]) -> None:
    """Write the map back, sorted so a diff shows only real changes."""
    ensure_dirs()
    ordered = {slug: known[slug] for slug in sorted(known)}
    TMDB_IDS_FILE.write_text(json.dumps(ordered, indent=2, ensure_ascii=False), encoding="utf-8")


def read_film_page(slug: str, client: httpx.Client) -> tuple[dict | None, bool]:
    """Read one film page and return what it says about TMDB.

    Returns the answer and whether the request itself succeeded, so a transient
    failure is never recorded as "this film has no TMDB record".
    """
    try:
        # read_text_and_status streams the page under a size budget and never
        # reads the body of a non-200 answer at all.
        status, html = read_text_and_status(client, f"{BASE_URL}/film/{slug}/")
    except (httpx.HTTPError, ResponseTooLarge):
        return None, False

    if status == 404:
        # The film page is gone. That is an answer, though an unhelpful one.
        return NO_TMDB_RECORD, True
    if status != 200:
        return None, False

    id_match = TMDB_ID_PATTERN.search(html)
    type_match = TMDB_TYPE_PATTERN.search(html)
    if id_match is None:
        return NO_TMDB_RECORD, True

    return {
        "tmdb_id": int(id_match.group(1)),
        "tmdb_type": type_match.group(1) if type_match else None,
    }, True


def resolve(slugs: list[str]) -> dict[str, dict]:
    """Resolve every slug not already known."""
    known = load_known()
    pending = [slug for slug in dict.fromkeys(slugs) if slug not in known]

    if not pending:
        print(f"All {len(set(slugs))} films already have an answer.")
        return known

    print(f"Reading {len(pending)} film pages. {len(known)} were already known.")
    found = absent = failed = 0

    with httpx.Client(
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        for position, slug in enumerate(pending, start=1):
            answer, settled = read_film_page(slug, client)

            if not settled:
                failed += 1
            elif answer["tmdb_id"] is None:
                known[slug] = answer
                absent += 1
            else:
                known[slug] = answer
                found += 1

            if position % 100 == 0:
                print(f"  {position} of {len(pending)}")
                save_known(known)

            time.sleep(DELAY_BETWEEN_REQUESTS)

    save_known(known)
    print(f"Found an id for {found}. No TMDB record: {absent}. Request failed: {failed}.")
    if failed:
        print(
            f"{failed} pages could not be read and were not recorded, so the next run will "
            f"try them again. Re-run this script when the network is healthy."
        )

    by_type: dict[str, int] = {}
    for answer in known.values():
        kind = answer.get("tmdb_type") or "none"
        by_type[kind] = by_type.get(kind, 0) + 1
    print("Records by type: " + ", ".join(f"{k} {v}" for k, v in sorted(by_type.items())))
    return known


def main() -> None:
    if not HISTORY_FILE.exists():
        print(
            f"No history at {HISTORY_FILE}. Run scripts/backfill.py against your export "
            f"first, then run this script.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    slugs = [entry["slug"] for entry in history.get("entries", []) if entry.get("slug")]
    if not slugs:
        print(f"{HISTORY_FILE} holds no films with a slug.", file=sys.stderr)
        raise SystemExit(1)

    resolve(slugs)
    print(f"Stored in {TMDB_IDS_FILE}.")


if __name__ == "__main__":
    main()
