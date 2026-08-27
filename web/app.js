"use strict";

const $ = (sel) => document.querySelector(sel);

// Local server by default; the GitHub Pages build sets data-endpoint on
// <body> to read the committed status JSON instead.
const ENDPOINT = document.body.dataset.endpoint || "/api/state";
const STALE_AFTER_SECONDS = 900;

let windowMin = 5;
let prevGainers = new Set();
let prevHod = new Set();
let prevHighs = {};

// Long tables collapse to the newest ROW_LIMIT rows behind a toggle.
const ROW_LIMIT = 10;
const rowCounts = {};

function applyCollapse(id, count) {
  const table = $("#" + id);
  const btn = $("#" + id + "-more");
  if (!table || !btn) return;
  rowCounts[id] = count;
  const extra = count - ROW_LIMIT;
  if (extra <= 0) {
    btn.classList.add("hidden");
    return;
  }
  btn.classList.remove("hidden");
  btn.textContent = table.classList.contains("collapsed")
    ? `See ${extra} more ▾`
    : "Show less ▴";
}

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
const fmtMoney = (v) => `${v >= 0 ? "+" : "-"}$${Math.abs(v).toFixed(2)}`;
const tvLink = (sym) =>
  `<a href="https://www.tradingview.com/chart/?symbol=${sym}" target="_blank" rel="noopener">${sym}</a>`;
