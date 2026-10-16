"use strict";
/* Güvenlik Kamerası – arayüz */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const icon = (n) => `<svg class="i"><use href="#i-${n}"/></svg>`;
const pad = (n) => String(n).padStart(2, "0");
const DAY = 86400000;
const MONTHS = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"];

// cihaz saati ("YYYY-MM-DDTHH:MM:SS", yerel) <-> ms
const parseT = (s) => { const m = s.match(/(\d+)-(\d+)-(\d+)[T ](\d+):(\d+):(\d+)/); return new Date(+m[1], m[2] - 1, +m[3], +m[4], +m[5], +m[6]).getTime(); };
const fmtISO = (t) => { const d = new Date(t); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
const fmtTime = (t) => { const d = new Date(t); return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
const fmtDate = (t) => { const d = new Date(t); return `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`; };
const dayStart = (t) => { const d = new Date(t); d.setHours(0, 0, 0, 0); return d.getTime(); };
const dayKey = (t) => fmtISO(dayStart(t)).slice(0, 10);
const fmtDur = (ms) => { const s = Math.round(ms / 1000); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60; return h ? `${h} sa ${m} dk ${x} sn` : m ? `${m} dk ${x} sn` : `${x} sn`; };
const fmtSize = (b) => b > 1e9 ? (b / 1e9).toFixed(2) + " GB" : b > 1e6 ? (b / 1e6).toFixed(1) + " MB" : Math.max(1, Math.round(b / 1e3)) + " KB";

async function api(path, opts = {}) {
  const o = { ...opts };
  if (o.body && typeof o.body !== "string") { o.body = JSON.stringify(o.body); o.headers = { "Content-Type": "application/json" }; }
  const r = await fetch(path, o);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `İstek başarısız (${r.status})`);
  return data;
}

function toast(text, { err = false, action = null, time = 4500 } = {}) {
  const el = document.createElement("div");
  el.className = "toast" + (err ? " err" : "");
  el.innerHTML = `<div class="t"></div>`;
  $(".t", el).textContent = text;
  if (action) {
    const b = document.createElement("button");
    b.className = "btn small"; b.textContent = action.label;
    b.onclick = () => { action.run(); el.remove(); };
    el.appendChild(b);
  }
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), time);
}

// ------------------------------------------------------------------ durum
const S = {
  state: null,
  page: "live",            // live | archive | downloads | settings
  channels: [],
  single: null,            // tek görünümdeki kanal
  pos: null,               // oynatma konumu (ms, cihaz saati); null = canlı
  playing: true,
  speed: 1,
  devOffset: 0,
  useH264: !(window.MediaSource && (MediaSource.isTypeSupported('video/mp4; codecs="hev1.1.6.L93.B0"') || MediaSource.isTypeSupported('video/mp4; codecs="hvc1.1.6.L93.B0"'))),
  recs: {},                // dayKey -> {ch: [[s,e,kind]]}
  recsLoading: {},
  clip: null,              // {start, end}
  zoom: { k: 1, x: 0, y: 0 },
  players: new Map(),
};
const devNow = () => Date.now() + S.devOffset;
const isWatch = () => S.page === "live" || S.page === "archive";

// ------------------------------------------------------------------ oynatıcı
class Player {
  constructor(ch, tile) {
    this.ch = ch;
    this.tile = tile;
    this.video = $("video", tile);
    this.ws = null;
    this.gen = 0;
    this.h264 = S.useH264;      // bu akış için H.264 yedeği (yalnızca bu kamera etkilenir)
    this.copyErrors = 0;
  }

  open({ start = null, speed = 1, single = false } = {}) {
    this.close();
    const gen = ++this.gen;
    this.start = start;
    this.speed = speed;
    this.base = null;
    this.queue = [];
    this.ended = false;
    this.failed = false;
    this.overlay("loading");
    this.setTag();
    this.args = { start, speed, single };
    const q = new URLSearchParams({ ch: this.ch, sub: single ? 0 : 1, codec: this.h264 ? "h264" : "copy", speed });
    if (start != null) q.set("start", fmtISO(start));
    if (!single) q.set("small", "1");
    const ws = new WebSocket(`ws://${location.host}/ws/stream?${q}`);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onmessage = (e) => {
      if (gen !== this.gen) return;
      if (typeof e.data === "string") return this.onText(JSON.parse(e.data));
      this.queue.push(e.data);
      this.pump();
    };
    ws.onclose = () => {
      if (gen !== this.gen || this.failed || this.ended) return;
      // canlıda bağlantı koparsa yeniden dene
      if (this.start == null && isWatch()) { this.overlay("loading"); setTimeout(() => gen === this.gen && this.open({ start: null, speed: 1, single }), 2500); }
    };
    this.single = single;
  }

  onText(m) {
    if (m.type === "error") { this.failed = true; this.overlay("msg", m.message); return; }
    if (m.type === "end") { this.ended = true; if (this.start != null) this.overlay("msg", "Kayıt sonu"); return; }
    if (m.type === "init") this.setupMSE(m.codec);
  }

  setupMSE(codec) {
    let mime = `video/mp4; codecs="${codec}"`;
    if (!MediaSource.isTypeSupported(mime)) {
      const fam = codec.slice(0, 4);
      const generic = { hev1: 'hev1.1.6.L93.B0', hvc1: 'hvc1.1.6.L93.B0', avc1: 'avc1.640028' }[fam];
      if (generic && MediaSource.isTypeSupported(`video/mp4; codecs="${generic}"`)) mime = `video/mp4; codecs="${generic}"`;
      else if (!this.h264) { this.h264 = true; return this.reopen(); }
      else { this.failed = true; return this.overlay("msg", "Bu görüntü biçimi oynatılamıyor"); }
    }
    const ms = new MediaSource();
    this.ms = ms;
    this.video.src = URL.createObjectURL(ms);
    ms.addEventListener("sourceopen", () => {
      try { this.sb = ms.addSourceBuffer(mime); } catch (e) { this.failed = true; return this.overlay("msg", "Oynatıcı başlatılamadı"); }
      this.sb.mode = "segments";
      this.sb.addEventListener("updateend", () => this.pump());
      this.pump();
    }, { once: true });
    this.video.onerror = () => {
      // Çözme hatası: önce aynı (kayıpsız) akışı bir kez daha dene; tekrarlarsa yalnızca bu kamerayı
      // H.264'e çevirerek göster. Diğer kameralar kayıpsız kalır.
      if (this.h264) return;
      this.copyErrors++;
      if (this.copyErrors >= 2) this.h264 = true;
      this.reopen();
    };
  }

  reopen() {
    const a = this.args || {};
    const pos = a.start != null && this.base != null ? this.position() : a.start;
    this.open({ start: pos, speed: a.speed || 1, single: a.single });
  }

  pump() {
    const sb = this.sb;
    if (!sb || sb.updating || !this.queue.length || this.ms.readyState !== "open") return;
    const v = this.video;
    // eski tamponu temizle
    if (sb.buffered.length && v.currentTime - sb.buffered.start(0) > 40) {
      try { sb.remove(0, v.currentTime - 20); return; } catch (e) { }
    }
    const chunk = this.queue.length > 1 ? concat(this.queue.splice(0)) : this.queue.shift();
    try { sb.appendBuffer(chunk); } catch (e) {
      if (e.name === "QuotaExceededError") { this.queue.unshift(chunk); try { sb.remove(0, v.currentTime - 5); } catch (_) { } }
      return;
    }
    sb.addEventListener("updateend", () => this.afterAppend(), { once: true });
  }

