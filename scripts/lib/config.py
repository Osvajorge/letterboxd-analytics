"""Shared settings and paths for the stats pipeline."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
CACHE = DATA / "cache"
RAW = DATA / "raw"
SITE_DATA = ROOT / "docs" / "data"

HISTORY_FILE = DATA / "history.json"
MANUAL_MATCHES_FILE = DATA / "manual-matches.json"
TMDB_CACHE_FILE = CACHE / "tmdb.sqlite"
LISTS_CACHE_DIR = CACHE / "lists"
STATS_FILE = SITE_DATA / "stats.json"

LETTERBOXD_USER = os.getenv("LETTERBOXD_USER", "osvajorge")

# Browser-like identification. Letterboxd serves list pages to ordinary clients;
# this only says who we are, it does not work around any challenge.
USER_AGENT = (
    "letterboxd-analytics/1.0 (+https://github.com/Osvajorge/letterboxd-analytics)"
)

REQUEST_TIMEOUT = 30
REQUEST_DELAY = 1.0  # seconds between requests, to stay a polite client

# The sixteen lists the reference stats panel tracks, read from that page.
CURATED_LISTS = [
    ("letterboxd-top-500", "Letterboxd Top 500", "/official/list/letterboxds-top-500-films/"),
    ("imdb-top-250", "IMDb Top 250", "/dave/list/imdb-top-250/"),
    ("oscar-best-picture", "Oscar Best Picture Winners", "/oscars/list/oscar-winning-films-best-picture/"),
    ("cannes-palme-dor", "Cannes Palme d'Or Winners", "/brsan/list/cannes-palme-dor-winners/"),
    ("billion-club", "$1 Billion Club", "/000_leo/list/1-billion-club/"),
    ("afi-100", "AFI 100 Years 100 Movies", "/afi/list/afis-100-years100-movies-10th-anniversary/"),
    ("sight-and-sound", "Sight and Sound Greatest Films", "/sightsoundmag/list/sight-and-sounds-greatest-films-of-all-time/"),
    ("1001-films", "1001 Films To See Before You Die", "/gubarenko/list/1001-movies-you-must-see-before-you-die-latest/"),
    ("tspdt-all-time", "They Shoot Pictures All Time", "/thisisdrew/list/they-shoot-pictures-dont-they-1000-greatest-7/"),
    ("tspdt-21st-century", "They Shoot Pictures 21st Century", "/thisisdrew/list/they-shoot-pictures-dont-they-21st-centurys-5/"),
    ("top-250-women", "Top 250 Women-Directed", "/official/list/top-250-films-by-women-directors/"),
    ("top-250-black", "Top 250 Black-Directed", "/official/list/top-250-films-by-black-directors/"),
    ("top-250-most-fans", "Top 250 Most Fans", "/official/list/top-250-films-with-the-most-fans/"),
    ("top-250-documentary", "Top 250 Documentaries", "/official/list/top-250-documentary-films/"),
    ("top-250-animation", "Top 250 Animation", "/official/list/top-250-animated-films/"),
    ("top-250-horror", "Top 250 Horror", "/official/list/top-250-horror-films/"),
]

BASE_URL = "https://letterboxd.com"


def ensure_dirs() -> None:
    """Create every directory the pipeline writes into."""
    for path in (DATA, CACHE, RAW, SITE_DATA, LISTS_CACHE_DIR):
        path.mkdir(parents=True, exist_ok=True)
