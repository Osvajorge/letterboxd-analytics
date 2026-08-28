# Letterboxd Analytics: build specification

## Purpose

Rebuild the Letterboxd Pro stats panel as a self-hosted static site that refreshes
itself once a week, with no manual export step.

## Architecture

```
scripts/backfill.py                    run once, locally, by you
  |                                    reads the export ZIP -> data/history.json
  |
.github/workflows/update-stats.yml     weekly cron, needs no Letterboxd credential
  |
  +-- scripts/fetch_rss.py             public RSS feed -> new entries
  +-- scripts/merge_history.py         new entries -> data/history.json
  +-- scripts/enrich_tmdb.py           film metadata -> data/cache/tmdb.sqlite
  +-- scripts/fetch_lists.py           curated list membership -> data/cache/lists/
  +-- scripts/build_stats.py           everything above -> docs/data/stats.json
  |
docs/                                  static site, deployed by Cloudflare Pages
```

The site reads one file, `docs/data/stats.json`. Nothing is computed in the browser.

## Data sources

| Source | Gives | Auth |
|---|---|---|
| Export ZIP, once | full watch history, ratings, diary dates, rewatches, reviews, watchlist, lists | your session, locally, one time |
| Public RSS feed, weekly | recent entries: watched date, rating, rewatch, like, review, TMDB id | none |
| TMDB API | genres, countries, languages, runtime, cast, crew, studios, collections, posters | API key |
| Curated list sources | Top 500, IMDb 250, Oscars, Cannes, AFI, Sight and Sound, 1001 Films, TSPDT | none |

### Why not scrape the profile

Measured on 2026-08-28:

- `robots.txt` sets `User-agent: ClaudeBot` to `Disallow: /`. The site blocks AI
  scrapers across the whole domain.
- `/<user>/films/diary/` returns 403 behind a Cloudflare challenge. The diary is where
  watch dates live, and every streak, by-year, and heatmap stat depends on them.
- `robots.txt` disallows `/*/genre/*`, `/*/country/*`, `/*/decade/*`, and `/*/by/*`
  for every crawler. Those are the breakdown views the panel is built from.
- `/<user>/films/` does return 200, but pages 72 films at a time and carries no watch
  dates.

Scraping yields less than the export, costs more requests, breaks on any markup change,
and requires working around an active bot challenge.

### Why RSS carries the weekly load

Each `<item>` in `/<user>/rss/` already contains `letterboxd:watchedDate`,
`letterboxd:rewatch`, `letterboxd:memberRating`, `letterboxd:memberLike`, the review
body, and `tmdb:movieId`. That last field removes the title-and-year matching step
entirely, along with the roughly 5 percent of films it would fail on.

The feed holds a rolling window, measured at 67 items covering about three months, and
`?page=2` returns the same content. It cannot replace the one-time backfill, and it does
not need to.

## Stat modules

Ported from the reference panel:

- Header totals: films, hours, directors, countries, longest streak, multi-film days
- By year: films, ratings, diary entries
- Highest rated decades
- Genres, countries and languages, by most watched and by highest rated
- Cast, directors, crew and studios, by most watched
- Collections, complete and almost complete
- World map
- List progress

Not portable: **Themes and Nanogenres**. That is a proprietary Letterboxd
classification and a registered trademark. TMDB `keywords` give a rough equivalent,
which must carry a different name.

Added, because the export supports them and the reference panel does not show them:

- Rating bias: your average against the TMDB average, per film and overall
- Rating drift: whether you have grown harsher over the years
- Rewatch rate, and which directors you return to
- Watchlist age and conversion rate
- Viewing heatmap by day
- Director completeness: films seen against a director's full filmography
- Decade coverage gaps
- Runtime distribution and total days of life spent watching

## Weekly refresh

`update-stats.yml` runs on a `schedule` cron plus `workflow_dispatch`. It reads the
public RSS feed, merges new entries into `data/history.json` by `guid`, enriches
anything new through TMDB, and rebuilds `docs/data/stats.json`. It commits only when
the content changed.

The only repository secret is `TMDB_API_KEY`: read-only, scoped to TMDB, and revocable
in one click.

## Security

No Letterboxd credential is ever stored in this repository or in GitHub Actions.

The one-time backfill runs on your own machine. It reads a session cookie from a local
`.env` that stays git-ignored, uses it for a single request, and nothing about it leaves
your disk. That cookie is a full account credential, so it is worth stating plainly why
this design keeps it local: anyone holding it can act as the account owner, without a
password and without a second factor.

`data/raw/` and any downloaded archive are git-ignored. The weekly workflow never runs
on `pull_request`, so a fork cannot read even the TMDB key.

## Design

Tokens are in `docs/design-tokens.css`, read from Letterboxd's published stylesheet so
the panel matches the reference layout.

Their typefaces are commercially licensed and are not reused. The substitutes are
Inter for GraphikWeb, Source Serif 4 for the Tiempos faces, and JetBrains Mono for
PitchSansWeb.