const localTime = (epoch) =>
  new Date(epoch * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const shortDate = (epoch) =>
  new Date(epoch * 1000).toLocaleDateString([], { month: "short", day: "numeric" });

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
function catalystCell(row) {
  const c = row.catalyst;
  if (!c) return '<td><span class="waiting">none</span></td>';
  const cls = c.veto ? "cat-veto" : c.score >= 0.6 ? "cat-strong" : c.score >= 0.3 ? "cat-ok" : "cat-weak";
  const age = c.age_minutes < 60 ? `${Math.round(c.age_minutes)}m` : `${Math.round(c.age_minutes / 60)}h`;
  const label = c.veto ? "offering ⚠" : `${c.category} ${c.score.toFixed(2)}`;
  return `<td title="${(c.headline || "").replace(/"/g, "'")}"><span class="cat-chip ${cls}">${label}</span> <span class="waiting">${age}</span></td>`;
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

const FAIL_LABELS = { pct_up: "%day", volume: "volume", rvol: "rvol", float: "float", hod: "off high", news: "no news", vwap: "below VWAP", liquidity: "too illiquid" };

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
    <td class="num ${r.gap_pct >= 10 ? "hot" : ""}">${r.gap_pct == null ? "–" : fmtPct(r.gap_pct)}</td>
    <td class="num ${r.above_vwap ? "up" : "down"}">${r.vwap == null ? "–" : (r.above_vwap ? "above" : "below")}</td>
    <td>${r.setup ? `<span class="setup-chip">${r.setup.setup.replace("_", " ")}</span>` : '<span class="waiting">waiting</span>'}</td>
    ${catalystCell(r)}
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
        <span class="fx" title="forecast vs previous reading">${e.forecast ? `fcst ${e.forecast}` : ""}${e.previous ? ` · prev ${e.previous}` : ""}</span>
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

function renderBot(payload) {
  const bot = payload.bot;
  const off = $("#bot-off");
  const body = $("#bot-body");
  const chips = $("#bot-chips");
  if (!bot) {
    off.classList.remove("hidden");
    body.classList.add("hidden");
    chips.innerHTML = "";
    return;
  }
  off.classList.add("hidden");
  body.classList.remove("hidden");

  const pnl = bot.day_pnl || 0;
  const pnlCls = pnl >= 0 ? "pnl-up" : "pnl-down";
  let chipHtml =
    `<span class="chip">trades ${bot.trades_today}/${bot.cap}</span>` +
    `<span class="chip ${pnlCls}">day ${fmtMoney(pnl)}</span>` +
    `<span class="chip">bankroll $${fmtBig(bot.bankroll)}</span>`;
  (bot.open || []).forEach((o) => {
    chipHtml += `<span class="chip warn">open: ${o.symbol} x${o.qty} @$${fmtNum(o.entry)}</span>`;
  });
  if (bot.error) chipHtml += `<span class="chip pnl-down">⚠ ${bot.error}</span>`;
  if (bot.trades_today >= bot.cap) chipHtml += `<span class="chip warn">daily cap reached</span>`;
  chips.innerHTML = chipHtml;

  const trades = bot.today || [];
  $("#bot-trades tbody").innerHTML = trades
    .map((t) => {
      const closed = t.exit_price != null;
      const rCls = closed ? (t.r_multiple >= 0 ? "up" : "down") : "";
      return `<tr>
        <td>${localTime(t.ts)}</td>
        <td class="sym">${tvLink(t.symbol)}</td>
        <td class="num">${t.qty}</td>
        <td class="num">$${fmtNum(t.entry)}</td>
        <td class="num">${closed ? "$" + fmtNum(t.exit_price) : "open"}</td>
        <td class="num ${rCls}">${closed ? (t.r_multiple >= 0 ? "+" : "") + t.r_multiple.toFixed(1) + "R" : "–"}</td>
        <td class="num ${rCls}">${closed ? fmtMoney(t.pnl) : "–"}</td>
        <td>${t.exit_reason || ""}</td>
      </tr>`;
    })
    .join("");
  $("#bot-no-trades").classList.toggle("hidden", trades.length > 0);

  const stats = bot.stats || {};
  const model = bot.model || {};
  const lp = bot.learning || {};
  const rows = [
    ["Progress to trained model",
     lp.labeled != null ? `${lp.labeled}/${lp.needed} (${lp.tradable || 0} tradable)` : "–"],
    ["Scoring", model.kind === "logreg" ? `trained model (v${(bot.model_history || []).length})` : "heuristic (collecting data)"],
    ["Training samples", model.samples ?? 0],
    ["Holdout accuracy", model.holdout_acc != null ? (model.holdout_acc * 100).toFixed(0) + "%" : "–"],
    [`Win rate (last ${stats.count || 0})`, stats.win_rate != null ? (stats.win_rate * 100).toFixed(0) + "%" : "–"],
    ["Expectancy", stats.expectancy_r != null ? (stats.expectancy_r >= 0 ? "+" : "") + stats.expectancy_r.toFixed(2) + "R / trade" : "–"],
  ];
  (bot.setup_stats || []).forEach((s) => {
    const exp = s.exp_r == null ? "–" : (s.exp_r >= 0 ? "+" : "") + s.exp_r.toFixed(2) + "R";
    rows.push([`↳ ${String(s.setup).replace("_", " ")} (${s.n})`,
               `${Math.round((s.wins / s.n) * 100)}% · ${exp}`]);
  });
  $("#bot-learning").innerHTML = rows
    .map(([k, v]) => `<li><span class="k">${k}</span><span>${v}</span></li>`)
    .join("");

  const eq = bot.equity || [];
  const svg = $("#bot-equity");
  if (eq.length > 1) {
    const values = eq.map((p) => p[1]);
    const min = Math.min(...values), max = Math.max(...values);
    const span = max - min || 1;
    const pts = eq
      .map((p, i) => `${(i / (eq.length - 1)) * 260},${76 - ((p[1] - min) / span) * 72}`)
      .join(" ");
    const last = values[values.length - 1], first = values[0];
    const color = last >= first ? "var(--green)" : "var(--red)";
    svg.innerHTML = `<polyline points="${pts}" fill="none" stroke="${color}" stroke-width="2"/>`;
  } else {
    svg.innerHTML = "";
  }

  const history = bot.recent || [];
  $("#bot-history tbody").innerHTML = history
    .map((t) => {
      const bought = (t.qty || 0) * (t.entry || 0);
      const cls = t.pnl >= 0 ? "up" : "down";
      return `<tr>
        <td>${shortDate(t.ts)}</td>
        <td class="sym">${tvLink(t.symbol)}</td>
        <td>${t.setup ? t.setup.replace("_", " ") : "–"}</td>
        <td class="num">$${fmtNum(bought)}</td>
        <td class="num">$${fmtNum(t.exit_price)}</td>
        <td class="num ${cls}">${fmtMoney(t.pnl)}</td>
        <td class="num ${cls}">${(t.r_multiple >= 0 ? "+" : "") + t.r_multiple.toFixed(1)}R</td>
        <td>${t.exit_reason || ""}</td>
      </tr>`;
    })
    .join("");
  $("#bot-history-empty").classList.toggle("hidden", history.length > 0);
  applyCollapse("bot-history", history.length);

  const orders = bot.orders || [];
  $("#bot-orders tbody").innerHTML = orders
    .map((o) => `<tr>
      <td class="sym">${tvLink(o.symbol)}</td>
      <td class="${o.side === "buy" ? "up" : "down"}">${o.side}</td>
      <td>${(o.type || "").replace("_", " ")}</td>
      <td class="num">${o.qty}</td>
      <td class="num">${o.limit_price ? "$" + fmtNum(+o.limit_price) : "–"}</td>
      <td class="num">${o.stop_price ? "$" + fmtNum(+o.stop_price) : "–"}</td>
      <td>${o.status || ""}</td>
    </tr>`)
    .join("");
  $("#bot-orders-empty").classList.toggle("hidden", orders.length > 0);

  const alerts = bot.alerts || [];
  $("#bot-alerts tbody").innerHTML = alerts
    .map((a) => {
      const done = a.label != null;
      const cls = !done ? "" : a.label === 1 ? "up" : "down";
      const outcome = !done ? "tracking…" : a.label === 1 ? "hit +2R" : "failed";
      return `<tr>
        <td>${a.day || shortDate(a.ts)}</td>
        <td>${localTime(a.ts)}</td>
        <td class="sym">${tvLink(a.symbol)}</td>
        <td class="num">$${fmtNum(a.price)}</td>
        <td>${a.setup ? a.setup.replace("_", " ") : "–"}</td>
        <td>${a.observed ? '<span class="waiting">near miss</span>' : '<span class="setup-chip">tradable</span>'}</td>
        <td class="${cls}">${outcome}</td>
      </tr>`;
    })
    .join("");
  $("#bot-alerts-empty").classList.toggle("hidden", alerts.length > 0);
  applyCollapse("bot-alerts", alerts.length);
}

function renderStatus(payload) {
  const badge = $("#mode-badge");
  badge.textContent = payload.mode.toUpperCase();
  badge.classList.toggle("live", payload.mode === "live");
  const status = $("#status");
  status.classList.remove("ok", "bad");
  const banner = $("#stale-banner");
  // On the static cloud page, an old snapshot means the session has ended.
  if (!payload.stale_since && payload.now &&
      Date.now() / 1000 - payload.now > STALE_AFTER_SECONDS) {
    payload.stale_since = payload.now;
  }
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
    const sep = ENDPOINT.includes("?") ? "&" : "?";
    const resp = await fetch(`${ENDPOINT}${sep}require_news=${requireNews}&_=${Date.now()}`);
    const payload = await resp.json();
    renderStatus(payload);
    renderGainers(payload);
    renderHod(payload);
    renderCalendar(payload);
    renderNews(payload);
    renderBot(payload);
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

["bot-history", "bot-alerts"].forEach((id) => {
  const btn = $("#" + id + "-more");
  if (!btn) return;
  btn.addEventListener("click", () => {
    $("#" + id).classList.toggle("collapsed");
    applyCollapse(id, rowCounts[id] || 0);
  });
});

setInterval(tick, 1000);
tick();
