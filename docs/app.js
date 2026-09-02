/**
 * Renders the stats panel from docs/data/stats.json.
 *
 * Read this file in four passes:
 *   1. Formatting and guards, which keep a missing figure from reaching the page.
 *   2. Chart drawing, which turns rows of numbers into inline SVG.
 *   3. Panel renderers, the sections that mirror the reference stats panel.
 *   4. Extras renderers, the sections the reference panel does not have.
 *
 * The page computes nothing. Every figure is read from the file the pipeline
 * writes, and the shapes it may hold are fixed by DATA_CONTRACT.md.
 *
 * Every renderer treats a missing key and an empty value the same way, because
 * a stats file written before a module existed simply has no key for it.
 */

const STATS_URL = "./data/stats.json";
const SVG_NAMESPACE = "http://www.w3.org/2000/svg";
const TMDB_PROFILE_BASE = "https://image.tmdb.org/t/p/w185";
const LETTERBOXD_BASE = "https://letterboxd.com/";

/**
 * Where Letterboxd files each kind of name this page prints.
 *
 * A film link is exact. Its slug is the path segment of the film's own
 * Letterboxd page, and it is what this whole pipeline joins on, so a film link
 * either works or the pipeline is joining on the wrong thing.
 *
 * A person or a studio link is a GUESS. Letterboxd builds those slugs from the
 * name and nameSlug below follows the same convention, but the site also
 * disambiguates two people who share a name and transliterates in its own way,
 * so a link here can land on a page that does not exist. Nothing depends on one
 * of them resolving: a wrong guess costs the reader one back button.
 *
 * The film, director, actor and studio paths were checked against live pages.
 * The four crew paths follow the same shape and were not checked, because this
 * project reads no Letterboxd page. Note that Letterboxd files a
 * cinematographer under "cinematography", the craft rather than the job.
 */
const LETTERBOXD_PATHS = {
  film: "film",
  director: "director",
  actor: "actor",
  studio: "studio",
  composer: "composer",
  cinematographer: "cinematography",
  editor: "editor",
  writer: "writer",
};

/** Rows beyond this are cut from a long ranking, with a note saying so. */
const MAXIMUM_BAR_ROWS = 20;

/** Cards beyond this are cut from a people grid, with a note saying so. */
const MAXIMUM_PEOPLE_CARDS = 24;

/** Rows beyond this are cut from a ranked text list, with a note saying so. */
const MAXIMUM_LIST_ROWS = 10;

/**
 * A year averaged over fewer films than this is named as thin in the caption.
 *
 * Not a filter. The year is still drawn, because it happened. But an average of
 * one film plotted on the same line as an average of a hundred and sixteen is
 * the steepest movement on the chart and means the least, so the caption says
 * what it rests on.
 */
const THIN_YEAR_FILMS = 10;

/**
 * Rows a long ranking shows before the rest go behind a disclosure.
 *
 * Twelve rankings of twenty identical rows is what made this page thirty
 * thousand pixels tall on a phone. Nothing is removed: the disclosure says how
 * many rows are behind it, and the note under the list still names the total the
 * pipeline measured, which is the number that matters and is usually far larger
 * than either.
 */
const RANKED_ROWS_BEFORE_DISCLOSURE = 8;

/** The same, for the four crew lists that sit side by side in one block. */
const CREW_ROWS_BEFORE_DISCLOSURE = 5;

/** Calendar years beyond this are cut from the heatmap, with a note saying so. */
const MAXIMUM_HEATMAP_YEARS = 6;

/** Words beyond this are cut from the title word list, with a note saying so. */
const MAXIMUM_TITLE_WORDS = 40;

/** A chart narrower than this cannot fit a label and a bar side by side. */
const MINIMUM_CHART_WIDTH = 240;

/** Shown wherever a figure is missing, so no cell ever reads "undefined". */
const MISSING_VALUE = "-";

/**
 * What every empty module is waiting for, so one fix is named one way.
 *
 * Each fix is taken from WHAT_AN_EMPTY_MODULE_IS_WAITING_FOR in
 * scripts/build_stats.py, which is where the pipeline records what actually
 * fills each module. Two were wrong until an earlier pass: collections and
 * director completeness sent the reader to scripts/enrich_tmdb.py, which writes
 * films, credits and lookups and neither of the two tables those modules read.
 * Dropping both tables and running that script recreated neither, and both
 * modules stayed at zero rows.
 *
 * Reason and fix are kept apart rather than run into one paragraph, because two
 * readers arrive at an empty module. A visitor needs the first sentence and
 * nothing else; whoever runs the pipeline needs the command. Splitting them lets
 * showEmptyState set the command as a command, so a page of empty modules reads
 * as three kinds of missing input rather than as thirty-eight walls of prose.
 * Every empty state still names its own step, on its own module.
 */
const FIX_TMDB = "Run scripts/enrich_tmdb.py, then rebuild the stats with scripts/build_stats.py.";
const FIX_PEOPLE_AND_COLLECTIONS =
  "Run scripts/enrich_tmdb.py, then scripts/enrich_people_and_collections.py, then rebuild the stats.";
const FIX_EXPORT = "Run scripts/backfill.py on your Letterboxd export, then rebuild the stats.";
const FIX_HISTORY =
  "Run scripts/backfill.py once on your Letterboxd export, then scripts/fetch_rss.py and " +
  "scripts/merge_history.py, then rebuild the stats.";

const NEEDS_TMDB = { reason: "It needs film details cached from TMDB.", fix: FIX_TMDB };
const NEEDS_CREDITS = { reason: "It needs film credits cached from TMDB.", fix: FIX_TMDB };
const NEEDS_EXPORT = {
  reason: "It needs the one-time Letterboxd export, which the RSS feed cannot supply.",
  fix: FIX_EXPORT,
};
const NEEDS_HISTORY = { reason: "It needs a watch history.", fix: FIX_HISTORY };
const NEEDS_WATCH_DATES = { reason: "It needs entries that carry a watch date.", fix: FIX_HISTORY };

/**
 * The locale for every number and date the page prints.
 *
 * It is pinned rather than read from the browser. Captions are written in
 * English and put figures inside sentences, and a Spanish reader would see
 * "36.000 votes" in an English line and read it as thirty-six.
 */
const DISPLAY_LOCALE = "en-GB";

const integerFormatter = new Intl.NumberFormat(DISPLAY_LOCALE);

/* =============================================================== Formatting */

/**
 * Returns the value as a finite number, or null when it cannot be one.
 *
 * Everything drawn on this page goes through here first. A null from the
 * pipeline, a string, or a NaN all collapse to the same "no figure" case.
 */
function toNumber(value) {
  if (typeof value === "number") {
    return Number.isFinite(value) ? value : null;
  }
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

/** Returns the value when it is a non-empty array, otherwise an empty array. */
function toArray(value) {
  return Array.isArray(value) ? value : [];
}

/** Returns the value when it is a plain object, otherwise null. */
function toObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value : null;
}

/** Returns trimmed text, or null when there is nothing readable to show. */
function toText(value) {
  if (typeof value === "string" && value.trim() !== "") {
    return value.trim();
  }
  if (typeof value === "number" && Number.isFinite(value)) {
    return String(value);
  }
  return null;
}

/** Formats a whole number with thousands separators, or the missing marker. */
function formatCount(value) {
  const number = toNumber(value);
  return number === null ? MISSING_VALUE : integerFormatter.format(Math.round(number));
}

/**
 * Formats a calendar year or a decade, or the missing marker.
 *
 * Years are never grouped: a thousands separator would turn 2008 into 2,008,
 * which reads as a quantity rather than as a year.
 */
function formatYear(value) {
  const number = toNumber(value);
  return number === null ? MISSING_VALUE : String(Math.round(number));
}

/** Formats a decimal to a fixed number of places, or the missing marker. */
function formatDecimal(value, places = 1) {
  const number = toNumber(value);
  return number === null ? MISSING_VALUE : number.toFixed(places);
}

/** Formats a decimal with an explicit plus or minus, or the missing marker. */
function formatSignedDecimal(value, places = 2) {
  const number = toNumber(value);
  if (number === null) {
    return MISSING_VALUE;
  }
  return `${number > 0 ? "+" : ""}${number.toFixed(places)}`;
}

/** Formats a fraction from 0 to 1 as a percentage, or the missing marker. */
function formatPercentage(fraction, places = 0) {
  const number = toNumber(fraction);
  return number === null ? MISSING_VALUE : `${(number * 100).toFixed(places)}%`;
}

/**
 * Formats a fraction as a percentage figure, without rounding it away.
 *
 * Plain rounding prints a watchlist conversion of one film in a thousand as
 * "0%", which is a claim that none of them converted. Below the rounding floor
 * the figure says so instead.
 */
function formatShareFigure(fraction) {
  const number = toNumber(fraction);
  if (number === null) {
    return MISSING_VALUE;
  }
  if (number > 0 && number < 0.005) {
    return "<1%";
  }
  if (number < 1 && number > 0.995) {
    return ">99%";
  }
  return formatPercentage(number);
}

/** Formats a whole number as an English ordinal, such as 11th. */
function formatOrdinal(value) {
  const number = toNumber(value);
  if (number === null) {
    return MISSING_VALUE;
  }
  const whole = Math.round(number);
  const lastTwo = Math.abs(whole) % 100;
  const lastOne = Math.abs(whole) % 10;
  // Eleventh through thirteenth break the pattern the last digit would predict.
  const suffix =
    lastTwo >= 11 && lastTwo <= 13
      ? "th"
      : lastOne === 1
        ? "st"
        : lastOne === 2
          ? "nd"
          : lastOne === 3
            ? "rd"
            : "th";
  return `${integerFormatter.format(whole)}${suffix}`;
}

/** Formats a count of things with the right singular or plural noun. */
function formatQuantity(value, singular, plural) {
  const number = toNumber(value);
  if (number === null) {
    return MISSING_VALUE;
  }
  return `${formatCount(number)} ${Math.abs(number) === 1 ? singular : plural}`;
}

/** Formats a "YYYY-MM-DD" date for reading, falling back to the raw text. */
function formatDate(value) {
  const text = toText(value);
  if (text === null) {
    return MISSING_VALUE;
  }
  const parsed = parseIsoDate(text);
  if (parsed === null) {
    return text;
  }
  return parsed.toLocaleDateString(DISPLAY_LOCALE, {
    day: "numeric",
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  });
}

/**
 * Parses "YYYY-MM-DD" into a UTC date, or null when the text is not a date.
 *
 * Parsing is done by hand rather than by the Date constructor so that a viewer
 * west of UTC does not see every watch date shift back by a day.
 */
function parseIsoDate(text) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(text));
  if (match === null) {
    return null;
  }
  const [, year, month, day] = match;
  const date = new Date(Date.UTC(Number(year), Number(month) - 1, Number(day)));
  return Number.isNaN(date.getTime()) ? null : date;
}

/** Turns a two-letter country code into its flag character, or empty text. */
function flagForCountryCode(code) {
  const text = toText(code);
  if (text === null || !/^[A-Za-z]{2}$/.test(text)) {
    return "";
  }
  const offset = 0x1f1e6 - "A".charCodeAt(0);
  return String.fromCodePoint(
    ...[...text.toUpperCase()].map((letter) => letter.charCodeAt(0) + offset),
  );
}

/** Returns the first one or two initials of a name, for a photoless avatar. */
function initialsFor(name) {
  const words = String(name ?? "")
    .split(/\s+/)
    .filter((word) => word.length > 0);
  return words
    .slice(0, 2)
    .map((word) => word[0].toUpperCase())
    .join("");
}

/** Returns the Letterboxd page for a film slug, or null when there is no slug. */
function filmUrl(slug) {
  const text = toText(slug);
  return text === null
    ? null
    : `${LETTERBOXD_BASE}${LETTERBOXD_PATHS.film}/${encodeURIComponent(text)}/`;
}

/**
 * Builds the slug Letterboxd most likely files a person or a studio under.
 *
 * The convention is the name lowercased with its words joined by hyphens.
 * Accents are folded and apostrophes dropped because Letterboxd's own slugs
 * carry neither, so keeping them would miss every accented name rather than
 * only the few the convention really misses. It stays a convention: see
 * LETTERBOXD_PATHS for what that costs when it is wrong.
 */
function nameSlug(name) {
  const text = toText(name);
  if (text === null) {
    return null;
  }
  const slug = text
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/['\u2019\u02bc`]/g, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return slug === "" ? null : slug;
}

/**
 * Returns the Letterboxd page for a person or a studio, or null when there is
 * no page to guess at: an unknown role, or a name that leaves no slug behind.
 */
function personUrl(role, name) {
  const path = LETTERBOXD_PATHS[role];
  const slug = nameSlug(name);
  return path === undefined || slug === null ? null : `${LETTERBOXD_BASE}${path}/${slug}/`;
}

/* ==================================================== Small DOM conveniences */

/** Finds an element by id, or null when the page does not carry it. */
function elementById(id) {
  return document.getElementById(id);
}

/** Builds an element with a class and, when given, text content. */
function build(tagName, className, text) {
  const node = document.createElement(tagName);
  if (className) {
    node.className = className;
  }
  if (text !== undefined && text !== null) {
    node.textContent = String(text);
  }
  return node;
}

/**
 * Replaces a container's contents with an explanation of why it is bare.
 *
 * `waiting` is either a { reason, fix } pair from the block above, or a bare
 * reason for a module whose absence is a real answer rather than a missing
 * input. The fix is printed on its own line, in the mono face the page already
 * uses for anything a reader would type, so it reads as a command and not as
 * another sentence of apology.
 */
function showEmptyState(container, waiting, extraFix) {
  if (container === null) {
    return;
  }

  const reason = typeof waiting === "string" ? waiting : waiting.reason;
  const fix = typeof waiting === "string" ? extraFix : (waiting.fix ?? extraFix);

  const box = build("div", "empty");
  box.append(build("p", "empty__reason", reason));
  if (fix) {
    box.append(build("p", "empty__fix", fix));
  }
  container.replaceChildren(box);
}

/** Joins a module-specific opening line to a shared reason. */
function waitingFor(opening, waiting) {
  return { reason: `${opening} ${waiting.reason}`, fix: waiting.fix };
}

/** Appends a small grey note, used when a chart shows only the top rows. */
function appendNote(container, message) {
  container.append(build("p", "note", message));
}

/**
 * How long each ranked module was before the pipeline cut it, read once.
 *
 * Fourteen renderers need one number each, for one sentence each, so it is read
 * at the top of render() rather than threaded through every signature. It is
 * written once per page load. An older stats file carries none of it, and every
 * reader treats a missing entry as "no denominator to print", never as an error.
 */
let rowTotals = {};

/**
 * The member's own average rating, read once, for the rating axis reference.
 *
 * Every ranking of averages on this page draws a faint line at this value, so a
 * row can be read as "above or below what you usually give" rather than only as
 * a position between 0.5 and 5. It is stats.extras.rating_bias.member_average,
 * which is averaged over the films TMDB also scores, so the key that names it
 * says which films it covers. A file that carries no such average draws no line
 * and no key, and every row still shows its own figure in words.
 */
let libraryAverageRating = null;

/**
 * Appends the note that says a ranking was cut, and how much was cut off.
 *
 * The denominator comes from stats.row_totals and never from the array. The
 * array in the file was already cut by scripts/build_stats.py, so its length is
 * a cap: printing it stated a display limit as a measurement, and the page read
 * "Showing the top 24 of 50 directors" directly under a tile reading "530
 * Directors".
 *
 * `rowsInFile` still matters, because a file carrying no total for this module
 * can only say how many rows are on show. Naming no denominator is the honest
 * answer there. Naming the array's length is not.
 */
function appendTruncationNote(container, shownCount, rowsInFile, moduleName, plural) {
  const total = toNumber(rowTotals[moduleName]);

  if (total !== null) {
    if (total > shownCount) {
      appendNote(
        container,
        `Showing the top ${formatCount(shownCount)} of ${formatCount(total)} ${plural}.`,
      );
    }
    return;
  }

  if (rowsInFile > shownCount) {
    appendNote(container, `Showing the top ${formatCount(shownCount)} ${plural}.`);
  }
}

/**
 * Builds one figure tile.
 *
 * `unresolved` dims the figure. Both grids used to mix two ways of showing the
 * same state: seven tiles printed a bold white dash with their ordinary note
 * under it, and two printed a bold white dash with "not counted yet". A figure
 * nobody has worked out should not be set in the same weight as one that has
 * been, so the dash is dimmed wherever it appears and the note carries the
 * reason.
 */
function buildTile({ value, label, note, unresolved }) {
  const missing = unresolved === true || value === MISSING_VALUE;
  const node = build("div", missing ? "tile tile--unresolved" : "tile");
  node.append(build("p", "tile__value", value));
  node.append(build("p", "tile__label", label));
  if (note) {
    node.append(build("p", missing ? "tile__note tile__note--reason" : "tile__note", note));
  }
  return node;
}

/** Builds a heading of the given level, for blocks the script creates itself. */
function buildSubheading(level, text) {
  return build(`h${level}`, "pair__heading", text);
}

/* ============================================================ SVG scaffolding */

/** Builds an SVG element with the given attributes, skipping empty ones. */
function svgNode(name, attributes = {}) {
  const node = document.createElementNS(SVG_NAMESPACE, name);
  for (const [key, value] of Object.entries(attributes)) {
    if (value === null || value === undefined) {
      continue;
    }
    node.setAttribute(key, String(value));
  }
  return node;
}

/**
 * Builds the outer SVG for a chart.
 *
 * The SVG is hidden from assistive technology on purpose: every chart is
 * published beside a caption and a data table that carry the same figures in
 * text, which reads far better than a tree of rectangles.
 */
function svgCanvas(width, height) {
  return svgNode("svg", {
    width,
    height,
    viewBox: `0 0 ${width} ${height}`,
    "aria-hidden": "true",
    focusable: "false",
  });
}

/** Builds an SVG text node. */
function svgText(text, attributes) {
  const node = svgNode("text", attributes);
  node.textContent = String(text);
  return node;
}

/** Adds a hover tooltip to a shape, which browsers show as a native title. */
function withTooltip(node, text) {
  const title = svgNode("title");
  title.textContent = String(text);
  node.append(title);
  return node;
}

/**
 * Shortens text to fit a pixel width, adding an ellipsis when it is cut.
 *
 * SVG has no text overflow, so the cut has to be made before drawing. The
 * factor below is the average glyph width of Inter as a share of its font size.
 */
const AVERAGE_GLYPH_WIDTH_RATIO = 0.56;

function truncateToWidth(text, availableWidth, fontSize) {
  const source = String(text);
  const maximumCharacters = Math.floor(availableWidth / (fontSize * AVERAGE_GLYPH_WIDTH_RATIO));
  if (maximumCharacters >= source.length) {
    return source;
  }
  if (maximumCharacters <= 1) {
    return "…";
  }
  return `${source.slice(0, maximumCharacters - 1).trimEnd()}…`;
}

/* ================================================ Redrawing charts on resize */

const chartDrawFunctions = new WeakMap();
const lastDrawnWidths = new WeakMap();

/**
 * Redraws a chart at its container's current width.
 *
 * Charts are drawn at one to one pixel scale rather than stretched from a fixed
 * viewBox, because a stretched viewBox shrinks the labels along with the bars
 * and makes them unreadable on a phone.
 */
function drawChart(plot) {
  const draw = chartDrawFunctions.get(plot);
  if (draw === undefined) {
    return;
  }
  const width = Math.max(MINIMUM_CHART_WIDTH, Math.round(plot.clientWidth));
  if (lastDrawnWidths.get(plot) === width) {
    return;
  }
  lastDrawnWidths.set(plot, width);
  plot.replaceChildren(draw(width));
}

const chartResizeObserver =
  typeof ResizeObserver === "function"
    ? new ResizeObserver((entries) => {
        for (const entry of entries) {
          drawChart(entry.target);
        }
      })
    : null;

/** Draws a chart now and keeps it in step with its container's width. */
function mountChart(plot, draw) {
  chartDrawFunctions.set(plot, draw);
  drawChart(plot);
  if (chartResizeObserver !== null) {
    chartResizeObserver.observe(plot);
  }
}

/**
 * Adds a chart with its caption and its data table to a container.
 *
 * Parameters worth noting:
 *   caption  one sentence naming what the chart shows, visible under it.
 *   draw     a function taking a pixel width and returning an SVG element.
 *   table    the same figures as text, hidden from view but read aloud.
 */
function appendChart(container, { caption, draw, table }) {
  const figure = build("figure", "chart");
  const plot = build("div", "chart__plot");
  figure.append(plot);

  if (caption) {
    figure.append(build("figcaption", "chart__caption", caption));
  }
  if (table) {
    figure.append(table);
  }

  container.append(figure);
  mountChart(plot, draw);
}

/** Empties a container, then draws a single chart into it. */
function replaceWithChart(container, options) {
  container.replaceChildren();
  appendChart(container, options);
}

/**
 * Builds a hidden table holding the figures a chart draws.
 *
 * The table sits inside a clipping wrapper rather than carrying the hiding
 * class itself. A table sizes to its content whatever width is set on it, so a
 * table hidden directly kept its full width and widened the whole document.
 */
function buildDataTable(caption, columnNames, rows) {
  const wrapper = build("div", "visually-hidden");
  const table = document.createElement("table");
  table.append(build("caption", null, caption));

  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const name of columnNames) {
    const cell = build("th", null, name);
    cell.setAttribute("scope", "col");
    headRow.append(cell);
  }
  head.append(headRow);
  table.append(head);

  const body = document.createElement("tbody");
  for (const row of rows) {
    const bodyRow = document.createElement("tr");
    row.forEach((value, index) => {
      const cell = build(index === 0 ? "th" : "td", null, value);
      if (index === 0) {
        cell.setAttribute("scope", "row");
      }
      bodyRow.append(cell);
    });
    body.append(bodyRow);
  }
  table.append(body);

  wrapper.append(table);
  return wrapper;
}

