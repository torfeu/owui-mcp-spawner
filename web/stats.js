import { apiFetch, esc } from "./common.js";

// Infrastructure records its own traffic — the control tool logs every
// management call, the router every proxied one. Left in the ranking they
// would always sit on top without being tools anyone deliberately uses.
const SYSTEM_CATEGORY = "system";

export function bindStats() {
  document.getElementById("stats-btn").addEventListener("click", openStats);
  document.getElementById("stats-close").addEventListener("click", closeStats);
  document.getElementById("stats-backdrop").addEventListener("click", closeStats);
  document.getElementById("stats-days").addEventListener("change", loadStats);
}

function openStats() {
  document.getElementById("stats-modal").classList.remove("hidden");
  loadStats();
}

function closeStats() {
  document.getElementById("stats-modal").classList.add("hidden");
}

async function loadStats() {
  const body = document.getElementById("stats-body");
  const days = document.getElementById("stats-days").value;
  body.innerHTML = '<p class="modal-hint">Loading…</p>';
  try {
    const data = await apiFetch(`/api/usage?days=${encodeURIComponent(days)}`);
    document.getElementById("stats-hint").textContent = data.retention_days
      ? `Individual calls are kept for ${data.retention_days} days; the totals are kept for good.`
      : "Individual calls are kept indefinitely.";
    charts.clear();
    body.innerHTML = render(data);
    bindCharts(body);
    body.querySelectorAll("[data-toggle]").forEach(row => {
      row.addEventListener("click", () => {
        document.getElementById(`tools-${row.dataset.toggle}`).classList.toggle("hidden");
      });
    });
  } catch (e) {
    body.innerHTML = `<p class="modal-hint">Could not load statistics: ${esc(e.message)}</p>`;
  }
}

function render(data) {
  const tools = data.instances.filter(i => (i.category || "").toLowerCase() !== SYSTEM_CATEGORY);
  const system = data.instances.filter(i => (i.category || "").toLowerCase() === SYSTEM_CATEGORY);
  const used = tools.filter(i => i.calls > 0);
  const unused = tools.filter(i => !i.calls);
  const labels = data.bucket_labels || [];
  const hourly = data.bucket === "hour";

  const parts = [];
  // The overview first: which day was busy is a question about all tools at
  // once, and answering it per row would mean comparing a dozen sparklines.
  parts.push(lineChart(totalPerBucket(used, labels.length), labels, {
    title: hourly ? "Calls per hour" : "Calls per day",
    subject: "all tools",
    hourly,
    // Past the retention window every bucket is zero by definition. Without
    // saying so the flat stretch on the left reads as "nothing happened".
    note: data.retention_days && data.days > data.retention_days
      ? `only the last ${data.retention_days} days are kept`
      : "",
  }));
  parts.push(section(used, labels, hourly, "Used"));
  if (unused.length) {
    // The actual payload of this view: stopping these frees a port, and with a
    // tool router it also frees the context their schemas occupy in the prompt.
    parts.push(`<h3>Never used (${unused.length})</h3>`);
    parts.push(`<div class="stats-unused">${unused.map(i =>
      `<span class="stats-chip">${esc(i.name)}${i.status === "running" ? "" : ' <span class="cell-muted">· stopped</span>'}</span>`
    ).join("")}</div>`);
  }
  if (system.length) {
    parts.push('<h3>System <span class="info-count">(records its own traffic)</span></h3>');
    parts.push(section(system, labels, hourly, "System"));
  }
  return parts.join("") || '<p class="modal-hint">Nothing recorded yet.</p>';
}

function totalPerBucket(rows, count) {
  const sum = new Array(count).fill(0);
  rows.forEach(row => (row.daily || []).forEach((value, index) => {
    if (index < count) sum[index] += value;
  }));
  return sum;
}

function section(rows, labels, hourly, label) {
  if (!rows.length) return `<p class="modal-hint">No ${label.toLowerCase()} instance was called in this period.</p>`;
  const peak = Math.max(1, ...rows.map(r => Math.max(...(r.daily || [0]))));
  return `<div class="stats-list">${rows.map(row => `
    <div class="stats-row" data-toggle="${esc(row.id)}">
      <div class="stats-main">
        <span class="stats-name">${esc(row.name)}</span>
        ${row.category ? `<span class="category-badge">${esc(row.category)}</span>` : ""}
        ${row.status === "running" ? "" : `<span class="badge badge-${esc(row.status)}">${esc(row.status)}</span>`}
      </div>
      <div class="stats-spark" title="calls per ${hourly ? "hour" : "day"}">${sparkline(row.daily || [], peak)}</div>
      <div class="stats-numbers">
        <span class="stats-recent">${row.recent}</span>
        <span class="cell-muted">of ${row.calls} total · ${row.last_call ? ago(row.last_call) : "—"}</span>
      </div>
    </div>
    <div class="stats-tools hidden" id="tools-${esc(row.id)}">
      ${lineChart(row.daily || [], labels, { compact: true, hourly, subject: row.name })}
      ${row.tools.length ? row.tools.map(tool => `
        <div class="stats-tool">
          <code>${esc(tool.name)}</code>
          <span class="stats-recent">${tool.recent}</span>
          <span class="cell-muted">of ${tool.calls}</span>
        </div>`).join("")
        : '<div class="stats-tool cell-muted">no call recorded</div>'}
    </div>
  `).join("")}</div>`;
}

