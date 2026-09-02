"""Cache the two facts a film payload cannot hold, so two modules stop being empty.

Run this after scripts/enrich_tmdb.py and before scripts/build_stats.py.

Everything TMDB says about one film is already cached by scripts/enrich_tmdb.py,
and two modules of the panel need something that is not about one film:

    collections            "The Godfather Collection, seen 2 of 3". A film
                           payload names the collection it belongs to. It never
                           says how many films that collection holds, so the
                           denominator has to be asked for once per collection
                           at /collection/{id}.

    director_completeness  "Kurosawa, seen 12 of 30". The cached credits list
                           the crew of one film. A director's own body of work
                           is a fact about the person, not about any of their
                           films, and it lives at /person/{id}/movie_credits.

Without these two requests neither module has an honest denominator, so
build_stats.py emits an empty array for both. Nothing scripts/enrich_tmdb.py can
download changes that, because both facts sit one level above the film.

What this run asks for
----------------------

Only what the member's own history needs, and only once:

    every collection any watched film belongs to        about 206 requests
    every director with two or more watched films       about 164 requests

A director seen once is left out on purpose. One out of thirty and one out of
one both read as "seen once", so completeness against a single film says nothing
and the request would be spent for nothing.

What this run cannot see
------------------------

This script learns what to ask for by reading cached film payloads and cached
credits. A film with neither names no collection here and credits no director
here, so its collection is never sized and its director is never counted. Every
such film is counted at the start of the run, with the step that fixes it, so a
short pass says it is short rather than reporting that the history needed
nothing more.

Which film a slug is comes from data/tmdb-ids.json, read from each film's own
Letterboxd page. That file also says which films have no TMDB film record at
all, and those refusals are honoured: taking a title and year search answer for
them is what put three collections in the panel that the member has never seen a
film from.

Why the cache is permanent
--------------------------

A collection's size and a director's filmography change rarely, in the way a
film's runtime does. Asking again every Monday would spend 370 requests a week
to be told the same numbers, so a record already in the cache is never fetched
again. To refresh one on purpose, delete its row and run this script again:

    sqlite3 data/cache/tmdb.sqlite "DELETE FROM collections WHERE tmdb_id = 230"

What is written down, and what is not
-------------------------------------

Only an answer from TMDB is cached. A request that got no answer records
nothing, so the next run asks again. This matters more here than anywhere else
in the pipeline: these rows are never refreshed, so a failure written down as a
settled answer would be believed forever.

A 404 is an answer, but it is not cached either. These ids come from TMDB's own
film payloads, so a 404 means something changed at TMDB rather than that the id
was ever a guess, and one wasted request a week is cheaper than freezing a wrong
negative into a table nothing ever revisits.

One bad record costs that record. A row that will not store, a body that is not
the shape TMDB documents, or an id TMDB no longer holds all leave the rest of
the run untouched, and every one of them is named at the end.

Exit codes
----------

    0  the run finished. Anything it could not fetch is named in the report and
       is left out of the panel rather than guessed at, and running this script
       again is what fills it.
    1  the run could not start or could not continue: no credential, no TMDB
       cache to read, a credential TMDB rejects, or TMDB no longer answering.
       Nothing untrue was written in any of those cases.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

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
    REQUEST_DELAY,
    REQUEST_TIMEOUT,
    ROOT,
    TMDB_CACHE_FILE,
    USER_AGENT,
)

TMDB_API_ROOT = "https://api.themoviedb.org/3"
TMDB_KEY_PAGE = "https://www.themoviedb.org/settings/api"

# Which TMDB record each film is, read from that film's own Letterboxd page by
# scripts/resolve_tmdb_ids.py. It is the authority on a film's id, and on which
# films have no TMDB film record at all. Optional: without it the ids come from
# the history and from the cache, which is where they came from before.
TMDB_IDS_FILE = DATA / "tmdb-ids.json"

# A quarter second between requests keeps a full first run of roughly 370
# records under two minutes and stays well inside TMDB's limits.
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

# When to decide TMDB is down rather than one record being awkward.
#
# Records fail independently: a collection id and a person id have nothing in
# common except the service being asked. So a run of ten unanswered requests is
# the service, and every remaining record would only spend its retries to reach
# the same place. Ten costs about forty seconds to establish, which is nothing
# against a run of several hundred.
GIVE_UP_AFTER_UNANSWERED_REQUESTS = 10

# A director seen once tells you nothing about how much of their work the member
# has seen, because one out of thirty and one out of one both read as "seen
# once". scripts/build_stats.py applies the same floor when it builds the
# module, so the table and the panel agree on who belongs in it.
MINIMUM_FILMS_FOR_COMPLETENESS = 2

# The two tables this script owns. Both follow the shape the three tables in
# DATA_CONTRACT.md already use: the TMDB id of the thing the row is about, the
# raw JSON answer, and when it was fetched. Keeping the answer raw means a new
# stat never needs any of this downloaded again.
#
# `tmdb_id` is the id of whatever the table holds, so it is a collection id in
# one and a person id in the other. Those are separate TMDB namespaces and the
# tables never join on each other.
#
# These names are the only source of table and column names in the SQL below, so
# nothing from the cache file or from TMDB is ever interpolated into a query.
CACHE_SCHEMA: dict[str, tuple[str, ...]] = {
    "collections": (
        "tmdb_id INTEGER PRIMARY KEY",
        "payload TEXT",
        "fetched_at TEXT",
    ),
    "person_credits": (
        "tmdb_id INTEGER PRIMARY KEY",
        "payload TEXT",
        "fetched_at TEXT",
    ),
}


class CredentialRejected(RuntimeError):
    """TMDB refused the credential, so no record in this run can succeed."""


class TmdbUnavailable(RuntimeError):
    """TMDB stopped answering, so the run gave up instead of asking for every record."""


class NoSuchRecord:
    """TMDB's answer that the thing asked for does not exist.

    This is an answer, so it is kept apart from None, which means no answer at
    all. Confusing the two is how a network failure becomes a permanent fact.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        # Names itself in any debug print, rather than showing an object address.
        return "NO_SUCH_RECORD"