/* ==================================================================== Charts */

/* The two SVG ranked-bar charts that used to live here are gone.
 *
 * Both drew their row labels as SVG text, which cannot wrap, cannot be a link,
 * and has to be cut to a measured column. At 360px that cut 41 labels, and two
 * rows of List progress both came out as "They Shoot Pi\u2026" with different
 * numbers beside them. The value column was a hard-coded 76px in the progress
 * chart, and "154 / 1,000" measures about 79px, so the figure ran into its own
 * bar at every width.
 *
 * renderStudios had already moved to buildRankedList for exactly that reason.
 * The rest now follow it, and the rows are chosen by what the number means:
 *
 *   a count of something        buildRankedList with a proportion bar
 *   a share of a known whole    buildRankedList with a progress bar
 *   an average on 0.5 to 5      buildRankedList with a dot on the rating axis
 *
 * The third is the one that changes what a reader can see. Every rating in this
 * library falls between about 2.0 and 4.8, so a bar drawn from zero to five is
 * always about three quarters full and the ranking reads as texture. A dot on a
 * shared axis, with the library average marked on it, puts the difference where
 * the eye can measure it without giving up the honest fixed scale.
 */

const COLUMN_CHART_HEIGHT = 200;
const COLUMN_CHART_SHORT_HEIGHT = 150;
const COLUMN_CHART_TOP = 12;
const COLUMN_CHART_AXIS_HEIGHT = 24;

/** The narrowest the vertical axis gutter is ever drawn. */
const COLUMN_CHART_MINIMUM_GUTTER = 30;

/** Width of one digit of the mono face the axis labels are set in, at 11px. */
const AXIS_DIGIT_WIDTH = 6.7;

/** A column narrower than this cannot carry its own figure above it. */
const COLUMN_VALUE_MINIMUM_BAR = 20;

/**
 * Rounds a maximum up to a value that divides into readable axis steps.
 *
 * A y-axis reading 0, 39, 78, 117 is arithmetic rather than a scale. This walks
 * up 1, 2, 2.5, 5, 10 within the value's own decade, so the steps land on the
 * numbers a reader would have chosen.
 */
function niceCeiling(value) {
  if (!(value > 0)) {
    return 1;
  }
  const magnitude = 10 ** Math.floor(Math.log10(value));
  for (const step of [1, 2, 2.5, 5, 10]) {
    if (value <= step * magnitude) {
      return step * magnitude;
    }
  }
  return 10 * magnitude;
}

/**
 * Draws vertical columns, one group per category.
 *
 * Used for the year histogram, the ratings histogram, and the small extras
 * charts. A second series draws a narrower bar beside the first in the same
 * slot.
 *
 * The chart prints its own numbers. It used to draw a baseline, the category
 * ticks, and nothing else: the only route to a figure was a native SVG tooltip,
 * which never fires on a touch screen, or the hidden data table, which only a
 * screen reader reads. That left a sighted reader on a phone with no figures at
 * all from eight of the charts on a page whose whole subject is figures. So the
 * vertical axis, its gridlines and its labels are drawn here the way
 * ratingLineChart already drew them, and where a slot is wide enough each column
 * is labelled with its own value as well.
 *
 * Parameters worth noting:
 *   categories       objects of { label, shortLabel?, values, classNames?,
 *                    valueText? } where values holds one number per series.
 *   seriesClassNames one class per series, in the same order as values.
 *   height           the drawing height in pixels, shorter for small charts.
 *   valueFormatter   turns one column's number into the text drawn above it.
 */
function columnChart(
  width,
  categories,
  {
    seriesClassNames = ["chart__bar"],
    height = COLUMN_CHART_HEIGHT,
    valueFormatter = formatCount,
  } = {},
) {
  if (categories.length === 0) {
    // Dividing the width by zero categories would put NaN into every attribute.
    return svgCanvas(width, 1);
  }

  const canvas = svgCanvas(width, height);

  const plotHeight = height - COLUMN_CHART_TOP - COLUMN_CHART_AXIS_HEIGHT;
  const baselineY = COLUMN_CHART_TOP + plotHeight;
  const sidePadding = 6;
  const seriesCount = seriesClassNames.length;

  const highest = categories.reduce(
    (peak, category) =>
      category.values.reduce((inner, value) => Math.max(inner, toNumber(value) ?? 0), peak),
    0,
  );
  const axisTop = niceCeiling(highest);
  const scale = axisTop > 0 ? plotHeight / axisTop : 0;

  // Three steps and zero. More than that crowds a 150px plot; fewer stops the
  // reader estimating a bar that carries no label of its own.
  const axisSteps = [0, 1, 2, 3].map((step) => valueFormatter((axisTop * step) / 3));

  // The gutter is sized from the labels it has to hold. A constant 34px was fine
  // for "200" and clipped "50,000" down to ",000" on the release-recency chart,
  // which is a wrong figure rather than a missing one.
  const widestAxisLabel = axisSteps.reduce((widest, label) => Math.max(widest, label.length), 1);
  const leftGutter = Math.max(
    COLUMN_CHART_MINIMUM_GUTTER,
    Math.min(width * 0.3, widestAxisLabel * AXIS_DIGIT_WIDTH + 10),
  );
  const innerWidth = Math.max(20, width - leftGutter - sidePadding);
  const slotWidth = innerWidth / categories.length;

  // No absolute cap on the group width. A cap of 40px per series left the
  // four-year chart covering a third of the plot at 1280px and looking empty.
  const groupWidth = slotWidth * 0.74;
  const barWidth = Math.max(2, groupWidth / seriesCount);

  axisSteps.forEach((label, step) => {
    const value = (axisTop * step) / 3;
    const y = baselineY - value * scale;
    canvas.append(
      svgNode("line", {
        class: step === 0 ? "chart__axis" : "chart__gridline",
        x1: leftGutter - 4,
        y1: y + 0.5,
        x2: width - sidePadding,
        y2: y + 0.5,
      }),
    );
    canvas.append(
      svgText(label, {
        class: "chart__tick",
        x: leftGutter - 8,
        y: y + 4,
        "text-anchor": "end",
      }),
    );
  });

  // Every category is named when its own label fits. When it does not, a short
  // label is used if the caller gave one, and only then are labels stepped.
  const labelFor = (category, wide) =>
    String(wide ? category.label : (category.shortLabel ?? category.label));
  const longestLabel = categories.reduce(
    (longest, category) => Math.max(longest, String(category.label).length),
    1,
  );
  const wideLabels = longestLabel * 7 + 6 <= slotWidth;
  const longestShort = categories.reduce(
    (longest, category) => Math.max(longest, labelFor(category, false).length),
    1,
  );
  const labelStep = wideLabels
    ? 1
    : Math.max(1, Math.ceil((longestShort * 7 + 6) / Math.max(1, slotWidth)));

  const showColumnValues = barWidth >= COLUMN_VALUE_MINIMUM_BAR;

  categories.forEach((category, index) => {
    const slotStartX = leftGutter + slotWidth * index;
    const groupStartX = slotStartX + (slotWidth - groupWidth) / 2;

    category.values.forEach((rawValue, seriesIndex) => {
      const value = Math.max(0, toNumber(rawValue) ?? 0);
      const barHeight = value > 0 ? Math.max(2, value * scale) : 0;
      const bar = svgNode("rect", {
        class: category.classNames?.[seriesIndex] ?? seriesClassNames[seriesIndex] ?? "chart__bar",
        x: groupStartX + barWidth * seriesIndex,
        y: baselineY - barHeight,
        width: Math.max(1, barWidth - 1),
        height: barHeight,
        rx: 1.5,
      });
      canvas.append(
        withTooltip(bar, `${category.label}: ${category.valueText ?? valueFormatter(value)}`),
      );

      const drawnValue =
        seriesCount > 1
          ? valueFormatter(value)
          : (category.shortValueText ?? category.valueText ?? valueFormatter(value));

      // The figure goes above the column, and only where it will not collide
      // with the column beside it or run off the top of the plot.
      if (showColumnValues && value > 0) {
        const labelY = baselineY - barHeight - 5;
        if (labelY > COLUMN_CHART_TOP - 2) {
          canvas.append(
            svgText(drawnValue, {
              class: "chart__value",
              // Centred on the bar rather than on the slot, so a grouped chart
              // draws one figure per bar instead of two on top of each other.
              x: groupStartX + barWidth * seriesIndex + barWidth / 2,
              y: labelY,
              "text-anchor": "middle",
            }),
          );
        }
      }
    });

    if (index % labelStep === 0) {
      canvas.append(
        svgText(labelFor(category, wideLabels), {
          class: "chart__tick",
          x: slotStartX + slotWidth / 2,
          y: baselineY + 15,
          "text-anchor": "middle",
        }),
      );
    }
  });

  return canvas;
}

const RATING_SCALE_MAXIMUM = 5;

/**
 * The lowest rating Letterboxd lets a member give.
 *
 * The rating axis runs from here rather than from zero, because zero is not a
 * rating anyone can give and an axis that reserves a tenth of its length for an
 * impossible value is a tenth of the length wasted. The scale still holds the
 * whole possible range, so it stays comparable across every rating chart.
 */
const RATING_SCALE_MINIMUM = 0.5;

/**
 * Draws a line across a series of yearly averages on the 0 to 5 rating scale.
 *
 * The axis is pinned to the full rating scale rather than the observed range,
 * because a drift of a tenth of a star should look like a tenth of a star. That
 * is the right call and it has a cost: on this library the years sit between
 * 3.6 and 4.5 and the line looks nearly flat, so every point is labelled with
 * its own value rather than left to be read off the axis.
 *
 * The gridlines are labelled 0.5, 2.5 and 5. A tick at 0.0 was a label for a
 * rating that cannot be given, on a chart whose caption calls it the 0.5 to 5
 * scale.
 */
function ratingLineChart(width, points) {
  const height = 190;
  const canvas = svgCanvas(width, height);

  const leftGutter = 30;
  const rightPadding = 10;
  const topPadding = 20;
  const bottomAxis = 24;
  const plotHeight = height - topPadding - bottomAxis;
  const plotWidth = Math.max(20, width - leftGutter - rightPadding);
  const baselineY = topPadding + plotHeight;

  const yFor = (value) =>
    baselineY - (Math.max(0, Math.min(RATING_SCALE_MAXIMUM, value)) / RATING_SCALE_MAXIMUM) * plotHeight;
  const xFor = (index) =>
    points.length === 1
      ? leftGutter + plotWidth / 2
      : leftGutter + (plotWidth * index) / (points.length - 1);

  canvas.append(
    svgNode("line", {
      class: "chart__axis",
      x1: leftGutter,
      y1: yFor(0) + 0.5,
      x2: width - rightPadding,
      y2: yFor(0) + 0.5,
    }),
  );

  for (const tickValue of [0.5, 2.5, 5]) {
    const y = yFor(tickValue);
    canvas.append(
      svgNode("line", {
        class: "chart__gridline",
        x1: leftGutter,
        y1: y + 0.5,
        x2: width - rightPadding,
        y2: y + 0.5,
      }),
    );
    canvas.append(
      svgText(tickValue.toFixed(1), {
        class: "chart__tick",
        x: leftGutter - 6,
        y: y + 4,
        "text-anchor": "end",
      }),
    );
  }

  if (points.length > 1) {
    canvas.append(
      svgNode("polyline", {
        class: "chart__line",
        points: points.map((point, index) => `${xFor(index)},${yFor(point.value)}`).join(" "),
      }),
    );
  }

  const labelStep = Math.max(1, Math.ceil(40 / Math.max(1, plotWidth / Math.max(1, points.length))));

  const lastIndex = points.length - 1;

  points.forEach((point, index) => {
    const x = xFor(index);
    const y = yFor(point.value);
    const dot = svgNode("circle", { class: "chart__dot", cx: x, cy: y, r: 3.5 });
    const films =
      point.count === null || point.count === undefined
        ? ""
        : ` over ${formatQuantity(point.count, "film", "films")}`;
    canvas.append(withTooltip(dot, `${point.label}: ${formatDecimal(point.value, 2)}${films}`));

    // Every point carries its own value. Without it the only readable motion in
    // this chart is the last year, and on this library the last year is one film.
    const anchor = index === 0 ? "start" : index === lastIndex ? "end" : "middle";
    canvas.append(
      svgText(formatDecimal(point.value, 2), {
        class: "chart__value",
        x,
        y: y - 9,
        "text-anchor": anchor,
      }),
    );

    // The last year is always labelled, so a stepped label too close to it is
    // dropped rather than left to overlap.
    const labelThisPoint =
      index === lastIndex || (index % labelStep === 0 && index <= lastIndex - labelStep);

    if (labelThisPoint) {
      canvas.append(
        svgText(point.label, {
          class: "chart__tick",
          x,
          y: baselineY + 16,
          "text-anchor": anchor,
        }),
      );
    }
  });

  return canvas;
}

/** Where a rating sits along the 0.5 to 5 axis, as a percentage from the left. */
function ratingAxisPosition(rating) {
  const value = toNumber(rating);
  if (value === null) {
    return null;
  }
  const clamped = Math.max(RATING_SCALE_MINIMUM, Math.min(RATING_SCALE_MAXIMUM, value));
  const span = RATING_SCALE_MAXIMUM - RATING_SCALE_MINIMUM;
  return ((clamped - RATING_SCALE_MINIMUM) / span) * 100;
}

/**
 * Builds the mark that compares one rating against another on one shared axis.
 *
 * Two stacked bars used to do this job, and they drew the wrong thing. Every
 * film in the "you rated it higher" column is one you gave five stars, so ten
 * cards drew ten identical full-length green bars; every film in the other
 * column is a half or one star, so ten identical short ones. The quantity the
 * list is actually ranked by, the distance between the two ratings, was the one
 * thing not drawn.
 *
 * A dumbbell draws it: one axis, a dot for each rating, and a connecting segment
 * whose length is the gap. It also halves the height of a card and lets the two
 * colours keep one meaning each, green for your rating and blue for the crowd's,
 * which is what the legend above the columns promises.
 *
 * This is HTML rather than inline SVG on purpose. Every other small mark on the
 * page is an SVG stretched from a fixed viewBox with preserveAspectRatio="none",
 * which is fine for a rectangle and wrong for a circle: a dot drawn that way
 * comes out as an ellipse whose shape changes with the container. Positioning
 * two dots as a percentage along a track needs no drawing surface at all.
 *
 * It carries no text and is hidden from assistive technology, because the figures
 * line underneath already states both ratings and the gap in words.
 */
function buildRatingDumbbell(memberRating, crowdRating) {
  const mark = build("div", "dumbbell");
  mark.setAttribute("aria-hidden", "true");
  mark.append(build("span", "dumbbell__track"));

  const minePosition = ratingAxisPosition(memberRating);
  const theirsPosition = ratingAxisPosition(crowdRating);

  if (minePosition !== null && theirsPosition !== null) {
    const segment = build("span", "dumbbell__segment");
    segment.style.left = `${Math.min(minePosition, theirsPosition)}%`;
    segment.style.width = `${Math.abs(minePosition - theirsPosition)}%`;
    mark.append(segment);
  }

  for (const [position, className] of [
    [theirsPosition, "dumbbell__dot dumbbell__dot--theirs"],
    [minePosition, "dumbbell__dot dumbbell__dot--mine"],
  ]) {
    if (position === null) {
      continue;
    }
    const dot = build("span", className);
    dot.style.left = `${position}%`;
    mark.append(dot);
  }

  return mark;
}

const PROPORTION_BAR_VIEW_WIDTH = 100;
const PROPORTION_BAR_HEIGHT = 6;

/** Returns a value's share of a whole, from 0 to 1, or 0 when it cannot be one. */
function shareOf(value, total) {
  const amount = toNumber(value);
  const whole = toNumber(total);
  if (amount === null || whole === null || whole <= 0) {
    return 0;
  }
  return Math.max(0, Math.min(1, amount / whole));
}

