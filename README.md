# Letterboxd analytics

A self-hosted rebuild of the Letterboxd Pro stats panel.

It takes one member's watch history, works out the same breakdowns the paid panel
shows, and publishes them as a static site that refreshes itself once a week. The
account it is built for is `osvajorge`, with 827 watched films.

The site reads exactly one file, `docs/data/stats.json`. Nothing is computed in
the browser. `DATA_CONTRACT.md` defines that file, and it is the authority: change
a shape there before changing it in code.

Every figure in this README was measured on 2026-08-28, against the real export
and the real data files in this repository. A figure that describes this one
account says so.

## What it shows

Ported from the reference panel: header totals, films by year, highest rated
decades, genres, countries, languages, cast, directors, studios, collections, and
progress against 16 curated lists.

The reference panel draws its countries on a world map. This one ranks them in a
bar chart instead, from the same `world_map` figures. A map needs boundary data
this project does not carry, and a ranked chart answers the same question.

Added, because the data supports them and the reference panel does not show them:
rating bias against the TMDB average, rating drift over the years, rewatch rate,
watchlist age and conversion, a viewing heatmap, director completeness, decade
coverage gaps, runtime distribution, and the seventeen further modules listed
under `extras` in `DATA_CONTRACT.md`.

Not every module describes the same number of films, and the site says so. Marking
a film as watched on Letterboxd records no date; only a diary entry does. In this
account 827 films are watched and 284 of them carry a watch date, so the totals
describe the whole library while anything time-based describes about a third of
it. The `coverage` block in `docs/data/stats.json` carries those denominators, and
`DATA_CONTRACT.md` explains why the site must show them.

A module whose input is missing emits an empty array rather than disappearing, so
the site renders an empty section instead of breaking. That is the contract at
work, not a fault.

## Where the data comes from

| Source | What it gives | When | Credential |
|---|---|---|---|
| Export archive | full history, ratings, diary dates, rewatches, reviews, and the real date each watchlist film was added | once, by hand | your own session, on your own machine |
| Public RSS feed | recent entries: watched date, rating, rewatch, like, review, TMDB id | weekly | none |
| Public watchlist pages | which films are on the watchlist now, 1,044 of them | weekly | none |
| Public film pages | which TMDB record each film is, its id and its type | weekly, one read per new film | none |
| Curated list pages | membership of the 16 tracked lists, 5,882 films in total | weekly | none |
| TMDB API | genres, countries, languages, runtime, cast, crew, studios, collections | weekly | free API key |

Films are joined across every one of those sources by the Letterboxd **slug**, the
path segment in a film URL. Never by title. Titles repeat across remakes and
translations, and title matching fails on roughly one film in twenty. Slugs do
not.

The export is the one source that does not state the slug. It names every film by
a `boxd.it` short link instead, so `scripts/resolve_short_links.py` turns those
links into slugs before the backfill runs. Step 5 of the setup covers it.

### The watchlist has membership from one source and dates from another

The watchlist pages say which films are on the list. They never say when a film
was added. Only the export does.

So a film the weekly reader has not seen before is stamped with the day it first
appeared, and `added_date_estimated` marks that date as an estimate rather than a
measurement. A film already stored keeps whatever date it had, because restamping
every film each week would reset all of their ages to zero.

Until the export has been loaded, every watchlist date is a first-sighting
estimate and the age figures read as zero. That is the honest answer, not a bug.
`DATA_CONTRACT.md` sets out how the site must label them. Once the backfill in
step 6 has run, all 1,044 watchlist films carry the real date the export
recorded, and `extras.watchlist.estimated_date_share` is 0.

## Why an export plus RSS, and not a scrape of the profile

Two sources here are read from public HTML: the curated list pages and the
member's own watchlist. Both are plain paginated pages that need no sign-in.

The profile is a different matter. Scraping it would give less than this, not
more. Checked again on 2026-08-28:

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
entries. Read on 2026-08-28 it carried 54 items, 50 of them diary entries and the
rest lists the member published. It can keep a history current, but it cannot
build one. That is why the one-time backfill exists.

