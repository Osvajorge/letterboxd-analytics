"""Build the single JSON file the site reads.

Reads three local inputs, downloads nothing:

    data/history.json        every diary entry, plus the watchlist
    data/cache/tmdb.sqlite   raw TMDB payloads for the films in that history
    data/cache/lists/*.json  the sixteen curated lists, one file each

Writes docs/data/stats.json in the shape DATA_CONTRACT.md specifies, then prints
a summary table so one run can be checked at a glance.

Two rules run through the whole file:

    Missing input never fails the build. A module whose input is absent emits an
    empty array or null and the site renders an empty state. An empty TMDB cache
    still produces a valid stats.json.

    Entries with no watched date count towards totals but are excluded from every
    module that reads a calendar: by year, streaks, heatmap, rating drift. Mixing
    them in would put films in the wrong year without any visible error.

Films are joined across sources by Letterboxd slug. Nothing is matched by title.

This script reaches the network nowhere, and it imports nothing that does. It
reads the cached list files itself rather than importing the fetcher, so it runs
on a machine with no HTTP client installed.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from math import sqrt
from pathlib import Path
from statistics import median, quantiles
from typing import Any, Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.config import (
    CURATED_LISTS,
    HISTORY_FILE,
    LETTERBOXD_USER,
    LISTS_CACHE_DIR,
    STATS_FILE,
    TMDB_CACHE_FILE,
    ensure_dirs,
)

# A genre with one 5.0 rating would top every "highest rated" chart, so a group
# needs this many RATED films before it is ranked by average rating at all.
# Films in the group that carry no rating do not count towards it.
MINIMUM_FILMS_FOR_RATED_RANKING = 5

# How many rows each ranked module keeps. The site shows fewer; these caps only
# stop the file from carrying a long tail nobody reads.
TOP_PEOPLE_SHOWN = 50
TOP_GROUPS_SHOWN = 25
TOP_FILMS_SHOWN = 20
TOP_WORDS_SHOWN = 40

# The two rating scales this pipeline compares. Every comparison happens in
# five-star units, so a TMDB vote is divided by VOTES_PER_STAR first and a
# delta of -0.5 means half a star below the crowd.
MEMBER_RATING_MAX = 5.0
TMDB_VOTE_MAX = 10.0
VOTES_PER_STAR = TMDB_VOTE_MAX / MEMBER_RATING_MAX

# Runtime buckets, in minutes. Each number opens a new bucket.
RUNTIME_BUCKET_EDGES = (60, 90, 120, 150, 180)

MINUTES_PER_DAY = 24 * 60

# A heart on a film rated this low or lower is the disagreement the
# "liked but low" module is about.
LIKED_BUT_LOW_MAX_RATING = 3.0

# What counts as a background face: seen in at least this many films, and
# billed no higher than this position in the cast list on a typical one. TMDB
# numbers the cast from 0, so a larger number means further down the bill.
BACKGROUND_ACTOR_MINIMUM_FILMS = 3
BACKGROUND_ACTOR_MINIMUM_MEDIAN_BILLING = 10

# The crew roles the panel tracks, and the TMDB "job" values that fill each one.
CREW_ROLES = {
    "composer": ("Original Music Composer",),
    "cinematographer": ("Director of Photography",),
    "editor": ("Editor",),
    "writer": ("Writer", "Screenplay"),
}

WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

# Buckets for the gap between watching a film and logging it, shortest first.
# "logged early" holds the entries logged before the watched date, which happens
# when a member backdates a viewing.
LOGGING_LAG_BUCKET_LABELS = (
    "logged early",
    "same day",
    "1 to 2 days",
    "3 to 7 days",
    "8 to 30 days",
    "31 days and over",
)

# Words in a title that say nothing about the film. The history mixes English,
# Spanish, French, Italian, German and Portuguese titles, so each of those
# languages contributes its own articles, prepositions and conjunctions.
TITLE_STOPWORDS = frozenset(
    """
    a an the and or of in on at to for with from by is it its as not
    el la los las un una unos unas y o de del al en con por para es su sus lo
    le les une des du au aux et ou dans sur pour par avec ne pas est
    il gli i uno di da della dei delle e per
    der die das den dem des ein eine einen einem eines und oder von zu im mit
    os um uma uns umas do dos das no na nos nas em
    """.split()
)

# One letter on its own is never a subject word, and "I" would otherwise lead
# the count.
MINIMUM_TITLE_WORD_LENGTH = 2

# Letters only. Digits and punctuation are dropped, so "8 1/2" contributes no
# words rather than contributing "8" and "1".
TITLE_WORD_PATTERN = re.compile(r"[^\W\d_]+")

# Optional tables. The three tables in DATA_CONTRACT.md hold film-level data
# only, so two modules need facts that live one level up: how many films a
# director has made, and how many films a collection holds. If the enrichment
# step has cached those, this script uses them; if not, the modules stay empty
# rather than reporting a number derived from the wrong population.
PERSON_CREDITS_TABLE = "person_credits"
COLLECTIONS_TABLE = "collections"


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_history(path: Path) -> dict[str, Any]:
    """Read history.json, or return an empty history if it is not there yet.

    A missing file is not an error here. It means the one-time backfill has not
    run, and the site should still load.
    """
    if not path.exists():
        print(
            f"No watch history at {path}. "
            "Run scripts/backfill.py once to create it, then run this script again. "
            "Writing an empty stats file for now."
        )
        return {"username": LETTERBOXD_USER, "entries": [], "watchlist": []}

    try:
        history = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise SystemExit(
            f"Could not read {path}: the file is not valid JSON ({error}). "
            "Restore it from git or run scripts/backfill.py again."
        ) from error

    history.setdefault("entries", [])
    history.setdefault("watchlist", [])
    return history


def load_cached_lists(directory: Path = LISTS_CACHE_DIR) -> dict[str, dict[str, Any]]:
    """Read the cached curated lists, keyed by list id.

    The files are read straight from the cache directory rather than through
    scripts/fetch_lists.py, because that module imports an HTTP client at import
    time and this script must run without one.

    A list with no cache file is simply absent. A list whose file is damaged is
    reported and skipped, so one bad file narrows the progress module instead of
    failing the whole build.
    """
    lists: dict[str, dict[str, Any]] = {}

    for list_id, _, _ in CURATED_LISTS:
        path = directory / f"{list_id}.json"
        if not path.exists():
            continue
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as error:
            print(
                f"Skipping the curated list {list_id}: could not read {path} ({error}). "
                "Delete that file and run scripts/fetch_lists.py to rebuild it."
            )
            continue
        if isinstance(cached, dict):
            lists[list_id] = cached

    return lists


# Both the missing-cache and the unreadable-cache messages end with this, so
# the reader learns what an absent cache costs without either message having to
# repeat the list or the two lists drifting apart.
TMDB_CACHE_ABSENT_EFFECT = (
    "Until then every module built on film details stays empty: genres, "
    "countries, languages, cast, directors, studios, collections, runtime, "
    "rating bias, contrarian index, obscurity, release recency, crew, "
    "background actors, life in days and rating against runtime. The modules "
    "built on the history alone still report in full."
)


def open_tmdb_cache(path: Path) -> sqlite3.Connection | None:
    """Open the TMDB cache read only, or return None when it cannot be read.

    Read only matters: opening a missing path for writing would create an empty
    database and hide the fact that enrichment never ran.

    A cache that is present but not a database is treated as absent rather than
    fatal. The file holds nothing but downloaded TMDB responses, so the answer
    is always to delete it and download them again, and the rest of the panel
    still builds from the history in the meantime.
    """
    if not path.exists():
        print(
            f"No TMDB cache at {path}. "
            "Run scripts/enrich_tmdb.py to fill it. " + TMDB_CACHE_ABSENT_EFFECT
        )
        return None

    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row

    # sqlite opens the file without reading far enough to know it is a database,
    # so a damaged or truncated file only fails on the first real query. Ask one
    # cheap question here, where the answer can still be reported plainly.
    try:
        connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
    except sqlite3.DatabaseError as error:
        connection.close()
        print(
            f"The TMDB cache at {path} could not be read as a database ({error}). "
            f"Delete {path.name} and run scripts/enrich_tmdb.py to build it again. "
            "Treating it as missing for this run. " + TMDB_CACHE_ABSENT_EFFECT
        )
        return None

    return connection


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    """Report whether one table is present in the cache."""
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def load_payloads_by_slug(
    connection: sqlite3.Connection | None,
    slug_to_tmdb_id: dict[str, int],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Read the film and credit payloads for the films in the history.

    Returns two maps keyed by slug: film details, and credits. Films the cache
    does not hold are simply absent from both.

    Several slugs can point at one TMDB id, because Letterboxd keeps a renamed
    film reachable under its old slug and because a provisional slug built from
    a title can land on a film the history already holds. Each of them gets the
    payload, so no film loses its details on one of its slugs without a word.
    """
    films: dict[str, dict[str, Any]] = {}
    credits: dict[str, dict[str, Any]] = {}
    if connection is None:
        return films, credits

    slugs_per_tmdb_id: dict[int, list[str]] = defaultdict(list)
    for slug, tmdb_id in slug_to_tmdb_id.items():
        slugs_per_tmdb_id[tmdb_id].append(slug)

    def collect(table: str, into: dict[str, dict[str, Any]]) -> None:
        """Read one payload table and file each row under every slug it serves."""
        if not table_exists(connection, table):
            return
        for row in connection.execute(f"SELECT tmdb_id, payload FROM {table}"):
            slugs = slugs_per_tmdb_id.get(row["tmdb_id"])
            if not slugs:
                continue
            payload = decode_payload(row["payload"])
            if payload is None:
                continue
            for slug in slugs:
                into[slug] = payload

    collect("films", films)
    collect("credits", credits)

    return films, credits


