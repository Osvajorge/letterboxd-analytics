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
const LETTERBOXD_FILM_BASE = "https://letterboxd.com/film/";

/** Rows beyond this are cut from a bar chart, with a note saying so. */
const MAXIMUM_BAR_ROWS = 20;

/** Cards beyond this are cut from a people grid, with a note saying so. */
const MAXIMUM_PEOPLE_CARDS = 24;

/** Rows beyond this are cut from a ranked text list, with a note saying so. */
const MAXIMUM_LIST_ROWS = 10;

/** Calendar years beyond this are cut from the heatmap, with a note saying so. */
const MAXIMUM_HEATMAP_YEARS = 6;

/** Words beyond this are cut from the title word list, with a note saying so. */
const MAXIMUM_TITLE_WORDS = 40;

/** A chart narrower than this cannot fit a label and a bar side by side. */
const MINIMUM_CHART_WIDTH = 240;

/** Shown wherever a figure is missing, so no cell ever reads "undefined". */
const MISSING_VALUE = "-";

/** Sentences reused by empty states, so each one names the same fix. */
const NEEDS_TMDB = "It needs TMDB metadata. Run scripts/enrich_tmdb.py, then rebuild the stats.";
const NEEDS_EXPORT = "It needs the one-time Letterboxd export, which the RSS feed cannot supply. Import the export, then rebuild the stats.";
const NEEDS_HISTORY = "Run scripts/fetch_rss.py, merge it with scripts/merge_history.py, then rebuild the stats.";

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
  return text === null ? null : `${LETTERBOXD_FILM_BASE}${encodeURIComponent(text)}/`;
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

/** Replaces a container's contents with a short explanation of why it is bare. */
function showEmptyState(container, message) {
  if (container === null) {
    return;
  }
  container.replaceChildren(build("p", "empty", message));
}

/** Appends a small grey note, used when a chart shows only the top rows. */
function appendNote(container, message) {
  container.append(build("p", "note", message));
}

