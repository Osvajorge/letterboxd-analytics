# Letterboxd analytics

A self-hosted rebuild of the Letterboxd Pro stats panel.

It takes one member's watch history, works out the same breakdowns the paid panel
shows, and publishes them as a static site that refreshes itself once a week. The
account it is built for is `osvajorge`, with 827 watched films.

The site reads exactly one file, `docs/data/stats.json`. Nothing is computed in
the browser. `DATA_CONTRACT.md` defines that file, and it is the authority: change
a shape there before changing it in code.

## What it shows

Ported from the reference panel: header totals, films by year, highest rated
decades, genres, countries, languages, cast, directors, studios, collections,
countries ranked by films watched, and progress against 16 curated lists.

The reference panel draws its countries on a world map. This one ranks them in a
bar chart instead, from the same `world_map` figures. A map needs boundary data
this project does not carry, and a ranked chart answers the same question.

Added, because the data supports them and the reference panel does not show them:
rating bias against the TMDB average, rating drift over the years, rewatch rate,
watchlist age and conversion, a viewing heatmap, director completeness, decade
coverage gaps, runtime distribution, and the seventeen further modules listed
under `extras` in `DATA_CONTRACT.md`.

## Where the data comes from

| Source | What it gives | When | Credential |
|---|---|---|---|
| Export archive | full history, ratings, diary dates, rewatches, reviews, and the real date each watchlist film was added | once, by hand | your own session, on your own machine |
| Public RSS feed | recent entries: watched date, rating, rewatch, like, review, TMDB id | weekly | none |
| Public watchlist pages | which films are on the watchlist now, 1,044 of them | weekly | none |
| Curated list pages | membership of the 16 tracked lists, 5,882 films in total | weekly | none |
| TMDB API | genres, countries, languages, runtime, cast, crew, studios, collections | weekly | free API key |

Films are joined across every one of those sources by the Letterboxd **slug**, the
path segment in a film URL. Never by title. Titles repeat across remakes and
translations, and title matching fails on roughly one film in twenty. Slugs do
not.

### The watchlist has membership from one source and dates from another

The watchlist pages say which films are on the list. They never say when a film
was added. Only the export does.

So a film the weekly reader has not seen before is stamped with the day it first
appeared, and `added_date_estimated` marks that date as an estimate rather than a
measurement. A film already stored keeps whatever date it had, because restamping
every film each week would reset all of their ages to zero.

Until the export has been loaded, every watchlist date is a first-sighting
estimate and the age figures read as zero. That is the honest answer, not a bug.
`DATA_CONTRACT.md` sets out how the site must label them.

## Why an export plus RSS, and not a scrape of the profile

Two sources here are read from public HTML: the curated list pages and the
member's own watchlist. Both are plain paginated pages that need no sign-in.

The profile is a different matter. Scraping it would give less than this, not
more. Measured on 2026-08-28:

- `/<user>/films/diary/` returns **403** behind a bot challenge. The diary is
  where watch dates live, and every streak, by-year, and heatmap figure depends
  on them.
- `robots.txt` disallows the aggregation views for **every** crawler:
  `/*/genre/*`, `/*/country/*`, `/*/decade/*`, and `/*/by/*`. Those are the
  breakdown pages the panel is built from.
- `robots.txt` also sets `User-agent: ClaudeBot` to `Disallow: /`, so AI clients
  are blocked across the whole domain.
- `/<user>/films/` does answer with 200, but it pages 72 films at a time and
  carries no watch dates at all.

So scraping the profile means more requests, breakage on any markup change,
working around an active bot challenge, and a thinner result. The export already
holds the full history in one download, and the public RSS feed keeps it current
for free.

The feed's limit is worth knowing: it is a rolling window of about 50 diary
entries. It can keep a history current, but it cannot build one. That is why the
one-time backfill exists.

