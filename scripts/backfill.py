"""Turn a Letterboxd data export into data/history.json.

Run this once, on your own machine, with the ZIP that Letterboxd sends you from
Settings, Data, Export your data. The public RSS feed is a rolling window of
about fifty diary entries, so the export is the only way to reach the older
ones. After this file exists, the weekly RSS run keeps it current.

What the parser assumes, and what it refuses to assume:

- Every CSV inside the export is optional. A missing file reduces what is read,
  it never stops the run.
- Column names are matched after lowercasing and trimming whitespace, against a
  list of known aliases. Letterboxd has changed these headers before, and the
  same idea appears under different names in different files: the watch date is
  "Watched Date" in diary.csv and "Date" in older layouts.
- A column that is needed and not found stops the run with an error naming the
  file, listing the headers that were found, and saying what was looked for.
  Guessing a column would quietly corrupt the history.

Merge order, richest source first:

1. diary.csv gives one entry per film and watch date, with rating and rewatch.
2. reviews.csv adds review text to those entries, and creates any the diary
   file did not cover.
3. ratings.csv gives an undated entry for a film with no diary entry.
4. watched.csv gives an undated entry for a film found nowhere else.
5. watchlist.csv becomes the watchlist array, never an entry.
6. likes/films.csv, when present, sets the liked flag on entries already built.

Where a film's slug comes from:

Every Letterboxd URI in a real export is a boxd.it short link, and a short link
carries an opaque id rather than the slug. The whole pipeline joins on slug, so
the export on its own cannot say which film a row is about. data/short-links.json
is the answer: scripts/resolve_short_links.py follows each short link once and
records the film slug it lands on. This script reads that map and uses it as the
first source of a slug, ahead of anything built from a title.

Without the map, every film in the export gets an invented slug. None of them
match the RSS feed or the curated lists, so every film appears twice and list
progress collapses to near zero. That is why the run reports how many slugs it
had to invent, and why that number should be zero.

A row the map does not cover keeps the older behaviour, and this is where the two
keys the data contract does not list come from. The row gets a slug built from
its title and year, flagged with slug_provisional, and its original link is kept
in letterboxd_uri so a later run can resolve it. A title that yields no letters
or digits falls back to the boxd.it id in the row's own link, because two
different films under one slug merge into one entry and one of the two films
disappears.

Which files are read, and which are refused:

Files are recognised by where they sit inside the export, never by file name
alone. The member's own diary.csv, reviews.csv, ratings.csv, watched.csv and
watchlist.csv are read only at the export root, and films.csv only at the exact
path likes/films.csv. Every other file is refused and named in the run summary.

That rule is what keeps three sets of lookalikes out of the history. A real
export holds deleted/ and orphaned/ folders whose diary.csv and reviews.csv have
headers identical to the member's own, and a likes/ folder holding other people's
reviews and lists. Reading any of them writes activity that was deleted, was
orphaned, or belongs to somebody else into the history as if the member had
watched it.

profile.csv is refused by name and its bytes are never read, because it holds the
member's email address and a published stats panel has no use for it.

Every row the run does not keep is counted and printed: rows that named no
film, rows that merged into an entry already built, and rows that repeated
something already read. Losing a row without saying so is the one outcome this
script treats as a failure.

Usage:
    python scripts/backfill.py path/to/letterboxd-export.zip
    python scripts/backfill.py path/to/unzipped-export-directory
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import unicodedata
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.config import DATA, HISTORY_FILE, LETTERBOXD_USER, ensure_dirs

# --------------------------------------------------------------------------
# What the export looks like
# --------------------------------------------------------------------------

# The member's own files, each at the exact path it has inside the export, and
# the source it feeds. The whole path decides this, never the bare file name,
# because an export holds several files that carry the member's headers without
# holding the member's current activity:
#
#   deleted/diary.csv     entries the member deleted
#   orphaned/diary.csv    entries whose film Letterboxd no longer has
#   likes/reviews.csv     other people's reviews that the member liked
#
# Their headers are identical to the files at the root. A parser that matched on
# the file name would write deleted, orphaned, or other people's activity into
# this history as if the member had watched it.
SOURCE_BY_EXPORT_PATH = {
    "diary.csv": "diary",
    "reviews.csv": "reviews",
    "ratings.csv": "ratings",
    "watched.csv": "watched",
    "watchlist.csv": "watchlist",
    "likes/films.csv": "likes",
}

# The bare names of those files. A refused path that ends in one of them is a
# lookalike, and the run names it separately from a file that merely went unread,
# so the reader can see the refusal was deliberate rather than accidental.
MEMBER_FILE_NAMES = frozenset(path.rsplit("/", 1)[-1] for path in SOURCE_BY_EXPORT_PATH)

# Files this parser must never read, whatever else changes. profile.csv holds the
# member's email address next to their account settings, and this pipeline
# publishes a public web page. Refusing it by name, before its bytes are read,
# means nobody can start reading it by accident later: adding an entry to
# SOURCE_BY_EXPORT_PATH is not enough to open it.
PRIVATE_FILE_NAMES = frozenset({"profile.csv"})

# Every folder the export itself gives a meaning to. None of them is the dated
# folder the export is wrapped in, so removing the shared wrapper folders stops
# at one of these. A folder missing from this set can be stripped away as if it
# were a wrapper, and its diary.csv then reads as the member's own. That is why
# "orphaned" belongs here beside "deleted".
NON_WRAPPER_FOLDERS = frozenset({"likes", "lists", "deleted", "orphaned"})

# Where scripts/resolve_short_links.py stores the boxd.it short id to film slug
# map. The path is spelled out here rather than imported from that script,
# because reading a JSON file needs no HTTP client and this script should not
# pull one in.
SHORT_LINKS_FILE = DATA / "short-links.json"

# Accepted header spellings, already normalized. Order is preference order.
FILM_URI_HEADERS = ("letterboxd uri", "letterboxd url", "film uri", "film url", "uri", "url")
TITLE_HEADERS = ("name", "title", "film name", "film title")
YEAR_HEADERS = ("year", "film year", "release year")
WATCHED_DATE_HEADERS = ("watched date", "date watched", "date")
LOGGED_DATE_HEADERS = ("date", "date added", "added")
RATING_HEADERS = ("rating", "your rating", "member rating")
REWATCH_HEADERS = ("rewatch",)
REVIEW_HEADERS = ("review", "review text")

FILM_SLUG_PATTERN = re.compile(r"/film/([^/?#]+)")
BOXD_ID_PATTERN = re.compile(r"boxd\.it/([A-Za-z0-9]+)")
ISO_DATE_PATTERN = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
TRUTHY_VALUES = {"yes", "true", "1", "y"}

# Prefixes of the slugs built from a row's own link or digest rather than from
# its title. The run reports how many of these it had to fall back on.
ROW_IDENTITY_SLUG_PREFIXES = ("boxd-", "untitled-")

# Where a row's slug came from. The run reports these per file, because a slug
# invented from a title matches nothing else in the pipeline, and the count of
# invented slugs is the quickest way to see whether the short-link map covered
# this export.
SLUG_FROM_FILM_URL = "film url"
SLUG_FROM_SHORT_LINK_MAP = "short link map"
SLUG_FROM_ANOTHER_EXPORT_FILE = "another file in the export"
SLUG_INVENTED_FROM_TITLE = "title"


class BackfillError(Exception):
    """Something about the export stops the run and needs a human decision."""


class ExportNotReadable(BackfillError):
    """The path given is not an export ZIP or an unpacked export directory."""


class ExportColumnMissing(BackfillError):
    """A CSV in the export does not carry a column the parser needs."""


@dataclass(frozen=True)
class FilmReference:
    """One film as a single export row identifies it.

    slug_source records where the slug came from, so the run can say how many
    slugs it had to invent. Only a slug invented from a title is provisional. A
    slug read from a film URL, looked up in the short-link map, or borrowed from
    another file in the same export is the film's real slug.
    """

    slug: str
    slug_source: str
    letterboxd_uri: str | None
    title: str | None
    year: int | None

    @property
    def slug_is_provisional(self) -> bool:
        """Say whether this slug was invented here instead of read from a source."""
        return self.slug_source == SLUG_INVENTED_FROM_TITLE


@dataclass(frozen=True)
class ExportContents:
    """Everything one export holds: the tables read, and the files left unread.

    The unread files are carried out of here so the run can report them. A file
    this parser does not use is normal, but the reader still gets to see that it
    was seen and passed over.

    They are split by why they went unread. refused_lookalikes holds the files
    that carry the member's headers without being the member's current activity,
    such as deleted/diary.csv. private_files holds the files that must never be
    read at all. Both are named in the run summary, because a refusal nobody can
    see is a refusal nobody can check.
    """

    tables: dict[str, SourceTable]
    ignored_files: list[str]
    duplicate_files: list[tuple[str, str]]
    refused_lookalikes: list[str]
    private_files: list[str]


@dataclass(frozen=True)
class ParsedRow:
    """One export row, with every field the merge step can use."""

    film: FilmReference
    watched_date: str | None
    logged_date: str | None
    rating: float | None
    rewatch: bool
    review: str | None


@dataclass(frozen=True)
class SourceTable:
    """One CSV read out of the export, with its headers already normalized."""

    source: str
    member_path: str
    headers: list[str]
    rows: list[dict[str, str]]
    blank_rows: int = 0

    def header_for(
        self,
        purpose: str,
        aliases: Sequence[str],
        required: bool = True,
    ) -> str | None:
        """Find the header this table uses for one purpose.

        purpose is printed back to the reader when nothing matches, so write it
        as a plain noun phrase: "the watched date", not "watched_date".
        Returns None when the column is absent and required is False.
        """
        for alias in aliases:
            if alias in self.headers:
                return alias
        if not required:
            return None
        raise ExportColumnMissing(
            f"{self.member_path} has no column for {purpose}.\n"
            f"  Headers found:    {', '.join(self.headers) or '(the file has no header row)'}\n"
            f"  Headers accepted: {', '.join(aliases)}\n"
            f"  What to do: open the CSV and check the real column name, then add it to the "
            f"header lists near the top of scripts/backfill.py, or rename the column in the file."
        )


# --------------------------------------------------------------------------
# Reading the files
# --------------------------------------------------------------------------


def normalize_header(header: str) -> str:
    """Lowercase a CSV header and collapse the whitespace around and inside it."""
    return " ".join(header.strip().lower().split())


def normalize_member_path(member_path: str) -> str:
    """Rewrite one path inside the export into a single comparable form.

    Windows separators become forward slashes, letters are lowercased, and any
    leading "./" or "/" is dropped, so the same file compares equal whether it
    arrived from a ZIP listing or from walking a directory.
    """
    normalized = member_path.replace("\\", "/").lower()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lstrip("/")


def is_archive_noise(normalized_path: str) -> bool:
    """Say whether a path is packaging leftovers rather than an export file.

    macOS adds a __MACOSX folder and dot-underscore twins to ZIP archives. They
    are excluded before anything else, because they are not part of the export
    and their paths would otherwise be mistaken for its shape.
    """
    if not normalized_path or normalized_path.endswith("/"):
        return True
    if normalized_path.startswith("__macosx/"):
        return True
    return normalized_path.rsplit("/", 1)[-1].startswith(".")


def is_export_file(member_path: str) -> bool:
    """Say whether a path could be a file this parser reads.

    Every source this parser knows is a CSV, so anything else is not export
    data. Both readers apply this one rule, which is what keeps them seeing the
    same set of files: wrapper_depth counts the leading folders that all of them
    share, so a reader that hides some files measures a different wrapper and
    then leaves every CSV one folder too deep to be recognised. One stray
    README.txt beside the wrapper folder used to be enough to do that.

    A directory holding an unusably large non-CSV file is also never read into
    memory, because a path this rejects is never opened.
    """
    return normalize_member_path(member_path).endswith(".csv")


def is_private_file(member_path: str) -> bool:
    """Say whether a file must never be read, wherever in the export it sits.

    profile.csv holds the member's email address alongside their account
    settings, and this pipeline publishes a public web page. An address that is
    never read cannot be written out by mistake, so the readers list this file
    instead of opening it, and its bytes never enter the process.

    The bare file name is matched rather than the full path, so a copy of it in
    any folder of the export is refused too.
    """
    return normalize_member_path(member_path).rsplit("/", 1)[-1] in PRIVATE_FILE_NAMES


def wrapper_depth(normalized_paths: Sequence[str]) -> int:
    """Count the leading folders every file in the export shares.

    Letterboxd wraps the CSVs in one dated folder, but the export also arrives
    unpacked, passed either as that folder or as its parent. Removing the shared
    folders first means likes/films.csv reads as "likes/films.csv" every time,
    which is what lets classify_member trust the folder it sees. A folder the
    export gives a meaning to is never removed, so a lone likes/films.csv does
    not get stripped down to films.csv.
    """
    remaining = list(normalized_paths)
    depth = 0
    while remaining and all("/" in path for path in remaining):
        first_folders = {path.split("/", 1)[0] for path in remaining}
        if len(first_folders) != 1:
            return depth
        (folder,) = first_folders
        if folder in NON_WRAPPER_FOLDERS:
            return depth
        remaining = [path.split("/", 1)[1] for path in remaining]
        depth += 1
    return depth


def strip_wrapper(normalized_path: str, depth: int) -> str:
    """Remove the shared wrapper folders from one normalized path."""
    parts = normalized_path.split("/")
    return "/".join(parts[depth:]) if len(parts) > depth else normalized_path


def classify_member(relative_path: str) -> str | None:
    """Name the source a file belongs to, or None when it is not a source.

    relative_path is the path inside the export's wrapper folder, normalized by
    normalize_member_path and shortened by strip_wrapper. The whole path has to
    match, because the folder a file sits in is what tells the member's own files
    from the lookalikes: reviews.csv at the root is the member's own writing,
    likes/reviews.csv is other people's reviews the member liked, and
    orphaned/reviews.csv is writing about films Letterboxd no longer has.

    Everything else returns None: comments.csv, profile.csv, the lists folder,
    and the whole of the deleted and orphaned folders. The run names them.
    """
    return SOURCE_BY_EXPORT_PATH.get(relative_path)


def _member_sort_key(member_path: str) -> tuple[int, str]:
    """Sort shallow paths first, so a top-level diary.csv beats a nested copy."""
    normalized = member_path.replace("\\", "/")
    return (normalized.count("/"), normalized)


def _read_zip(archive_path: Path) -> tuple[list[tuple[str, bytes]], list[str]]:
    """Read the export files out of a ZIP, shallowest path first.

    Returns the files that were read, and the paths of the files that were
    refused unread by is_private_file. The refused paths come back rather than
    disappearing, because the run has to name them and because they still count
    towards the shared wrapper folder that strip_wrapper removes.
    """
    with zipfile.ZipFile(archive_path) as archive:
        names = sorted(
            (name for name in archive.namelist() if is_export_file(name)),
            key=_member_sort_key,
        )
        opened: list[tuple[str, bytes]] = []
        refused: list[str] = []
        for name in names:
            if is_private_file(name):
                refused.append(name)
            else:
                opened.append((name, archive.read(name)))
        return opened, refused


def _read_directory(directory: Path) -> tuple[list[tuple[str, bytes]], list[str]]:
    """Read the export files out of an unpacked export, shallowest path first.

    Returns the same two lists as _read_zip, for the same reasons.
    """
    files = sorted(
        (path for path in directory.rglob("*") if path.is_file() and is_export_file(str(path))),
        key=lambda path: _member_sort_key(str(path.relative_to(directory))),
    )
    opened: list[tuple[str, bytes]] = []
    refused: list[str] = []
    for path in files:
        member_path = str(path.relative_to(directory))
        if is_private_file(str(path)):
            refused.append(member_path)
        else:
            opened.append((member_path, path.read_bytes()))
    return opened, refused


def decode_csv(raw: bytes, member_path: str) -> str:
    """Decode one CSV member as UTF-8, tolerating a byte order mark."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise BackfillError(
            f"{member_path} is not valid UTF-8 text ({error.reason} at byte {error.start}).\n"
            f"  What to do: re-download the export, or open the file and save it as UTF-8."
        ) from error