NO_SUCH_RECORD = NoSuchRecord()


@dataclass
class OutageDetector:
    """Counts requests in a row that got no answer, and stops the run once TMDB is down.

    One record can fail on its own. A long run of them cannot, because they have
    nothing in common except the service, so a run that long means no further
    record will do any better.
    """

    unanswered_in_a_row: int = 0

    def note_answer(self) -> None:
        """Record that TMDB answered, whatever the answer said."""
        self.unanswered_in_a_row = 0

    def note_no_answer(self) -> None:
        """Record a request that got no usable answer, and give up once TMDB is down.

        Raises TmdbUnavailable once the run of unanswered requests is long enough
        to be the service rather than one awkward record.
        """
        self.unanswered_in_a_row += 1
        if self.unanswered_in_a_row < GIVE_UP_AFTER_UNANSWERED_REQUESTS:
            return

        raise TmdbUnavailable(
            f"TMDB gave no usable answer for the last {self.unanswered_in_a_row}"
            " requests in a row, so this run stopped instead of spending the same"
            " retries on every record left.\n"
            "TMDB looks unavailable. Nothing was recorded for the records that got"
            " no answer, so nothing untrue was written, and everything fetched"
            " before this point is still cached. Run this script again later."
        )


@dataclass
class Job:
    """One kind of record this script caches, and where to ask TMDB for it.

    `path` carries a single `{id}` placeholder. `what` and `plural` name the
    record in every message the reader sees, so one wording covers both passes.
    Both are spelled out because "director filmography" does not take an s.
    """

    table: str
    what: str
    plural: str
    path: str


COLLECTION_JOB = Job(
    table="collections",
    what="collection",
    plural="collections",
    path="/collection/{id}",
)

DIRECTOR_JOB = Job(
    table="person_credits",
    what="director filmography",
    plural="director filmographies",
    path="/person/{id}/movie_credits",
)


