"""Attach TMDB metadata to every film in the watch history.

The stats panel needs facts Letterboxd does not publish: runtime, genres,
countries, languages, cast, crew, studios, collections and keywords. TMDB has
all of them, so each film is fetched once and kept in a local SQLite cache.

How a film reaches TMDB:

1. A manual match in `data/manual-matches.json` wins over everything else. It is
   the place to correct a film the automatic steps get wrong.
2. RSS entries already carry `tmdb_id`, so those need no search at all.
3. Anything left is searched by title and year, and the top result is taken.

Every answer TMDB gives is written to the `lookups` table, including the answer
"there is no such film". A weekly run therefore searches nothing it has already
searched and downloads nothing it already holds.

An HTTP 404 is one of those answers. It says the id is wrong, not that the
request failed, so it is cached like any other answer and the film is never asked
for again.

A request that never got an answer is not an answer. When TMDB is unreachable,
failing, or rate limiting past the retries, nothing is written for that film and
the next run tries it again. Recording a null there would drop the film from the
stats forever over a few minutes of downtime.

When TMDB stops answering altogether, the run gives up rather than spending the
same retries on every remaining film. It exits non-zero and asks to be run again
later.

Run it after `merge_history.py` and before `build_stats.py`:

    python scripts/enrich_tmdb.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, NamedTuple

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.config import (
    HISTORY_FILE,
    MANUAL_MATCHES_FILE,
    REQUEST_DELAY,
    REQUEST_TIMEOUT,
    ROOT,
    TMDB_CACHE_FILE,
    USER_AGENT,
    ensure_dirs,
)

TMDB_API_ROOT = "https://api.themoviedb.org/3"
TMDB_KEY_PAGE = "https://www.themoviedb.org/settings/api"

# One request per film covers metadata, cast, crew and keywords.
APPEND_TO_RESPONSE = "credits,keywords"

# TMDB allows far more than this. A quarter second keeps a full run of several
# hundred films down to a few minutes while staying well inside their limits.
DELAY_BETWEEN_REQUESTS = 0.25

MAX_ATTEMPTS_PER_REQUEST = 3
FALLBACK_RETRY_AFTER_SECONDS = 5.0

# When to decide TMDB is down rather than one film being odd.
#
# A film that gets no answer costs its whole retry budget, close to four seconds
# of waiting. Films fail independently, so a run of ten in a row is the service
# and not the films. Ten costs about forty seconds to establish, which is nothing
# against a library of hundreds, and it is far more than any single bad film or
# momentary blip can produce.
GIVE_UP_AFTER_UNANSWERED_REQUESTS = 10

# The schema is fixed by DATA_CONTRACT.md. Keep the three tables as written.
# These names are the only source of table and column names in the SQL below,
# so nothing from the cache file or from TMDB is ever interpolated into a query.
CACHE_SCHEMA: dict[str, tuple[str, ...]] = {
    "films": (
        "tmdb_id INTEGER PRIMARY KEY",
        "slug TEXT",
        "payload TEXT",
        "fetched_at TEXT",
    ),
    "credits": (
        "tmdb_id INTEGER PRIMARY KEY",
        "payload TEXT",
        "fetched_at TEXT",
    ),
    "lookups": (
        "slug TEXT PRIMARY KEY",
        "tmdb_id INTEGER",
        "resolved_at TEXT",
    ),
}


class CredentialRejected(RuntimeError):
    """TMDB refused the credential, so no film in this run can succeed."""


class TmdbUnavailable(RuntimeError):
    """TMDB stopped answering, so the run gave up instead of asking for every film."""


class NoSuchRecord:
    """TMDB's answer that the thing asked for does not exist.

    This is an answer, so it is kept apart from None, which means no answer at
    all. The caller caches it and never asks again, instead of retrying a dead
    id every week and reporting it as a download failure.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        # Names itself in any debug print, rather than showing an object address.
        return "NO_SUCH_RECORD"


NO_SUCH_RECORD = NoSuchRecord()