def parse_csv(source: str, member_path: str, text: str) -> SourceTable:
    """Read one CSV member into normalized headers and row dictionaries.

    Rows shorter or longer than the header row are tolerated. Letterboxd review
    text contains newlines and commas, which the csv module already handles.
    """
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        raw_headers = next(reader)
    except StopIteration:
        return SourceTable(source=source, member_path=member_path, headers=[], rows=[])

    headers = [normalize_header(header) for header in raw_headers]
    rows: list[dict[str, str]] = []
    blank_rows = 0
    for values in reader:
        if not any(value.strip() for value in values):
            blank_rows += 1
            continue
        rows.append({header: value for header, value in zip(headers, values)})

    return SourceTable(
        source=source,
        member_path=member_path,
        headers=headers,
        rows=rows,
        blank_rows=blank_rows,
    )


def read_export(export_path: Path) -> ExportContents:
    """Read every CSV the export holds, keyed by the source it belongs to.

    Accepts either the ZIP or a directory holding the unpacked files, so the
    owner can look through the CSVs before running this. Files that are not a
    source, and files that repeat a source already read, are returned alongside
    the tables rather than dropped, so the run can report them.
    """
    if not export_path.exists():
        raise ExportNotReadable(
            f"Nothing exists at {export_path}.\n"
            f"  What to do: pass the path to the export ZIP Letterboxd sent you, "
            f"or to the directory you unzipped it into."
        )

    # Pointing this script at one folder inside the export would make that
    # folder look like the export's root, and orphaned/diary.csv would then read
    # as the member's own diary. Nothing inside the file listing can tell the two
    # apart, so the folder's own name is checked here.
    #
    # The name is read from the resolved path, never from the path as it was
    # typed. Path(".").name is the empty string, so "backfill.py ." run from
    # inside orphaned/ used to walk straight past this check, and that is the
    # likeliest way to reach the failure the check exists to stop. Path("..")
    # and a trailing slash have the same problem.
    folder_name = export_path.resolve().name.lower() if export_path.is_dir() else ""
    if folder_name in NON_WRAPPER_FOLDERS:
        raise ExportNotReadable(
            f"{export_path.resolve()} is the export's {folder_name} folder, not the export "
            f"itself.\n"
            f"  Files in there are not the member's current activity: deleted and orphaned hold "
            f"entries Letterboxd removed, likes and lists hold other people's work.\n"
            f"  What to do: pass the folder that holds diary.csv and watched.csv."
        )

    if export_path.is_dir():
        members, refused_members = _read_directory(export_path)
    elif zipfile.is_zipfile(export_path):
        members, refused_members = _read_zip(export_path)
    else:
        raise ExportNotReadable(
            f"{export_path} is neither a ZIP archive nor a directory.\n"
            f"  What to do: pass the export ZIP itself, or the directory you unzipped it into."
        )

    # The wrapper folder has to go before anything is classified, because the
    # folder a file sits in is what tells the member's own files from the ones
    # holding other people's material.
    export_files: list[tuple[str, str, bytes]] = []
    for member_path, raw in members:
        normalized = normalize_member_path(member_path)
        if not is_archive_noise(normalized):
            export_files.append((member_path, normalized, raw))

    # A refused file is still one of the export's files, so it is measured with
    # the rest. Leaving it out would let its absence change the shared wrapper
    # depth, and one folder of difference is enough to leave every CSV
    # unrecognised.
    refused_paths = [
        normalized
        for normalized in (normalize_member_path(path) for path in refused_members)
        if not is_archive_noise(normalized)
    ]
    depth = wrapper_depth([normalized for _, normalized, _ in export_files] + refused_paths)
    private_files = [strip_wrapper(normalized, depth) for normalized in refused_paths]

    tables: dict[str, SourceTable] = {}
    ignored_files: list[str] = []
    refused_lookalikes: list[str] = []
    duplicate_files: list[tuple[str, str]] = []
    for member_path, normalized, raw in export_files:
        relative_path = strip_wrapper(normalized, depth)
        source = classify_member(relative_path)
        if source is None:
            # A refused file that carries a member file's name is reported on its
            # own line, because it is the one a reader would expect to have been
            # read: deleted/diary.csv and orphaned/diary.csv have exactly the
            # headers of the diary.csv beside them.
            if relative_path.rsplit("/", 1)[-1] in MEMBER_FILE_NAMES:
                refused_lookalikes.append(relative_path)
            else:
                ignored_files.append(relative_path)
            continue
        if source in tables:
            # Named as it is written in the export, because the shortened path
            # can read the same as the path of the file that was kept.
            duplicate_files.append((member_path, source))
            continue
        tables[source] = parse_csv(source, relative_path, decode_csv(raw, member_path))

    if not tables:
        seen = refused_lookalikes + ignored_files + private_files
        found = ", ".join(seen[:8]) or "(no files at all)"
        raise ExportNotReadable(
            f"{export_path} holds no file this parser recognises.\n"
            f"  Looked for: {', '.join(sorted(SOURCE_BY_EXPORT_PATH))}\n"
            f"  Found instead: {found}\n"
            f"  What to do: check that you passed the Letterboxd export and not another archive. "
            f"A file such as deleted/diary.csv is refused on purpose, so pass the folder that "
            f"holds diary.csv rather than a folder inside it. If the path holds more than one "
            f"export, pass one export folder rather than the folder containing them."
        )
    return ExportContents(
        tables=tables,
        ignored_files=ignored_files,
        duplicate_files=duplicate_files,
        refused_lookalikes=refused_lookalikes,
        private_files=private_files,
    )


