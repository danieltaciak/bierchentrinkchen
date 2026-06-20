"use strict";

const DE = new Intl.NumberFormat("de-DE");
const fmt = (n) => DE.format(n);

const BOARD_UNITS = {
  all: "PUNKTE",
  last24h: "/24H",
  longest_streak: "FOLGE",
  assists: "VORLAGEN",
  night_owls: "NACHTS",
};

const SVGNS = "http://www.w3.org/2000/svg";

let DATA = null;
let PHONE_BY_ID = {};

// Stats live on a separate, frequently-updated branch (scoreboard-data) so the
// page on main stays static. We read them newest-first:
//   1. GitHub contents API -- freshest (~60s edge cache), CORS-enabled, but
//      rate-limited to 60 req/h per IP when unauthenticated;
//   2. raw.githubusercontent -- unlimited, but its CDN caches each path for up
//      to 5 min and ignores query strings, so it can lag a few minutes;
//   3. a local copy for offline/local development.
const DATA_SOURCES = [
  {
    url: "https://api.github.com/repos/danieltaciak/bierchentrinkchen/contents/stats.json?ref=scoreboard-data",
    headers: { Accept: "application/vnd.github.raw" },
  },
  { url: "https://raw.githubusercontent.com/danieltaciak/bierchentrinkchen/scoreboard-data/stats.json" },
  { url: "data/stats.json" },
];

async function fetchStats() {
  let lastErr = null;
  for (const src of DATA_SOURCES) {
    try {
      const sep = src.url.includes("?") ? "&" : "?";
      const res = await fetch(src.url + sep + "ts=" + Date.now(), {
        cache: "no-store",
        headers: src.headers || {},
      });
      if (!res.ok) throw new Error(`HTTP ${res.status} for ${src.url}`);
      return await res.json();
    } catch (e) {
      lastErr = e;
    }
  }
  throw lastErr || new Error("no data source reachable");
}

async function load() {
  try {
    DATA = await fetchStats();
  } catch (e) {
    document.getElementById("bigCount").textContent = "ERR";
    console.error(e);
    return;
  }
  PHONE_BY_ID = {};
  for (const p of DATA.players || []) {
    if (p.anon_phone) PHONE_BY_ID[p.id] = p.anon_phone;
  }
  render();
}

// small grey anonymised-phone line shown below a name
function phoneLine(phone) {
  return phone ? `<span class="phone-hint">${esc(phone)}</span>` : "";
}

function render() {
  renderCounter();
  renderQuickStats();
  renderCumulative();
  renderParty();
  renderBoard("all");
  renderRecords();
  renderDaily();
  renderHours();
  renderRecent();
  wireTabs();
  document.getElementById("updated").textContent = new Date(
    DATA.generated_at
  ).toLocaleString("de-DE");
}

/* ---------------- counter ---------------- */
function renderCounter() {
  document.getElementById("target").textContent = fmt(DATA.target);
  const bc = document.getElementById("bigCount");
  animateNumber(bc, DATA.current_count);
  if (DATA.current_count % 100 === 69) {
    setTimeout(() => spawnNice(bc), 1180);
  }

  const pct = DATA.progress_pct;
  const visual = Math.max(0.5, Math.sqrt(pct / 100) * 100);
  requestAnimationFrame(() => {
    document.getElementById("progressFill").style.width = visual + "%";
  });
  document.getElementById("progressPct").textContent =
    pct.toFixed(4).replace(".", ",") + " %";

  const tl = DATA.timeline || [];
  if (tl.length >= 1 && DATA.current_count > 0) {
    const perDay = DATA.current_count / tl.length;
    if (perDay > 0) {
      const years = (DATA.target - DATA.current_count) / perDay / 365;
      document.getElementById("etaText").textContent =
        "ZIEL IN ~" + fmt(Math.round(years)) + " J.";
    }
  }
}

function spawnNice(host) {
  const el = document.createElement("span");
  el.className = "nice-pop";
  el.textContent = "nice!";
  host.appendChild(el);
  setTimeout(() => el.remove(), 1700);
}

function renderQuickStats() {
  const tl = DATA.timeline || [];
  const today = tl.length ? tl[tl.length - 1].count : 0;
  const stats = [
    { v: fmt(DATA.num_players), l: "Mittrinkende" },
    { v: fmt(today), l: "heute" },
    { v: fmt(DATA.records?.busiest_day?.value || 0), l: "Tagesrekord" },
    { v: fmt(DATA.records?.longest_streak?.value || 0), l: "längste Serie" },
  ];
  document.getElementById("quickStats").innerHTML = stats
    .map((s) => `<div class="qs"><b>${s.v}</b><span>${s.l}</span></div>`)
    .join("");
}