@dataclass
class JobResult:
    """What one pass did, in the terms its report is written in.

    Every list holds a human-readable label such as "Akira Kurosawa (5026)", so
    the report names records rather than bare ids.
    """

    job: Job
    wanted: int = 0
    already_cached: int = 0
    fetched: int = 0
    absent: list[str] = field(default_factory=list)
    unanswered: list[str] = field(default_factory=list)
    not_stored: list[str] = field(default_factory=list)
    answered_with_nothing_usable: list[str] = field(default_factory=list)

    @property
    def missing_from_the_panel(self) -> int:
        """How many wanted records the panel still has no usable answer for."""
        return (
            len(self.absent)
            + len(self.unanswered)
            + len(self.not_stored)
            + len(self.answered_with_nothing_usable)
        )


# ---------------------------------------------------------------------------
# Talking to TMDB
# ---------------------------------------------------------------------------


def load_credential() -> str:
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
    fail on every remaining record too.

    There are three kinds of return value, and they must not be confused:

    - a JSON object, which is what TMDB holds at that path.
    - NO_SUCH_RECORD, from an HTTP 404. That is TMDB answering that nothing is
      there. It is a real answer, so it is reported rather than retried.
    - None, meaning no usable answer. It never means "TMDB has no such record",
      so no caller may write it down as one.

    Every request that ends in None also feeds `outage`, which stops the whole
    run once enough of them arrive in a row to mean TMDB itself is down.

    The `description` names the record in any message the reader sees.
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

        try:
            payload = response.json()
        except ValueError as error:
            # A 200 whose body is not JSON is not an answer about this record, so
            # it must not be cached. Asking again next week is the whole cost.
            print(
                f"  {description}: TMDB answered with something that is not JSON"
                f" ({error}), retried on the next run"
            )
            outage.note_no_answer()
            return None

        if not isinstance(payload, dict):
            print(
                f"  {description}: TMDB answered with a {type(payload).__name__}"
                " where an object was expected, retried on the next run"
            )
            outage.note_no_answer()
            return None

        outage.note_answer()
        return payload

    print(
        f"  {description}: still rate limited after"
        f" {MAX_ATTEMPTS_PER_REQUEST} attempts, retried on the next run"
    )
    outage.note_no_answer()
    return None


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------


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
    keeps everything already fetched into it, instead of failing on the first
    query against a table that does not have the column yet.
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
                "cannot be added to an existing table. Drop that one table and run this\n"
                "script again, which refetches only what that table held:\n"
                f'  sqlite3 {TMDB_CACHE_FILE} "DROP TABLE {table}"',
                file=sys.stderr,
            )
            raise SystemExit(1)

        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column}")


