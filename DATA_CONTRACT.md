# Data contract

Every script in this pipeline reads and writes the shapes below. Change a shape
here before changing it in code.

## data/history.json

The full watch history: the export provides the bulk, the weekly RSS read keeps
it current. Entries are unique by `guid`.

```json
{
  "username": "osvajorge",
  "entry_count": 827,
  "entries": [
    {
      "guid": "letterboxd-review-1273678751",
      "slug": "el-compadre-mendoza",
      "title": "El compadre Mendoza",
      "year": 1934,
      "watched_date": "2026-01-04",
      "logged_date": "2026-01-06",
      "rating": 4.5,
      "rewatch": false,
      "liked": true,
      "tmdb_id": 114626,
      "review": null,
      "source": "rss"
    }
  ],
  "watchlist": [
    {
      "slug": "the-brutalist",
      "title": "The Brutalist",
      "year": 2024,
      "added_date": "2025-03-02",
      "added_date_estimated": false
    }
  ]
}
```

Field rules:

- `guid` from RSS is Letterboxd's own. Export rows have no guid, so build one as
  `export:<slug>:<watched_date or "undated">`.
- `slug` is the film identifier used everywhere. It is the path segment in a
  Letterboxd film URL and it is what curated lists key on.
- `watched_date` is `YYYY-MM-DD`, or null when the source has no date.
- `logged_date` is `YYYY-MM-DD`: the day the entry was written down, which the
  export records and RSS does not. It is null on every RSS entry and on any
  export row whose file has no date column. `extras.logging_lag` is the gap
  between this and `watched_date`, so it needs both.
- `rating` is the member's own rating on the 0.5 to 5.0 scale, or null.
- `tmdb_id` is the id the RSS feed carried for that entry, or null. The export
  carries none and nothing fills one in later, so 47 of this account's 830 entries
  hold one. It is a weaker source than `data/tmdb-ids.json` and is bound by that
  file's refusals.
- `source` is `rss` or `export`. RSS wins on conflict: it is newer and richer.
- `slug_provisional` (boolean, export entries only) marks a slug that was built from
  the title and year because the export gave a `boxd.it` short link instead of a full
  film URL. A later step may resolve it to the real slug.
- `letterboxd_uri` (string or null, export entries only) keeps the original link so a
  provisional slug can be resolved without re-reading the export.
- RSS entries carry neither field. Both are optional, and any consumer must treat a
  missing one as absent rather than as an error.

### The watchlist is read two ways

Membership comes from the member's public watchlist pages, read weekly, so a film
added or removed on the site is reflected within the week.

Those pages do not say when a film was added. Only the export does. So `added_date`
is filled two ways, and `added_date_estimated` says which:

- `false`: the date came from the export's watchlist file and is the real one.
- `true`: the film first appeared to the weekly reader on that date, which is an
  upper bound on when it was really added, not the actual date.

The weekly reader must preserve the `added_date` already stored for a slug it has
seen before. Overwriting it would reset every film's age to the current week and
make `median_age_days` meaningless.

Membership itself is a snapshot, not a log: a film removed on the site disappears
here too.

## data/tmdb-ids.json

Which TMDB record each film is. Nothing else in the pipeline answers that
question, and nothing guesses at it.

`scripts/resolve_tmdb_ids.py` writes this file by reading each film's own
Letterboxd page, which states the TMDB id and type Letterboxd itself uses.
`scripts/enrich_tmdb.py`, `scripts/enrich_people_and_collections.py` and
`scripts/build_stats.py` read it. It is committed, because a film's TMDB id does
not change, so a film is resolved once and never again.

`scripts/enrich_tmdb.py` exits 1 when it is missing. It has nothing to download
without it.

```json
{
  "the-beasts-2022": { "tmdb_id": 848685, "tmdb_type": "movie" },
  "el-compadre-mendoza": { "tmdb_id": null, "tmdb_type": null }
}
```

One entry per film, keyed by the same `slug` as `data/history.json`. Every entry
holds both fields: `tmdb_id` is an integer or null, and `tmdb_type` is `"movie"`
or null.

### Only `tmdb_type == "movie"` names a film

That is the whole rule, and every reader applies it the same way:

| The entry says | What it means |
| --- | --- |
| an id and `"movie"` | the TMDB film to download, and the only case that names a film |
| an id and any other type, `null` included | TMDB does not hold this as a film. Nothing is downloaded and nothing is guessed |
| `"tmdb_id": null` | Letterboxd names no TMDB record for this film at all |
| no entry for the slug | nobody has resolved this film yet. Run `scripts/resolve_tmdb_ids.py` |