/* ---------------- cumulative line chart ---------------- */
function renderCumulative() {
  const tl = DATA.timeline || [];
  const host = document.getElementById("cumulativeChart");
  host.innerHTML = "";
  if (tl.length < 2) return;

  const W = 920, H = 300;
  const padL = 64, padR = 18, padT = 16, padB = 34;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;

  const maxTotal = tl[tl.length - 1].total || 1;
  const n = tl.length;
  const x = (i) => padL + (n === 1 ? 0 : (i / (n - 1)) * innerW);
  const y = (v) => padT + innerH - (v / maxTotal) * innerH;

  const svg = document.createElementNS(SVGNS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");

  const defs = document.createElementNS(SVGNS, "defs");
  defs.innerHTML =
    `<linearGradient id="beerFill" x1="0" y1="0" x2="0" y2="1">
       <stop offset="0%" stop-color="#f8d24a" stop-opacity="0.55"/>
       <stop offset="100%" stop-color="#e8a020" stop-opacity="0.05"/>
     </linearGradient>`;
  svg.appendChild(defs);

  // horizontal grid + y labels (4 steps)
  for (let s = 0; s <= 4; s++) {
    const v = (maxTotal / 4) * s;
    const gy = y(v);
    const line = document.createElementNS(SVGNS, "line");
    line.setAttribute("class", "lc-grid");
    line.setAttribute("x1", padL); line.setAttribute("x2", W - padR);
    line.setAttribute("y1", gy); line.setAttribute("y2", gy);
    svg.appendChild(line);
    const lbl = document.createElementNS(SVGNS, "text");
    lbl.setAttribute("class", "axis");
    lbl.setAttribute("x", padL - 8); lbl.setAttribute("y", gy + 3);
    lbl.setAttribute("text-anchor", "end");
    lbl.textContent = fmt(Math.round(v));
    svg.appendChild(lbl);
  }

  // area + line paths
  let line = `M ${x(0)} ${y(tl[0].total)}`;
  tl.forEach((d, i) => { line += ` L ${x(i)} ${y(d.total)}`; });
  const area = `${line} L ${x(n - 1)} ${y(0)} L ${x(0)} ${y(0)} Z`;

  const areaEl = document.createElementNS(SVGNS, "path");
  areaEl.setAttribute("class", "lc-area");
  areaEl.setAttribute("d", area);
  svg.appendChild(areaEl);

  const lineEl = document.createElementNS(SVGNS, "path");
  lineEl.setAttribute("class", "lc-line");
  lineEl.setAttribute("d", line);
  svg.appendChild(lineEl);

  // end dot
  const dot = document.createElementNS(SVGNS, "rect");
  dot.setAttribute("class", "lc-dot");
  dot.setAttribute("x", x(n - 1) - 3); dot.setAttribute("y", y(tl[n - 1].total) - 3);
  dot.setAttribute("width", 6); dot.setAttribute("height", 6);
  svg.appendChild(dot);

  // x labels: first, middle, last
  [0, Math.floor((n - 1) / 2), n - 1].forEach((i) => {
    const t = document.createElementNS(SVGNS, "text");
    t.setAttribute("class", "axis");
    t.setAttribute("x", x(i));
    t.setAttribute("y", H - 12);
    t.setAttribute("text-anchor", i === 0 ? "start" : i === n - 1 ? "end" : "middle");
    t.textContent = new Date(tl[i].date).toLocaleDateString("de-DE", {
      day: "2-digit", month: "2-digit",
    });
    svg.appendChild(t);
  });

  host.appendChild(svg);
}

/* ---------------- party (top 3) ---------------- */
function renderParty() {
  const top3 = DATA.players.slice(0, 3);
  const order = [1, 0, 2];
  const cls = { 0: "m1", 1: "m2", 2: "m3" };
  const html = order
    .map((idx) => {
      const p = top3[idx];
      if (!p) return "";
      return `
        <div class="member ${cls[idx]}">
          <div class="lvl">RANG ${idx + 1}</div>
          ${avatarSVG(p.id)}
          <div class="pname">${esc(p.name)}</div>
          ${phoneLine(p.anon_phone)}
          <div class="ppoints">${fmt(p.points)}</div>
          <div class="plabel">Bierle · ${p.pct.toString().replace(".", ",")} %</div>
        </div>`;
    })
    .join("");
  document.getElementById("party").innerHTML = html;
}

// deterministic little pixel sigil so each hero has a face
function avatarSVG(id) {
  let h = 0;
  for (const c of id) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  const palette = ["#f8d24a", "#e8a020", "#7a8ae0", "#c8cfe8", "#d08a4e"];
  const fg = palette[h % palette.length];
  let cells = "";
  for (let yy = 0; yy < 5; yy++) {
    for (let xx = 0; xx < 3; xx++) {
      h = (h * 1103515245 + 12345) >>> 0;
      if (h & 1) {
        cells += `<rect x="${xx}" y="${yy}" width="1" height="1"/>`;
        cells += `<rect x="${4 - xx}" y="${yy}" width="1" height="1"/>`;
      }
    }
  }
  return `<svg class="avatar" viewBox="0 0 5 5" fill="${fg}">
            <g shape-rendering="crispEdges">${cells}</g></svg>`;
}

/* ---------------- leaderboard ---------------- */
function boardRows(board) {
  if (board === "all") {
    return DATA.players.slice(0, 25).map((p) => ({
      name: p.name, value: p.points, sub: p.pct.toString().replace(".", ",") + " %",
      phone: p.anon_phone,
    }));
  }
  if (board === "last24h") {
    return DATA.players
      .filter((p) => p.last24h > 0)
      .sort((a, b) => b.last24h - a.last24h)
      .slice(0, 25)
      .map((p) => ({ name: p.name, value: p.last24h, sub: "", phone: p.anon_phone }));
  }
  return (DATA.leaderboards?.[board] || []).map((e) => ({
    name: e.name, value: e.value, sub: "", phone: PHONE_BY_ID[e.id],
  }));
}

function renderBoard(board) {
  const rows = boardRows(board);
  const unit = BOARD_UNITS[board] || "";
  const max = rows.reduce((m, r) => Math.max(m, r.value), 0) || 1;
  document.getElementById("leaderboard").innerHTML = rows
    .map((r, i) => {
      const rankCls = i < 3 ? `top${i + 1}` : "";
      const w = Math.max(4, (r.value / max) * 100);
      const sub = r.sub ? ` <small>${r.sub}</small>` : "";
      return `
        <li class="${rankCls}">
          <span class="lrank">${String(i + 1).padStart(2, "0")}</span>
          <span class="lname">${esc(r.name)}${sub}${phoneLine(r.phone)}</span>
          <span class="lval">${fmt(r.value)} <span class="lunit">${unit}</span></span>
          <span class="lbar" style="width:${w}%"></span>
        </li>`;
    })
    .join("");
}

function wireTabs() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      renderBoard(tab.dataset.board);
    });
  });
}

