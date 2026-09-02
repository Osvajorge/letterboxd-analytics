"""Download TMDB metadata for every film in the watch history.

The stats panel needs facts Letterboxd does not publish: runtime, genres,
countries, languages, cast, crew, studios, collections and keywords. TMDB holds
all of them, so each film is downloaded once and kept in a local SQLite cache.

Which TMDB record a film is comes from one file, `data/tmdb-ids.json`:

    "the-beasts-2022": { "tmdb_id": 848685, "tmdb_type": "movie" }

`scripts/resolve_tmdb_ids.py` builds that file by reading each film's own
Letterboxd page, which states the TMDB id and type Letterboxd itself uses. That
is the answer to "which film is this", so this script reads it and stops there.

It used to guess instead, by searching TMDB for the title and the year. The guess
was wrong for 43 of this member's 827 films. Six of the 43 were visible, because
they landed on an id another slug already held. The other 37 were silent:
"Aladdin" 2019 was filed as the 1992 animated film, and no message said so. The
searching, the result ranking and all the bookkeeping written to contain them are
gone. Do not bring any of it back. A wrong id files one real film's runtime,
cast, genres and countries under a different film, and no later step can see that
it happened.

The map answers in three ways, and all three are answers:

    an id with the type "movie"   the film to download.
    an id with any other type     TMDB holds this as television, and the film
                                  endpoint cannot serve a television record, so
                                  it is skipped rather than requested and then
                                  reported as a mysterious failure.
    a null id                     Letterboxd has no TMDB record for this film.
                                  Seventeen of this member's films are in this
                                  state. It is a settled answer, so it costs no
                                  request this week and none in any later week.

A slug the map does not mention is none of those. It is a film nobody has
resolved yet, usually one the RSS feed added after the map was last built. The
summary names it and names `scripts/resolve_tmdb_ids.py` as the step that settles
it. Nothing is searched for and nothing is guessed.

Letterboxd can also publish an id TMDB's film endpoint does not hold, and it does
so for 21 of this member's 827 films, most of them anthology episodes logged as
films. That is the two sites disagreeing, and only TMDB can settle it, so nothing
is written down for those films. Each run asks once more, which costs 21 requests
a week and is the one thing that brings a film back if TMDB restores the record.

`data/manual-matches.json` still outranks the map, because a person who has
checked a film outranks every automatic source.

A cache written during the guessing era holds records downloaded under the wrong
id, so every run starts by putting the cache back in step with the map:

  * A cached identity that disagrees with the map is overwritten by the map.
  * A downloaded record whose id the map now gives to no film in this library is
    deleted. Nothing points at it any more, and leaving it lets a later step read
    one film's runtime and cast for another.
  * A downloaded record the map gives to a different film keeps its payload,
    which is TMDB's record for that id and is correct, and is relabelled with the
    film that owns it.

Each film whose right id is not in the cache is then downloaded, which is the
re-fetch that repairs it.

A request that never got an answer is not an answer. When TMDB is unreachable,
failing, or rate limiting past the retries, nothing is written for that film and
the next run asks again. Recording anything there would drop the film from the
stats over a few minutes of downtime.

When TMDB stops answering altogether, or answers that one id after another is
missing, the run gives up rather than spending the same retries on every film
left. It exits non-zero and asks to be run again later.

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

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - depends on which requirements file ran
    def load_dotenv(*_args: object, **_kwargs: object) -> None:
        """Stand in for python-dotenv when it is not installed.

        Reading a local .env is a convenience for running this script by hand,
        so python-dotenv lives in requirements-dev.txt. GitHub Actions passes
        TMDB_API_KEY straight into the step's environment and no .env exists
        there, so the weekly run installs neither the package nor anything it
        depends on. There is nothing to read and nothing to do.
        """

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.config import (
    DATA,
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

# The map of slug to TMDB id and type. scripts/resolve_tmdb_ids.py writes it and
# this script only reads it.
TMDB_IDS_FILE = DATA / "tmdb-ids.json"

# The one type this script can download. TMDB serves anything else, television
# above all, from endpoints this cache has no room for.
TMDB_FILM_TYPE = "movie"

# One request per film covers metadata, cast, crew and keywords.
APPEND_TO_RESPONSE = "credits,keywords"

# TMDB allows far more than this. A quarter second keeps a full run of several
# hundred films down to a few minutes while staying well inside their limits.
DELAY_BETWEEN_REQUESTS = 0.25

MAX_ATTEMPTS_PER_REQUEST = 3
FALLBACK_RETRY_AFTER_SECONDS = 5.0

# The longest a rate limit may park this run.
#
# Retry-After is a number chosen by whatever answered the request, and it is
# handed straight to time.sleep. A real TMDB backoff is seconds; "999999999" is
# eleven thousand days, and three of those per request is a run that never ends
# and never says why. Two minutes is longer than TMDB has ever asked for and
# short enough that a job timeout is not the only thing that ends the wait.
MAX_RETRY_AFTER_SECONDS = 120.0

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
# A 404 for one film's id is an answer about that id. A 404 for every id in a long
# run is not, because a service that holds nothing at all is the likelier reading
# than a library that went dead all at once.
#
# The threshold has to clear the longest run of ids that really are dead, and
# those ids are not scattered. Letterboxd publishes a TMDB id for an anthology
# episode, TMDB's film endpoint rightly holds no such record, and this member logs
# a series in one sitting, so the dead ids arrive together: the longest run in
# this library today is eight, every one of them a Love Death and Robots episode.
# Ten stood two films from stopping a perfectly healthy run, and the member keeps
# logging that series.
#
# Twenty five clears that with room to grow, costs about seven seconds of
# requesting to establish, and is still reached in the first seconds of a run
# where TMDB answers 404 to everything, because a 404 costs one request and no
# retries. Nothing is written down for a missing id either way, so the run of them
# ending early saves time rather than preventing damage.
GIVE_UP_AFTER_MISSING_IDS_IN_A_ROW = 25

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


class NoSuchRecord:
    """TMDB's answer that the thing asked for does not exist.

    This is an answer, so it is kept apart from None, which means no answer at
    all. It says the id itself is wrong, which is a different thing from the
    download having failed, and the summary keeps the two apart for the reader.
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
    failures. A few of this library's ids are dead on their own, because
    Letterboxd publishes a TMDB id for an anthology episode and TMDB's film
    endpoint holds no such record. Those arrive in runs, since the member logs a
    series in one sitting, which is why the threshold is set well above the
    longest such run rather than at the first surprise.

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
            "A run this long is longer than any stretch of episodes this library"
            " holds, so TMDB is answering strangely rather than reporting real gaps."
            " Nothing was written off as missing, so nothing is lost, and everything"
            " downloaded earlier in the run is still cached. Run this script again"
            " later. If it stops here again, check whether TMDB is healthy before"
            " treating any of these ids as wrong."
        )


