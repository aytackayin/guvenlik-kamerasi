"use strict";
/* Disk › Kayıt dosyaları ve olaylar: diskteki kayıtları ve akıllı hareket olaylarını listeler, indirir */

const E = { files: [], rows: [], sel: new Set(), loaded: false, busy: false };
const P2P_BPS = 1.8e6;      // P2P ile ölçülen yaklaşık indirme hızı (bayt/sn)

const exTypes = [
  ["hours", "Saatlik kayıtlar"], ["evhours", "Olaylı saatler"], ["events", "Tüm olaylar"],
  ["İnsan", "Olay: İnsan"], ["Araç", "Olay: Araç"], ["Hareket", "Olay: Hareket"],
];

function diskTab(t) {
  $$("#disk-tabs button").forEach((b) => b.classList.toggle("on", b.dataset.t === t));
  $("#disk-body").hidden = t !== "general";
  $("#disk-files").hidden = t !== "files";
  if (t === "files" && !E.loaded) exInit();
}

function exInit() {
  const today = dayKey(devNow());
  if (!$("#ex-from").value) { $("#ex-from").value = today; $("#ex-to").value = today; }
  $("#ex-from").max = $("#ex-to").max = today;
  const t = $("#ex-type");
  if (t.options[0]?.value !== exTypes[0][0]) t.innerHTML = exTypes.map(([v, l]) => `<option value="${v}">${l}</option>`).join("");
  const cams = $("#ex-cams");
  if (!cams.children.length) {
    for (const c of S.channels) {
      const l = document.createElement("label");
      l.className = "chip";
      l.innerHTML = `<input type="checkbox" value="${c.id}" checked><span></span>`;
      $("span", l).textContent = c.name;
      $("input", l).onchange = exRender;
      cams.appendChild(l);
    }
  }
  exLoad();
}

const exCams = () => $$("#ex-cams input:checked").map((i) => +i.value);

async function exLoad() {
  const from = $("#ex-from").value, to = $("#ex-to").value;
  if (!from || !to) return;
  if (to < from) return toast("Bitiş tarihi başlangıçtan önce olamaz", { err: true });
  const days = (parseT(to + "T00:00:00") - parseT(from + "T00:00:00")) / DAY + 1;
  if (days > 15) return toast("En fazla 15 günlük aralık listelenebilir", { err: true });
  $("#ex-list").innerHTML = `<div style="display:flex;gap:12px;align-items:center;padding:16px"><div class="spinner"></div>Cihazdaki kayıtlar okunuyor…</div>`;
  $("#ex-bar").innerHTML = "";
  try {
    const all = S.channels.map((c) => c.id).join(",");
    const r = await api(`/api/recfiles?from=${from}&to=${to}&ch=${all}`);
    E.files = r.files; E.loaded = true; E.sel.clear();
    exRender();
  } catch (e) { $("#ex-list").textContent = "Okunamadı: " + e.message; }
}

// saatlik satırlar: kayıt parçalarını kamera+saat bazında topla
function exHourRows(files) {
  const map = new Map();
  for (const f of files) {
    let a = parseT(f.start), b = parseT(f.end);
    const dur = Math.max(1, b - a);
    while (a < b) {
      const h0 = new Date(a); h0.setMinutes(0, 0, 0);
      const h1 = h0.getTime() + 3600000, e = Math.min(b, h1);
      const key = `${f.channel}|${fmtISO(h0.getTime()).slice(0, 13)}`;
      let row = map.get(key);
      if (!row) map.set(key, row = { key, kind: "hour", channel: f.channel, camera: f.camera, start: a, end: e, hour: h0.getTime(), size: 0, events: [] });
      row.start = Math.min(row.start, a); row.end = Math.max(row.end, e);
      row.size += f.size * (e - a) / dur;
      if (f.kind === "event") row.events.push({ start: Math.max(a, parseT(f.start)), end: Math.min(e, parseT(f.end)), names: f.event_names });
      a = e;
    }
  }
  return [...map.values()];
}

function exEventRows(files) {
  return files.filter((f) => f.kind === "event").map((f) => ({
    key: `${f.channel}|${f.start}`, kind: "event", channel: f.channel, camera: f.camera,
    start: parseT(f.start), end: parseT(f.end), size: f.size, names: f.event_names,
    events: [{ start: parseT(f.start), end: parseT(f.end), names: f.event_names }],
  }));
}

