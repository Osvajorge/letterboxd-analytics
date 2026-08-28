"""Merge freshly read RSS entries into the stored watch history.

The export builds the history once. This script keeps it current: it folds the
weekly RSS read into `data/history.json` without disturbing anything already
there.

Entries are identified by `guid`. When the same film and watch date arrive from
both sources, the RSS version wins, because it carries a rating, a like flag,
and a TMDB id that the export does not.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.config import HISTORY_FILE, LETTERBOXD_USER, ensure_dirs

SOURCE_PRIORITY = {"export": 0, "rss": 1}


def load_history() -> dict[str, Any]:
    """Read the stored history, or start an empty one on the very first run."""
    if not HISTORY_FILE.exists():
        return {
            "username": LETTERBOXD_USER,
            "entry_count": 0,
            "entries": [],
            "watchlist": [],
        }
    return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))


def identity(entry: dict[str, Any]) -> str:
    """Return the key that decides whether two entries are the same watch.

    What identifies a watch is the film and the day it was seen, not the guid.
    The two sources number the same watch differently: the export builds
    `export:<slug>:<date>` while the feed carries Letterboxd's own review id. Keying
    on the guid therefore stores one watch twice, once per source, which inflates
    every count built from entries. On a real account that meant 880 stored entries
    for 830 actual watches.

    The guid is only a fallback, for an entry that somehow has no slug.
    """
    slug = entry.get("slug")
    if slug:
        return f"{slug}:{entry.get('watched_date') or 'undated'}"
    return str(entry.get("guid") or "")


def wins(candidate: dict[str, Any], incumbent: dict[str, Any]) -> bool:
    """Decide whether the candidate entry should replace the stored one."""
    candidate_rank = SOURCE_PRIORITY.get(candidate.get("source", ""), 0)
    incumbent_rank = SOURCE_PRIORITY.get(incumbent.get("source", ""), 0)
    if candidate_rank != incumbent_rank:
        return candidate_rank > incumbent_rank

    # Same source: prefer whichever entry knows more.
    candidate_known = sum(1 for field in ("rating", "tmdb_id", "watched_date") if candidate.get(field) is not None)
    incumbent_known = sum(1 for field in ("rating", "tmdb_id", "watched_date") if incumbent.get(field) is not None)
    return candidate_known > incumbent_known


def merge(history: dict[str, Any], incoming: list[dict[str, Any]]) -> tuple[dict[str, Any], int, int]:
    """Fold incoming entries into the history.

    Returns the updated history, how many entries were added, and how many were
    replaced by a better version of the same watch.
    """
    by_identity = {identity(entry): entry for entry in history.get("entries", [])}
    added = 0
    replaced = 0

    for entry in incoming:
        key = identity(entry)
        existing = by_identity.get(key)
        if existing is None:
            by_identity[key] = entry
            added += 1
        elif wins(entry, existing):
            by_identity[key] = entry
            replaced += 1

    entries = sorted(
        by_identity.values(),
        key=lambda item: (item.get("watched_date") or "", item.get("title") or ""),
        reverse=True,
    )

    history["entries"] = entries
    history["entry_count"] = len(entries)
    history.setdefault("username", LETTERBOXD_USER)
    history.setdefault("watchlist", [])
    return history, added, replaced


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "Usage: python scripts/merge_history.py <entries.json>\n"
            "Produce that file with: python scripts/fetch_rss.py > entries.json",
            file=sys.stderr,
        )
        raise SystemExit(2)

    source = Path(sys.argv[1])
    if not source.exists():
        print(
            f"Cannot read {source}. Run fetch_rss.py first and point this script at its output.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    incoming = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(incoming, list):
        print(
            f"{source} should hold a JSON array of entries, but it holds a {type(incoming).__name__}.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    ensure_dirs()
    history, added, replaced = merge(load_history(), incoming)
    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Read {len(incoming)} entries from {source.name}.")
    print(f"Added {added}, replaced {replaced}.")
    print(f"History now holds {history['entry_count']} entries at {HISTORY_FILE}.")


if __name__ == "__main__":
    main()