/**
 * Draws one bar showing a value against the whole it is measured against.
 *
 * Drawn to a fixed viewBox and stretched to its row, so a list of dozens of them
 * needs no resize observer. A rectangle survives that stretch; anything with a
 * curve of its own does not, which is why buildRatingDumbbell is HTML instead.
 * It carries no text and is hidden from assistive technology, because the row
 * above it already prints the name and the figure the bar is drawing.
 */
function proportionBar(value, total, className = "chart__bar") {
  const canvas = svgNode("svg", {
    viewBox: `0 0 ${PROPORTION_BAR_VIEW_WIDTH} ${PROPORTION_BAR_HEIGHT}`,
    preserveAspectRatio: "none",
    height: PROPORTION_BAR_HEIGHT,
    "aria-hidden": "true",
    focusable: "false",
  });

  canvas.append(
    svgNode("rect", {
      class: "chart__track",
      x: 0,
      y: 0,
      width: PROPORTION_BAR_VIEW_WIDTH,
      height: PROPORTION_BAR_HEIGHT,
      rx: 1,
    }),
  );

  const share = shareOf(value, total);
  if (share > 0) {
    canvas.append(
      svgNode("rect", {
        class: className,
        x: 0,
        y: 0,
        width: Math.max(1, share * PROPORTION_BAR_VIEW_WIDTH),
        height: PROPORTION_BAR_HEIGHT,
        rx: 1,
      }),
    );
  }

  return canvas;
}

/* =================================================================== Heatmap */

const HEATMAP_CELL = 12;
const HEATMAP_STEP = 15;
const HEATMAP_LEFT_GUTTER = 26;
const HEATMAP_TOP_GUTTER = 16;
/* Short names label a crowded axis; full names go into the prose beside it. */
const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const MONTH_FULL_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];
const WEEKDAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

/** Maps a day's film count onto one of the five heatmap shades. */
function heatmapLevel(count) {
  if (count >= 5) return 4;
  if (count >= 3) return 3;
  if (count >= 2) return 2;
  if (count >= 1) return 1;
  return 0;
}

/**
 * Draws one calendar year as a grid of days, weeks across and weekdays down.
 *
 * The grid is a fixed pixel size rather than a responsive one, because a day
 * square smaller than about ten pixels stops being clickable or readable. Its
 * container scrolls sideways instead.
 */
function calendarHeatmap(year, countsByDate) {
  const width = HEATMAP_LEFT_GUTTER + 53 * HEATMAP_STEP;
  const height = HEATMAP_TOP_GUTTER + 7 * HEATMAP_STEP;
  const canvas = svgCanvas(width, height);

  const firstOfYear = new Date(Date.UTC(year, 0, 1));
  const firstWeekday = firstOfYear.getUTCDay();
  const columnFor = (dayOfYear) => Math.floor((dayOfYear + firstWeekday) / 7);

  for (const weekdayIndex of [1, 3, 5]) {
    canvas.append(
      svgText(["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][weekdayIndex], {
        class: "chart__tick",
        x: 0,
        y: HEATMAP_TOP_GUTTER + weekdayIndex * HEATMAP_STEP + 10,
      }),
    );
  }

  const cursor = new Date(firstOfYear.getTime());
  let dayOfYear = 0;

  while (cursor.getUTCFullYear() === year) {
    const isoDate = cursor.toISOString().slice(0, 10);
    const count = toNumber(countsByDate.get(isoDate)) ?? 0;
    const column = columnFor(dayOfYear);

    if (cursor.getUTCDate() === 1) {
      canvas.append(
        svgText(MONTH_NAMES[cursor.getUTCMonth()], {
          class: "chart__tick",
          x: HEATMAP_LEFT_GUTTER + column * HEATMAP_STEP,
          y: 10,
        }),
      );
    }

    const cell = svgNode("rect", {
      class: "heatmap__cell",
      "data-level": heatmapLevel(count),
      x: HEATMAP_LEFT_GUTTER + column * HEATMAP_STEP,
      y: HEATMAP_TOP_GUTTER + cursor.getUTCDay() * HEATMAP_STEP,
      width: HEATMAP_CELL,
      height: HEATMAP_CELL,
      rx: 2,
    });
    canvas.append(
      withTooltip(cell, `${isoDate}: ${count === 1 ? "1 film" : `${formatCount(count)} films`}`),
    );

    cursor.setUTCDate(cursor.getUTCDate() + 1);
    dayOfYear += 1;
  }

  return canvas;
}

/**
 * Marks a sideways scroller as scrollable, and keeps the mark current.
 *
 * The fade on the right edge and the "scroll for the rest of the year" line
 * only make sense while there is something off to the right. A full year is
 * 821px wide, which fits the content column above about 860px and does not fit
 * below it, so neither can be decided in CSS from the viewport alone without
 * hard-coding a width that would go stale the moment a cell size changed.
 */
function trackScrollable(scroller) {
  const update = () => {
    const scrollable = scroller.scrollWidth > scroller.clientWidth + 2;
    scroller.dataset.scrollable = scrollable ? "true" : "false";
  };

  update();
  if (typeof ResizeObserver === "function") {
    new ResizeObserver(update).observe(scroller);
  } else {
    window.addEventListener("resize", update);
  }
}

/** Builds the "less to more" key that explains the heatmap shades. */
function buildHeatmapLegend() {
  const legend = build("p", "heatmap-legend");
  legend.append(build("span", null, "No film"));
  for (const level of [0, 1, 2, 3, 4]) {
    const swatch = build("span", "heatmap-legend__swatch");
    swatch.dataset.level = String(level);
    swatch.setAttribute("aria-hidden", "true");
    legend.append(swatch);
  }
  legend.append(build("span", null, "Five or more"));
  return legend;
}

/* ============================================================ Text components */

/**
 * Builds a single large figure with a label and one line of context.
 *
 * Used where a chart would add nothing, such as a count of days.
 */
function buildStatCard({ value, unit, label, note, fix, unresolved }) {
  const card = build("div", unresolved === true || value === MISSING_VALUE ? "stat stat--unresolved" : "stat");
  const figure = build("p", "stat__value", value);
  if (unit) {
    figure.append(build("span", "stat__unit", ` ${unit}`));
  }
  card.append(figure);
  card.append(build("p", "stat__label", label));
  if (note) {
    card.append(build("p", "stat__note", note));
  }
  if (fix) {
    card.append(build("p", "empty__fix", fix));
  }
  return card;
}

/**
 * Builds one row of a ranked list: a name, a figure, and a mark under them.
 *
 * A row takes { title, href, meta, value, bar, dot }. A row with an href links
 * to the film, person or studio on Letterboxd; a row without one is plain text.
 * A row carries at most one mark:
 *
 *   bar { value, total, className }  a share of a whole, drawn as a filled track
 *   dot { value }                    a rating, drawn as a point on the 0.5 to 5
 *                                    axis, with the library average marked
 *
 * The mark exists so a ranking can be a list of links and still show its shape.
 * An SVG chart cannot: its labels are drawn text, and turning one into a link
 * would put a focusable link inside a drawing this page hides from assistive
 * technology.
 *
 * The name and the figure sit in a wrapper rather than directly in the list
 * item, because a list item laid out as a grid stops being a list item and
 * loses both its number and its place in the list for a screen reader.
 */
function buildRankedRow(row) {
  const item = build("li", "ranked__row");
  const line = build("div", "ranked__line");
  const name = build("p", "ranked__name");

  if (row.href) {
    const link = build("a", null, row.title);
    link.href = row.href;
    link.rel = "noopener";
    name.append(link);
  } else {
    name.textContent = row.title;
  }

  line.append(name);
  line.append(build("p", "ranked__value", row.value));
  item.append(line);

  // The mark and the sentence that qualifies it share a line. On their own
  // lines every ranked row ran to three, and a page holding a hundred and
  // thirty of them cannot afford a line it does not need.
  if (row.bar || row.dot || row.meta) {
    const under = build("div", "ranked__under");

    if (row.bar) {
      const bar = build("div", "ranked__bar");
      bar.append(proportionBar(row.bar.value, row.bar.total, row.bar.className));
      under.append(bar);
    } else if (row.dot) {
      under.append(buildRatingRowMark(row.dot.value, row.dot.reference));
    }

    if (row.meta) {
      under.append(build("p", "ranked__meta", row.meta));
    }

    item.append(under);
  }

  return item;
}

/**
 * Draws one average rating as a point on the shared 0.5 to 5 axis.
 *
 * `reference` is the library-wide average, drawn as a faint upright line so the
 * row answers "compared to what?" without needing a second chart. Every average
 * on this page is drawn against the same axis, so two rankings in different
 * sections stay comparable by eye.
 */
function buildRatingRowMark(value, reference) {
  const mark = build("div", "rating-mark");
  mark.setAttribute("aria-hidden", "true");
  mark.append(build("span", "rating-mark__track"));

  const referencePosition = ratingAxisPosition(reference);
  if (referencePosition !== null) {
    const line = build("span", "rating-mark__reference");
    line.style.left = `${referencePosition}%`;
    mark.append(line);
  }

  const position = ratingAxisPosition(value);
  if (position !== null) {
    const dot = build("span", "rating-mark__dot");
    dot.style.left = `${position}%`;
    mark.append(dot);
  }

  return mark;
}

/**
 * Builds the key that names the ends of the rating axis and the average on it.
 *
 * It is drawn once above a list rather than once per row, for the same reason
 * the denominators are explained once under the totals: repeated on every row it
 * would be longer than the rows it explains.
 */
function buildRatingScaleKey(reference) {
  const key = build("p", "rating-scale");
  key.append(build("span", "rating-scale__end", RATING_SCALE_MINIMUM.toFixed(1)));

  const bar = build("span", "rating-scale__bar");
  const referencePosition = ratingAxisPosition(reference);
  if (referencePosition !== null) {
    const marker = build("span", "rating-scale__marker");
    marker.style.left = `${referencePosition}%`;
    bar.append(marker);
  }
  key.append(bar);

  key.append(build("span", "rating-scale__end", RATING_SCALE_MAXIMUM.toFixed(1)));

  if (referencePosition !== null) {
    key.append(
      build(
        "span",
        "rating-scale__note",
        `The upright mark is your own average, ${formatDecimal(reference, 2)}, taken over the films TMDB also scores.`,
      ),
    );
  }
  return key;
}

/**
 * Builds a ranked list of named rows, each with a figure on the right.
 *
 * `collapseAfter` puts the tail of a long ranking behind a native disclosure.
 * Twenty rows of the same shape, twelve times over, is what made this page forty
 * screens tall on a phone; eight rows and a summary that says how many more
 * there are loses nothing, because the count is in the summary and the note
 * underneath still names the total the pipeline measured.
 */
function buildRankedList(rows, { collapseAfter } = {}) {
  const list = build("ol", "ranked");
  const visibleCount =
    collapseAfter !== undefined && rows.length > collapseAfter + 2 ? collapseAfter : rows.length;

  for (const row of rows.slice(0, visibleCount)) {
    list.append(buildRankedRow(row));
  }

  if (visibleCount === rows.length) {
    return list;
  }

  const hidden = rows.slice(visibleCount);
  const details = build("details", "ranked__more");
  const summary = build(
    "summary",
    "ranked__more-summary",
    `Show the remaining ${formatCount(hidden.length)}`,
  );
  details.append(summary);

  const tail = build("ol", "ranked");
  tail.setAttribute("start", String(visibleCount + 1));
  for (const row of hidden) {
    tail.append(buildRankedRow(row));
  }
  details.append(tail);

  const wrapper = build("div", "ranked-group");
  wrapper.append(list);
  wrapper.append(details);
  return wrapper;
}

/** Builds a description list of one-off facts, such as the shortest film seen. */
function buildFactList(facts) {
  const list = build("dl", "facts");
  for (const fact of facts) {
    const row = build("div", "facts__row");
    row.append(build("dt", "facts__term", fact.term));

    const definition = build("dd", "facts__definition");
    if (fact.href) {
      const link = build("a", null, fact.value);
      link.href = fact.href;
      link.rel = "noopener";
      definition.append(link);
    } else {
      definition.textContent = fact.value;
    }
    if (fact.meta) {
      definition.append(build("span", "facts__meta", ` ${fact.meta}`));
    }

    row.append(definition);
    list.append(row);
  }
  return list;
}

/** Builds the two-swatch key that names which bar is yours and which is theirs. */
function buildComparisonLegend(mineLabel, theirsLabel) {
  const legend = build("p", "legend");
  for (const [className, label] of [
    ["legend__swatch--primary", mineLabel],
    ["legend__swatch--secondary", theirsLabel],
  ]) {
    const item = build("span", "legend__item");
    const swatch = build("span", `legend__swatch ${className}`);
    swatch.setAttribute("aria-hidden", "true");
    item.append(swatch);
    item.append(document.createTextNode(label));
    legend.append(item);
  }
  return legend;
}

/* ============================================== Tolerant readers for open shapes */

const BUCKET_LABEL_KEYS = ["label", "bucket", "range", "name", "days"];
const BUCKET_COUNT_KEYS = ["count", "films", "value"];

/**
 * Reads a histogram bucket whose key names the contract leaves open.
 *
 * The contract fixes the buckets for `rating_vs_runtime` but not for `runtime`
 * or `logging_lag`. Rather than guess, a bucket is used only when it names
 * itself and carries a count. Anything else is skipped.
 */
function readBucket(bucket) {
  const source = toObject(bucket);
  if (source === null) {
    return null;
  }
  const label = BUCKET_LABEL_KEYS.map((key) => toText(source[key])).find(
    (value) => value !== null && value !== undefined,
  );
  const count = BUCKET_COUNT_KEYS.map((key) => toNumber(source[key])).find(
    (value) => value !== null && value !== undefined,
  );
  if (label === undefined || count === undefined) {
    return null;
  }
  return { label, count };
}

/**
 * Shortens a bucket name to something that fits under a column.
 *
 * "under 60", "120-149" and "180 and over" are the right words in a caption and
 * in a data table, and they are far too long to sit under a bar. The full name
 * stays in both of those places and in the tooltip; only the axis gets the short
 * form. A name that fits no pattern here keeps its first word, which is still
 * better than a label the chart has to skip.
 */
function shortenBucketLabel(label) {
  const text = String(label).trim();
  const under = /^(?:under|less than|below)\s+(\d+)/i.exec(text);
  if (under !== null) {
    return `<${under[1]}`;
  }
  const over = /^(\d+)\D*(?:and over|and above|or more|\+)\s*$/i.exec(text);
  if (over !== null) {
    return `${over[1]}+`;
  }
  const range = /^(\d+)\D+(\d+)/.exec(text);
  if (range !== null) {
    return `${range[1]}\u2013${range[2]}`;
  }
  const single = /^(\d+)/.exec(text);
  if (single !== null) {
    return single[1];
  }
  return text.split(/\s+/)[0];
}

/** Reads a whole distribution into column chart categories, skipping bad rows. */
function readDistribution(list) {
  const categories = [];
  for (const bucket of toArray(list)) {
    const read = readBucket(bucket);
    if (read !== null) {
      categories.push({
        label: read.label,
        shortLabel: shortenBucketLabel(read.label),
        values: [read.count],
      });
    }
  }
  return categories;
}

const FILM_VALUE_KEYS = ["vote_count", "votes", "count", "popularity"];

/**
 * Reads a film row from any of the extras lists that name films.
 *
 * Every such list carries a title and usually a slug. The figure beside it
 * differs by module, so the caller passes the key it expects and falls back to
 * the common vote count names.
 */
function readFilmRow(entry, valueKey) {
  const source = toObject(entry);
  if (source === null) {
    return null;
  }
  const title = toText(source.title) ?? toText(source.name);
  if (title === null) {
    return null;
  }
  const keys = valueKey ? [valueKey, ...FILM_VALUE_KEYS] : FILM_VALUE_KEYS;
  const value = keys.map((key) => toNumber(source[key])).find((found) => found !== undefined && found !== null);
  return {
    title,
    year: toNumber(source.year),
    href: filmUrl(source.slug),
    value: value ?? null,
  };
}

/** Adds the release year to a film title when the file carries one. */
function titleWithYear(row) {
  return row.year === null ? row.title : `${row.title} (${formatYear(row.year)})`;
}

/**
 * Lowers the first letter, for a label reused in the middle of a sentence.
 *
 * A coverage label such as "Films with a rating" opens its own line, and the
 * same words have to read as "films with a rating" when a claim names the
 * figure first.
 */
function lowerCaseFirstLetter(text) {
  return text.charAt(0).toLowerCase() + text.slice(1);
}

/* ================================================================== Coverage */

/**
 * How many films each kind of figure on this page can actually see.
 *
 * Four denominators share one page. `totals.films` counts the whole library.
 * Anything built from dates counts only the films with a watch date, because
 * Letterboxd dates a diary entry and not a film simply marked as seen. Anything
 * built from a genre, a runtime, or a credit counts only the films the TMDB
 * cache has resolved. Anything averaged over the member's own ratings counts
 * only the films that carry one.
 *
 * Reading these counts is what lets the page tell "measured none" from "not
 * worked out yet". Both arrive as zero, and only the coverage block separates
 * them, so no renderer may decide that question by looking at the figure.
 */
function readCoverage(stats) {
  const coverage = toObject(stats?.coverage);
  return {
    total: toNumber(coverage?.films_total),
    dated: toNumber(coverage?.films_with_a_date),
    rated: toNumber(coverage?.films_with_a_rating),
    tmdb: toNumber(coverage?.films_with_tmdb_data),
  };
}

/**
 * The film subsets a figure can be built from, and the words for each.
 *
 * The wordings live here so every module built on the same subset describes it
 * the same way, and so a new module needs a basis name rather than new prose.
 */
const COVERAGE_BASES = {
  dated: {
    countOf: (coverage) => coverage.dated,
    label: "Films with a watch date",
    countedAcross: (count) => `counted among the ${formatCount(count)} films with a watch date`,
    notCounted: "not counted: no film carries a watch date",
  },
  rated: {
    countOf: (coverage) => coverage.rated,
    label: "Films with a rating",
    countedAcross: (count) => `counted across the ${formatCount(count)} films you have rated`,
    notCounted: "not counted: no film carries a rating",
  },
  tmdb: {
    countOf: (coverage) => coverage.tmdb,
    label: "Films with TMDB metadata",
    countedAcross: (count) => `counted across the ${formatCount(count)} films with TMDB metadata`,
    notCounted: "not counted: no film has TMDB metadata yet",
  },
};

/** True when the coverage block carries every count the page reads from it. */
function coverageIsComplete(coverage) {
  return (
    coverage.total !== null &&
    coverage.dated !== null &&
    coverage.rated !== null &&
    coverage.tmdb !== null
  );
}