function inHours(a, b) {
  const h1 = $("#ex-h1").value, h2 = $("#ex-h2").value;
  if (!h1 || !h2) return true;
  // satır, herhangi bir günün saat penceresiyle kesişiyor mu?
  for (let d = dayStart(a) - DAY; d <= b; d += DAY) {
    const s = parseT(fmtISO(d).slice(0, 10) + "T" + h1 + ":00");
    let e = parseT(fmtISO(d).slice(0, 10) + "T" + h2 + ":00");
    if (e <= s) e += DAY;
    if (s < b && e > a) return true;
  }
  return false;
}

function exRender() {
  if (!E.loaded) return;
  const type = $("#ex-type").value, cams = new Set(exCams());
  const q = $("#ex-q").value.trim().toLocaleLowerCase("tr");
  const files = E.files.filter((f) => cams.has(f.channel));
  let rows = ["hours", "evhours"].includes(type) ? exHourRows(files) : exEventRows(files);
  if (type === "evhours") rows = rows.filter((r) => r.events.length);
  if (["İnsan", "Araç", "Hareket"].includes(type)) rows = rows.filter((r) => (r.names || []).includes(type));
  rows = rows.filter((r) => inHours(r.start, r.end));
  if (q) rows = rows.filter((r) => {
    const names = r.kind === "event" ? r.names : [...new Set(r.events.flatMap((e) => e.names))];
    const hay = `${r.camera} ${fmtISO(r.start).slice(0, 10)} ${fmtDate(r.start)} ${fmtTime(r.start)} ${fmtTime(r.end)} ${names.join(" ")}`.toLocaleLowerCase("tr");
    return q.split(/\s+/).every((w) => hay.includes(w));
  });
  const sort = $("#ex-sort").value;
  const key = { desc: (r) => -r.start, asc: (r) => r.start, size: (r) => -r.size, events: (r) => -r.events.length, cam: (r) => r.channel * 1e13 + r.start }[sort];
  rows.sort((a, b) => key(a) - key(b));
  E.rows = rows;
  for (const k of [...E.sel]) if (!rows.some((r) => r.key === k)) E.sel.delete(k);

  const list = $("#ex-list");
  if (!rows.length) { list.innerHTML = `<div class="lib-empty">Bu filtrelere uyan kayıt yok.${E.files.some((f) => f.kind === "event") ? "" : " (Akıllı hareket algılama yeni açıldığı için eski günlerde olay kaydı yoktur.)"}</div>`; exBar(); return; }
  const shown = rows.slice(0, 600);
  list.innerHTML = `<div class="ex-head"><span></span><span>Tür</span><span>Kamera</span><span>Zaman</span><span>Boyut</span><span></span></div>`;
  for (const r of shown) {
    const el = document.createElement("div");
    el.className = "ex-row";
    const names = r.kind === "event" ? r.names : [...new Set(r.events.flatMap((e) => e.names))];
    const badge = r.kind === "event"
      ? `<span class="ex-type ${names.includes("İnsan") ? "hum" : names.includes("Araç") ? "veh" : "ev"}">${esc(names.join(", ") || "Olay")}</span>`
      : `<span class="ex-type">Kayıt</span>`;
    const span = Math.max(1, r.end - r.start);
    const mini = r.kind === "hour" ? `<div class="ex-mini" title="${r.events.length} olay">${r.events.map((e) => `<i style="left:${(e.start - r.hour) / 36000}%;width:${Math.max(0.3, (e.end - e.start) / 36000)}%"></i>`).join("")}</div>` : "";
    el.innerHTML = `<input type="checkbox" ${E.sel.has(r.key) ? "checked" : ""}>${badge}<div>${esc(r.camera)}</div>
      <div class="ex-when"><b>${fmtTime(r.start).slice(0, r.kind === "event" ? 8 : 5)} – ${fmtTime(r.end).slice(0, r.kind === "event" ? 8 : 5)}</b>
        <span class="sub2"> · ${fmtDate(r.start)} · ${fmtDur(span)}${r.kind === "hour" ? ` · ${r.events.length ? `<b style="color:#ffc46b">${r.events.length} olay</b>${names.length ? " (" + esc(names.join(", ")) + ")" : ""}` : "olay yok"}` : ""}</span>${mini}</div>
      <div>${fmtSize(r.size)}</div>
      <div class="dl-actions"><button class="btn small" data-a="play" title="Kayıtlar ekranında oynat">${icon("play")}</button>
        <button class="btn small" data-a="video">${icon("download")}Video</button>
        <button class="btn small" data-a="log" ${r.events.length ? "" : "disabled"}>${icon("history")}Log</button></div>`;
    $("input", el).onchange = (e) => { e.target.checked ? E.sel.add(r.key) : E.sel.delete(r.key); exBar(); };
    el.addEventListener("click", (e) => {
      const a = e.target.closest("[data-a]")?.dataset.a;
      if (a === "video") exDownloadDialog([r]);
      else if (a === "log") exLogDialog([r]);
      else if (a === "play") { go("archive"); setTimeout(() => { if (!S.single || S.single !== r.channel) openSingle(r.channel); seek(r.start); }, 300); }
    });
    list.appendChild(el);
  }
  if (rows.length > shown.length) list.insertAdjacentHTML("beforeend", `<p class="sub" style="padding:10px 16px">İlk ${shown.length} satır gösteriliyor (toplam ${rows.length}). Aralığı daraltın.</p>`);
  exBar();
}

