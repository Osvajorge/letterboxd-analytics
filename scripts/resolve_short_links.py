"""Turn the export's boxd.it short links into real Letterboxd film slugs.

Every film in a Letterboxd data export is identified by a short link such as
`https://boxd.it/hTha`, never by the film slug. The whole pipeline joins on the
slug, so without this step the export cannot be matched against the RSS feed or
against the curated lists, and the same film would appear twice.

Resolving one is a single request. The short link answers with a 302 whose
Location header already names the film, so the film page itself is never
fetched:

    https://boxd.it/hTha  ->  302  ->  letterboxd.com/film/parasite-2019/

Results are stored in `data/short-links.json` and committed. A short link never
changes its destination, so this runs once per new film and never again.

Short link ids are case-sensitive base62. Lowercasing them merges distinct
films: in one real export `boxd.it/1JzG` is Inglourious Basterds and
`boxd.it/1jzg` is Paris Is Burning.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.config import DATA, REQUEST_TIMEOUT, USER_AGENT, ensure_dirs

SHORT_LINKS_FILE = DATA / "short-links.json"

SHORT_LINK_PATTERN = re.compile(r"boxd\.it/([A-Za-z0-9]+)")
FILM_SLUG_PATTERN = re.compile(r"/film/([^/]+)/?")

# The short link host answers instantly, so a short pause is enough to stay a
# well-behaved client without making a 2,000-film first run take an hour.
DELAY_BETWEEN_REQUESTS = 0.15


def short_link_id(uri: str) -> str | None:
    """Return the boxd.it id inside a URI, preserving its case.

    Case matters: these ids are base62, so folding case merges distinct films.
    """
    match = SHORT_LINK_PATTERN.search(uri or "")
    return match.group(1) if match else None


def load_known() -> dict[str, str | None]:
    """Read the slugs resolved by earlier runs."""
    if not SHORT_LINKS_FILE.exists():
        return {}
    return json.loads(SHORT_LINKS_FILE.read_text(encoding="utf-8"))


def save_known(known: dict[str, str | None]) -> None:
    """Write the mapping back, sorted so a diff shows only real changes."""
    ensure_dirs()
    ordered = {key: known[key] for key in sorted(known)}
    SHORT_LINKS_FILE.write_text(json.dumps(ordered, indent=2, ensure_ascii=False), encoding="utf-8")


def resolve_one(short_id: str, client: httpx.Client) -> tuple[str | None, bool]:
    """Resolve one short link to a film slug.

    Returns the slug and whether the request itself succeeded. A request that
    failed is reported separately from a link that genuinely points nowhere, so
    a transient outage is never recorded as a permanent answer.
    """
    try:
        response = client.head(f"https://boxd.it/{short_id}")
    except httpx.HTTPError:
        return None, False

    if response.status_code not in (301, 302, 303, 307, 308):
        # Anything other than a redirect means the link did not resolve. A 404
        # is a real answer; a 5xx is not, so only treat 4xx as settled.
        return None, 400 <= response.status_code < 500

    match = FILM_SLUG_PATTERN.search(response.headers.get("location", ""))
    return (match.group(1) if match else None), True


def resolve_all(short_ids: list[str]) -> dict[str, str | None]:
    """Resolve every short link not already known, and report what happened."""
    known = load_known()
    pending = [short_id for short_id in dict.fromkeys(short_ids) if short_id not in known]

    if not pending:
        print(f"All {len(short_ids)} short links were already resolved.")
        return known

    print(f"Resolving {len(pending)} short links. {len(known)} were already known.")
    resolved = failed = missing = 0

    with httpx.Client(
        follow_redirects=False,
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        for position, short_id in enumerate(pending, start=1):
            slug, settled = resolve_one(short_id, client)

            if not settled:
                # Leave it out of the file so the next run tries again.
                failed += 1
            elif slug is None:
                known[short_id] = None
                missing += 1
            else:
                known[short_id] = slug
                resolved += 1

            if position % 200 == 0:
                print(f"  {position} of {len(pending)}")
                save_known(known)

            time.sleep(DELAY_BETWEEN_REQUESTS)

    save_known(known)
    print(f"Resolved {resolved}. Point nowhere: {missing}. Request failed: {failed}.")
    if failed:
        print(
            f"{failed} short links could not be reached and were not recorded, so the next "
            f"run will try them again. Re-run this script when the network is healthy."
        )
    return known


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "Usage: python scripts/resolve_short_links.py <export-dir-or-zip>\n"
            "Reads every Letterboxd URI in the export and resolves the short links.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    source = Path(sys.argv[1])
    text = ""
    if source.is_dir():
        for csv_file in source.rglob("*.csv"):
            text += csv_file.read_text(encoding="utf-8-sig", errors="replace")
    else:
        import zipfile

        with zipfile.ZipFile(source) as archive:
            for member in archive.namelist():
                if member.endswith(".csv"):
                    text += archive.read(member).decode("utf-8-sig", errors="replace")

    short_ids = SHORT_LINK_PATTERN.findall(text)
    if not short_ids:
        print(f"No boxd.it links found in {source}.", file=sys.stderr)
        raise SystemExit(1)

    resolve_all(short_ids)
    print(f"Stored in {SHORT_LINKS_FILE}.")


if __name__ == "__main__":
    main()
