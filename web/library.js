"use strict";
/* İndirilenler kitaplığı, dahili oynatıcı, notlar ve Disk sayfası */

const L = { items: [], jobs: [], root: "", seenDone: new Set() };
const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fileUrl = (kind, p) => `/api/files/${kind}?path=${encodeURIComponent(p)}`;
const aspectNum = (a) => { if (!a) return null; const [w, h] = String(a).split(":").map(Number); return w && h ? w / h : null; };
const recStart = (it) => it.start ? parseT(it.start) : null;
const fmtHMS = (s) => { s = Math.max(0, s); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = Math.floor(s % 60); return (h ? `${h}:${pad(m)}` : `${m}`) + `:${pad(x)}`; };

// ------------------------------------------------------------------ liste
async function libLoad() {
  try {
    const r = await api("/api/files");
    L.items = r.items; L.jobs = r.jobs; L.root = r.root;
    libRender();
    libJobs(r.jobs);
  } catch (e) { $("#lib-grid").innerHTML = `<div class="lib-empty"></div>`; $("#lib-grid .lib-empty").textContent = "Dosyalar okunamadı: " + e.message; }
}

function libFiltered() {
  const q = $("#lib-q").value.trim().toLocaleLowerCase("tr");
  const from = $("#lib-from").value, to = $("#lib-to").value, type = $("#lib-type").value, cam = $("#lib-cam").value;
  let list = L.items.filter((it) => {
    if (cam && !(it.camera || "").split(", ").includes(cam)) return false;
    if (type === "video" || type === "image" || type === "log") { if (it.kind !== type) return false; }
    else if (type) { if (!(it.ext === type || (type === "jpg" && it.ext === "jpeg"))) return false; }
    const d = (it.start || it.mtime).slice(0, 10);
    if (from && d < from) return false;
    if (to && d > to) return false;
    if (q) {
      const hay = [it.name, it.camera, it.note].join(" ").toLocaleLowerCase("tr");
      if (!q.split(/\s+/).every((w) => hay.includes(w))) return false;
    }
    return true;
  });
  const key = { "rec-desc": (a) => a.start || a.mtime, "rec-asc": (a) => a.start || a.mtime, "mtime-desc": (a) => a.mtime, name: (a) => a.name.toLocaleLowerCase("tr"), "size-desc": (a) => a.size }[$("#lib-sort").value];
  const sort = $("#lib-sort").value;
  list.sort((a, b) => { const x = key(a), y = key(b); return (x < y ? -1 : x > y ? 1 : 0) * (sort === "rec-asc" || sort === "name" ? 1 : -1); });
  return list;
}

function hl(text, q) {
  let out = esc(text);
  for (const w of q.split(/\s+/).filter(Boolean)) {
    const re = new RegExp(w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "giu");
    out = out.replace(re, (m) => `<mark>${m}</mark>`);
  }
  return out;
}

function libCams() {
  const sel = $("#lib-cam"), cur = sel.value;
  const cams = [...new Set(L.items.flatMap((i) => (i.camera || "").split(", ").filter(Boolean)))].sort((a, b) => a.localeCompare(b, "tr"));
  sel.innerHTML = `<option value="">Tüm kameralar</option>` + cams.map((c) => `<option>${esc(c)}</option>`).join("");
  sel.value = cams.includes(cur) ? cur : "";
}