function exBar() {
  const bar = $("#ex-bar");
  const rows = E.rows, sel = rows.filter((r) => E.sel.has(r.key));
  const size = rows.reduce((a, r) => a + r.size, 0), evs = rows.reduce((a, r) => a + r.events.length, 0);
  const ssize = sel.reduce((a, r) => a + r.size, 0), sevs = sel.reduce((a, r) => a + r.events.length, 0);
  bar.innerHTML = `<label class="chip"><input type="checkbox" id="ex-all" ${sel.length && sel.length === rows.length ? "checked" : ""}> Tümünü seç</label>
    <span class="sub" style="margin:0">${rows.length} satır · ${fmtSize(size)} · ${evs} olay${sel.length ? ` — <b style="color:var(--text)">${sel.length} seçili (${fmtSize(ssize)}, ${sevs} olay)</b>` : ""}</span>
    <div style="flex:1"></div>
    <button class="btn small" id="ex-sel-log" ${sel.length && sevs ? "" : "disabled"}>${icon("history")}Seçilenlerin olay logu</button>
    <button class="btn small primary" id="ex-sel-video" ${sel.length ? "" : "disabled"}>${icon("download")}Seçilenleri indir</button>`;
  $("#ex-all").onchange = (e) => { E.sel = new Set(e.target.checked ? rows.map((r) => r.key) : []); exRender(); };
  $("#ex-sel-video").onclick = () => exDownloadDialog(sel);
  $("#ex-sel-log").onclick = () => exLogDialog(sel);
}

function exHoursHtml(prefix) {
  const h1 = $("#ex-h1").value, h2 = $("#ex-h2").value;
  return `<label class="switch" style="margin-top:4px"><input type="checkbox" id="${prefix}-hon" ${h1 && h2 ? "checked" : ""}> Yalnızca her günün şu saatleri arası:</label>
    <div style="display:flex;gap:8px;align-items:center;margin:8px 0 0 50px"><input type="time" id="${prefix}-h1" class="date-input" value="${h1 || "07:00"}"> – <input type="time" id="${prefix}-h2" class="date-input" value="${h2 || "19:00"}">
    <span class="hint" style="margin:0">Ör. geceleri karanlık kayıtları istemiyorsanız 07:00 – 19:00</span></div>`;
}
const exHours = (p) => ($(`#${p}-hon`).checked ? { from: $(`#${p}-h1`).value, to: $(`#${p}-h2`).value } : null);