def load_short_link_slugs(path: Path = SHORT_LINKS_FILE) -> dict[str, str]:
    """Read the boxd.it short id to film slug map, or an empty map if there is none.

    Every Letterboxd URI in a real export is a boxd.it short link, and a short
    link carries an opaque id rather than the slug. This map is how the export's
    films get their real slugs, which is what lets them match the RSS feed and
    the curated lists. scripts/resolve_short_links.py writes it.

    Short ids are base62 and case-sensitive, so they are read exactly as written.
    One real export holds both boxd.it/1JzG, which is Inglourious Basterds, and
    boxd.it/1jzg, which is Paris Is Burning.

    Ids stored as null are dropped. They are short links that point at a list or
    a member page rather than at a film, so they name no slug.

    A missing file is not fatal: the run still produces a history and reports
    every slug it had to invent. A file that exists but cannot be read is fatal,
    because carrying on would invent every slug in the export without the reader
    having asked for that.
    """
    if not path.exists():
        return {}

    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BackfillError(
            f"{path} exists but could not be read as JSON ({error}).\n"
            f"  Without it every film in the export gets a slug invented from its title, and "
            f"an invented slug matches nothing in the RSS feed or the curated lists.\n"
            f"  What to do: restore the file from git, or delete it and run "
            f"scripts/resolve_short_links.py against this export to rebuild it."
        ) from error

    if not isinstance(stored, dict):
        raise BackfillError(
            f"{path} is not a JSON object mapping a short id to a film slug.\n"
            f"  What to do: delete it and run scripts/resolve_short_links.py against this "
            f"export to rebuild it."
        )

    return {
        short_id: slug
        for short_id, slug in stored.items()
        if isinstance(short_id, str) and isinstance(slug, str) and slug
    }