function libRender() {
  libCams();
  const list = libFiltered();
  const q = $("#lib-q").value.trim();
  const grid = $("#lib-grid");
  const total = L.items.reduce((a, b) => a + b.size, 0);
  $("#lib-info").textContent = `${L.root} · ${L.items.length} dosya (${fmtSize(total)})` + (list.length !== L.items.length ? ` · ${list.length} tanesi gösteriliyor` : "");
  if (!list.length) {
    grid.innerHTML = `<div class="lib-empty">${L.items.length ? "Aramaya uyan dosya yok." : "Henüz dosya yok. Canlı veya Kayıtlar ekranında <b>Kırp ve İndir</b> ile kayıt indirebilir, <b>Kare al</b> ile fotoğraf kaydedebilirsiniz."}</div>`;
    return;
  }
  grid.innerHTML = "";
  for (const it of list) {
    const card = document.createElement("div");
    card.className = "lib-card";
    const dur = it.start && it.end ? (parseT(it.end) - parseT(it.start)) / 1000 : null;
    const when = it.start ? `${fmtDate(parseT(it.start))} ${it.start.slice(11, 19)}${it.end ? "–" + it.end.slice(11, 19) : ""}` : `İndirme: ${it.mtime.replace("T", " ").slice(0, 16)}`;
    card.innerHTML = `
      <div class="lib-thumb"><img loading="lazy" alt="" style="width:100%;height:100%;object-fit:cover;display:block">
        <span class="badge2">${esc(it.ext.toUpperCase())}${it.source === "kesit" ? " · KESİT" : ""}</span>
        ${dur ? `<span class="dur">${fmtHMS(dur)}</span>` : ""}
        <div class="play"><span>${icon(it.kind === "video" ? "play" : "expand")}</span></div></div>
      <div class="lib-meta"><div class="lib-title">${hl(it.camera || it.name, q)}</div>
        <div class="lib-sub">${esc(when)}</div><div class="lib-sub">${fmtSize(it.size)}${it.folder ? " · " + esc(it.folder) : ""}</div>
        ${it.note ? `<div class="lib-note">${hl(it.note, q)}</div>` : ""}</div>
      <div class="lib-actions">
        <button class="btn small" data-a="note" title="${it.note ? "Notu düzenle" : "Not ekle"}">${icon("note")}<span class="lbl">${it.note ? "Notu düzenle" : "Not ekle"}</span></button>
        <div class="grow"></div>
        <button class="btn small icon ghost" data-a="show" title="Klasörde göster">${icon("folder")}</button>
        <button class="btn small icon ghost danger" data-a="del" title="Sil (Geri Dönüşüm Kutusu)">${icon("x")}</button>
      </div>`;
    if (it.kind === "log") {
      $(".lib-thumb", card).innerHTML = `<div style="position:absolute;inset:0;display:grid;place-items:center;background:var(--panel-2);color:var(--warn)"><div style="text-align:center">${icon("history").replace('class="i"', 'class="i" style="width:40px;height:40px"')}<div style="font-size:12px;margin-top:6px;color:var(--muted)">Olay logu</div></div></div><span class="badge2">${esc(it.ext.toUpperCase())}</span>`;
    } else $("img", card).src = fileUrl("thumb", it.path);
    if ($("img", card)) $("img", card).onerror = (e) => (e.target.style.display = "none");
    card.title = it.name;
    $(".lib-thumb", card).onclick = () => openPlayer(it);
    card.addEventListener("click", (e) => {
      const a = e.target.closest("[data-a]")?.dataset.a;
      if (a === "open") openPlayer(it);
      else if (a === "note") noteEdit(it);
      else if (a === "show") api("/api/open", { method: "POST", body: { path: L.root + "\\" + it.path.replace(/\//g, "\\"), select: true } });
      else if (a === "del") libDelete(it);
    });
    grid.appendChild(card);
  }
}

function libJobs(list) {
  L.jobs = list;
  const box = $("#dl-active");
  if (!box) return;
  const active = list.filter((j) => ["waiting", "running", "converting", "paused", "error"].includes(j.state));
  if (!active.length) { box.innerHTML = ""; }
  else {
    if (!$("#dl-list", box)) box.innerHTML = `<div class="card" style="padding:10px 16px"><h2 style="font-size:13.5px;margin:4px 0">Devam eden işler</h2><div id="dl-list"></div></div>`;
    const lst = $("#dl-list");
    // satırlar yerinde güncellenir (her saniye yeniden çizilirse tıklanan düğme elden kayıyordu);
    // düğmeler yalnızca işin durumu değişince yenilenir
    const ids = new Set(active.map((j) => String(j.id)));
    for (const el of [...lst.children]) if (!ids.has(el.dataset.id)) el.remove();
    active.forEach((j, i) => {
      let el = lst.querySelector(`[data-id="${j.id}"]`);
      if (!el || el.dataset.state !== j.state) {
        const fresh = jobRow(j);
        if (el) el.replaceWith(fresh); else lst.appendChild(fresh);
        el = fresh;
      }
      $(".dl-meta", el).textContent = `${j.name} · ${j.start.slice(0, 10).split("-").reverse().join(".")} ${j.start.slice(11, 16)} → ${j.end.slice(11, 16)} · ${j.format_label} · ${j.message}`;
      const bar = $(".bar i", el);
      if (bar) bar.style.width = `${Math.round(j.progress * 100)}%`;
      if (lst.children[i] !== el) lst.insertBefore(el, lst.children[i] || null);
    });
  }
  // biten iş varsa listeyi tazele
  let fresh = false;
  for (const j of list) if (j.state === "done" && !L.seenDone.has(j.id)) { L.seenDone.add(j.id); fresh = true; }
  if (fresh && S.page === "downloads") libLoad();
}

function jobRow(j) {
  const el = document.createElement("div");
  el.className = `dl-item ${j.state}`;
  el.dataset.id = j.id;
  el.dataset.state = j.state;
  const running = ["waiting", "running", "converting", "paused"].includes(j.state);
  const canPause = j.state === "waiting" || j.state === "running";
  el.innerHTML = `<div class="dl-ico">${esc(j.filename.split(".").pop().toUpperCase())}</div>
    <div><div class="dl-name"></div><div class="dl-meta"></div>${running ? `<div class="bar"><i></i></div>` : ""}</div>
    <div class="dl-actions">${canPause ? `<button class="btn small" data-a="pause">${icon("pause")}Duraklat</button>` : ""}${j.state === "paused" ? `<button class="btn small primary" data-a="resume">${icon("play")}Devam et</button>` : ""}<button class="btn small ${running ? "danger" : "icon ghost"}" data-a="del">${icon("x")}${running ? "İptal" : ""}</button></div>`;
  $(".dl-name", el).textContent = j.filename;
  $(".dl-actions", el).onclick = (e) => {
    const a = e.target.closest("[data-a]")?.dataset.a;
    if (a === "del") {
      if (j.state === "paused" && !confirm("Duraklatılmış indirme iptal edilsin mi? İnen kısım silinir.")) return;
      api(`/api/downloads/${j.id}`, { method: "DELETE" }).then(pollDownloads);
    } else if (a) api(`/api/downloads/${j.id}/${a}`, { method: "POST" }).then(pollDownloads);
  };
  return el;
}

async function libDelete(it) {
  if (!confirm(`"${it.name}" silinsin mi?\n\nDosya Geri Dönüşüm Kutusu'na taşınır; gerekirse oradan geri alabilirsiniz.`)) return;
  try {
    await api(`/api/files?path=${encodeURIComponent(it.path)}`, { method: "DELETE" });
    L.items = L.items.filter((x) => x.path !== it.path);
    libRender();
    toast("Geri Dönüşüm Kutusu'na taşındı");
  } catch (e) { toast(e.message, { err: true }); }
}

// ------------------------------------------------------------------ not editörü
function noteEdit(it) {
  if (document.fullscreenElement) document.exitFullscreen();   // tam ekranda pencere görünmez
  const box = modal(`
    <div class="modal-head"><h2></h2><button class="btn icon ghost" data-x>${icon("x")}</button></div>
    <div class="modal-body">
      <div class="note-tools">
        <button class="btn small" data-ins="time">${icon("history")}Tarih/saat ekle</button>
        <button class="btn small" data-ins="bullet">• Madde</button>
        <button class="btn small" data-ins="plate">Plaka:</button>
        <div style="flex:1"></div><span class="sub" id="nt-count" style="margin:0"></span>
      </div>
      <textarea class="note-editor" id="nt-text" placeholder="Bu kayıtla ilgili notunuz… (ör. 23:14'te beyaz araç, plaka 35 ABC 123)"></textarea>
      ${it.note_updated ? `<p class="sub" style="margin:8px 0 0">Son düzenleme: ${esc(it.note_updated)}</p>` : ""}
    </div>
    <div class="modal-foot">${it.note ? `<button class="btn ghost danger" id="nt-del">Notu sil</button><div style="flex:1"></div>` : ""}
      <button class="btn ghost" data-x>Vazgeç</button><button class="btn primary" id="nt-save">${icon("check")}Kaydet</button></div>`);
  $(".modal-head h2", box).textContent = `Not – ${it.camera || it.name}`;
  const ta = $("#nt-text");
  ta.value = it.note || "";
  const cnt = () => ($("#nt-count").textContent = `${ta.value.length} karakter`);
  ta.oninput = cnt; cnt();
  setTimeout(() => ta.focus(), 50);
  const insert = (t) => { const s = ta.selectionStart, e = ta.selectionEnd; ta.value = ta.value.slice(0, s) + t + ta.value.slice(e); ta.selectionStart = ta.selectionEnd = s + t.length; ta.focus(); cnt(); };
  $$("[data-ins]", box).forEach((b) => (b.onclick = () => {
    const k = b.dataset.ins, nl = ta.value && !ta.value.endsWith("\n") && ta.selectionStart === ta.value.length ? "\n" : "";
    if (k === "time") { const d = new Date(); insert(`${nl}[${d.getDate()} ${MONTHS[d.getMonth()]} ${pad(d.getHours())}:${pad(d.getMinutes())}] `); }
    if (k === "bullet") insert(`${nl}• `);
    if (k === "plate") insert(`${nl}Plaka: `);
  }));
  $$("[data-x]", box).forEach((b) => (b.onclick = modalClose));
  const save = async (text) => {
    try {
      await api("/api/files/note", { method: "POST", body: { path: it.path, text } });
      it.note = text.trim() || undefined;
      it.note_updated = new Date().toISOString().slice(0, 16).replace("T", " ");
      modalClose(); libRender(); toast(text.trim() ? "Not kaydedildi" : "Not silindi");
    } catch (e) { toast(e.message, { err: true }); }
  };
  $("#nt-save").onclick = () => save(ta.value);
  if ($("#nt-del")) $("#nt-del").onclick = () => save("");
  ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.ctrlKey) save(ta.value); });
}

// ------------------------------------------------------------------ oynatıcı
function openPlayer(it) {
  if (it.kind === "log") return logView(it);
  const isVideo = it.kind === "video";
  const el = document.createElement("div");
  el.className = "player";
  el.innerHTML = `
    <div class="player-head">
      <button class="btn" data-x>${icon("back")}Geri</button>
      <h2></h2>
      <select class="date-input" id="pl-aspect" title="Görüntü oranı">
        <option value="auto">Oran: otomatik</option><option value="4:3">4:3</option><option value="16:9">16:9</option><option value="orig">Orijinal</option></select>
      <button class="btn icon" id="pl-zout" title="Uzaklaştır (−)">${icon("zout")}</button>
      <button class="btn icon" id="pl-zin" title="Yakınlaştır (+)">${icon("zin")}</button>
      <button class="btn" id="pl-snap" title="Kare al (S)">${icon("snap")}Kare al</button>
      <button class="btn" id="pl-note">${icon("note")}Not</button>
    </div>
    <div class="player-stage" id="pl-stage"><div class="vwrap" id="pl-wrap"></div>
      <div class="rec-time" id="pl-rec" hidden></div><div class="zoom-ind" id="pl-zi" hidden></div>
      <div class="overlay" id="pl-ov" style="position:absolute;inset:0;display:grid;place-items:center;color:var(--muted)"><div class="msg" style="display:flex;flex-direction:column;align-items:center;gap:10px"><div class="spinner"></div><span>Hazırlanıyor…</span></div></div></div>
    ${isVideo ? `<div class="player-ctl">
      <div class="row">
        <button class="btn icon" id="pl-back10" title="10 sn geri (←)">${icon("rew")}</button>
        <button class="btn icon" id="pl-fb" title="Bir kare geri (,)">${icon("stepb")}</button>
        <button class="btn icon primary" id="pl-play" title="Oynat / Duraklat (Boşluk)">${icon("play")}</button>
        <button class="btn icon" id="pl-ff" title="Bir kare ileri (.)">${icon("step")}</button>
        <button class="btn icon" id="pl-fwd10" title="10 sn ileri (→)">${icon("fwd")}</button>
        <select class="date-input" id="pl-speed" style="height:36px"><option value="0.25">0.25×</option><option value="0.5">0.5×</option><option value="1" selected>1×</option><option value="2">2×</option><option value="4">4×</option></select>
        <div class="clock" id="pl-clock">0:00 / 0:00</div>
        <div class="grow"></div>
        <button class="btn" id="pl-trim">${icon("scissors")}Kırp</button>
        <button class="btn icon" id="pl-full" title="Tam ekran (F)">${icon("full")}</button>
      </div>
      <div class="ptl" id="pl-tl"><canvas></canvas></div>
      <div class="trim-box" id="pl-trimbox" hidden>
        <span>Kesit: <b id="pl-ta"></b> – <b id="pl-tb"></b> (<span id="pl-td"></span>)</span>
        <button class="btn small" id="pl-seta">Başlangıç = şu an</button>
        <button class="btn small" id="pl-setb">Bitiş = şu an</button>
        <label>Biçim <select id="pl-fmt"></select></label>
        <div style="flex:1"></div>
        <button class="btn small ghost" id="pl-trimx">Vazgeç</button>
        <button class="btn small primary" id="pl-trimgo">${icon("download")}Kesiti kaydet</button>
      </div>
    </div>` : ""}`;
  document.body.appendChild(el);
  $("h2", el).textContent = `${it.camera || it.name} · ${it.start ? fmtDate(parseT(it.start)) + " " + it.start.slice(11, 19) : it.name}`;
  const stage = $("#pl-stage", el), wrap = $("#pl-wrap", el), ov = $("#pl-ov", el);
  const media = document.createElement(isVideo ? "video" : "img");
  media.style.objectFit = "fill";
  if (isVideo) { media.playsInline = true; media.preload = "auto"; }
  wrap.appendChild(media);
  const z = { k: 1, x: 0, y: 0 };
  const rec0 = recStart(it);
  let fps = 15;

  const natural = () => (isVideo ? (media.videoWidth ? media.videoWidth / media.videoHeight : null) : (media.naturalWidth ? media.naturalWidth / media.naturalHeight : null));
  const aspect = () => {
    const sel = $("#pl-aspect", el).value;
    if (sel === "orig") return natural() || 16 / 9;
    if (sel !== "auto") return aspectNum(sel);
    if (isVideo) return aspectNum(it.aspect) || natural() || 16 / 9;
    return natural() || 4 / 3;                     // fotoğraflar kaydedilirken zaten düzeltilmiş
  };
  const fit = () => {
    const a = aspect(), W = stage.clientWidth, H = stage.clientHeight;
    let w = W, h = W / a;
    if (h > H) { h = H; w = H * a; }
    Object.assign(media.style, { width: w + "px", height: h + "px", left: (W - w) / 2 + "px", top: (H - h) / 2 + "px" });
    applyZ();
  };
  const applyZ = () => {
    const W = stage.clientWidth, H = stage.clientHeight;
    z.k = Math.min(10, Math.max(1, z.k));
    z.x = Math.min(0, Math.max(W - W * z.k, z.x));
    z.y = Math.min(0, Math.max(H - H * z.k, z.y));
    wrap.style.transform = `translate(${z.x}px, ${z.y}px) scale(${z.k})`;
    $("#pl-zi", el).hidden = z.k <= 1.01;
    $("#pl-zi", el).textContent = `${z.k.toFixed(1)}×`;
  };
  const zoomAt = (f, cx = stage.clientWidth / 2, cy = stage.clientHeight / 2) => {
    const k = Math.min(10, Math.max(1, z.k * f));
    z.x = cx - (cx - z.x) * (k / z.k); z.y = cy - (cy - z.y) * (k / z.k); z.k = k; applyZ();
  };
  stage.addEventListener("wheel", (e) => { e.preventDefault(); const r = stage.getBoundingClientRect(); zoomAt(e.deltaY < 0 ? 1.2 : 1 / 1.2, e.clientX - r.left, e.clientY - r.top); }, { passive: false });
  let drag = null;
  stage.addEventListener("mousedown", (e) => { if (e.button === 0) { drag = { x: e.clientX, y: e.clientY, zx: z.x, zy: z.y, moved: false }; stage.classList.add("dragging"); } });
  const mm = (e) => { if (!drag) return; z.x = drag.zx + e.clientX - drag.x; z.y = drag.zy + e.clientY - drag.y; if (Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) > 4) drag.moved = true; applyZ(); };
  const mu = () => { if (drag && !drag.moved && isVideo) togglePlay(); drag = null; stage.classList.remove("dragging"); };
  window.addEventListener("mousemove", mm); window.addEventListener("mouseup", mu);
  stage.addEventListener("dblclick", () => { if (z.k > 1.01) { z.k = 1; z.x = 0; z.y = 0; applyZ(); } else zoomAt(2.5); });
  const ro = new ResizeObserver(fit); ro.observe(stage);
  $("#pl-aspect", el).onchange = fit;
  $("#pl-zin", el).onclick = () => zoomAt(1.4);
  $("#pl-zout", el).onclick = () => zoomAt(1 / 1.4);
  $("#pl-note", el).onclick = () => noteEdit(it);

  // ---- kare alma (yakınlaştırılmış bölge, gerçek çözünürlükte, doğru oranda)
  const snap = async () => {
    const vw = isVideo ? media.videoWidth : media.naturalWidth, vh = isVideo ? media.videoHeight : media.naturalHeight;
    if (!vw) return;
    const bx = parseFloat(media.style.left), by = parseFloat(media.style.top), bw = parseFloat(media.style.width), bh = parseFloat(media.style.height);
    const fx = bw / vw, fy = bh / vh, W = stage.clientWidth, H = stage.clientHeight;
    const toV = (px, py) => [((px - z.x) / z.k - bx) / fx, ((py - z.y) / z.k - by) / fy];
    const [x0, y0] = toV(0, 0), [x1, y1] = toV(W, H);
    const sx = Math.max(0, x0), sy = Math.max(0, y0), sw = Math.min(vw, x1) - sx, sh = Math.min(vh, y1) - sy;
    const cv = document.createElement("canvas");
    cv.height = Math.round(sh); cv.width = Math.round(sw * fx / fy);
    (() => { const g = cv.getContext("2d"); g.imageSmoothingEnabled = true; g.imageSmoothingQuality = "high"; return g; })().drawImage(media, sx, sy, sw, sh, 0, 0, cv.width, cv.height);
    const fmt = S.state?.settings?.snap_format || "jpg";
    const mime = { jpg: "image/jpeg", png: "image/png", webp: "image/webp" }[fmt] || "image/jpeg";
    const when = rec0 && isVideo ? fmtISO(rec0 + media.currentTime * 1000) : fmtISO(Date.now());
    try {
      const r = await api("/api/snapshot", { method: "POST", body: { image: cv.toDataURL(mime, mime === "image/png" ? undefined : 0.97), name: it.camera || "Kare", time: when } });
      flash(stage);
      toast(`Kare kaydedildi: ${r.filename}`, { action: { label: "Göster", run: () => api("/api/open", { method: "POST", body: { path: r.file, select: true } }) } });
      L.stale = true;
    } catch (e) { toast(e.message, { err: true }); }
  };
  $("#pl-snap", el).onclick = snap;

  // ---- kapatma
  const close = () => {
    ro.disconnect();
    window.removeEventListener("mousemove", mm); window.removeEventListener("mouseup", mu);
    document.removeEventListener("keydown", keys, true);
    if (isVideo) { media.pause(); media.removeAttribute("src"); media.load(); }
    el.remove();
    if (document.fullscreenElement) document.exitFullscreen();
    if (L.stale) { L.stale = false; libLoad(); }
  };
  $("[data-x]", el).onclick = close;

  // ---- klavye
  const keys = (e) => {
    if (!$("#modal").hidden || e.target.closest("input,select,textarea")) return;
    const k = e.key;
    if (k === "Escape") { e.preventDefault(); close(); }
    else if (k.toLowerCase() === "s") snap();
    else if (k === "+" || k === "=") zoomAt(1.4);
    else if (k === "-") zoomAt(1 / 1.4);
    else if (!isVideo) return;
    else if (k === " ") { e.preventDefault(); togglePlay(); }
    else if (k === "ArrowLeft") media.currentTime = Math.max(0, media.currentTime - 5);
    else if (k === "ArrowRight") media.currentTime = Math.min(media.duration || 0, media.currentTime + 5);
    else if (k === ",") stepFrame(-1);
    else if (k === ".") stepFrame(1);
    else if (k.toLowerCase() === "f") $("#pl-full", el).click();
    else return;
    e.stopPropagation();
  };
  document.addEventListener("keydown", keys, true);

  if (!isVideo) {
    media.onload = () => { ov.remove(); fit(); };
    media.onerror = () => { ov.innerHTML = "Fotoğraf açılamadı"; };
    media.src = fileUrl("media", it.path);
    return;
  }

  // ---- video
  const togglePlay = () => (media.paused ? media.play() : media.pause());
  const stepFrame = (d) => { media.pause(); media.currentTime = Math.max(0, Math.min(media.duration || 0, media.currentTime + d / fps)); };
  api(fileUrl("info", it.path)).then((info) => {
    const v = (info.streams || []).find((s) => s.codec_type === "video");
    if (v?.avg_frame_rate) { const [a, b] = v.avg_frame_rate.split("/").map(Number); if (a && b && a / b < 120) fps = a / b; }
  }).catch(() => { });
  if (it.ext !== "mp4" && it.ext !== "webm") $("span", ov).textContent = "Oynatmaya hazırlanıyor (bir kez yapılır)…";
  media.src = fileUrl("media", it.path);
  media.onloadedmetadata = () => { ov.remove(); fit(); draw(); media.play().catch(() => { }); };
  media.onerror = () => { ov.innerHTML = `<span>Bu video bu bilgisayarda oynatılamadı. "Klasörde göster" ile başka bir oynatıcıda açabilirsiniz.</span>`; };
  media.onplay = media.onpause = () => ($("#pl-play", el).innerHTML = icon(media.paused ? "play" : "pause"));
  $("#pl-play", el).onclick = togglePlay;
  $("#pl-back10", el).onclick = () => (media.currentTime = Math.max(0, media.currentTime - 10));
  $("#pl-fwd10", el).onclick = () => (media.currentTime = Math.min(media.duration || 0, media.currentTime + 10));
  $("#pl-fb", el).onclick = () => stepFrame(-1);
  $("#pl-ff", el).onclick = () => stepFrame(1);
  $("#pl-speed", el).onchange = (e) => (media.playbackRate = +e.target.value);
  $("#pl-full", el).onclick = () => (document.fullscreenElement ? document.exitFullscreen() : el.requestFullscreen());

  // ---- zaman çizelgesi + kırpma
  const cv = $("#pl-tl canvas", el), tl = $("#pl-tl", el);
  const trim = { on: false, a: 0, b: 0 };
  const X = (t, w) => (t / (media.duration || 1)) * w;
  const T = (x, w) => Math.max(0, Math.min(media.duration || 0, (x / w) * (media.duration || 0)));
  function draw() {
    const dpr = window.devicePixelRatio || 1, w = cv.clientWidth, h = cv.clientHeight;
    if (!w) return;
    if (cv.width !== Math.round(w * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
    const c = cv.getContext("2d");
    c.setTransform(dpr, 0, 0, dpr, 0, 0); c.clearRect(0, 0, w, h);
    const dur = media.duration || 0;
    c.fillStyle = "rgba(255,255,255,.06)"; c.fillRect(0, h / 2 - 3, w, 6);
    for (let i = 0; i < media.buffered.length; i++) { c.fillStyle = "rgba(255,255,255,.12)"; c.fillRect(X(media.buffered.start(i), w), h / 2 - 3, X(media.buffered.end(i) - media.buffered.start(i), w), 6); }
    c.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--accent");
    c.fillRect(0, h / 2 - 3, X(media.currentTime, w), 6);
    // ölçek
    c.font = "10.5px Segoe UI"; c.fillStyle = "rgba(255,255,255,.4)"; c.textBaseline = "top";
    const step = [1, 5, 10, 15, 30, 60, 300, 600, 1800, 3600].find((s) => (s / (dur || 1)) * w >= 70) || 3600;
    for (let t = 0; t <= dur; t += step) { const x = X(t, w); c.fillRect(x, h - 8, 1, 8); c.fillText(rec0 ? fmtTime(rec0 + t * 1000).slice(0, step < 60 ? 8 : 5) : fmtHMS(t), x + 3, 2); }
    if (trim.on) {
      const a = X(trim.a, w), b = X(trim.b, w);
      c.fillStyle = "rgba(79,140,255,.28)"; c.fillRect(a, 0, b - a, h);
      c.fillStyle = getComputedStyle(document.documentElement).getPropertyValue("--accent");
      for (const x of [a, b]) { c.fillRect(x - 1.5, 0, 3, h); c.beginPath(); c.roundRect(x - 5, h / 2 - 10, 10, 20, 3); c.fill(); }
    }
    const px = X(media.currentTime, w);
    c.fillStyle = "#fff"; c.fillRect(px - 1, 0, 2, h);
    $("#pl-clock", el).textContent = `${fmtHMS(media.currentTime)} / ${fmtHMS(dur)}`;
    const rt = $("#pl-rec", el);
    if (rec0) { rt.hidden = false; rt.textContent = `${fmtTime(rec0 + media.currentTime * 1000)} · ${fmtDate(rec0 + media.currentTime * 1000)}`; }
    if (trim.on) { $("#pl-ta", el).textContent = fmtHMS(trim.a); $("#pl-tb", el).textContent = fmtHMS(trim.b); $("#pl-td", el).textContent = fmtDur((trim.b - trim.a) * 1000); }
  }
  media.ontimeupdate = draw;
  media.onprogress = draw;
  const tick = setInterval(() => { if (!document.body.contains(el)) return clearInterval(tick); if (!media.paused) draw(); }, 100);
  new ResizeObserver(draw).observe(tl);
  let tdrag = null;
  tl.addEventListener("mousedown", (e) => {
    const r = tl.getBoundingClientRect(), x = e.clientX - r.left;
    tdrag = { w: r.width, handle: null };
    if (trim.on) for (const k of ["a", "b"]) if (Math.abs(X(trim[k], r.width) - x) < 9) tdrag.handle = k;
    if (!tdrag.handle) media.currentTime = T(x, r.width);
    draw();
  });
  const tmove = (e) => {
    if (!tdrag) return;
    const r = tl.getBoundingClientRect(), t = T(e.clientX - r.left, r.width);
    if (tdrag.handle) { trim[tdrag.handle] = t; if (trim.a > trim.b) { [trim.a, trim.b] = [trim.b, trim.a]; tdrag.handle = tdrag.handle === "a" ? "b" : "a"; } media.currentTime = t; }
    else media.currentTime = t;
    draw();
  };
  window.addEventListener("mousemove", tmove);
  window.addEventListener("mouseup", () => (tdrag = null));
  const fmtSel = $("#pl-fmt", el);
  fmtSel.innerHTML = (S.state?.formats || []).map((f) => `<option value="${f.id}">${f.label}</option>`).join("");
  fmtSel.value = S.state?.settings?.format || "mp4";
  $("#pl-trim", el).onclick = () => {
    trim.on = !trim.on;
    if (trim.on) { const d = media.duration || 0, t = media.currentTime; trim.a = Math.max(0, t - 5); trim.b = Math.min(d, t + 5); if (trim.b - trim.a < 1) { trim.a = 0; trim.b = d; } }
    $("#pl-trimbox", el).hidden = !trim.on;
    $("#pl-trim", el).classList.toggle("primary", trim.on);
    draw();
  };
  $("#pl-trimx", el).onclick = () => $("#pl-trim", el).click();
  $("#pl-seta", el).onclick = () => { trim.a = Math.min(media.currentTime, trim.b - 0.5); draw(); };
  $("#pl-setb", el).onclick = () => { trim.b = Math.max(media.currentTime, trim.a + 0.5); draw(); };
  $("#pl-trimgo", el).onclick = async () => {
    try {
      const j = await api("/api/files/export", { method: "POST", body: { path: it.path, start: trim.a, end: trim.b, format: fmtSel.value } });
      toast(`Kesit hazırlanıyor: ${j.filename}`, { action: { label: "İndirilenler", run: close } });
      $("#pl-trim", el).click();
      pollDownloads();
    } catch (e) { toast(e.message, { err: true }); }
  };
}

// ------------------------------------------------------------------ olay logu görüntüleyici
async function logView(it) {
  const box = modal(`<div class="modal-head"><h2></h2><button class="btn icon ghost" data-x>${icon("x")}</button></div>
    <div class="modal-body" id="lv-body"><div class="spinner"></div></div>`);
  $(".modal-head h2", box).textContent = it.name;
  $("[data-x]", box).onclick = modalClose;
  try {
    const text = await (await fetch(fileUrl("media", it.path))).text();
    let rows;
    if (it.ext === "json") rows = (JSON.parse(text).olaylar || []).map((r) => [r.kamera, r.baslangic, r.bitis, r.sure_sn, r.olay]);
    else rows = text.replace(/^\uFEFF/, "").trim().split(/\r?\n/).slice(1).map((l) => { const c = l.split(";"); return [c[0], c[2], c[3], c[4], c[5]]; });
    $("#lv-body").innerHTML = `<p class="sub">${rows.length} olay</p><div style="overflow:auto;max-height:60vh"><table class="tbl"><thead><tr><th>Kamera</th><th>Başlangıç</th><th>Bitiş</th><th>Süre</th><th>Olay</th></tr></thead><tbody>${rows.map((r) => `<tr><td>${esc(r[0])}</td><td>${esc(r[1])}</td><td>${esc((r[2] || "").slice(11))}</td><td>${esc(r[3])} sn</td><td>${esc(r[4])}</td></tr>`).join("")}</tbody></table></div>`;
  } catch (e) { $("#lv-body").textContent = "Açılamadı: " + e.message; }
}

// ------------------------------------------------------------------ Disk sayfası
async function diskLoad(fresh = false) {
  const body = $("#disk-body");
  body.innerHTML = `<div class="card"><div style="display:flex;gap:12px;align-items:center"><div class="spinner"></div>Disk bilgileri okunuyor…</div></div>`;
  let d;
  try { d = await api("/api/disk" + (fresh ? "?fresh=1" : "")); }
  catch (e) { body.innerHTML = `<div class="card"><p class="sub" style="margin:0"></p></div>`; $("p", body).textContent = "Okunamadı: " + e.message; return; }
  const now = parseT(d.now);
  const oldest = d.ranges.map((r) => r.oldest).filter(Boolean).map(parseT);
  const minT = oldest.length ? Math.min(...oldest) : now - DAY;
  const totalUsed = d.disks.reduce((a, x) => a + x.used, 0), totalCap = d.disks.reduce((a, x) => a + x.total, 0);
  const days = (now - minT) / DAY;
  const perDay = days > 0.5 ? totalUsed / days : null;
  const disks = d.disks.map((x) => `
    <div style="margin-bottom:14px">
      <div style="display:flex;align-items:center;gap:10px"><b>${esc(x.name)}</b>
        <span class="pill ${x.error ? "" : "on"}" style="${x.error ? "background:rgba(240,82,79,.18);color:#ff8a88" : ""}">${x.error ? "Hata var" : "Sağlıklı"}</span>
        <span class="sub" style="margin:0">${esc(x.state === "Success" ? "Çalışıyor" : x.state)}</span>
        <div style="flex:1"></div><span>${fmtSize(x.used)} / ${fmtSize(x.total)}</span></div>
      <div class="disk-bar"><i style="width:${x.total ? Math.round(x.used / x.total * 100) : 0}%"></i></div>
      <div class="sub" style="margin:0">${x.partitions.length} bölüm · %${x.total ? Math.round(x.used / x.total * 100) : 0} dolu</div>
    </div>`).join("") || `<p class="sub">Disk bulunamadı.</p>`;
  const rows = d.ranges.map((r) => {
    const a = r.oldest ? parseT(r.oldest) : null;
    const left = a ? (a - minT) / (now - minT || 1) * 100 : 0;
    return `<div class="range-row"><div><b>${esc(r.name)}</b></div>
      <div><div class="range-track"><i style="left:${left}%;right:0"></i></div>
        <div class="muted" style="margin-top:4px">${a ? `${fmtDate(a)} ${r.oldest.slice(11, 16)} → şimdi` : "Kayıt bulunamadı"}</div></div>
      <div style="text-align:right">${a ? `<b>${((now - a) / DAY).toFixed(1)} gün</b>` : "—"}</div></div>`;
  }).join("");
  body.innerHTML = `
    <div class="card">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px"><h2 style="flex:1">Diskler</h2>
        <button class="btn small" id="dk-refresh">${icon("refresh")}Yenile</button></div>
      <p class="sub">Kayıt cihazındaki sabit disk(ler). ${d.overwrite ? "Disk dolunca <b>en eski kayıtların üzerine yazılır</b> (kayıt hiç durmaz)." : d.overwrite === false ? "<b>Dikkat:</b> disk dolunca kayıt durur (üzerine yazma kapalı)." : ""}</p>
      ${disks}
    </div>
    <div class="info-grid" style="margin-bottom:16px">
      <div><span>Toplam kapasite</span><b>${fmtSize(totalCap)}</b></div>
      <div><span>Ortalama günlük kullanım</span><b>${perDay ? fmtSize(perDay) + " / gün" : "—"}</b></div>
      <div><span>Kayıtlar geriye gidiyor</span><b>${days ? days.toFixed(1) + " gün" : "—"}</b></div>
    </div>
    <div class="card">
      <h2>Diskte duran kayıtlar</h2>
      <p class="sub">Her kameranın diskteki en eski kaydından bugüne. Disk dolu olduğu için bu süre her gün yaklaşık sabit kalır; eskiler silinip yenileri yazılır.</p>
      ${rows || `<p class="sub">Kamera bulunamadı.</p>`}
    </div>
    ${S.state?.device?.perms?.users ? `<div class="card"><div style="display:flex;align-items:center;gap:10px"><div style="flex:1"><h2>Disk ve sistem olayları</h2>
      <p class="sub" style="margin:0">Disk sağlığı (S.M.A.R.T.), disk değişimleri, yeniden başlatmalar.</p></div>
      <button class="btn" id="dk-logs">${icon("history")}Olayları göster</button></div></div>` : ""}
    <p class="sub">Bilgiler 10 dakikada bir tazelenir (cihazı yormamak için). Son okuma: ${fmtTime(now)}</p>`;
  $("#dk-refresh").onclick = () => diskLoad(true);
  if ($("#dk-logs")) $("#dk-logs").onclick = () => logsOpen("", "Sistem");
}

// ------------------------------------------------------------------ olaylar
(function bindLibrary() {
  for (const id of ["lib-q", "lib-from", "lib-to", "lib-type", "lib-sort", "lib-cam"]) $("#" + id).addEventListener(id === "lib-q" ? "input" : "change", libRender);
  $("#lib-clear").onclick = () => { $("#lib-q").value = ""; $("#lib-from").value = ""; $("#lib-to").value = ""; $("#lib-type").value = ""; $("#lib-cam").value = ""; libRender(); };
  $("#mp-save").onclick = async () => {
    const o = $("#mp-old").value, n = $("#mp-new").value, n2 = $("#mp-new2").value;
    if (!o || !n) return toast("Mevcut ve yeni şifreyi girin", { err: true });
    if (n.length < 8) return toast("Yeni şifre en az 8 karakter olmalı", { err: true });
    if (n !== n2) return toast("Yeni şifreler aynı değil", { err: true });
    try {
      await api("/api/my-password", { method: "POST", body: { old: o, new: n } });
      ["#mp-old", "#mp-new", "#mp-new2"].forEach((s) => ($(s).value = ""));
      toast("Şifreniz değiştirildi. Program yeni şifreyle yeniden bağlanıyor.", { time: 7000 });
    } catch (e) { toast(e.message, { err: true }); }
  };
})();

// ------------------------------------------------------------------ Ayarlar › Program verileri
async function appDataRender() {
  const box = $("#appdata-body");
  if (!box) return;
  let d;
  try { d = await api("/api/appdata"); } catch (e) { box.textContent = "Okunamadı: " + e.message; return; }
  const lg = d.logs, cache = d.playcache.bytes + d.thumbs.bytes;
  const canLogs = !!S.state?.device?.perms?.users;
  const days = [7, 14, 30, 60, 90];
  box.innerHTML = `
    ${canLogs ? `<div class="setting">
      <label>Cihaz logu arşivi</label>
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <span><b>${lg.entries.toLocaleString("tr-TR")}</b> kayıt · ${fmtSize(lg.bytes)}${lg.oldest ? ` · ${esc(lg.oldest.slice(0, 16))} → ${esc(lg.newest.slice(0, 16))}` : ""}</span>
        <div style="flex:1"></div>
        <label class="sub" style="margin:0;display:flex;gap:6px;align-items:center">Sakla:
          <select id="ad-keep" class="date-input" style="height:32px">${days.map((x) => `<option value="${x}" ${x === lg.keep_days ? "selected" : ""}>${x} gün</option>`).join("")}</select></label>
        <button class="btn small danger" id="ad-logs">${icon("x")}Arşivi temizle</button>
      </div>
      <div class="note">Kullanıcı geçmişi ve Sistem olayları pencerelerinin hızlı açılması için cihazın logu burada da saklanır. Seçilen süreden eski kayıtlar <b>kendiliğinden silinir</b>. "Arşivi temizle" yalnızca bu bilgisayardaki kopyayı siler; cihazdaki asıl log silinmez (cihaz kendi logunu kapasitesi dolunca en eskiden başlayarak kendisi siler).</div>
    </div>` : ""}
    <div class="setting">
      <label>Oynatma kopyaları ve küçük resimler</label>
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <span><b>${fmtSize(cache)}</b> · ${d.playcache.files + d.thumbs.files} dosya</span>
        <div style="flex:1"></div>
        <button class="btn small danger" id="ad-cache" ${cache ? "" : "disabled"}>${icon("x")}Temizle</button>
      </div>
      <div class="note">İndirilenler sayfasındaki küçük resimler ve tarayıcının doğrudan açamadığı videolar (MKV, AVI, DAV) için hazırlanan oynatma kopyaları. En fazla ~3 GB tutulur, eskileri kendiliğinden silinir. Temizlerseniz gerektiğinde yeniden oluşturulur.</div>
    </div>`;
  if (canLogs) {
    $("#ad-keep").onchange = async (e) => {
      await api("/api/settings", { method: "POST", body: { log_keep_days: +e.target.value } });
      await api("/api/appdata/clear", { method: "POST", body: { what: "prune" } });
      toast(`Log arşivi artık ${e.target.value} gün saklanacak`);
      appDataRender();
    };
    $("#ad-logs").onclick = async () => {
      if (!confirm("Bu bilgisayardaki log arşivi silinsin mi?\n\nCihazdaki asıl log silinmez. Kullanıcı geçmişi bir sonraki açılışta cihazdan yeniden okunur (ilk açılış biraz uzun sürer).")) return;
      await api("/api/appdata/clear", { method: "POST", body: { what: "logs" } });
      toast("Log arşivi temizlendi");
      appDataRender();
    };
  }
  $("#ad-cache").onclick = async () => {
    await api("/api/appdata/clear", { method: "POST", body: { what: "cache" } });
    toast("Oynatma kopyaları ve küçük resimler temizlendi");
    appDataRender();
  };
}