class Identity(Enum):
    """What the id map says one film is.

    The four answers have different lifetimes, which is why they are kept apart.
    A_FILM is downloaded. NO_TMDB_RECORD and NOT_A_FILM are settled answers that
    cost no request now and none later. UNRESOLVED is the only one that asks the
    reader to do something, and what it asks for is a run of
    scripts/resolve_tmdb_ids.py.
    """

    A_FILM = "TMDB holds this as a film"
    NO_TMDB_RECORD = "Letterboxd has no TMDB record for this film"
    NOT_A_FILM = "the id map does not state that this is a film"
    UNRESOLVED = "no one has read this film's Letterboxd page yet"


class FilmIdentity(NamedTuple):
    """One film's identity: what the map says it is, and its id when it has one."""

    outcome: Identity
    tmdb_id: int | None = None


class CorrectedIdentity(NamedTuple):
    """A cached identity the map disagreed with, and both answers."""

    slug: str
    cached_id: int | None
    map_id: int | None


class DiscardedRecord(NamedTuple):
    """A downloaded record that belonged to no film in this library, and its id."""

    tmdb_id: int
    downloaded_for: str


class CacheAlignment(NamedTuple):
    """What one run changed to bring the cache back in step with the id map."""

    newly_recorded: int
    corrected: list[CorrectedIdentity]
    discarded: list[DiscardedRecord]
    relabelled: int


def nothing_to_align() -> CacheAlignment:
    """Return the alignment of a run that has not looked at the cache yet."""
    return CacheAlignment(newly_recorded=0, corrected=[], discarded=[], relabelled=0)