The last two rows are different states and must not be merged. A null id is a
settled answer that costs no request this week and none in any later week. A
missing entry is an unanswered question, which is what every film the weekly RSS
merge adds starts as.

**Every refusal binds every weaker source.** A slug this file names no film for
gets no id from a history entry, from the `lookups` table, or from anything else.
Those sources hold search answers, and a search answer for a film TMDB has no
film record for is a different film: its runtime, its cast and its country filed
under this one. Filling a refusal from a weaker source does not close a gap, it
publishes a wrong film, so the refusal wins.

`data/manual-matches.json` is the one source that outranks this file. It maps a
slug to a TMDB id a person has checked by hand, and a person who has opened the
film's TMDB page outranks every automatic source. It is empty on this account.

### What the file holds today

827 slugs, one for every film in the history:

- 810 typed `"movie"`, each with its own id, so no two films share one.
- 17 with a null id, because Letterboxd names no TMDB record for them.
- none of any other type.

Of the 810, TMDB's film endpoint answers for 789 and holds no record for the
other 21. Twenty of those are episodes of an anthology or a miniseries that
Letterboxd lists as films, so the two sites disagree about what the id is rather
than about which record it is. Only TMDB can settle that, so nothing is written
down for those films and every run asks once more.
`coverage.films_with_tmdb_data` reports the 789.

## data/cache/lists/<list_id>.json

```json
{ "title": "AFI 100 Years 100 Movies", "path": "/afi/list/...", "films": [ { "slug": "citizen-kane", "name": "Citizen Kane (1941)" } ] }
```

## data/cache/tmdb.sqlite

```sql
CREATE TABLE films          (tmdb_id INTEGER PRIMARY KEY, slug TEXT, payload TEXT, fetched_at TEXT);
CREATE TABLE credits        (tmdb_id INTEGER PRIMARY KEY, payload TEXT, fetched_at TEXT);
CREATE TABLE lookups        (slug TEXT PRIMARY KEY, tmdb_id INTEGER, resolved_at TEXT);
CREATE TABLE collections    (tmdb_id INTEGER PRIMARY KEY, payload TEXT, fetched_at TEXT);
CREATE TABLE person_credits (tmdb_id INTEGER PRIMARY KEY, payload TEXT, fetched_at TEXT);
```

`payload` holds the raw TMDB JSON response. Keeping it raw means a new stat
never requires re-downloading anything.

`tmdb_id` is the id of whatever the row is about, so it is a film id in `films`
and `credits`, a collection id in `collections`, and a person id in
`person_credits`. Those are separate TMDB namespaces, and no table ever joins on
another table's ids.

### The two tables that hold a fact no film payload carries

`films` and `credits` answer questions about one film. Two modules ask something
one level up, which no number of film payloads can answer:

| Table | Written by | From | Answers |
| --- | --- | --- | --- |
| `collections` | `scripts/enrich_people_and_collections.py` | `/collection/{id}` | how many films a collection holds, so `collections` can say "seen 7 of 8" |
| `person_credits` | `scripts/enrich_people_and_collections.py` | `/person/{id}/movie_credits` | a director's whole filmography, so `extras.director_completeness` can say "seen 12 of 30" |

A film payload names the collection a film belongs to and stops there, and a
film's credits list that film's crew and never a person's body of work. Without
these two tables both modules emit an empty array rather than a denominator
counted from the films already seen, which would report every collection and
every director as complete.

Which records are asked for:

- one collection for every collection any film in the history belongs to.
- one filmography for every director with at least two films in the history. A
  director seen once is left out on purpose, because "1 of 30" and "1 of 1" both
  read as "seen once". `scripts/build_stats.py` applies the same floor, so the
  table and the panel hold the same set of directors.

Both tables are cached permanently. A collection's size and a director's
filmography change rarely, so a row already present is never fetched again and a
weekly run costs no requests for either. To refresh one on purpose, delete its
row and run the script again:

```
sqlite3 data/cache/tmdb.sqlite "DELETE FROM collections WHERE tmdb_id = 230"
```

Only an answer from TMDB is ever written. A request that got no answer records
nothing, so the next run asks again. That matters more here than anywhere else
in this cache: these rows are never refreshed, so a failure written down as a
settled answer would be believed forever. A 404 is an answer, but it is not
written either, because these ids come from TMDB's own film payloads and a 404
means TMDB changed rather than that the id was ever a guess.

### Run order

```
scripts/resolve_tmdb_ids.py                data/tmdb-ids.json
scripts/enrich_tmdb.py                     films, credits, lookups
scripts/enrich_people_and_collections.py   collections, person_credits
scripts/build_stats.py                     docs/data/stats.json
```