# --------------------------------------------------------------------------
# Reading single values
# --------------------------------------------------------------------------


def cell(row: dict[str, str], header: str | None) -> str | None:
    """Read one cell by header name, treating blanks as absent."""
    if header is None:
        return None
    value = row.get(header)
    if value is None:
        return None
    value = value.strip()
    return value or None


def parse_year(raw: str | None) -> int | None:
    """Read a release year, or return None when the cell is blank or odd."""
    if raw is None:
        return None
    digits = raw.strip()
    return int(digits) if digits.isdigit() and len(digits) == 4 else None


def parse_date(raw: str | None) -> str | None:
    """Read an export date as YYYY-MM-DD, or return None when there is none.

    Only the ISO layout is accepted. A date such as 03/04/2025 is ambiguous
    between day-first and month-first, and a wrong guess would move films into
    the wrong year for the rest of the pipeline.
    """
    if raw is None:
        return None
    match = ISO_DATE_PATTERN.match(raw.strip())
    if match is None:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_rating(raw: str | None) -> float | None:
    """Read a member rating on the 0.5 to 5.0 scale, or return None."""
    if raw is None:
        return None
    try:
        rating = float(raw.strip())
    except ValueError:
        return None
    return rating if 0.0 < rating <= 5.0 else None


def parse_flag(raw: str | None) -> bool:
    """Read a yes or no cell. Letterboxd writes "Yes" and leaves no blank."""
    return raw is not None and raw.strip().lower() in TRUTHY_VALUES


def slug_from_uri(uri: str | None) -> str | None:
    """Read the film slug out of a Letterboxd URL.

    Returns None for a boxd.it short link, which carries an opaque id rather
    than the slug.
    """
    if uri is None:
        return None
    match = FILM_SLUG_PATTERN.search(uri)
    return match.group(1).strip("/") if match else None


def boxd_id(uri: str | None) -> str | None:
    """Read the short id out of a boxd.it link, or return None.

    The id is opaque, so it says nothing about which film this is. It is unique
    to the row, which is what makes it usable as a last-resort key.

    The id is base62 and case-sensitive, so its case is kept: "aB12" and "Ab12"
    are two different films, and lowercasing would merge them into one entry.
    """
    if uri is None:
        return None
    match = BOXD_ID_PATTERN.search(uri)
    return match.group(1) if match else None