function sparkline(daily, peak) {
  if (!daily.length) return "";
  return daily.map(value => {
    const height = value ? Math.max(2, Math.round((value / peak) * 14)) : 1;
    return `<i style="height:${height}px" class="${value ? "" : "empty"}"></i>`;
  }).join("");
}

// ── the daily chart ──────────────────────────────────────────────────────────
//
// Hand-rolled SVG rather than a charting library: one line over at most ninety
// points does not justify a dependency, and the modal already ships no JS but
// its own. Geometry is in viewBox units and the element scales with `width:100%`
// at a fixed aspect ratio, so a pixel here maps linearly to a pixel on screen —
// which is what lets the hover handler turn a mouse position back into a day.

const FULL = { w: 720, h: 168, l: 42, r: 16, t: 16, b: 26 };
const COMPACT = { w: 720, h: 92, l: 42, r: 16, t: 12, b: 20 };

let chartSeq = 0;
const charts = new Map();

function lineChart(values, labels, { compact = false, title = "", subject = "", note = "", hourly = false } = {}) {
  // A single bucket is a number, not a trend; two points are the shortest line
  // that still says something.
  if (!values || values.length < 2) return "";
  const g = compact ? COMPACT : FULL;
  const n = values.length;
  const total = values.reduce((a, b) => a + b, 0);
  const peak = Math.max(...values);
  const peakIndex = values.lastIndexOf(peak);
  const top = Math.max(1, peak);
  const plotW = g.w - g.l - g.r;
  const plotH = g.h - g.t - g.b;
  const x = i => g.l + (i * plotW) / (n - 1);
  const y = v => g.t + plotH * (1 - v / top);
  const id = `usage-chart-${++chartSeq}`;
  charts.set(id, { values, labels, geometry: g, x, y, n, hourly });

  const points = values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const area = `M${x(0).toFixed(1)},${y(0).toFixed(1)} L${points.split(" ").join(" L")} L${x(n - 1).toFixed(1)},${y(0).toFixed(1)} Z`;

  // Every label would be noise at 90 days and a wasted axis at 7. Counting the
  // step from the right keeps today labelled whatever the window is.
  const step = Math.max(1, Math.ceil(n / 8));
  const ticks = values.map((_, i) => i).filter(i => (n - 1 - i) % step === 0);

  // Calls are whole numbers, so the axis is too — and deduplicated, or a peak
  // of one would draw "1" twice with a line between the two of them.
  const grid = [...new Set([0, Math.round(top / 2), top])].map(value =>
    `<line x1="${g.l}" x2="${g.w - g.r}" y1="${y(value).toFixed(1)}" y2="${y(value).toFixed(1)}"/>`
    + (compact ? "" : `<text x="${g.l - 8}" y="${(y(value) + 3.5).toFixed(1)}" text-anchor="end">${value}</text>`)
  ).join("");

  const xLabels = ticks.map(i =>
    `<text x="${x(i).toFixed(1)}" y="${g.h - 8}" text-anchor="${i === 0 ? "start" : i === n - 1 ? "end" : "middle"}">${esc(shortLabel(labels[i], n, hourly))}</text>`
  ).join("");

  // One direct label, on the busiest day — the single number worth reading
  // without hovering. Everything else stays in the tooltip.
  const marker = peak > 0 ? `
    <circle class="chart-peak" cx="${x(peakIndex).toFixed(1)}" cy="${y(peak).toFixed(1)}" r="4"/>
    ${compact ? "" : `<text class="chart-peak-label" x="${x(peakIndex).toFixed(1)}" y="${Math.max(11, y(peak) - 9).toFixed(1)}"
       text-anchor="${peakIndex > n * 0.8 ? "end" : peakIndex < n * 0.2 ? "start" : "middle"}">${peak}</text>`}` : "";

  const caption = [
    total ? `${total} call${total === 1 ? "" : "s"} · busiest ${hourly ? "hour" : "day"} `
            + `${fullLabel(labels[peakIndex], hourly)} with ${peak}`
          : "no calls in this period",
    note,
  ].filter(Boolean).join(" · ");

  return `<div class="chart${compact ? " chart-compact" : ""}" id="${id}">
    ${title || !compact ? `<div class="chart-head"><span>${esc(title)}</span><span class="cell-muted">${esc(caption)}</span></div>` : ""}
    <svg class="chart-svg" viewBox="0 0 ${g.w} ${g.h}" role="img"
         aria-label="Calls per day for ${esc(subject)}: ${esc(caption)}">
      <g class="chart-grid">${grid}</g>
      <path class="chart-area" d="${area}"/>
      <polyline class="chart-line" points="${points}"/>
      ${marker}
      <g class="chart-xlabels">${xLabels}</g>
      <g class="chart-cursor hidden">
        <line y1="${g.t}" y2="${g.t + plotH}"/>
        <circle r="4.5"/>
      </g>
      <rect class="chart-hit" x="${g.l}" y="${g.t}" width="${plotW}" height="${plotH}"/>
    </svg>
    <div class="chart-tip hidden"></div>
  </div>`;
}