The first step is what says which TMDB record each film is, and the three steps
under it read its answers, so it runs first and it runs every week. Every film
the RSS merge adds is a film it has not answered for yet, and a film with no
answer downloads nothing: no runtime, no genres, no countries, no languages, no
cast, no crew and no collection.

The third step reads the film payloads and the credits to learn which collections
and which directors the history needs, so it must run after the second and before
the last.

`.github/workflows/update-stats.yml` runs all four every Monday.

## docs/data/stats.json

The single file the site reads. Nothing is computed in the browser.

> **Every number below is a made-up placeholder that shows the shape, not a
> measurement.** Do not read any of them as this account's data. The real values
> live in `docs/data/stats.json`, and a module that has no input yet reports zero
> or null there, not the figure printed here.

```json
{
  "generated_at": "2026-08-28",
  "username": "osvajorge",
  "totals": {
    "films": 999, "hours": 999, "directors": 999, "countries": 999,
    "longest_streak_weeks": 99, "multi_film_days": 99
  },
  "coverage": {
    "films_total": 999,
    "films_with_a_date": 999,
    "films_with_a_rating": 999,
    "films_with_tmdb_data": 999
  },
  "row_totals": { "directors": 999, "countries.most_watched": 99, "extras.title_words": 999 },
  "by_year": [ { "year": 2025, "films": 120, "diary": 118, "ratings": { "0.5": 1, "5.0": 9 } } ],
  "decades": [ { "decade": 1960, "films": 40, "average_rating": 4.1 } ],
  "genres":    { "most_watched": [ { "name": "Drama", "count": 210 } ], "highest_rated": [ { "name": "Drama", "average": 4.0, "count": 210 } ] },
  "countries": { "most_watched": [], "highest_rated": [] },
  "languages": { "most_watched": [], "highest_rated": [] },
  "cast":      [ { "tmdb_id": 1, "name": "Toshiro Mifune", "count": 12, "profile_path": "/x.jpg" } ],
  "directors": [ { "tmdb_id": 1, "name": "Akira Kurosawa", "count": 12, "average_rating": 4.4, "profile_path": null } ],
  "studios":   [ { "name": "A24", "count": 30, "average_rating": 3.9 } ],
  "collections": [ { "name": "The Godfather Collection", "seen": 2, "total": 3 } ],
  "world_map": [ { "iso_3166_1": "JP", "name": "Japan", "count": 60 } ],
  "list_progress": [ { "id": "afi-100", "title": "AFI 100 Years 100 Movies", "seen": 12, "total": 100 } ],
  "extras": {
    "rating_bias": { "member_average": 3.6, "tmdb_average": 6.9, "delta": -0.15 },
    "rating_drift": [ { "year": 2025, "average": 3.7 } ],
    "rewatch_rate": 0.08,
    "watchlist": { "size": 999, "median_age_days": 999, "conversion_rate": 0.99, "estimated_date_share": 0.99 },
    "heatmap": [ { "date": "2026-01-04", "count": 1 } ],
    "runtime": { "total_minutes": 91200, "median": 108, "distribution": [] },
    "decade_gaps": [ 1920, 1930 ],
    "director_completeness": [ { "name": "Akira Kurosawa", "seen": 12, "filmography": 30 } ],

    "contrarian_index": {
      "hotter_than_crowd": [ { "slug": "x", "title": "X", "year": 2001, "member_rating": 5.0, "crowd_rating": 2.6, "delta": 2.4 } ],
      "colder_than_crowd": []
    },
    "obscurity": { "median_vote_count": 1240, "quartiles": [120, 1240, 8600], "most_obscure": [], "most_popular": [] },
    "release_recency": { "median_days_after_release": 3650, "by_year": [ { "year": 2025, "median_days": 220 } ] },
    "half_star_usage": { "half_star_share": 0.34, "distribution": [ { "rating": 0.5, "count": 1 } ] },
    "liked_but_low": [ { "slug": "x", "title": "X", "rating": 2.5 } ],

    "longest_drought": { "days": 61, "from": "2025-02-01", "to": "2025-04-03" },
    "weekday_profile": [ { "weekday": "Monday", "count": 40 } ],
    "month_seasonality": [ { "month": 1, "count": 70 } ],
    "logging_lag": { "median_days": 1, "distribution": [] },

    "lucky_director":   [ { "name": "Akira Kurosawa", "films": 6, "average_rating": 4.6 } ],
    "unlucky_director": [ { "name": "Someone", "films": 4, "average_rating": 2.1 } ],
    "background_actor": [ { "tmdb_id": 1, "name": "Someone", "count": 9, "median_billing": 11 } ],
    "crew_most_watched": {
      "composer":       [ { "tmdb_id": 1, "name": "Ennio Morricone", "count": 8 } ],
      "cinematographer":[ { "tmdb_id": 1, "name": "Roger Deakins", "count": 7 } ],
      "editor":         [], "writer": []
    },

    "life_in_days": { "days": 63.4, "would_end_on": "2026-10-30" },
    "extremes": { "shortest": { "slug": "x", "title": "X", "runtime": 62 }, "longest": null,
                  "oldest": { "slug": "y", "title": "Y", "year": 1927 }, "newest": null },
    "rating_vs_runtime": { "correlation": 0.12, "buckets": [ { "range": "60-89", "films": 20, "average_rating": 3.4 } ] },
    "title_words": [ { "word": "love", "count": 14 } ]
  }
}
```