def slug_words(title: str) -> str:
    """Turn a title into slug words, or return an empty string if it has none.

    A letter that has a plain ASCII form is folded to it, the way Letterboxd
    spells its own slugs, so "Amelie" is what "Amélie" becomes. A letter with no
    ASCII form is kept as itself. Dropping it would leave two films in another
    script sharing one slug, and two films under one slug merge into one entry,
    which makes one of them disappear from the history without a trace.

    The fold runs one character at a time, never over the whole title first.
    Folding the title and then keeping only its ASCII discards every character
    the fold could not replace, so "ゴジラvsビオランテ" and "モスラvsバトラ" both
    come out as "vs" and collide.

    Each character is judged in its composed form, so a kana and its voiced
    counterpart stay two characters and two slugs.
    """
    pieces: list[str] = []
    for char in unicodedata.normalize("NFKC", title):
        decomposed = unicodedata.normalize("NFKD", char)
        folded = "".join(part for part in decomposed if not unicodedata.combining(part))
        if folded.isascii() and folded.isalnum():
            pieces.append(folded.lower())
        elif char.isalnum():
            pieces.append(char.lower())
        else:
            pieces.append("-")
    return re.sub(r"-+", "-", "".join(pieces)).strip("-")


def provisional_slug(title: str | None, year: int | None, uri: str | None) -> str:
    """Build a stand-in slug for a row whose link does not carry the real slug.

    The slug reads like a Letterboxd slug whenever the title allows it, because
    a later step resolves these against the site. What matters more is that it
    is unique to the film: two films under one slug merge into one entry, and
    one of the two films then disappears from the history without a trace.

    So the title comes first, with the year appended when known, and a title
    that yields no letters or digits at all falls back to the boxd.it id in the
    row's own link, and then to a digest of the row itself.
    """
    words = slug_words(title) if title else ""
    if words:
        return f"{words}-{year}" if year is not None else words

    short_id = boxd_id(uri)
    if short_id is not None:
        return f"boxd-{short_id}"

    # Nothing in this row reads as a name and nothing links it to Letterboxd,
    # so key on the row's own text. Rows that differ at all stay apart.
    row_text = f"{title or ''}|{year or ''}|{uri or ''}"
    digest = hashlib.sha256(row_text.encode("utf-8")).hexdigest()[:10]
    return f"untitled-{digest}"


def slug_is_from_row_identity(slug: str) -> bool:
    """Say whether a slug was built from the row's link or digest, not its title."""
    return slug.startswith(ROW_IDENTITY_SLUG_PREFIXES)


def film_reference(
    row: dict[str, str],
    uri_header: str | None,
    title_header: str | None,
    year_header: str | None,
    short_link_slugs: dict[str, str],
) -> FilmReference | None:
    """Identify the film one row is about, or None when the row names no film.

    The slug is looked for in three places, best answer first:

    1. the row's own link, when it is a full film URL that spells the slug out;
    2. the short-link map, which is where a real export's slugs come from,
       because every URI a real export carries is a boxd.it short link;
    3. the title and year, which only invents a slug shaped like a Letterboxd
       one. Nothing else in the pipeline joins on it, so this is a last resort
       and the run reports how often it was needed.
    """
    uri = cell(row, uri_header)
    title = cell(row, title_header)
    year = parse_year(cell(row, year_header))

    slug = slug_from_uri(uri)
    if slug is not None:
        return FilmReference(slug, SLUG_FROM_FILM_URL, uri, title, year)

    short_id = boxd_id(uri)
    if short_id is not None:
        resolved = short_link_slugs.get(short_id)
        if resolved is not None:
            return FilmReference(resolved, SLUG_FROM_SHORT_LINK_MAP, uri, title, year)

    if title is None:
        return None
    return FilmReference(
        provisional_slug(title, year, uri), SLUG_INVENTED_FROM_TITLE, uri, title, year
    )


# --------------------------------------------------------------------------
# Reading whole tables
# --------------------------------------------------------------------------


def parse_rows(
    table: SourceTable,
    short_link_slugs: dict[str, str],
) -> tuple[list[ParsedRow], dict[str, int]]:
    """Read one source table into rows the merge step can use.

    short_link_slugs turns this file's boxd.it links into real film slugs. The
    counters returned say how many rows it answered for and how many rows were
    left with an invented slug, because an invented slug joins to nothing.

    Raises ExportColumnMissing when the table lacks a column its source needs:
    a film identity everywhere, a watched date in diary.csv, a rating in
    ratings.csv, and review text in reviews.csv.
    """
    # A file with no rows has no columns to check and no rows to lose, so it is
    # read as the empty table it is. Every CSV in the export is optional, and a
    # file that is present but empty has to count as one of the missing ones:
    # stopping the run over it would throw away every other file as well.
    if not table.rows:
        return [], {
            "rows": 0,
            "blank_rows": table.blank_rows,
            "rows_without_a_film": 0,
            "unreadable_dates": 0,
            "slugs_from_the_short_link_map": 0,
            "slugs_invented_from_a_title": 0,
        }

    uri_header = table.header_for("the film URL", FILM_URI_HEADERS, required=False)
    title_header = table.header_for("the film title", TITLE_HEADERS, required=False)
    if uri_header is None and title_header is None:
        table.header_for(
            "the film identity, which is a Letterboxd URL, or a title to fall back on",
            FILM_URI_HEADERS + TITLE_HEADERS,
        )
    year_header = table.header_for("the release year", YEAR_HEADERS, required=False)

    needs_watched_date = table.source in {"diary", "reviews"}
    watched_header = (
        table.header_for(
            "the watched date",
            WATCHED_DATE_HEADERS,
            required=table.source == "diary",
        )
        if needs_watched_date
        else None
    )
    logged_header = table.header_for("the logged date", LOGGED_DATE_HEADERS, required=False)
    rating_header = table.header_for(
        "the rating",
        RATING_HEADERS,
        required=table.source == "ratings",
    )
    rewatch_header = table.header_for("the rewatch flag", REWATCH_HEADERS, required=False)
    review_header = table.header_for(
        "the review text",
        REVIEW_HEADERS,
        required=table.source == "reviews",
    )

    parsed: list[ParsedRow] = []
    counters = {
        "rows": len(table.rows),
        "blank_rows": table.blank_rows,
        "rows_without_a_film": 0,
        "unreadable_dates": 0,
        "slugs_from_the_short_link_map": 0,
        "slugs_invented_from_a_title": 0,
    }

    for row in table.rows:
        film = film_reference(row, uri_header, title_header, year_header, short_link_slugs)
        if film is None:
            counters["rows_without_a_film"] += 1
            continue

        if film.slug_source == SLUG_FROM_SHORT_LINK_MAP:
            counters["slugs_from_the_short_link_map"] += 1
        elif film.slug_source == SLUG_INVENTED_FROM_TITLE:
            counters["slugs_invented_from_a_title"] += 1

        raw_watched = cell(row, watched_header)
        watched_date = parse_date(raw_watched)
        if raw_watched is not None and watched_date is None:
            counters["unreadable_dates"] += 1

        parsed.append(
            ParsedRow(
                film=film,
                watched_date=watched_date,
                logged_date=parse_date(cell(row, logged_header)),
                rating=parse_rating(cell(row, rating_header)),
                rewatch=parse_flag(cell(row, rewatch_header)),
                review=cell(row, review_header),
            )
        )

    return parsed, counters