@dataclass
class OutageDetector:
    """Counts requests in a row that got no answer, and stops the run once TMDB is down.

    One film can fail on its own. A long run of films cannot all fail on their
    own, because they have nothing in common except the service, so a run that
    long means TMDB is the problem and no further film will do any better.
    """

    unanswered_in_a_row: int = 0

    def note_answer(self) -> None:
        """Record that TMDB answered, whatever the answer said."""
        self.unanswered_in_a_row = 0

    def note_no_answer(self) -> None:
        """Record a request that got no usable answer, and give up once TMDB is down.

        Raises TmdbUnavailable once the run of unanswered requests is long enough
        to be the service rather than one awkward film.
        """
        self.unanswered_in_a_row += 1
        if self.unanswered_in_a_row < GIVE_UP_AFTER_UNANSWERED_REQUESTS:
            return

        raise TmdbUnavailable(
            f"TMDB gave no usable answer for the last {self.unanswered_in_a_row}"
            " films in a row, so this run stopped instead of spending the same"
            " retries on every film left.\n"
            "TMDB looks unavailable. Nothing was recorded for the films that got no"
            " answer, so nothing is lost, and everything downloaded before this"
            " point is still cached. Run this script again later."
        )


class HistoryFilm(NamedTuple):
    """One film to enrich, reduced to the fields TMDB needs."""

    slug: str
    title: str | None
    year: int | None
    tmdb_id: int | None


class MatchOutcome(Enum):
    """Why a film did or did not end up with a TMDB id.

    The three failures are kept apart because they have different lifetimes.
    ABSENT_FROM_TMDB is TMDB's own answer and is cached forever. The other two
    mean nothing was learned, so nothing is cached and the next run tries again.
    """

    MATCHED = "matched"
    ABSENT_FROM_TMDB = "TMDB has no such film"
    TMDB_DID_NOT_ANSWER = "TMDB did not answer"
    NO_TITLE_TO_SEARCH = "the history entry has no title"


class Match(NamedTuple):
    """The result of identifying one film on TMDB, with the id when there is one."""

    outcome: MatchOutcome
    tmdb_id: int | None = None


@dataclass
class RunSummary:
    """What one enrichment run did, in the terms the reader cares about.

    Films that got no metadata are listed by reason, not lumped together, because
    the reader does something different about each reason.
    """

    films_in_history: int = 0
    already_cached: int = 0
    fetched: int = 0

    # TMDB answered and has no such film. Already cached, never searched again.
    absent_slugs: list[str] = field(default_factory=list)

    # The search never got an answer. Nothing cached, retried on the next run.
    search_failed_slugs: list[str] = field(default_factory=list)

    # No title to search with, so TMDB was never asked. Nothing cached.
    unsearchable_slugs: list[str] = field(default_factory=list)

    # The id is known but the details request never got an answer.
    download_failed_slugs: list[str] = field(default_factory=list)

    # TMDB answered 404: nothing has that id, so the id itself is wrong. That is
    # an answer, so it is cached and the film is never asked for again.
    wrong_id_slugs: list[str] = field(default_factory=list)

    def record_unresolved(self, slug: str, outcome: MatchOutcome) -> None:
        """File a film that got no TMDB id under the reason it got none."""
        buckets = {
            MatchOutcome.ABSENT_FROM_TMDB: self.absent_slugs,
            MatchOutcome.TMDB_DID_NOT_ANSWER: self.search_failed_slugs,
            MatchOutcome.NO_TITLE_TO_SEARCH: self.unsearchable_slugs,
        }
        buckets[outcome].append(slug)