def decode_payload(raw: Any) -> dict[str, Any] | None:
    """Turn one stored payload into a dictionary, or None if it is unusable."""
    if not isinstance(raw, str):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def resolve_tmdb_ids(
    entries: list[dict[str, Any]], connection: sqlite3.Connection | None
) -> dict[str, int]:
    """Map every film slug in the history to its TMDB id.

    RSS entries carry the id already. Export rows do not, so the cache's lookups
    table fills the gap, and the films table is the last resort.
    """
    slug_to_tmdb_id: dict[str, int] = {}

    for entry in entries:
        slug = entry.get("slug")
        tmdb_id = entry.get("tmdb_id")
        if isinstance(slug, str) and isinstance(tmdb_id, int):
            slug_to_tmdb_id.setdefault(slug, tmdb_id)

    if connection is None:
        return slug_to_tmdb_id

    for table, slug_column in (("lookups", "slug"), ("films", "slug")):
        if not table_exists(connection, table):
            continue
        for row in connection.execute(f"SELECT {slug_column}, tmdb_id FROM {table}"):
            slug = row[slug_column]
            tmdb_id = row["tmdb_id"]
            if isinstance(slug, str) and isinstance(tmdb_id, int):
                slug_to_tmdb_id.setdefault(slug, tmdb_id)

    return slug_to_tmdb_id


def load_director_filmography_sizes(
    connection: sqlite3.Connection | None,
) -> dict[int, int]:
    """Read how many films each director has directed in total.

    This is a person-level fact, so it comes from the optional person credits
    table. When that table is absent the result is empty and the completeness
    module emits an empty array, which is better than comparing what was seen
    against a filmography counted only from films already seen.
    """
    if connection is None or not table_exists(connection, PERSON_CREDITS_TABLE):
        return {}

    sizes: dict[int, int] = {}
    for row in connection.execute(f"SELECT * FROM {PERSON_CREDITS_TABLE}"):
        keys = row.keys()
        payload = decode_payload(row["payload"]) if "payload" in keys else None
        if payload is None:
            continue

        person_id = payload.get("id")
        if not isinstance(person_id, int) and "tmdb_id" in keys:
            person_id = row["tmdb_id"]
        if not isinstance(person_id, int):
            continue

        directed = {
            credit.get("id")
            for credit in payload.get("crew", [])
            if isinstance(credit, dict) and credit.get("job") == "Director"
        }
        directed.discard(None)
        if directed:
            sizes[person_id] = len(directed)

    return sizes


def load_collection_sizes(connection: sqlite3.Connection | None) -> dict[int, int]:
    """Read how many films each TMDB collection holds.

    Film details name the collection but not its size, so the size comes from a
    cached collection payload: either the optional collections table, or an
    expanded collection already stored inside a film payload.
    """
    if connection is None or not table_exists(connection, COLLECTIONS_TABLE):
        return {}

    sizes: dict[int, int] = {}
    for row in connection.execute(f"SELECT * FROM {COLLECTIONS_TABLE}"):
        payload = decode_payload(row["payload"]) if "payload" in row.keys() else None
        if payload is None:
            continue
        collection_id = payload.get("id")
        parts = payload.get("parts")
        if isinstance(collection_id, int) and isinstance(parts, list) and parts:
            sizes[collection_id] = len(parts)

    return sizes


def collection_size_in_film_payload(payload: dict[str, Any]) -> tuple[int, int] | None:
    """Return (collection id, size) when a film payload already carries the parts."""
    for key in ("collection", "belongs_to_collection"):
        candidate = payload.get(key)
        if not isinstance(candidate, dict):
            continue
        collection_id = candidate.get("id")
        parts = candidate.get("parts")
        if isinstance(collection_id, int) and isinstance(parts, list) and parts:
            return collection_id, len(parts)
    return None


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def parse_iso_date(value: Any) -> date | None:
    """Read a YYYY-MM-DD date, or None when the value is missing or malformed.

    Every date in these inputs uses the same layout: watched dates, logged dates
    and TMDB release dates alike.
    """
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_watched_date(value: Any) -> date | None:
    """Read an entry's watched date, or None when the entry has no usable one."""
    return parse_iso_date(value)


def release_date_of(payload: dict[str, Any] | None) -> date | None:
    """Read a film's TMDB release date, or None when the payload has none."""
    return parse_iso_date((payload or {}).get("release_date"))


def crowd_rating_in_stars(payload: dict[str, Any] | None) -> float | None:
    """Convert a film's TMDB vote average to the member's five-star scale.

    TMDB averages run 0 to 10 and the member rates 0.5 to 5.0, so the two are
    comparable only once the vote is halved. A vote of zero means nobody on TMDB
    has rated the film, not that the crowd disliked it, so it is dropped.
    """
    vote = (payload or {}).get("vote_average")
    if not isinstance(vote, (int, float)) or vote <= 0:
        return None
    return float(vote) / VOTES_PER_STAR


def rounded(value: float | None, places: int = 2) -> float | None:
    """Round a number for output, passing None through untouched."""
    return None if value is None else round(value, places)


def average(values: Iterable[float]) -> float | None:
    """Average a set of numbers, or None when there are none."""
    collected = list(values)
    return sum(collected) / len(collected) if collected else None


def median_days(values: list[int]) -> int:
    """Return the median of a set of day counts, as whole days.

    Rounded rather than truncated, because these gaps can be negative and
    truncation would pull a negative median towards zero.
    """
    return int(round(median(values)))


def rated_average(
    slugs: Iterable[str],
    rating_per_film: dict[str, float],
    minimum_rated_films: int = MINIMUM_FILMS_FOR_RATED_RANKING,
) -> tuple[float, int] | None:
    """Average the ratings inside one group, or None when too few films carry one.

    The minimum counts films that CARRY a rating, never films in the group. A
    genre of twenty films holding one rating is a sample of one: gating on the
    size of the group instead would let that single film top every
    highest-rated chart, and the history holds many unrated entries.

    Returns the average and the size of the rated sample it was built from, so
    callers report the sample rather than the group.
    """
    ratings = [rating_per_film[slug] for slug in slugs if slug in rating_per_film]
    if len(ratings) < minimum_rated_films:
        return None
    return sum(ratings) / len(ratings), len(ratings)