def index_real_slugs(rows: Iterable[ParsedRow]) -> dict[tuple[str, int | None], str]:
    """Map title and year to the real slug, for rows that carried a film URL.

    This is the last chance for a row the short-link map did not cover. An
    export can mix link styles, and one file may spell out a slug that another
    file gives only as an unresolved short link. The index lets that row borrow
    the real slug rather than opening a second entry under an invented one.
    """
    index: dict[tuple[str, int | None], str] = {}
    for row in rows:
        film = row.film
        if film.slug_is_provisional or film.title is None:
            continue
        index.setdefault((film.title.strip().lower(), film.year), film.slug)
    return index


def resolve_slug(row: ParsedRow, index: dict[tuple[str, int | None], str]) -> ParsedRow:
    """Replace a provisional slug with the real one when the export knows it."""
    film = row.film
    if not film.slug_is_provisional or film.title is None:
        return row

    key = (film.title.strip().lower(), film.year)
    real_slug = index.get(key) or index.get((key[0], None))
    if real_slug is None:
        return row
    return replace(
        row, film=replace(film, slug=real_slug, slug_source=SLUG_FROM_ANOTHER_EXPORT_FILE)
    )


# --------------------------------------------------------------------------
# Building history entries
# --------------------------------------------------------------------------


def new_entry(film: FilmReference, watched_date: str | None, row: ParsedRow) -> dict[str, Any]:
    """Build one history entry in the shape DATA_CONTRACT.md defines.

    The whole row is passed in rather than its fields one by one, so that a
    field the row already carries cannot be left behind here. That is what
    happened to logged_date, which build_stats needs to measure logging lag and
    which only the export can supply.

    watched_date is passed separately because it is not always the row's own:
    ratings.csv and watched.csv say nothing about when a film was seen, so their
    entries are undated even when the row carries some other date.
    """
    return {
        "guid": f"export:{film.slug}:{watched_date or 'undated'}",
        "slug": film.slug,
        "title": film.title,
        "year": film.year,
        "watched_date": watched_date,
        "logged_date": row.logged_date,
        "rating": row.rating,
        "rewatch": row.rewatch,
        "liked": False,
        "tmdb_id": None,
        "review": row.review,
        "source": "export",
        "slug_provisional": film.slug_is_provisional,
        "letterboxd_uri": film.letterboxd_uri,
    }


def fill_gaps(entry: dict[str, Any], row: ParsedRow) -> None:
    """Add to an entry what a later row knows and the entry does not.

    Nothing already set is overwritten, because the sources are visited richest
    first and the first writer is the better informed one.
    """
    if entry["rating"] is None and row.rating is not None:
        entry["rating"] = row.rating
    if entry["logged_date"] is None and row.logged_date is not None:
        entry["logged_date"] = row.logged_date
    if row.rewatch:
        entry["rewatch"] = True
    if entry["review"] is None and row.review is not None:
        entry["review"] = row.review
    if entry["title"] is None and row.film.title is not None:
        entry["title"] = row.film.title
    if entry["year"] is None and row.film.year is not None:
        entry["year"] = row.film.year
    if entry["letterboxd_uri"] is None and row.film.letterboxd_uri is not None:
        entry["letterboxd_uri"] = row.film.letterboxd_uri


def apply_separate_rating(film_entries: list[dict[str, Any]], rating: float | None) -> bool:
    """Give a film's entries the rating that a ratings.csv row carries alone.

    Letterboxd lets a member log a film without a rating and rate it separately,
    so ratings.csv holds the only rating some diary entries will ever have.
    Leaving those null drops the film out of every average that reads ratings:
    the overall average, by_year, decades, and rating_bias.

    A rating belongs to the film, not to one watch, so every entry for the film
    that carries no rating gets it. An entry that already has one is left alone,
    because the diary records that watch and is the more specific source.

    Returns whether any entry changed, so the run can report what the row did.
    """
    if rating is None:
        return False
    changed = False
    for entry in film_entries:
        if entry["rating"] is None:
            entry["rating"] = rating
            changed = True
    return changed