  afterAppend() {
    const v = this.video, b = this.sb.buffered;
    if (!b.length) return;
    if (this.base == null) {
      this.base = b.start(0);
      v.currentTime = b.start(0);
      v.playbackRate = this.speed;
      if (S.playing) v.play().catch(() => { });
      this.overlay(null);
    }
    const end = b.end(b.length - 1);
    if (this.start == null && end - v.currentTime > 1.5) v.currentTime = end - 0.3;   // canlıda gecikmeyi düşük tut
    if (v.paused && S.playing && v.readyState >= 2) v.play().catch(() => { });
  }

  position() {
    if (this.start == null) return devNow();
    if (this.base == null) return this.start;
    return this.start + (this.video.currentTime - this.base) * 1000;
  }

  pause() { this.video.pause(); }

  stop() {   // akışı kes ama son kare ekranda kalsın
    this.gen++;
    if (this.ws) { this.ws.onclose = null; try { this.ws.close(); } catch (e) { } this.ws = null; }
    this.video.pause();
  }

  close() {
    this.gen++;
    if (this.ws) { this.ws.onclose = null; try { this.ws.close(); } catch (e) { } this.ws = null; }
    if (this.ms) { try { URL.revokeObjectURL(this.video.src); } catch (e) { } }
    this.sb = null; this.ms = null;
    this.video.onerror = null;
    this.video.removeAttribute("src");
    this.video.load();
  }

  setTag() {
    const tag = $(".tag", this.tile);
    if (this.start == null) { tag.className = "tag live"; tag.textContent = "CANLI"; }
    else { tag.className = "tag"; tag.textContent = this.speed > 1 ? `KAYIT · ${this.speed}×` : "KAYIT"; }
  }

  overlay(kind, text = "") {
    const o = $(".overlay", this.tile);
    if (!kind) { o.innerHTML = ""; return; }
    o.innerHTML = kind === "loading" ? `<div class="msg"><div class="spinner"></div></div>` : `<div class="msg"></div>`;
    if (kind === "msg") $(".msg", o).textContent = text;
  }
}

function concat(parts) {
  const n = parts.reduce((a, p) => a + p.byteLength, 0);
  const out = new Uint8Array(n);
  let i = 0;
  for (const p of parts) { out.set(new Uint8Array(p), i); i += p.byteLength; }
  return out.buffer;
}

// ------------------------------------------------------------------ ızgara / tek görünüm
function visibleChannels() { return S.single ? S.channels.filter((c) => c.id === S.single) : S.channels; }

function buildGrid() {
  for (const p of S.players.values()) p.close();
  S.players.clear();
  const grid = $("#grid");
  grid.innerHTML = "";
  const chans = visibleChannels();
  const n = Math.max(1, chans.length);
  const cols = n === 1 ? 1 : n <= 4 ? 2 : n <= 9 ? 3 : 4;
  grid.style.setProperty("--cols", cols);
  grid.style.setProperty("--rows", Math.ceil(n / cols));
  for (const c of chans) {
    const tile = document.createElement("div");
    tile.className = "tile";
    tile.innerHTML = `<div class="vwrap"><video muted playsinline disablepictureinpicture></video></div><div class="overlay"></div>
      <div class="tag"></div><div class="label">${icon("cam")}<span></span></div>
      <div class="tools"><button data-act="snap" title="Kare al">${icon("snap")}</button><button data-act="open" title="Büyüt">${icon("expand")}</button></div>`;
    $(".label span", tile).textContent = c.name;
    tile.addEventListener("click", (e) => {
      const act = e.target.closest("button")?.dataset.act;
      if (act === "snap") { e.stopPropagation(); return snapshot(c.id); }
      if (!S.single) openSingle(c.id);
    });
    grid.appendChild(tile);
    S.players.set(c.id, new Player(c.id, tile));
    fitAspect(tile, () => displayAspect(c));
  }
  $("#stage").classList.toggle("single", !!S.single);
  $("#single-bar").hidden = !S.single;
  if (S.single) {
    $("#single-title").textContent = chans[0]?.name || "";
    setupZoom($(".tile", grid));
  }
  startAll();
  timeline.draw();
}

// Görüntü oranı: kameranın gerçek oranında göster (alt akış ve "N" çözünürlükler yatayda sıkıştırılmıştır)
const MP_ASPECT = { 1: 16 / 9, 2: 16 / 9, 4: 16 / 9, 5: 4 / 3 };
function displayAspect(c, video) {
  const p = c?.prefs || {}, a = p.aspect || "auto";
  if (a === "16:9") return 16 / 9;
  if (a === "4:3") return 4 / 3;
  if (a === "orig") return c?.aspect || (video?.videoWidth ? video.videoWidth / video.videoHeight : 16 / 9);
  return MP_ASPECT[p.mp || 5] || 16 / 9;
}

function fitAspect(tile, getA) {
  const v = $("video", tile);
  v.style.objectFit = "fill";
  v.style.position = "absolute";
  const fit = () => {
    const a = getA();
    const W = tile.clientWidth, H = tile.clientHeight;
    let w = W, h = W / a;
    if (h > H) { h = H; w = H * a; }
    Object.assign(v.style, { width: w + "px", height: h + "px", left: (W - w) / 2 + "px", top: (H - h) / 2 + "px" });
  };
  tile._fit = fit;
  new ResizeObserver(fit).observe(tile);
  v.addEventListener("loadedmetadata", fit);
  fit();
}

function startAll() {
  const playback = S.pos != null;
  for (const p of S.players.values()) {
    if (!S.playing && playback) { p.close(); p.start = S.pos; p.overlay("msg", "Duraklatıldı"); p.setTag(); continue; }
    p.open({ start: playback ? S.pos : null, speed: playback ? S.speed : 1, single: !!S.single });
  }
  updateControls();
}

function reopenAll() {
  if (!isWatch()) return;
  if (S.pos != null) S.pos = currentPos();
  startAll();
}

function openSingle(ch) {
  if (S.pos != null) S.pos = currentPos();
  S.single = ch;
  S.zoom = { k: 1, x: 0, y: 0 };
  buildGrid();
}

function closeSingle() {
  if (!S.single) return;
  if (S.pos != null) S.pos = currentPos();
  S.single = null;
  buildGrid();
}

function currentPos() {
  const p = S.players.values().next().value;
  if (S.pos == null) return null;
  if (!p || !S.playing) return S.pos;
  const t = p.position();
  return isFinite(t) ? t : S.pos;
}

// ------------------------------------------------------------------ zaman / konum
function seek(t) {
  const now = devNow();
  if (S.page === "live" && t >= now - 4000) return goLive();
  t = Math.min(t, now - 5000);
  S.pos = Math.round(t);
  S.playing = true;
  ensureRecords(S.pos);
  startAll();
  timeline.follow(S.pos, true);
}

function goLive() {
  S.pos = null;
  S.playing = true;
  S.speed = 1;
  startAll();
  timeline.follow(devNow(), true);
}

function togglePlay() {
  if (S.pos == null) {             // canlıyı duraklat -> o andan kayıt moduna geç
    S.pos = devNow() - 2000;
    S.playing = false;
    for (const p of S.players.values()) { p.stop(); p.start = S.pos; p.setTag(); }
    updateControls();
    return;
  }
  if (S.playing) {
    S.pos = currentPos();
    S.playing = false;
    for (const p of S.players.values()) { p.stop(); p.start = S.pos; }
    updateControls();
  } else {
    S.playing = true;
    startAll();
  }
}