def pearson_correlation(pairs: list[tuple[float, float]]) -> float | None:
    """Correlate two paired measures, or None when the answer is undefined.

    Two pairs are the minimum, and both sides have to vary. A set where every
    film shares one runtime, or one rating, has no spread to correlate: the
    formula would divide by zero and report NaN, which the site cannot show.
    """
    if len(pairs) < 2:
        return None

    first = [float(x) for x, _ in pairs]
    second = [float(y) for _, y in pairs]
    mean_first = sum(first) / len(first)
    mean_second = sum(second) / len(second)

    spread_first = sum((x - mean_first) ** 2 for x in first)
    spread_second = sum((y - mean_second) ** 2 for y in second)
    if spread_first <= 0 or spread_second <= 0:
        return None

    together = sum((x - mean_first) * (y - mean_second) for x, y in zip(first, second))
    return together / sqrt(spread_first * spread_second)


def title_per_slug(entries: list[dict[str, Any]]) -> dict[str, str]:
    """Map every film slug to a title the site can print.

    Some export rows carry no title. The slug is always present and always
    readable, so it stands in and no row is left without a label.
    """
    titles: dict[str, str] = {}

    for entry in entries:
        slug = entry.get("slug")
        title = entry.get("title")
        if isinstance(slug, str) and isinstance(title, str) and title and slug not in titles:
            titles[slug] = title

    for entry in entries:
        slug = entry.get("slug")
        if isinstance(slug, str) and slug not in titles:
            titles[slug] = slug

    return titles


def latest_rating_per_film(entries: list[dict[str, Any]]) -> dict[str, float]:
    """Keep one member rating per film: the most recent one.

    A rewatched film can carry two different ratings. Averages over genres,
    directors and studios are about films, not viewings, so each film votes once
    and its newest rating wins. Undated entries sort before every dated one.
    """
    newest: dict[str, tuple[str, float]] = {}

    for entry in entries:
        slug = entry.get("slug")
        rating = entry.get("rating")
        if not isinstance(slug, str) or not isinstance(rating, (int, float)):
            continue
        stamp = entry.get("watched_date") or ""
        if slug not in newest or stamp >= newest[slug][0]:
            newest[slug] = (stamp, float(rating))

    return {slug: rating for slug, (_, rating) in newest.items()}


def film_release_year(entry: dict[str, Any], payload: dict[str, Any] | None) -> int | None:
    """Find a film's release year, preferring the value the history already holds."""
    year = entry.get("year")
    if isinstance(year, int):
        return year

    release_date = (payload or {}).get("release_date")
    if isinstance(release_date, str) and len(release_date) >= 4 and release_date[:4].isdigit():
        return int(release_date[:4])

    return None


def collect_group_members(
    films_by_slug: dict[str, dict[str, Any]],
    names_in_payload: Callable[[dict[str, Any]], list[str]],
) -> dict[str, set[str]]:
    """Group film slugs by a name taken from each film payload."""
    members: dict[str, set[str]] = defaultdict(set)
    for slug, payload in films_by_slug.items():
        for name in names_in_payload(payload):
            if name:
                members[name].add(slug)
    return members


def summarize_groups(
    members: dict[str, set[str]],
    rating_per_film: dict[str, float],
    limit: int = TOP_GROUPS_SHOWN,
) -> dict[str, list[dict[str, Any]]]:
    """Rank one dimension twice: by films watched, and by average rating.

    The two rankings count different things, on purpose. "most_watched" counts
    every film in the group. "highest_rated" counts only the films that carry a
    rating: a group needs MINIMUM_FILMS_FOR_RATED_RANKING of those to be ranked
    at all, and the "count" it reports is that rated sample, not the size of the
    group. Without that split, a genre of six films with one 5.0 rating would
    outrank a genre of six films all rated 4.0.
    """
    most_watched = sorted(
        ({"name": name, "count": len(slugs)} for name, slugs in members.items()),
        key=lambda row: (-row["count"], row["name"]),
    )[:limit]

    rated: list[dict[str, Any]] = []
    for name, slugs in members.items():
        summary = rated_average(slugs, rating_per_film)
        if summary is None:
            continue
        group_average, rated_films = summary
        rated.append({"name": name, "average": rounded(group_average), "count": rated_films})

    highest_rated = sorted(rated, key=lambda row: (-row["average"], row["name"]))[:limit]

    return {"most_watched": most_watched, "highest_rated": highest_rated}


# ---------------------------------------------------------------------------
# Time based modules
# ---------------------------------------------------------------------------


def longest_run_of_consecutive_weeks(watched_dates: Iterable[date]) -> int:
    """Count the longest run of consecutive calendar weeks holding at least one entry.

    The reference panel counts weeks, not days. A week with one entry and a week
    with six each count once, and the run breaks only when a whole week passes
    with nothing logged. Weeks start on Monday.
    """
    week_starts = sorted({day - timedelta(days=day.weekday()) for day in watched_dates})
    if not week_starts:
        return 0

    longest = 1
    current = 1
    for earlier, later in zip(week_starts, week_starts[1:]):
        if later - earlier == timedelta(days=7):
            current += 1
            longest = max(longest, current)
        else:
            current = 1

    return longest


def count_multi_film_days(watched_dates: Iterable[date]) -> int:
    """Count calendar days holding two or more diary entries."""
    per_day = Counter(watched_dates)
    return sum(1 for count in per_day.values() if count >= 2)