/**
 * Says what a figure built on one subset of the library can claim.
 *
 * Four answers, because the page has to print each of them differently:
 *   - "whole":   the subset is the library, so nothing needs qualifying.
 *   - "part":    the figure is real, but its denominator is not the library and
 *                the page has to say which one it is.
 *   - "empty":   the subset holds no film, so any figure built on it is
 *                unresolved. A zero here means "not worked out", never "none".
 *   - "unknown": the file does not say, so the page must not claim either.
 */
function describeBasis(coverage, basisName) {
  const basis = COVERAGE_BASES[basisName];
  const count = basis.countOf(coverage);
  const total = coverage.total;

  if (count === null || total === null) {
    return { state: "unknown", basis, count, total };
  }
  if (count === 0) {
    return { state: "empty", basis, count, total };
  }
  if (count >= total) {
    return { state: "whole", basis, count, total };
  }
  return { state: "part", basis, count, total };
}

/**
 * The scope labels the page prints, and the claim each one makes.
 *
 * A label is a claim about the figures under it, so each one is written from
 * what scripts/build_stats.py counts that module from, not from where the
 * module sits on the page. A wrong denominator is worse than no denominator: it
 * reads as a confident measurement of a set the figures were never counted over.
 *
 * A section may mix two subsets, so a label is a list of claims. Each claim is
 * one of:
 *   { basis }            every figure under the label comes from that subset.
 *   { figures, basis }   one named figure comes from a different subset than
 *                        the rest of the section, and says so beside its name.
 *   { figures, scope }   the subset has no count in the coverage block, so the
 *                        claim names it in words and gives no denominator. A
 *                        count the stats file does not carry is never invented.
 *
 * The subsets themselves are explained once, in the paragraph under the totals,
 * so a label here stays short enough to read as a label.
 */
const COVERAGE_NOTE_TARGETS = [
  // build_by_year reads the entries that carry a watch date, and the ratings
  // histogram beside it is built from the same entries.
  { id: "by-year-coverage", claims: [{ basis: "dated" }] },

  // build_decades groups films by release year, and the history carries that
  // year even for films TMDB never resolved, so this section is not date bound
  // at all. What it ranks by is the average rating, which needs a rating.
  {
    id: "decades-coverage",
    claims: [
      { figures: "Average rating", basis: "rated" },
      { figures: "Film count", scope: "every film with a known release year" },
    ],
  },

  // Everything here is counted from watch dates except the first headline
  // figure, which totals the runtime of every film logged, dated or not.
  {
    id: "rhythm-coverage",
    claims: [{ basis: "dated" }, { figures: "Days of film", basis: "tmdb" }],
  },

  { id: "rating-drift-coverage", claims: [{ basis: "dated" }] },

  // Everything from here down is built from a genre, a country, a credit, a
  // runtime or a crowd rating, and every one of those comes from the TMDB
  // cache. The paragraph under the totals gives the rule once; these labels put
  // the denominator beside the figures it applies to, so a reader who arrives
  // in the middle of the page does not have to go looking for it.
  { id: "genres-countries-languages-coverage", claims: [{ basis: "tmdb" }] },
  { id: "cast-coverage", claims: [{ basis: "tmdb" }] },
  { id: "directors-coverage", claims: [{ basis: "tmdb" }] },
  { id: "studios-coverage", claims: [{ basis: "tmdb" }] },
  { id: "collections-coverage", claims: [{ basis: "tmdb" }] },
  { id: "countries-ranked-coverage", claims: [{ basis: "tmdb" }] },
  { id: "contrarian-coverage", claims: [{ basis: "tmdb" }] },
  { id: "reach-coverage", claims: [{ basis: "tmdb" }] },
  { id: "rating-vs-runtime-coverage", claims: [{ basis: "tmdb" }] },
  { id: "extras-people-coverage", claims: [{ basis: "tmdb" }] },

  // build_half_star_usage counts each rated film once, whether or not it is
  // dated, so this histogram covers far more films than the ratings histogram
  // under "By year". Two rating charts with different totals need saying.
  { id: "half-star-coverage", claims: [{ basis: "rated" }] },

  // build_release_recency needs a watch date and a TMDB release date. The watch
  // date is the narrower of the two, and the other is named in words because
  // the coverage block counts films with metadata, not films with a release date.
  {
    id: "release-recency-coverage",
    claims: [
      { basis: "dated" },
      { scope: "counted only where TMDB gives a release date" },
    ],
  },
];

/**
 * Writes one claim, or null when the file gives nothing to claim.
 *
 * A claim on a subset that is the whole library says nothing worth printing,
 * and a claim on a subset the file does not count is a guess, so both come back
 * as null and the label drops them.
 */
function describeClaim(coverage, claim) {
  if (claim.basis === undefined) {
    return claim.figures === undefined
      ? startSentence(claim.scope)
      : `${claim.figures}: ${claim.scope}`;
  }

  const scope = describeBasis(coverage, claim.basis);
  if (scope.state !== "part" && scope.state !== "empty") {
    return null;
  }

  const counted = `${formatCount(scope.count)} of ${formatCount(scope.total)}`;
  if (claim.figures === undefined) {
    return `${scope.basis.label}: ${counted}`;
  }

  return `${claim.figures}: ${lowerCaseFirstLetter(scope.basis.label)}, ${counted}`;
}

/** Prints the denominator beside every module counted from part of the library. */
function renderCoverageNotes(coverage) {
  for (const target of COVERAGE_NOTE_TARGETS) {
    const node = elementById(target.id);
    if (node === null) {
      continue;
    }

    const claims = target.claims
      .map((claim) => describeClaim(coverage, claim))
      .filter((claim) => claim !== null);

    if (claims.length === 0) {
      node.hidden = true;
      node.textContent = "";
      continue;
    }

    // One claim is a label and takes no full stop. Several claims are sentences
    // about different figures, and run together without stops they read as one
    // muddled claim.
    node.hidden = false;
    node.textContent =
      claims.length === 1 ? claims[0] : claims.map((claim) => `${claim}.`).join(" ");
  }
}

/**
 * Writes the one paragraph that explains every denominator on the page.
 *
 * Saying it once, next to the totals it qualifies, is what keeps the sections
 * below down to a short label each instead of nine repetitions of the same
 * caveat. It stays hidden when there is nothing to qualify.
 */
function renderCoverageSummary(coverage) {
  const banner = elementById("coverage-summary");
  if (banner === null) {
    return;
  }

  const sentences = [];

  // No films at all is its own case. Every sentence below reads as a
  // measurement of a library, and there is no library to measure.
  if (coverage.total === 0) {
    banner.hidden = false;
    banner.textContent =
      "No films are recorded yet, so nothing on this page has been counted. " +
      NEEDS_HISTORY.fix;
    return;
  }

  if (!coverageIsComplete(coverage)) {
    sentences.push(
      "This stats file does not record how many films each figure is counted from, so a figure " +
        `shown as "${MISSING_VALUE}" here may be missing rather than zero. Rebuild the stats with ` +
        "scripts/build_stats.py, which writes those counts.",
    );
  } else {
    const dated = describeBasis(coverage, "dated");
    if (dated.state === "part") {
      sentences.push(
        `${formatCount(dated.count)} of the ${formatCount(dated.total)} films watched carry a watch date. ` +
          "Letterboxd dates a diary entry, not a film marked as seen, so every figure built from " +
          "dates describes that smaller set, and each section built that way states its own count.",
      );
    } else if (dated.state === "empty") {
      sentences.push(
        `None of the ${formatCount(dated.total)} films watched carries a watch date, so nothing built ` +
          "from dates can be counted. Letterboxd dates a diary entry, not a film marked as seen, and " +
          `only the export carries those entries. ${FIX_EXPORT}`,
      );
    }

    const tmdb = describeBasis(coverage, "tmdb");
    if (tmdb.state === "empty") {
      sentences.push(
        "Hours, directors and countries are not counted yet, and neither is any section below that " +
          "needs a genre, a runtime, or a credit: no film has TMDB metadata. " +
          FIX_TMDB,
      );
    } else if (tmdb.state === "part") {
      // The tiles this qualifies are named on the scope line directly above, so
      // this sentence gives the rule rather than the list a second time.
      sentences.push(
        `${formatCount(tmdb.count)} of the ${formatCount(tmdb.total)} films have TMDB metadata, so ` +
          "every figure built from a genre, a country, a runtime or a credit describes those.",
      );
    }
  }

  if (sentences.length === 0) {
    banner.hidden = true;
    banner.replaceChildren();
    return;
  }

  banner.hidden = false;
  banner.textContent = sentences.join(" ");
}

/* ==================================================== Panel section renderers */

/**
 * The six header figures, and which films each one is counted from.
 *
 * `basis` names the subset the figure needs. A tile with no basis is counted
 * from the whole library. The three TMDB tiles and the two date tiles are all
 * zero both when the answer is really none and when the input has not been
 * built yet, so their basis decides which of the two the page may print.
 */
const TOTAL_TILES = [
  { key: "films", label: "Films", note: "watched in total" },
  { key: "hours", label: "Hours", note: "of screen time", basis: "tmdb" },
  { key: "directors", label: "Directors", note: "with at least one film seen", basis: "tmdb" },
  { key: "countries", label: "Countries", note: "of production", basis: "tmdb" },
  {
    key: "longest_streak_weeks",
    label: "Longest streak",
    note: "consecutive weeks with a film",
    basis: "dated",
  },
  {
    key: "multi_film_days",
    label: "Double bills",
    note: "days with more than one film",
    basis: "dated",
  },
];

/**
 * Narrows one tile's reading to the subset that tile's figure is counted from.
 *
 * A figure counted from an empty subset is printed as unresolved: zero hours
 * across zero resolved films is not a screen time of nothing, it is a screen
 * time nobody has worked out.
 *
 * A figure counted from part of the library keeps its own short note. Which
 * part, and how many films that part holds, is stated once for the whole grid
 * by describeTileScopes. Repeating it on every tile put the same clause under
 * five figures in the totals grid and two more below, and the clause is longer
 * than the figure it qualifies.
 */
function scopeTileReading(reading, basisName, coverage) {
  if (basisName === undefined) {
    return reading;
  }

  if (describeBasis(coverage, basisName).state === "empty") {
    return { value: MISSING_VALUE, note: "not counted yet", unresolved: true };
  }

  return reading;
}

