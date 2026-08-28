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

Two keys appear on export entries that the data contract does not list:
slug_provisional and letterboxd_uri. Some export rows carry a boxd.it short
link instead of a film URL, and a short link does not contain the slug. Those
rows get a slug built from the title and year, flagged as provisional, with the
original link kept so a later step can resolve it against Letterboxd. A title
that yields no letters or digits falls back to the boxd.it id in the row's own
link, because two different films under one slug merge into one entry and one
of the two films disappears.

Files are recognised by where they sit inside the export, never by file name
alone. The likes folder holds other people's reviews and lists that the member
liked, so likes/reviews.csv must never be read as the member's own reviews.csv.

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
from lib.config import HISTORY_FILE, LETTERBOXD_USER, ensure_dirs

# --------------------------------------------------------------------------
# What the export looks like
# --------------------------------------------------------------------------

# Which source each file at the top level of the export belongs to. Position
# inside the export decides this, never the bare file name: likes/reviews.csv is
# other people's reviews that the member liked, and reading it as the member's
# own reviews.csv writes other people's writing into this history.
SOURCE_BY_TOP_LEVEL_FILE = {
    "diary.csv": "diary",
    "reviews.csv": "reviews",
    "ratings.csv": "ratings",
    "watched.csv": "watched",
    "watchlist.csv": "watchlist",
}

# The only file below the top level that belongs to the member.
LIKED_FILMS_PATH = "likes/films.csv"

# Folders the export gives a meaning to. None of them is the dated folder the
# export is wrapped in, so removing shared folders stops at one of these.
NON_WRAPPER_FOLDERS = frozenset({"likes", "lists", "deleted"})

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


class BackfillError(Exception):
    """Something about the export stops the run and needs a human decision."""


class ExportNotReadable(BackfillError):
    """The path given is not an export ZIP or an unpacked export directory."""


class ExportColumnMissing(BackfillError):
    """A CSV in the export does not carry a column the parser needs."""


@dataclass(frozen=True)
class FilmReference:
    """One film as a single export row identifies it."""

    slug: str
    slug_is_provisional: bool
    letterboxd_uri: str | None
    title: str | None
    year: int | None