/* ---------------- records (curated) ---------------- */
function renderRecords() {
  const r = DATA.records || {};
  const cards = [
    r.longest_streak && {
      title: "Längste Serie",
      name: r.longest_streak.name,
      phone: PHONE_BY_ID[r.longest_streak.id],
      value: `${r.longest_streak.value} Bierle am Stück (bis ${fmt(r.longest_streak.end_n)})`,
    },
    r.busiest_day && {
      title: "Fleißigster Tag",
      name: new Date(r.busiest_day.date).toLocaleDateString("de-DE"),
      value: `${fmt(r.busiest_day.value)} Bierle an einem Tag`,
    },
    r.night_owl && {
      title: "Nachteule",
      name: r.night_owl.name,
      phone: PHONE_BY_ID[r.night_owl.id],
      value: `${r.night_owl.value} Bierle zwischen 0 und 5 Uhr`,
    },
    r.early_bird && {
      title: "Frühaufstehende",
      name: r.early_bird.name,
      phone: PHONE_BY_ID[r.early_bird.id],
      value: `${r.early_bird.value} Bierle am frühen Morgen`,
    },
    r.top_assist && {
      title: "Meiste Vorlagen",
      name: r.top_assist.name,
      phone: PHONE_BY_ID[r.top_assist.id],
      value: `${r.top_assist.value} Punkte an andere verteilt`,
    },
  ].filter(Boolean);

  document.getElementById("records").innerHTML = cards
    .map(
      (c) => `
      <div class="record-card">
        <div class="rc-title">${c.title}</div>
        <div class="rc-name">${esc(c.name)}</div>
        ${phoneLine(c.phone)}
        <div class="rc-value">${esc(c.value)}</div>
      </div>`
    )
    .join("");
}

/* ---------------- daily / hours / recent ---------------- */
function renderDaily() {
  const tl = DATA.timeline || [];
  const max = tl.reduce((m, d) => Math.max(m, d.count), 0) || 1;
  document.getElementById("dailyBars").innerHTML = tl
    .map((d) => {
      const h = (d.count / max) * 100;
      const label = new Date(d.date).toLocaleDateString("de-DE", {
        day: "2-digit", month: "2-digit",
      });
      return `<div class="bar" title="${d.date}: ${d.count}">
        <span class="bval">${d.count}</span>
        <div class="fill" style="height:${h}%"></div>
        <span class="blabel">${label}</span>
      </div>`;
    })
    .join("");
}

function renderHours() {
  const hist = DATA.hour_histogram || [];
  const max = hist.reduce((m, v) => Math.max(m, v), 0) || 1;
  document.getElementById("hourBars").innerHTML = hist
    .map((v, h) => {
      const ht = (v / max) * 100;
      const peak = v === max ? "peak" : "";
      const lbl = h % 6 === 0 ? `<span>${h}</span>` : "";
      return `<div class="hbar ${peak}" style="height:${ht}%" title="${h}:00 – ${v}">${lbl}</div>`;
    })
    .join("");
}

function renderRecent() {
  document.getElementById("recent").innerHTML = (DATA.recent || [])
    .map(
      (e) => `<li><span class="rn">${fmt(e.n)}</span>
        <span class="rwho">${esc(e.name)}</span></li>`
    )
    .join("");
}

/* ---------------- helpers ---------------- */
function animateNumber(el, target) {
  const dur = 1100, start = performance.now();
  function step(now) {
    const t = Math.min(1, (now - start) / dur);
    const eased = 1 - Math.pow(1 - t, 3);
    el.textContent = fmt(Math.round(target * eased));
    if (t < 1) requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
  setTimeout(() => { el.textContent = fmt(target); }, dur + 80);
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

load();