def timestamp() -> str:
    """Return the current UTC time, for the `fetched_at` and `resolved_at` columns."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_credential() -> str:
    """Read the TMDB credential from the environment, loading a local .env first.

    Accepts either kind of TMDB credential. The value is never printed, not even
    when it is rejected.
    """
    load_dotenv(ROOT / ".env")
    credential = (os.getenv("TMDB_API_KEY") or "").strip()

    if not credential:
        print(
            "No TMDB credential found: TMDB_API_KEY is missing or empty.\n"
            f"Copy {ROOT / '.env.example'} to {ROOT / '.env'} and set TMDB_API_KEY in it.\n"
            f"Create a credential at {TMDB_KEY_PAGE}. Either the v3 API key or the\n"
            "v4 read access token works, and this script tells them apart on its own.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    return credential


def build_authentication(credential: str) -> tuple[dict[str, str], dict[str, str]]:
    """Turn one credential into the headers and query parameters TMDB expects.

    A v4 read access token is a JSON Web Token: three segments separated by dots.
    It travels in an Authorization bearer header. A v3 API key has no dots and
    travels in the `api_key` query parameter. The shape says which one it is, so
    nobody has to configure that.

    Returns the request headers and the query parameters every call must carry.
    """
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}

    if credential.count(".") == 2:
        headers["Authorization"] = f"Bearer {credential}"
        return headers, {}

    return headers, {"api_key": credential}


def retry_after_seconds(response: httpx.Response) -> float:
    """Read how long TMDB asked us to wait after a rate limit."""
    try:
        return max(1.0, float(response.headers.get("Retry-After", "")))
    except ValueError:
        # The header is absent or is an HTTP date rather than a count of seconds.
        return FALLBACK_RETRY_AFTER_SECONDS


def request_json(
    client: httpx.Client,
    path: str,
    params: dict[str, Any],
    description: str,
    outage: OutageDetector,
) -> dict[str, Any] | NoSuchRecord | None:
    """Call one TMDB endpoint and return its answer, or None if it never answers.

    Waits out a 429 for as long as the Retry-After header asks, retries a server
    error a couple of times, and gives up on anything else rather than raising. A
    rejected credential is the one failure worth stopping for, because it would
    fail on every remaining film too.

    There are three kinds of return value, and they must not be confused:

    - a JSON body, which is what TMDB holds for that path.
    - NO_SUCH_RECORD, from an HTTP 404. That is TMDB answering that nothing has
      that id. It is a real answer, so a caller caches it and stops asking.
    - None, meaning no usable answer from TMDB. It never means "TMDB has no such
      film", so a caller must not cache None as a result.

    Every request that ends in None also feeds `outage`, which stops the whole
    run once enough of them arrive in a row to mean TMDB itself is down.

    The `description` names the film in any message the reader sees.
    """
    for attempt in range(1, MAX_ATTEMPTS_PER_REQUEST + 1):
        try:
            response = client.get(path, params=params)
        except httpx.HTTPError as error:
            if attempt < MAX_ATTEMPTS_PER_REQUEST:
                time.sleep(REQUEST_DELAY * attempt)
                continue
            print(
                f"  {description}: could not reach TMDB ({error}),"
                " retried on the next run"
            )
            outage.note_no_answer()
            return None
        finally:
            time.sleep(DELAY_BETWEEN_REQUESTS)

        if response.status_code == 429:
            wait = retry_after_seconds(response)
            print(f"  rate limited by TMDB, waiting {wait:.0f}s before retrying")
            time.sleep(wait)
            continue

        if response.status_code in (401, 403):
            raise CredentialRejected(
                f"TMDB rejected the credential with HTTP {response.status_code}. "
                f"Check TMDB_API_KEY in {ROOT / '.env'}, or create a new credential "
                f"at {TMDB_KEY_PAGE}."
            )

        if response.status_code == 404:
            # TMDB has answered the question: it holds nothing at that path.
            # Retrying cannot change that, so this counts as the service being up.
            outage.note_answer()
            return NO_SUCH_RECORD

        if response.status_code >= 500:
            if attempt < MAX_ATTEMPTS_PER_REQUEST:
                time.sleep(REQUEST_DELAY * attempt)
                continue
            print(
                f"  {description}: TMDB is failing ({response.status_code}),"
                " retried on the next run"
            )
            outage.note_no_answer()
            return None

        if response.status_code != 200:
            print(
                f"  {description}: TMDB answered {response.status_code},"
                " retried on the next run"
            )
            outage.note_no_answer()
            return None

        outage.note_answer()
        return response.json()

    print(
        f"  {description}: still rate limited after"
        f" {MAX_ATTEMPTS_PER_REQUEST} attempts, retried on the next run"
    )
    outage.note_no_answer()
    return None


def existing_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    """Return the columns a cache table has, or an empty set if the table is absent."""
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def ensure_table(
    connection: sqlite3.Connection, table: str, columns: tuple[str, ...]
) -> None:
    """Create one cache table, or add whatever columns an older copy of it lacks.

    The weekly run restores this database from the Actions cache, so the file can
    arrive written by an earlier version of this script. Adding what is missing
    keeps that file and everything already downloaded into it, instead of failing
    on the first query against a table that does not have the column yet.
    """
    present = existing_columns(connection, table)

    if not present:
        connection.execute(f"CREATE TABLE {table} ({', '.join(columns)})")
        return

    for column in columns:
        name = column.split(" ", 1)[0]
        if name in present:
            continue

        if "PRIMARY KEY" in column:
            # SQLite cannot add a primary key to a table that already exists.
            print(
                f"The TMDB cache table {table} has no {name} column, and a key column\n"
                "cannot be added to an existing table. Delete\n"
                f"{TMDB_CACHE_FILE} and run this script again to rebuild the cache.",
                file=sys.stderr,
            )
            raise SystemExit(1)

        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column}")


def open_cache() -> sqlite3.Connection:
    """Open data/cache/tmdb.sqlite, creating or completing its three tables.

    The file may be new, or restored by actions/cache from a run of an older
    version of this script, so nothing here assumes a fresh database.
    """
    ensure_dirs()
    connection = sqlite3.connect(TMDB_CACHE_FILE)
    for table, columns in CACHE_SCHEMA.items():
        ensure_table(connection, table, columns)
    connection.commit()
    return connection


def read_lookups(connection: sqlite3.Connection) -> dict[str, int | None]:
    """Read every slug already resolved, including the ones resolved to nothing."""
    rows = connection.execute("SELECT slug, tmdb_id FROM lookups").fetchall()
    return {slug: tmdb_id for slug, tmdb_id in rows}


def remember_lookup(
    connection: sqlite3.Connection, slug: str, tmdb_id: int | None
) -> None:
    """Record what a slug resolved to, so the search never runs for it again.

    A null id is recorded on purpose, but only for TMDB's own answer that it has
    no such film: that one should not be searched for every single week. A search
    that never got an answer must not come here, because the row it wrote would
    keep the film out of the stats for good.
    """
    connection.execute(
        "INSERT OR REPLACE INTO lookups (slug, tmdb_id, resolved_at) VALUES (?, ?, ?)",
        (slug, tmdb_id, timestamp()),
    )
    connection.commit()


def read_cached_film_ids(connection: sqlite3.Connection) -> set[int]:
    """Read the ids of films already downloaded, so they are not downloaded twice."""
    rows = connection.execute("SELECT tmdb_id FROM films").fetchall()
    return {row[0] for row in rows}


def store_film(
    connection: sqlite3.Connection,
    tmdb_id: int,
    slug: str,
    response: dict[str, Any],
) -> None:
    """Write one TMDB response into the films and credits tables.

    The credits block is filed in its own table because the contract gives it
    one, and holding it in both places would only make the two disagree later.
    Keywords stay inside the film payload, where TMDB returns them. Both stay
    raw, so a new stat never means downloading anything again.

    The write commits per film, so an interrupted run keeps everything it got.
    """
    fetched_at = timestamp()
    payload = dict(response)
    credits = payload.pop("credits", None)

    connection.execute(
        "INSERT OR REPLACE INTO films (tmdb_id, slug, payload, fetched_at)"
        " VALUES (?, ?, ?, ?)",
        (tmdb_id, slug, json.dumps(payload, ensure_ascii=False), fetched_at),
    )
    if credits is not None:
        connection.execute(
            "INSERT OR REPLACE INTO credits (tmdb_id, payload, fetched_at)"
            " VALUES (?, ?, ?)",
            (tmdb_id, json.dumps(credits, ensure_ascii=False), fetched_at),
        )
    connection.commit()


def load_manual_matches() -> dict[str, int]:
    """Read data/manual-matches.json, creating an empty one the first time.

    The file maps a Letterboxd slug to a TMDB id. It is where a wrong or missing
    match gets corrected by hand, so it wins over every automatic answer.
    """
    ensure_dirs()

    if not MANUAL_MATCHES_FILE.exists():
        MANUAL_MATCHES_FILE.write_text("{}\n")
        return {}

    try:
        raw = json.loads(MANUAL_MATCHES_FILE.read_text())
    except json.JSONDecodeError as error:
        print(
            f"{MANUAL_MATCHES_FILE} is not valid JSON ({error}).\n"
            'Fix the file, or replace its contents with {} to start over.',
            file=sys.stderr,
        )
        raise SystemExit(1)

    if not isinstance(raw, dict):
        print(
            f"{MANUAL_MATCHES_FILE} must hold one object mapping slug to TMDB id,\n"
            'for example {"el-compadre-mendoza": 114626}.',
            file=sys.stderr,
        )
        raise SystemExit(1)

    matches: dict[str, int] = {}
    for slug, tmdb_id in raw.items():
        if isinstance(tmdb_id, int) and not isinstance(tmdb_id, bool):
            matches[slug] = tmdb_id
        else:
            print(
                f"  ignoring manual match for {slug}: {tmdb_id!r} is not a TMDB id."
                " Ids are plain integers."
            )
    return matches


def load_history() -> dict[str, Any]:
    """Read data/history.json, the file this script enriches."""
    if not HISTORY_FILE.exists():
        print(
            f"Nothing to enrich: {HISTORY_FILE} does not exist.\n"
            "Run backfill.py once to build the history, then fetch_rss.py and\n"
            "merge_history.py to keep it current.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    return json.loads(HISTORY_FILE.read_text())


def collect_films(history: dict[str, Any]) -> list[HistoryFilm]:
    """Reduce the history to one entry per film.

    A film watched more than once appears once here. The copy carrying a TMDB id
    is preferred, because it saves a search.
    """
    films: dict[str, HistoryFilm] = {}

    for entry in history.get("entries", []):
        slug = entry.get("slug")
        if not slug:
            # Without a slug there is no stable identity to cache the film under.
            continue

        candidate = HistoryFilm(
            slug=slug,
            title=entry.get("title"),
            year=entry.get("year"),
            tmdb_id=entry.get("tmdb_id"),
        )
        existing = films.get(slug)
        if existing is None or (existing.tmdb_id is None and candidate.tmdb_id):
            films[slug] = candidate

    return list(films.values())


def search_film(
    client: httpx.Client,
    auth_params: dict[str, str],
    film: HistoryFilm,
    outage: OutageDetector,
) -> Match:
    """Find a film's TMDB id by title and year, taking TMDB's own top result.

    TMDB ranks results by popularity, and for a member's watch history the top
    result is right often enough that checking the rest by hand costs more than
    it saves. Wrong matches get corrected in data/manual-matches.json.

    When the year finds nothing the title is tried alone, because Letterboxd and
    TMDB disagree on the release year of festival and re-release titles.

    An empty result list and an unanswered request are told apart in the returned
    outcome. Only the empty result list means TMDB has no such film.
    """
    if not film.title:
        return Match(MatchOutcome.NO_TITLE_TO_SEARCH)

    attempts: list[dict[str, Any]] = []
    if film.year:
        attempts.append({"query": film.title, "year": film.year})
    attempts.append({"query": film.title})

    for search_params in attempts:
        params = {**auth_params, **search_params, "include_adult": "false"}
        body = request_json(
            client, "/search/movie", params, f"search {film.slug}", outage
        )
        if not isinstance(body, dict):
            # Either TMDB never answered, or it answered 404. A 404 here is not
            # a missing film: the search endpoint always exists, so it would mean
            # this script asked wrongly. Neither case proves the film is absent,
            # so nothing is cached and the broader query is not worth trying.
            return Match(MatchOutcome.TMDB_DID_NOT_ANSWER)

        results = body.get("results") or []
        if results:
            tmdb_id = results[0].get("id")
            if isinstance(tmdb_id, int):
                return Match(MatchOutcome.MATCHED, tmdb_id)

    return Match(MatchOutcome.ABSENT_FROM_TMDB)


def resolve_tmdb_id(
    film: HistoryFilm,
    manual_matches: dict[str, int],
    known_lookups: dict[str, int | None],
    connection: sqlite3.Connection,
    client: httpx.Client,
    auth_params: dict[str, str],
    outage: OutageDetector,
) -> Match:
    """Decide which TMDB film a history entry is, and record a real answer.

    The order is deliberate: a hand written match, then TMDB's own answer that
    there is no such film, then the id the RSS feed gave, then a search.

    A cached "no such film" comes before the feed's id on purpose. That row is
    written either because the search found nothing or because the id came back
    404, and in both cases believing the feed again would ask TMDB the same dead
    question every week. The hand written match still wins over all of it, so
    data/manual-matches.json remains the way to correct any of these.

    Only an answer from TMDB reaches the lookups table. A search that failed
    leaves no row, so the next run searches that film again instead of treating
    an outage as proof the film does not exist.
    """
    manual_id = manual_matches.get(film.slug)
    if manual_id is not None:
        if known_lookups.get(film.slug) != manual_id:
            remember_lookup(connection, film.slug, manual_id)
            known_lookups[film.slug] = manual_id
        return Match(MatchOutcome.MATCHED, manual_id)

    if film.slug in known_lookups and known_lookups[film.slug] is None:
        return Match(MatchOutcome.ABSENT_FROM_TMDB)

    if film.tmdb_id:
        if film.slug not in known_lookups:
            remember_lookup(connection, film.slug, film.tmdb_id)
            known_lookups[film.slug] = film.tmdb_id
        return Match(MatchOutcome.MATCHED, film.tmdb_id)

    cached_id = known_lookups.get(film.slug)
    if cached_id is not None:
        return Match(MatchOutcome.MATCHED, cached_id)

    match = search_film(client, auth_params, film, outage)

    if match.outcome is MatchOutcome.MATCHED:
        remember_lookup(connection, film.slug, match.tmdb_id)
        known_lookups[film.slug] = match.tmdb_id
    elif match.outcome is MatchOutcome.ABSENT_FROM_TMDB:
        remember_lookup(connection, film.slug, None)
        known_lookups[film.slug] = None

    return match


def fetch_film_details(
    client: httpx.Client,
    auth_params: dict[str, str],
    tmdb_id: int,
    slug: str,
    outage: OutageDetector,
) -> dict[str, Any] | NoSuchRecord | None:
    """Download one film's metadata, cast, crew and keywords in a single request.

    Returns NO_SUCH_RECORD when TMDB has no film with that id, which is an answer
    about the id and not a download failure. Returns None only when TMDB gave no
    answer at all.
    """
    params = {**auth_params, "append_to_response": APPEND_TO_RESPONSE}
    return request_json(client, f"/movie/{tmdb_id}", params, f"film {slug}", outage)


def enrich(
    films: list[HistoryFilm], transport: httpx.BaseTransport | None = None
) -> RunSummary:
    """Resolve and download every film that is not already in the cache.

    Returns the counts the run prints: cached, fetched, and each reason a film
    got nothing.

    Raises TmdbUnavailable when TMDB stops answering, which ends the run early
    and on purpose. Everything downloaded before that point stays cached, and
    nothing is recorded for the films that got no answer.

    `transport` replaces the HTTP layer. A run leaves it unset and talks to TMDB.
    Tests pass an httpx.MockTransport to exercise outages without a network.
    """
    credential = read_credential()
    headers, auth_params = build_authentication(credential)
    manual_matches = load_manual_matches()

    summary = RunSummary(films_in_history=len(films))
    outage = OutageDetector()
    connection = open_cache()

    try:
        known_lookups = read_lookups(connection)
        cached_ids = read_cached_film_ids(connection)

        with httpx.Client(
            base_url=TMDB_API_ROOT,
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
            headers=headers,
            transport=transport,
        ) as client:
            for film in films:
                match = resolve_tmdb_id(
                    film,
                    manual_matches,
                    known_lookups,
                    connection,
                    client,
                    auth_params,
                    outage,
                )

                if match.tmdb_id is None:
                    summary.record_unresolved(film.slug, match.outcome)
                    continue

                if match.tmdb_id in cached_ids:
                    summary.already_cached += 1
                    continue

                details = fetch_film_details(
                    client, auth_params, match.tmdb_id, film.slug, outage
                )

                if details is NO_SUCH_RECORD:
                    # TMDB says nothing carries this id, so the id is wrong. Record
                    # that answer, or the same dead id is requested every week and
                    # reported as a download failure the reader is told to retry.
                    remember_lookup(connection, film.slug, None)
                    known_lookups[film.slug] = None
                    summary.wrong_id_slugs.append(film.slug)
                    continue

                if not isinstance(details, dict):
                    # The only value left is None: TMDB gave no answer at all, so
                    # nothing is recorded and the next run asks again.
                    summary.download_failed_slugs.append(film.slug)
                    continue

                store_film(connection, match.tmdb_id, film.slug, details)
                cached_ids.add(match.tmdb_id)
                summary.fetched += 1
                print(f"  fetched {film.slug} ({match.tmdb_id})")
    finally:
        connection.close()

    return summary


def report(summary: RunSummary) -> None:
    """Print what the run did, and what the reader can do about what it could not."""
    never_answered = len(summary.search_failed_slugs) + len(
        summary.download_failed_slugs
    )

    print("")
    print(f"films in history:     {summary.films_in_history}")
    print(f"already cached:       {summary.already_cached}")
    print(f"fetched now:          {summary.fetched}")
    print(f"no match on TMDB:     {len(summary.absent_slugs)}")
    print(f"id not on TMDB:       {len(summary.wrong_id_slugs)}")
    print(f"TMDB never answered:  {never_answered}")
    print(f"no title to search:   {len(summary.unsearchable_slugs)}")

    if summary.search_failed_slugs:
        print("")
        print(
            "These films could not be looked up because TMDB never answered.\n"
            "Nothing was recorded for them, so run this script again to retry them:"
        )
        for slug in summary.search_failed_slugs:
            print(f"  {slug}")

    if summary.download_failed_slugs:
        print("")
        print(
            "These films were identified but their metadata never downloaded,\n"
            "again because TMDB never answered. Run this script again to retry them:"
        )
        for slug in summary.download_failed_slugs:
            print(f"  {slug}")

    if summary.wrong_id_slugs:
        print("")
        print(
            "TMDB has no film with the id these entries carry, so the id is wrong\n"
            "rather than the network. That answer is now cached, and they will not\n"
            "be requested again. To give one the right id, add it to\n"
            f"{MANUAL_MATCHES_FILE} as \"slug\": tmdb_id and run this script again:"
        )
        for slug in summary.wrong_id_slugs:
            print(f"  {slug}")

    if summary.absent_slugs:
        print("")
        print(
            "TMDB answered that it has no match for these slugs, and that answer is\n"
            "now cached. To correct one, add it to\n"
            f"{MANUAL_MATCHES_FILE} as \"slug\": tmdb_id and run this script again:"
        )
        for slug in summary.absent_slugs:
            print(f"  {slug}")

    if summary.unsearchable_slugs:
        print("")
        print(
            "These history entries carry no title, so there was nothing to search\n"
            f"TMDB with. Add each one to {MANUAL_MATCHES_FILE}\n"
            'as "slug": tmdb_id and run this script again:'
        )
        for slug in summary.unsearchable_slugs:
            print(f"  {slug}")


def main() -> None:
    history = load_history()
    films = collect_films(history)

    try:
        summary = enrich(films)
    except (CredentialRejected, TmdbUnavailable) as error:
        # Both mean no film left in this run could succeed, so the run stops and
        # the exit code tells the weekly workflow the same thing.
        print(error, file=sys.stderr)
        raise SystemExit(1)

    report(summary)


if __name__ == "__main__":
    main()