/** Appends a note naming how many rows were left out, when any were. */
function appendTruncationNote(container, shownCount, totalCount, plural) {
  if (shownCount < totalCount) {
    appendNote(container, `Showing the top ${shownCount} of ${totalCount} ${plural}.`);
  }
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

const BAR_ROW_HEIGHT = 30;
const BAR_HEIGHT = 13;
const BAR_LABEL_FONT_SIZE = 13;

/**
 * Draws a ranked list of horizontal bars.
 *
 * Parameters worth noting:
 *   rows      objects of { label, value, valueText }.
 *   maxValue  the value that fills a bar. Pass 5 for a rating scale so that
 *             every rating chart on the page shares one baseline.
 *   showTrack draws the unfilled remainder, which suits a fixed scale.
 */
function horizontalBarChart(width, rows, { maxValue, showTrack = false, barClassName = "chart__bar" } = {}) {
  const height = rows.length * BAR_ROW_HEIGHT + 4;
  const canvas = svgCanvas(width, Math.max(1, height));

  const valueColumnWidth = 54;
  const labelColumnWidth = Math.min(Math.max(80, width * 0.36), 210);
  const barStartX = labelColumnWidth + 10;
  const barMaximumWidth = Math.max(12, width - barStartX - valueColumnWidth);

  const highest =
    toNumber(maxValue) ??
    rows.reduce((peak, row) => Math.max(peak, toNumber(row.value) ?? 0), 0);
  const scale = highest > 0 ? barMaximumWidth / highest : 0;

  rows.forEach((row, index) => {
    const top = index * BAR_ROW_HEIGHT + 2;
    const barTop = top + (BAR_ROW_HEIGHT - BAR_HEIGHT) / 2;
    const value = toNumber(row.value) ?? 0;
    const group = svgNode("g");

    if (showTrack) {
      group.append(
        svgNode("rect", {
          class: "chart__track",
          x: barStartX,
          y: barTop,
          width: barMaximumWidth,
          height: BAR_HEIGHT,
          rx: 2,
        }),
      );
    }

    group.append(
      svgNode("rect", {
        class: row.barClassName ?? barClassName,
        x: barStartX,
        y: barTop,
        width: Math.max(value > 0 ? 2 : 0, value * scale),
        height: BAR_HEIGHT,
        rx: 2,
      }),
    );

    group.append(
      svgText(truncateToWidth(row.label, labelColumnWidth, BAR_LABEL_FONT_SIZE), {
        class: "chart__label",
        x: 0,
        y: barTop + BAR_HEIGHT - 1,
      }),
    );

    group.append(
      svgText(row.valueText, {
        class: "chart__value",
        x: width,
        y: barTop + BAR_HEIGHT - 1,
        "text-anchor": "end",
      }),
    );

    canvas.append(withTooltip(group, `${row.label}: ${row.valueText}`));
  });

  return canvas;
}

/**
 * Draws bars for a value counted against a total, such as 12 of 100 seen.
 *
 * Each row carries its own denominator, so the bars share no scale. The track
 * behind every bar is the whole, which makes the shares comparable by eye.
 */
function progressBarChart(width, rows) {
  const height = rows.length * BAR_ROW_HEIGHT + 4;
  const canvas = svgCanvas(width, Math.max(1, height));

  const valueColumnWidth = 76;
  const labelColumnWidth = Math.min(Math.max(80, width * 0.34), 230);
  const barStartX = labelColumnWidth + 10;
  const barMaximumWidth = Math.max(12, width - barStartX - valueColumnWidth);

  rows.forEach((row, index) => {
    const top = index * BAR_ROW_HEIGHT + 2;
    const barTop = top + (BAR_ROW_HEIGHT - BAR_HEIGHT) / 2;
    const seen = Math.max(0, toNumber(row.value) ?? 0);
    const total = Math.max(0, toNumber(row.total) ?? 0);
    const share = total > 0 ? Math.min(1, seen / total) : 0;
    const group = svgNode("g");

    group.append(
      svgNode("rect", {
        class: "chart__track",
        x: barStartX,
        y: barTop,
        width: barMaximumWidth,
        height: BAR_HEIGHT,
        rx: 2,
      }),
    );

    group.append(
      svgNode("rect", {
        class: "chart__bar",
        x: barStartX,
        y: barTop,
        width: Math.max(share > 0 ? 2 : 0, share * barMaximumWidth),
        height: BAR_HEIGHT,
        rx: 2,
      }),
    );

    group.append(
      svgText(truncateToWidth(row.label, labelColumnWidth, BAR_LABEL_FONT_SIZE), {
        class: "chart__label",
        x: 0,
        y: barTop + BAR_HEIGHT - 1,
      }),
    );

    group.append(
      svgText(row.valueText, {
        class: "chart__value",
        x: width,
        y: barTop + BAR_HEIGHT - 1,
        "text-anchor": "end",
      }),
    );

    canvas.append(withTooltip(group, `${row.label}: ${row.valueText}`));
  });

  return canvas;
}

const COLUMN_CHART_HEIGHT = 200;
const COLUMN_CHART_SHORT_HEIGHT = 150;
const COLUMN_CHART_TOP = 12;
const COLUMN_CHART_AXIS_HEIGHT = 24;

/**
 * Draws vertical columns, one group per category.
 *
 * Used for the year histogram, the ratings histogram, and the small extras
 * charts. A second series draws a narrower bar beside the first in the same
 * slot.
 *
 * Parameters worth noting:
 *   categories       objects of { label, values, classNames?, valueText? }
 *                    where values holds one number per series.
 *   seriesClassNames one class per series, in the same order as values.
 *   height           the drawing height in pixels, shorter for small charts.
 */
function columnChart(width, categories, { seriesClassNames = ["chart__bar"], height = COLUMN_CHART_HEIGHT } = {}) {
  if (categories.length === 0) {
    // Dividing the width by zero categories would put NaN into every attribute.
    return svgCanvas(width, 1);
  }

  const canvas = svgCanvas(width, height);

  const plotHeight = height - COLUMN_CHART_TOP - COLUMN_CHART_AXIS_HEIGHT;
  const baselineY = COLUMN_CHART_TOP + plotHeight;
  const sidePadding = 6;
  const innerWidth = Math.max(20, width - sidePadding * 2);
  const slotWidth = innerWidth / categories.length;
  const groupWidth = Math.min(slotWidth * 0.74, 40 * seriesClassNames.length);
  const barWidth = Math.max(2, groupWidth / seriesClassNames.length);

  const highest = categories.reduce(
    (peak, category) =>
      category.values.reduce((inner, value) => Math.max(inner, toNumber(value) ?? 0), peak),
    0,
  );
  const scale = highest > 0 ? plotHeight / highest : 0;

  canvas.append(
    svgNode("line", {
      class: "chart__axis",
      x1: sidePadding,
      y1: baselineY + 0.5,
      x2: width - sidePadding,
      y2: baselineY + 0.5,
    }),
  );

  // Labels are drawn every nth column so that neighbouring years never collide.
  const longestLabel = categories.reduce(
    (longest, category) => Math.max(longest, String(category.label).length),
    1,
  );
  const labelStep = Math.max(1, Math.ceil((longestLabel * 7 + 8) / Math.max(1, slotWidth)));

  categories.forEach((category, index) => {
    const slotStartX = sidePadding + slotWidth * index;
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
        withTooltip(bar, `${category.label}: ${category.valueText ?? formatCount(value)}`),
      );
    });

    if (index % labelStep === 0) {
      canvas.append(
        svgText(category.label, {
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
 * Draws a line across a series of yearly averages on the 0 to 5 rating scale.
 *
 * The axis is pinned to the full rating scale rather than the observed range,
 * because a drift of a tenth of a star should look like a tenth of a star.
 */
function ratingLineChart(width, points) {
  const height = 180;
  const canvas = svgCanvas(width, height);

  const leftGutter = 26;
  const rightPadding = 8;
  const topPadding = 12;
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

  for (const tickValue of [0, 2.5, 5]) {
    const y = yFor(tickValue);
    canvas.append(
      svgNode("line", {
        class: tickValue === 0 ? "chart__axis" : "chart__gridline",
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
    const dot = svgNode("circle", {
      class: "chart__dot",
      cx: xFor(index),
      cy: yFor(point.value),
      r: 3.5,
    });
    canvas.append(withTooltip(dot, `${point.label}: ${formatDecimal(point.value, 2)}`));

    // The last year is always labelled, so a stepped label too close to it is
    // dropped rather than left to overlap.
    const labelThisPoint =
      index === lastIndex || (index % labelStep === 0 && index <= lastIndex - labelStep);

    if (labelThisPoint) {
      // The end labels are anchored inwards so that neither one is clipped.
      const anchor = index === 0 ? "start" : index === lastIndex ? "end" : "middle";
      canvas.append(
        svgText(point.label, {
          class: "chart__tick",
          x: xFor(index),
          y: baselineY + 16,
          "text-anchor": anchor,
        }),
      );
    }
  });

  return canvas;
}

const COMPARISON_BAR_VIEW_WIDTH = 100;
const COMPARISON_BAR_HEIGHT = 22;

/**
 * Draws two stacked bars comparing one rating against another, both out of five.
 *
 * This one chart is drawn to a fixed viewBox and stretched to its container,
 * rather than redrawn on resize like the others, because a hot takes list holds
 * dozens of them and each carries no text of its own to distort. Only the
 * horizontal axis stretches, so the bars keep their drawn height.
 */
function comparisonBars(memberRating, crowdRating) {
  const canvas = svgNode("svg", {
    viewBox: `0 0 ${COMPARISON_BAR_VIEW_WIDTH} ${COMPARISON_BAR_HEIGHT}`,
    preserveAspectRatio: "none",
    height: COMPARISON_BAR_HEIGHT,
    "aria-hidden": "true",
    focusable: "false",
  });

  const series = [
    { value: memberRating, y: 1, className: "chart__bar" },
    { value: crowdRating, y: 12, className: "chart__bar--secondary" },
  ];

  for (const { value, y, className } of series) {
    canvas.append(
      svgNode("rect", { class: "chart__track", x: 0, y, width: COMPARISON_BAR_VIEW_WIDTH, height: 9 }),
    );
    const rating = toNumber(value);
    if (rating === null) {
      continue;
    }
    const share = Math.max(0, Math.min(RATING_SCALE_MAXIMUM, rating)) / RATING_SCALE_MAXIMUM;
    canvas.append(
      svgNode("rect", {
        class: className,
        x: 0,
        y,
        width: share * COMPARISON_BAR_VIEW_WIDTH,
        height: 9,
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

/** Builds the "less to more" key that explains the heatmap shades. */
function buildHeatmapLegend() {
  const legend = build("p", "heatmap-legend");
  legend.append(build("span", null, "Fewer films"));
  for (const level of [0, 1, 2, 3, 4]) {
    const swatch = build("span", "heatmap-legend__swatch");
    swatch.dataset.level = String(level);
    swatch.setAttribute("aria-hidden", "true");
    legend.append(swatch);
  }
  legend.append(build("span", null, "More films"));
  return legend;
}

/* ============================================================ Text components */

/**
 * Builds a single large figure with a label and one line of context.
 *
 * Used where a chart would add nothing, such as a count of days.
 */
function buildStatCard({ value, unit, label, note, tone }) {
  const card = build("div", tone ? `stat stat--${tone}` : "stat");
  const figure = build("p", "stat__value", value);
  if (unit) {
    figure.append(build("span", "stat__unit", ` ${unit}`));
  }
  card.append(figure);
  card.append(build("p", "stat__label", label));
  if (note) {
    card.append(build("p", "stat__note", note));
  }
  return card;
}

/**
 * Builds a ranked list of named rows, each with a figure on the right.
 *
 * Rows take { title, href, meta, value }. A row with an href links to the film
 * or person on Letterboxd; a row without one is plain text.
 *
 * The name and the figure sit in a wrapper rather than directly in the list
 * item, because a list item laid out as a grid stops being a list item and
 * loses both its number and its place in the list for a screen reader.
 */
function buildRankedList(rows) {
  const list = build("ol", "ranked");

  for (const row of rows) {
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
    if (row.meta) {
      item.append(build("p", "ranked__meta", row.meta));
    }
    list.append(item);
  }

  return list;
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

/** Reads a whole distribution into column chart categories, skipping bad rows. */
function readDistribution(list) {
  const categories = [];
  for (const bucket of toArray(list)) {
    const read = readBucket(bucket);
    if (read !== null) {
      categories.push({ label: read.label, values: [read.count] });
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

/* ==================================================== Panel section renderers */

const TOTAL_TILES = [
  { key: "films", label: "Films", note: "watched in total" },
  { key: "hours", label: "Hours", note: "of screen time" },
  { key: "directors", label: "Directors", note: "with at least one film seen" },
  { key: "countries", label: "Countries", note: "of production" },
  { key: "longest_streak_weeks", label: "Longest streak", note: "consecutive weeks with a film" },
  { key: "multi_film_days", label: "Double bills", note: "days with more than one film" },
];

/** Renders the header figures: films, hours, directors, countries, streaks. */
function renderTotals(stats) {
  const container = elementById("totals-grid");
  if (container === null) {
    return;
  }

  const totals = toObject(stats.totals);
  if (totals === null) {
    showEmptyState(container, "No totals yet. They appear once the pipeline has run at least once.");
    return;
  }

  const tiles = TOTAL_TILES.map(({ key, label, note }) => {
    const tile = build("div", "tile");
    tile.append(build("p", "tile__value", formatCount(totals[key])));
    tile.append(build("p", "tile__label", label));
    tile.append(build("p", "tile__note", note));
    return tile;
  });

  container.replaceChildren(...tiles);
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
        `No years recorded yet. A year appears once the history holds an entry with a watch date. ${NEEDS_HISTORY}`,
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

      appendChart(yearsContainer, {
        caption:
          `Films watched each year from ${categories[0].label} to ${categories[categories.length - 1].label}.` +
          (busiest === null
            ? ""
            : ` Busiest year: ${formatYear(busiest.year)} with ${formatCount(busiest.films)} films.`),
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
      `No ratings recorded yet. This histogram counts only entries that carry a rating. ${NEEDS_HISTORY}`,
    );
    return;
  }

  const categories = [...buckets.entries()].map(([rating, count]) => ({
    label: rating,
    values: [count],
  }));

  replaceWithChart(container, {
    caption: `${formatCount(total)} ratings given, grouped by half-star step from 0.5 to 5.`,
    draw: (width) => columnChart(width, categories),
    table: buildDataTable(
      "Number of ratings given at each half-star step",
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
    .filter((entry) => toNumber(entry?.decade) !== null)
    .sort((left, right) => (toNumber(right.average_rating) ?? 0) - (toNumber(left.average_rating) ?? 0));

  if (decades.length === 0) {
    showEmptyState(
      container,
      `No decades yet. A decade appears once a film you have watched carries a release year. ${NEEDS_HISTORY}`,
    );
    return;
  }

  const rows = decades.map((entry) => ({
    label: `${formatYear(entry.decade)}s`,
    value: toNumber(entry.average_rating) ?? 0,
    valueText: formatDecimal(entry.average_rating, 2),
  }));

  replaceWithChart(container, {
    caption: "Average rating per decade, highest first. The bar fills at five stars.",
    draw: (width) => horizontalBarChart(width, rows, { maxValue: RATING_SCALE_MAXIMUM, showTrack: true }),
    table: buildDataTable(
      "Average rating and film count per decade",
      ["Decade", "Average rating", "Films"],
      decades.map((entry) => [
        `${formatYear(entry.decade)}s`,
        formatDecimal(entry.average_rating, 2),
        formatCount(entry.films),
      ]),
    ),
  });
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
      showEmptyState(mostWatchedContainer, `No ${singularNoun} counts yet. ${NEEDS_TMDB}`);
    } else {
      const shown = items.slice(0, MAXIMUM_BAR_ROWS);
      const rows = shown.map((item) => ({
        label: toText(item.name),
        value: toNumber(item.count) ?? 0,
        valueText: formatCount(item.count),
      }));

      replaceWithChart(mostWatchedContainer, {
        caption: `Films watched per ${singularNoun}, most first.`,
        draw: (width) => horizontalBarChart(width, rows),
        table: buildDataTable(
          `Films watched per ${singularNoun}`,
          ["Name", "Films"],
          rows.map((row) => [row.label, row.valueText]),
        ),
      });
      appendTruncationNote(mostWatchedContainer, shown.length, items.length, `${singularNoun}s`);
    }
  }

  if (highestRatedContainer !== null) {
    const items = toArray(group?.highest_rated).filter((item) => toText(item?.name) !== null);
    if (items.length === 0) {
      showEmptyState(highestRatedContainer, `No ${singularNoun} ratings yet. ${NEEDS_TMDB}`);
    } else {
      const shown = items.slice(0, MAXIMUM_BAR_ROWS);
      const rows = shown.map((item) => ({
        label: toText(item.name),
        value: toNumber(item.average) ?? 0,
        valueText: formatDecimal(item.average, 2),
      }));

      replaceWithChart(highestRatedContainer, {
        caption: `Average rating per ${singularNoun}. The bar fills at five stars.`,
        draw: (width) =>
          horizontalBarChart(width, rows, { maxValue: RATING_SCALE_MAXIMUM, showTrack: true }),
        table: buildDataTable(
          `Average rating per ${singularNoun}`,
          ["Name", "Average rating", "Films"],
          shown.map((item) => [
            toText(item.name),
            formatDecimal(item.average, 2),
            formatCount(item.count),
          ]),
        ),
      });
      appendTruncationNote(highestRatedContainer, shown.length, items.length, `${singularNoun}s`);
    }
  }
}

/**
 * Builds a grid of people cards with a photo, a name, and a count.
 *
 * A person with no photo keeps the same card size and shows their initials, so
 * a missing image never leaves a hole in the grid.
 */
function buildPeopleGrid(people, describeCount) {
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

    item.append(avatar);
    item.append(build("p", "person__name", name));
    item.append(build("p", "person__count", describeCount(person)));
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
    showEmptyState(container, `No cast counts yet. ${NEEDS_TMDB}`);
    return;
  }

  const shown = cast.slice(0, MAXIMUM_PEOPLE_CARDS);
  container.replaceChildren(
    buildPeopleGrid(shown, (person) => `${formatCount(person.count)} films`),
  );
  appendTruncationNote(container, shown.length, cast.length, "actors");
}

/** Renders the most watched directors, with the average rating given to each. */
function renderDirectors(stats) {
  const container = elementById("directors-list");
  if (container === null) {
    return;
  }

  const directors = toArray(stats.directors).filter((person) => toText(person?.name) !== null);
  if (directors.length === 0) {
    showEmptyState(container, `No director counts yet. ${NEEDS_TMDB}`);
    return;
  }

  const shown = directors.slice(0, MAXIMUM_PEOPLE_CARDS);
  container.replaceChildren(
    buildPeopleGrid(shown, (person) => {
      const average = toNumber(person.average_rating);
      const films = `${formatCount(person.count)} films`;
      return average === null ? films : `${films} · ${average.toFixed(1)}`;
    }),
  );
  appendTruncationNote(container, shown.length, directors.length, "directors");
}

/** Renders the studios watched most, with the average rating given to each. */
function renderStudios(stats) {
  const container = elementById("studios-chart");
  if (container === null) {
    return;
  }

  const studios = toArray(stats.studios).filter((studio) => toText(studio?.name) !== null);
  if (studios.length === 0) {
    showEmptyState(container, `No studio counts yet. ${NEEDS_TMDB}`);
    return;
  }

  const shown = studios.slice(0, MAXIMUM_BAR_ROWS);
  const rows = shown.map((studio) => ({
    label: toText(studio.name),
    value: toNumber(studio.count) ?? 0,
    valueText: formatCount(studio.count),
  }));

  replaceWithChart(container, {
    caption: "Films watched per studio, most first.",
    draw: (width) => horizontalBarChart(width, rows),
    table: buildDataTable(
      "Films and average rating per studio",
      ["Studio", "Films", "Average rating"],
      shown.map((studio) => [
        toText(studio.name),
        formatCount(studio.count),
        formatDecimal(studio.average_rating, 2),
      ]),
    ),
  });
  appendTruncationNote(container, shown.length, studios.length, "studios");
}

/** Renders how much of each film collection has been watched. */
function renderCollections(stats) {
  const container = elementById("collections-chart");
  if (container === null) {
    return;
  }

  const collections = toArray(stats.collections).filter((entry) => toText(entry?.name) !== null);
  if (collections.length === 0) {
    showEmptyState(container, `No collections yet. ${NEEDS_TMDB}`);
    return;
  }

  const ranked = [...collections].sort((left, right) => {
    const leftShare = shareSeen(left);
    const rightShare = shareSeen(right);
    return rightShare - leftShare || (toNumber(right.total) ?? 0) - (toNumber(left.total) ?? 0);
  });

  const shown = ranked.slice(0, MAXIMUM_BAR_ROWS);
  const rows = shown.map((entry) => ({
    label: toText(entry.name),
    value: toNumber(entry.seen) ?? 0,
    total: toNumber(entry.total) ?? 0,
    valueText: `${formatCount(entry.seen)} / ${formatCount(entry.total)}`,
  }));

  replaceWithChart(container, {
    caption: "Films seen out of each collection, most complete first.",
    draw: (width) => progressBarChart(width, rows),
    table: buildDataTable(
      "Films seen per collection",
      ["Collection", "Seen", "Total"],
      shown.map((entry) => [toText(entry.name), formatCount(entry.seen), formatCount(entry.total)]),
    ),
  });
  appendTruncationNote(container, shown.length, ranked.length, "collections");
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
    showEmptyState(container, "No list progress yet. Run the list fetcher, then rebuild the stats.");
    return;
  }

  const ranked = [...lists].sort((left, right) => shareSeen(right) - shareSeen(left));
  const rows = ranked.map((entry) => ({
    label: toText(entry.title),
    value: toNumber(entry.seen) ?? 0,
    total: toNumber(entry.total) ?? 0,
    valueText: `${formatCount(entry.seen)} / ${formatCount(entry.total)}`,
  }));

  replaceWithChart(container, {
    caption: `Progress through ${formatCount(ranked.length)} curated lists, furthest first.`,
    draw: (width) => progressBarChart(width, rows),
    table: buildDataTable(
      "Films seen per curated list",
      ["List", "Seen", "Total", "Share"],
      ranked.map((entry) => [
        toText(entry.title),
        formatCount(entry.seen),
        formatCount(entry.total),
        formatPercentage(shareSeen(entry)),
      ]),
    ),
  });
}

/** Renders the ranked country table that stands in for a world map. */
function renderCountriesRanked(stats) {
  const container = elementById("countries-ranked-chart");
  if (container === null) {
    return;
  }

  const countries = toArray(stats.world_map).filter((entry) => toText(entry?.name) !== null);
  if (countries.length === 0) {
    showEmptyState(container, `No country counts yet. ${NEEDS_TMDB}`);
    return;
  }

  const ranked = [...countries].sort(
    (left, right) => (toNumber(right.count) ?? 0) - (toNumber(left.count) ?? 0),
  );
  const shown = ranked.slice(0, MAXIMUM_BAR_ROWS);
  const rows = shown.map((entry) => {
    const flag = flagForCountryCode(entry.iso_3166_1);
    return {
      label: flag === "" ? toText(entry.name) : `${flag} ${toText(entry.name)}`,
      value: toNumber(entry.count) ?? 0,
      valueText: formatCount(entry.count),
    };
  });

  replaceWithChart(container, {
    caption: `Films watched per country of production, most first, across ${formatCount(ranked.length)} countries.`,
    draw: (width) => horizontalBarChart(width, rows),
    table: buildDataTable(
      "Films watched per country of production",
      ["Country", "Code", "Films"],
      shown.map((entry) => [
        toText(entry.name),
        toText(entry.iso_3166_1) ?? MISSING_VALUE,
        formatCount(entry.count),
      ]),
    ),
  });
  appendTruncationNote(container, shown.length, ranked.length, "countries");
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
        `${estimated} of watchlist films have no real added date. For those, the date used is ` +
        "the day the weekly reader first saw the film, which understates how long they have " +
        "waited, so the median above is a lower bound. Load the remaining dates by running " +
        "scripts/backfill.py on your Letterboxd export, then rebuild the stats.",
    };
  }

  // Every added date came from the export, so the wait needs no qualification.
  return null;
}

/** Renders the extras tiles: rating bias, rewatch rate, watchlist, runtime. */
function renderExtrasTiles(extras) {
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
      value: formatDecimal(bias?.member_average, 2),
      label: "Your average",
      note: "on the 0.5 to 5 scale",
    },
    {
      value: formatDecimal(bias?.tmdb_average, 2),
      label: "TMDB average",
      note: "same films, on the 0 to 10 scale",
    },
    {
      value: formatSignedDecimal(bias?.delta, 2),
      label: "Rating bias",
      note: "your rating minus the crowd's, both scaled to five",
    },
    {
      value: formatPercentage(extras.rewatch_rate),
      label: "Rewatch rate",
      note: "share of entries that were rewatches",
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
      unverified: waitQuality !== null,
    },
    {
      value: formatPercentage(watchlist?.conversion_rate),
      label: "Watchlist conversion",
      note: "share of added films eventually watched",
    },
    {
      value: daysWatched,
      label: "Days watched",
      note: "total runtime, counted as whole days",
    },
    {
      value: formatCount(runtime?.median),
      label: "Median runtime",
      note: "minutes per film",
    },
  ];

  container.replaceChildren(
    ...tiles.map((tile) => {
      const node = build("div", tile.unverified === true ? "tile tile--unverified" : "tile");
      node.append(build("p", "tile__value", tile.value));
      node.append(build("p", "tile__label", tile.label));
      node.append(build("p", "tile__note", tile.note));
      return node;
    }),
  );

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
function renderContrarianColumn(container, films, emptyMessage) {
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
    bars.append(comparisonBars(memberRating, crowdRating));
    item.append(bars);

    const figures = build("p", "take__figures");
    figures.append(
      document.createTextNode(
        `You ${formatDecimal(memberRating, 1)} · Crowd ${formatDecimal(crowdRating, 1)} · `,
      ),
    );
    const deltaTone = delta !== null && delta < 0 ? "down" : "up";
    figures.append(
      build("span", `take__delta take__delta--${deltaTone}`, `${formatSignedDecimal(delta, 1)} stars`),
    );
    item.append(figures);

    list.append(item);
  }

  container.replaceChildren(list);
  appendTruncationNote(container, shown.length, rows.length, "films");
}

/** Renders both hot takes columns, hottest disagreement first in each. */
function renderContrarianIndex(extras) {
  const index = toObject(extras.contrarian_index);
  const missing = `No hot takes yet. Comparing your rating with the crowd's needs TMDB vote averages. ${NEEDS_TMDB}`;

  renderContrarianColumn(
    elementById("contrarian-hotter"),
    toArray(index?.hotter_than_crowd),
    missing,
  );
  renderContrarianColumn(
    elementById("contrarian-colder"),
    toArray(index?.colder_than_crowd),
    missing,
  );

  const legendHost = elementById("contrarian-legend");
  if (legendHost !== null) {
    legendHost.replaceChildren(buildComparisonLegend("Your rating", "The crowd's rating"));
  }
}

/** Renders films marked as liked yet rated below the member's own average. */
function renderLikedButLow(extras) {
  const container = elementById("liked-but-low");
  if (container === null) {
    return;
  }

  const rows = toArray(extras.liked_but_low)
    .map((entry) => readFilmRow(entry, "rating"))
    .filter((row) => row !== null);

  if (rows.length === 0) {
    showEmptyState(
      container,
      "No films here. Nothing you liked was also rated below your usual mark.",
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
  appendTruncationNote(container, shown.length, rows.length, "films");
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
        note: `Total runtime is not known yet. ${NEEDS_TMDB}`,
      }),
    );
  } else {
    const endsOn = toText(life?.would_end_on);
    cards.push(
      buildStatCard({
        value: formatDecimal(lifeDays, 1),
        unit: "days",
        label: "Days of film",
        tone: "primary",
        note:
          endsOn === null
            ? "Everything you have logged, played back to back without stopping."
            : `Played back to back from today, everything you have logged would run until ${formatDate(endsOn)}.`,
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
        tone: "secondary",
        note:
          from === null || to === null
            ? "Your longest run of days without logging a film."
            : `Your longest run without a film, from ${formatDate(from)} to ${formatDate(to)}.`,
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
    showEmptyState(container, "No weekday profile yet. It needs entries that carry a watch date.");
    return;
  }

  const categories = rows.map((row) => ({ label: row.weekday.slice(0, 3), values: [row.count] }));
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
    showEmptyState(container, "No monthly profile yet. It needs entries that carry a watch date.");
    return;
  }

  const categories = rows.map((row) => ({
    label: MONTH_NAMES[row.month - 1],
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
      `No logging lag yet. It needs both the logged date and the watched date. ${NEEDS_EXPORT}`,
    );
    return;
  }

  container.replaceChildren(
    buildStatCard({
      value: formatCount(median),
      unit: median === 1 ? "day" : "days",
      label: "Median logging lag",
      tone: "primary",
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
    showEmptyState(container, "No watch dates yet, so there is nothing to plot by day.");
    return;
  }

  const years = [...countsByYear.keys()].sort((left, right) => right - left);
  const shown = years.slice(0, MAXIMUM_HEATMAP_YEARS);

  container.replaceChildren();
  for (const year of shown) {
    const countsByDate = countsByYear.get(year);
    const block = build("div", "heatmap-year");
    block.append(build("p", "heatmap-year__label", String(year)));

    const scroller = build("div", "scroll-x");
    scroller.append(calendarHeatmap(year, countsByDate));
    block.append(scroller);

    const filmCount = [...countsByDate.values()].reduce((sum, count) => sum + count, 0);
    block.append(
      build(
        "p",
        "chart__caption",
        `${formatCount(filmCount)} films across ${formatCount(countsByDate.size)} days in ${year}.`,
      ),
    );

    container.append(block);
  }

  container.append(buildHeatmapLegend());
  if (shown.length < years.length) {
    appendNote(container, `Showing the ${shown.length} most recent of ${years.length} years.`);
  }
}

/* ========================================================= Extras: your taste */

/** Renders the year by year average rating, to show whether taste has hardened. */
function renderRatingDrift(extras) {
  const container = elementById("rating-drift-chart");
  if (container === null) {
    return;
  }

  const points = toArray(extras.rating_drift)
    .filter((entry) => toNumber(entry?.year) !== null && toNumber(entry?.average) !== null)
    .sort((left, right) => toNumber(left.year) - toNumber(right.year))
    .map((entry) => ({ label: String(toNumber(entry.year)), value: toNumber(entry.average) }));

  if (points.length === 0) {
    showEmptyState(
      container,
      `No yearly averages yet. An average needs entries that carry both a rating and a watch date. ${NEEDS_HISTORY}`,
    );
    return;
  }

  const first = points[0];
  const last = points[points.length - 1];
  const change = last.value - first.value;
  const direction = change > 0.05 ? "risen" : change < -0.05 ? "fallen" : "held steady";

  replaceWithChart(container, {
    caption: `Average rating by year. From ${first.label} to ${last.label} it has ${direction}, from ${first.value.toFixed(2)} to ${last.value.toFixed(2)}.`,
    draw: (width) => ratingLineChart(width, points),
    table: buildDataTable(
      "Average rating given in each year",
      ["Year", "Average rating"],
      points.map((point) => [point.label, point.value.toFixed(2)]),
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
function renderHalfStarUsage(extras) {
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
      "No half-star figures yet. They need entries that carry a rating.",
    );
    return;
  }

  container.replaceChildren(
    buildStatCard({
      value: formatPercentage(share),
      label: "Ratings on a half star",
      tone: "primary",
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
    showEmptyState(container, `No runtime comparison yet. Pairing a rating with a runtime needs both. ${NEEDS_TMDB}`);
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
      tone: "secondary",
      note: `On a scale from -1 to +1. ${leaning}`,
    }),
  );

  if (buckets.length === 0) {
    return;
  }

  const rows = buckets.map((bucket) => ({
    label: `${bucket.label} min`,
    value: bucket.average,
    valueText: formatDecimal(bucket.average, 2),
  }));

  appendChart(container, {
    caption: "Average rating per runtime bucket. The bar fills at five stars.",
    draw: (width) => horizontalBarChart(width, rows, { maxValue: RATING_SCALE_MAXIMUM, showTrack: true }),
    table: buildDataTable(
      "Average rating per runtime bucket",
      ["Runtime in minutes", "Average rating", "Films"],
      buckets.map((bucket) => [
        bucket.label,
        formatDecimal(bucket.average, 2),
        formatCount(bucket.films),
      ]),
    ),
  });
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
    showEmptyState(container, `No obscurity figures yet. Vote counts are the measure. ${NEEDS_TMDB}`);
  } else {
    const card = buildStatCard({
      value: formatCount(median),
      unit: "votes",
      label: "Median film",
      tone: "primary",
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
    "votes",
    `No deep cuts listed yet. ${NEEDS_TMDB}`,
  );
  renderFilmSideList(
    elementById("obscurity-most-popular"),
    toArray(obscurity?.most_popular),
    "votes",
    `No crowd favourites listed yet. ${NEEDS_TMDB}`,
  );
}

/** Renders a short ranked list of films with one figure beside each. */
function renderFilmSideList(container, films, unit, emptyMessage) {
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
        value: row.value === null ? MISSING_VALUE : `${formatCount(row.value)} ${unit}`,
      })),
    ),
  );
  appendTruncationNote(container, shown.length, rows.length, "films");
}

/** Renders how long after release a film is usually watched. */
function renderReleaseRecency(extras) {
  const container = elementById("release-recency");
  if (container === null) {
    return;
  }

  const recency = toObject(extras.release_recency);
  const medianDays = toNumber(recency?.median_days_after_release);

  const points = toArray(recency?.by_year)
    .map((entry) => ({ year: toNumber(entry?.year), medianDays: toNumber(entry?.median_days) }))
    .filter((point) => point.year !== null && point.medianDays !== null)
    .sort((left, right) => left.year - right.year);

  if (medianDays === null && points.length === 0) {
    showEmptyState(container, `No release dates yet, so the wait cannot be measured. ${NEEDS_TMDB}`);
    return;
  }

  const years = medianDays === null ? null : medianDays / 365.25;
  container.replaceChildren(
    buildStatCard({
      value: formatCount(medianDays),
      unit: medianDays === 1 ? "day" : "days",
      label: "Typical wait after release",
      tone: "primary",
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
  }));

  appendChart(container, {
    caption: "Median days between a film's release and the day you watched it, by watch year.",
    draw: (width) => columnChart(width, categories, { height: COLUMN_CHART_SHORT_HEIGHT }),
    table: buildDataTable(
      "Median days after release, by watch year",
      ["Watch year", "Median days after release"],
      points.map((point) => [String(point.year), formatCount(point.medianDays)]),
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
    showEmptyState(container, `No runtime buckets yet. ${NEEDS_TMDB}`);
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
    showEmptyState(
      container,
      `No extremes yet. The shortest and longest need runtimes. ${NEEDS_TMDB}`,
    );
    return;
  }

  container.replaceChildren(buildFactList(facts));
}

/** Renders the decades with no film watched, as a row of chips. */
function renderDecadeGaps(extras) {
  const container = elementById("decade-gaps");
  if (container === null) {
    return;
  }

  const gaps = toArray(extras.decade_gaps)
    .map((decade) => toNumber(decade))
    .filter((decade) => decade !== null)
    .sort((left, right) => left - right);

  if (gaps.length === 0) {
    showEmptyState(container, "No gaps. Every decade in the data has at least one film watched.");
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
function renderDirectorLuck(container, directors, emptyMessage, tone) {
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

  const shown = entries.slice(0, MAXIMUM_LIST_ROWS);
  const rows = shown.map((entry) => ({
    label: entry.name,
    value: entry.average,
    valueText: formatDecimal(entry.average, 2),
    barClassName: tone === "secondary" ? "chart__bar--secondary" : "chart__bar",
  }));

  replaceWithChart(container, {
    caption: "Average rating you give each director. The bar fills at five stars.",
    draw: (width) => horizontalBarChart(width, rows, { maxValue: RATING_SCALE_MAXIMUM, showTrack: true }),
    table: buildDataTable(
      "Average rating per director",
      ["Director", "Average rating", "Films rated"],
      shown.map((entry) => [entry.name, formatDecimal(entry.average, 2), formatCount(entry.films)]),
    ),
  });
  appendTruncationNote(container, shown.length, entries.length, "directors");
}

/** Renders the directors you rate highest and the ones you rate lowest. */
function renderDirectorLuckPair(extras) {
  const missing = `No director averages yet. They need directing credits. ${NEEDS_TMDB}`;
  renderDirectorLuck(elementById("lucky-director"), extras.lucky_director, missing, "primary");
  renderDirectorLuck(elementById("unlucky-director"), extras.unlucky_director, missing, "secondary");
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
    showEmptyState(container, `No background faces yet. They need cast credits with billing order. ${NEEDS_TMDB}`);
    return;
  }

  const shown = actors.slice(0, MAXIMUM_LIST_ROWS);
  container.replaceChildren(
    buildRankedList(
      shown.map((entry) => ({
        title: entry.name,
        value: formatQuantity(entry.count, "film", "films"),
        meta:
          entry.billing === null
            ? null
            : `Usually ${formatOrdinal(entry.billing)} on the cast list.`,
      })),
    ),
  );
  appendTruncationNote(container, shown.length, actors.length, "actors");
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
          value: formatQuantity(person.count, "film", "films"),
        })),
      ),
    );
    appendTruncationNote(column, shown.length, people.length, "people");
    groups.push(column);
  }

  if (groups.length === 0) {
    showEmptyState(container, `No crew counts yet. They need crew credits. ${NEEDS_TMDB}`);
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
    showEmptyState(container, `No filmography counts yet. ${NEEDS_TMDB}`);
    return;
  }

  const ranked = [...directors].sort((left, right) => {
    const leftShare = (toNumber(left.seen) ?? 0) / Math.max(1, toNumber(left.filmography) ?? 0);
    const rightShare = (toNumber(right.seen) ?? 0) / Math.max(1, toNumber(right.filmography) ?? 0);
    return rightShare - leftShare;
  });

  const shown = ranked.slice(0, MAXIMUM_BAR_ROWS);
  const rows = shown.map((entry) => ({
    label: toText(entry.name),
    value: toNumber(entry.seen) ?? 0,
    total: toNumber(entry.filmography) ?? 0,
    valueText: `${formatCount(entry.seen)} / ${formatCount(entry.filmography)}`,
  }));

  replaceWithChart(container, {
    caption: "Films seen out of each director's known filmography, most complete first.",
    draw: (width) => progressBarChart(width, rows),
    table: buildDataTable(
      "Films seen per director filmography",
      ["Director", "Seen", "Filmography"],
      shown.map((entry) => [
        toText(entry.name),
        formatCount(entry.seen),
        formatCount(entry.filmography),
      ]),
    ),
  });
  appendTruncationNote(container, shown.length, ranked.length, "directors");
}

/* ======================================================== Extras: title words */

const SMALLEST_WORD_SIZE_REM = 0.8125;
const LARGEST_WORD_SIZE_REM = 2;

/**
 * Renders the words that recur most in the titles watched.
 *
 * Each word is sized between the two constants above in proportion to its
 * count, and carries that count as text beside it, so the size is decoration
 * and the figure is still readable.
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
      "No repeated title words yet. They appear once enough films share a word.",
    );
    return;
  }

  const shown = words.slice(0, MAXIMUM_TITLE_WORDS);
  const highest = shown[0].count;
  const lowest = shown[shown.length - 1].count;
  const span = Math.max(1, highest - lowest);

  const list = build("ul", "wordcloud");
  for (const entry of shown) {
    const item = build("li", "wordcloud__item");
    const share = (entry.count - lowest) / span;
    const size = SMALLEST_WORD_SIZE_REM + share * (LARGEST_WORD_SIZE_REM - SMALLEST_WORD_SIZE_REM);
    item.style.fontSize = `${size.toFixed(3)}rem`;
    item.append(build("span", "wordcloud__word", entry.word));
    item.append(build("span", "wordcloud__count", formatCount(entry.count)));
    list.append(item);
  }

  container.replaceChildren(list);
  appendNote(
    container,
    `Word size follows how often the word appears, from ${formatCount(lowest)} to ${formatCount(highest)} titles.`,
  );
  appendTruncationNote(container, shown.length, words.length, "words");
}

/* ============================================================= Page assembly */

/** Renders every extras block, or an empty state for each when there is none. */
function renderExtras(stats) {
  const extras = toObject(stats.extras) ?? {};

  renderExtrasTiles(extras);

  renderContrarianIndex(extras);
  renderLikedButLow(extras);

  renderRhythmFacts(extras);
  renderWeekdayProfile(extras);
  renderMonthSeasonality(extras);
  renderLoggingLag(extras);
  renderHeatmap(extras);

  renderRatingDrift(extras);
  renderHalfStarUsage(extras);
  renderRatingVersusRuntime(extras);

  renderObscurity(extras);
  renderReleaseRecency(extras);
  renderRuntimeDistribution(extras);
  renderExtremes(extras);
  renderDecadeGaps(extras);

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
    parts.push(`${formatCount(filmCount)} films tracked`);
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
  renderPageHeader(stats);
  renderTotals(stats);
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
  renderCountriesRanked(stats);
  renderExtras(stats);
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

trackSectionNavigationHeight();
start();