/** Joins names into an English list, such as "Hours, Directors and Countries". */
function joinNames(names) {
  if (names.length <= 1) {
    return names.join("");
  }
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/**
 * Writes the sentences that give one tile grid its denominators.
 *
 * One sentence per subset the grid draws on, naming the tiles counted from it
 * and how many films it holds. This is what keeps the denominator on the page
 * while taking it off every tile: a reader can still learn what any figure was
 * counted from without leaving the page, by reading one line instead of the
 * same clause five times.
 */
function describeTileScopes(tiles, coverage) {
  const sentences = [];

  // In the order the subsets first appear across the grid, so the line reads in
  // the same order as the tiles a reader has just looked at.
  const basisNames = [...new Set(tiles.map((tile) => tile.basis))].filter(
    (basisName) => basisName !== undefined && basisName in COVERAGE_BASES,
  );

  for (const basisName of basisNames) {
    const labels = tiles.filter((tile) => tile.basis === basisName).map((tile) => tile.label);

    const scope = describeBasis(coverage, basisName);
    const verb = labels.length === 1 ? "is" : "are";

    if (scope.state === "part") {
      sentences.push(`${joinNames(labels)} ${verb} ${scope.basis.countedAcross(scope.count)}.`);
    } else if (scope.state === "empty") {
      sentences.push(`${joinNames(labels)} ${verb} ${scope.basis.notCounted}.`);
    }
  }

  return sentences;
}

/** Prints one tile grid's denominators under it, or hides the line when it has none. */
function renderTileScope(id, tiles, coverage) {
  const node = elementById(id);
  if (node === null) {
    return;
  }

  const sentences = describeTileScopes(tiles, coverage);
  node.hidden = sentences.length === 0;
  node.textContent = sentences.join(" ");
}

/**
 * Decides what one totals tile may claim, from the figure and the coverage.
 *
 * An unresolved figure must never reach the page as a measurement, so the
 * decision is taken from the coverage block rather than from the figure.
 */
function readTotalTile(tile, totals, coverage) {
  const reading = scopeTileReading(
    { value: formatCount(totals[tile.key]), note: tile.note, unresolved: false },
    tile.basis,
    coverage,
  );

  // With no coverage to read, a zero cannot be told from an absent figure. The
  // honest reading of an unbacked zero is that nothing was counted.
  const unbackedZero =
    tile.basis !== undefined &&
    describeBasis(coverage, tile.basis).state === "unknown" &&
    toNumber(totals[tile.key]) === 0;

  if (unbackedZero) {
    return {
      value: MISSING_VALUE,
      note: "not counted: this file does not record its coverage",
      unresolved: true,
    };
  }

  return reading;
}

/** Renders the header figures: films, hours, directors, countries, streaks. */
function renderTotals(stats, coverage) {
  const container = elementById("totals-grid");
  if (container === null) {
    return;
  }

  const totals = toObject(stats.totals);
  if (totals === null) {
    showEmptyState(container, waitingFor("No totals yet.", NEEDS_HISTORY));
    renderTileScope("totals-scope", [], coverage);
    return;
  }

  const tiles = TOTAL_TILES.map((tile) => {
    const read = readTotalTile(tile, totals, coverage);
    return buildTile({
      value: read.value,
      label: tile.label,
      note: read.note,
      unresolved: read.unresolved,
    });
  });

  container.replaceChildren(...tiles);
  renderTileScope("totals-scope", TOTAL_TILES, coverage);
}

/** Renders films and diary entries per year, and the ratings histogram. */
function renderByYear(stats) {
  const yearsContainer = elementById("by-year-chart");
  const ratingsContainer = elementById("ratings-chart");
  const legend = elementById("by-year-legend");
  const years = toArray(stats.by_year)
    .filter((entry) => toNumber(entry?.year) !== null)
    .sort((left, right) => toNumber(left.year) - toNumber(right.year));

  if (yearsContainer !== null) {
    if (years.length === 0) {
      showEmptyState(
        yearsContainer,
        waitingFor("No years recorded yet. A year appears once the history holds an entry with a watch date.", NEEDS_HISTORY),
      );
    } else {
      yearsContainer.replaceChildren();
      if (legend !== null) {
        legend.hidden = false;
      }

      const categories = years.map((entry) => ({
        label: String(toNumber(entry.year)),
        values: [toNumber(entry.films) ?? 0, toNumber(entry.diary) ?? 0],
      }));

      const busiest = years.reduce(
        (peak, entry) => ((toNumber(entry.films) ?? 0) > (toNumber(peak?.films) ?? -1) ? entry : peak),
        null,
      );

      // The two series differ by one or two entries a year, which no pair of
      // bars can show: a difference of one on a bar of 116 is under two pixels.
      // The columns now carry their own figures, and the caption states the gap
      // in words, because the caption has to be true read on its own.
      const filmTotal = years.reduce((sum, entry) => sum + (toNumber(entry.films) ?? 0), 0);
      const diaryTotal = years.reduce((sum, entry) => sum + (toNumber(entry.diary) ?? 0), 0);
      const rewatchGap = diaryTotal - filmTotal;

      appendChart(yearsContainer, {
        caption:
          `Films watched each year from ${categories[0].label} to ${categories[categories.length - 1].label}.` +
          (busiest === null
            ? ""
            : ` Busiest year: ${formatYear(busiest.year)} with ${formatQuantity(busiest.films, "film", "films")}.`) +
          (rewatchGap <= 0
            ? ""
            : ` ${formatQuantity(diaryTotal, "diary entry", "diary entries")} against ` +
              `${formatCount(filmTotal)} films: ${formatQuantity(rewatchGap, "film was", "films were")} logged more than once.`),
        draw: (width) =>
          columnChart(width, categories, { seriesClassNames: ["chart__bar", "chart__bar--secondary"] }),
        table: buildDataTable(
          "Films and diary entries per year",
          ["Year", "Films", "Diary entries"],
          years.map((entry) => [
            formatYear(entry.year),
            formatCount(entry.films),
            formatCount(entry.diary),
          ]),
        ),
      });
    }
  }

  if (ratingsContainer !== null) {
    renderRatingsHistogram(ratingsContainer, years);
  }
}

/**
 * Renders the histogram of ratings given, summed across every year.
 *
 * The buckets are fixed at the ten half-star steps rather than read from the
 * data, so a rating never used still shows as an empty column in its place.
 */
function renderRatingsHistogram(container, years) {
  const buckets = new Map();
  for (let step = 1; step <= 10; step += 1) {
    buckets.set((step / 2).toFixed(1), 0);
  }

  for (const year of years) {
    const ratings = toObject(year.ratings);
    if (ratings === null) {
      continue;
    }
    for (const [rawRating, rawCount] of Object.entries(ratings)) {
      const rating = toNumber(rawRating);
      const count = toNumber(rawCount);
      if (rating === null || count === null) {
        continue;
      }
      const key = rating.toFixed(1);
      if (buckets.has(key)) {
        buckets.set(key, buckets.get(key) + count);
      }
    }
  }

  const total = [...buckets.values()].reduce((sum, count) => sum + count, 0);
  if (total === 0) {
    showEmptyState(
      container,
      waitingFor("No ratings recorded yet. This histogram counts only entries that carry a rating.", NEEDS_HISTORY),
    );
    return;
  }

  const categories = [...buckets.entries()].map(([rating, count]) => ({
    label: rating,
    values: [count],
  }));

  replaceWithChart(container, {
    // build_by_year counts ratings on dated entries only, so the caption says so:
    // it is the text alternative to the chart and has to be true read alone.
    caption: `${formatQuantity(total, "rating", "ratings")} given on entries with a watch date, grouped by half-star step from 0.5 to 5.`,
    draw: (width) => columnChart(width, categories),
    table: buildDataTable(
      "Ratings given at each half-star step, on entries with a watch date",
      ["Rating", "Times given"],
      categories.map((category) => [category.label, formatCount(category.values[0])]),
    ),
  });
}

/** Renders the average rating given to films from each decade. */
function renderDecades(stats) {
  const container = elementById("decades-chart");
  if (container === null) {
    return;
  }

  const decades = toArray(stats.decades)
    .map((entry) => ({
      decade: toNumber(entry?.decade),
      average: toNumber(entry?.average_rating),
      films: toNumber(entry?.films),
    }))
    .filter((entry) => entry.decade !== null);

  if (decades.length === 0) {
    showEmptyState(
      container,
      waitingFor("No decades yet. A decade appears once a film you have watched carries a release year.", NEEDS_HISTORY),
    );
    return;
  }

  // A decade whose films carry no rating has no place in a ranking by rating.
  // It used to sort as though its average were zero, so the 1870s sat last with
  // a dash where its figure should be, which reads as the worst decade rather
  // than as the one decade that has not been rated.
  const rated = decades
    .filter((entry) => entry.average !== null)
    .sort((left, right) => right.average - left.average);
  const unrated = decades.filter((entry) => entry.average === null);

  if (rated.length === 0) {
    showEmptyState(
      container,
      "No decade averages yet. Every decade you have watched from carries films, but none of " +
        "those films carries a rating.",
    );
    return;
  }

  container.replaceChildren();
  if (libraryAverageRating !== null) {
    container.append(buildRatingScaleKey(libraryAverageRating));
  }
  container.append(
    buildRankedList(
      rated.map((entry) => ({
        title: `${formatYear(entry.decade)}s`,
        value: formatDecimal(entry.average, 2),
        dot: { value: entry.average, reference: libraryAverageRating },
        // The count is the honesty here. Two of the top three decades rest on
        // two films each, and a ranking that hides that invites the reader to
        // read a sample of two as a verdict.
        meta:
          entry.films === null
            ? null
            : `over ${formatQuantity(entry.films, "film", "films")}`,
      })),
      { collapseAfter: RANKED_ROWS_BEFORE_DISCLOSURE },
    ),
  );

  const unratedNote =
    unrated.length === 0
      ? ""
      : ` Not ranked: ${joinNames(unrated.map((entry) => `${formatYear(entry.decade)}s`))}, ` +
        `${unrated.length === 1 ? "which carries" : "which carry"} no rated film.`;

  container.append(
    build(
      "p",
      "chart__caption",
      `Average rating per decade, highest first, on the ${RATING_SCALE_MINIMUM.toFixed(1)} to ` +
        `${RATING_SCALE_MAXIMUM.toFixed(1)} scale.${unratedNote}`,
    ),
  );

  container.append(
    buildDataTable(
      "Average rating and film count per decade",
      ["Decade", "Average rating", "Films"],
      decades
        .slice()
        .sort((left, right) => left.decade - right.decade)
        .map((entry) => [
          `${formatYear(entry.decade)}s`,
          entry.average === null ? MISSING_VALUE : formatDecimal(entry.average, 2),
          formatCount(entry.films),
        ]),
    ),
  );
}

/**
 * Renders one "most watched" and one "highest rated" pair.
 *
 * Genres, countries, and languages all carry the same two-list shape, so they
 * share this renderer and differ only in the labels passed in.
 */
function renderRankedPair(stats, key, singularNoun) {
  const group = toObject(stats[key]);
  const mostWatchedContainer = elementById(`${key}-most-watched`);
  const highestRatedContainer = elementById(`${key}-highest-rated`);

  if (mostWatchedContainer !== null) {
    const items = toArray(group?.most_watched).filter((item) => toText(item?.name) !== null);
    if (items.length === 0) {
      showEmptyState(mostWatchedContainer, waitingFor(`No ${singularNoun} counts yet.`, NEEDS_TMDB));
    } else {
      const shown = items.slice(0, MAXIMUM_BAR_ROWS);
      const busiest = shown.reduce((peak, item) => Math.max(peak, toNumber(item.count) ?? 0), 0);

      mostWatchedContainer.replaceChildren(
        buildRankedList(
          shown.map((item) => ({
            title: toText(item.name),
            value: formatQuantity(item.count, "film", "films"),
            bar: { value: item.count, total: busiest },
          })),
          { collapseAfter: RANKED_ROWS_BEFORE_DISCLOSURE },
        ),
      );
      mostWatchedContainer.append(
        build("p", "chart__caption", `Films watched per ${singularNoun}, most first.`),
      );
      mostWatchedContainer.append(
        buildDataTable(
          `Films watched per ${singularNoun}`,
          ["Name", "Films"],
          shown.map((item) => [toText(item.name), formatCount(item.count)]),
        ),
      );
      // The module key is already the plural, which is how "countrys" happened.
      appendTruncationNote(mostWatchedContainer, shown.length, items.length, `${key}.most_watched`, key);
    }
  }

  if (highestRatedContainer !== null) {
    const items = toArray(group?.highest_rated).filter((item) => toText(item?.name) !== null);
    if (items.length === 0) {
      showEmptyState(highestRatedContainer, waitingFor(`No ${singularNoun} ratings yet.`, NEEDS_TMDB));
    } else {
      const shown = items.slice(0, MAXIMUM_BAR_ROWS);

      highestRatedContainer.replaceChildren();
      if (libraryAverageRating !== null) {
        highestRatedContainer.append(buildRatingScaleKey(libraryAverageRating));
      }
      highestRatedContainer.append(
        buildRankedList(
          shown.map((item) => ({
            title: toText(item.name),
            value: formatDecimal(item.average, 2),
            dot: { value: item.average, reference: libraryAverageRating },
            // Without the count, a genre seen twice outranks one seen 276 times
            // and nothing on the row says so.
            meta:
              toNumber(item.count) === null
                ? null
                : `over ${formatQuantity(item.count, "film", "films")}`,
          })),
          { collapseAfter: RANKED_ROWS_BEFORE_DISCLOSURE },
        ),
      );
      highestRatedContainer.append(
        build(
          "p",
          "chart__caption",
          `Average rating per ${singularNoun}, highest first, on the ` +
            `${RATING_SCALE_MINIMUM.toFixed(1)} to ${RATING_SCALE_MAXIMUM.toFixed(1)} scale.`,
        ),
      );
      highestRatedContainer.append(
        buildDataTable(
          `Average rating per ${singularNoun}`,
          ["Name", "Average rating", "Films"],
          shown.map((item) => [
            toText(item.name),
            formatDecimal(item.average, 2),
            formatCount(item.count),
          ]),
        ),
      );
      appendTruncationNote(
        highestRatedContainer,
        shown.length,
        items.length,
        `${key}.highest_rated`,
        key,
      );
    }
  }
}

/**
 * Builds a grid of people cards with a photo, a name, and a count.
 *
 * A person with no photo keeps the same card size and shows their initials, so
 * a missing image never leaves a hole in the grid.
 *
 * `role` is the Letterboxd path the names link to, one of LETTERBOXD_PATHS. A
 * name that yields no slug is printed as plain text rather than as a link that
 * goes nowhere.
 */
function buildPeopleGrid(people, describeCount, role) {
  const grid = build("ul", "people");

  for (const person of people) {
    const name = toText(person.name);
    if (name === null) {
      continue;
    }

    const item = build("li", "person");
    const avatar = build("div", "person__avatar");
    avatar.append(build("span", "person__initials", initialsFor(name)));

    const profilePath = toText(person.profile_path);
    if (profilePath !== null) {
      const photo = build("img", "person__photo");
      photo.src = `${TMDB_PROFILE_BASE}${profilePath}`;
      photo.alt = "";
      photo.loading = "lazy";
      photo.decoding = "async";
      photo.referrerPolicy = "no-referrer";
      // The initials sit underneath, so dropping a broken image reveals them.
      photo.addEventListener("error", () => photo.remove(), { once: true });
      avatar.append(photo);
    }

    const countText = describeCount(person);
    const nameLine = build("p", "person__name", name);
    const countLine = build("p", "person__count", countText);
    const href = personUrl(role, name);

    if (href === null) {
      item.append(avatar, nameLine, countLine);
      grid.append(item);
      continue;
    }

    // The whole card is the link, not only the name. The portrait is the
    // obvious thing to press on a touch screen and it used to do nothing, which
    // left a 16px line of text as the only target on a card over a hundred
    // pixels tall. The label names the person and the count, so the link still
    // says where it goes when it is read out of context.
    const link = build("a", "person__link");
    link.href = href;
    link.rel = "noopener";
    link.setAttribute("aria-label", `${name}, ${countText}`);
    link.append(avatar, nameLine, countLine);

    item.append(link);
    grid.append(item);
  }

  return grid;
}

/** Renders the most frequent cast members as a grid of cards. */
function renderCast(stats) {
  const container = elementById("cast-list");
  if (container === null) {
    return;
  }

  const cast = toArray(stats.cast).filter((person) => toText(person?.name) !== null);
  if (cast.length === 0) {
    showEmptyState(container, waitingFor("No cast counts yet.", NEEDS_CREDITS));
    return;
  }

  const shown = cast.slice(0, MAXIMUM_PEOPLE_CARDS);
  container.replaceChildren(
    buildPeopleGrid(shown, (person) => formatQuantity(person.count, "film", "films"), "actor"),
  );
  appendTruncationNote(container, shown.length, cast.length, "cast", "actors");
}

/** Renders the most watched directors, with the average rating given to each. */
function renderDirectors(stats) {
  const container = elementById("directors-list");
  if (container === null) {
    return;
  }

  const directors = toArray(stats.directors).filter((person) => toText(person?.name) !== null);
  if (directors.length === 0) {
    showEmptyState(container, waitingFor("No director counts yet.", NEEDS_CREDITS));
    return;
  }

  const shown = directors.slice(0, MAXIMUM_PEOPLE_CARDS);
  container.replaceChildren(
    buildPeopleGrid(
      shown,
      (person) => {
        const average = toNumber(person.average_rating);
        const films = formatQuantity(person.count, "film", "films");
        return average === null ? films : `${films} · ${average.toFixed(1)}`;
      },
      "director",
    ),
  );
  appendTruncationNote(container, shown.length, directors.length, "directors", "directors");
}

/** Renders the studios watched most, with the average rating given to each. */
function renderStudios(stats) {
  const container = elementById("studios-chart");
  if (container === null) {
    return;
  }

  const studios = toArray(stats.studios).filter((studio) => toText(studio?.name) !== null);
  if (studios.length === 0) {
    showEmptyState(container, waitingFor("No studio counts yet.", NEEDS_TMDB));
    return;
  }

  // A list of links rather than a bar chart, because a studio name has to be
  // clickable and an SVG label cannot be. The bar rides inside each row, so the
  // proportions survive the change, and the names now wrap instead of being cut
  // to fit a drawn label column.
  const shown = studios.slice(0, MAXIMUM_BAR_ROWS);
  const busiest = shown.reduce((peak, studio) => Math.max(peak, toNumber(studio.count) ?? 0), 0);

  container.replaceChildren(
    buildRankedList(
      shown.map((studio) => {
        const average = toNumber(studio.average_rating);
        return {
          title: toText(studio.name),
          href: personUrl("studio", studio.name),
          value: formatQuantity(studio.count, "film", "films"),
          bar: { value: studio.count, total: busiest },
          meta: average === null ? null : `${average.toFixed(2)} average`,
        };
      }),
      { collapseAfter: RANKED_ROWS_BEFORE_DISCLOSURE },
    ),
  );
  appendTruncationNote(container, shown.length, studios.length, "studios", "studios");
}

/** Renders how much of each film collection has been watched. */
function renderCollections(stats) {
  const container = elementById("collections-chart");
  if (container === null) {
    return;
  }

  const collections = toArray(stats.collections).filter((entry) => toText(entry?.name) !== null);
  if (collections.length === 0) {
    // Not enrich_tmdb.py. How many films a collection holds is not a fact about
    // any one film, so no film payload carries it and no number of them
    // produces it. It comes from the collections table, which
    // scripts/enrich_people_and_collections.py writes.
    showEmptyState(container, {
      reason:
        "No collections yet. Each one needs the size TMDB reports for the whole series, " +
        "which no film's own record carries.",
      fix: FIX_PEOPLE_AND_COLLECTIONS,
    });
    return;
  }

  const ranked = [...collections].sort((left, right) => {
    const leftShare = shareSeen(left);
    const rightShare = shareSeen(right);
    return rightShare - leftShare || (toNumber(right.total) ?? 0) - (toNumber(left.total) ?? 0);
  });

  // "Pirates of the Caribbean Collection" cut to "Pirates of th…" was the cost
  // of drawing these names into an SVG label column. As rows they wrap in full.
  const shown = ranked.slice(0, MAXIMUM_BAR_ROWS);

  container.replaceChildren(
    buildRankedList(
      shown.map((entry) => ({
        title: toText(entry.name),
        value: `${formatCount(entry.seen)} of ${formatCount(entry.total)}`,
        bar: { value: entry.seen, total: entry.total },
        meta: `${formatPercentage(shareSeen(entry))} seen`,
      })),
      { collapseAfter: RANKED_ROWS_BEFORE_DISCLOSURE },
    ),
  );
  container.append(
    build("p", "chart__caption", "Films seen out of each collection, most complete first."),
  );
  container.append(
    buildDataTable(
      "Films seen per collection",
      ["Collection", "Seen", "Total"],
      shown.map((entry) => [toText(entry.name), formatCount(entry.seen), formatCount(entry.total)]),
    ),
  );
  appendTruncationNote(container, shown.length, ranked.length, "collections", "collections");
}

/** Returns the fraction of a collection or list already seen, from 0 to 1. */
function shareSeen(entry) {
  const seen = toNumber(entry?.seen) ?? 0;
  const total = toNumber(entry?.total) ?? 0;
  return total > 0 ? seen / total : 0;
}

/** Renders progress through the curated lists the pipeline tracks. */
function renderListProgress(stats) {
  const container = elementById("list-progress-chart");
  if (container === null) {
    return;
  }

  const lists = toArray(stats.list_progress).filter((entry) => toText(entry?.title) !== null);
  if (lists.length === 0) {
    showEmptyState(container, {
      reason: "No list progress yet. The curated lists are not cached.",
      fix: "Run scripts/fetch_lists.py, then rebuild the stats.",
    });
    return;
  }

  // Two lists used to render as the same cut label, "They Shoot Pi…", with
  // different numbers beside them and no way to tell which was which. The title
  // now wraps in full, and the figure sits beside it rather than at the far end
  // of a drawn row where it also overlapped its own bar.
  const ranked = [...lists].sort((left, right) => shareSeen(right) - shareSeen(left));

  container.replaceChildren(
    buildRankedList(
      ranked.map((entry) => ({
        title: toText(entry.title),
        value: `${formatCount(entry.seen)} of ${formatCount(entry.total)}`,
        bar: { value: entry.seen, total: entry.total },
        meta: `${formatPercentage(shareSeen(entry))} seen`,
      })),
      { collapseAfter: RANKED_ROWS_BEFORE_DISCLOSURE },
    ),
  );
  container.append(
    build(
      "p",
      "chart__caption",
      `Progress through ${formatCount(ranked.length)} curated lists, furthest first.`,
    ),
  );
  container.append(
    buildDataTable(
      "Films seen per curated list",
      ["List", "Seen", "Total", "Share"],
      ranked.map((entry) => [
        toText(entry.title),
        formatCount(entry.seen),
        formatCount(entry.total),
        formatPercentage(shareSeen(entry)),
      ]),
    ),
  );
}

/**
 * Renders the complete country table that stands in for a world map.
 *
 * This section used to be the same twenty rows, the same ranking and the same
 * drawing as "Countries, most watched" eleven sections above it, which left it
 * with nothing to say for itself. A map does not answer "which country is
 * highest"; it answers "how much of the library does each country cover", and it
 * answers it for every country at once. So this one is complete rather than a
 * top twenty, and each row carries the country's share of the films the metadata
 * can see. A film credited to two countries counts once under each, so the
 * shares overlap and do not add up to a hundred: the caption says so.
 */
function renderCountriesRanked(stats, coverage) {
  const container = elementById("countries-ranked-chart");
  if (container === null) {
    return;
  }

  const countries = toArray(stats.world_map).filter((entry) => toText(entry?.name) !== null);
  if (countries.length === 0) {
    showEmptyState(container, waitingFor("No country counts yet.", NEEDS_TMDB));
    return;
  }

  const ranked = [...countries].sort(
    (left, right) => (toNumber(right.count) ?? 0) - (toNumber(left.count) ?? 0),
  );
  const resolvedFilms = coverage.tmdb;

  container.replaceChildren(
    buildRankedList(
      ranked.map((entry) => {
        const flag = flagForCountryCode(entry.iso_3166_1);
        const count = toNumber(entry.count);
        // The flag is decoration beside a name that is already there in words,
        // so it is marked as such: on a system with no flag glyphs it renders
        // as the two letters of the country code, which would otherwise read as
        // an abbreviation of the name beside it.
        const title = toText(entry.name);
        return {
          title: flag === "" ? title : `${flag} ${title}`,
          value: formatCount(count),
          bar: { value: count, total: resolvedFilms ?? ranked[0].count },
          meta:
            resolvedFilms === null || resolvedFilms === 0 || count === null
              ? null
              : `${formatShareForReading(count / resolvedFilms)} of ${formatCount(resolvedFilms)}`,
        };
      }),
      { collapseAfter: RANKED_ROWS_BEFORE_DISCLOSURE },
    ),
  );

  container.append(
    build(
      "p",
      "chart__caption",
      `Every one of the ${formatCount(ranked.length)} countries credited on a film you have ` +
        "watched, most first. A film made in two countries is counted under both, so the shares " +
        "overlap.",
    ),
  );

  container.append(
    buildDataTable(
      "Films watched per country of production",
      ["Country", "Code", "Films"],
      ranked.map((entry) => [
        toText(entry.name),
        toText(entry.iso_3166_1) ?? MISSING_VALUE,
        formatCount(entry.count),
      ]),
    ),
  );
}

/* ============================================== Extras: figures at a glance */

/**
 * Formats a share for reading inside a sentence, without rounding away meaning.
 *
 * Plain rounding turns 0.004 into "0%" and 0.998 into "100%", either of which
 * would contradict the sentence it sits in by claiming none or all.
 */
function formatShareForReading(share) {
  if (share < 0.01) {
    return "under 1%";
  }
  if (share > 0.99) {
    return "over 99%";
  }
  return formatPercentage(share);
}

/**
 * Capitalises the first letter, for a phrase that has to open a sentence.
 *
 * `formatShareForReading` returns a word for a share near either end of the
 * scale, so the same value reads as "under 1%" inside a sentence and has to
 * read as "Under 1%" at the start of one.
 */
function startSentence(text) {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/**
 * Says how far the watchlist wait figures can be trusted, or null when fully.
 *
 * The public watchlist pages carry membership but not the date a film was
 * added, so only the export supplies a real added date. Every other date is the
 * day the weekly reader first saw the film, which makes the wait it implies an
 * artefact of when the reader started rather than a measurement.
 * `estimated_date_share` is the fraction built that way, and the contract
 * requires the page to label the wait with it.
 */
function describeWatchlistWaitQuality(watchlist) {
  const share = toNumber(watchlist?.estimated_date_share);

  // A negative share is not a fraction, so it says nothing about the dates and
  // is treated as no share at all. A share above 1 still means every date is a
  // first sighting, so it keeps the strongest caveat below.
  if (share === null || share < 0) {
    return {
      tileNote: "days waiting, from dates of unrecorded origin",
      caveat:
        "This stats file does not record how many watchlist added dates are real, so the " +
        "median wait above cannot be checked. Rebuild the stats with scripts/build_stats.py, " +
        "which writes that share.",
    };
  }

  if (share >= 1) {
    return {
      tileNote: "not a real wait: no added dates are loaded yet",
      caveat:
        "No watchlist film has a real added date yet. Each date is the day the weekly reader " +
        "first saw the film, so the median above measures how long this site has been reading " +
        "the watchlist, not how long the films have waited. Load the real dates by running " +
        "scripts/backfill.py on your Letterboxd export, then rebuild the stats with " +
        "scripts/build_stats.py.",
    };
  }

  if (share > 0) {
    const estimated = formatShareForReading(share);
    return {
      tileNote: `days waiting, though ${estimated} of the dates are estimated`,
      caveat:
        `${startSentence(estimated)} of watchlist films have no real added date. For those, the date used is ` +
        "the day the weekly reader first saw the film, which understates how long they have " +
        "waited, so the median above is a lower bound. Load the remaining dates by running " +
        "scripts/backfill.py on your Letterboxd export, then rebuild the stats.",
    };
  }

  // Every added date came from the export, so the wait needs no qualification.
  return null;
}

/**
 * Renders the extras tiles: rating bias, rewatch rate, watchlist, runtime.
 *
 * The nine figures here are counted from five different populations, so no one
 * label can sit over the grid. Each tile carries its own scope in its own note:
 * the two runtime tiles through the shared coverage rule, and the rest in words,
 * because the coverage block counts films and the watchlist is not the library.
 */
function renderExtrasTiles(extras, coverage) {
  const container = elementById("extras-tiles");
  if (container === null) {
    return;
  }

  const bias = toObject(extras.rating_bias);
  const watchlist = toObject(extras.watchlist);
  const runtime = toObject(extras.runtime);
  const totalMinutes = toNumber(runtime?.total_minutes);
  const waitQuality = describeWatchlistWaitQuality(watchlist);

  // A member with films logged cannot really have watched zero minutes, so a
  // zero here means no runtime was resolved rather than a runtime of nothing.
  const daysWatched =
    totalMinutes === null || totalMinutes === 0 ? MISSING_VALUE : formatCount(totalMinutes / 1440);

  const tiles = [
    {
      // build_rating_bias averages only the films that carry both your rating
      // and a TMDB score, which is neither the rated set nor the resolved set.
      value: formatDecimal(bias?.member_average, 2),
      label: "Your average",
      note: "on the 0.5 to 5 scale, over the films TMDB also scores",
    },
    {
      value: formatDecimal(bias?.tmdb_average, 2),
      label: "TMDB average",
      note: "the same films, on the 0 to 10 scale",
    },
    {
      value: formatSignedDecimal(bias?.delta, 2),
      label: "Rating bias",
      note: "your rating minus the crowd's on those films, both scaled to five",
    },
    {
      value: formatShareFigure(extras.rewatch_rate),
      label: "Rewatch rate",
      note: "share of all entries that were rewatches",
    },
    {
      value: formatCount(watchlist?.size),
      label: "Watchlist",
      note: "films waiting",
    },
    {
      value: formatCount(watchlist?.median_age_days),
      label: "Median wait",
      note: waitQuality === null ? "days a watchlist film has been waiting" : waitQuality.tileNote,
      unresolved: waitQuality !== null,
    },
    {
      // Films watched and then taken off the watchlist leave no trace here, so
      // the denominator is the watchlist as it stands, not everything ever added.
      value: formatShareFigure(watchlist?.conversion_rate),
      label: "Watchlist conversion",
      note: "share of the films still listed that you have watched",
    },
    {
      value: daysWatched,
      label: "Days watched",
      note: "total runtime, counted as whole days",
      basis: "tmdb",
    },
    {
      value: formatCount(runtime?.median),
      label: "Median runtime",
      note: "minutes per film",
      basis: "tmdb",
    },
  ];

  container.replaceChildren(
    ...tiles.map((tile) => {
      const read = scopeTileReading(tile, tile.basis, coverage);
      return buildTile({
        value: read.value,
        label: tile.label,
        note: read.note,
        unresolved: read.unresolved,
      });
    }),
  );

  renderTileScope("extras-tiles-scope", tiles, coverage);

  // The tile note is small print. The caveat is repeated at full size below the
  // grid, because a reader who takes the median at face value is misled.
  const caveatBanner = elementById("watchlist-dates-caveat");
  if (caveatBanner !== null) {
    if (waitQuality === null) {
      caveatBanner.hidden = true;
      caveatBanner.replaceChildren();
    } else {
      caveatBanner.hidden = false;
      caveatBanner.textContent = waitQuality.caveat;
    }
  }
}

/* ================================================== Extras: hot takes */

/**
 * Renders one column of the hot takes, either side of the crowd's opinion.
 *
 * Each row draws the member's rating over the crowd's on the same five-star
 * scale. The bars repeat what the figures line already says in words, so the
 * bars are hidden from assistive technology and the words are the alternative.
 */
function renderContrarianColumn(container, films, emptyMessage, moduleName) {
  if (container === null) {
    return;
  }

  // The source object is carried alongside the read row because the ratings and
  // the delta are read straight from it, and filtering would break an index.
  const rows = films
    .map((entry) => ({ row: readFilmRow(entry, "member_rating"), source: toObject(entry) }))
    .filter((pair) => pair.row !== null && pair.source !== null);

  if (rows.length === 0) {
    showEmptyState(container, emptyMessage);
    return;
  }

  const shown = rows.slice(0, MAXIMUM_LIST_ROWS);
  const list = build("ol", "takes");

  for (const { row, source } of shown) {
    const memberRating = toNumber(source.member_rating);
    const crowdRating = toNumber(source.crowd_rating);
    const delta = toNumber(source.delta);

    const item = build("li", "take");

    const filmLine = build("p", "take__film");
    if (row.href !== null) {
      const link = build("a", null, row.title);
      link.href = row.href;
      link.rel = "noopener";
      filmLine.append(link);
    } else {
      filmLine.append(document.createTextNode(row.title));
    }
    if (row.year !== null) {
      filmLine.append(build("span", "take__year", ` ${formatYear(row.year)}`));
    }
    item.append(filmLine);

    const bars = build("div", "take__bars");
    bars.append(buildRatingDumbbell(memberRating, crowdRating));
    item.append(bars);

    const figures = build("p", "take__figures");
    figures.append(
      document.createTextNode(
        `You ${formatDecimal(memberRating, 1)} · Crowd ${formatDecimal(crowdRating, 1)} · `,
      ),
    );
    // The delta used to be green when positive and blue when negative, which
    // spent the two colours the legend had just assigned to you and the crowd on
    // a second, unrelated meaning inside the same card. The sign already says
    // which way it went, and so does the column it is sitting in.
    figures.append(build("span", "take__delta", `${formatSignedDecimal(delta, 1)} stars`));
    item.append(figures);

    list.append(item);
  }

  container.replaceChildren(list);
  appendTruncationNote(container, shown.length, rows.length, moduleName, "films");
}

/** Renders both hot takes columns, hottest disagreement first in each. */
function renderContrarianIndex(extras) {
  const index = toObject(extras.contrarian_index);
  const missing = {
    reason: "No hot takes yet. Comparing your rating with the crowd's needs TMDB vote averages.",
    fix: FIX_TMDB,
  };

  renderContrarianColumn(
    elementById("contrarian-hotter"),
    toArray(index?.hotter_than_crowd),
    missing,
    "extras.contrarian_index.hotter_than_crowd",
  );
  renderContrarianColumn(
    elementById("contrarian-colder"),
    toArray(index?.colder_than_crowd),
    missing,
    "extras.contrarian_index.colder_than_crowd",
  );

  const legendHost = elementById("contrarian-legend");
  if (legendHost !== null) {
    legendHost.replaceChildren(buildComparisonLegend("Your rating", "The crowd's rating"));
  }
}

/** Renders films marked as liked yet rated low all the same. */
function renderLikedButLow(extras, coverage) {
  const container = elementById("liked-but-low");
  if (container === null) {
    return;
  }

  const rows = toArray(extras.liked_but_low)
    .map((entry) => readFilmRow(entry, "rating"))
    .filter((row) => row !== null);

  if (rows.length === 0) {
    // An empty library and a library where nothing qualifies both arrive here
    // as no rows, and only the coverage block tells them apart.
    showEmptyState(
      container,
      coverage.total === 0 || coverage.total === null
        ? waitingFor("No films here yet. Your history holds no films.", NEEDS_HISTORY)
        : "No films here. Nothing you gave a heart to also carries a low rating.",
    );
    return;
  }

  const shown = rows.slice(0, MAXIMUM_LIST_ROWS);
  container.replaceChildren(
    buildRankedList(
      shown.map((row) => ({
        title: titleWithYear(row),
        href: row.href,
        value: row.value === null ? MISSING_VALUE : `${formatDecimal(row.value, 1)} ★`,
      })),
    ),
  );
  appendTruncationNote(container, shown.length, rows.length, "extras.liked_but_low", "films");
}

/* ================================================== Extras: viewing rhythm */

/** Renders the two headline rhythm figures: days of film, and the longest gap. */
function renderRhythmFacts(extras) {
  const container = elementById("rhythm-facts");
  if (container === null) {
    return;
  }

  const cards = [];

  const life = toObject(extras.life_in_days);
  const lifeDays = toNumber(life?.days);
  if (lifeDays === null) {
    cards.push(
      buildStatCard({
        value: MISSING_VALUE,
        label: "Days of film",
        note: `Total runtime is not known yet. ${NEEDS_TMDB.reason}`,
        fix: NEEDS_TMDB.fix,
      }),
    );
  } else {
    const endsOn = toText(life?.would_end_on);
    cards.push(
      buildStatCard({
        value: formatDecimal(lifeDays, 1),
        unit: "days",
        label: "Days of film",
        note:
          endsOn === null
            ? "Every film you have logged that has a known runtime, played back to back without stopping."
            : `Played back to back from today, every film you have logged that has a known runtime would run until ${formatDate(endsOn)}.`,
      }),
    );
  }

  const drought = toObject(extras.longest_drought);
  const droughtDays = toNumber(drought?.days);
  if (droughtDays === null) {
    cards.push(
      buildStatCard({
        value: MISSING_VALUE,
        label: "Longest drought",
        note: "No gap measured yet. It needs at least two entries that carry a watch date.",
        fix: NEEDS_HISTORY.fix,
      }),
    );
  } else {
    const from = toText(drought?.from);
    const to = toText(drought?.to);
    cards.push(
      buildStatCard({
        value: formatCount(droughtDays),
        unit: droughtDays === 1 ? "day" : "days",
        label: "Longest drought",
        note:
          from === null || to === null
            ? "Your longest run of days with no dated entry between two others."
            : `Your longest run with no dated entry, from ${formatDate(from)} to ${formatDate(to)}.`,
      }),
    );
  }

  container.replaceChildren(...cards);
}

/** Renders how many films are watched on each day of the week. */
function renderWeekdayProfile(extras) {
  const container = elementById("weekday-profile");
  if (container === null) {
    return;
  }

  const rows = toArray(extras.weekday_profile)
    .map((entry) => ({ weekday: toText(entry?.weekday), count: toNumber(entry?.count) }))
    .filter((row) => row.weekday !== null && row.count !== null)
    .sort((left, right) => weekdayRank(left.weekday) - weekdayRank(right.weekday));

  if (rows.length === 0) {
    showEmptyState(container, waitingFor("No weekday profile yet.", NEEDS_WATCH_DATES));
    return;
  }

  const categories = rows.map((row) => ({
    label: row.weekday.slice(0, 3),
    shortLabel: row.weekday.slice(0, 1),
    values: [row.count],
  }));
  const busiest = rows.reduce((peak, row) => (row.count > peak.count ? row : peak), rows[0]);

  replaceWithChart(container, {
    caption: `Films watched per day of the week. Your busiest day is ${busiest.weekday}, with ${formatQuantity(busiest.count, "film", "films")}.`,
    draw: (width) => columnChart(width, categories, { height: COLUMN_CHART_SHORT_HEIGHT }),
    table: buildDataTable(
      "Films watched per day of the week",
      ["Weekday", "Films"],
      rows.map((row) => [row.weekday, formatCount(row.count)]),
    ),
  });
}

/** Returns a weekday's position in the week, putting unknown names last. */
function weekdayRank(weekday) {
  const index = WEEKDAY_ORDER.indexOf(weekday);
  return index < 0 ? WEEKDAY_ORDER.length : index;
}

/** Renders how many films are watched in each month of the year. */
function renderMonthSeasonality(extras) {
  const container = elementById("month-seasonality");
  if (container === null) {
    return;
  }

  const rows = toArray(extras.month_seasonality)
    .map((entry) => ({ month: toNumber(entry?.month), count: toNumber(entry?.count) }))
    .filter((row) => row.month !== null && row.month >= 1 && row.month <= 12 && row.count !== null)
    .sort((left, right) => left.month - right.month);

  if (rows.length === 0) {
    showEmptyState(container, waitingFor("No monthly profile yet.", NEEDS_WATCH_DATES));
    return;
  }

  // Twelve three-letter names do not fit a phone, and stepping them named Jan,
  // Mar, May, Jul, Sep and Nov and left the other six columns anonymous. An
  // initial fits at every width, so every month keeps its own label.
  const categories = rows.map((row) => ({
    label: MONTH_NAMES[row.month - 1],
    shortLabel: MONTH_NAMES[row.month - 1].slice(0, 1),
    values: [row.count],
  }));
  const busiest = rows.reduce((peak, row) => (row.count > peak.count ? row : peak), rows[0]);

  replaceWithChart(container, {
    caption: `Films watched per month, summed across every year. Your busiest month is ${MONTH_FULL_NAMES[busiest.month - 1]}, with ${formatQuantity(busiest.count, "film", "films")}.`,
    draw: (width) => columnChart(width, categories, { height: COLUMN_CHART_SHORT_HEIGHT }),
    table: buildDataTable(
      "Films watched per month of the year",
      ["Month", "Films"],
      rows.map((row) => [MONTH_FULL_NAMES[row.month - 1], formatCount(row.count)]),
    ),
  });
}

/** Renders how long after watching a film it gets logged. */
function renderLoggingLag(extras) {
  const container = elementById("logging-lag");
  if (container === null) {
    return;
  }

  const lag = toObject(extras.logging_lag);
  const median = toNumber(lag?.median_days);
  if (lag === null || median === null) {
    showEmptyState(
      container,
      waitingFor("No logging lag yet. It needs both the logged date and the watched date.", NEEDS_EXPORT),
    );
    return;
  }

  container.replaceChildren(
    buildStatCard({
      value: formatCount(median),
      unit: median === 1 ? "day" : "days",
      label: "Median logging lag",
      note: "Half your entries reach the diary sooner than this, half take longer.",
    }),
  );

  const categories = readDistribution(lag.distribution);
  if (categories.length === 0) {
    return;
  }

  appendChart(container, {
    caption: "Entries grouped by how many days passed between watching and logging.",
    draw: (width) => columnChart(width, categories, { height: COLUMN_CHART_SHORT_HEIGHT }),
    table: buildDataTable(
      "Entries per logging lag bucket",
      ["Days", "Entries"],
      categories.map((category) => [category.label, formatCount(category.values[0])]),
    ),
  });
}

/** Renders one calendar grid per year of watching, most recent first. */
function renderHeatmap(extras) {
  const container = elementById("heatmap");
  if (container === null) {
    return;
  }

  const countsByYear = new Map();
  let totalDays = 0;

  for (const entry of toArray(extras.heatmap)) {
    const date = parseIsoDate(entry?.date);
    const count = toNumber(entry?.count);
    if (date === null || count === null || count <= 0) {
      continue;
    }
    const year = date.getUTCFullYear();
    if (!countsByYear.has(year)) {
      countsByYear.set(year, new Map());
    }
    countsByYear.get(year).set(entry.date, count);
    totalDays += 1;
  }

  if (totalDays === 0) {
    showEmptyState(
      container,
      waitingFor("No watch dates yet, so there is nothing to plot by day.", NEEDS_HISTORY),
    );
    return;
  }

  const years = [...countsByYear.keys()].sort((left, right) => right - left);
  const shown = years.slice(0, MAXIMUM_HEATMAP_YEARS);

  // The key goes above the grids rather than after the last of them. Below it
  // the 2026 grid sat about fourteen hundred pixels from the key that explains
  // its shades, which is no use to anyone reading down the page.
  container.replaceChildren(buildHeatmapLegend());

  let firstYear = true;
  for (const year of shown) {
    const countsByDate = countsByYear.get(year);
    const block = build("div", "heatmap-year");
    block.append(build("p", "heatmap-year__label", String(year)));

    // A full year is 53 weeks wide and does not fit a phone, so the grid
    // scrolls. Overlay scrollbars are invisible at rest, so without this line a
    // reader on a phone sees January to May and concludes the year ended there.
    // The edge of the scroller is also faded in CSS while there is more to see.
    const scroller = build("div", "scroll-x");
    scroller.append(calendarHeatmap(year, countsByDate));
    block.append(scroller);
    trackScrollable(scroller);

    if (firstYear) {
      // Hidden by CSS whenever the grid fits, so it never tells a reader on a
      // wide screen to scroll something that has nothing to scroll to.
      block.append(
        build("p", "chart__caption heatmap-hint", "Scroll a year sideways to reach December."),
      );
      firstYear = false;
    }

    // build_heatmap counts one diary entry per square, so two viewings of the
    // same film on one day are two entries and not one film.
    const entryCount = [...countsByDate.values()].reduce((sum, count) => sum + count, 0);
    block.append(
      build(
        "p",
        "chart__caption",
        `${formatQuantity(entryCount, "entry", "entries")} across ${formatQuantity(countsByDate.size, "day", "days")} in ${year}.`,
      ),
    );

    container.append(block);
  }

  if (shown.length < years.length) {
    appendNote(container, `Showing the ${shown.length} most recent of ${years.length} years.`);
  }
}

/* ========================================================= Extras: your taste */

/** Renders the year by year average rating, to show whether taste has hardened. */
function renderRatingDrift(extras, filmsByYear) {
  const container = elementById("rating-drift-chart");
  if (container === null) {
    return;
  }

  const points = toArray(extras.rating_drift)
    .filter((entry) => toNumber(entry?.year) !== null && toNumber(entry?.average) !== null)
    .sort((left, right) => toNumber(left.year) - toNumber(right.year))
    .map((entry) => {
      const year = toNumber(entry.year);
      return {
        label: String(year),
        value: toNumber(entry.average),
        count: filmsByYear.get(year) ?? null,
      };
    });

  if (points.length === 0) {
    showEmptyState(
      container,
      waitingFor("No yearly averages yet. An average needs entries that carry both a rating and a watch date.", NEEDS_HISTORY),
    );
    return;
  }

  const first = points[0];
  const last = points[points.length - 1];

  // A single year is a reading, not a drift. Saying it has "held steady, from
  // 3.61 to 3.61" would be a claim about a change that has not happened yet.
  const movement =
    points.length === 1
      ? `One year of ratings so far: ${first.label}, averaging ${first.value.toFixed(2)}.`
      : `From ${first.label} to ${last.label} it has ` +
        `${last.value - first.value > 0.05 ? "risen" : last.value - first.value < -0.05 ? "fallen" : "held steady"}, ` +
        `from ${first.value.toFixed(2)} to ${last.value.toFixed(2)}.`;

  // A year averaged over a handful of films will swing on one opinion, and the
  // line drawn through it reads as a trend. Naming the thin years is the
  // difference between a chart that shows drift and one that shows an artefact.
  const thin = points.filter((point) => point.count !== null && point.count < THIN_YEAR_FILMS);
  const thinNote =
    thin.length === 0
      ? ""
      : ` ${joinNames(thin.map((point) => `${point.label} rests on ${formatQuantity(point.count, "film", "films")}`))}.`;

  replaceWithChart(container, {
    caption: `Average rating by year, on the ${RATING_SCALE_MINIMUM.toFixed(1)} to ${RATING_SCALE_MAXIMUM.toFixed(1)} scale. ${movement}${thinNote}`,
    draw: (width) => ratingLineChart(width, points),
    table: buildDataTable(
      "Average rating given in each year",
      ["Year", "Average rating", "Films rated"],
      points.map((point) => [
        point.label,
        point.value.toFixed(2),
        point.count === null ? MISSING_VALUE : formatCount(point.count),
      ]),
    ),
  });
}

/**
 * Returns true when a rating sits on a half star rather than a whole one.
 *
 * Ratings come in half-star steps, so doubling one gives a whole number. An odd
 * result means the rating fell on a half star.
 */
function isHalfStar(rating) {
  return Math.round(rating * 2) % 2 === 1;
}

/** Renders how often ratings land on a half star rather than a whole one. */
function renderHalfStarUsage(extras, coverage) {
  const container = elementById("half-star-usage");
  if (container === null) {
    return;
  }

  const usage = toObject(extras.half_star_usage);
  const share = toNumber(usage?.half_star_share);

  const rows = toArray(usage?.distribution)
    .map((entry) => ({ rating: toNumber(entry?.rating), count: toNumber(entry?.count) }))
    .filter((row) => row.rating !== null && row.count !== null)
    .sort((left, right) => left.rating - right.rating);

  if (share === null && rows.length === 0) {
    showEmptyState(
      container,
      coverage.total === 0 || coverage.total === null
        ? waitingFor("No half-star figures yet. Your history holds no films.", NEEDS_HISTORY)
        : "No half-star figures yet. They need films that carry a rating, and none of yours does.",
    );
    return;
  }

  container.replaceChildren(
    buildStatCard({
      value: formatShareFigure(share),
      label: "Ratings on a half star",
      note: "The rest land on a whole star. A high share means you use the full ten-step scale.",
    }),
  );

  if (rows.length === 0) {
    return;
  }

  const categories = rows.map((row) => ({
    label: row.rating.toFixed(1),
    values: [row.count],
    classNames: [isHalfStar(row.rating) ? "chart__bar" : "chart__bar--muted"],
  }));

  appendChart(container, {
    caption:
      "Ratings given at each step. The bright columns are the half stars, the dim ones the whole stars.",
    draw: (width) => columnChart(width, categories, { height: COLUMN_CHART_SHORT_HEIGHT }),
    table: buildDataTable(
      "Ratings given at each half-star step",
      ["Rating", "Times given", "Step"],
      rows.map((row) => [
        row.rating.toFixed(1),
        formatCount(row.count),
        isHalfStar(row.rating) ? "Half star" : "Whole star",
      ]),
    ),
  });
}

/**
 * Adds the unit to a runtime bucket name, in the place English puts it.
 *
 * "180 and over" takes the unit in the middle, not at the end: "180 and over
 * min" is not a phrase anyone says.
 */
function withMinutes(label) {
  const trailing = /^(.*?)(\s+(?:and over|and above|or more))$/i.exec(String(label));
  return trailing === null ? `${label} min` : `${trailing[1]} min${trailing[2]}`;
}

/** Renders whether longer films get better or worse ratings. */
function renderRatingVersusRuntime(extras) {
  const container = elementById("rating-vs-runtime");
  if (container === null) {
    return;
  }

  const pairing = toObject(extras.rating_vs_runtime);
  const correlation = toNumber(pairing?.correlation);

  const buckets = toArray(pairing?.buckets)
    .map((entry) => {
      const source = toObject(entry);
      if (source === null) {
        return null;
      }
      const label = toText(source.range) ?? toText(source.label);
      const average = toNumber(source.average_rating);
      return label === null || average === null
        ? null
        : { label, average, films: toNumber(source.films) };
    })
    .filter((bucket) => bucket !== null);

  if (correlation === null && buckets.length === 0) {
    showEmptyState(container, {
      reason:
        "No runtime comparison yet. It pairs your rating with the film's runtime, and TMDB " +
        "carries the runtime.",
      fix: FIX_TMDB,
    });
    return;
  }

  const leaning =
    correlation === null
      ? "Not enough paired ratings and runtimes to say."
      : correlation > 0.1
        ? "Longer films tend to get the higher ratings from you."
        : correlation < -0.1
          ? "Shorter films tend to get the higher ratings from you."
          : "Runtime barely moves your rating either way.";

  container.replaceChildren(
    buildStatCard({
      value: formatSignedDecimal(correlation, 2),
      label: "Runtime and rating",
      note: `On a scale from -1 to +1. ${leaning}`,
    }),
  );

  if (buckets.length === 0) {
    return;
  }

  // Four of these six averages sit within three hundredths of each other. Drawn
  // as bars from zero they were pixel-identical; as dots on the rating axis the
  // two that differ are the two that stand out.
  if (libraryAverageRating !== null) {
    container.append(buildRatingScaleKey(libraryAverageRating));
  }
  container.append(
    buildRankedList(
      buckets.map((bucket) => ({
        title: withMinutes(bucket.label),
        value: formatDecimal(bucket.average, 2),
        dot: { value: bucket.average, reference: libraryAverageRating },
        meta:
          bucket.films === null
            ? null
            : `over ${formatQuantity(bucket.films, "film", "films")}`,
      })),
    ),
  );
  container.append(
    build(
      "p",
      "chart__caption",
      "Average rating per runtime bucket, in runtime order, on the " +
        `${RATING_SCALE_MINIMUM.toFixed(1)} to ${RATING_SCALE_MAXIMUM.toFixed(1)} scale.`,
    ),
  );
  container.append(
    buildDataTable(
      "Average rating per runtime bucket",
      ["Runtime in minutes", "Average rating", "Films"],
      buckets.map((bucket) => [
        bucket.label,
        formatDecimal(bucket.average, 2),
        formatCount(bucket.films),
      ]),
    ),
  );
}

/* ========================================================= Extras: your reach */

/** Renders how well known the films watched are, by TMDB vote count. */
function renderObscurity(extras) {
  const container = elementById("obscurity");
  if (container === null) {
    return;
  }

  const obscurity = toObject(extras.obscurity);
  const median = toNumber(obscurity?.median_vote_count);
  const quartiles = toArray(obscurity?.quartiles)
    .map((value) => toNumber(value))
    .filter((value) => value !== null);

  if (median === null && quartiles.length === 0) {
    showEmptyState(container, {
      reason: "No obscurity figures yet. TMDB vote counts are the measure.",
      fix: FIX_TMDB,
    });
  } else {
    const card = buildStatCard({
      value: formatCount(median),
      unit: "votes",
      label: "Median film",
      note:
        quartiles.length === 3
          ? `Half the films you have seen have fewer TMDB votes than this. The quartiles run ${formatCount(quartiles[0])}, ${formatCount(quartiles[1])}, ${formatCount(quartiles[2])}.`
          : "Half the films you have seen have fewer TMDB votes than this.",
    });
    container.replaceChildren(card);
  }

  renderFilmSideList(
    elementById("obscurity-most-obscure"),
    toArray(obscurity?.most_obscure),
    "vote",
    waitingFor("No deep cuts listed yet.", NEEDS_TMDB),
    "extras.obscurity.most_obscure",
  );
  renderFilmSideList(
    elementById("obscurity-most-popular"),
    toArray(obscurity?.most_popular),
    "vote",
    waitingFor("No crowd favourites listed yet.", NEEDS_TMDB),
    "extras.obscurity.most_popular",
  );
}

/**
 * Renders a short ranked list of films with one figure beside each.
 *
 * `unit` names the figure in the singular. A count of one keeps it; anything else
 * is pluralised, so a film with a single TMDB vote reads "1 vote".
 */
function renderFilmSideList(container, films, unit, emptyMessage, moduleName) {
  if (container === null) {
    return;
  }

  const rows = films.map((entry) => readFilmRow(entry)).filter((row) => row !== null);
  if (rows.length === 0) {
    showEmptyState(container, emptyMessage);
    return;
  }

  const shown = rows.slice(0, MAXIMUM_LIST_ROWS);
  container.replaceChildren(
    buildRankedList(
      shown.map((row) => ({
        title: titleWithYear(row),
        href: row.href,
        value: row.value === null ? MISSING_VALUE : formatQuantity(row.value, unit, `${unit}s`),
      })),
    ),
  );
  appendTruncationNote(container, shown.length, rows.length, moduleName, "films");
}

/** Renders how long after release a film is usually watched. */
function renderReleaseRecency(extras, filmsByYear) {
  const container = elementById("release-recency");
  if (container === null) {
    return;
  }

  const recency = toObject(extras.release_recency);
  const medianDays = toNumber(recency?.median_days_after_release);

  const points = toArray(recency?.by_year)
    .map((entry) => ({
      year: toNumber(entry?.year),
      medianDays: toNumber(entry?.median_days),
      films: filmsByYear.get(toNumber(entry?.year)) ?? null,
    }))
    .filter((point) => point.year !== null && point.medianDays !== null)
    .sort((left, right) => left.year - right.year);

  if (medianDays === null && points.length === 0) {
    showEmptyState(container, {
      reason:
        "No wait measured yet. It needs a watch date on the entry and a release date from TMDB.",
      fix: "Run scripts/backfill.py and scripts/enrich_tmdb.py, then rebuild the stats.",
    });
    return;
  }

  const years = medianDays === null ? null : medianDays / 365.25;
  container.replaceChildren(
    buildStatCard({
      value: formatCount(medianDays),
      unit: medianDays === 1 ? "day" : "days",
      label: "Typical wait after release",
      note:
        years === null
          ? "How long after a film came out you tend to watch it."
          : `About ${formatDecimal(years, 1)} years. Half the films you watch are older than this when you see them.`,
    }),
  );

  if (points.length === 0) {
    return;
  }

  const categories = points.map((point) => ({
    label: String(point.year),
    values: [point.medianDays],
    valueText: formatQuantity(point.medianDays, "day", "days"),
    shortValueText: formatCount(point.medianDays),
  }));

  // One year of this chart runs to ninety years after release and towers over
  // the rest. It rests on a single film, and a column that tall with nothing
  // saying so reads as a trend rather than as one old film watched in January.
  const thin = points.filter((point) => point.films !== null && point.films < THIN_YEAR_FILMS);
  const thinNote =
    thin.length === 0
      ? ""
      : ` ${joinNames(thin.map((point) => `${point.year} rests on ${formatQuantity(point.films, "film", "films")}`))}.`;

  appendChart(container, {
    caption: `Median days between a film's release and the day you watched it, by watch year.${thinNote}`,
    draw: (width) => columnChart(width, categories, { height: COLUMN_CHART_SHORT_HEIGHT }),
    table: buildDataTable(
      "Median days after release, by watch year",
      ["Watch year", "Median days after release", "Films watched"],
      points.map((point) => [
        String(point.year),
        formatCount(point.medianDays),
        point.films === null ? MISSING_VALUE : formatCount(point.films),
      ]),
    ),
  });
}

/**
 * Renders the runtime distribution when the file carries labelled buckets.
 *
 * The contract leaves the bucket shape open, so a bucket is only drawn when it
 * names itself and carries a count. Anything else falls through to the empty
 * state rather than being guessed at.
 */
function renderRuntimeDistribution(extras) {
  const container = elementById("runtime-distribution");
  if (container === null) {
    return;
  }

  const runtime = toObject(extras.runtime);
  const categories = readDistribution(runtime?.distribution);

  if (categories.length === 0) {
    showEmptyState(container, waitingFor("No runtime buckets yet.", NEEDS_TMDB));
    return;
  }

  replaceWithChart(container, {
    caption: "Films grouped by runtime.",
    draw: (width) => columnChart(width, categories),
    table: buildDataTable(
      "Films per runtime bucket",
      ["Runtime", "Films"],
      categories.map((category) => [category.label, formatCount(category.values[0])]),
    ),
  });
}

/** Renders the shortest, longest, oldest, and newest films watched. */
function renderExtremes(extras) {
  const container = elementById("extremes");
  if (container === null) {
    return;
  }

  const extremes = toObject(extras.extremes);
  const definitions = [
    { key: "shortest", term: "Shortest film", unit: "runtime" },
    { key: "longest", term: "Longest film", unit: "runtime" },
    { key: "oldest", term: "Oldest film", unit: "year" },
    { key: "newest", term: "Newest film", unit: "year" },
  ];

  const facts = [];
  for (const { key, term, unit } of definitions) {
    const row = readFilmRow(extremes?.[key]);
    if (row === null) {
      continue;
    }
    const source = toObject(extremes[key]);
    const runtime = toNumber(source?.runtime);
    const meta =
      unit === "runtime"
        ? runtime === null
          ? ""
          : `(${formatQuantity(runtime, "minute", "minutes")})`
        : row.year === null
          ? ""
          : `(${formatYear(row.year)})`;
    facts.push({ term, value: row.title, meta, href: row.href });
  }

  if (facts.length === 0) {
    showEmptyState(container, {
      reason:
        "No extremes yet. The shortest and longest need runtimes from TMDB, the oldest and " +
        "newest need release years from your history.",
      fix: "Run scripts/enrich_tmdb.py and scripts/backfill.py, then rebuild the stats.",
    });
    return;
  }

  container.replaceChildren(buildFactList(facts));
}

/** Renders the decades with no film watched, as a row of chips. */
function renderDecadeGaps(extras, coverage) {
  const container = elementById("decade-gaps");
  if (container === null) {
    return;
  }

  const gaps = toArray(extras.decade_gaps)
    .map((decade) => toNumber(decade))
    .filter((decade) => decade !== null)
    .sort((left, right) => left - right);

  if (gaps.length === 0) {
    // "No gaps" is a claim about a run of decades, and a library with no films
    // has no such run. Both cases produce an empty list.
    showEmptyState(
      container,
      coverage.total === 0 || coverage.total === null
        ? waitingFor("No decades to check yet. Your history holds no films.", NEEDS_HISTORY)
        : "No gaps. Every decade from your oldest film to now has at least one film watched.",
    );
    return;
  }

  const chips = build("ul", "chips");
  for (const decade of gaps) {
    chips.append(build("li", "chip", `${formatYear(decade)}s`));
  }
  container.replaceChildren(chips);
}

/* ======================================================== Extras: the people */

/** Renders one column of directors ranked by the average rating you give them. */
function renderDirectorLuck(container, directors, emptyMessage, moduleName) {
  if (container === null) {
    return;
  }

  const entries = toArray(directors)
    .map((entry) => ({
      name: toText(entry?.name),
      films: toNumber(entry?.films),
      average: toNumber(entry?.average_rating),
    }))
    .filter((entry) => entry.name !== null && entry.average !== null);

  if (entries.length === 0) {
    showEmptyState(container, emptyMessage);
    return;
  }

  // A list of links rather than a bar chart, for the reason buildRankedList
  // gives: a drawn SVG label cannot carry a link to the director's page. The
  // two columns used to be told apart by the colour of their bars, which spent
  // the same green and blue the hot takes use for you and the crowd. The
  // headings already say which column is which, and the dots now sit either
  // side of your own average, which says it again in the drawing.
  const shown = entries.slice(0, MAXIMUM_LIST_ROWS);
  container.replaceChildren();
  if (libraryAverageRating !== null) {
    container.append(buildRatingScaleKey(libraryAverageRating));
  }
  container.append(
    buildRankedList(
      shown.map((entry) => ({
        title: entry.name,
        href: personUrl("director", entry.name),
        value: `${formatDecimal(entry.average, 2)} ★`,
        dot: { value: entry.average, reference: libraryAverageRating },
        meta:
          entry.films === null
            ? null
            : `over ${formatQuantity(entry.films, "rated film", "rated films")}`,
      })),
    ),
  );
  appendTruncationNote(container, shown.length, entries.length, moduleName, "directors");
}

/** Renders the directors you rate highest and the ones you rate lowest. */
function renderDirectorLuckPair(extras) {
  const missing = {
    reason:
      "No director averages yet. Each one needs directing credits from TMDB and enough rated " +
      "films behind the average to be worth ranking.",
    fix: FIX_TMDB,
  };
  renderDirectorLuck(
    elementById("lucky-director"),
    extras.lucky_director,
    missing,
    "extras.lucky_director",
  );
  renderDirectorLuck(
    elementById("unlucky-director"),
    extras.unlucky_director,
    missing,
    "extras.unlucky_director",
  );
}

/** Renders actors seen often but rarely near the top of the billing. */
function renderBackgroundActor(extras) {
  const container = elementById("background-actor");
  if (container === null) {
    return;
  }

  const actors = toArray(extras.background_actor)
    .map((entry) => ({
      name: toText(entry?.name),
      count: toNumber(entry?.count),
      billing: toNumber(entry?.median_billing),
    }))
    .filter((entry) => entry.name !== null);

  if (actors.length === 0) {
    showEmptyState(container, {
      reason: "No background faces yet. They need cast credits with a billing order.",
      fix: FIX_TMDB,
    });
    return;
  }

  const shown = actors.slice(0, MAXIMUM_LIST_ROWS);
  container.replaceChildren(
    buildRankedList(
      shown.map((entry) => ({
        title: entry.name,
        href: personUrl("actor", entry.name),
        value: formatQuantity(entry.count, "film", "films"),
        meta:
          entry.billing === null
            ? null
            : `usually ${formatOrdinal(entry.billing)} on the cast list`,
      })),
    ),
  );
  appendTruncationNote(container, shown.length, actors.length, "extras.background_actor", "actors");
}

const CREW_ROLES = [
  { key: "composer", label: "Composers" },
  { key: "cinematographer", label: "Cinematographers" },
  { key: "editor", label: "Editors" },
  { key: "writer", label: "Writers" },
];

/** Renders the crew seen most often, one short list per role. */
function renderCrewMostWatched(extras) {
  const container = elementById("crew-most-watched");
  if (container === null) {
    return;
  }

  const crew = toObject(extras.crew_most_watched);
  const groups = [];

  for (const { key, label } of CREW_ROLES) {
    const people = toArray(crew?.[key])
      .map((entry) => ({ name: toText(entry?.name), count: toNumber(entry?.count) }))
      .filter((person) => person.name !== null);
    if (people.length === 0) {
      continue;
    }

    const column = build("div", "pair__column");
    column.append(buildSubheading(5, label));
    const shown = people.slice(0, MAXIMUM_LIST_ROWS);
    column.append(
      buildRankedList(
        shown.map((person) => ({
          title: person.name,
          href: personUrl(key, person.name),
          value: formatQuantity(person.count, "film", "films"),
        })),
        // Four roles of ten stack into forty rows on a phone, which is most of a
        // section on its own. Five each still shows the shape of every role.
        { collapseAfter: CREW_ROWS_BEFORE_DISCLOSURE },
      ),
    );
    appendTruncationNote(
      column,
      shown.length,
      people.length,
      `extras.crew_most_watched.${key}`,
      label.toLowerCase(),
    );
    groups.push(column);
  }

  if (groups.length === 0) {
    showEmptyState(container, {
      reason: "No crew counts yet. They need crew credits from TMDB.",
      fix: FIX_TMDB,
    });
    return;
  }

  const grid = build("div", "pair");
  grid.append(...groups);
  container.replaceChildren(grid);
}

/** Renders how much of each director's filmography has been seen. */
function renderDirectorCompleteness(extras) {
  const container = elementById("director-completeness");
  if (container === null) {
    return;
  }

  const directors = toArray(extras.director_completeness).filter(
    (entry) => toText(entry?.name) !== null,
  );
  if (directors.length === 0) {
    // Not enrich_tmdb.py. A director's whole body of work is not a fact about
    // any one film, so no film's credits carry it. It comes from the
    // person_credits table, which scripts/enrich_people_and_collections.py
    // writes, and only for directors with at least two films in the history.
    showEmptyState(container, {
      reason:
        "No filmography counts yet. Each one needs the director's full body of work from TMDB, " +
        "which no film's credits carry, and it is downloaded only for directors with at least " +
        "two films in your history.",
      fix: FIX_PEOPLE_AND_COLLECTIONS,
    });
    return;
  }

  const ranked = [...directors].sort((left, right) => {
    const leftShare = (toNumber(left.seen) ?? 0) / Math.max(1, toNumber(left.filmography) ?? 0);
    const rightShare = (toNumber(right.seen) ?? 0) / Math.max(1, toNumber(right.filmography) ?? 0);
    return rightShare - leftShare;
  });

  // A list of links rather than a progress chart, for the reason buildRankedList
  // gives: a drawn SVG label cannot carry a link to the director's page.
  const shown = ranked.slice(0, MAXIMUM_BAR_ROWS);
  container.replaceChildren(
    buildRankedList(
      shown.map((entry) => ({
        title: toText(entry.name),
        href: personUrl("director", entry.name),
        value: `${formatCount(entry.seen)} of ${formatCount(entry.filmography)}`,
        bar: { value: entry.seen, total: entry.filmography },
        meta: `${formatPercentage(shareSeen({ seen: entry.seen, total: entry.filmography }))} seen`,
      })),
      { collapseAfter: RANKED_ROWS_BEFORE_DISCLOSURE },
    ),
  );
  appendTruncationNote(
    container,
    shown.length,
    ranked.length,
    "extras.director_completeness",
    "directors",
  );
}

/* ======================================================== Extras: title words */

const SMALLEST_WORD_SIZE_REM = 0.875;
const LARGEST_WORD_SIZE_REM = 1.5;

/**
 * Renders the words that recur most in the titles watched.
 *
 * The size follows the square root of the count rather than the count itself.
 * A word is read by the area it covers, not by its height, so mapping a count
 * straight onto a font size made a word seen 23 times look about six times the
 * weight of one seen 5 times when it is really about four and a half. The top
 * of the range is also pulled in, because eight sizes spread over more than
 * twice the body size stopped the line being readable as a line.
 *
 * The size stays decoration either way: every word carries its own count in
 * text beside it, so nothing here depends on judging a size by eye.
 */
function renderTitleWords(extras) {
  const container = elementById("title-words-cloud");
  if (container === null) {
    return;
  }

  const words = toArray(extras.title_words)
    .map((entry) => ({ word: toText(entry?.word), count: toNumber(entry?.count) }))
    .filter((entry) => entry.word !== null && entry.count !== null && entry.count > 0)
    .sort((left, right) => right.count - left.count);

  if (words.length === 0) {
    showEmptyState(
      container,
      waitingFor("No title words yet. They are counted from the titles in your history.", NEEDS_HISTORY),
    );
    return;
  }

  const shown = words.slice(0, MAXIMUM_TITLE_WORDS);
  const highest = shown[0].count;
  const lowest = shown[shown.length - 1].count;
  const span = Math.max(1e-9, Math.sqrt(highest) - Math.sqrt(lowest));

  const list = build("ul", "wordcloud");
  for (const entry of shown) {
    const item = build("li", "wordcloud__item");
    const share = (Math.sqrt(entry.count) - Math.sqrt(lowest)) / span;
    const size = SMALLEST_WORD_SIZE_REM + share * (LARGEST_WORD_SIZE_REM - SMALLEST_WORD_SIZE_REM);
    item.style.fontSize = `${size.toFixed(3)}rem`;
    item.append(build("span", "wordcloud__word", entry.word));
    item.append(build("span", "wordcloud__count", formatCount(entry.count)));
    list.append(item);
  }

  container.replaceChildren(list);
  appendNote(
    container,
    `Word size follows how often the word appears, from ${formatCount(lowest)} to ${formatCount(highest)} titles. ` +
      "The count beside each word is the figure; the size is only the shape of it.",
  );
  appendTruncationNote(container, shown.length, words.length, "extras.title_words", "words");
}

/* ============================================================= Page assembly */

/** Renders every extras block, or an empty state for each when there is none. */
function renderExtras(stats, coverage) {
  const extras = toObject(stats.extras) ?? {};

  // How many films each watch year holds. stats.extras.rating_drift carries an
  // average per year and no count behind it, and a drift chart that cannot say
  // how many films a point rests on cannot be read.
  const filmsByYear = new Map();
  for (const entry of toArray(stats.by_year)) {
    const year = toNumber(entry?.year);
    const films = toNumber(entry?.films);
    if (year !== null && films !== null) {
      filmsByYear.set(year, films);
    }
  }

  renderExtrasTiles(extras, coverage);

  renderContrarianIndex(extras);
  renderLikedButLow(extras, coverage);

  renderRhythmFacts(extras);
  renderWeekdayProfile(extras);
  renderMonthSeasonality(extras);
  renderLoggingLag(extras);
  renderHeatmap(extras);

  renderRatingDrift(extras, filmsByYear);
  renderHalfStarUsage(extras, coverage);
  renderRatingVersusRuntime(extras);

  renderObscurity(extras);
  renderReleaseRecency(extras, filmsByYear);
  renderRuntimeDistribution(extras);
  renderExtremes(extras);
  renderDecadeGaps(extras, coverage);

  renderDirectorLuckPair(extras);
  renderBackgroundActor(extras);
  renderCrewMostWatched(extras);
  renderDirectorCompleteness(extras);

  renderTitleWords(extras);
}

/** Fills the page title and the line of provenance under it. */
function renderPageHeader(stats) {
  const username = toText(stats.username) ?? "This account";
  const title = elementById("page-title");
  const meta = elementById("page-meta");

  if (title !== null) {
    title.textContent = username;
    document.title = `Letterboxd stats for ${username}`;
  }

  if (meta === null) {
    return;
  }

  const filmCount = toNumber(stats.totals?.films);
  const parts = [];
  if (filmCount !== null) {
    parts.push(`${formatQuantity(filmCount, "film", "films")} tracked`);
  }
  parts.push(`generated ${formatDate(stats.generated_at)}`);

  meta.replaceChildren(document.createTextNode(`${parts.join(" · ")} · profile: `));

  const profileLink = build("a", null, `letterboxd.com/${username}`);
  profileLink.href = `https://letterboxd.com/${encodeURIComponent(username)}/`;
  profileLink.rel = "noopener";
  meta.append(profileLink);
}

/** Renders every section of the page from one stats file. */
function render(stats) {
  // Read once and passed down: every module that covers part of the library has
  // to state the same denominators.
  const coverage = readCoverage(stats);

  // Read once and kept, for the same reason: every ranked module the page
  // shortens has to name a total it did not measure from a shortened array.
  rowTotals = toObject(stats.row_totals) ?? {};

  // Read once and kept, for the reference line every ranking of averages draws.
  libraryAverageRating = toNumber(stats.extras?.rating_bias?.member_average);

  renderPageHeader(stats);
  renderTotals(stats, coverage);
  renderCoverageSummary(coverage);
  renderCoverageNotes(coverage);
  renderByYear(stats);
  renderDecades(stats);
  renderRankedPair(stats, "genres", "genre");
  renderRankedPair(stats, "countries", "country");
  renderRankedPair(stats, "languages", "language");
  renderCast(stats);
  renderDirectors(stats);
  renderStudios(stats);
  renderCollections(stats);
  renderListProgress(stats);
  renderCountriesRanked(stats, coverage);
  renderExtras(stats, coverage);
  renderClosing(stats, coverage);

  // After the page has its sections, not before: the observer needs something
  // to observe, and the jump list needs to know how tall it really is.
  trackCurrentSection();
}

/* ================================================== Finding your way around */

/**
 * Marks the section the reader is currently in, in the jump list.
 *
 * Seventeen links sat above a document forty screens tall on a phone with
 * nothing at all saying which of them you were looking at. The mark is
 * aria-current rather than a class, so it is announced as well as seen, and the
 * link is scrolled into view inside the bar when the bar is a scroller, so the
 * current section is never the one link parked off the right edge.
 */
function trackCurrentSection() {
  const links = [...document.querySelectorAll(".section-nav__list a[href^='#']")];
  if (links.length === 0 || typeof IntersectionObserver !== "function") {
    return;
  }

  const linksByTargetId = new Map();
  for (const link of links) {
    linksByTargetId.set(decodeURIComponent(link.hash.slice(1)), link);
  }

  const sections = [...document.querySelectorAll("main section[id]")].filter((section) =>
    linksByTargetId.has(section.id),
  );
  const visible = new Set();
  let current = null;

  const mark = () => {
    // The topmost section still on screen is the one being read. Several are
    // visible at once on a tall window, and the last one to scroll in is not
    // the one under the reader's eye.
    const next =
      sections.find((section) => visible.has(section.id))?.id ??
      (visible.size === 0 ? current : null);

    if (next === null || next === current) {
      return;
    }
    current = next;

    for (const [id, link] of linksByTargetId) {
      if (id === current) {
        link.setAttribute("aria-current", "location");
      } else {
        link.removeAttribute("aria-current");
      }
    }

    const active = linksByTargetId.get(current);
    const list = active?.closest(".section-nav__list");
    // Only when the bar really scrolls. Below the wrapping width it does, and
    // that is exactly where a link can be three screen-widths off to the right.
    if (list && list.scrollWidth > list.clientWidth + 4) {
      const offset = active.offsetLeft - list.clientWidth / 2 + active.offsetWidth / 2;
      list.scrollTo({ left: Math.max(0, offset), behavior: "auto" });
    }
  };

  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          visible.add(entry.target.id);
        } else {
          visible.delete(entry.target.id);
        }
      }
      mark();
    },
    // The band starts under the sticky bar and stops well short of the fold, so
    // a section only counts as current once it is genuinely the thing on screen.
    { rootMargin: "-96px 0px -55% 0px", threshold: 0 },
  );

  for (const section of sections) {
    observer.observe(section);
  }
}