Reading HTML at all is the fragile part of this pipeline. A markup change breaks
a reader silently, because the page still answers 200 and simply matches nothing.
The list refresh guards against exactly that, and the guard is described under
[How the weekly refresh works](#how-the-weekly-refresh-works).

## Setup

You need Python 3.11 or newer and git.

1. **Install the dependencies.**

   ```sh
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Copy the environment file.**

   ```sh
   cp .env.example .env
   ```

   It holds two values, `TMDB_API_KEY` and `LETTERBOXD_USER`. `.env` is
   git-ignored.

3. **Get a TMDB key.** Create one at
   <https://www.themoviedb.org/settings/api>. It is free and read-only. Paste it
   into `.env` as `TMDB_API_KEY`.

4. **Download your export.** In Letterboxd, go to Settings, then Data, then
   Export your data. Put the archive in `data/raw/`, which is git-ignored,
   because it contains your full account data.

5. **Run the one-time backfill.**

   ```sh
   python scripts/backfill.py data/raw/letterboxd-osvajorge.zip
   ```

   This writes `data/history.json`: for this account, 827 entries plus the
   watchlist the export carries, with the real dates films were added to it.

6. **Refresh the caches and build the site data.**

   ```sh
   python scripts/fetch_lists.py
   python scripts/fetch_watchlist.py
   python scripts/enrich_tmdb.py
   python scripts/build_stats.py
   ```

   `data/cache/lists/` is tracked in git rather than ignored, so a clone
   already carries all 16 caches. `fetch_lists.py` therefore reads no pages on
   a fresh clone and only reports what those caches hold. Pass `--force` to
   actually re-read the list pages.

   `fetch_watchlist.py` takes about 46 seconds for a 1,044-film watchlist,
   because it waits a second between pages.

   The first TMDB pass is the slow one, because it fetches every film. Results go
   into `data/cache/tmdb.sqlite` as raw responses, so later runs only fetch what
   is new, and a new statistic never means downloading anything again.

That is the whole setup. `build_stats.py` ends by printing a summary table of
every module it filled and every module it left empty, so read that to check the
run, then push. From here the weekly workflow keeps everything current.

There is no test suite in this repository yet. `requirements.txt` installs
`pytest`, but nothing collects: running it reports "no tests ran" and exits 5.

## How the weekly refresh works

`.github/workflows/update-stats.yml` runs every Monday at 06:17 UTC, and on
demand from the Actions tab. In order, it:

1. reads the public RSS feed and merges anything new into `data/history.json`,
   matching on `guid`;
2. reads the public watchlist pages and replaces the watchlist in
   `data/history.json`, keeping the added date already stored for every film it
   has seen before. This is the slow step at about 46 seconds, and it needs no
   credential, because those pages are public;
3. re-reads all 16 curated list pages. The refresh is forced, because the
   checkout supplies the cache files and a run that trusted them would read
   nothing at all;
4. restores `data/cache/tmdb.sqlite` from the previous run, looks up only the
   films that database does not already hold, and saves it back;
5. rebuilds `docs/data/stats.json`;
6. commits `docs/data/stats.json`, `data/history.json` and `data/cache/lists/`,
   and only when at least one of them changed.

### The forced list refresh is the one step that can destroy data

Step 3 overwrites 5,882 films' worth of committed cache on every run, and it does
so unattended. Letterboxd answers 200 whether or not its pages still carry the
attribute the films are read from, so a markup change is indistinguishable, from
inside the job, from 16 lists that emptied overnight.

So `fetch_lists.py` refuses any refresh that comes back far smaller than the
cache it would replace: nothing at all, or less than 90 percent of what is
already stored. A refused list keeps its cached copy untouched, and the script
exits non-zero. That stops the job before the build and before the commit, so the
week goes red and the published site keeps last week's real numbers rather than
gaining 16 rows of nothing.

A list that answers with an error is treated the same way: it keeps its cache and
it fails the run. A cache nobody can refresh is a set of progress figures that has
quietly stopped moving, which is worth a red run.

`--allow-shrink` overrides the size check for a person who has looked at the
pages and knows the lists really did shrink. It is deliberately absent from the
workflow, where nobody is watching to make that judgement.

### Caches and secrets

`data/cache/tmdb.sqlite` is git-ignored, so it travels from one run to the next
in the GitHub Actions cache rather than in a commit. GitHub drops a cache that
has not been read for seven days, which is about the gap between two scheduled
runs, so now and then a run finds no cache and resolves all 827 films from
scratch. That makes the run slow. It does not change the result.

One secret is involved, `TMDB_API_KEY`. Add it under Settings, then Secrets and
variables, then Actions. There is no Letterboxd credential in this repository and
none is needed: the feed, the watchlist pages, and the list pages are all public.
The workflow never runs on `pull_request`, so a fork cannot read even the TMDB
key.

## When it breaks

| What you see | Likely cause | What to do |
|---|---|---|
| The run stops at "Confirm the TMDB key is present" | the secret is missing or was renamed | re-add `TMDB_API_KEY` in the repository secrets |
| The TMDB step fails with 401 | the key was revoked or regenerated | create a new key, update both `.env` and the repository secret |
| The list step fails with "Refused N of 16 list refreshes" | Letterboxd changed the list markup, or blocked the run, so the pages came back far smaller than the caches | nothing was overwritten and nothing was committed. Open one of the named list pages and check whether its films still carry a `data-item-slug` attribute. If they do not, fix `SLUG_PATTERN` and `NAME_PATTERN` in `scripts/fetch_lists.py`. If the lists really did shrink that much, re-run by hand with `--allow-shrink` |
| The list step fails with "Could not read N lists" | those pages answered with an error, usually a list that was renamed, moved, or deleted, or a 403 | each one kept its cached copy. Fix the path in `scripts/lib/config.py` for a list that moved. For a 403, start the run again later |
| The watchlist step warns that it read fewer films than the site states | a watchlist page failed, or its markup changed | run `python scripts/fetch_watchlist.py` again. Treat that week's watchlist size, age and conversion figures as unreliable until the two counts agree |
| The TMDB step fetches every film, not just the new ones | the Actions cache was dropped, which GitHub does after seven days without a read | nothing. The run rebuilds `data/cache/tmdb.sqlite` and saves it again. Only the run time changes |
| The commit step says nothing to commit | no new films, no watchlist changes and no list changes that week | nothing. This is the normal case |
| New films are missing from the site | more than about 50 entries were logged since the last successful run, so the rolling window moved past them | download a fresh export and re-run the backfill. Then find out why the weekly run had been failing |
| The site renders an empty section | a module produced no data | expected behaviour. Per the data contract, an empty module emits an empty array rather than disappearing, so the page never breaks |
| Every watchlist age reads as zero | the export has not been loaded, so every added date is a first-sighting estimate | expected until step 5 of the setup has been run. See "The watchlist has membership from one source and dates from another" |

A failing step stops the job before the commit, so a red run never publishes bad
numbers: the site keeps whatever it was last given. If a scheduled run fails,
GitHub does not retry it. Fix the cause, then start a run by hand from the
Actions tab.

## What is deliberately not reproduced

**Themes and Nanogenres** are a proprietary Letterboxd classification and a
Letterboxd trademark. They are not rebuilt here, and nothing here is presented
under those names. TMDB keywords give a rough approximation, and if that is ever
added it must carry a different name.

Letterboxd's typefaces are likewise not copied. GraphikWeb, TiemposHeadlineWeb,
TiemposTextWeb, and PitchSansWeb are commercially licensed. `docs/design-tokens.css`
names open substitutes with comparable metrics.

## Repository layout

```
scripts/backfill.py       export archive   -> data/history.json   (once, on your machine)
scripts/fetch_rss.py      public RSS feed  -> recent entries      (weekly)
scripts/merge_history.py  recent entries   -> data/history.json   (weekly, merges by guid)
scripts/fetch_watchlist.py watchlist pages -> data/history.json   (weekly, membership only)
scripts/fetch_lists.py    list pages       -> data/cache/lists/   (weekly, refuses a bad read)
scripts/enrich_tmdb.py    TMDB API         -> data/cache/tmdb.sqlite
scripts/build_stats.py    all of the above -> docs/data/stats.json
scripts/lib/config.py     paths, the user, and the 16 curated list URLs

data/history.json         the watch history and the watchlist, committed
data/cache/lists/         the 16 list caches, 5,882 films, committed
data/cache/tmdb.sqlite    raw TMDB payloads, git-ignored, carried by the Actions cache
data/raw/                 your export archive, git-ignored

docs/                     the static site
docs/data/stats.json      the one file the site reads
DATA_CONTRACT.md          the shapes every script reads and writes
docs/SPEC.md              the build specification and the measurements behind it
```

## Credits

Film metadata comes from **TMDB**. This product uses the TMDB API but is not
endorsed or certified by TMDB.

This is a personal, non-commercial project. It is not affiliated with, endorsed
by, or connected to Letterboxd. All Letterboxd trademarks belong to their owner.