function bindCharts(root) {
  charts.forEach((chart, id) => {
    const element = root.querySelector(`#${id}`);
    if (!element) return;
    const svg = element.querySelector(".chart-svg");
    const hit = element.querySelector(".chart-hit");
    const cursor = element.querySelector(".chart-cursor");
    const line = cursor.querySelector("line");
    const dot = cursor.querySelector("circle");
    const tip = element.querySelector(".chart-tip");

    hit.addEventListener("mousemove", event => {
      const box = svg.getBoundingClientRect();
      if (!box.width) return;
      const local = ((event.clientX - box.left) / box.width) * chart.geometry.w;
      const index = Math.min(chart.n - 1, Math.max(0, Math.round(
        ((local - chart.geometry.l) / (chart.geometry.w - chart.geometry.l - chart.geometry.r)) * (chart.n - 1)
      )));
      const value = chart.values[index];
      cursor.classList.remove("hidden");
      line.setAttribute("x1", chart.x(index));
      line.setAttribute("x2", chart.x(index));
      dot.setAttribute("cx", chart.x(index));
      dot.setAttribute("cy", chart.y(value));
      tip.classList.remove("hidden");
      tip.innerHTML = `<strong>${value}</strong> call${value === 1 ? "" : "s"}<br><span class="cell-muted">${esc(fullLabel(chart.labels[index], chart.hourly))}</span>`;
      // Pinned inside the chart so the tooltip never leaves the modal at the
      // edges, where the pointer is most likely to be.
      const ratio = chart.x(index) / chart.geometry.w;
      tip.style.left = `${(ratio * 100).toFixed(2)}%`;
      tip.style.transform = `translate(${ratio > 0.8 ? "-100%" : ratio < 0.2 ? "0" : "-50%"}, 0)`;
    });

    hit.addEventListener("mouseleave", () => {
      cursor.classList.add("hidden");
      tip.classList.add("hidden");
    });
  });
}

// Labels are parsed by hand: `new Date("2026-07-31")` is midnight UTC and slips
// to the previous day west of Greenwich, which would mislabel every bucket. The
// hourly form is the same string with "T" and the hour appended.
function parseLabel(label) {
  const [day, hour] = String(label || "").split("T");
  const [year, month, date] = day.split("-").map(Number);
  return year ? new Date(year, month - 1, date, hour ? Number(hour) : 0) : null;
}

// Fixed locale, not the browser's: these dates sit inside English sentences
// ("busiest day Tue, 21 Jul"), and the surrounding interface is English throughout.
const DATE_LOCALE = "en-GB";

function shortLabel(label, span, hourly) {
  const when = parseLabel(label);
  if (!when) return "";
  if (hourly) return `${String(when.getHours()).padStart(2, "0")}:00`;
  return span <= 7
    ? when.toLocaleDateString(DATE_LOCALE, { weekday: "short" })
    : when.toLocaleDateString(DATE_LOCALE, { day: "numeric", month: "short" });
}

function fullLabel(label, hourly) {
  const when = parseLabel(label);
  if (!when) return "";
  const day = when.toLocaleDateString(DATE_LOCALE, { weekday: "short", day: "numeric", month: "short" });
  // The window spans two dates, so the hour alone would be ambiguous at the
  // wrap-around — "03:00" appears once, but yesterday's and today's 03:00 are
  // different buckets on either side of it.
  return hourly ? `${day}, ${String(when.getHours()).padStart(2, "0")}:00` : day;
}

function ago(seconds) {
  const diff = Math.max(0, Date.now() / 1000 - seconds);
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}
