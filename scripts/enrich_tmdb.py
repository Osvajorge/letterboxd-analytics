"""Attach TMDB metadata to every film in the watch history.

The stats panel needs facts Letterboxd does not publish: runtime, genres,
countries, languages, cast, crew, studios, collections and keywords. TMDB has
all of them, so each film is fetched once and kept in a local SQLite cache.

How a film reaches TMDB:

1. A manual match in `data/manual-matches.json` wins over everything else. It is
   the place to correct a film the automatic steps get wrong.
2. RSS entries already carry `tmdb_id`, so those need no search at all.
3. Anything left is searched by title and year, and the result that best answers
   the search is taken: an exact title first, then the closest release year.

A TMDB id names one film, so no two slugs may hold the same one. A search whose
best answer is an id another slug already holds is refused: that is evidence the
search went wrong, not evidence the two slugs are one film. The slug is left
unresolved and named in the summary, so a person can settle it in
`data/manual-matches.json`. Taking the id anyway is what merged six pairs of this
member's films into six single films, and it did so without a single message.

A cache written before that rule can still hold the contradiction, so each run
starts by forgetting every cached lookup where one id is held by two slugs and
searching those slugs again.

Every answer TMDB gives is written to the `lookups` table, including the answer
"there is no such film". A weekly run therefore searches nothing it has already
searched and downloads nothing it already holds.

An HTTP 404 is one of those answers. It says the id is wrong, not that the
request failed, so it is cached like any other answer and the film is not asked
about again. Two rules keep that from becoming damage. A run of 404s long enough
to be the service rather than the ids stops the run and writes nothing. And an id
the feed carries later beats a cached "there is no such film", because a
corrected id is new evidence about the film.

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
import re
import sqlite3
import sys
import time
from collections import defaultdict
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

# When to decide TMDB is denying every id rather than a few ids being wrong.
#
# A 404 for one film's id is an answer about that id. A 404 for a long run of ids
# is not, because those ids were issued at different times, come from the
# Letterboxd feed or from a TMDB search that just returned them, and share
# nothing except the service now judging them. So the service is what changed.
#
# Ten is high enough that no plausible run of genuinely dead ids reaches it: the
# history is ordered by watch date, which has nothing to do with whether an id
# still exists, so dead ids arrive scattered rather than ten in a row. It is low
# enough to catch a service answering 404 to everything within the first seconds
# of a run, because a 404 costs one request and no retries.
GIVE_UP_AFTER_MISSING_IDS_IN_A_ROW = 10

# When to decide TMDB is finding nothing rather than a few films being absent.
#
# A search that comes back empty is TMDB answering that it holds no such film,
# and that answer is cached forever. A TMDB that answers every search with an
# empty list would therefore write off the whole library in one run, quietly and
# with an exit code of zero.
#
# The threshold has to clear the longest run of films that really are absent.
# They are not scattered: this member logs television, TMDB's film endpoint
# rightly has none of it, and the episodes were logged together, so 25 absent
# titles arrive in runs of up to eight. Twenty-five clears that with room to
# spare, costs about seven seconds of searching to establish, and is still
# reached within the first seconds of a run where TMDB finds nothing at all.
GIVE_UP_AFTER_EMPTY_SEARCHES_IN_A_ROW = 25

# How far TMDB's release year may sit from the year Letterboxd records before a
# result whose title is different is a different film.
#
# Festival runs, re-releases and staggered international dates put the two a year
# apart often enough that demanding the same year would lose real matches. A
# result whose title matches exactly is not held to this at all, because the
# title is the stronger evidence: TMDB dates the Demon Slayer compilations by
# their 2025 re-release and Letterboxd by the 2023 original, and they are the
# same film.
MAX_RELEASE_YEAR_GAP = 1

# Everything in a title that is neither a letter nor a digit nor a space. The two
# sites punctuate the same film differently, so none of it tells films apart.
TITLE_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)

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


class TmdbDeniesEveryId(RuntimeError):
    """TMDB called every id in a long run missing, which says more about TMDB."""


class TmdbFindsNoFilm(RuntimeError):
    """TMDB found nothing for a long run of searches, which says more about TMDB."""


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


@dataclass
class MissingIdDetector:
    """Counts films in a row whose id TMDB calls missing, and stops the run.

    This is the outage detector's reasoning applied to answers instead of
    failures. One film's id can be wrong on its own. A long run of them cannot,
    because the ids have nothing in common except the service judging them, so a
    run that long means TMDB is answering strangely and its answer must not be
    written down. Believing it would file a permanent "there is no such film" for
    the whole library in a single run.

    Any film whose details the run holds resets the count, whether it downloaded
    them now or already had them cached. The threshold's argument is about the
    library, not about the wire: ten films in a row all reported missing cannot
    all be genuinely missing. A film served from the cache is a film that did not
    go wrong, so it breaks that run exactly as a download does.

    Counting only downloads voided the argument on every warm cache, which is
    every weekly run. With nothing left to download, the count no longer measured
    a run of films at all. It measured every dead id the library had gathered
    over months, added up across the whole run, and stopped a healthy run once
    the tenth arrived.

    A request that got no answer neither confirms nor denies, so it leaves the
    count where it is.
    """

    missing_in_a_row: int = 0

    def note_film_has_details(self) -> None:
        """Record a film the run holds the details of, downloaded now or cached before."""
        self.missing_in_a_row = 0

    def note_id_missing(self) -> None:
        """Record a 404 for a film id, and give up once TMDB denies everything.

        Raises TmdbDeniesEveryId once the run of missing ids is too long to be
        the ids and must be the service.
        """
        self.missing_in_a_row += 1
        if self.missing_in_a_row < GIVE_UP_AFTER_MISSING_IDS_IN_A_ROW:
            return

        raise TmdbDeniesEveryId(
            "TMDB answered that it holds no film for the last"
            f" {self.missing_in_a_row} ids in a row, so this run stopped instead"
            " of believing that about the whole library.\n"
            "Ids this far apart do not all go wrong at once, so TMDB is answering"
            " strangely rather than reporting real gaps. No film was written off"
            " as missing, so nothing is lost, and everything downloaded earlier in"
            " the run is still cached. Run this script again later. If it stops"
            " here again, check whether TMDB is healthy before treating any of"
            " these ids as wrong."
        )


@dataclass
class EmptySearchDetector:
    """Counts searches in a row that found nothing, and stops the run.

    This is the missing id detector's reasoning applied to the search path,
    which had no such guard at all. A search that finds nothing is cached as
    "TMDB has no such film" and is never run again, so a TMDB that answers every
    search with an empty list writes that verdict over the whole library in one
    run, and the run still exits reporting success.

    A film really can be absent, and in this library twenty-five are: television
    that TMDB's film endpoint rightly does not hold. Those arrive in runs,
    because episodes get logged together, so the threshold clears the longest
    such run rather than assuming absent films are scattered.

    Any film the run ends up with an id for resets the count, for the same
    reason a cached film resets the missing id count: it proves this run is not
    one where everything is going wrong.
    """

    empty_in_a_row: int = 0

    def note_film_found(self) -> None:
        """Record a film that came out with an id, so the searches are working."""
        self.empty_in_a_row = 0

    def note_nothing_found(self) -> None:
        """Record a search that found nothing, and give up once TMDB finds nothing at all.

        Raises TmdbFindsNoFilm once the run of empty searches is too long to be
        the films and must be the service.
        """
        self.empty_in_a_row += 1
        if self.empty_in_a_row < GIVE_UP_AFTER_EMPTY_SEARCHES_IN_A_ROW:
            return

        raise TmdbFindsNoFilm(
            "TMDB found no film for the last"
            f" {self.empty_in_a_row} searches in a row, so this run stopped"
            " instead of writing that answer over the whole library.\n"
            "Films that are genuinely absent do not arrive in a run this long, so"
            " TMDB is answering strangely rather than reporting real gaps. No film"
            " was written off as absent, so nothing is lost, and everything"
            " downloaded earlier in the run is still cached. Run this script again"
            " later. If it stops here again, check whether TMDB is healthy before"
            " treating any of these films as absent."
        )


@dataclass
class ClaimedIds:
    """Which slug holds each TMDB id, so that two films never become one.

    A TMDB id names one film. Two slugs holding one id therefore says that one
    of the two searches was wrong, and nothing about the two slugs being the
    same film. Believing it merges the pair everywhere downstream: one runtime,
    one set of genres, one row in every count, and one film fewer in the total.

    So an id is claimed by the first slug that earns it, and a later search that
    lands on the same id is refused rather than allowed to share it. The refused
    slug is reported, and data/manual-matches.json is where a person settles it.
    """

    holder_of: dict[int, str] = field(default_factory=dict)

    def held_by_another(self, tmdb_id: int, slug: str) -> str | None:
        """Return the slug already holding this id, or None if this slug may take it."""
        holder = self.holder_of.get(tmdb_id)
        return holder if holder is not None and holder != slug else None

    def claim(self, tmdb_id: int, slug: str) -> None:
        """Record this slug as the holder of this id, unless another slug holds it."""
        self.holder_of.setdefault(tmdb_id, slug)


class HistoryFilm(NamedTuple):
    """One film to enrich, reduced to the fields TMDB needs."""

    slug: str
    title: str | None
    year: int | None
    tmdb_id: int | None


class MatchOutcome(Enum):
    """Why a film did or did not end up with a TMDB id.

    The four failures are kept apart because they have different lifetimes.
    ABSENT_FROM_TMDB is TMDB's own answer and is cached forever. The other three
    mean nothing was learned, so nothing is cached and the next run tries again.

    ID_BELONGS_TO_ANOTHER_FILM is not TMDB failing. It is this script declining
    to file two films under one id, which is the only thing that keeps the film
    count honest. Nothing is cached, because the right id is still out there and
    a person can supply it.
    """

    MATCHED = "matched"
    ABSENT_FROM_TMDB = "TMDB has no such film"
    TMDB_DID_NOT_ANSWER = "TMDB did not answer"
    NO_TITLE_TO_SEARCH = "the history entry has no title"
    ID_BELONGS_TO_ANOTHER_FILM = "the best match already belongs to another film"


class Match(NamedTuple):
    """The result of identifying one film on TMDB, with the id when there is one.

    `wanted_id` and `held_by` are filled only for ID_BELONGS_TO_ANOTHER_FILM, so
    the summary can name the id the search wanted and the slug that has it.
    """

    outcome: MatchOutcome
    tmdb_id: int | None = None
    wanted_id: int | None = None
    held_by: str | None = None


class TakenId(NamedTuple):
    """A slug whose best search result already belongs to another film."""

    slug: str
    tmdb_id: int
    held_by: str


class ForgottenLookups(NamedTuple):
    """One TMDB id that two or more cached slugs held, and the rows dropped for it."""

    tmdb_id: int
    slugs: list[str]


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
    # an answer, so it is cached and the search is never run for the film again.
    wrong_id_slugs: list[str] = field(default_factory=list)

    # The search's best answer was an id another slug already holds, so it was
    # refused. Nothing cached: the right id still exists and a person can give it.
    id_taken: list[TakenId] = field(default_factory=list)

    # Cached lookups dropped because one id was held by more than one slug.
    # Those slugs were searched again in this same run.
    forgotten_lookups: list[ForgottenLookups] = field(default_factory=list)

    def record_unresolved(self, slug: str, outcome: MatchOutcome) -> None:
        """File a film that got no TMDB id under the reason it got none.

        Handles the three reasons that need nothing but the slug.
        ID_BELONGS_TO_ANOTHER_FILM carries an id and another slug with it, so the
        caller files that one itself.
        """
        buckets = {
            MatchOutcome.ABSENT_FROM_TMDB: self.absent_slugs,
            MatchOutcome.TMDB_DID_NOT_ANSWER: self.search_failed_slugs,
            MatchOutcome.NO_TITLE_TO_SEARCH: self.unsearchable_slugs,
        }
        bucket = buckets.get(outcome)
        if bucket is None:
            raise ValueError(
                f"{outcome.name} carries more than a slug, so file it where it"
                " arises rather than here."
            )
        bucket.append(slug)


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


def forget_contradicted_lookups(
    connection: sqlite3.Connection,
    known_lookups: dict[str, int | None],
    manual_matches: dict[str, int],
) -> list[ForgottenLookups]:
    """Drop every cached lookup where one TMDB id is held by more than one slug.

    A TMDB id names one film, so two slugs holding one id is a contradiction. At
    least one of those searches was wrong and the cache does not say which, so
    neither row can be trusted. Left alone the contradiction is permanent: a
    cached lookup is never searched again, so both films keep whichever one's
    runtime, genres and cast the id belongs to, week after week.

    Dropping both rows is what lets this run search them again under the rule
    that refuses an id another slug already holds. Only the `lookups` rows go.
    Nothing downloaded is deleted, so a slug that lands on the same id again
    costs no request at all.

    A slug that data/manual-matches.json names for that id keeps its row: a
    person settled that one, and this run has nothing better to offer. Two slugs
    named for the same id by hand are left alone entirely, which is how a genuine
    alias is told that it may stay.

    Mutates `known_lookups` to match, and returns what it dropped so the run can
    report it.
    """
    holders: dict[int, list[str]] = defaultdict(list)
    for slug, tmdb_id in known_lookups.items():
        if tmdb_id is not None:
            holders[tmdb_id].append(slug)

    forgotten: list[ForgottenLookups] = []
    for tmdb_id, slugs in sorted(holders.items()):
        if len(slugs) < 2:
            continue

        dropped = sorted(
            slug for slug in slugs if manual_matches.get(slug) != tmdb_id
        )
        if not dropped:
            continue

        for slug in dropped:
            connection.execute("DELETE FROM lookups WHERE slug = ?", (slug,))
            known_lookups.pop(slug, None)
        forgotten.append(ForgottenLookups(tmdb_id, dropped))

    connection.commit()
    return forgotten


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


def comparable_title(title: str) -> str:
    """Reduce a title to what Letterboxd and TMDB can be expected to agree on.

    The two sites write the same film with different case, different punctuation
    and different spacing: "Kimetsu no Yaiba - Sibling's Bond" against
    "Kimetsu no Yaiba: Siblings Bond". None of that tells two films apart.
    Letters, digits and single spaces do.
    """
    return " ".join(TITLE_PUNCTUATION.sub(" ", title).casefold().split())


def release_year_of(result: dict[str, Any]) -> int | None:
    """Read the release year out of one search result, or None when it has no date."""
    release_date = result.get("release_date")
    if isinstance(release_date, str) and len(release_date) >= 4 and release_date[:4].isdigit():
        return int(release_date[:4])
    return None


class SearchCandidate(NamedTuple):
    """One search result, measured against the film the history asked about."""

    tmdb_id: int
    title_matches: bool
    year_gap: int | None
    position: int

    @property
    def is_plausible(self) -> bool:
        """Report whether this result could be the film that was asked for.

        An exact title is enough on its own, whatever the release year says.
        Anything else has to be dated within a year of what Letterboxd records,
        because a different title and a different year is a different film.
        """
        if self.title_matches:
            return True
        return self.year_gap is not None and self.year_gap <= MAX_RELEASE_YEAR_GAP

    @property
    def rank(self) -> tuple[int, int, int]:
        """Sort key, best first: exact title, then closest release year, then TMDB's order.

        Title outranks year because it is the stronger evidence. TMDB's own order
        is popularity, which decides nothing about identity and so comes last: it
        is what made "The Beasts" resolve to Fantastic Beasts.
        """
        return (
            0 if self.title_matches else 1,
            MAX_RELEASE_YEAR_GAP + 1 if self.year_gap is None else self.year_gap,
            self.position,
        )


def rank_search_results(
    results: list[Any], film: HistoryFilm, title_must_match: bool
) -> list[SearchCandidate]:
    """Measure every search result against the film, best answer first.

    Results that could not be this film at all are left out, so the caller can
    take the first one it is allowed to have.

    `title_must_match` drops the release year as evidence and leaves the title to
    carry the match alone. The search that asks TMDB for one year has already
    narrowed the field, so a near-enough year means something there. The search
    that asks for a title across all years has narrowed nothing, and a search for
    "Demon Slayer: Kimetsu no Yaiba" returns twenty Demon Slayer films of which
    one is a year away from the one asked about. Taking that is guessing.
    """
    wanted = comparable_title(film.title or "")
    candidates: list[SearchCandidate] = []

    for position, result in enumerate(results):
        if not isinstance(result, dict):
            continue
        tmdb_id = result.get("id")
        if not isinstance(tmdb_id, int):
            continue

        # TMDB answers with the title in the requested language and with the
        # title the film was released under. A member logs either one.
        titles = {
            comparable_title(value)
            for value in (result.get("title"), result.get("original_title"))
            if isinstance(value, str)
        }
        year = release_year_of(result)

        candidates.append(
            SearchCandidate(
                tmdb_id=tmdb_id,
                title_matches=wanted in titles,
                year_gap=None if year is None or not film.year else abs(year - film.year),
                position=position,
            )
        )

    def may_answer_this_search(candidate: SearchCandidate) -> bool:
        """Report whether this candidate is allowed to answer this particular search."""
        return candidate.title_matches if title_must_match else candidate.is_plausible

    return sorted(
        (candidate for candidate in candidates if may_answer_this_search(candidate)),
        key=lambda candidate: candidate.rank,
    )


def search_film(
    client: httpx.Client,
    auth_params: dict[str, str],
    film: HistoryFilm,
    outage: OutageDetector,
    empty_searches: EmptySearchDetector,
    claimed: ClaimedIds,
) -> Match:
    """Find a film's TMDB id by title and year, taking the result that best answers it.

    The best answer is an exact title match, then the result released closest to
    the year Letterboxd records. TMDB returns its results by popularity, and
    taking the first of them is what gave one member six pairs of films that had
    become one: the popular "Fantastic Beasts: The Secrets of Dumbledore" for a
    search for "The Beasts", "Part 1" for a search for "Part 2".

    A result that could not be this film is skipped rather than ranked: a
    different title released more than a year from the recorded one.

    An id another slug already holds is refused, and the outcome says so. Two
    slugs holding one id would be two of the member's films counted as one, and
    the refusal is what leaves the second one visible instead.

    When the year finds nothing the title is tried alone, because Letterboxd and
    TMDB disagree on the release year of festival and re-release titles. That
    second search has to match the title exactly: it has dropped the year as
    evidence, so nothing else is left to tell one film from another.

    An empty result list and an unanswered request are told apart in the returned
    outcome. Only the empty result list means TMDB has no such film, and a long
    run of those stops the run rather than writing the answer over the library.
    """
    if not film.title:
        return Match(MatchOutcome.NO_TITLE_TO_SEARCH)

    # Each attempt is a query and how much the result has to prove. The narrow
    # search asks TMDB for one year, so a near-enough year is evidence. The wide
    # search asks across every year, so only the title is left to go on.
    attempts: list[tuple[dict[str, Any], bool]] = []
    if film.year:
        attempts.append(({"query": film.title, "year": film.year}, False))
    attempts.append(({"query": film.title}, True))

    refused: SearchCandidate | None = None
    refused_holder: str | None = None

    for search_params, title_must_match in attempts:
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

        for candidate in rank_search_results(
            body.get("results") or [], film, title_must_match
        ):
            holder = claimed.held_by_another(candidate.tmdb_id, film.slug)
            if holder is None:
                empty_searches.note_film_found()
                return Match(MatchOutcome.MATCHED, candidate.tmdb_id)
            if refused is None:
                refused, refused_holder = candidate, holder

    if refused is not None and refused_holder is not None:
        # Every answer good enough to take was already another film's. Say so
        # rather than caching "TMDB has no such film", which is not what
        # happened and would keep this film out of the panel for good.
        return Match(
            MatchOutcome.ID_BELONGS_TO_ANOTHER_FILM,
            wanted_id=refused.tmdb_id,
            held_by=refused_holder,
        )

    empty_searches.note_nothing_found()
    return Match(MatchOutcome.ABSENT_FROM_TMDB)


def resolve_tmdb_id(
    film: HistoryFilm,
    manual_matches: dict[str, int],
    known_lookups: dict[str, int | None],
    claimed: ClaimedIds,
    connection: sqlite3.Connection,
    client: httpx.Client,
    auth_params: dict[str, str],
    outage: OutageDetector,
    empty_searches: EmptySearchDetector,
) -> Match:
    """Decide which TMDB film a history entry is, and record a real answer.

    The order is deliberate: a hand written match, then the id the feed gave,
    then TMDB's own answer that there is no such film, then a search.

    The feed's id comes before a cached "no such film" because it may be the
    correction for it. A cached negative records that some id was wrong, never
    which id, so an id the feed carries this week cannot be told apart from the
    one already disproved. Trying it costs one request a week for a film whose id
    stays wrong, and it is the only thing that can bring back a film whose id was
    fixed. A cached negative that a working id replaces is overwritten here, so
    it stops standing in the film's way. The hand written match still wins over
    all of it, so data/manual-matches.json remains the way to correct any of
    these.

    Every id this returns is claimed for the slug, so no later search can take
    the same id and turn two of the member's films into one.

    Only an answer from TMDB reaches the lookups table. A search that failed
    leaves no row, so the next run searches that film again instead of treating
    an outage as proof the film does not exist. TMDB's answer that it has no
    such film is written by the caller once the run has finished, for the reason
    given there.
    """
    manual_id = manual_matches.get(film.slug)
    if manual_id is not None:
        if known_lookups.get(film.slug) != manual_id:
            remember_lookup(connection, film.slug, manual_id)
            known_lookups[film.slug] = manual_id
        claimed.claim(manual_id, film.slug)
        return Match(MatchOutcome.MATCHED, manual_id)

    if film.tmdb_id:
        if known_lookups.get(film.slug) != film.tmdb_id:
            remember_lookup(connection, film.slug, film.tmdb_id)
            known_lookups[film.slug] = film.tmdb_id
        claimed.claim(film.tmdb_id, film.slug)
        return Match(MatchOutcome.MATCHED, film.tmdb_id)

    if film.slug in known_lookups and known_lookups[film.slug] is None:
        return Match(MatchOutcome.ABSENT_FROM_TMDB)

    cached_id = known_lookups.get(film.slug)
    if cached_id is not None:
        claimed.claim(cached_id, film.slug)
        return Match(MatchOutcome.MATCHED, cached_id)

    match = search_film(client, auth_params, film, outage, empty_searches, claimed)

    if match.outcome is MatchOutcome.MATCHED and match.tmdb_id is not None:
        remember_lookup(connection, film.slug, match.tmdb_id)
        known_lookups[film.slug] = match.tmdb_id
        claimed.claim(match.tmdb_id, film.slug)

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

    Raises TmdbUnavailable when TMDB stops answering, TmdbDeniesEveryId when it
    answers that one id after another is missing, and TmdbFindsNoFilm when it
    finds nothing for one search after another. All three end the run early and
    on purpose. Everything downloaded before that point stays cached, and nothing
    is recorded for the films those stops were about.

    `transport` replaces the HTTP layer. A run leaves it unset and talks to TMDB.
    Tests pass an httpx.MockTransport to exercise outages without a network.
    """
    credential = read_credential()
    headers, auth_params = build_authentication(credential)
    manual_matches = load_manual_matches()

    summary = RunSummary(films_in_history=len(films))
    outage = OutageDetector()
    missing_ids = MissingIdDetector()
    empty_searches = EmptySearchDetector()
    connection = open_cache()

    try:
        known_lookups = read_lookups(connection)
        cached_ids = read_cached_film_ids(connection)

        # A cache written before ids were claimed can hold one id under two
        # slugs. That is a contradiction, not a fact, so it is dropped here and
        # the slugs are searched again below under the rule that refuses it.
        summary.forgotten_lookups = forget_contradicted_lookups(
            connection, known_lookups, manual_matches
        )
        for forgotten in summary.forgotten_lookups:
            print(
                f"  forgot the cached id {forgotten.tmdb_id} for"
                f" {', '.join(forgotten.slugs)}: one id cannot be two films"
            )

        # A hand written match settles an id, so it holds that id before any
        # search runs and no search can take it away.
        claimed = ClaimedIds()
        for slug, tmdb_id in manual_matches.items():
            claimed.claim(tmdb_id, slug)
        for slug, tmdb_id in known_lookups.items():
            if tmdb_id is not None:
                claimed.claim(tmdb_id, slug)

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
                    claimed,
                    connection,
                    client,
                    auth_params,
                    outage,
                    empty_searches,
                )

                if match.outcome is MatchOutcome.ID_BELONGS_TO_ANOTHER_FILM:
                    # Refusing costs this film its details for now. Sharing the
                    # id would have cost the member a film out of every count,
                    # and said nothing about it.
                    summary.id_taken.append(
                        TakenId(film.slug, match.wanted_id, match.held_by)
                    )
                    continue

                if match.tmdb_id is None:
                    summary.record_unresolved(film.slug, match.outcome)
                    continue

                if match.tmdb_id in cached_ids:
                    summary.already_cached += 1
                    # The run holds this film's details, so it is a film that did
                    # not go wrong. Both detectors count runs of films, so both
                    # start again here, exactly as they do after a download.
                    missing_ids.note_film_has_details()
                    empty_searches.note_film_found()
                    continue

                details = fetch_film_details(
                    client, auth_params, match.tmdb_id, film.slug, outage
                )

                if details is NO_SUCH_RECORD:
                    # TMDB says nothing carries this id, so the id is wrong. The
                    # answer is written after the loop rather than here: see the
                    # note there for why a negative waits for the run to end.
                    missing_ids.note_id_missing()
                    summary.wrong_id_slugs.append(film.slug)
                    continue

                if not isinstance(details, dict):
                    # The only value left is None: TMDB gave no answer at all, so
                    # nothing is recorded and the next run asks again.
                    summary.download_failed_slugs.append(film.slug)
                    continue

                store_film(connection, match.tmdb_id, film.slug, details)
                cached_ids.add(match.tmdb_id)
                missing_ids.note_film_has_details()
                empty_searches.note_film_found()
                summary.fetched += 1
                print(f"  fetched {film.slug} ({match.tmdb_id})")

            # Record TMDB's two permanent negatives only now the run has got this
            # far: "no film has this id" and "no film matches this title". A run
            # of either long enough to be the service raises out of the loop above
            # and never reaches this line, so it leaves no film written off.
            # A negative is permanent and worth one run's wait to be sure of; a
            # downloaded film is neither, which is why store_film commits as it
            # goes.
            for slug in summary.wrong_id_slugs + summary.absent_slugs:
                remember_lookup(connection, slug, None)
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
    print(f"id was another film:  {len(summary.id_taken)}")
    print(f"TMDB never answered:  {never_answered}")
    print(f"no title to search:   {len(summary.unsearchable_slugs)}")
    print(f"contradictions fixed: {len(summary.forgotten_lookups)}")

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
            "rather than the network. That answer is now cached, so none of them is\n"
            "searched for again. An id that came from the Letterboxd feed is still\n"
            "tried once each week, because a corrected id is the one thing that can\n"
            "bring the film back, so those entries appear here again until\n"
            "Letterboxd changes the id. To settle any of them now, add the right id\n"
            f"to {MANUAL_MATCHES_FILE} as \"slug\": tmdb_id\n"
            "and run this script again:"
        )
        for slug in summary.wrong_id_slugs:
            print(f"  {slug}")

    if summary.id_taken:
        print("")
        print(
            "The best match for these films is a TMDB id that another film in this\n"
            "history already holds. One id is one film, so the id was refused rather\n"
            "than shared: sharing it would have counted the two as one film in every\n"
            "total, with nothing said about it. Nothing was cached, so give each one\n"
            f"its own id in {MANUAL_MATCHES_FILE}\n"
            'as "slug": tmdb_id and run this script again:'
        )
        for taken in summary.id_taken:
            print(f"  {taken.slug} wanted {taken.tmdb_id}, which {taken.held_by} holds")

    if summary.forgotten_lookups:
        print("")
        print(
            "These cached lookups held one TMDB id under more than one slug, which\n"
            "cannot be true of one film. Both rows were dropped and both slugs were\n"
            "searched again in this run. Check that each film above ended up with an\n"
            "id of its own, and settle any that did not in\n"
            f"{MANUAL_MATCHES_FILE}:"
        )
        for forgotten in summary.forgotten_lookups:
            print(f"  {forgotten.tmdb_id} was held by {', '.join(forgotten.slugs)}")

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
    except (
        CredentialRejected,
        TmdbUnavailable,
        TmdbDeniesEveryId,
        TmdbFindsNoFilm,
    ) as error:
        # Each of these means the rest of the run could only fail or record
        # something untrue, so the run stops and the exit code tells the weekly
        # workflow to try again later.
        print(error, file=sys.stderr)
        raise SystemExit(1)

    report(summary)


if __name__ == "__main__":
    main()