/**
 * Turns the jump list into a disclosure on a screen too narrow to hold it.
 *
 * At 360px the seventeen links ran to 1,657px inside a 320px strip, so four of
 * them were visible, the marker for the whole second half of the page sat about
 * three screen-widths to the right, and nothing said the strip scrolled at all.
 * A closed list with one button reaches any section in two presses.
 *
 * The button starts hidden in the markup and is revealed here, so a reader with
 * scripting switched off keeps the scrolling strip rather than a button that
 * cannot open anything.
 */
function setUpSectionNavigationToggle() {
  const nav = document.querySelector(".section-nav");
  const toggle = elementById("section-nav-toggle");
  const list = elementById("section-nav-list");
  if (nav === null || toggle === null || list === null) {
    return;
  }

  toggle.hidden = false;
  nav.dataset.collapsible = "true";

  const setOpen = (open) => {
    nav.dataset.open = open ? "true" : "false";
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
  };

  setOpen(false);

  toggle.addEventListener("click", () => {
    setOpen(toggle.getAttribute("aria-expanded") !== "true");
  });

  // Following a link inside the open list closes it, or the reader lands on a
  // heading with the list still covering it.
  //
  // This has to run before trackSectionNavigationHeight's own click handler, or
  // the anchor offset is measured against the open menu: the menu is 442px tall
  // on a phone, so every jump landed more than four hundred pixels below the
  // heading it was aimed at and then the menu closed, leaving a screenful of
  // nothing above it. Both are capture-phase listeners on the document, so they
  // fire in the order they were added, and setUpSectionNavigationToggle is
  // called first at the bottom of this file.
  document.addEventListener(
    "click",
    (event) => {
      const target = event.target;
      if (
        target instanceof Element &&
        target.closest(".section-nav__list a") !== null
      ) {
        setOpen(false);
      }
    },
    true,
  );

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && toggle.getAttribute("aria-expanded") === "true") {
      setOpen(false);
      toggle.focus();
    }
  });
}