def open_cache() -> sqlite3.Connection:
    """Open data/cache/tmdb.sqlite for writing, and add the two tables this script owns.

    A missing cache file is refused rather than created. Creating one would make
    an empty database that hides the fact that scripts/enrich_tmdb.py never ran,
    and this script has nothing to work from without the film payloads it writes.
    """
    if not TMDB_CACHE_FILE.exists():
        print(
            f"No TMDB cache at {TMDB_CACHE_FILE}.\n"
            "This script reads the cached film payloads to learn which collections and\n"
            "which directors the member's history needs, so there is nothing for it to\n"
            "do yet. Run scripts/enrich_tmdb.py first, then run this script again.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    try:
        connection = sqlite3.connect(TMDB_CACHE_FILE)
        connection.row_factory = sqlite3.Row

        for table, columns in CACHE_SCHEMA.items():
            ensure_table(connection, table, columns)
        connection.commit()
    except (sqlite3.Error, OSError) as error:
        # Nothing has been fetched yet, so stopping here costs no request. Going
        # on would spend several hundred of them on a database that cannot store
        # a single answer.
        print(
            f"The TMDB cache at {TMDB_CACHE_FILE} could not be opened for writing"
            f" ({error}).\n"
            "Check that the file and its directory are writable and have room. If the\n"
            "file is damaged, delete it and run scripts/enrich_tmdb.py to download the\n"
            "film payloads again, then run this script.\n"
            "Nothing was fetched and nothing was written.",
            file=sys.stderr,
        )
        raise SystemExit(1) from error

    return connection


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    """Report whether one table is present in the cache."""
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def read_cached_ids(connection: sqlite3.Connection, table: str) -> set[int]:
    """Read the ids one of this script's tables already holds an answer for.

    A table that will not list is reported and treated as empty. That costs
    requests, because everything in it is asked for again, and it costs nothing
    true: each answer is written back with INSERT OR REPLACE. The opposite
    reading, treating an unreadable table as complete, would skip every record
    and leave the panel short with nothing said.
    """
    try:
        rows = connection.execute(f"SELECT tmdb_id FROM {table}").fetchall()
    except sqlite3.DatabaseError as error:
        print(
            f"The cache table {table} could not be listed ({error}), so this run"
            " asks TMDB for every record again and writes each answer back over"
            " whatever is there."
        )
        return set()

    return {row["tmdb_id"] for row in rows if isinstance(row["tmdb_id"], int)}


def store_payload(
    connection: sqlite3.Connection, table: str, tmdb_id: int, payload: dict[str, Any]
) -> bool:
    """Write one answer to the cache, and report whether it landed.

    The write commits per record, so an interrupted run keeps everything it got
    and the next run asks only for the rest.

    A record that will not store costs that record. Returning False rather than
    raising is what keeps one unwritable row from ending a run of hundreds.
    """
    try:
        connection.execute(
            f"INSERT OR REPLACE INTO {table} (tmdb_id, payload, fetched_at)"
            " VALUES (?, ?, ?)",
            (tmdb_id, json.dumps(payload), datetime.now(timezone.utc).isoformat()),
        )
        connection.commit()
    except (sqlite3.Error, TypeError, ValueError) as error:
        print(f"  could not cache {table} row {tmdb_id} ({error})")
        return False

    return True


def decode_payload(raw: Any) -> dict[str, Any] | None:
    """Turn one stored payload into a dictionary, or None if it is unusable."""
    if not isinstance(raw, str):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def read_film_payloads(connection: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    """Read the cached film details, keyed by TMDB id, skipping any row that will not read.

    A row is lost two ways, and each costs one film rather than the run: the
    stored text may not parse, which a half written cache produces, or SQLite may
    refuse to produce the row at all, which a damaged page produces. Reading one
    row at a time is what makes the second one survivable, because a single query
    over the whole table cannot be resumed once it has raised.
    """
    return read_payload_table(connection, "films")


def read_credit_payloads(connection: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    """Read the cached credits, keyed by TMDB film id, skipping any row that will not read."""
    return read_payload_table(connection, "credits")


def read_payload_table(
    connection: sqlite3.Connection, table: str
) -> dict[int, dict[str, Any]]:
    """Read one payload table written by scripts/enrich_tmdb.py, one row at a time."""
    payloads: dict[int, dict[str, Any]] = {}

    if not table_exists(connection, table):
        return payloads

    try:
        tmdb_ids = [row[0] for row in connection.execute(f"SELECT tmdb_id FROM {table}")]
    except sqlite3.DatabaseError as error:
        print(
            f"The TMDB cache table {table} could not be listed ({error}), so nothing"
            f" was read from it. Delete {TMDB_CACHE_FILE.name} and run"
            " scripts/enrich_tmdb.py to build it again, then run this script."
        )
        return payloads

    for tmdb_id in tmdb_ids:
        try:
            row = connection.execute(
                f"SELECT payload FROM {table} WHERE tmdb_id = ?", (tmdb_id,)
            ).fetchone()
        except sqlite3.DatabaseError:
            # One record's stored bytes are unreadable. Every other one still is.
            continue

        payload = decode_payload(row["payload"]) if row is not None else None
        if payload is not None and isinstance(tmdb_id, int):
            payloads[tmdb_id] = payload

    return payloads


# ---------------------------------------------------------------------------
# Working out what the history needs
# ---------------------------------------------------------------------------


def load_history() -> dict[str, Any]:
    """Read data/history.json, or stop naming the step that creates it."""
    if not HISTORY_FILE.exists():
        print(
            f"No watch history at {HISTORY_FILE}.\n"
            "Run scripts/backfill.py once to create it, then scripts/enrich_tmdb.py,\n"
            "then this script.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    try:
        history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(
            f"Could not read {HISTORY_FILE}: {error}\n"
            "Restore it from git or run scripts/backfill.py again.",
            file=sys.stderr,
        )
        raise SystemExit(1) from error

    if not isinstance(history, dict):
        print(
            f"{HISTORY_FILE} does not hold an object, so it is not a history file.\n"
            "Restore it from git or run scripts/backfill.py again.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    return history


def tmdb_id_per_history_slug(
    history: dict[str, Any], connection: sqlite3.Connection
) -> dict[str, int]:
    """Say which TMDB film each slug in the member's history is.

    Three sources answer, and the strongest one holding an answer wins:

        data/tmdb-ids.json  read from each film's own Letterboxd page, which
                            states the TMDB record Letterboxd itself uses
        the history entry   the id the RSS feed carried
        the lookups table   what a title and year search resolved

    The map is the authority, and its refusals bind the two sources under it. A
    slug it names no film id for has no TMDB film at all, usually because
    Letterboxd files it as television, so an id from a weaker source does not
    fill a gap: it puts another film's collection and another film's director on
    this one. On this account that is eleven television records, and taking the
    search answer for them invented three collections the member has never seen
    a film from.

    Restricting to slugs the history still holds is the other half. The cache can
    outlive an entry the member deleted, and this run should not spend requests
    on a film that is no longer in the library.
    """
    slugs = {
        entry["slug"]
        for entry in history.get("entries", [])
        if isinstance(entry, dict) and isinstance(entry.get("slug"), str)
    }

    stated, refused = read_resolved_tmdb_ids()

    slug_to_id: dict[str, int] = {
        slug: tmdb_id for slug, tmdb_id in stated.items() if slug in slugs
    }

    for entry in history.get("entries", []):
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        tmdb_id = entry.get("tmdb_id")
        if isinstance(slug, str) and isinstance(tmdb_id, int) and slug not in refused:
            slug_to_id.setdefault(slug, tmdb_id)

    if not table_exists(connection, "lookups"):
        return slug_to_id

    try:
        rows = connection.execute("SELECT slug, tmdb_id FROM lookups").fetchall()
    except sqlite3.DatabaseError as error:
        print(
            f"The cache table lookups could not be read ({error}), so a film named"
            " by it alone has no id this run and its collection and its director"
            " are not asked for. Delete"
            f" {TMDB_CACHE_FILE.name} and run scripts/enrich_tmdb.py to build it"
            " again."
        )
        return slug_to_id

    for row in rows:
        slug = row["slug"]
        tmdb_id = row["tmdb_id"]
        if not isinstance(slug, str) or slug not in slugs or slug in refused:
            continue
        if isinstance(tmdb_id, int):
            slug_to_id.setdefault(slug, tmdb_id)

    return slug_to_id


def read_resolved_tmdb_ids() -> tuple[dict[str, int], set[str]]:
    """Read data/tmdb-ids.json: the film ids it states, and the slugs it refuses.

    Returns the ids and the refusals separately, because a refusal is an answer.
    A slug the file lists with no id, or with a type other than "movie", has no
    film record in TMDB, and no weaker source may name one for it.

    The file is optional. Without it both answers are empty and the ids come from
    the history and the cache, which is where they came from before this file
    existed, so a missing or damaged copy narrows the answer rather than ending
    the run.
    """
    if not TMDB_IDS_FILE.exists():
        print(
            f"No resolved TMDB ids at {TMDB_IDS_FILE}. Run"
            " scripts/resolve_tmdb_ids.py to read each film's id from its own"
            " Letterboxd page. Until then ids come from the history and the cache,"
            " which are search answers and can name the wrong film."
        )
        return {}, set()

    try:
        resolved = json.loads(TMDB_IDS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(
            f"Could not read {TMDB_IDS_FILE} ({error}), so film ids come from the"
            " history and the cache instead. Run scripts/resolve_tmdb_ids.py to"
            " build it again."
        )
        return {}, set()

    if not isinstance(resolved, dict):
        print(
            f"{TMDB_IDS_FILE} does not hold an object keyed by slug, so it says"
            " nothing about any film. Run scripts/resolve_tmdb_ids.py to build it"
            " again."
        )
        return {}, set()

    ids: dict[str, int] = {}
    refused: set[str] = set()

    for slug, record in resolved.items():
        if not isinstance(slug, str) or not isinstance(record, dict):
            continue
        tmdb_id = record.get("tmdb_id")
        # Only a film counts. A record Letterboxd files as television has nothing
        # behind TMDB's movie endpoint, and no film payload was ever cached for it.
        if isinstance(tmdb_id, int) and record.get("tmdb_type") == "movie":
            ids[slug] = tmdb_id
        else:
            refused.add(slug)

    return ids, refused


def collections_to_fetch(
    film_payloads: dict[int, dict[str, Any]], film_ids: set[int]
) -> dict[int, str]:
    """Name every TMDB collection the member's watched films belong to.

    The film payload already says which collection a film belongs to. It is the
    collection's size that is missing, which is the whole reason for this pass.
    """
    collections: dict[int, str] = {}

    for tmdb_id in film_ids:
        payload = film_payloads.get(tmdb_id)
        if payload is None:
            continue
        collection = payload.get("belongs_to_collection")
        if not isinstance(collection, dict):
            continue
        collection_id = collection.get("id")
        if not isinstance(collection_id, int):
            continue
        name = collection.get("name")
        collections.setdefault(collection_id, name if isinstance(name, str) else "")

    return collections


def directors_to_fetch(
    credit_payloads: dict[int, dict[str, Any]], film_ids: set[int]
) -> dict[int, tuple[str, int]]:
    """Name every director with enough watched films for completeness to mean anything.

    Returns each person's name and how many of the member's films they directed.
    Directors under MINIMUM_FILMS_FOR_COMPLETENESS are left out: their
    filmography would be downloaded to answer a question nobody can read.

    Films are counted by TMDB id here rather than by slug, because that is the
    only identity the cached credits carry. The two agree in every case that
    matters: a director sitting on either side of the threshold has films to
    spare, and one extra film in the count only ever adds a director to the pass.
    """
    films_per_director: dict[int, set[int]] = {}
    names: dict[int, str] = {}

    for tmdb_id in film_ids:
        payload = credit_payloads.get(tmdb_id)
        if payload is None:
            continue
        for member in payload.get("crew", []) or []:
            if not isinstance(member, dict) or member.get("job") != "Director":
                continue
            person_id = member.get("id")
            if not isinstance(person_id, int):
                continue
            films_per_director.setdefault(person_id, set()).add(tmdb_id)
            name = member.get("name")
            names.setdefault(person_id, name if isinstance(name, str) else "")

    return {
        person_id: (names.get(person_id, ""), len(films))
        for person_id, films in films_per_director.items()
        if len(films) >= MINIMUM_FILMS_FOR_COMPLETENESS
    }


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def collection_is_usable(payload: dict[str, Any]) -> bool:
    """Report whether a collection answer states a size the panel can use."""
    parts = payload.get("parts")
    return isinstance(parts, list) and len(parts) > 0


def filmography_is_usable(payload: dict[str, Any]) -> bool:
    """Report whether a person answer states a directed filmography the panel can use."""
    directed = {
        credit.get("id")
        for credit in payload.get("crew", []) or []
        if isinstance(credit, dict) and credit.get("job") == "Director"
    }
    directed.discard(None)
    return bool(directed)


def run_job(
    job: Job,
    wanted: dict[int, str],
    connection: sqlite3.Connection,
    client: httpx.Client,
    auth_params: dict[str, str],
    outage: OutageDetector,
    is_usable: Callable[[dict[str, Any]], bool],
) -> JobResult:
    """Fetch and cache every record one pass wants, skipping what is already cached.

    `wanted` maps each TMDB id to the label the report should use for it.
    `is_usable` says whether an answer states the number the panel needs, so an
    answer that states nothing usable is cached once and named rather than
    fetched again every week.

    Nothing in this loop can end the run except a rejected credential and TMDB
    going away, both of which would fail every remaining record too.
    """
    result = JobResult(job=job, wanted=len(wanted))
    cached = read_cached_ids(connection, job.table)

    for tmdb_id in sorted(wanted):
        label = f"{wanted[tmdb_id]} ({tmdb_id})" if wanted[tmdb_id] else str(tmdb_id)

        if tmdb_id in cached:
            result.already_cached += 1
            continue

        payload = request_json(
            client,
            job.path.format(id=tmdb_id),
            auth_params,
            f"{job.what} {label}",
            outage,
        )

        if payload is NO_SUCH_RECORD:
            # An answer, but not one worth writing down. These ids come from
            # TMDB's own film payloads, so this says TMDB changed rather than
            # that the id was a guess, and caching it would freeze that forever.
            result.absent.append(label)
            continue

        if not isinstance(payload, dict):
            # The only value left is None: no answer at all. Recording nothing is
            # what lets the next run ask again.
            result.unanswered.append(label)
            continue

        if not store_payload(connection, job.table, tmdb_id, payload):
            result.not_stored.append(label)
            continue

        result.fetched += 1

        if not is_usable(payload):
            result.answered_with_nothing_usable.append(label)
            print(f"  fetched {job.what} {label}, which states no usable number")
            continue

        print(f"  fetched {job.what} {label}")

    return result


def fetch_everything(
    connection: sqlite3.Connection,
    collections: dict[int, str],
    directors: dict[int, str],
) -> list[JobResult]:
    """Run both passes against one TMDB session, sharing one outage detector.

    Sharing the detector is deliberate: a TMDB that has stopped answering
    collections has stopped answering people too, and asking anyway would only
    spend the same retries again.
    """
    headers, auth_params = build_authentication(load_credential())
    outage = OutageDetector()
    results: list[JobResult] = []

    with httpx.Client(
        base_url=TMDB_API_ROOT,
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
        headers=headers,
    ) as client:
        results.append(
            run_job(
                COLLECTION_JOB,
                collections,
                connection,
                client,
                auth_params,
                outage,
                collection_is_usable,
            )
        )
        results.append(
            run_job(
                DIRECTOR_JOB,
                directors,
                connection,
                client,
                auth_params,
                outage,
                filmography_is_usable,
            )
        )

    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_labels(heading: str, labels: Iterable[str]) -> None:
    """Print one group of records the reader may need to act on."""
    labels = list(labels)
    if not labels:
        return

    print("")
    print(heading)
    for label in labels:
        print(f"  {label}")


def report(results: list[JobResult]) -> None:
    """Print what each pass did, and what the reader can do about what it could not."""
    for result in results:
        job = result.job
        print("")
        print(f"{job.plural} the history needs: {result.wanted}")
        print(f"  already cached:         {result.already_cached}")
        print(f"  fetched now:            {result.fetched}")
        print(f"  not on TMDB:            {len(result.absent)}")
        print(f"  TMDB never answered:    {len(result.unanswered)}")
        print(f"  would not cache:        {len(result.not_stored)}")
        # A subset of "fetched now": the answer arrived and was cached, it just
        # states no number the panel can use. Counting it again below the others
        # would make the column add up to more than the pass asked for.
        print(
            "  of those, no usable number:"
            f" {len(result.answered_with_nothing_usable)}"
        )

        print_labels(
            f"TMDB no longer holds these {job.plural}. Nothing was cached, so this\n"
            "run asks again next time. Each one is left out of the panel rather than\n"
            "given a made up total:",
            result.absent,
        )
        print_labels(
            f"TMDB never answered for these {job.plural}, so nothing was written\n"
            "down and they are left out of the panel. Run this script again to retry\n"
            "them:",
            result.unanswered,
        )
        print_labels(
            f"These {job.plural} arrived but would not write to"
            f" {TMDB_CACHE_FILE.name}.\n"
            "Check that the file is writable and has room, then run this script again:",
            result.not_stored,
        )
        print_labels(
            f"TMDB answered for these {job.plural} but stated no number the panel\n"
            "can use: a collection that lists no films, or a person TMDB credits as\n"
            "director on no film. The answer is cached so it is not asked for weekly,\n"
            "and the record stays out of the panel. To ask again, delete its row:\n"
            f'  sqlite3 {TMDB_CACHE_FILE} "DELETE FROM {job.table} WHERE tmdb_id = <id>"',
            result.answered_with_nothing_usable,
        )

    missing = sum(result.missing_from_the_panel for result in results)
    print("")
    if missing:
        print(
            f"{missing} records have no usable answer cached, so they are left out of\n"
            "the panel instead of being guessed at. Run scripts/build_stats.py to\n"
            "rebuild docs/data/stats.json with everything that did arrive."
        )
    else:
        print(
            "Every collection and every director filmography this run asked for is now\n"
            "cached. Run scripts/build_stats.py to rebuild docs/data/stats.json.\n"
            "If any film above was named as having no cached details, its collection and\n"
            "its director were never asked for, so run scripts/enrich_tmdb.py and then\n"
            "this script again before reading the two modules as complete."
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def report_films_this_pass_cannot_see(
    tmdb_id_per_slug: dict[str, int],
    history_slug_count: int,
    film_payloads: dict[int, dict[str, Any]],
    credit_payloads: dict[int, dict[str, Any]],
) -> None:
    """Say which films this pass could not read, and what that leaves out.

    This pass learns what to ask for by reading cached film payloads and cached
    credits. A film with neither names no collection and no director here, so its
    collection is never sized and its director is never counted, and the panel is
    short without a word being said about it. Saying it is the difference between
    a panel the reader can trust and a panel that looks complete.
    """
    wanted_ids = set(tmdb_id_per_slug.values())
    without_an_id = history_slug_count - len(tmdb_id_per_slug)
    without_a_payload = len(wanted_ids - set(film_payloads))
    without_credits = len(wanted_ids - set(credit_payloads))

    if without_an_id:
        print(
            f"{without_an_id} films in the history have no TMDB film record, so they"
            " belong to no\ncollection here and credit no director here. That is what"
            " data/tmdb-ids.json\nstates for them, read from their own Letterboxd"
            " pages, and it is usually a\ntelevision record. Nothing to run: TMDB has"
            " no film to ask about."
        )

    if without_a_payload or without_credits:
        print(
            f"{without_a_payload} films have a TMDB id but no cached film details, and"
            f" {without_credits} have no\ncached credits. A collection only they belong"
            " to is not sized in this run, and a\ndirector only they credit is not"
            " counted.\nRun scripts/enrich_tmdb.py, then run this script again. If it"
            " reports those films\nas ids TMDB does not hold, nothing here can fill"
            " them: they stay out of both\nmodules, which is short by whatever they"
            " alone would have added rather than\nwrong."
        )


def main() -> None:
    """Work out what the history needs, fetch what is missing, and report the run."""
    history = load_history()
    connection = open_cache()

    try:
        film_payloads = read_film_payloads(connection)

        # Nothing below can be worked out without these, so the run stops here
        # rather than reporting that it needs no collections and no directors.
        if not film_payloads:
            print(
                "The TMDB cache holds no film payloads, so there is nothing to work\n"
                "out which collections and which directors the history needs. Run\n"
                "scripts/enrich_tmdb.py first, then run this script again.\n"
                "Nothing was fetched and nothing was written.",
                file=sys.stderr,
            )
            raise SystemExit(1)

        credit_payloads = read_credit_payloads(connection)
        tmdb_id_per_slug = tmdb_id_per_history_slug(history, connection)
        film_ids = set(tmdb_id_per_slug.values())

        history_slug_count = len(
            {
                entry["slug"]
                for entry in history.get("entries", [])
                if isinstance(entry, dict) and isinstance(entry.get("slug"), str)
            }
        )

        collections = collections_to_fetch(film_payloads, film_ids)
        directors_with_counts = directors_to_fetch(credit_payloads, film_ids)

        print(
            f"{history_slug_count} films in the history,"
            f" {len(film_ids)} of them with a TMDB id,"
            f" {len(film_ids & set(film_payloads))} of those cached."
        )
        report_films_this_pass_cannot_see(
            tmdb_id_per_slug, history_slug_count, film_payloads, credit_payloads
        )
        print(
            f"{len(collections)} collections to size, and"
            f" {len(directors_with_counts)} directors with at least"
            f" {MINIMUM_FILMS_FOR_COMPLETENESS} films."
        )

        directors = {
            person_id: name for person_id, (name, _) in directors_with_counts.items()
        }

        try:
            results = fetch_everything(connection, collections, directors)
        except (CredentialRejected, TmdbUnavailable) as error:
            # Both mean the rest of the run could only fail, so it stops and the
            # exit code tells the caller to try again. Everything already fetched
            # is committed, and nothing untrue was written.
            print(error, file=sys.stderr)
            raise SystemExit(1) from error
    finally:
        connection.close()

    report(results)


if __name__ == "__main__":
    main()