function exDownloadDialog(rows) {
  if (!rows.length) return;
  const evs = rows.reduce((a, r) => a + r.events.length, 0);
  const size = rows.reduce((a, r) => a + r.size, 0);
  const box = modal(`
    <div class="modal-head"><h2>Video indir</h2><button class="btn icon ghost" data-x>${icon("x")}</button></div>
    <div class="modal-body">
      <p class="sub">${rows.length} parça · ${new Set(rows.map((r) => r.camera)).size} kamera · yaklaşık ${fmtSize(size)}. İndirme internet üzerinden (P2P) tahminen <b id="dd-eta"></b> sürer; aynı ağdayken çok daha hızlıdır.</p>
      <div class="form" style="grid-template-columns:140px 1fr">
        <label>Biçim</label><select id="dd-fmt" class="date-input">${(S.state?.formats || []).map((f) => `<option value="${f.id}">${f.label}</option>`).join("")}</select>
      </div>
      <div style="margin-top:14px">${exHoursHtml("dd")}</div>
      <div style="margin-top:16px;padding:12px;border-radius:10px;background:var(--panel-2)">
        ${evs ? `<label class="switch"><input type="checkbox" id="dd-ev" checked> <span><b>Bu aralığın olay logunu da indir</b> (${evs} olay)</span></label>
        <div style="margin:8px 0 0 50px"><select id="dd-evfmt" class="date-input"><option value="csv">CSV (Excel'de açılır)</option><option value="json">JSON (tarama programı için)</option><option value="both">İkisi de</option></select></div>`
        : `<span class="sub" style="margin:0">Bu aralıkta akıllı hareket olayı yok; yalnızca video indirilecek.</span>`}
      </div>
    </div>
    <div class="modal-foot"><button class="btn ghost" data-x>Vazgeç</button><button class="btn primary" id="dd-go">${icon("download")}İndirmeyi başlat</button></div>`);
  $("#dd-fmt").value = S.state?.settings?.format || "mp4";
  const eta = () => {
    const h = exHours("dd");
    let frac = 1;
    if (h) { const a = parseT("2000-01-01T" + h.from + ":00"), b0 = parseT("2000-01-01T" + h.to + ":00"); frac = (b0 > a ? b0 - a : b0 + DAY - a) / DAY; }   // kaba tahmin
    const sec = size * frac / P2P_BPS;
    $("#dd-eta").textContent = sec < 90 ? `${Math.max(1, Math.round(sec))} sn` : sec < 5400 ? `${Math.round(sec / 60)} dk` : `${(sec / 3600).toFixed(1)} saat`;
  };
  eta();
  ["dd-hon", "dd-h1", "dd-h2"].forEach((id) => $("#" + id).addEventListener("change", eta));
  $$("[data-x]", box).forEach((b) => (b.onclick = modalClose));
  $("#dd-go").onclick = async () => {
    const items = rows.map((r) => ({ channel: r.channel, start: fmtISO(r.start), end: fmtISO(r.end) }));
    const body = { items, format: $("#dd-fmt").value, hours: exHours("dd"), with_events: !!$("#dd-ev")?.checked, events_format: $("#dd-evfmt")?.value || "csv" };
    $("#dd-go").disabled = true;
    try {
      const r = await api("/api/downloads", { method: "POST", body });
      modalClose();
      const ev = r.events ? (r.events.error ? ` Olay logu alınamadı: ${r.events.error}` : ` ${r.events.count} olay logu kaydedildi.`) : "";
      toast(`${r.jobs.length} parça indirme sırasına alındı.${ev}`, { time: 7000, action: { label: "İndirilenler", run: () => go("downloads") } });
      pollDownloads();
    } catch (e) { $("#dd-go").disabled = false; toast(e.message, { err: true }); }
  };
}

function exLogDialog(rows) {
  if (!rows.length) return;
  const evs = rows.reduce((a, r) => a + r.events.length, 0);
  const box = modal(`
    <div class="modal-head"><h2>Olay logu indir</h2><button class="btn icon ghost" data-x>${icon("x")}</button></div>
    <div class="modal-body">
      <p class="sub">${evs} olay · ${new Set(rows.map((r) => r.camera)).size} kamera. Log; kamera, başlangıç-bitiş saati, süre ve olay türünü (insan / araç / hareket) içerir. Videolarla birlikte tarama programında kullanılabilir.</p>
      <div class="form" style="grid-template-columns:140px 1fr"><label>Biçim</label>
        <select id="dl-evfmt" class="date-input"><option value="csv">CSV (Excel'de açılır)</option><option value="json">JSON (tarama programı için)</option><option value="both">İkisi de</option></select></div>
      <div style="margin-top:14px">${exHoursHtml("dl")}</div>
    </div>
    <div class="modal-foot"><button class="btn ghost" data-x>Vazgeç</button><button class="btn primary" id="dl-go">${icon("download")}Logu kaydet</button></div>`);
  $$("[data-x]", box).forEach((b) => (b.onclick = modalClose));
  $("#dl-go").onclick = async () => {
    const chans = [...new Set(rows.map((r) => r.channel))];
    const start = Math.min(...rows.map((r) => r.start)), end = Math.max(...rows.map((r) => r.end));
    $("#dl-go").disabled = true;
    try {
      const r = await api("/api/events/export", { method: "POST", body: { channels: chans, start: fmtISO(start), end: fmtISO(end), hours: exHours("dl"), format: $("#dl-evfmt").value } });
      modalClose();
      toast(`${r.count} olay kaydedildi`, { time: 6000, action: { label: "Göster", run: () => api("/api/open", { method: "POST", body: { path: r.files[0], select: true } }) } });
    } catch (e) { $("#dl-go").disabled = false; toast(e.message, { err: true }); }
  };
}

(function bindExplorer() {
  $$("#disk-tabs button").forEach((b) => (b.onclick = () => diskTab(b.dataset.t)));
  $("#ex-load").onclick = exLoad;
  $("#ex-from").onchange = () => { if ($("#ex-to").value < $("#ex-from").value) $("#ex-to").value = $("#ex-from").value; };
  for (const id of ["ex-type", "ex-sort", "ex-h1", "ex-h2"]) $("#" + id).addEventListener("change", exRender);
  $("#ex-q").addEventListener("input", exRender);
})();