function setSpeed(v) {
  S.speed = v;
  if (S.pos == null) { toast("Hızlı oynatma kayıtta kullanılabilir. Zaman çizelgesinde geçmiş bir ana tıklayın."); updateControls(); return; }
  S.pos = currentPos();
  if (S.playing) startAll(); else updateControls();
}

function updateControls() {
  const live = S.pos == null;
  $("#btn-play").innerHTML = icon(S.playing ? "pause" : "play");
  $("#btn-live").classList.toggle("on", live);
  $("#btn-live").hidden = S.page !== "live";
  $("#date").hidden = S.page !== "archive";
  $$("#speed button").forEach((b) => b.classList.toggle("on", +b.dataset.speed === (live ? 1 : S.speed)));
  $("#speed").style.opacity = live ? .5 : 1;
}

const rate = { samples: [] };
function effectiveSpeed(t) {
  // son ~4 sn'de kayıt konumunun gerçek ilerleme hızı
  const now = Date.now(), s = rate.samples;
  s.push([now, t]);
  while (s.length && now - s[0][0] > 4000) s.shift();
  if (s.length < 2 || now - s[0][0] < 3000) return null;
  return (t - s[0][1]) / (now - s[0][0]);
}

function tickClock() {
  if (!isWatch()) return;
  const t = S.pos == null ? devNow() : currentPos();
  let mode = S.pos == null ? " · canlı" : !S.playing ? " · duraklatıldı" : " · kayıt";
  if (S.pos != null && S.playing && S.speed > 1) {
    const eff = effectiveSpeed(t);
    mode = ` · ${S.speed}× kayıt`;
    if (eff != null && eff < S.speed * 0.7) mode = ` · ~${Math.max(1, eff).toFixed(1)}× (internet hızı sınırlı)`;
  } else rate.samples = [];
  $("#clock").innerHTML = `${fmtTime(t)}<small>${fmtDate(t)}${mode}</small>`;
  if (S.pos != null && S.playing) {
    // kayıt ilerlerken çizelgeyi takip et ve gün değişince kayıtları yükle
    ensureRecords(t);
    if (S.page === "live" && t > devNow() - 3000) goLive();
  }
  timeline.follow(t, false);
  timeline.draw();
}

// ------------------------------------------------------------------ kayıt listesi
async function ensureRecords(t, force = false) {
  const key = dayKey(t);
  const chans = S.channels.map((c) => c.id);
  if (!chans.length || !S.state?.device?.connected) return;
  const isToday = key === dayKey(devNow());
  const cached = S.recs[key];
  // Cihaza her istek loguna "giriş" olarak düşer: bugünün kayıt listesi 5 dakikada bir tazelenir
  if (!force && cached && !cached._force && (!isToday || Date.now() - cached._at < 300000)) return;
  if (!force && cached && cached._force && Date.now() - cached._at < 300000) return;
  if (S.recsLoading[key]) return;
  S.recsLoading[key] = true;
  try {
    const r = await api(`/api/records?date=${key}&ch=${chans.join(",")}`);
    // bazı kameraların listesi alınamadıysa 20 sn sonra yeniden denensin (şerit boş kalmasın)
    const m = { _at: (r.failed && r.failed.length) ? Date.now() - 280000 : Date.now(), _force: !!(r.failed && r.failed.length) };
    for (const [ch, list] of Object.entries(r.records)) m[ch] = list.map(([a, b, k]) => [parseT(a), parseT(b), k]);
    const prev = S.recs[key];
    if (prev) for (const ch of r.failed || []) if (prev[ch]?.length) m[ch] = prev[ch];   // eldeki iyi veriyi koru
    S.recs[key] = m;
    timeline.draw();
  } catch (e) { /* bağlantı yoksa sessiz geç */ }
  finally { delete S.recsLoading[key]; }
}

function recordsFor(ch, from, to) {
  const out = [];
  for (let d = dayStart(from); d <= to; d += DAY) {
    const m = S.recs[dayKey(d + 3600000)];
    if (m && m[ch]) for (const r of m[ch]) if (r[1] > from && r[0] < to) out.push(r);
  }
  return out;
}

function firstRecordAfter(t) {
  let best = null;
  for (const c of S.channels) for (const r of recordsFor(c.id, t, t + DAY)) {
    const s = Math.max(r[0], t);
    if (best == null || s < best) best = s;
  }
  return best;
}