/**
 * Shows the way back to the top once the reader is well past it.
 *
 * On a phone this document runs to tens of thousands of pixels, and it had no
 * return path of any kind: no way back to the jump list, and nothing at the end
 * but a truncation note and a disclaimer.
 */
function setUpBackToTop() {
  const control = elementById("back-to-top");
  const header = document.querySelector(".page-header");
  if (control === null || header === null || typeof IntersectionObserver !== "function") {
    return;
  }

  const observer = new IntersectionObserver(
    ([entry]) => {
      control.hidden = entry.isIntersecting;
    },
    { threshold: 0 },
  );
  observer.observe(header);
}

/**
 * Writes the closing note: what the page counted, and what it counted it from.
 *
 * The page used to end on "Showing the top 40 of 1,120 words" and a legal line.
 * The four coverage counts are the frame for everything above, and restating
 * them at the end is the one place a reader who has scrolled the whole way can
 * check a figure against its denominator without going back to the top.
 */
function renderClosing(stats, coverage) {
  const container = elementById("closing-summary");
  if (container === null) {
    return;
  }

  const readings = [
    { label: "Films in the history", count: coverage.total },
    { label: COVERAGE_BASES.dated.label, count: coverage.dated },
    { label: COVERAGE_BASES.rated.label, count: coverage.rated },
    { label: COVERAGE_BASES.tmdb.label, count: coverage.tmdb },
  ].filter((reading) => reading.count !== null);

  if (readings.length === 0) {
    container.replaceChildren(
      build(
        "p",
        "closing__line",
        "This stats file does not record how many films each figure is counted from.",
      ),
    );
    return;
  }

  const list = build("dl", "facts");
  for (const reading of readings) {
    const row = build("div", "facts__row");
    row.append(build("dt", "facts__term", reading.label));
    const value = build("dd", "facts__definition", formatCount(reading.count));
    if (coverage.total !== null && coverage.total > 0 && reading.count < coverage.total) {
      value.append(
        build("span", "facts__meta", ` of ${formatCount(coverage.total)}`),
      );
    }
    row.append(value);
    list.append(row);
  }

  container.replaceChildren(list);

  const generated = toText(stats.generated_at);
  if (generated !== null) {
    container.append(
      build(
        "p",
        "closing__line",
        `Rebuilt from the public feed on ${formatDate(generated)}, and again every week.`,
      ),
    );
  }

  const profile = elementById("closing-profile");
  const username = toText(stats.username);
  if (profile !== null && username !== null) {
    profile.replaceChildren(document.createTextNode("The history behind all of it lives at "));
    const link = build("a", null, `letterboxd.com/${username}`);
    link.href = `https://letterboxd.com/${encodeURIComponent(username)}/`;
    link.rel = "noopener";
    profile.append(link, document.createTextNode("."));
  }
}