Reading HTML at all is the fragile part of this pipeline. A markup change breaks
a reader silently, because the page still answers 200 and simply matches nothing.
The two readers that replace stored data, the watchlist and the curated lists,
guard against exactly that, and both guards are described under
[How the weekly refresh works](#how-the-weekly-refresh-works).

The third reader, `resolve_tmdb_ids.py`, is guarded by its own scale rather than
by a size check. It reads one page per new film, so a week brings a handful, and
a page that answers without naming a TMDB id is recorded as a film TMDB has no
record for. If Letterboxd ever stops publishing `data-tmdb-id`, that reading is
wrong and it is written down as settled. What limits the damage is that it can
only be written for films added since the last run, and that
`data/tmdb-ids.json` is committed, so the answer for every film already resolved
is in git history and a wrong batch can be reverted.

## Setup

You need Python 3.11 or newer and git.

Run the steps in the order below. Steps 5 and 7 are the long ones: on this
account the whole setup takes about 21 minutes, and almost all of that is the
passes in those two steps that talk to a network.

1. **Install the dependencies.** Under a minute.

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

5. **Resolve the export's short links.** About 12 minutes on this export.

   ```sh
   python scripts/resolve_short_links.py data/raw/letterboxd-osvajorge.zip
   ```

   Run this before the backfill. Every film in a Letterboxd export is identified
   by a `boxd.it` short link and never by a slug, and a short link does not
   contain the slug. The whole pipeline joins films by slug, so the backfill has
   to invent one from the title and year until this step has run.

   It reads the member's own six film files, `diary.csv`, `watched.csv`,
   `ratings.csv`, `reviews.csv`, `watchlist.csv` and `likes/films.csv`, follows
   each distinct short link in them once, and stores the slug it points at in
   `data/short-links.json`, which is committed. It never opens `profile.csv`,
   which holds your email address and names no film.

   Those six files carry 3,418 links, 2,141 of them distinct, and the reader
   waits 0.15 seconds between requests. That is where the 12 minutes goes, and it
   is paid once: a short link never changes where it points, so the file is
   permanent. Later runs resolve only films added since, and re-running it on the
   same export sends no requests at all.

   All 827 watched films and all 1,044 watchlist films resolve to a film page.

   The committed `data/short-links.json` holds 2,258 entries rather than the 2,141
   the reader now finds, of which 2,233 name a film and 25 do not. The extra 117
   came from an earlier version that also read `comments.csv` and the liked-list
   and liked-review files, and those are where every one of the 25 non-film links
   came from: a comment or a liked list points at a member or a list, not a film.
   The surplus is harmless, since a resolved link is only ever looked up by id.

6. **Run the one-time backfill.** Under a second.

   ```sh
   python scripts/backfill.py data/raw/letterboxd-osvajorge.zip --force
   ```

   `--force` is needed because `data/history.json` is committed, so every clone
   already has one and the script refuses to replace a file it did not write.
   That guard is there so an accidental backfill cannot discard entries the
   weekly RSS run has added since. Pass the flag only when replacing the history
   is what you want.

   This writes `data/history.json`: for this account, 830 entries over 827
   distinct films, 287 of those entries carrying a watch date, plus the
   1,044-film watchlist with the real date the export recorded for each one. The
   run ends by reporting how many slugs it had to invent. With step 5 done that
   number is 0, and it should stay 0.

7. **Refresh the caches and build the site data.** About 8 minutes, almost all
   of it the two TMDB passes.

   ```sh
   python scripts/fetch_lists.py                    # no requests on a fresh clone
   python scripts/fetch_watchlist.py                # about 46 seconds
   python scripts/resolve_tmdb_ids.py               # no requests on a fresh clone
   python scripts/enrich_tmdb.py                    # about 5 minutes on a cold cache
   python scripts/enrich_people_and_collections.py  # about 2 minutes on a cold cache
   python scripts/build_stats.py                    # about a second
   ```

   `data/cache/lists/` is tracked in git rather than ignored, so a clone already
   carries all 16 caches, 5,882 films in total. `fetch_lists.py` therefore reads
   no pages on a fresh clone and only reports what those caches hold. Pass
   `--force` to actually re-read the list pages.

   `fetch_watchlist.py` walks 38 pages for a 1,044-film watchlist, waiting a
   second between them. It replaces membership with what the site shows today and
   keeps the added dates the backfill has just stored, so run it after step 6 and
   not before.

   `resolve_tmdb_ids.py` reads each film's own Letterboxd page for the TMDB id
   and type Letterboxd itself uses, and writes them to `data/tmdb-ids.json`. That
   file is committed and already answers for all 827 films, so on a fresh clone
   this step reads no pages. Run it anyway, and run it before the three steps
   under it, because all three read that file and none of them guesses: a film
   missing from it downloads no metadata at all. A film it has never seen costs one page read,
   and resolving all 827 from nothing takes about 7 minutes.

   `enrich_tmdb.py` then downloads one record per film, 810 of them, one for each
   film the id file types as `movie`. There is no search: which film a slug is was
   settled by the step above. Results go into `data/cache/tmdb.sqlite` as raw
   responses, so later runs only fetch what is new, and a new statistic never
   means downloading anything again.

   `enrich_people_and_collections.py` asks for the two facts no film record
   carries: how many films each collection holds, and how many films each
   director has made. On this account that is 200 collections and 160 directors,
   360 requests, cached permanently, so a later run asks for none of them again.
   Skip this pass and `collections` and `extras.director_completeness` are empty
   arrays, because neither has an honest denominator without it.

   38 of the 827 films end with no TMDB metadata, and they are counted rather
   than hidden: 17 because Letterboxd names no TMDB record for them at all, and
   21 because TMDB's film endpoint holds no record under the id Letterboxd names.
   Twenty of those 21 are episodes of an anthology or a miniseries that
   Letterboxd lists as films. `build_stats.py` prints the total as "input: films
   with no TMDB payload", and `coverage.films_with_tmdb_data` in
   `docs/data/stats.json`, 789, is the denominator the site shows beside anything
   built from film details.

That is the whole setup. `build_stats.py` ends by printing a summary table of
every module it filled and every module it left empty, and under it the counts of
what it read, so read that to check the run, then push. From here the weekly
workflow keeps everything current.

There is no test suite in this repository yet. `requirements.txt` installs
`pytest`, but nothing collects: running it reports "no tests ran" and exits 5.

## How the weekly refresh works

`.github/workflows/update-stats.yml` runs every Monday at 06:17 UTC, and on
demand from the Actions tab. Its steps, in the order the file lists them:

1. **Check out the repository.** This is what supplies `data/history.json`,
   `data/tmdb-ids.json` and the 16 list caches. `data/cache/tmdb.sqlite` is
   git-ignored, so it comes from step 9 instead.
2. **Set up Python 3.12.**
3. **Install dependencies** from `requirements.txt`.
4. **Confirm the TMDB key is present.** Failing here costs seconds. Failing
   later, inside the TMDB pass, costs the whole RSS and list run first and
   reports a bare 401.
5. **Read the public RSS feed and merge new entries into the history.** The feed
   is written to a temporary file, then merged into `data/history.json`. Two
   entries are the same watch when they name the same film on the same day, so
   re-reading the same rolling window changes nothing.
6. **Read the TMDB id of every film the merge just added**, from each film's
   own Letterboxd page, and add it to `data/tmdb-ids.json`. Steps 10 and 11 read
   that file and neither guesses, so a film missing from it downloads no runtime,
   no genres, no countries, no cast, no crew and no collection. No step fails
   when that happens, which is why this one is not optional. A week with no new
   films sends no requests.
7. **Read the public watchlist** and replace the watchlist in
   `data/history.json`, keeping the added date already stored for every film it
   has seen before. It refuses to write a read that came back short, and fails
   the run instead. About 46 seconds, and no credential, because those pages are
   public.
8. **Refresh membership of the curated lists.** All 16, and the refresh is
   forced, because the checkout supplies the cache files and a run that trusted
   them would read nothing at all. That is roughly 80 page reads, about two
   minutes.
9. **Restore the TMDB database saved by the last successful run** from the
   GitHub Actions cache. The job's own post step saves it again under a fresh
   key.
10. **Look up film metadata on TMDB**, fetching only the films that database does
    not already hold. A normal week is the 21 ids TMDB no longer holds, which are
    asked for again every time, plus one request per new film.
11. **Size the collections and the director filmographies**, which is what gives
    `collections` and `extras.director_completeness` a denominator. Everything
    already in the database is skipped, so a normal week costs no requests. A run
    that starts on a dropped cache pays for all 363 again.
12. **Build the site data file**, `docs/data/stats.json`.
13. **Check the files it is about to commit.** See
    [A step that exits zero can still be wrong](#a-step-that-exits-zero-can-still-be-wrong).
14. **Commit `docs/data/stats.json`, `data/history.json`, `data/tmdb-ids.json`
    and `data/cache/lists/`**, and only when at least one of them changed.

### Two steps can destroy data, and both refuse rather than guess

Steps 7 and 8 replace committed data wholesale, unattended, from HTML. That is
the hazard. Letterboxd answers 200 whether or not its pages still carry the
attributes the films are read from, so from inside the job a markup change is
indistinguishable from an account that emptied overnight. Both readers therefore
treat the size of a read as the only evidence available that it went right, and
both refuse to write a result that came back far too small.

Size is not the only thing that can go wrong, and it is the only thing a size
check can catch. Each film carries its slug and its display name as two
attributes of the same element, and both readers pair them from that one element.
A page where some film carries one attribute without the other yields a
full-sized result in which every following film wears the next film's title, so
both readers refuse a page whose attributes do not pair one for one, rather than
storing a result that no size check would ever question.

**The watchlist, step 7.** `fetch_watchlist.py` refuses a read that found no
films at all, that fell more than one page short of the total the watchlist pages
themselves state, or that fell more than 10 percent short of the watchlist
already stored. The stored watchlist is left exactly as it was and the script
exits non-zero.

One page is 28 films, and it is the whole tolerance against the stated total,
because that total is an exact number rather than an estimate. The only gap worth
allowing is films moving on or off the watchlist during the 46 seconds the walk
takes. A wider tolerance would accept a walk that stopped early: at 10 percent of
1,044 films it accepted a read of 940 and wrote it, and every one of the 104 films
that fell out lost its real added date.

An unattended run also refuses when nothing is stored yet. It has nothing to check
the size of the read against, the same hole `fetch_lists.py` refuses to fall into
under `--force`, and it is the one run that stamps every film with an estimated
date. Creating the first watchlist is a job for `scripts/backfill.py`, which reads
the real added dates out of the export.

The reason this matters more than a wrong number for one week: `data/history.json`
is the only place the export's real `added_date` values live. The pages never
state them. A film dropped from the stored watchlist and seen again next week
comes back stamped with next week's date and flagged as an estimate, and no later
run can undo that. Writing a broken read once loses the real dates permanently.

**The curated lists, step 8.** `fetch_lists.py` refuses any refresh that comes
back far smaller than the cache it would replace: nothing at all, or less than 90
percent of what is already stored. A refused list keeps its cached copy untouched
and the script exits non-zero. Under `--force` a list with no cached copy is
refused too, because there is nothing to check the size against and accepting the
result unmeasured is the outcome the gate exists to prevent.

A list that answers with an error is treated the same way: it keeps its cache and
it fails the run. A cache nobody can refresh is a set of progress figures that has
quietly stopped moving, which is worth a red run.

Both failures stop the job before the build and before the commit, so the week
goes red and the published site keeps last week's real numbers.

`--allow-first-watchlist` is the watchlist's second override, and it covers only
the case above: storing a read when nothing is stored yet. Use it when there is
no export to load and every watchlist age will be a guess. It must never appear
in the workflow either.

`--allow-shrink` overrides the comparison against what is already stored, for a
person who has looked at the pages and knows the watchlist or the lists really did
shrink that much. Both scripts take it, and it is deliberately absent from the
workflow, where nobody is watching to make that judgement. On the watchlist it
does not override the comparison against the total the pages state: when the page
and the parser disagree about the same page, the read simply did not finish. Nor
does it store an empty read on its own. Clearing the stored watchlist takes both
confirmations, the pages stating a watchlist of zero and a person passing the
flag, because either one alone is exactly what a markup change looks like.

### A step that exits zero can still be wrong

Every step in the job fails the whole job on a non-zero exit, and nothing in the
workflow overrides that: there is no `continue-on-error` and no `if:` condition
anywhere in it. So the commit at step 14 can only run after every step above it
succeeded. That is the protection against a script that crashes.

It is not protection against a script that writes a wrong file and exits zero,
which is the shape of failure this repository keeps producing. Step 13 is there
for that narrow case, and it checks three things that are cheap to state and hard
to break by accident:

- `docs/data/stats.json` parses as JSON and reports at least one film.
- It still reports TMDB metadata for at least nine tenths of the films the
  committed copy had it for, 789 today. This is the one that catches a hollow
  panel. The film count cannot: it is counted from the history, so it stays right
  at 827 while every runtime, genre, country, language, cast list, crew list,
  studio and collection has gone, which is exactly what a TMDB pass that was
  skipped or that failed halfway produces. TMDB retires a record now and then and
  the figure drifts down by a film or two, so the check allows a tenth and no
  more.
- `data/history.json` parses, and holds at least as many entries as the copy the
  checkout supplied. Within one run the history can only gain entries: the RSS
  merge adds, and the watchlist step writes a different array. A history that came
  out smaller was not edited, it was damaged.

The check runs before the commit, so a file that fails it is never pushed and the
published site keeps the last good copy. It deliberately does not restate the size
rules `fetch_watchlist.py` and `fetch_lists.py` already enforce. Two copies of one
rule drift apart, and those scripts are where that rule belongs.

### Caches and secrets

`data/cache/tmdb.sqlite` is git-ignored, so it travels from one run to the next
in the GitHub Actions cache rather than in a commit. GitHub drops a cache that
has not been read for seven days, which is about the gap between two scheduled
runs, so now and then a run finds no cache and downloads everything again: the
810 film ids, the 789 TMDB still holds plus the 21 it does not, plus 200 collection sizes and 160 director
filmographies, about 1,170 requests. That makes the run slow. It does not change
the result.

`data/tmdb-ids.json` is committed instead of cached, because a film's TMDB id
does not change and because the answer for a new film costs a Letterboxd page
read rather than a TMDB request. The weekly job stages it with the other data
files, so a film resolved this Monday is still resolved next Monday.

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
| The list step fails with "Refused N of 16 list refreshes" | Letterboxd changed the list markup, or blocked the run, so the pages came back far smaller than the caches | each named list kept its cached copy, and nothing was committed, because the run stops before the commit step. Lists that passed the check earlier in the same run were already written to disk: in the workflow that work dies with the runner, but on your own machine those cache files are changed, so check `git status` before re-running. Then open one of the named list pages and check whether its films still carry `data-item-slug` and `data-item-name` on the same element. If they do not, fix `FILM_ELEMENT_PATTERN`, `SLUG_PATTERN` and `NAME_PATTERN` in `scripts/fetch_lists.py`. If the lists really did shrink that much, re-run by hand with `--allow-shrink` |
| The list step fails and names a list with no cached copy | a list was added to `CURATED_LISTS` without committing its cache, or the checkout did not supply one | a forced run has nothing to check the size against, so it refuses rather than accept whatever the page returned. Run `python scripts/fetch_lists.py` once without `--force`, check the film count looks right for that list, and commit the new file under `data/cache/lists/`. `--allow-shrink` does not cover this case |
| The list step fails with "Could not read N lists" | those pages answered with an error, usually a list that was renamed, moved, or deleted, or a 403 | each one kept its cached copy. Fix the path in `scripts/lib/config.py` for a list that moved. For a 403, start the run again later |
| The watchlist step fails with "Refused to store this watchlist read" | Letterboxd changed the watchlist markup, or a page failed part way through, so the read came back empty or more than one page short of the total the pages state | nothing was written, so the stored watchlist keeps every added date it had. Open `https://letterboxd.com/<user>/watchlist/` and check whether its films still carry `data-item-slug` and `data-item-name` on the same element. If they do not, fix `FILM_ELEMENT_PATTERN`, `SLUG_PATTERN` and `NAME_PATTERN` in `scripts/fetch_watchlist.py`. If the pages look normal, run the script again, because a page that failed mid-walk gives the same short read. Only when you have checked and the watchlist really did lose that many films, re-run by hand with `--allow-shrink`. That flag does not cover a read short of the stated total, which is a walk that stopped early, and on a read of no films at all it changes nothing unless the pages themselves state a watchlist of zero |
| The watchlist step fails and says no watchlist is stored yet | `data/history.json` has an empty watchlist, so the read has nothing to be checked against, and an unattended run must not create the first one | run `python scripts/backfill.py <your export>.zip --force` first, which writes the watchlist with the real date each film was added. The flag is needed because `data/history.json` is committed, so the file already exists. Only if you have no export, run `python scripts/fetch_watchlist.py --allow-first-watchlist` by hand and check the count it prints against the total the pages state. Every added date it stores will be an estimate |
| Either HTML step fails saying the film markup changed and the attributes do not pair | a page carries more `data-item-slug` or `data-item-name` attributes than it has elements carrying both, so films can no longer be matched to their own titles | nothing was written or cached. This is not a short read and no override covers it: storing it would give films each other's titles at the right count, which nothing downstream would notice. Open the page named in the message and fix `FILM_ELEMENT_PATTERN`, `SLUG_PATTERN` and `NAME_PATTERN` in the script that named it |
| The watchlist step fails with "Could not read the watchlist pages" | a page answered with an error or timed out | nothing was written. Run `python scripts/fetch_watchlist.py` again; a 403 or a timeout usually clears |
| The check before the commit rejects the stats file or the history | a step wrote a file that is empty, unparseable, or smaller than the one the checkout supplied, and still exited zero | nothing was committed, so the published site keeps its last good copy. The step's message names the file and what it found. Look at the step that wrote that file, because the fault is there and not in the check |
| The check before the commit says the stats file lost TMDB metadata | the film metadata never arrived, so the panel would name every film and describe almost none of them. Usually a TMDB pass that answered nothing, or new films that were never resolved to a TMDB id | nothing was committed. Read the logs of the id resolver and the two TMDB steps, in that order, because the earliest one that went quiet is the cause. Start the run again once TMDB answers |
| The TMDB step fetches every film, not just the new ones | the Actions cache was dropped, which GitHub does after seven days without a read | nothing. The run rebuilds `data/cache/tmdb.sqlite` and saves it again. Only the run time changes |
| The commit step says nothing to commit | no new films, no watchlist changes and no list changes that week | nothing. This is the normal case |
| New films are missing from the site | more than about 50 entries were logged since the last successful run, so the rolling window moved past them | download a fresh export and re-run the backfill. Then find out why the weekly run had been failing |
| The site renders an empty section | a module produced no data | expected behaviour. Per the data contract, an empty module emits an empty array rather than disappearing, so the page never breaks |
| Every watchlist age reads as zero | the export has not been loaded, so every added date is a first-sighting estimate | expected until step 6 of the setup has been run. See "The watchlist has membership from one source and dates from another" |
| `backfill.py` says `data/history.json` already exists | that file is committed, so every clone has one | the guard is there to stop an accidental backfill discarding entries the weekly run has added. Pass `--force` when replacing the history is what you want |

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

The scripts, in the order they run:

```
scripts/resolve_short_links.py  export links     -> data/short-links.json  (once, before the backfill)
scripts/backfill.py             export archive   -> data/history.json      (once, on your machine)
scripts/fetch_rss.py            public RSS feed  -> recent entries         (weekly)
scripts/merge_history.py        recent entries   -> data/history.json      (weekly, one entry per film and watch date)
scripts/resolve_tmdb_ids.py     film pages       -> data/tmdb-ids.json     (weekly, one page read per new film)
scripts/fetch_watchlist.py      watchlist pages  -> data/history.json      (weekly, refuses a bad read)
scripts/fetch_lists.py          list pages       -> data/cache/lists/      (weekly, refuses a bad read)
scripts/enrich_tmdb.py          TMDB API         -> data/cache/tmdb.sqlite (weekly, one record per film)
scripts/enrich_people_and_collections.py         -> data/cache/tmdb.sqlite (weekly, collection sizes and director filmographies)
scripts/build_stats.py          all of the above -> docs/data/stats.json
scripts/lib/config.py           paths, the user, and the 16 curated list URLs
```

The data they read and write:

```
data/history.json         the watch history and the watchlist, committed
data/tmdb-ids.json        film slug -> TMDB id and type, read from each film's own page, committed
data/short-links.json     boxd.it link -> film slug, committed, written once
data/manual-matches.json  film slug -> TMDB id, checked by hand, outranks data/tmdb-ids.json, empty here
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