// ------------------------------------------------------------------ zaman çizelgesi
const timeline = {
  cv: null, ctx: null, view: { start: 0, span: 2 * 3600000 }, lastUser: 0, drag: null,
  init() {
    this.cv = $("#tl-canvas");
    this.ctx = this.cv.getContext("2d");
    const wrap = $("#timeline");
    new ResizeObserver(() => this.draw()).observe(wrap);
    wrap.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = wrap.getBoundingClientRect();
      const fx = (e.clientX - r.left) / r.width;
      const at = this.view.start + fx * this.view.span;
      const span = Math.min(3 * DAY, Math.max(5 * 60000, this.view.span * (e.deltaY > 0 ? 1.25 : 0.8)));
      this.view = { start: at - fx * span, span };
      this.lastUser = Date.now();
      this.draw();
    }, { passive: false });
    wrap.addEventListener("mousedown", (e) => {
      const r = wrap.getBoundingClientRect();
      const x = e.clientX - r.left;
      let handle = null;
      if (S.clip) {
        for (const k of ["start", "end"]) if (Math.abs(this.x(S.clip[k], r.width) - x) < 8) handle = k;
      }
      this.drag = { x0: e.clientX, start0: this.view.start, moved: false, handle, width: r.width };
    });
    window.addEventListener("mousemove", (e) => {
      const r = wrap.getBoundingClientRect();
      const inside = e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
      const tip = $("#tl-tip");
      if (inside || this.drag) {
        const t = this.view.start + (e.clientX - r.left) / r.width * this.view.span;
        tip.style.display = "block";
        tip.style.left = Math.max(40, Math.min(r.width - 40, e.clientX - r.left)) + "px";
        tip.textContent = `${fmtTime(t)} · ${new Date(t).getDate()} ${MONTHS[new Date(t).getMonth()]}`;
        // fare bir kamera şeridinin üzerindeyse kameranın adını da göster
        const ln = this.lanes, yy = e.clientY - r.top;
        if (ln && yy >= ln.top) {
          const idx = Math.floor((yy - ln.top) / (ln.laneH + ln.gap));
          const cam = S.channels.find((c) => c.id === ln.ids[idx]);
          if (cam && (yy - ln.top) % (ln.laneH + ln.gap) <= ln.laneH) tip.textContent += ` · ${cam.name}`;
        }
        if (S.clip) wrap.style.cursor = ["start", "end"].some((k) => Math.abs(this.x(S.clip[k], r.width) - (e.clientX - r.left)) < 8) ? "ew-resize" : "pointer";
      } else tip.style.display = "none";
      if (!this.drag) return;
      const dx = e.clientX - this.drag.x0;
      if (Math.abs(dx) > 3) this.drag.moved = true;
      if (this.drag.handle) {
        const t = Math.min(devNow(), this.view.start + (e.clientX - r.left) / r.width * this.view.span);
        S.clip[this.drag.handle] = t;
        if (S.clip.start > S.clip.end) [S.clip.start, S.clip.end] = [S.clip.end, S.clip.start], this.drag.handle = this.drag.handle === "start" ? "end" : "start";
        clipSync();
      } else if (this.drag.moved) {
        this.view.start = this.drag.start0 - dx / this.drag.width * this.view.span;
        this.lastUser = Date.now();
      }
      this.draw();
    });
    window.addEventListener("mouseup", (e) => {
      if (!this.drag) return;
      const d = this.drag;
      this.drag = null;
      if (!d.moved && !d.handle) {
        const r = wrap.getBoundingClientRect();
        let t = this.view.start + (e.clientX - r.left) / r.width * this.view.span;
        // kayıt olmayan bir boşluğa tıklanırsa sonraki kayda atla
        const has = S.channels.some((c) => recordsFor(c.id, t, t + 1).length);
        if (!has) { const n = firstRecordAfter(t); if (n != null && n - t < 6 * 3600000) t = n; }
        seek(t);
      }
    });
  },
  x(t, w) { return (t - this.view.start) / this.view.span * w; },
  follow(t, force) {
    const v = this.view;
    if (force || (Date.now() - this.lastUser > 8000 && (t < v.start + v.span * 0.05 || t > v.start + v.span * 0.92))) {
      v.start = t - v.span * (S.pos == null ? 0.85 : 0.5);
    }
    const d = dayStart(v.start);
    ensureRecords(v.start);
    if (dayStart(v.start + v.span) !== d) ensureRecords(v.start + v.span);
  },
  draw() {
    const cv = this.cv;
    if (!cv) return;
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth, h = cv.clientHeight;
    if (!w) return;
    if (cv.width !== Math.round(w * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
    const c = this.ctx;
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.clearRect(0, 0, w, h);
    const { start, span } = this.view;
    const end = start + span;
    const X = (t) => (t - start) / span * w;
    const css = getComputedStyle(document.documentElement);
    const col = (n) => css.getPropertyValue(n).trim();

    // gelecek (henüz olmayan) bölge
    const now = devNow();
    if (now < end) { c.fillStyle = "rgba(255,255,255,.03)"; c.fillRect(X(now), 0, w - X(now), h); }

    // kayıt şeritleri
    const chans = visibleChannels();
    // şeritler tam piksele oturtulur; küsuratlı yükseklikte aradaki boşluk kaybolup iki şerit tek görünüyordu
    const top = 24, gap = 3, n = Math.max(1, chans.length);
    const laneH = Math.max(3, Math.min(12, Math.floor((h - top - 4 - gap * (n - 1)) / n)));
    this.lanes = { top, gap, laneH, ids: chans.map((c) => c.id) };
    chans.forEach((ch, i) => {
      const y = top + i * (laneH + gap);
      c.fillStyle = "rgba(255,255,255,.04)";
      c.fillRect(0, y, w, laneH);
      for (const [a, b, k] of recordsFor(ch.id, start, end)) {
        c.fillStyle = k === "event" ? col("--event") : col("--rec");
        c.globalAlpha = .75;
        c.fillRect(X(a), y, Math.max(1, X(b) - X(a)), laneH);
      }
      c.globalAlpha = 1;
    });

    // ölçek çizgileri
    const steps = [60e3, 5 * 60e3, 10 * 60e3, 15 * 60e3, 30 * 60e3, 3600e3, 2 * 3600e3, 3 * 3600e3, 6 * 3600e3, 12 * 3600e3];
    const step = steps.find((s) => s / span * w >= 80) || DAY;
    const off = new Date(start).getTimezoneOffset() * 60000;
    c.font = "11px Segoe UI, sans-serif";
    c.textBaseline = "top";
    for (let t = Math.floor((start - off) / step) * step + off; t <= end; t += step) {
      const x = X(t);
      const midnight = new Date(t).getHours() === 0 && new Date(t).getMinutes() === 0;
      c.fillStyle = midnight ? "rgba(79,140,255,.6)" : "rgba(255,255,255,.12)";
      c.fillRect(Math.round(x), midnight ? 0 : 16, 1, midnight ? h : h - 16);
      c.fillStyle = midnight ? col("--accent") : col("--muted");
      const d = new Date(t);
      c.fillText(midnight ? `${d.getDate()} ${MONTHS[d.getMonth()]}` : `${pad(d.getHours())}:${pad(d.getMinutes())}`, x + 4, 4);
    }

    // kırpma seçimi
    if (S.clip) {
      const a = X(S.clip.start), b = X(S.clip.end);
      c.fillStyle = col("--sel");
      c.fillRect(a, 0, b - a, h);
      c.fillStyle = col("--accent");
      for (const x of [a, b]) { c.fillRect(x - 1.5, 0, 3, h); c.beginPath(); c.roundRect(x - 5, h / 2 - 12, 10, 24, 3); c.fill(); }
    }

    // şimdi işareti
    if (now >= start && now <= end) {
      c.fillStyle = col("--live");
      c.fillRect(X(now) - 1, 0, 2, h);
    }
    // oynatma kafası
    const p = S.pos == null ? now : currentPos();
    if (p != null && p >= start && p <= end && S.pos != null) {
      const x = X(p);
      c.fillStyle = "#fff";
      c.fillRect(x - 1, 0, 2, h);
      c.beginPath(); c.moveTo(x - 6, 0); c.lineTo(x + 6, 0); c.lineTo(x, 7); c.fill();
    }
  },
};

// ------------------------------------------------------------------ yakınlaştırma
function setupZoom(tile) {
  const v = $(".vwrap", tile);
  const ind = document.createElement("div");
  ind.className = "zoom-ind";
  tile.appendChild(ind);
  const apply = () => {
    const z = S.zoom, W = tile.clientWidth, H = tile.clientHeight;
    z.k = Math.min(8, Math.max(1, z.k));
    z.x = Math.min(0, Math.max(W - W * z.k, z.x));
    z.y = Math.min(0, Math.max(H - H * z.k, z.y));
    v.style.transform = `translate(${z.x}px, ${z.y}px) scale(${z.k})`;
    ind.textContent = `${z.k.toFixed(1)}×`;
    ind.hidden = z.k <= 1.01;
  };
  S.applyZoom = apply;
  S.zoomAt = (factor, cx = tile.clientWidth / 2, cy = tile.clientHeight / 2) => {
    const z = S.zoom, k = Math.min(8, Math.max(1, z.k * factor));
    z.x = cx - (cx - z.x) * (k / z.k);
    z.y = cy - (cy - z.y) * (k / z.k);
    z.k = k;
    apply();
  };
  tile.addEventListener("wheel", (e) => {
    e.preventDefault();
    const r = tile.getBoundingClientRect();
    S.zoomAt(e.deltaY < 0 ? 1.2 : 1 / 1.2, e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });
  let drag = null;
  tile.addEventListener("mousedown", (e) => { if (e.button === 0) { drag = { x: e.clientX, y: e.clientY, zx: S.zoom.x, zy: S.zoom.y }; tile.classList.add("dragging"); } });
  window.addEventListener("mousemove", (e) => { if (!drag) return; S.zoom.x = drag.zx + e.clientX - drag.x; S.zoom.y = drag.zy + e.clientY - drag.y; apply(); });
  window.addEventListener("mouseup", () => { drag = null; tile.classList.remove("dragging"); });
  tile.addEventListener("dblclick", () => { if (S.zoom.k > 1.01) S.zoom = { k: 1, x: 0, y: 0 }; else S.zoomAt(2.5); apply(); });
  new ResizeObserver(apply).observe(tile);
  apply();
}

// ------------------------------------------------------------------ kare alma
async function snapshot(ch) {
  const p = S.players.get(ch);
  if (S.single !== ch) {
    // çoklu görünümde ekrandaki küçük akış yerine cihazın ana akışından tam çözünürlüklü kare
    toast("Kare tam çözünürlükte alınıyor…", { time: 2500 });
    try {
      const r = await api("/api/grab", { method: "POST", body: { channel: ch, time: S.pos == null ? null : fmtISO(currentPos()) } });
      if (p) flash(p.tile);
      toast(`Kare kaydedildi: ${r.filename}`, { action: { label: "Göster", run: () => api("/api/open", { method: "POST", body: { path: r.file, select: true } }) } });
    } catch (e) { toast(e.message, { err: true }); }
    return;
  }
  if (!p || !p.video.videoWidth || p.video.readyState < 2) return toast("Görüntü henüz hazır değil, bir saniye sonra tekrar deneyin", { err: true });
  const v = p.video, vw = v.videoWidth, vh = v.videoHeight;
  // video kutusunun ekrandaki yeri (oran düzeltmesi sonrası)
  const bx = parseFloat(v.style.left) || 0, by = parseFloat(v.style.top) || 0;
  const bw = parseFloat(v.style.width) || v.clientWidth, bh = parseFloat(v.style.height) || v.clientHeight;
  const fx = bw / vw, fy = bh / vh;           // ekran pikseli / video pikseli
  let sx = 0, sy = 0, sw = vw, sh = vh;
  if (S.single === ch && S.zoom.k > 1.01) {
    // yakınlaştırılmış bölgeyi tam çözünürlükte al
    const W = p.tile.clientWidth, H = p.tile.clientHeight, z = S.zoom;
    const toV = (px, py) => [((px - z.x) / z.k - bx) / fx, ((py - z.y) / z.k - by) / fy];
    const [x0, y0] = toV(0, 0), [x1, y1] = toV(W, H);
    sx = Math.max(0, x0); sy = Math.max(0, y0);
    sw = Math.min(vw, x1) - sx; sh = Math.min(vh, y1) - sy;
  }
  const cv = document.createElement("canvas");
  // dikey çözünürlük korunur, genişlik gerçek orana göre açılır
  cv.height = Math.round(sh);
  cv.width = Math.round(sw * fx / fy);
  (() => { const g = cv.getContext("2d"); g.imageSmoothingEnabled = true; g.imageSmoothingQuality = "high"; return g; })().drawImage(v, sx, sy, sw, sh, 0, 0, cv.width, cv.height);
  const name = S.channels.find((c) => c.id === ch)?.name || `Kamera ${ch}`;
  try {
    const fmt = S.state?.settings?.snap_format || "jpg";
    const mime = { jpg: "image/jpeg", png: "image/png", webp: "image/webp" }[fmt] || "image/jpeg";
    const r = await api("/api/snapshot", { method: "POST", body: { image: cv.toDataURL(mime, mime === "image/png" ? undefined : 0.97), name, time: fmtISO(p.position()) } });
    flash(p.tile);
    toast(`Kare kaydedildi: ${r.filename}`, { action: { label: "Göster", run: () => api("/api/open", { method: "POST", body: { path: r.file, select: true } }) } });
  } catch (e) { toast(e.message, { err: true }); }
}

function flash(tile) {
  const f = document.createElement("div");
  f.style.cssText = "position:absolute;inset:0;background:#fff;opacity:.7;transition:opacity .35s;pointer-events:none";
  tile.appendChild(f);
  requestAnimationFrame(() => (f.style.opacity = 0));
  setTimeout(() => f.remove(), 400);
}

// ------------------------------------------------------------------ kırp ve indir
const toLocalInput = (t) => fmtISO(t);
function openClip() {
  const p = S.pos == null ? devNow() - 60000 : currentPos();
  const now = devNow() - 3000;
  let a = p - 30000, b = Math.min(now, p + 30000);
  if (b - a < 10000) a = b - 60000;
  S.clip = { start: a, end: b };
  const cams = $("#clip-cams");
  cams.innerHTML = "";
  for (const c of S.channels) {
    const l = document.createElement("label");
    l.className = "chip";
    l.innerHTML = `<input type="checkbox" value="${c.id}"><span></span>`;
    $("span", l).textContent = c.name;
    $("input", l).checked = S.single ? c.id === S.single : true;
    cams.appendChild(l);
  }
  const sel = $("#clip-format");
  sel.innerHTML = (S.state?.formats || []).map((f) => `<option value="${f.id}">${f.label}</option>`).join("");
  sel.value = S.state?.settings?.format || "mp4";
  $("#clip-panel").classList.add("open");
  $("#btn-clip").classList.add("primary");
  const span = Math.min(timeline.view.span, Math.max(10 * 60000, (b - a) * 6));
  timeline.view = { start: (a + b) / 2 - span / 2, span };
  timeline.lastUser = Date.now();
  clipSync();
  timeline.draw();
}
function closeClip() {
  S.clip = null;
  $("#clip-panel").classList.remove("open");
  $("#btn-clip").classList.remove("primary");
  timeline.draw();
}
function clipSync() {
  if (!S.clip) return;
  $("#clip-start").value = toLocalInput(S.clip.start);
  $("#clip-end").value = toLocalInput(S.clip.end);
  $("#clip-dur").textContent = fmtDur(S.clip.end - S.clip.start);
}
async function clipGo() {
  const channels = $$("#clip-cams input:checked").map((i) => +i.value);
  if (!channels.length) return toast("En az bir kamera seçin", { err: true });
  try {
    const jobs = await api("/api/downloads", { method: "POST", body: { channels, start: fmtISO(S.clip.start), end: fmtISO(S.clip.end), format: $("#clip-format").value } });
    toast(`${jobs.length} indirme başlatıldı`, { action: { label: "İndirilenler", run: () => go("downloads") } });
    closeClip();
    pollDownloads();
  } catch (e) { toast(e.message, { err: true }); }
}

// ------------------------------------------------------------------ indirilenler
let dlTimer = null;
async function pollDownloads() {
  clearTimeout(dlTimer);
  let list = [];
  try { list = await api("/api/downloads"); } catch (e) { }
  const active = list.filter((j) => ["waiting", "running", "converting"].includes(j.state)).length;
  const b = $("#dl-badge");
  b.hidden = !active; b.textContent = active;
  if (S.page === "downloads") libJobs(list);
  for (const j of list) {
    if (j.state === "done" && !S.notified?.[j.id] && S.seenRunning?.[j.id]) {
      toast(`İndirme tamamlandı: ${j.filename}`, { action: { label: "Göster", run: () => api("/api/open", { method: "POST", body: { path: j.file, select: true } }) } });
    }
    if (j.state === "error" && !S.notified?.[j.id] && S.seenRunning?.[j.id]) toast(`İndirme başarısız: ${j.message}`, { err: true });
    S.seenRunning = S.seenRunning || {}; S.notified = S.notified || {};
    if (["waiting", "running", "converting"].includes(j.state)) S.seenRunning[j.id] = true;
    else if (S.seenRunning[j.id]) S.notified[j.id] = true;
  }
  dlTimer = setTimeout(pollDownloads, active || S.page === "downloads" ? 1000 : 5000);
}

function renderDownloads(list) {
  $("#dl-folder").textContent = `Kayıt klasörü: ${S.state?.settings?.save_dir || ""}`;
  const box = $("#dl-list");
  if (!list.length) {
    box.innerHTML = `<div class="empty-state" style="padding:30px 0"><div><div class="ico">${icon("scissors")}</div><div class="big">Henüz indirme yok</div><div>Canlı veya Kayıtlar ekranında <b>Kırp ve İndir</b> ile istediğiniz bölümü seçin.</div></div></div>`;
    return;
  }
  box.innerHTML = "";
  for (const j of list) {
    const el = document.createElement("div");
    el.className = `dl-item ${j.state}`;
    const running = ["waiting", "running", "converting"].includes(j.state);
    el.innerHTML = `<div class="dl-ico">${j.filename.split(".").pop().toUpperCase()}</div>
      <div><div class="dl-name"></div><div class="dl-meta"></div>${running ? `<div class="bar"><i style="width:${Math.round(j.progress * 100)}%"></i></div>` : ""}</div>
      <div class="dl-actions"></div>`;
    $(".dl-name", el).textContent = j.filename;
    $(".dl-meta", el).textContent = `${j.name} · ${j.start.replace("T", " ")} → ${j.end.slice(11)} · ${j.format_label} · ${j.state === "done" ? fmtSize(j.size) + " · " : ""}${j.message}`;
    const acts = $(".dl-actions", el);
    const btn = (label, ic, fn, cls = "") => { const b = document.createElement("button"); b.className = `btn small ${cls}`; b.innerHTML = `${ic ? icon(ic) : ""}${label}`; b.onclick = fn; acts.appendChild(b); };
    if (j.state === "done") {
      btn("Aç", "play", () => api("/api/open", { method: "POST", body: { path: j.file, play: true } }));
      btn("Klasörde göster", "folder", () => api("/api/open", { method: "POST", body: { path: j.file, select: true } }));
    }
    if (running) btn("İptal", "x", () => api(`/api/downloads/${j.id}`, { method: "DELETE" }).then(pollDownloads), "danger");
    else btn("", "x", () => api(`/api/downloads/${j.id}`, { method: "DELETE" }).then(pollDownloads), "icon ghost");
    box.appendChild(el);
  }
}

// ------------------------------------------------------------------ ayarlar
function fillSettings() {
  const st = S.state, s = st.settings;
  if (document.activeElement?.closest("#page-settings")) return;
  $("#f-serial").value = s.serial || "";
  $("#f-user").value = s.username || "";
  $("#f-pass").placeholder = s.has_password ? "•••••••• (kayıtlı)" : "Cihaz şifresi";
  $("#f-mode").value = s.mode || "auto";
  $("#f-ip").value = s.ip || "";
  $("#f-dir").value = s.save_dir || "";
  $("#f-showall").checked = !!s.show_all;
  const fs = $("#f-format");
  if (!fs.options.length) fs.innerHTML = st.formats.map((f) => `<option value="${f.id}">${f.label}</option>`).join("");
  fs.value = s.format || "mp4";
  $("#f-snapfmt").value = s.snap_format || "jpg";
  renderDevice();
}

function renderDevice() {
  const d = S.state.device;
  $("#device-card").hidden = !d;
  $("#welcome-card").hidden = !(S.state.status === "setup");
  if (!d) return;
  const mode = { lan: "Yerel ağ", p2p: "İnternet (P2P)", relay: "İnternet (bulut aktarımı)" }[d.mode] || "—";
  $("#dev-info").innerHTML = `<div><span>Model</span><b></b></div><div><span>Bağlantı</span><b>${mode}</b></div><div><span>Kamera</span><b>${S.channels.length} / ${d.channels.length} kanal</b></div>`;
  $("#dev-info b").textContent = d.model || "Dahua";
  const list = $("#cam-list");
  if (list.contains(document.activeElement)) return;
  list.innerHTML = "";
  for (const c of S.state.all_channels) {
    const row = document.createElement("div");
    row.className = "cam-row";
    row.innerHTML = `<div class="num">${c.id}</div><input type="text" maxlength="30"><div class="st ${c.active ? "on" : ""}">${c.active ? "● Kamera bağlı" : "Boş kanal"}</div>`;
    const inp = $("input", row);
    inp.value = c.name;
    const save = async () => {
      const v = inp.value.trim();
      if (!v || v === c.name) return;
      try { S.state = await api("/api/rename", { method: "POST", body: { channel: c.id, name: v } }); toast("Kamera adı kaydedildi"); applyState(); }
      catch (e) { toast(e.message, { err: true }); inp.value = c.name; }
    };
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") inp.blur(); });
    inp.addEventListener("change", save);
    list.appendChild(row);
  }
}

async function saveSettings(connect) {
  const body = {
    serial: $("#f-serial").value.trim(), username: $("#f-user").value.trim(), mode: $("#f-mode").value,
    ip: $("#f-ip").value.trim(), save_dir: $("#f-dir").value, format: $("#f-format").value, show_all: $("#f-showall").checked,
    snap_format: $("#f-snapfmt").value,
  };
  if ($("#f-pass").value) body.password = $("#f-pass").value;
  if (connect && !body.serial && !body.ip) return toast("SN numarasını girin", { err: true });
  try {
    S.state = await api("/api/settings", { method: "POST", body });
    $("#f-pass").value = "";
    toast(connect ? "Kaydedildi, bağlanılıyor…" : "Ayarlar kaydedildi");
    applyState();
    if (connect) setTimeout(() => S.state?.status !== "error" && go("live"), 600);
  } catch (e) { toast(e.message, { err: true }); }
}

// ------------------------------------------------------------------ hakkında / güncelleme
function updRender(u) {
  S.upd = u;
  $("#ab-ver").textContent = u.version;
  const rel = u.release;
  const busy = u.state === "downloading" || u.state === "installing";
  let txt = "Denetlenmedi";
  if (u.state === "checking") txt = "Denetleniyor…";
  else if (u.state === "error") txt = esc(u.message);
  else if (busy) txt = esc(u.message) + (u.state === "downloading" ? ` ${Math.round(u.progress * 100)}%` : "");
  else if (rel?.newer) txt = `Yeni sürüm var: <b>${esc(rel.tag)}</b>` + (rel.asset ? ` (${(rel.asset.size / 1048576).toFixed(0)} MB)` : "");
  else if (rel) txt = `Program güncel (en son sürüm ${esc(rel.tag)})`;
  $("#upd-text").innerHTML = txt;
  $("#upd-check").disabled = busy || u.state === "checking";
  $("#upd-install").hidden = !(rel?.newer && rel.asset) || busy;
  $("#upd-install").disabled = !u.frozen;
  $("#upd-install").title = u.frozen ? "" : "Program kaynak koddan çalışıyor; güncellemek için git pull kullanın";
  $("#upd-bar").hidden = !busy;
  $("#upd-fill").style.width = `${Math.round((u.state === "installing" ? 1 : u.progress) * 100)}%`;
  $("#upd-notes").hidden = !(rel?.newer && rel.notes);
  $("#upd-notes").textContent = rel?.notes || "";
  if (busy || u.state === "checking") setTimeout(updPoll, 700);
}

async function updPoll() {
  try { updRender(await api("/api/update")); }
  catch { if (S.upd?.state !== "checking") { $("#upd-text").textContent = "Program yeniden başlatılıyor…"; setTimeout(updWaitRestart, 1500); } }
}

async function updWaitRestart() {
  try { await api("/api/state"); location.reload(); } catch { setTimeout(updWaitRestart, 1500); }
}

async function aboutOpen() {
  const u = await api("/api/update");
  updRender(u);
  if (!u.release && u.state === "idle") updRender(await api("/api/update/check", { method: "POST" }));
}

function aboutBind() {
  const open = (url) => api("/api/open-url", { method: "POST", body: { url } });
  $$("#tab-about [data-url]").forEach((b) => b.addEventListener("click", () => open(b.dataset.url)));
  $("#ab-repo").addEventListener("click", () => open(S.upd?.repo_url || "https://github.com/aytackayin"));
  $("#upd-check").addEventListener("click", async () => { $("#upd-text").textContent = "Denetleniyor…"; updRender(await api("/api/update/check", { method: "POST" })); });
  $("#upd-install").addEventListener("click", async () => {
    if (!confirm(`${S.upd.release.tag} sürümü indirilip kurulacak. Program kısa bir süre kapanıp yeniden açılacak. Devam edilsin mi?`)) return;
    try { updRender(await api("/api/update/install", { method: "POST" })); setTimeout(updPoll, 500); }
    catch (e) { toast(e.message, { err: true }); }
  });
}

// ------------------------------------------------------------------ genel durum
function applyState() {
  const st = S.state;
  if (!st) return;
  if (st.device?.now) S.devOffset = parseT(st.device.now) - Date.now();
  if (st.update && S.updToast !== st.update) {      // yeni sürüm bildirimi (oturum başına bir kez)
    S.updToast = st.update;
    toast(`Yeni sürüm hazır: ${st.update}`, { action: { label: "Göster", run: () => { go("settings"); showSetTab("about"); } }, time: 12000 });
  }
  const dot = $("#status-dot");
  dot.className = "dot " + ({ connected: "ok", connecting: "wait", error: "err" }[st.status] || "");
  const model = st.device?.model ? ` · ${st.device.model}` : "";
  $("#status-text").textContent = (st.message || "") + (st.status === "connected" ? model : "");

  // yetkiler: sekmeler ve kanallar oturum açan kullanıcının cihazdaki yetkisine göre
  const perms = st.device?.perms;
  let chans = st.channels;
  if (perms) {
    const allow = S.page === "archive" ? perms.replay : perms.monitor;
    if (allow && allow.length) chans = chans.filter((c) => allow.includes(c.id));
    $('#set-tabs [data-tab="cams"]').hidden = !perms.config;
    $('#set-tabs [data-tab="users"]').hidden = !perms.users;
    // indirme kayıt oynatmayla yapılır: kayıt izleme yetkisi olan kırpıp indirebilir
    $("#btn-clip").hidden = !(perms.replay && perms.replay.length);
    if ((S.setTab === "cams" && !perms.config) || (S.setTab === "users" && !perms.users)) showSetTab("general");
  }
  S.availChannels = chans;                      // seçiciye gelecek tüm kameralar
  const hidden = (st.settings.hidden_cams || {})[S.page === "archive" ? "archive" : "live"] || [];
  const shown = chans.filter((c) => !hidden.includes(c.id));
  chans = shown.length ? shown : chans;         // hepsi gizlenmişse boş ekran yerine hepsini göster
  camsLabel();
  const ids = chans.map((c) => [c.id, c.name, c.prefs?.mp, c.prefs?.aspect, c.aspect].join(":")).join(",") + "|" + S.page;
  const changed = ids !== S.chanKey;
  S.channels = chans;
  S.chanKey = ids;
  if (S.page === "settings" || !S.didInitSettings) { fillSettings(); S.didInitSettings = true; }
  $("#mypass-card").hidden = !st.device?.connected;
  if (st.device?.user) $("#mp-user").textContent = st.device.user;

  const ready = st.status === "connected" && S.channels.length;
  $("#stage").hidden = !ready;
  $("#controls").hidden = !ready;
  $("#watch-empty").hidden = !!ready;
  if (!ready) {
    for (const p of S.players.values()) p.close();
    S.players.clear();
    S.chanKey = null;
    const t = { connecting: "Cihaza bağlanılıyor…", error: "Bağlantı kurulamadı", setup: "Hoş geldiniz", connected: "Kamera bulunamadı", off: "Bağlantı kapalı" }[st.status] || "Hazırlanıyor…";
    $("#empty-title").textContent = t;
    $("#empty-text").textContent = st.status === "off" ? "Bağlantıyı siz kapattınız. Görüntüleri izlemek için yeniden bağlanın." : st.message || "";
    $("#empty-actions").innerHTML = st.status === "error" ? `<button class="btn primary" onclick="api('/api/reconnect',{method:'POST'})">${icon("refresh")}Tekrar dene</button> <button class="btn" onclick="go('settings')">Ayarlar</button>`
      : st.status === "off" ? `<button class="btn primary" onclick="api('/api/reconnect',{method:'POST'})">${icon("live")}Bağlan</button>`
      : st.status === "setup" ? `<button class="btn primary" onclick="go('settings')">${icon("settings")}Cihaz bilgilerini gir</button>` : "";
    if (st.status === "setup" && !S.sentToSetup) { S.sentToSetup = true; go("settings"); }
    return;
  }
  if (document.hidden) return;                  // tray'deyken görüntü açılmaz
  if (changed && isWatch()) {
    if (S.single && !S.channels.some((c) => c.id === S.single)) S.single = null;
    buildGrid();
    ensureRecords(S.pos ?? devNow(), true);
  }
}

async function pollState() {
  try { S.state = await api("/api/state"); applyState(); }
  catch (e) {
    $("#status-dot").className = "dot err";
    $("#status-text").textContent = "Program arka planı yanıt vermiyor";
  }
  setTimeout(pollState, S.state?.status === "connecting" ? 800 : 2500);
}

// ------------------------------------------------------------------ sayfalar
const TITLES = { live: "Canlı İzleme", archive: "Kayıtlar", downloads: "İndirilenler", settings: "Ayarlar", disk: "Disk" };
function go(page) {
  const wasWatch = isWatch();
  const prev = S.page;
  S.page = page;
  $$(".nav button").forEach((b) => b.classList.toggle("active", b.dataset.page === page));
  $("#page-title").textContent = TITLES[page];
  $$(".page").forEach((p) => p.classList.remove("active"));
  $(page === "live" || page === "archive" ? "#page-watch" : `#page-${page}`).classList.add("active");
  if (page === "settings") { fillSettings(); showSetTab(S.setTab || (S.state?.status === "setup" ? "general" : S.setTab || "general")); }
  else if (prev === "settings") camClosePreview();
  if (page === "downloads") { pollDownloads(); libLoad(); }
  if (page === "disk") diskLoad();
  if (isWatch()) {
    if (page === "live" && prev !== "live") { S.pos = null; S.speed = 1; S.playing = true; }
    if (page === "archive" && prev !== "archive") {
      const now = devNow();
      S.pos = Math.max(dayStart(now), now - 3600000);
      S.playing = true;
      $("#date").value = dayKey(now);
      $("#date").max = dayKey(now);
    }
    closeClip();
    S.chanKey = null;
    applyState();
    timeline.follow(S.pos ?? devNow(), true);
    updateControls();
  } else if (wasWatch) {
    for (const p of S.players.values()) p.close();   // izlemiyorken bant genişliği harcama
  }
}

// ------------------------------------------------------------------ olaylar
function bind() {
  $$(".nav button").forEach((b) => b.addEventListener("click", () => go(b.dataset.page)));
  $("#btn-back").onclick = closeSingle;
  $("#btn-play").onclick = togglePlay;
  $("#btn-rew").onclick = () => seek((S.pos == null ? devNow() : currentPos()) - 30000);
  $("#btn-fwd").onclick = () => { if (S.pos != null) seek(currentPos() + 30000); };
  $("#btn-live").onclick = goLive;
  $$("#speed button").forEach((b) => (b.onclick = () => setSpeed(+b.dataset.speed)));
  $("#btn-zin").onclick = () => S.zoomAt?.(1.4);
  $("#btn-zout").onclick = () => S.zoomAt?.(1 / 1.4);
  $("#btn-snap").onclick = () => S.single && snapshot(S.single);
  $("#btn-full").onclick = () => document.fullscreenElement ? document.exitFullscreen() : $("#watch").requestFullscreen();
  $("#btn-clip").onclick = () => (S.clip ? closeClip() : openClip());
  $("#clip-cancel").onclick = closeClip;
  $("#clip-go").onclick = clipGo;
  for (const k of ["start", "end"]) $(`#clip-${k}`).addEventListener("change", (e) => {
    const t = e.target.value ? parseT(e.target.value + (e.target.value.length === 16 ? ":00" : "")) : null;
    if (t && S.clip) { S.clip[k] = Math.min(t, devNow()); if (S.clip.start > S.clip.end) [S.clip.start, S.clip.end] = [S.clip.end, S.clip.start]; clipSync(); timeline.draw(); }
  });
  const goTo = () => {
    const d = S.page === "archive" ? $("#date").value : dayKey(devNow());
    const tm = $("#time").value || "00:00:00";
    if (!d) return;
    seek(parseT(`${d}T${tm.length === 5 ? tm + ":00" : tm}`));
  };
  $("#btn-go").onclick = goTo;
  $("#time").addEventListener("keydown", (e) => e.key === "Enter" && goTo());
  $("#date").addEventListener("change", async () => {
    const d = $("#date").value;
    if (!d) return;
    const t0 = parseT(d + "T00:00:00");
    await ensureRecords(t0 + 3600000, true);
    const first = firstRecordAfter(t0);
    seek(first != null && first < t0 + DAY ? first : t0);
    if (first == null || first >= t0 + DAY) toast("Bu tarihte kayıt bulunamadı");
    timeline.view.span = Math.max(timeline.view.span, 6 * 3600000);
    timeline.follow(S.pos, true);
  });

  $("#f-pass-show").onclick = () => { const i = $("#f-pass"); i.type = i.type === "password" ? "text" : "password"; };
  $("#btn-save").onclick = () => saveSettings(true);
  $("#btn-save2").onclick = () => saveSettings(false);
  $("#f-showall").onchange = () => saveSettings(false);
  $("#btn-dir").onclick = async () => { const r = await api("/api/choose-folder", { method: "POST" }); if (r.path) { $("#f-dir").value = r.path; saveSettings(false); } };
  $("#btn-open-folder").onclick = () => api("/api/open", { method: "POST", body: {} });
  $("#btn-rescan").onclick = async () => { toast("Kameralar taranıyor…"); S.state = await api("/api/refresh-channels", { method: "POST" }); applyState(); renderDevice(); toast(`${S.channels.length} kamera bulundu`); };

  document.addEventListener("keydown", (e) => {
    if (!isWatch() || e.target.closest("input,select,textarea")) return;
    if (e.key === " ") { e.preventDefault(); togglePlay(); }
    else if (e.key === "Escape") { if (!$("#cam-sel").hidden) $("#cam-sel").hidden = true; else if (S.clip) closeClip(); else closeSingle(); }
    else if (e.key === "ArrowLeft") seek((S.pos == null ? devNow() : currentPos()) - 10000);
    else if (e.key === "ArrowRight" && S.pos != null) seek(currentPos() + 10000);
    else if (e.key.toLowerCase() === "f") $("#btn-full").click();
    else if (e.key.toLowerCase() === "s" && S.single) snapshot(S.single);
    else if ((e.key === "+" || e.key === "=") && S.single) S.zoomAt(1.4);
    else if (e.key === "-" && S.single) S.zoomAt(1 / 1.4);
  });

  aboutBind();

  // pencere küçültülüp tray'e gidince görüntü akışları durur (boşuna veri çekilmesin), geri gelince yeniden başlar
  document.addEventListener("visibilitychange", () => {
    if (!isWatch()) return;
    if (document.hidden) {
      if (S.page === "archive" && S.pos != null) S.pos = currentPos();
      for (const p of S.players.values()) p.close();
      S.players.clear();
      S.chanKey = null;
    } else applyState();
  });

  // pencere açık kaldığı sürece arka plan çalışır
  const keep = () => { const ws = new WebSocket(`ws://${location.host}/ws/app`); ws.onclose = () => setTimeout(keep, 2000); };
  keep();
}

bind();
timeline.init();
timeline.view.start = devNow() - timeline.view.span * 0.85;
updateControls();
pollState();
pollDownloads();
setInterval(tickClock, 250);


// ------------------------------------------------------------------ gösterilecek kameralar
function camsLabel() {
  const all = S.availChannels || [], n = S.channels?.length || 0;
  const el = $("#cams-label");
  if (el) el.textContent = all.length && n && n < all.length ? `Kameralar ${n}/${all.length}` : "Kameralar";
}

function camSelOpen() {
  const box = $("#cam-sel");
  if (!box.hidden) { box.hidden = true; return; }
  const page = S.page === "archive" ? "archive" : "live";
  const hidden = new Set(((S.state?.settings?.hidden_cams) || {})[page] || []);
  const all = S.availChannels || [];
  box.innerHTML = `<div class="hint">${page === "live" ? "Canlı" : "Kayıtlar"} ekranında gösterilecek kameralar. Gizlenen kameraların görüntüsü cihazdan hiç istenmez.</div>` +
    all.map((c) => `<label><input type="checkbox" value="${c.id}" ${hidden.has(c.id) ? "" : "checked"}><span></span></label>`).join("") +
    `<div class="cs-foot"><button class="btn small" data-all>Tümü</button><div style="flex:1"></div><button class="btn small ghost" data-close>Kapat</button></div>`;
  $$("label span", box).forEach((s, i) => (s.textContent = all[i].name));
  const save = async () => {
    const checked = $$("input", box).filter((i) => i.checked).map((i) => +i.value);
    if (!checked.length) { toast("En az bir kamera seçili olmalı", { err: true }); return false; }
    const hc = { ...((S.state?.settings?.hidden_cams) || {}) };
    hc[page] = all.map((c) => c.id).filter((id) => !checked.includes(id));
    try { S.state = await api("/api/settings", { method: "POST", body: { hidden_cams: hc } }); S.chanKey = null; applyState(); }
    catch (e) { toast(e.message, { err: true }); }
    return true;
  };
  $$("input", box).forEach((i) => (i.onchange = async () => { if (!(await save())) i.checked = true; }));
  $("[data-all]", box).onclick = () => { $$("input", box).forEach((i) => (i.checked = true)); save(); };
  $("[data-close]", box).onclick = () => (box.hidden = true);
  box.hidden = false;
}
document.addEventListener("mousedown", (e) => { const b = $("#cam-sel"); if (b && !b.hidden && !e.target.closest(".cam-sel-wrap")) b.hidden = true; });
$("#btn-cams").onclick = camSelOpen;