def build_by_year(dated_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Summarize each watched year: distinct films, diary entries, rating spread.

    "films" counts distinct films watched that year and "diary" counts entries,
    so a film watched twice in one year adds one to the first and two to the
    second.
    """
    films_per_year: dict[int, set[str]] = defaultdict(set)
    entries_per_year: Counter[int] = Counter()
    ratings_per_year: dict[int, Counter[str]] = defaultdict(Counter)

    for entry in dated_entries:
        year = parse_watched_date(entry["watched_date"]).year
        entries_per_year[year] += 1

        slug = entry.get("slug")
        if isinstance(slug, str):
            films_per_year[year].add(slug)

        rating = entry.get("rating")
        if isinstance(rating, (int, float)):
            ratings_per_year[year][f"{float(rating):.1f}"] += 1

    def ratings_in_order(year: int) -> dict[str, int]:
        """Order one year's rating counts from 0.5 upwards, keeping only ratings given."""
        return dict(sorted(ratings_per_year[year].items(), key=lambda item: float(item[0])))

    return [
        {
            "year": year,
            "films": len(films_per_year.get(year, ())),
            "diary": entries_per_year[year],
            "ratings": ratings_in_order(year),
        }
        for year in sorted(entries_per_year, reverse=True)
    ]


def build_heatmap(dated_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Count diary entries per calendar day, oldest day first."""
    per_day = Counter(entry["watched_date"] for entry in dated_entries)
    return [{"date": day, "count": per_day[day]} for day in sorted(per_day)]


def build_rating_drift(dated_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Average the ratings given in each year, oldest year first."""
    ratings_per_year: dict[int, list[float]] = defaultdict(list)

    for entry in dated_entries:
        rating = entry.get("rating")
        if isinstance(rating, (int, float)):
            year = parse_watched_date(entry["watched_date"]).year
            ratings_per_year[year].append(float(rating))

    return [
        {"year": year, "average": rounded(average(ratings_per_year[year]))}
        for year in sorted(ratings_per_year)
    ]


# ---------------------------------------------------------------------------
# Film based modules
# ---------------------------------------------------------------------------


def build_decades(
    release_year_per_film: dict[str, int], rating_per_film: dict[str, float]
) -> list[dict[str, Any]]:
    """Count films and average their ratings per release decade, oldest first."""
    films_per_decade: dict[int, set[str]] = defaultdict(set)

    for slug, year in release_year_per_film.items():
        films_per_decade[(year // 10) * 10].add(slug)

    return [
        {
            "decade": decade,
            "films": len(slugs),
            "average_rating": rounded(
                average(rating_per_film[slug] for slug in slugs if slug in rating_per_film)
            ),
        }
        for decade, slugs in sorted(films_per_decade.items())
    ]


def build_decade_gaps(release_year_per_film: dict[str, int], today: date) -> list[int]:
    """List decades with no films at all, from the earliest film seen to now."""
    if not release_year_per_film:
        return []

    decades_seen = {(year // 10) * 10 for year in release_year_per_film.values()}
    earliest = min(decades_seen)
    current = (today.year // 10) * 10

    return [decade for decade in range(earliest, current + 1, 10) if decade not in decades_seen]


def build_world_map(films_by_slug: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Count films per production country, keyed by ISO 3166-1 code."""
    counts: Counter[str] = Counter()
    names: dict[str, str] = {}

    for payload in films_by_slug.values():
        for country in payload.get("production_countries", []) or []:
            if not isinstance(country, dict):
                continue
            code = country.get("iso_3166_1")
            if not isinstance(code, str):
                continue
            counts[code] += 1
            names.setdefault(code, country.get("name") or code)

    return [
        {"iso_3166_1": code, "name": names[code], "count": count}
        for code, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def build_cast(
    credits_by_slug: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rank credited actors by how many of the watched films they appear in."""
    films_per_person: dict[int, set[str]] = defaultdict(set)
    names: dict[int, str] = {}
    profiles: dict[int, str | None] = {}

    for slug, payload in credits_by_slug.items():
        for member in payload.get("cast", []) or []:
            if not isinstance(member, dict):
                continue
            person_id = member.get("id")
            if not isinstance(person_id, int):
                continue
            films_per_person[person_id].add(slug)
            names.setdefault(person_id, member.get("name") or "")
            profiles.setdefault(person_id, member.get("profile_path"))

    ranked = sorted(
        films_per_person.items(), key=lambda item: (-len(item[1]), names.get(item[0], ""))
    )

    return [
        {
            "tmdb_id": person_id,
            "name": names[person_id],
            "count": len(slugs),
            "profile_path": profiles.get(person_id),
        }
        for person_id, slugs in ranked[:TOP_PEOPLE_SHOWN]
    ]


def collect_directors(
    credits_by_slug: dict[str, dict[str, Any]],
) -> tuple[dict[int, set[str]], dict[int, str], dict[int, str | None]]:
    """Group watched films by director, and remember each director's name and photo."""
    films_per_director: dict[int, set[str]] = defaultdict(set)
    names: dict[int, str] = {}
    profiles: dict[int, str | None] = {}

    for slug, payload in credits_by_slug.items():
        for member in payload.get("crew", []) or []:
            if not isinstance(member, dict) or member.get("job") != "Director":
                continue
            person_id = member.get("id")
            if not isinstance(person_id, int):
                continue
            films_per_director[person_id].add(slug)
            names.setdefault(person_id, member.get("name") or "")
            profiles.setdefault(person_id, member.get("profile_path"))

    return films_per_director, names, profiles


def build_directors(
    films_per_director: dict[int, set[str]],
    names: dict[int, str],
    profiles: dict[int, str | None],
    rating_per_film: dict[str, float],
) -> list[dict[str, Any]]:
    """Rank directors by films watched, with the average rating given to each."""
    ranked = sorted(
        films_per_director.items(), key=lambda item: (-len(item[1]), names.get(item[0], ""))
    )

    return [
        {
            "tmdb_id": person_id,
            "name": names[person_id],
            "count": len(slugs),
            "average_rating": rounded(
                average(rating_per_film[slug] for slug in slugs if slug in rating_per_film)
            ),
            "profile_path": profiles.get(person_id),
        }
        for person_id, slugs in ranked[:TOP_PEOPLE_SHOWN]
    ]


def build_director_completeness(
    films_per_director: dict[int, set[str]],
    names: dict[int, str],
    filmography_sizes: dict[int, int],
) -> list[dict[str, Any]]:
    """Compare films seen against each director's full filmography.

    Without cached filmography sizes there is no honest denominator, so the
    module emits an empty array instead of a number counted from the wrong set.
    """
    if not filmography_sizes:
        return []

    rows = [
        {
            "name": names.get(person_id, ""),
            "seen": len(slugs),
            "filmography": filmography_sizes[person_id],
        }
        for person_id, slugs in films_per_director.items()
        if person_id in filmography_sizes
    ]

    rows.sort(key=lambda row: (-row["seen"] / row["filmography"], -row["seen"], row["name"]))
    return rows[:TOP_PEOPLE_SHOWN]


def build_studios(
    films_by_slug: dict[str, dict[str, Any]], rating_per_film: dict[str, float]
) -> list[dict[str, Any]]:
    """Rank production companies by films watched, with the average rating given."""
    films_per_studio = collect_group_members(
        films_by_slug,
        lambda payload: [
            company.get("name")
            for company in payload.get("production_companies", []) or []
            if isinstance(company, dict) and company.get("name")
        ],
    )

    ranked = sorted(films_per_studio.items(), key=lambda item: (-len(item[1]), item[0]))

    return [
        {
            "name": name,
            "count": len(slugs),
            "average_rating": rounded(
                average(rating_per_film[slug] for slug in slugs if slug in rating_per_film)
            ),
        }
        for name, slugs in ranked[:TOP_GROUPS_SHOWN]
    ]


def build_collections(
    films_by_slug: dict[str, dict[str, Any]], collection_sizes: dict[int, int]
) -> list[dict[str, Any]]:
    """Count how much of each TMDB collection the history covers.

    "seen" counts collection members in the history. "total" is the collection
    size reported by TMDB, so a collection whose size was never cached is left
    out rather than reported as complete by accident.
    """
    sizes = dict(collection_sizes)
    seen_per_collection: Counter[int] = Counter()
    names: dict[int, str] = {}

    for payload in films_by_slug.values():
        expanded = collection_size_in_film_payload(payload)
        if expanded is not None:
            expanded_id, expanded_size = expanded
            sizes.setdefault(expanded_id, expanded_size)

        collection = payload.get("belongs_to_collection")
        if not isinstance(collection, dict):
            continue
        collection_id = collection.get("id")
        if not isinstance(collection_id, int):
            continue
        seen_per_collection[collection_id] += 1
        names.setdefault(collection_id, collection.get("name") or "")

    rows = [
        {"name": names[collection_id], "seen": seen, "total": sizes[collection_id]}
        for collection_id, seen in seen_per_collection.items()
        if collection_id in sizes
    ]

    rows.sort(key=lambda row: (-row["seen"] / row["total"], -row["seen"], row["name"]))
    return rows


def build_runtime(
    entries: list[dict[str, Any]], runtime_per_film: dict[str, int]
) -> dict[str, Any]:
    """Report total minutes watched, the median film length, and the length spread.

    Total minutes counts every diary entry, because a rewatch costs the time
    again. Median and distribution describe the films themselves, so each film
    counts once.
    """
    total_minutes = sum(
        runtime_per_film[entry["slug"]]
        for entry in entries
        if isinstance(entry.get("slug"), str) and entry["slug"] in runtime_per_film
    )

    lengths = sorted(runtime_per_film.values())
    buckets = Counter(runtime_bucket_label(length) for length in lengths)

    return {
        "total_minutes": total_minutes,
        "median": int(median(lengths)) if lengths else None,
        "distribution": [
            {"bucket": label, "count": buckets.get(label, 0)}
            for label in runtime_bucket_labels()
            if buckets.get(label, 0) > 0
        ],
    }


def runtime_bucket_labels() -> list[str]:
    """Name every runtime bucket, shortest first."""
    edges = RUNTIME_BUCKET_EDGES
    labels = [f"under {edges[0]}"]
    labels += [f"{lower}-{upper - 1}" for lower, upper in zip(edges, edges[1:])]
    labels.append(f"{edges[-1]} and over")
    return labels


def runtime_bucket_label(minutes: int) -> str:
    """Place one runtime in its bucket."""
    edges = RUNTIME_BUCKET_EDGES
    if minutes < edges[0]:
        return f"under {edges[0]}"
    for lower, upper in zip(edges, edges[1:]):
        if minutes < upper:
            return f"{lower}-{upper - 1}"
    return f"{edges[-1]} and over"


def build_rating_bias(
    rating_per_film: dict[str, float], films_by_slug: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    """Compare the member's ratings against the TMDB crowd on the same films.

    The delta is in stars, the unit the contract and the site both use, so the
    TMDB vote is halved before subtracting and a delta of -0.5 means the member
    rates half a star below the crowd. The two averages stay on their native
    scales, 0.5 to 5.0 and 0 to 10, because that is how a reader recognises them.
    """
    member_ratings: list[float] = []
    crowd_ratings_in_stars: list[float] = []
    deltas_in_stars: list[float] = []

    for slug, member_rating in rating_per_film.items():
        crowd_rating = crowd_rating_in_stars(films_by_slug.get(slug))
        if crowd_rating is None:
            continue
        member_ratings.append(member_rating)
        crowd_ratings_in_stars.append(crowd_rating)
        deltas_in_stars.append(member_rating - crowd_rating)

    if not deltas_in_stars:
        return None

    crowd_average_in_stars = average(crowd_ratings_in_stars)

    return {
        "member_average": rounded(average(member_ratings)),
        # Reported on the ten-point scale TMDB shows, which is the star average
        # scaled back up.
        "tmdb_average": rounded(crowd_average_in_stars * VOTES_PER_STAR),
        "delta": rounded(average(deltas_in_stars)),
    }


def build_watchlist(
    watchlist: list[dict[str, Any]], history_slugs: set[str], today: date
) -> dict[str, Any]:
    """Report watchlist size, how old its entries are, and how much of it was watched.

    Conversion rate is the share of the current watchlist whose films already
    appear in the history. It cannot see films removed from the watchlist after
    being watched, because nothing in these inputs records that.

    estimated_date_share says how much of the age figure is guesswork. The
    public watchlist pages do not say when a film was added, so the weekly
    reader stamps the day it first saw the film, which is an upper bound and not
    the real date. Only the export carries real dates. The site reads this share
    to decide whether it may present the ages as measurements at all: with no
    export loaded the share is 1.0 and every age is 0.
    """
    ages_in_days = [
        (today - added).days
        for added in (parse_watched_date(item.get("added_date")) for item in watchlist)
        if added is not None
    ]

    watchlist_slugs = {item["slug"] for item in watchlist if isinstance(item.get("slug"), str)}
    already_seen = watchlist_slugs & history_slugs

    # Only the flag set to true marks an estimate. A watchlist written before
    # the flag existed came from the export, so a missing flag counts as a real
    # date rather than as an unknown.
    estimated_dates = sum(1 for item in watchlist if item.get("added_date_estimated") is True)

    return {
        "size": len(watchlist),
        "median_age_days": int(median(ages_in_days)) if ages_in_days else None,
        "conversion_rate": (
            rounded(len(already_seen) / len(watchlist_slugs), 3) if watchlist_slugs else None
        ),
        # Measured against the whole watchlist, so it stays the share of the
        # ages the site is about to print, not the share of some smaller set.
        "estimated_date_share": (
            rounded(estimated_dates / len(watchlist), 3) if watchlist else None
        ),
    }


def build_list_progress(
    cached_lists: dict[str, dict[str, Any]], history_slugs: set[str]
) -> list[dict[str, Any]]:
    """Intersect the history with each curated list. Slugs only, no title matching."""
    titles = {list_id: title for list_id, title, _ in CURATED_LISTS}
    progress: list[dict[str, Any]] = []

    for list_id, cached in cached_lists.items():
        slugs = {
            film["slug"]
            for film in cached.get("films", [])
            if isinstance(film, dict) and isinstance(film.get("slug"), str)
        }
        progress.append(
            {
                "id": list_id,
                "title": cached.get("title") or titles.get(list_id, list_id),
                "seen": len(slugs & history_slugs),
                "total": len(slugs),
            }
        )

    return progress


# ---------------------------------------------------------------------------
# Extras: the member's ratings against the crowd
# ---------------------------------------------------------------------------


def build_contrarian_index(
    rating_per_film: dict[str, float],
    films_by_slug: dict[str, dict[str, Any]],
    titles: dict[str, str],
    release_year_per_film: dict[str, int],
) -> dict[str, list[dict[str, Any]]]:
    """List the films rated furthest from the TMDB crowd, in both directions.

    Both ratings are in stars, so a delta of 2.4 means the member rated the film
    2.4 stars above the crowd. Films nobody on TMDB has rated carry no crowd
    rating and are left out; without a TMDB cache both lists are empty.
    """
    rows: list[dict[str, Any]] = []

    for slug, member_rating in rating_per_film.items():
        crowd_rating = crowd_rating_in_stars(films_by_slug.get(slug))
        if crowd_rating is None:
            continue
        rows.append(
            {
                "slug": slug,
                "title": titles.get(slug, slug),
                "year": release_year_per_film.get(slug),
                "member_rating": member_rating,
                "crowd_rating": rounded(crowd_rating),
                "delta": rounded(member_rating - crowd_rating),
            }
        )

    hotter = sorted(
        (row for row in rows if row["delta"] > 0), key=lambda row: (-row["delta"], row["title"])
    )
    colder = sorted(
        (row for row in rows if row["delta"] < 0), key=lambda row: (row["delta"], row["title"])
    )

    return {
        "hotter_than_crowd": hotter[:TOP_FILMS_SHOWN],
        "colder_than_crowd": colder[:TOP_FILMS_SHOWN],
    }


def build_obscurity(
    films_by_slug: dict[str, dict[str, Any]], titles: dict[str, str]
) -> dict[str, Any] | None:
    """Describe how widely known the films watched are, by TMDB vote count.

    Vote count is the nearest thing TMDB reports to an audience size. The
    quartiles are the first quarter, the median and the third quarter, so the
    site can show the spread rather than a single number.
    """
    votes_per_film = {
        slug: payload["vote_count"]
        for slug, payload in films_by_slug.items()
        if isinstance(payload.get("vote_count"), int) and payload["vote_count"] > 0
    }
    if not votes_per_film:
        return None

    counts = sorted(votes_per_film.values())
    if len(counts) >= 2:
        quartiles = [int(round(cut)) for cut in quantiles(counts, n=4)]
    else:
        # One film has no spread, so every quartile sits on the same value.
        quartiles = [counts[0], counts[0], counts[0]]

    ordered = sorted(
        votes_per_film, key=lambda slug: (votes_per_film[slug], titles.get(slug, slug))
    )

    def rows(slugs: list[str]) -> list[dict[str, Any]]:
        """Render a run of film slugs as obscurity rows."""
        return [
            {"slug": slug, "title": titles.get(slug, slug), "vote_count": votes_per_film[slug]}
            for slug in slugs
        ]

    return {
        "median_vote_count": int(round(median(counts))),
        "quartiles": quartiles,
        "most_obscure": rows(ordered[:TOP_FILMS_SHOWN]),
        "most_popular": rows(list(reversed(ordered))[:TOP_FILMS_SHOWN]),
    }


def build_half_star_usage(rating_per_film: dict[str, float]) -> dict[str, Any] | None:
    """Report how often the member uses half stars, and the full rating spread.

    Each film votes once, with its most recent rating, so a rewatch does not
    count the same opinion twice. Every step from 0.5 to 5.0 appears in the
    distribution, including steps never used, because a bar chart with steps
    missing reads as a narrower scale than the member actually has.
    """
    ratings = list(rating_per_film.values())
    if not ratings:
        return None

    # A half star is a rating that is not a whole number, such as 3.5.
    half_stars = [rating for rating in ratings if abs(rating - round(rating)) > 1e-9]

    steps = [step / 2 for step in range(1, 11)]
    given = Counter(f"{rating:.1f}" for rating in ratings)

    return {
        "half_star_share": rounded(len(half_stars) / len(ratings), 3),
        "distribution": [
            {"rating": step, "count": given.get(f"{step:.1f}", 0)} for step in steps
        ],
    }


def build_liked_but_low(
    entries: list[dict[str, Any]], rating_per_film: dict[str, float], titles: dict[str, str]
) -> list[dict[str, Any]]:
    """List the films the member hearted and still rated low.

    A heart and a low rating disagree, and the disagreement is the point. A film
    counts as liked when any viewing of it was hearted, and its rating is the
    most recent one.
    """
    liked_slugs = {
        entry["slug"]
        for entry in entries
        if entry.get("liked") is True and isinstance(entry.get("slug"), str)
    }

    rows = [
        {"slug": slug, "title": titles.get(slug, slug), "rating": rating_per_film[slug]}
        for slug in liked_slugs
        if slug in rating_per_film and rating_per_film[slug] <= LIKED_BUT_LOW_MAX_RATING
    ]

    rows.sort(key=lambda row: (row["rating"], row["title"]))
    return rows[:TOP_FILMS_SHOWN]


# ---------------------------------------------------------------------------
# Extras: when films were watched
# ---------------------------------------------------------------------------


def build_release_recency(
    dated_entries: list[dict[str, Any]], films_by_slug: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    """Measure how long after its release each film was watched.

    A negative gap means the film was seen before its release date, at a
    festival or a preview screening. Those are kept as they are: clamping them
    to zero would hide a real viewing pattern.

    Every viewing counts, not every film, because a rewatch twenty years later
    is genuinely a late viewing. The release date comes from TMDB, so without a
    cache this module emits null.
    """
    gaps: list[int] = []
    gaps_per_year: dict[int, list[int]] = defaultdict(list)

    for entry in dated_entries:
        slug = entry.get("slug")
        watched = parse_watched_date(entry["watched_date"])
        released = release_date_of(films_by_slug.get(slug) if isinstance(slug, str) else None)
        if watched is None or released is None:
            continue
        gap = (watched - released).days
        gaps.append(gap)
        gaps_per_year[watched.year].append(gap)

    if not gaps:
        return None

    return {
        "median_days_after_release": median_days(gaps),
        "by_year": [
            {"year": year, "median_days": median_days(year_gaps)}
            for year, year_gaps in sorted(gaps_per_year.items())
        ],
    }


def build_longest_drought(watched_dates: list[date]) -> dict[str, Any] | None:
    """Find the longest stretch with nothing watched, and when it ran.

    Only days that carry an entry are known, so the drought is the widest gap
    between two consecutive watched days. Fewer than two dated entries leave no
    stretch to measure, and the module emits null.
    """
    days = sorted(set(watched_dates))
    if len(days) < 2:
        return None

    start, end = max(zip(days, days[1:]), key=lambda pair: (pair[1] - pair[0]).days)

    return {"days": (end - start).days, "from": start.isoformat(), "to": end.isoformat()}


def build_weekday_profile(watched_dates: list[date]) -> list[dict[str, Any]]:
    """Count diary entries per weekday, Monday first.

    Every weekday appears once there is any dated entry, including weekdays with
    nothing logged, so the chart keeps a full week of bars.
    """
    if not watched_dates:
        return []

    per_weekday = Counter(day.weekday() for day in watched_dates)
    return [
        {"weekday": name, "count": per_weekday.get(index, 0)}
        for index, name in enumerate(WEEKDAY_NAMES)
    ]


def build_month_seasonality(watched_dates: list[date]) -> list[dict[str, Any]]:
    """Count diary entries per calendar month, January first.

    Months are pooled across every year, so this answers which months the member
    watches most, not which month of which year.
    """
    if not watched_dates:
        return []

    per_month = Counter(day.month for day in watched_dates)
    return [{"month": month, "count": per_month.get(month, 0)} for month in range(1, 13)]


def logging_lag_bucket_label(days: int) -> str:
    """Place one watch-to-log gap in its bucket."""
    if days < 0:
        return "logged early"
    if days == 0:
        return "same day"
    if days <= 2:
        return "1 to 2 days"
    if days <= 7:
        return "3 to 7 days"
    if days <= 30:
        return "8 to 30 days"
    return "31 days and over"


def build_logging_lag(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Measure how long after watching a film the member logged it.

    This needs both dates, and only the export carries the logged date. A
    history read from RSS alone emits null, never a lag of zero: zero would say
    the member logs everything the same day, which is a claim the data does not
    support.
    """
    lags: list[int] = []

    for entry in entries:
        watched = parse_watched_date(entry.get("watched_date"))
        logged = parse_iso_date(entry.get("logged_date"))
        if watched is None or logged is None:
            continue
        lags.append((logged - watched).days)

    if not lags:
        return None

    per_bucket = Counter(logging_lag_bucket_label(lag) for lag in lags)

    return {
        "median_days": median_days(lags),
        "distribution": [
            {"bucket": label, "count": per_bucket[label]}
            for label in LOGGING_LAG_BUCKET_LABELS
            if per_bucket.get(label)
        ],
    }


# ---------------------------------------------------------------------------
# Extras: the people behind the films
# ---------------------------------------------------------------------------


def build_director_luck(
    films_per_director: dict[int, set[str]],
    names: dict[int, str],
    rating_per_film: dict[str, float],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Rank directors by the average rating the member gave them, both ends.

    Returns the luckiest first and the unluckiest first. "films" is the number
    of RATED films behind the average, which is also what the minimum sample
    counts: a director with six watched films and one rating is a sample of one.

    The two ends are a partition: no director can appear in both. Sorting one
    set of rows twice would put every director in both columns whenever fewer
    than twice TOP_GROUPS_SHOWN of them clear the minimum sample, and a panel
    that names the same person as best and worst rated is telling the reader
    nothing. Each end therefore takes at most half the ranking, so a short
    ranking yields fewer rows instead of repeated ones, and a single qualifying
    director yields none: one row cannot be split into a top half and a bottom
    half.
    """
    rows: list[dict[str, Any]] = []

    for person_id, slugs in films_per_director.items():
        summary = rated_average(slugs, rating_per_film)
        if summary is None:
            continue
        person_average, rated_films = summary
        rows.append(
            {
                "name": names.get(person_id, ""),
                "films": rated_films,
                "average_rating": rounded(person_average),
            }
        )

    ranked = sorted(rows, key=lambda row: (-row["average_rating"], row["name"]))

    # At most half the ranking to each end, which is what keeps one director out
    # of both. Fewer than two directors leaves nothing to divide, and returning
    # here also keeps the slice below away from a count of zero, where a
    # negative index would take the whole ranking and restore the duplication.
    rows_per_end = min(TOP_GROUPS_SHOWN, len(ranked) // 2)
    if rows_per_end == 0:
        return [], []

    luckiest = ranked[:rows_per_end]
    unluckiest = sorted(
        ranked[-rows_per_end:], key=lambda row: (row["average_rating"], row["name"])
    )

    return luckiest, unluckiest


def build_background_actor(
    credits_by_slug: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """List the faces seen often and billed low: the working actors of a history.

    TMDB orders a cast list by billing and numbers it from 0, so the lead is 0
    and a larger number is further down the bill. An actor who appears in
    several watched films and sits far down the bill on a typical one is someone
    the member keeps seeing without ever seeing them lead.

    Ranked by how often they turn up first, then by how far down they are
    billed, because both halves are the signal.
    """
    billings_per_person: dict[int, list[int]] = defaultdict(list)
    films_per_person: dict[int, set[str]] = defaultdict(set)
    names: dict[int, str] = {}

    for slug, payload in credits_by_slug.items():
        for member in payload.get("cast", []) or []:
            if not isinstance(member, dict):
                continue
            person_id = member.get("id")
            billing = member.get("order")
            if not isinstance(person_id, int) or not isinstance(billing, int):
                continue
            # One billing per film, so a double credit is not a second sighting.
            if slug in films_per_person[person_id]:
                continue
            films_per_person[person_id].add(slug)
            billings_per_person[person_id].append(billing)
            names.setdefault(person_id, member.get("name") or "")

    rows: list[dict[str, Any]] = []
    for person_id, billings in billings_per_person.items():
        if len(billings) < BACKGROUND_ACTOR_MINIMUM_FILMS:
            continue
        typical_billing = median(billings)
        if typical_billing < BACKGROUND_ACTOR_MINIMUM_MEDIAN_BILLING:
            continue
        rows.append(
            {
                "tmdb_id": person_id,
                "name": names.get(person_id, ""),
                "count": len(billings),
                "median_billing": int(round(typical_billing)),
            }
        )

    rows.sort(key=lambda row: (-row["count"], -row["median_billing"], row["name"]))
    return rows[:TOP_PEOPLE_SHOWN]


def build_crew_most_watched(
    credits_by_slug: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Rank the crew behind the camera by how many watched films they worked on.

    TMDB names the role in each crew member's "job" field, and CREW_ROLES maps
    those job names onto the four roles the panel shows. A person credited twice
    on one film counts that film once. Every role is present even when empty.
    """
    role_of_job = {job: role for role, jobs in CREW_ROLES.items() for job in jobs}
    films_per_person: dict[str, dict[int, set[str]]] = {
        role: defaultdict(set) for role in CREW_ROLES
    }
    names: dict[int, str] = {}

    for slug, payload in credits_by_slug.items():
        for member in payload.get("crew", []) or []:
            if not isinstance(member, dict):
                continue
            job = member.get("job")
            person_id = member.get("id")
            role = role_of_job.get(job) if isinstance(job, str) else None
            if role is None or not isinstance(person_id, int):
                continue
            films_per_person[role][person_id].add(slug)
            names.setdefault(person_id, member.get("name") or "")

    def ranked(people: dict[int, set[str]]) -> list[dict[str, Any]]:
        """Order one role's people by films watched, then by name."""
        order = sorted(people.items(), key=lambda item: (-len(item[1]), names.get(item[0], "")))
        return [
            {"tmdb_id": person_id, "name": names.get(person_id, ""), "count": len(slugs)}
            for person_id, slugs in order[:TOP_PEOPLE_SHOWN]
        ]

    return {role: ranked(people) for role, people in films_per_person.items()}


# ---------------------------------------------------------------------------
# Extras: the shape of the films themselves
# ---------------------------------------------------------------------------


def build_life_in_days(total_minutes: int, today: date) -> dict[str, Any] | None:
    """State the total watch time in days, and when a marathon of it would end.

    "would_end_on" answers one question: if every viewing in the history were
    played back to back starting today, what date would the last one finish on.
    Today comes from the caller, the same date the whole document is stamped
    with. Date arithmetic keeps whole days, so a marathon that finishes part way
    through a day is reported as ending on that day.
    """
    if total_minutes <= 0:
        return None

    return {
        "days": rounded(total_minutes / MINUTES_PER_DAY, 1),
        "would_end_on": (today + timedelta(minutes=total_minutes)).isoformat(),
    }


def build_extremes(
    runtime_per_film: dict[str, int],
    release_year_per_film: dict[str, int],
    titles: dict[str, str],
) -> dict[str, Any]:
    """Name the shortest and longest films watched, and the oldest and newest.

    Each key is null on its own when the fact behind it is missing, so a history
    with no cached runtimes still reports its oldest and newest film. Ties are
    broken by title, so the same run always names the same film.
    """

    def by_runtime(slug: str) -> dict[str, Any]:
        """Render one film as a runtime extreme."""
        return {"slug": slug, "title": titles.get(slug, slug), "runtime": runtime_per_film[slug]}

    def by_year(slug: str) -> dict[str, Any]:
        """Render one film as a release-year extreme."""
        return {"slug": slug, "title": titles.get(slug, slug), "year": release_year_per_film[slug]}

    shortest = longest = None
    if runtime_per_film:
        by_length = sorted(
            runtime_per_film, key=lambda slug: (runtime_per_film[slug], titles.get(slug, slug))
        )
        shortest = by_runtime(by_length[0])
        longest = by_runtime(by_length[-1])

    oldest = newest = None
    if release_year_per_film:
        by_age = sorted(
            release_year_per_film,
            key=lambda slug: (release_year_per_film[slug], titles.get(slug, slug)),
        )
        oldest = by_year(by_age[0])
        newest = by_year(by_age[-1])

    return {"shortest": shortest, "longest": longest, "oldest": oldest, "newest": newest}


def build_rating_vs_runtime(
    runtime_per_film: dict[str, int], rating_per_film: dict[str, float]
) -> dict[str, Any]:
    """Ask whether the member rates longer films higher.

    The correlation is Pearson's r over the films that carry both a runtime and
    a rating, and it is null when there are too few of them or when either side
    never varies. The buckets use the same runtime ranges as the runtime
    module, and each one reports how many rated films stand behind its average.
    """
    pairs = [
        (float(runtime_per_film[slug]), rating_per_film[slug])
        for slug in runtime_per_film
        if slug in rating_per_film
    ]

    ratings_per_bucket: dict[str, list[float]] = defaultdict(list)
    for runtime, rating in pairs:
        ratings_per_bucket[runtime_bucket_label(int(runtime))].append(rating)

    return {
        "correlation": rounded(pearson_correlation(pairs)),
        "buckets": [
            {
                "range": label,
                "films": len(ratings_per_bucket[label]),
                "average_rating": rounded(average(ratings_per_bucket[label])),
            }
            for label in runtime_bucket_labels()
            if ratings_per_bucket.get(label)
        ],
    }


def build_title_words(titles: dict[str, str]) -> list[dict[str, Any]]:
    """Count the words that recur across the titles of the films watched.

    Articles, prepositions and other joining words are dropped in every language
    the history mixes, because "the" and "de" would otherwise take the top rows
    and say nothing. Each film counts once. A title that holds no words at all,
    such as "8 1/2", simply contributes nothing.
    """
    counts: Counter[str] = Counter()

    for title in titles.values():
        for word in TITLE_WORD_PATTERN.findall(title.lower()):
            if len(word) >= MINIMUM_TITLE_WORD_LENGTH and word not in TITLE_STOPWORDS:
                counts[word] += 1

    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [{"word": word, "count": count} for word, count in ranked[:TOP_WORDS_SHOWN]]


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def build_stats_document(
    history: dict[str, Any],
    connection: sqlite3.Connection | None,
    cached_lists: dict[str, dict[str, Any]],
    today: date | None = None,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Compute every module and return the finished stats document.

    Returns two things: the document itself, in the shape DATA_CONTRACT.md
    specifies, and a small set of counts the printed summary uses to show what
    was left out of which module.

    Takes loaded inputs rather than paths so the same function can run against
    test data. Any missing input narrows the output; none of them fails it.
    """
    today = today or date.today()
    entries = [entry for entry in history.get("entries", []) if isinstance(entry, dict)]
    watchlist = [item for item in history.get("watchlist", []) if isinstance(item, dict)]

    # The split every time based module depends on. Undated entries stay in the
    # totals and in the film based modules, and appear nowhere on a calendar.
    dated_entries = [entry for entry in entries if parse_watched_date(entry.get("watched_date"))]
    watched_dates = [parse_watched_date(entry["watched_date"]) for entry in dated_entries]

    history_slugs = {entry["slug"] for entry in entries if isinstance(entry.get("slug"), str)}
    rating_per_film = latest_rating_per_film(entries)
    titles = title_per_slug(entries)

    slug_to_tmdb_id = {
        slug: tmdb_id
        for slug, tmdb_id in resolve_tmdb_ids(entries, connection).items()
        if slug in history_slugs
    }
    films_by_slug, credits_by_slug = load_payloads_by_slug(connection, slug_to_tmdb_id)

    release_year_per_film: dict[str, int] = {}
    for entry in entries:
        slug = entry.get("slug")
        if not isinstance(slug, str) or slug in release_year_per_film:
            continue
        year = film_release_year(entry, films_by_slug.get(slug))
        if year is not None:
            release_year_per_film[slug] = year

    runtime_per_film = {
        slug: payload["runtime"]
        for slug, payload in films_by_slug.items()
        if isinstance(payload.get("runtime"), int) and payload["runtime"] > 0
    }

    films_per_director, director_names, director_profiles = collect_directors(credits_by_slug)

    genres = summarize_groups(
        collect_group_members(
            films_by_slug,
            lambda payload: [
                genre.get("name")
                for genre in payload.get("genres", []) or []
                if isinstance(genre, dict) and genre.get("name")
            ],
        ),
        rating_per_film,
    )
    countries = summarize_groups(
        collect_group_members(
            films_by_slug,
            lambda payload: [
                country.get("name")
                for country in payload.get("production_countries", []) or []
                if isinstance(country, dict) and country.get("name")
            ],
        ),
        rating_per_film,
    )
    languages = summarize_groups(
        collect_group_members(
            films_by_slug,
            lambda payload: [
                language.get("english_name") or language.get("name")
                for language in payload.get("spoken_languages", []) or []
                if isinstance(language, dict)
            ],
        ),
        rating_per_film,
    )

    world_map = build_world_map(films_by_slug)
    runtime = build_runtime(entries, runtime_per_film)
    rewatches = sum(1 for entry in entries if entry.get("rewatch") is True)
    luckiest_directors, unluckiest_directors = build_director_luck(
        films_per_director, director_names, rating_per_film
    )

    audit = {
        "entries": len(entries),
        "entries_without_watched_date": len(entries) - len(dated_entries),
        "entries_without_slug": sum(
            1 for entry in entries if not isinstance(entry.get("slug"), str)
        ),
        "films_without_tmdb_payload": len(history_slugs) - len(films_by_slug),
        "films_without_credits": len(history_slugs) - len(credits_by_slug),
    }

    document = {
        "generated_at": today.isoformat(),
        "username": history.get("username") or LETTERBOXD_USER,
        "totals": {
            "films": len(history_slugs),
            "hours": runtime["total_minutes"] // 60,
            "directors": len(films_per_director),
            "countries": len(world_map),
            "longest_streak_weeks": longest_run_of_consecutive_weeks(watched_dates),
            "multi_film_days": count_multi_film_days(watched_dates),
        },
        "by_year": build_by_year(dated_entries),
        "decades": build_decades(release_year_per_film, rating_per_film),
        "genres": genres,
        "countries": countries,
        "languages": languages,
        "cast": build_cast(credits_by_slug),
        "directors": build_directors(
            films_per_director, director_names, director_profiles, rating_per_film
        ),
        "studios": build_studios(films_by_slug, rating_per_film),
        "collections": build_collections(films_by_slug, load_collection_sizes(connection)),
        "world_map": world_map,
        "list_progress": build_list_progress(cached_lists, history_slugs),
        "extras": {
            "rating_bias": build_rating_bias(rating_per_film, films_by_slug),
            "rating_drift": build_rating_drift(dated_entries),
            "rewatch_rate": rounded(rewatches / len(entries), 3) if entries else None,
            "watchlist": build_watchlist(watchlist, history_slugs, today),
            "heatmap": build_heatmap(dated_entries),
            "runtime": runtime,
            "decade_gaps": build_decade_gaps(release_year_per_film, today),
            "director_completeness": build_director_completeness(
                films_per_director,
                director_names,
                load_director_filmography_sizes(connection),
            ),
            "contrarian_index": build_contrarian_index(
                rating_per_film, films_by_slug, titles, release_year_per_film
            ),
            "obscurity": build_obscurity(films_by_slug, titles),
            "release_recency": build_release_recency(dated_entries, films_by_slug),
            "half_star_usage": build_half_star_usage(rating_per_film),
            "liked_but_low": build_liked_but_low(entries, rating_per_film, titles),
            "longest_drought": build_longest_drought(watched_dates),
            "weekday_profile": build_weekday_profile(watched_dates),
            "month_seasonality": build_month_seasonality(watched_dates),
            "logging_lag": build_logging_lag(entries),
            "lucky_director": luckiest_directors,
            "unlucky_director": unluckiest_directors,
            "background_actor": build_background_actor(credits_by_slug),
            "crew_most_watched": build_crew_most_watched(credits_by_slug),
            "life_in_days": build_life_in_days(runtime["total_minutes"], today),
            "extremes": build_extremes(runtime_per_film, release_year_per_film, titles),
            "rating_vs_runtime": build_rating_vs_runtime(runtime_per_film, rating_per_film),
            "title_words": build_title_words(titles),
        },
    }

    return document, audit


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def describe_module(value: Any) -> str:
    """Describe what one module produced, in one short line."""
    if value is None:
        return "null (no data)"
    if isinstance(value, list):
        return f"{len(value)} rows" if value else "empty"
    if isinstance(value, dict):
        return ", ".join(f"{key} {describe_value(item)}" for key, item in value.items())
    return str(value)


def describe_value(value: Any) -> str:
    """Render one value inside a module description."""
    if isinstance(value, list):
        return f"{len(value)} rows"
    if isinstance(value, dict):
        return f"{len(value)} keys"
    return "null" if value is None else str(value)


def print_summary(stats: dict[str, Any], audit: dict[str, int]) -> None:
    """Print what each module produced, so a run can be checked without opening the file."""
    rows: list[tuple[str, str]] = [("totals", describe_module(stats["totals"]))]

    for name in (
        "by_year",
        "decades",
        "genres",
        "countries",
        "languages",
        "cast",
        "directors",
        "studios",
        "collections",
        "world_map",
        "list_progress",
    ):
        rows.append((name, describe_module(stats[name])))

    for name, value in stats["extras"].items():
        rows.append((f"extras.{name}", describe_module(value)))

    rows.append(("", ""))
    rows.append(("input: diary entries read", str(audit["entries"])))
    rows.append(
        (
            "input: entries with no date",
            f"{audit['entries_without_watched_date']} (counted in totals, left out of by_year, "
            "streak, heatmap, drift)",
        )
    )
    rows.append(("input: entries with no slug", str(audit["entries_without_slug"])))
    rows.append(("input: films with no TMDB payload", str(audit["films_without_tmdb_payload"])))
    rows.append(("input: films with no credits", str(audit["films_without_credits"])))

    width = max(len(label) for label, _ in rows)
    print(f"\n{'module'.ljust(width)}  result")
    print(f"{'-' * width}  {'-' * 40}")
    for label, detail in rows:
        print(f"{label.ljust(width)}  {detail}".rstrip())


def main() -> None:
    """Read every input, build the stats document, and write it to docs/data."""
    ensure_dirs()

    history = load_history(HISTORY_FILE)
    connection = open_tmdb_cache(TMDB_CACHE_FILE)
    cached_lists = load_cached_lists()

    if not cached_lists:
        print(
            "No cached curated lists found. "
            "Run scripts/fetch_lists.py to fill data/cache/lists/. "
            "List progress will be empty until then."
        )

    try:
        stats, audit = build_stats_document(history, connection, cached_lists)
    finally:
        if connection is not None:
            connection.close()

    STATS_FILE.write_text(json.dumps(stats, indent=2, ensure_ascii=False) + "\n")

    print(f"Wrote {STATS_FILE}")
    print_summary(stats, audit)


if __name__ == "__main__":
    main()