/**
 * Keeps the anchor scroll offset equal to the sticky navigation's real height.
 *
 * The navigation bar wraps to two or three rows at some widths, so any constant
 * offset is right at some widths and too small at others, and too small means a
 * heading the reader jumped to sits hidden behind the bar. CSS cannot read one
 * element's height into another element's rule, so the height is measured here
 * and published as the custom property styles.css offsets by.
 */
function trackSectionNavigationHeight() {
  const nav = document.querySelector(".section-nav");
  if (nav === null) {
    return;
  }

  const publishHeight = () => {
    const height = Math.round(nav.getBoundingClientRect().height);
    // A zero reading means the bar is not laid out, as in a print preview or a
    // hidden tab. Keeping the last good value beats pinning headings under it.
    if (height > 0) {
      document.documentElement.style.setProperty("--section-nav-height", `${height}px`);
    }
  };

  publishHeight();

  // The offset only has to be right at the instant of a jump, and a click on a
  // link runs before the browser scrolls, so measuring here is always current
  // even where the observer below is throttled or missing.
  document.addEventListener(
    "click",
    (event) => {
      const target = event.target;
      if (target instanceof Element && target.closest('a[href^="#"]') !== null) {
        publishHeight();
      }
    },
    true,
  );

  // The two other moments the bar changes height: the window resizes, and the
  // web fonts arrive and reflow the links.
  window.addEventListener("resize", publishHeight);
  window.addEventListener("load", publishHeight);

  if (typeof ResizeObserver === "function") {
    new ResizeObserver(publishHeight).observe(nav);
  }
}

/** Shows why the page is empty and what the reader can do about it. */
function showLoadError(message) {
  const banner = elementById("load-error");
  if (banner === null) {
    return;
  }
  banner.hidden = false;
  banner.textContent = message;

  const meta = elementById("page-meta");
  if (meta !== null) {
    meta.textContent = "No figures loaded.";
  }
}

/** Reads the stats file once and renders the page from it. */
async function start() {
  let response;
  try {
    response = await fetch(STATS_URL, { cache: "no-cache" });
  } catch (error) {
    showLoadError(
      `Could not reach ${STATS_URL} (${error.message}). Open this page over http rather than from the file system, then reload.`,
    );
    return;
  }

  if (!response.ok) {
    showLoadError(
      `${STATS_URL} came back with status ${response.status}. Run scripts/build_stats.py to write it, then reload.`,
    );
    return;
  }

  let stats;
  try {
    stats = await response.json();
  } catch (error) {
    showLoadError(
      `${STATS_URL} is not valid JSON (${error.message}). Rebuild it with scripts/build_stats.py, then reload.`,
    );
    return;
  }

  if (toObject(stats) === null) {
    showLoadError(
      `${STATS_URL} does not hold a stats object. Check it against DATA_CONTRACT.md and rebuild it.`,
    );
    return;
  }

  render(stats);
}

// Order matters: the toggle closes the menu on the way to a section, and the
// height tracker then measures a bar that is already back to one row.
setUpSectionNavigationToggle();
trackSectionNavigationHeight();
setUpBackToTop();
start();