@dataclass(frozen=True)
class ExportContents:
    """Everything one export holds: the tables read, and the files left unread.

    The unread files are carried out of here so the run can report them. A file
    this parser does not use is normal, but the reader still gets to see that it
    was seen and passed over.
    """

    tables: dict[str, SourceTable]
    ignored_files: list[str]
    duplicate_files: list[tuple[str, str]]


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
    normalize_member_path and shortened by strip_wrapper. The folder a file sits
    in decides the answer, because several file names appear twice in an export
    under different owners: reviews.csv at the top level is the member's own
    writing, while likes/reviews.csv is other people's reviews the member liked.

    The export also holds comments.csv, profile.csv, and a lists folder. None of
    those feed the watch history, so they return None and the run counts them.
    """
    if relative_path == LIKED_FILMS_PATH:
        return "likes"
    if "/" in relative_path:
        return None
    return SOURCE_BY_TOP_LEVEL_FILE.get(relative_path)


def _member_sort_key(member_path: str) -> tuple[int, str]:
    """Sort shallow paths first, so a top-level diary.csv beats a nested copy."""
    normalized = member_path.replace("\\", "/")
    return (normalized.count("/"), normalized)


def _read_zip(archive_path: Path) -> list[tuple[str, bytes]]:
    """Read the export files out of a ZIP, shallowest path first."""
    with zipfile.ZipFile(archive_path) as archive:
        names = sorted(
            (name for name in archive.namelist() if is_export_file(name)),
            key=_member_sort_key,
        )
        return [(name, archive.read(name)) for name in names]


def _read_directory(directory: Path) -> list[tuple[str, bytes]]:
    """Read the export files out of an unpacked export, shallowest path first."""
    files = sorted(
        (path for path in directory.rglob("*") if path.is_file() and is_export_file(str(path))),
        key=lambda path: _member_sort_key(str(path.relative_to(directory))),
    )
    return [(str(path.relative_to(directory)), path.read_bytes()) for path in files]


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

    if export_path.is_dir():
        members = _read_directory(export_path)
    elif zipfile.is_zipfile(export_path):
        members = _read_zip(export_path)
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
    depth = wrapper_depth([normalized for _, normalized, _ in export_files])

    tables: dict[str, SourceTable] = {}
    ignored_files: list[str] = []
    duplicate_files: list[tuple[str, str]] = []
    for member_path, normalized, raw in export_files:
        relative_path = strip_wrapper(normalized, depth)
        source = classify_member(relative_path)
        if source is None:
            ignored_files.append(relative_path)
            continue
        if source in tables:
            # Named as it is written in the export, because the shortened path
            # can read the same as the path of the file that was kept.
            duplicate_files.append((member_path, source))
            continue
        tables[source] = parse_csv(source, relative_path, decode_csv(raw, member_path))

    if not tables:
        found = ", ".join(ignored_files[:8]) or "(no files at all)"
        raise ExportNotReadable(
            f"{export_path} holds no file this parser recognises.\n"
            f"  Looked for: {', '.join(sorted(SOURCE_BY_TOP_LEVEL_FILE))}, {LIKED_FILMS_PATH}\n"
            f"  Found instead: {found}\n"
            f"  What to do: check that you passed the Letterboxd export and not another archive. "
            f"If the path holds more than one export, pass one export folder rather than the "
            f"folder containing them, so the files can be told apart."
        )
    return ExportContents(
        tables=tables,
        ignored_files=ignored_files,
        duplicate_files=duplicate_files,
    )


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
) -> FilmReference | None:
    """Identify the film one row is about, or None when the row names no film.

    The Letterboxd URL is the only trustworthy identifier, because the whole
    pipeline joins on slug. When the row carries a short link instead, the slug
    is built from the title and marked provisional.
    """
    uri = cell(row, uri_header)
    title = cell(row, title_header)
    year = parse_year(cell(row, year_header))

    slug = slug_from_uri(uri)
    if slug is not None:
        return FilmReference(slug, False, uri, title, year)

    if title is None:
        return None
    return FilmReference(provisional_slug(title, year, uri), True, uri, title, year)


# --------------------------------------------------------------------------
# Reading whole tables
# --------------------------------------------------------------------------


def parse_rows(table: SourceTable) -> tuple[list[ParsedRow], dict[str, int]]:
    """Read one source table into rows the merge step can use.

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
    }

    for row in table.rows:
        film = film_reference(row, uri_header, title_header, year_header)
        if film is None:
            counters["rows_without_a_film"] += 1
            continue

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

    An export can mix the two link styles: ratings.csv with full film URLs and
    diary.csv with short links, for example. This index lets a short-link row
    borrow the real slug of the same film rather than opening a second entry
    under a provisional one.
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
    return replace(row, film=replace(film, slug=real_slug, slug_is_provisional=False))


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
    watchlist, so every date written here is the real one and
    added_date_estimated is false. The weekly reader of the public watchlist
    pages keeps any date it already has, so these real dates survive it, and it
    marks a film it sees for the first time as estimated instead.

    Returns the watchlist and a count of the repeated rows it dropped, so a film
    listed twice is reported rather than quietly halving the row count.
    """
    watchlist: dict[str, dict[str, Any]] = {}
    counters = {"repeated_watchlist_rows": 0}
    for row in rows:
        if row.film.slug in watchlist:
            counters["repeated_watchlist_rows"] += 1
            continue
        watchlist[row.film.slug] = {
            "slug": row.film.slug,
            "title": row.film.title,
            "year": row.film.year,
            "added_date": row.logged_date,
            "added_date_estimated": False,
        }
    ordered = sorted(
        watchlist.values(),
        key=lambda film: (film["added_date"] or "9999-99-99", film["slug"]),
    )
    return ordered, counters


def build_history(contents: ExportContents) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read every table and return the history document and a run summary.

    The summary accounts for every row: the ones that became entries, and the
    ones that did not, each under the reason it did not.
    """
    rows_by_source: dict[str, list[ParsedRow]] = {}
    per_source_counters: dict[str, dict[str, Any]] = {}

    for source, table in contents.tables.items():
        rows, counters = parse_rows(table)
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
        "rows_not_kept": rows_not_kept,
        "entries": len(entries),
        "with_a_date": sum(1 for entry in entries if entry["watched_date"]),
        "with_a_rating": sum(1 for entry in entries if entry["rating"] is not None),
        "with_a_review": sum(1 for entry in entries if entry["review"]),
        "provisional_slugs": sum(1 for entry in entries if entry["slug_provisional"]),
        "slugs_from_the_row_itself": sum(
            1 for entry in entries if slug_is_from_row_identity(entry["slug"])
        ),
        "watchlist": len(watchlist),
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
        print(line)

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

    print(f"\nEntries: {summary['entries']}")
    print(f"  with a watched date: {summary['with_a_date']}")
    print(f"  with a rating:       {summary['with_a_rating']}")
    print(f"  with a review:       {summary['with_a_review']}")
    if summary["provisional_slugs"]:
        print(
            f"  provisional slugs:   {summary['provisional_slugs']} "
            f"(built from the title, still to be resolved)"
        )
    if summary["slugs_from_the_row_itself"]:
        print(
            f"  of those, {summary['slugs_from_the_row_itself']} had no usable title and are "
            f"keyed on the row's own link instead"
        )
    print(f"Watchlist: {summary['watchlist']} films")
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
        history, summary = build_history(contents)
    except BackfillError as error:
        print(error, file=sys.stderr)
        return 1

    write_history(history)
    print_summary(arguments.export, summary, HISTORY_FILE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