def build_entries(
    rows_by_source: dict[str, list[ParsedRow]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Merge every source into one entry per film and watch date.

    Returns the entries and a count of what each row did, because a row that
    does not become an entry has to be reported rather than lost. Two rows can
    legitimately share one entry: the diary and reviews files describe the same
    watch, and the contract builds guid from the slug and the date. A rating for
    a film that already has an entry fills in a rating that entry is missing,
    and is otherwise skipped.
    """
    entries: dict[tuple[str, str | None], dict[str, Any]] = {}
    # Every entry each film has, so a rating recorded once for the film can
    # reach all of them. Its keys are the films that have an entry at all.
    entries_per_film: dict[str, list[dict[str, Any]]] = {}
    counters = {
        "rows_that_started_an_entry": 0,
        "rows_merged_into_an_entry_already_built": 0,
        "rows_that_rated_an_entry_already_built": 0,
        "rows_for_a_film_already_logged": 0,
        "liked_films_with_no_entry": 0,
    }

    def add(film: FilmReference, watched_date: str | None, row: ParsedRow) -> None:
        key = (film.slug, watched_date)
        entry = entries.get(key)
        if entry is None:
            entry = new_entry(film, watched_date, row)
            entries[key] = entry
            entries_per_film.setdefault(film.slug, []).append(entry)
            counters["rows_that_started_an_entry"] += 1
        else:
            fill_gaps(entry, row)
            counters["rows_merged_into_an_entry_already_built"] += 1

    # diary.csv and reviews.csv are the only dated sources.
    for source in ("diary", "reviews"):
        for row in rows_by_source.get(source, []):
            add(row.film, row.watched_date, row)

    # A rating for a film with no entry means the film was seen but never logged
    # with a date. A rating for a film already logged is the rating that entry
    # may be missing, so it is applied rather than dropped.
    for row in rows_by_source.get("ratings", []):
        film_entries = entries_per_film.get(row.film.slug)
        if film_entries is None:
            add(row.film, None, row)
        elif apply_separate_rating(film_entries, row.rating):
            counters["rows_that_rated_an_entry_already_built"] += 1
        else:
            counters["rows_for_a_film_already_logged"] += 1

    # watched.csv has a Date column, but it records when the film was marked as
    # seen, not when it was seen, so these entries stay undated. It carries no
    # rating, so it has nothing to add to a film that is already logged.
    for row in rows_by_source.get("watched", []):
        if row.film.slug in entries_per_film:
            counters["rows_for_a_film_already_logged"] += 1
            continue
        add(row.film, None, row)

    liked_slugs = {row.film.slug for row in rows_by_source.get("likes", [])}
    for entry in entries.values():
        entry["liked"] = entry["slug"] in liked_slugs
    # A like for a film with no entry marks nothing. It is usually a film liked
    # without being logged, but it can also be a slug that failed to resolve.
    counters["liked_films_with_no_entry"] = len(liked_slugs - entries_per_film.keys())

    # Oldest first, with undated entries last, so the file reads as a timeline
    # and two runs of the same export produce the same bytes.
    ordered = sorted(
        entries.values(),
        key=lambda entry: (entry["watched_date"] or "9999-99-99", entry["slug"]),
    )
    return ordered, counters


def build_watchlist(rows: Iterable[ParsedRow]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Turn watchlist rows into the watchlist array, one record per film.

    The export is the only source that records when a film was added to the
    watchlist, so a date read from it is the real one and added_date_estimated
    is false. The weekly reader of the public watchlist pages keeps any date it
    already has, so these real dates survive it, and it marks a film it sees for
    the first time as estimated instead.

    A row whose date cell is blank or unreadable gets no date, and it must not
    claim one: added_date_estimated is false only when a real date was read.
    Writing false beside a missing date would publish "added on nothing" as a
    measurement, and extras.watchlist.estimated_date_share is what the site uses
    to say how much of the watchlist age figure is guesswork.

    Returns the watchlist and a count of the repeated rows it dropped, so a film
    listed twice is reported rather than quietly halving the row count, and a
    count of the rows that carried no readable date.
    """
    watchlist: dict[str, dict[str, Any]] = {}
    counters = {"repeated_watchlist_rows": 0, "watchlist_rows_without_a_date": 0}
    for row in rows:
        if row.film.slug in watchlist:
            counters["repeated_watchlist_rows"] += 1
            continue
        if row.logged_date is None:
            counters["watchlist_rows_without_a_date"] += 1
        watchlist[row.film.slug] = {
            "slug": row.film.slug,
            "title": row.film.title,
            "year": row.film.year,
            "added_date": row.logged_date,
            "added_date_estimated": row.logged_date is None,
        }
    ordered = sorted(
        watchlist.values(),
        key=lambda film: (film["added_date"] or "9999-99-99", film["slug"]),
    )
    return ordered, counters


def build_history(
    contents: ExportContents,
    short_link_slugs: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read every table and return the history document and a run summary.

    short_link_slugs is passed in rather than read here, so that this function
    stays a pure reading of what it was given and a caller can build a history
    from a map it chose. load_short_link_slugs reads the committed one.

    The summary accounts for every row: the ones that became entries, and the
    ones that did not, each under the reason it did not.
    """
    rows_by_source: dict[str, list[ParsedRow]] = {}
    per_source_counters: dict[str, dict[str, Any]] = {}

    for source, table in contents.tables.items():
        rows, counters = parse_rows(table, short_link_slugs)
        rows_by_source[source] = rows
        per_source_counters[source] = {"file": table.member_path, **counters}

    # Resolve short-link rows against the slugs the other files spelled out.
    slug_index = index_real_slugs(row for rows in rows_by_source.values() for row in rows)
    rows_by_source = {
        source: [resolve_slug(row, slug_index) for row in rows]
        for source, rows in rows_by_source.items()
    }

    entries, entry_counters = build_entries(rows_by_source)
    watchlist, watchlist_counters = build_watchlist(rows_by_source.get("watchlist", []))

    history = {
        "username": LETTERBOXD_USER,
        "entry_count": len(entries),
        "entries": entries,
        "watchlist": watchlist,
    }

    # Read as plain sentences, because these lines are printed as they are.
    rows_not_kept = {
        "blank lines in the files": sum(
            counters["blank_rows"] for counters in per_source_counters.values()
        ),
        "rows that named no film": sum(
            counters["rows_without_a_film"] for counters in per_source_counters.values()
        ),
        "rows merged into an entry already built": entry_counters[
            "rows_merged_into_an_entry_already_built"
        ],
        "rows that only rated an entry already built": entry_counters[
            "rows_that_rated_an_entry_already_built"
        ],
        "rows for a film that was already logged": entry_counters[
            "rows_for_a_film_already_logged"
        ],
        "liked films with no entry to mark": entry_counters["liked_films_with_no_entry"],
        "repeated watchlist rows": watchlist_counters["repeated_watchlist_rows"],
        "files repeating a source already read": len(contents.duplicate_files),
    }

    summary = {
        "sources": per_source_counters,
        "ignored_files": contents.ignored_files,
        "duplicate_files": contents.duplicate_files,
        "refused_lookalikes": contents.refused_lookalikes,
        "private_files": contents.private_files,
        "rows_not_kept": rows_not_kept,
        "short_link_map_films": len(short_link_slugs),
        "rows_with_a_slug_from_the_short_link_map": sum(
            counters["slugs_from_the_short_link_map"] for counters in per_source_counters.values()
        ),
        "rows_with_a_slug_invented_from_a_title": sum(
            counters["slugs_invented_from_a_title"] for counters in per_source_counters.values()
        ),
        "films_with_a_slug_from_the_short_link_map": len(
            {
                row.film.slug
                for rows in rows_by_source.values()
                for row in rows
                if row.film.slug_source == SLUG_FROM_SHORT_LINK_MAP
            }
        ),
        "entries": len(entries),
        "with_a_date": sum(1 for entry in entries if entry["watched_date"]),
        "with_a_rating": sum(1 for entry in entries if entry["rating"] is not None),
        "with_a_review": sum(1 for entry in entries if entry["review"]),
        "provisional_slugs": sum(1 for entry in entries if entry["slug_provisional"]),
        "slugs_from_the_row_itself": sum(
            1 for entry in entries if slug_is_from_row_identity(entry["slug"])
        ),
        "watchlist": len(watchlist),
        "watchlist_films_without_a_date": watchlist_counters["watchlist_rows_without_a_date"],
    }
    return history, summary


def write_history(history: dict[str, Any], target: Path = HISTORY_FILE) -> None:
    """Write the history document to disk as formatted JSON."""
    ensure_dirs()
    target.write_text(
        json.dumps(history, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def print_summary(export_path: Path, summary: dict[str, Any], target: Path) -> None:
    """Print what the run read, what it did not keep, and what it wrote.

    Every row that did not become an entry is printed with a count. A row that
    disappears with no number beside it is the failure this script cannot let
    the reader miss.
    """
    print(f"Read {export_path}")
    name_width = max(
        (len(counters["file"]) for counters in summary["sources"].values()),
        default=0,
    )
    for source in sorted(summary["sources"]):
        counters = summary["sources"][source]
        line = f"  {counters['file']:<{name_width}}  {counters['rows']:>6} rows"
        if counters["blank_rows"]:
            line += f", {counters['blank_rows']} blank"
        if counters["rows_without_a_film"]:
            line += f", {counters['rows_without_a_film']} named no film"
        if counters["unreadable_dates"]:
            line += f", {counters['unreadable_dates']} dates unreadable"
        if counters["slugs_from_the_short_link_map"]:
            line += f", {counters['slugs_from_the_short_link_map']} slugs from the short-link map"
        if counters["slugs_invented_from_a_title"]:
            line += f", {counters['slugs_invented_from_a_title']} slugs invented from a title"
        print(line)

    for path in summary["private_files"]:
        print(f"  {path} was refused unread: it holds the member's email address")
    for path in summary["refused_lookalikes"]:
        member_file = path.rsplit("/", 1)[-1]
        print(
            f"  {path} was refused: it has the headers of {member_file} but is not the "
            f"member's own, which is only read at the export root"
        )

    ignored = summary["ignored_files"]
    if ignored:
        shown = ", ".join(ignored[:6])
        if len(ignored) > 6:
            shown += f", and {len(ignored) - 6} more"
        counted = "1 file is" if len(ignored) == 1 else f"{len(ignored)} files are"
        print(f"  {counted} not a source and went unread: {shown}")
    for path, source in summary["duplicate_files"]:
        print(f"  {path} went unread: {source} was already read from a shallower path")

    print("\nRows not kept as entries:")
    reasons = [(reason, count) for reason, count in summary["rows_not_kept"].items() if count]
    if reasons:
        reason_width = max(len(reason) for reason, _ in reasons)
        for reason, count in reasons:
            print(f"  {reason + ':':<{reason_width + 1}} {count}")
    else:
        print("  none, every row either became an entry or added to one")

    # The slug is what the RSS feed, the curated lists, and the TMDB cache all
    # join on, so how many slugs were read and how many were invented is the one
    # number that says whether this history can be matched to anything.
    known = summary["short_link_map_films"]
    rows_resolved = summary["rows_with_a_slug_from_the_short_link_map"]
    films_resolved = summary["films_with_a_slug_from_the_short_link_map"]
    made_up = summary["rows_with_a_slug_invented_from_a_title"]
    print("\nSlugs, which are what the rest of the pipeline joins on:")
    print(f"  short links the map knows:     {known:>6}")
    print(f"  rows the map answered for:     {rows_resolved:>6}")
    print(f"  distinct films it named:       {films_resolved:>6}")
    print(f"  rows left with a made-up slug: {made_up:>6}")
    if known == 0:
        print(
            f"  {SHORT_LINKS_FILE} named no film, so every slug here was invented from a "
            f"title and matches nothing in the RSS feed or the curated lists.\n"
            f"  What to do: run scripts/resolve_short_links.py against this export first, "
            f"then run this script again."
        )
    elif made_up:
        print(
            f"  Those rows match nothing in the RSS feed or the curated lists, so their films "
            f"will appear twice and count for nothing in list progress.\n"
            f"  What to do: run scripts/resolve_short_links.py against this export to add the "
            f"missing short links to {SHORT_LINKS_FILE}, then run this script again."
        )

    print(f"\nEntries: {summary['entries']}")
    print(f"  with a watched date: {summary['with_a_date']}")
    print(f"  with a rating:       {summary['with_a_rating']}")
    print(f"  with a review:       {summary['with_a_review']}")
    if summary["provisional_slugs"]:
        print(
            f"  provisional slugs:   {summary['provisional_slugs']} "
            f"(invented from the title, still to be resolved)"
        )
    if summary["slugs_from_the_row_itself"]:
        print(
            f"  of those, {summary['slugs_from_the_row_itself']} had no usable title and are "
            f"keyed on the row's own link instead"
        )
    undated_watchlist = summary["watchlist_films_without_a_date"]
    if undated_watchlist:
        print(
            f"Watchlist: {summary['watchlist']} films, "
            f"{summary['watchlist'] - undated_watchlist} with an added date read from the export"
        )
        print(
            f"  {undated_watchlist} rows had no readable date, so those films are marked as "
            f"having no real added date and the weekly watchlist run will estimate one"
        )
    else:
        print(f"Watchlist: {summary['watchlist']} films, every added date read from the export")
    print(f"\nWrote {target}")


def main(argv: Sequence[str] | None = None) -> int:
    """Parse an export into data/history.json and report what was read."""
    parser = argparse.ArgumentParser(
        description="Turn a Letterboxd data export into data/history.json.",
    )
    parser.add_argument(
        "export",
        type=Path,
        help="the export ZIP, or a directory holding the unzipped CSV files",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace data/history.json if it already exists",
    )
    arguments = parser.parse_args(argv)

    if HISTORY_FILE.exists() and not arguments.force:
        print(
            f"{HISTORY_FILE} already exists.\n"
            f"  Backfilling would replace it, including any entries the weekly RSS run added "
            f"since it was written.\n"
            f"  What to do: move that file aside first, or pass --force if replacing it is "
            f"what you want.",
            file=sys.stderr,
        )
        return 2

    try:
        contents = read_export(arguments.export)
        history, summary = build_history(contents, load_short_link_slugs())
    except BackfillError as error:
        print(error, file=sys.stderr)
        return 1

    write_history(history)
    print_summary(arguments.export, summary, HISTORY_FILE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
