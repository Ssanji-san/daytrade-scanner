"use strict";

const $ = (sel) => document.querySelector(sel);

let windowMin = 5;
let prevGainers = new Set();
let prevHod = new Set();
let prevHighs = {};

// ---------- formatting ----------
const fmtNum = (v, digits = 2) =>
  v == null ? "–" : v.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const fmtPct = (v) => {
  if (v == null) return "–";
  const s = v >= 0 ? "+" : "";
  return `${s}${v.toFixed(1)}%`;
};
const fmtBig = (v) => {
  if (v == null) return "–";
  if (v >= 1e9) return (v / 1e9).toFixed(1) + "B";
  if (v >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (v >= 1e3) return (v / 1e3).toFixed(0) + "K";
  return String(Math.round(v));
};
const pctClass = (v) => (v == null ? "" : v >= 0 ? "up" : "down");
const tvLink = (sym) =>
  `<a href="https://www.tradingview.com/chart/?symbol=${sym}" target="_blank" rel="noopener">${sym}</a>`;
const localTime = (epoch) =>
  new Date(epoch * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

// ---------- sound ----------
let audioCtx = null;
function beep() {
  if (!$("#sound").checked) return;
  audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
  const osc = audioCtx.createOscillator();
  const gain = audioCtx.createGain();
  osc.frequency.value = 880;
  gain.gain.setValueAtTime(0.15, audioCtx.currentTime);
  gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.25);
  osc.connect(gain).connect(audioCtx.destination);
  osc.start();
  osc.stop(audioCtx.currentTime + 0.25);
}

// ---------- rendering ----------
function newsCell(row) {
  return row.has_news ? '<td title="headline in the last 24h">📰</td>' : "<td></td>";
}
function floatCell(v, hot) {
  const cls = hot && v != null && v < 20e6 ? "hot" : "";
  return `<td class="num ${cls}">${fmtBig(v)}</td>`;
}

function renderGainers(payload) {
  const rows = payload.gainers[String(windowMin)] || [];
  const tbody = $("#gainers tbody");
  const seen = new Set();
  tbody.innerHTML = rows
    .map((r) => {
      seen.add(r.symbol);
      const isNew = prevGainers.size && !prevGainers.has(r.symbol);
      return `<tr class="${isNew ? "flash" : ""}">
        <td class="sym">${tvLink(r.symbol)}</td>
        <td class="num up">${fmtPct(r.changes[String(windowMin)])}</td>
        <td class="num ${pctClass(r.day_pct)}">${fmtPct(r.day_pct)}</td>
        <td class="num">$${fmtNum(r.price)}</td>
        <td class="num">${fmtBig(r.day_volume)}</td>
        <td class="num">${r.rvol == null ? "–" : r.rvol.toFixed(1)}</td>
        ${floatCell(r.float_shares, true)}
        ${newsCell(r)}
      </tr>`;
    })
    .join("");
  prevGainers = seen;
  $("#gainers-empty").classList.toggle("hidden", rows.length > 0);
}

const FAIL_LABELS = { pct_up: "%day", volume: "volume", rvol: "rvol", float: "float", hod: "off high", news: "no news" };

function hodRow(r, cls) {
  const fails = (r.failed || [])
    .map((f) => `<span class="fail-chip">${FAIL_LABELS[f] || f}</span>`)
    .join("");
  return `<tr class="${cls}">
    <td class="sym fail-chip-cell">${tvLink(r.symbol)}${fails}</td>
    <td class="num">$${fmtNum(r.price)}</td>
    <td class="num ${pctClass(r.day_pct)}">${fmtPct(r.day_pct)}</td>
    <td class="num">${r.dist_from_hod == null ? "–" : r.dist_from_hod < 0.05 ? '<span class="hot">HOD</span>' : "-" + r.dist_from_hod.toFixed(2) + "%"}</td>
    <td class="num">${fmtBig(r.day_volume)}</td>
    <td class="num">${r.rvol == null ? "–" : r.rvol.toFixed(1)}</td>
    ${floatCell(r.float_shares, true)}
    ${newsCell(r)}
  </tr>`;
}

function renderHod(payload) {
  const q = payload.hod.qualified || [];
  const near = payload.hod.near || [];
  const tbody = $("#hod tbody");
  const seen = new Set();
  let anyNew = false;
  const qHtml = q
    .map((r) => {
      seen.add(r.symbol);
      const isNew = prevHod.size && !prevHod.has(r.symbol);
      const newHigh = prevHighs[r.symbol] != null && r.day_high > prevHighs[r.symbol];
      if (isNew) anyNew = true;
      return hodRow(r, isNew ? "flash" : newHigh ? "hod-flash" : "");
    })
    .join("");
  const nearHtml = near.map((r) => hodRow(r, "near")).join("");
  tbody.innerHTML = qHtml + nearHtml;
  prevHod = seen;
  prevHighs = {};
  q.concat(near).forEach((r) => (prevHighs[r.symbol] = r.day_high));
  if (anyNew) beep();
  $("#hod-empty").classList.toggle("hidden", q.length + near.length > 0);
}

function renderCalendar(payload) {
  const now = payload.now;
  $("#calendar").innerHTML = (payload.calendar || [])
    .map(
      (e) => `<li class="${e.ts < now ? "past" : ""}">
        <span class="imp ${e.impact}"></span>
        <span class="t">${localTime(e.ts)}</span>
        <span>${e.title}</span>
        <span class="fx">${e.forecast ? "f " + e.forecast : ""}</span>
      </li>`
    )
    .join("") || '<li class="past">No red/orange events.</li>';
}

function renderNews(payload) {
  $("#news").innerHTML = (payload.news || [])
    .slice(0, 15)
    .map(
      (n) => `<li>
        <div class="meta"><span>${localTime(n.ts)}</span><span class="symchip">${n.symbol}</span></div>
        <a href="${n.url}" target="_blank" rel="noopener">${n.headline}</a>
      </li>`
    )
    .join("") || '<li class="past">No headlines yet.</li>';
}

function renderStatus(payload) {
  const badge = $("#mode-badge");
  badge.textContent = payload.mode.toUpperCase();
  badge.classList.toggle("live", payload.mode === "live");
  const status = $("#status");
  status.classList.remove("ok", "bad");
  const banner = $("#stale-banner");
  if (payload.stale_since) {
    status.classList.add("bad");
    $("#status-text").textContent = "stale";
    banner.textContent = `⚠ Data is stale — last update ${localTime(payload.stale_since)}. Serving last-good results.`;
    banner.classList.remove("hidden");
  } else {
    status.classList.add("ok");
    $("#status-text").textContent = payload.updated ? "updated " + localTime(payload.updated) : "waiting for data";
    banner.classList.add("hidden");
  }
  $("#clock").textContent = new Date().toLocaleTimeString();
}

// ---------- polling ----------
async function tick() {
  try {
    const requireNews = $("#require-news").checked ? 1 : 0;
    const resp = await fetch(`/api/state?require_news=${requireNews}`);
    const payload = await resp.json();
    renderStatus(payload);
    renderGainers(payload);
    renderHod(payload);
    renderCalendar(payload);
    renderNews(payload);
  } catch (err) {
    const status = $("#status");
    status.classList.remove("ok");
    status.classList.add("bad");
    $("#status-text").textContent = "server unreachable";
  }
}

$("#window-toggle").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button");
  if (!btn) return;
  windowMin = Number(btn.dataset.w);
  document.querySelectorAll("#window-toggle button").forEach((b) => b.classList.toggle("active", b === btn));
  $("#th-window").textContent = `%${windowMin}m`;
  $("#gainer-window-label").textContent = `last ${windowMin} min`;
  prevGainers = new Set();
  tick();
});

setInterval(tick, 1000);
tick();