@dataclass
class RunSummary:
    """What one enrichment run did, in the terms the reader cares about.

    Films that got no metadata are listed by reason, not lumped together, because
    the reader does something different about each reason, and for two of the
    reasons that something is nothing at all.
    """

    films_in_history: int = 0
    already_downloaded: int = 0
    downloaded: int = 0

    # The map says Letterboxd has no TMDB record for these. Settled, so no
    # request is spent on them now and none will be in any later run.
    no_tmdb_record_slugs: list[str] = field(default_factory=list)

    # The map gives these an id but does not state that the id is a film: it is
    # television, or the type is null because the film page did not say. Neither
    # is something the film endpoint can serve, so they are skipped rather than
    # asked for.
    not_a_film_slugs: list[str] = field(default_factory=list)

    # The map does not mention these at all, so nothing knows what they are yet.
    unresolved_slugs: list[str] = field(default_factory=list)

    # TMDB answered 404 for the id the map gives, so TMDB and Letterboxd disagree
    # about this film. Nothing is cached, and the next run asks again.
    id_not_on_tmdb_slugs: list[str] = field(default_factory=list)

    # The id is known but the details request never got an answer.
    download_failed_slugs: list[str] = field(default_factory=list)

    alignment: CacheAlignment = field(default_factory=nothing_to_align)

    def record_skipped(self, slug: str, outcome: Identity) -> None:
        """File a film that needed no download under the reason it needed none.

        Two of the three reasons are settled answers that ask nothing of anyone.
        The third, an unresolved film, is the only one a person acts on.
        """
        buckets = {
            Identity.NO_TMDB_RECORD: self.no_tmdb_record_slugs,
            Identity.NOT_A_FILM: self.not_a_film_slugs,
            Identity.UNRESOLVED: self.unresolved_slugs,
        }
        bucket = buckets.get(outcome)
        if bucket is None:
            raise ValueError(
                f"{outcome.name} is a film to download, so it does not belong"
                " here."
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
    """Read how long TMDB asked us to wait after a rate limit, within reason.

    The answer is clamped at both ends. The floor keeps a busy loop from forming
    on a zero. The ceiling matters more: without it the header decides how long
    this process sleeps, and a header is not something this pipeline controls.
    """
    try:
        asked = float(response.headers.get("Retry-After", ""))
    except ValueError:
        # The header is absent or is an HTTP date rather than a count of seconds.
        return FALLBACK_RETRY_AFTER_SECONDS

    if asked != asked:
        # "nan" parses as a float and then compares false against everything, so
        # it would slip past both bounds below.
        return FALLBACK_RETRY_AFTER_SECONDS

    return min(max(1.0, asked), MAX_RETRY_AFTER_SECONDS)


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
      that id, so the id and not the network is what went wrong.
    - None, meaning no usable answer from TMDB at all.

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


def read_cached_identities(connection: sqlite3.Connection) -> dict[str, int | None]:
    """Read which TMDB film the cache currently believes each slug is."""
    rows = connection.execute("SELECT slug, tmdb_id FROM lookups").fetchall()
    return {slug: tmdb_id for slug, tmdb_id in rows}


def remember_identity(
    connection: sqlite3.Connection, slug: str, tmdb_id: int | None
) -> None:
    """Record in the cache which TMDB film a slug is.

    This copies the map's answer into the `lookups` table the data contract
    defines, so the steps that read the cache rather than the map see the same
    thing. A null id is written on purpose: it is the answer that TMDB has no
    film record for this slug, and the contract's table is where that lives.

    The caller commits, so a whole pass of these is one transaction.
    """
    connection.execute(
        "INSERT OR REPLACE INTO lookups (slug, tmdb_id, resolved_at) VALUES (?, ?, ?)",
        (slug, tmdb_id, timestamp()),
    )


def read_cached_film_ids(connection: sqlite3.Connection) -> set[int]:
    """Read the ids of films fully downloaded, so they are not downloaded twice.

    A film is downloaded when the cache holds both of its halves. One request
    writes a `films` row and a `credits` row, so a film with only the first is a
    film whose download did not finish, and asking for it again is the only thing
    that can finish it.

    The films table alone used to answer this, which made every such film
    permanently invisible. store_film writes no credits row when the response
    carries no credits key, so a single answer of that shape left the film
    counted as already downloaded from then on, contributing no cast, no director
    and no crew while every run exited 0. The same gap swallows a films row
    restored from an Actions cache written before credits were stored, which
    ensure_table already plans for by adding the columns an older file lacks.

    The cost of asking again is one request for a film TMDB really does serve
    without credits. That is the right way round: a wasted request is visible in
    the run and a silently missing director is not.
    """
    rows = connection.execute(
        "SELECT tmdb_id FROM films WHERE tmdb_id IN (SELECT tmdb_id FROM credits)"
    ).fetchall()
    return {row[0] for row in rows}


def align_cache_with_the_map(
    connection: sqlite3.Connection, settled_ids: dict[str, int | None]
) -> CacheAlignment:
    """Bring the cache back in step with the id map, and report what had to change.

    This is what repairs a cache filled in by the old title and year search. That
    search got 43 of this member's 827 films wrong, so the cache holds records
    downloaded under an id that belongs to a different film. Nothing downstream
    can spot that on its own: the payload is real TMDB data, it is simply data
    about the wrong film.

    `settled_ids` gives the TMDB film id for every film the map has an answer
    about, and None for a film that has no film record to download. A film the
    map does not mention has no settled answer, so it does not appear there at
    all and nothing cached under it is touched.

    Three things can be out of step, and each has one right answer:

    - A cached identity that disagrees with the map. The map wins, always, and
      the corrected row is what makes the film download under its own id below.
    - A downloaded record whose id the map gives to no film in this library. It
      was downloaded for a film that turned out to be something else, so nothing
      points at it now. It is deleted, along with its credits, because a step
      that falls back to the `films` table would otherwise read it as that film.
    - A downloaded record the map gives to a different film in this library. The
      payload is TMDB's record for that id and is correct, so it is kept and
      relabelled with the film that owns it. Deleting it would only mean
      downloading the same bytes again.
    """
    cached_identities = read_cached_identities(connection)

    newly_recorded = 0
    corrected: list[CorrectedIdentity] = []
    for slug, map_id in settled_ids.items():
        if slug not in cached_identities:
            remember_identity(connection, slug, map_id)
            newly_recorded += 1
            continue
        if cached_identities[slug] == map_id:
            continue
        corrected.append(CorrectedIdentity(slug, cached_identities[slug], map_id))
        remember_identity(connection, slug, map_id)

    # Which film owns each id. Sorted so that two runs of the same data pick the
    # same owner if a manual match ever points two slugs at one record.
    owner_of_id: dict[int, str] = {}
    for slug in sorted(settled_ids):
        map_id = settled_ids[slug]
        if map_id is not None:
            owner_of_id.setdefault(map_id, slug)

    discarded: list[DiscardedRecord] = []
    relabelled = 0
    for tmdb_id, downloaded_for in connection.execute(
        "SELECT tmdb_id, slug FROM films"
    ).fetchall():
        if downloaded_for not in settled_ids:
            # Nothing settled says this record is wrong, so it stays. A film the
            # member deleted keeps its record here until the cache is rebuilt.
            continue
        if settled_ids[downloaded_for] == tmdb_id:
            continue

        owner = owner_of_id.get(tmdb_id)
        if owner is None:
            connection.execute("DELETE FROM films WHERE tmdb_id = ?", (tmdb_id,))
            connection.execute("DELETE FROM credits WHERE tmdb_id = ?", (tmdb_id,))
            discarded.append(DiscardedRecord(tmdb_id, downloaded_for))
            continue

        connection.execute(
            "UPDATE films SET slug = ? WHERE tmdb_id = ?", (owner, tmdb_id)
        )
        relabelled += 1

    connection.commit()
    return CacheAlignment(newly_recorded, corrected, discarded, relabelled)


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
    match gets corrected by hand, so it wins over every automatic answer,
    including the id map.
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


def read_identity(record: dict[str, Any]) -> FilmIdentity | None:
    """Turn one entry of the id map into an identity, or None if it says nothing.

    None means this entry is not an answer, so the film is reported as unresolved
    rather than settled. Only an entry that says `"tmdb_id": null` is the settled
    answer that Letterboxd has no TMDB record; a missing or malformed field is a
    damaged entry, and treating it as settled would write a film off in silence.
    """
    if "tmdb_id" not in record:
        return None

    tmdb_id = record["tmdb_id"]
    if tmdb_id is None:
        return FilmIdentity(Identity.NO_TMDB_RECORD)
    if not isinstance(tmdb_id, int) or isinstance(tmdb_id, bool):
        return None

    if record.get("tmdb_type") != TMDB_FILM_TYPE:
        # The test is positive on purpose: a film is an entry that SAYS it is a
        # film. Television, anything else TMDB does not serve from /movie, and an
        # id whose type is null or absent all fail it, which turns a request that
        # could only fail into a clean skip.
        #
        # The null case is why the test is written this way.
        # scripts/resolve_tmdb_ids.py writes an id with a null type whenever a
        # film page carries data-tmdb-id and the type attribute does not match,
        # and both scripts/build_stats.py and
        # scripts/enrich_people_and_collections.py refuse such a slug, so its id
        # is used nowhere else in the pipeline. Downloading it here would cache a
        # film no later step reads: three readers of one file, disagreeing about
        # what that file says.
        return FilmIdentity(Identity.NOT_A_FILM, tmdb_id)

    return FilmIdentity(Identity.A_FILM, tmdb_id)


def read_id_map() -> dict[str, FilmIdentity]:
    """Read data/tmdb-ids.json, which says which TMDB record each film is.

    The file is required, because it is the only thing that says what a film is.
    Without it there is nothing to enrich and nothing to guess from, so the run
    stops and names the script that builds it.
    """
    if not TMDB_IDS_FILE.exists():
        print(
            f"Nothing can be enriched: {TMDB_IDS_FILE} does not exist.\n"
            "It says which TMDB record each film is, read from each film's own\n"
            "Letterboxd page, and this script does not guess at that.\n"
            "Run scripts/resolve_tmdb_ids.py to build it, then run this script again.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    try:
        raw = json.loads(TMDB_IDS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(
            f"Could not read {TMDB_IDS_FILE} ({error}).\n"
            "Delete it and run scripts/resolve_tmdb_ids.py to rebuild it, then run\n"
            "this script again.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if not isinstance(raw, dict):
        print(
            f"{TMDB_IDS_FILE} must hold one object mapping slug to TMDB id and type,\n"
            'for example {"the-beasts-2022": {"tmdb_id": 848685, "tmdb_type": "movie"}}.\n'
            "Delete it and run scripts/resolve_tmdb_ids.py to rebuild it.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    id_map: dict[str, FilmIdentity] = {}
    for slug, record in raw.items():
        if not isinstance(slug, str) or not isinstance(record, dict):
            continue
        identity = read_identity(record)
        if identity is not None:
            id_map[slug] = identity

    # An entry that said nothing usable is simply absent here, so its film is
    # reported as unresolved and a person is told which script settles it.
    return id_map


def identify(
    slug: str, id_map: dict[str, FilmIdentity], manual_matches: dict[str, int]
) -> FilmIdentity:
    """Say which TMDB film one slug is.

    A hand written match wins, because a person who has checked the film outranks
    everything automatic. Otherwise the map answers, and if the map does not
    mention the slug then nobody has resolved that film yet. There is no third
    source and there must not be one: every source this script has ever had
    beyond these two was a guess, and the guessing is what put 43 films under the
    wrong id.
    """
    manual_id = manual_matches.get(slug)
    if manual_id is not None:
        # A person naming an id is naming a record in TMDB's film endpoint, so
        # the type question does not arise.
        return FilmIdentity(Identity.A_FILM, manual_id)

    identity = id_map.get(slug)
    return identity if identity is not None else FilmIdentity(Identity.UNRESOLVED)


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


def film_slugs(history: dict[str, Any]) -> list[str]:
    """List every film in the history once, in the order it first appears.

    A film watched more than once is one film here, because the cache holds one
    record per film and the slug is what identifies a film everywhere in this
    pipeline.
    """
    slugs = (
        entry["slug"]
        for entry in history.get("entries", [])
        if isinstance(entry, dict) and isinstance(entry.get("slug"), str) and entry["slug"]
    )
    return list(dict.fromkeys(slugs))


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
    slugs: list[str], transport: httpx.BaseTransport | None = None
) -> RunSummary:
    """Download every film the id map identifies and the cache does not already hold.

    Returns the counts the run prints: what was already there, what was
    downloaded, what the cache had to be put right, and each reason a film got
    nothing.

    Raises CredentialRejected when TMDB refuses the key, TmdbUnavailable when TMDB
    stops answering, and TmdbDeniesEveryId when it answers that one id after
    another is missing. All three end the run early and on purpose. Everything
    downloaded before that point stays cached, and nothing is recorded for the
    films those stops were about.

    `transport` replaces the HTTP layer. A run leaves it unset and talks to TMDB.
    Tests pass an httpx.MockTransport to exercise outages without a network.
    """
    credential = read_credential()
    headers, auth_params = build_authentication(credential)
    manual_matches = load_manual_matches()
    id_map = read_id_map()

    summary = RunSummary(films_in_history=len(slugs))
    outage = OutageDetector()
    missing_ids = MissingIdDetector()
    connection = open_cache()

    try:
        identities = {slug: identify(slug, id_map, manual_matches) for slug in slugs}

        # What this run will download, one film at a time: an id for a film TMDB
        # holds, and None for a film with nothing to download. A film nobody has
        # resolved is absent rather than None, because there is no answer for it
        # either way, and the alignment below must not act on a film it knows
        # nothing about.
        settled_ids = {
            slug: identity.tmdb_id if identity.outcome is Identity.A_FILM else None
            for slug, identity in identities.items()
            if identity.outcome is not Identity.UNRESOLVED
        }

        summary.alignment = align_cache_with_the_map(connection, settled_ids)
        for correction in summary.alignment.corrected:
            print(
                f"  {correction.slug}: the cache said {correction.cached_id},"
                f" the id map says {correction.map_id}"
            )
        for record in summary.alignment.discarded:
            print(
                f"  discarded the record for TMDB id {record.tmdb_id}, downloaded"
                f" for {record.downloaded_for}, which is a different film"
            )

        cached_ids = read_cached_film_ids(connection)

        with httpx.Client(
            base_url=TMDB_API_ROOT,
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
            headers=headers,
            transport=transport,
        ) as client:
            for slug in slugs:
                tmdb_id = settled_ids.get(slug)
                if tmdb_id is None:
                    # Nothing to download: TMDB has no record of this film, or
                    # holds it as television, or nobody has resolved it yet. The
                    # outcome says which, and the summary keeps the three apart.
                    summary.record_skipped(slug, identities[slug].outcome)
                    continue

                if tmdb_id in cached_ids:
                    summary.already_downloaded += 1
                    # The run holds this film's details, so it is a film that did
                    # not go wrong. The detector counts runs of films, so a
                    # cached film breaks that run exactly as a download does.
                    missing_ids.note_film_has_details()
                    continue

                details = fetch_film_details(
                    client, auth_params, tmdb_id, slug, outage
                )

                if details is NO_SUCH_RECORD:
                    # TMDB holds no film with the id Letterboxd publishes for this
                    # film, so the two disagree. Nothing is written: the id came
                    # from an authoritative source, and next week TMDB may hold it
                    # again.
                    missing_ids.note_id_missing()
                    summary.id_not_on_tmdb_slugs.append(slug)
                    continue

                if not isinstance(details, dict):
                    # The only value left is None: TMDB gave no answer at all, so
                    # nothing is recorded and the next run asks again.
                    summary.download_failed_slugs.append(slug)
                    continue

                store_film(connection, tmdb_id, slug, details)
                cached_ids.add(tmdb_id)
                missing_ids.note_film_has_details()
                summary.downloaded += 1
                print(f"  downloaded {slug} ({tmdb_id})")
    finally:
        connection.close()

    return summary


def report(summary: RunSummary) -> None:
    """Print what the run did, and what the reader can do about what it could not."""
    alignment = summary.alignment

    print("")
    print(f"films in history:      {summary.films_in_history}")
    print(f"already downloaded:    {summary.already_downloaded}")
    print(f"downloaded now:        {summary.downloaded}")
    print(f"no TMDB record:        {len(summary.no_tmdb_record_slugs)}")
    print(f"not stated as a film:  {len(summary.not_a_film_slugs)}")
    print(f"not resolved yet:      {len(summary.unresolved_slugs)}")
    print(f"id not on TMDB:        {len(summary.id_not_on_tmdb_slugs)}")
    print(f"TMDB never answered:   {len(summary.download_failed_slugs)}")
    print(f"identities recorded:   {alignment.newly_recorded}")
    print(f"identities corrected:  {len(alignment.corrected)}")
    print(f"records discarded:     {len(alignment.discarded)}")
    print(f"records relabelled:    {alignment.relabelled}")

    if summary.unresolved_slugs:
        print("")
        print(
            f"{TMDB_IDS_FILE} does not say what these films are,\n"
            "so nothing was downloaded for them and nothing was guessed. They are\n"
            "usually films the RSS feed added since the map was last built.\n"
            "Run scripts/resolve_tmdb_ids.py to read their Letterboxd pages, then\n"
            "run this script again:"
        )
        for slug in summary.unresolved_slugs:
            print(f"  {slug}")

    if summary.download_failed_slugs:
        print("")
        print(
            "These films have an id but their metadata never downloaded, because\n"
            "TMDB never answered. Nothing was recorded for them, so run this script\n"
            "again to retry them:"
        )
        for slug in summary.download_failed_slugs:
            print(f"  {slug}")

    if summary.id_not_on_tmdb_slugs:
        print("")
        print(
            "TMDB holds no film with the id Letterboxd publishes for these films, so\n"
            "the two sites disagree rather than the network having failed. Most of\n"
            "them are anthology episodes that this member logs as films, and TMDB\n"
            "holds no film record for an episode. Nothing was written down, so each\n"
            "run asks once more, which is what brings a film back if TMDB restores\n"
            "the record. To settle one now, add the right id to\n"
            f"{MANUAL_MATCHES_FILE} as \"slug\": tmdb_id\n"
            "and run this script again:"
        )
        for slug in summary.id_not_on_tmdb_slugs:
            print(f"  {slug}")

    if alignment.corrected:
        print("")
        print(
            "These films were cached under an id the map disagrees with, which is\n"
            "what the old title and year search left behind. The map won, and each\n"
            "of them was asked for again under its own id. Nothing else is needed\n"
            "here: any that could not be downloaded is listed above with the reason:"
        )
        for correction in alignment.corrected:
            print(
                f"  {correction.slug}: {correction.cached_id} became"
                f" {correction.map_id}"
            )

    if alignment.discarded:
        print("")
        print(
            "These downloaded records belonged to no film in this library. Each was\n"
            "downloaded for a film that turned out to be a different TMDB record, so\n"
            "they were deleted rather than left where a later step could read them\n"
            "as that film. Nothing is needed:"
        )
        for record in alignment.discarded:
            print(
                f"  TMDB id {record.tmdb_id}, downloaded for {record.downloaded_for}"
            )

    if summary.not_a_film_slugs:
        print("")
        print(
            "These have an id, and nothing that says the id is a film. Most are\n"
            "television, which TMDB's film endpoint does not hold. The rest have no\n"
            "type at all, because the Letterboxd page published an id without saying\n"
            "what kind of record it is. No request was made for either, and\n"
            "scripts/build_stats.py refuses the same ids, so anything downloaded for\n"
            "one would be cached and read by nothing. That is settled and needs\n"
            "nothing. They count in the film total and carry no runtime, genres or\n"
            "cast. If you have checked one and it is a film, set its tmdb_type to\n"
            f"\"{TMDB_FILM_TYPE}\" in\n"
            f"{TMDB_IDS_FILE}\n"
            "and run this script again. Correct it there rather than in the manual\n"
            "matches, because that file is the one every later step reads:"
        )
        for slug in summary.not_a_film_slugs:
            print(f"  {slug}")

    if summary.no_tmdb_record_slugs:
        print("")
        print(
            "Letterboxd publishes no TMDB record for these films, so there is nothing\n"
            "to download and no request was made. That is settled and needs nothing.\n"
            "To attach a film to one anyway, add it to\n"
            f"{MANUAL_MATCHES_FILE} as \"slug\": tmdb_id\n"
            "and run this script again:"
        )
        for slug in summary.no_tmdb_record_slugs:
            print(f"  {slug}")


def main() -> None:
    history = load_history()
    slugs = film_slugs(history)

    try:
        summary = enrich(slugs)
    except (CredentialRejected, TmdbUnavailable, TmdbDeniesEveryId) as error:
        # Each of these means the rest of the run could only fail or record
        # something untrue, so the run stops and the exit code tells the weekly
        # workflow to try again later.
        print(error, file=sys.stderr)
        raise SystemExit(1)

    report(summary)


if __name__ == "__main__":
    main()