A module with no data must emit an empty array or null, never be absent. The
site renders an empty state rather than breaking.


## Coverage, and why the site must show it

A Letterboxd account rarely has a watch date for every film. Marking a film as
seen records no date; only a diary entry does. In the account this was built for,
827 films are watched but only 284 carry a date.

So the time-based modules (`by_year`, `decades`, `heatmap`, `weekday_profile`,
`month_seasonality`, `rating_drift`, `longest_drought`, `multi_film_days`,
`longest_streak_weeks`) describe roughly a third of the library, while `totals`
describes all of it. Both are correct, and presenting them side by side without
saying so invites the reader to compare numbers that do not answer the same
question.

The `coverage` block exists so the site can state the denominator next to any
module that does not use the whole library. It is required, never absent, and its
counts are of distinct films rather than of entries.

## row_totals, and why a shortened array cannot report its own length

Most ranked modules are cut before they are written. `directors` keeps the top
50 of the 530 directors in this history, `cast` the top 50 of 31,317 actors.
The site shows fewer still, and says so under each chart: "Showing the top 24
of 530 directors".

That second number cannot come from the array. The array was already cut, so
its length is the cap, and printing it stated a display limit as a measurement:
"Showing the top 24 of 50 directors" sat directly under a tile reading "530
Directors".

`row_totals` carries the missing number. It is required, never absent, and each
key is the dotted path of a ranked module:

| Key | Counts |
| --- | --- |
| `genres.most_watched`, `countries.most_watched`, `languages.most_watched` | groups of that kind in the history |
| `genres.highest_rated`, `countries.highest_rated`, `languages.highest_rated` | groups with enough rated films to be ranked, which is a smaller set |
| `cast`, `directors`, `studios` | distinct people or companies credited |
| `collections`, `world_map`, `list_progress` | rows, for modules the file carries whole |
| `extras.director_completeness`, `extras.background_actor`, `extras.liked_but_low`, `extras.title_words` | rows the module ranked |
| `extras.lucky_director`, `extras.unlucky_director` | rows in that half of the ranking |
| `extras.contrarian_index.<end>`, `extras.obscurity.<end>` | films that qualified for that end |
| `extras.crew_most_watched.<role>` | people credited in that role |

`scripts/build_stats.py` writes each entry in `keep_top_rows`, which is the one
place a ranking is cut, so a module cannot be shortened without its full length
being recorded. A module the file carries whole records its length too, so the
site never has to tell the two cases apart.

A site reading a file with no `row_totals`, or with no entry for a module, must
print how many rows it is showing and no denominator. Inventing one from the
array is the defect this block replaces.

## Rules that apply to every extras module

- A module with no usable input emits an empty array, or null for a scalar object.
  It is never absent. The site renders an empty state; it must never show NaN.
- Any module that ranks by average rating applies the same minimum sample size as
  `highest_rated`, and that minimum counts films that CARRY a rating, not films in
  the group. A group of twenty films with one rating is a group of one.
- Ratings are compared in five-star units. TMDB `vote_average` is on a ten-point
  scale, so halve it before subtracting. `delta` is in stars, not in fractions of
  the scale.
- Time-based modules use only entries that carry a `watched_date`. Undated entries
  still count toward `totals`.
- `logging_lag` needs both the logged date and the watched date, which only the
  export carries. With RSS-only history it emits null.
- `collections` and `extras.director_completeness` need a denominator that no
  film payload carries, so a collection with no cached size and a director with
  no cached filmography are left out of their module rather than measured
  against the films already seen. `scripts/build_stats.py` reports how many were
  left out, so a short module says it is short.
- `extras.director_completeness` holds only directors with at least two films in
  the history, the same floor
  `scripts/enrich_people_and_collections.py` uses when it decides whose
  filmography to download.
- `watchlist.estimated_date_share` is the fraction of watchlist films whose
  `added_date` is a first-sighting estimate rather than the real date from the
  export. The site MUST use it to label the age figures. Before the export has
  been loaded this share is 1.0, every age is 0, and presenting that as a
  measurement would be a lie by omission.
