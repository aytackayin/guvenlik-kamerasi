"use strict";
/* Ayarlar › Kamera Ayarları sekmesi */

const RES_LABEL = {
  "1296x1944": "5M-N · 1296×1944", "1280x1440": "4M-N · 1280×1440", "1920x1080": "1080P · 1920×1080",
  "960x1080": "1080N · 960×1080", "1280x720": "720P · 1280×720", "960x576": "960H · 960×576",
  "704x576": "D1 · 704×576", "352x288": "CIF · 352×288",
};
// kameranın megapikseline göre anlamlı kayıt çözünürlükleri (yüksekten düşüğe)
const MP_RES = {
  5: ["1296x1944", "1280x1440", "1920x1080", "1280x720", "704x576", "352x288"],
  4: ["1280x1440", "1920x1080", "1280x720", "704x576", "352x288"],
  2: ["1920x1080", "960x1080", "1280x720", "960x576", "704x576", "352x288"],
  1: ["1280x720", "960x576", "704x576", "352x288"],
};
const BITRATES = [256, 384, 448, 512, 768, 1024, 1536, 2048, 2560, 3072, 3584, 4096, 5120, 6144, 8192];
const CAM = { ch: null, cfg: null, form: null, busy: false, warnings: [], player: null };

const camChannels = () => (S.state?.channels || []);
const camInfo = (ch) => camChannels().find((c) => c.id === ch);

function camTabs() {
  $$("#set-tabs button").forEach((b) => b.addEventListener("click", () => showSetTab(b.dataset.tab)));
}

function showSetTab(tab) {
  S.setTab = tab;
  $$("#set-tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  $("#tab-general").hidden = tab !== "general";
  $("#tab-cams").hidden = tab !== "cams";
  $("#tab-users").hidden = tab !== "users";
  $("#tab-about").hidden = tab !== "about";
  if (tab === "about") aboutOpen();
  if (tab === "cams") camOpen(); else camClosePreview();
  if (tab === "users") usersOpen();
  if (tab === "general") appDataRender();
}

function camOpen() {
  const chans = camChannels();
  const pick = $("#cam-pick");
  if (!S.state?.device?.connected) {
    pick.innerHTML = "";
    $("#cam-form").innerHTML = `<div class="card"><p class="sub" style="margin:0">Kamera ayarları için önce cihaza bağlanın.</p></div>`;
    return;
  }
  pick.innerHTML = "";
  for (const c of chans) {
    const b = document.createElement("button");
    b.innerHTML = `${icon("cam")}<span></span>`;
    $("span", b).textContent = c.name;
    b.classList.toggle("on", c.id === CAM.ch);
    b.onclick = () => camLoad(c.id);
    pick.appendChild(b);
  }
  if (!CAM.ch || !camInfo(CAM.ch)) camLoad(chans[0]?.id);
  else if (!CAM.cfg) camLoad(CAM.ch);
  else camRender();
}

async function camLoad(ch) {
  if (!ch) return;
  CAM.ch = ch;
  CAM.cfg = null;
  CAM.warnings = [];
  $$("#cam-pick button").forEach((b, i) => b.classList.toggle("on", camChannels()[i]?.id === ch));
  $("#cam-form").innerHTML = `<div class="card"><div style="display:flex;align-items:center;gap:12px"><div class="spinner"></div>Kamera ayarları cihazdan okunuyor…</div></div>`;
  try {
    const cfg = await api(`/api/camera/${ch}`);
    if (CAM.ch !== ch) return;
    CAM.cfg = cfg;
    CAM.form = camFormFrom(cfg);
    CAM.motion = cfg.motion ? { ...cfg.motion } : null;
    camRender(true);
  } catch (e) {
    $("#cam-form").innerHTML = `<div class="card"><p class="sub" style="margin:0"></p></div>`;
    $("#cam-form p").textContent = "Ayarlar okunamadı: " + e.message;
  }
}

function camFormFrom(cfg) {
  const pick = (s) => ({ resolution: s.resolution, fps: s.fps, compression: s.compression, control: s.control, quality: s.quality, bitrate: s.bitrate, gop: s.gop, audio: s.audio });
  return { main: pick(cfg.main), sub: pick(cfg.sub), smart: cfg.smart, ai: cfg.ai, prefs: { ...cfg.prefs } };
}

function camResOptions(mp, caps) {
  const dev = caps.main.resolutions;
  const list = (MP_RES[mp] || MP_RES[5]).filter((r) => dev.includes(r));
  return list.length ? list : dev;
}

// Bu kayıt cihazı kodlama gücünü kameralar arasında paylaştırır. Ölçüm: 5M-N'de 4 kamera
// birlikte en fazla 10 fps alabiliyor; daha düşük çözünürlüklerde 15 fps'e yer kalıyor.
const recFps = (res, max) => Math.min(res === "1296x1944" ? 10 : 15, max);

// Önerilen (varsayılan) ayarlar
function camRecommended(cfg, mp) {
  const cm = cfg.caps.main, cs = cfg.caps.sub;
  const res = camResOptions(mp, cfg.caps)[0];
  const fps = recFps(res, cm.fps_max);
  const subRes = cs.resolutions.includes("704x576") ? "704x576" : cs.resolutions[0];
  return {
    main: { resolution: res, fps, compression: cm.compressions.includes("H.265") ? "H.265" : cm.compressions[0],
            control: "VBR", quality: 6, bitrate: cm.bitrate_max, gop: Math.min(cm.gop_max, fps), audio: cfg.main.audio },
    sub: { resolution: subRes, fps: cs.fps_max, compression: cs.compressions.includes("H.265") ? "H.265" : cs.compressions[0],
           control: cfg.sub.control, quality: cfg.sub.quality, bitrate: cs.bitrate_max, gop: cfg.sub.gop, audio: cfg.sub.audio },
    smart: true, ai: cfg.ai == null ? null : true,
  };
}

function camChanged() {
  const f = CAM.form, c = CAM.cfg;
  if (!f || !c) return false;
  const keys = ["resolution", "fps", "compression", "control", "quality", "bitrate", "gop", "audio"];
  return keys.some((k) => String(f.main[k]) !== String(c.main[k])) ||
    ["resolution", "fps", "bitrate"].some((k) => String(f.sub[k]) !== String(c.sub[k])) ||
    f.smart !== c.smart || (c.ai != null && f.ai !== c.ai);
}

function sel(id, options, value) {
  return `<select id="${id}">${options.map(([v, l]) => `<option value="${v}"${String(v) === String(value) ? " selected" : ""}>${l}</option>`).join("")}</select>`;
}
function sw(id, on) { return `<label class="switch"><input type="checkbox" id="${id}"${on ? " checked" : ""}> <span>${on ? "Açık" : "Kapalı"}</span></label>`; }
function row(label, control, note, { off = false, changed = false } = {}) {
  return `<div class="setting${off ? " off" : ""}"><label>${label}${changed ? '<span class="changed-dot" title="Kaydedilmemiş değişiklik"></span>' : ""}</label><div>${control}</div><div class="note">${note}</div></div>`;
}

function camRender(fresh = false) {
  const cfg = CAM.cfg, f = CAM.form, info = camInfo(CAM.ch);
  if (!cfg || !f) return;
  const cm = cfg.caps.main, cs = cfg.caps.sub, m = f.main, s = f.sub, p = f.prefs;
  const ch = (k, sub = false) => String((sub ? s : m)[k]) !== String((sub ? cfg.sub : cfg.main)[k]);
  const resOpts = camResOptions(p.mp, cfg.caps).map((r) => [r, RES_LABEL[r] || r]);
  if (!resOpts.some(([r]) => r === m.resolution)) resOpts.push([m.resolution, (RES_LABEL[m.resolution] || m.resolution) + " (şu anki)"]);
  const rf = recFps(m.resolution, cm.fps_max);
  const fpsOpts = Array.from({ length: Math.max(cm.fps_max, m.fps || 1) }, (_, i) => [i + 1, `${i + 1} fps${i + 1 === rf ? " (önerilen)" : ""}`]);
  const subFps = Array.from({ length: Math.max(cs.fps_max, s.fps || 1) }, (_, i) => [i + 1, `${i + 1} fps`]);
  const brOpts = (lo, hi, cur) => {
    const l = BITRATES.filter((b) => b >= lo && b <= hi);
    if (!l.includes(hi)) l.push(hi);
    if (cur && !l.includes(cur)) l.push(cur);
    return l.sort((a, b) => a - b).map((b) => [b, `${b} kbps${b === hi ? " (en yüksek)" : ""}`]);
  };
  const gopOpts = Array.from({ length: Math.max(cm.gop_max, m.gop || 1) }, (_, i) => [i + 1, `${i + 1}`]);
  const vbr = m.control === "VBR";

  // disk tahmini: bu ayarlar bağlı tüm kameralarda kullanılsaydı
  const n = Math.max(1, camChannels().length);
  const gbDay = m.bitrate * 1000 / 8 * 86400 / 1e9;
  const days = cfg.disk_bytes ? cfg.disk_bytes / 1e9 / (gbDay * n) : null;
  const variable = vbr || f.smart;

  const warn = CAM.warnings.length ? `<div class="warn-box">${CAM.warnings.map((w) => `• ${escapeHtml(w)}`).join("<br>")}</div>` : "";

  const enh = cfg.enhance == null ? "" : `
  <div class="card">
    <h2>Kayıt cihazı: Kodlama Geliştirme</h2>
    <p class="sub">Tüm kameraları etkiler. Değiştirildiğinde kayıt cihazı yeniden başlar (1–2 dakika kayıt durur).</p>
    ${row("Kodlama Geliştirme", sw("c-enh", cfg.enhance),
      "Kayıt cihazının işlem gücünü yapay zekâ analizlerinden alıp görüntü kodlamaya verir. Dahua'ya göre açıkken kanal 1–4 5M-N'de 20 fps'e kadar çıkabilir. Karşılığında cihaz şu özellikleri kullanamaz: yüz algılama/tanıma, çevre koruma (sanal çizgi/alan), video kalite analizi, sahne değişikliği algılama ve QuickPick. <b>Not:</b> Bu kayıt cihazında (donanım V1.0, 2021 yazılımı) denendi; yeniden başlatmadan sonra bile kare hızı sınırı değişmedi. Yeni bir yazılım güncellemesiyle çalışabilir.")}
  </div>`;

  const html = `
  ${warn}
  ${enh}
  <div class="card">
    <h2>Görünüm</h2>
    <p class="sub">Bu bölüm yalnızca bu programdaki görüntüyü etkiler, cihaza yazılmaz ve hemen uygulanır.</p>
    <div class="cam-preview" id="cam-preview"></div>
    ${row("Kamera megapikseli", sel("c-mp", [[5, "5 MP (2592×1944)"], [4, "4 MP (2560×1440)"], [2, "2 MP (1920×1080)"], [1, "1 MP (1280×720)"]], p.mp),
      "Kameranızın gerçek çözünürlüğü (kameranın etiketinde yazar). <b>Kayıt çözünürlüğü listesi</b> ve otomatik görüntü oranı buna göre belirlenir.")}
    ${row("Görüntü oranı", sel("c-aspect", [["auto", `Otomatik (${p.mp == 5 ? "4:3" : "16:9"})`], ["16:9", "16:9 (geniş)"], ["4:3", "4:3"], ["orig", "Orijinal (cihazın kaydettiği gibi)"]], p.aspect),
      "Görüntü yandan sıkışık ya da yayvan görünüyorsa değiştirin. Kayıt cihazı bazı modlarda görüntüyü yatayda sıkıştırarak kaydeder; program bunu burada seçilen orana açarak gösterir. Kare alma da bu orana göre kaydedilir.")}
  </div>

  <div class="card">
    <h2>Kayıt kalitesi (ana akış)</h2>
    <p class="sub">Diske kaydedilen ve tek kamera görünümünde izlenen görüntü.</p>
    ${row("Çözünürlük", sel("c-res", resOpts, m.resolution),
      `Görüntüdeki ayrıntı miktarı (yüz, plaka). Bu kayıt cihazı ${p.mp} MP kamerayı tam çözünürlükte kaydedemez; <b>"N" modları</b> genişliği yarıya indirerek kaydeder. Listedeki ilk seçenek bu cihazın verebileceği en yüksek kalitedir.`, { changed: ch("resolution") })}
    ${row("Kare hızı", sel("c-fps", fpsOpts, m.fps),
      `Saniyedeki görüntü sayısı. <b>10</b> kesik kesik, <b>15</b> akıcı, <b>20+</b> çok akıcı görünür. Yüksek değer hızlı geçen kişi ve araçları yakalamayı kolaylaştırır.<br><b>Önemli:</b> Bu kayıt cihazının kodlama gücü sınırlıdır. Bu cihazda ölçülen sınır: <b>5M-N'de 4 kamerada her biri en fazla 10 fps</b> (Kodlama Geliştirme açıkken de aynı kaldı). 15 fps için 4M-N veya 1080P çözünürlük gerekir. Cihaz sınırı aşan değeri sessizce düşürür; program kaydettikten sonra size bildirir.`, { changed: ch("fps") })}
    ${row("Sıkıştırma", sel("c-comp", cm.compressions.map((c) => [c, c === "H.265" ? "H.265 (önerilen)" : c]), m.compression),
      "<b>H.265</b>, H.264'e göre aynı kalitede yaklaşık yarı yer kaplar. H.264 yalnızca çok eski cihazlarda açma sorunu varsa gerekir.", { changed: ch("compression") })}
    ${row("Bit hızı türü", sel("c-ctl", [["VBR", "Değişken – VBR (önerilen)"], ["CBR", "Sabit – CBR"]], m.control),
      "<b>Sabit:</b> sahne durgun da olsa her an aynı miktarda veri yazar, gece boşa disk harcar. <b>Değişken:</b> hareket olunca üst sınıra kadar çıkar, durgunken az yazar — aynı kalitede daha az disk.", { changed: ch("control") })}
    ${row("Kalite", sel("c-q", [[6, "6 – En iyi"], [5, "5"], [4, "4 – Orta"], [3, "3"], [2, "2"], [1, "1 – En düşük"]], m.quality),
      "Yalnızca <b>Değişken</b> bit hızında geçerlidir. Cihazın görüntüye ne kadar veri ayırmaya çalışacağını belirler; 6 en iyisidir.", { off: !vbr, changed: ch("quality") })}
    ${row(vbr ? "Bit hızı (üst sınır)" : "Bit hızı", sel("c-br", brOpts(cm.bitrate_min, cm.bitrate_max, m.bitrate), m.bitrate),
      `Saniyede yazılabilecek en fazla veri; netliği belirleyen en önemli ayar. Yüksek değer = daha net görüntü ama daha çok disk. Bu cihazda üst sınır <b>${cm.bitrate_max} kbps</b>.`, { changed: ch("bitrate") })}
    ${row("GOP (anahtar kare aralığı)", sel("c-gop", gopOpts, m.gop),
      `Kaç karede bir <b>tam kare</b> kaydedileceği; aradaki kareler yalnızca değişen kısmı saklar. Tam kareler en net karelerdir. <b>Kare hızına eşit</b> (saniyede bir tam kare) plaka ve yüz okumak için en iyisidir; daha büyük değer biraz yer kazandırır ama net kareler seyrekleşir (en fazla ${cm.gop_max}).`, { changed: ch("gop") })}
    ${row("Akıllı kodlama (H.265+)", sw("c-smart", f.smart),
      "Sahnenin değişmeyen kısımlarını tekrar tekrar kaydetmez. Durgun sahnelerde <b>disk kullanımını yarıya veya daha aza</b> indirir. Çok hızlı hareketlerde kalite hafifçe düşebilir; açıkken cihaz GOP ve bit hızı türünü kendisi ayarlayabilir.", { changed: f.smart !== cfg.smart })}
    ${cfg.ai == null ? "" : row("AI kodlama", sw("c-ai", f.ai),
      "İnsan ve araç olan bölgelere daha çok, arka plana daha az veri ayırır. Önemli ayrıntıları koruyarak yer kazandırır.", { changed: f.ai !== cfg.ai })}
    ${row("Ses kaydı", sw("c-audio", m.audio),
      "Kamerada mikrofon varsa sesi de kaydeder. Disk kullanımına etkisi çok azdır (~%2).", { changed: ch("audio") })}
  </div>

  <div class="card">
    <h2>Canlı izleme akışı (alt akış)</h2>
    <p class="sub">Çoklu görünümde ve telefondaki DMSS uygulamasında kullanılan küçük akış. Kayda etkisi yoktur.</p>
    ${row("Çözünürlük", sel("s-res", cs.resolutions.map((r) => [r, RES_LABEL[r] || r]), s.resolution),
      "<b>D1</b> çoklu görünümde ve telefonda daha net görüntü verir. İnternetiniz yavaşsa <b>CIF</b> daha akıcı olabilir.", { changed: ch("resolution", true) })}
    ${row("Kare hızı", sel("s-fps", subFps, s.fps), `Canlı izlemedeki akıcılık. Bu akışta en fazla ${cs.fps_max} fps.`, { changed: ch("fps", true) })}
    ${row("Bit hızı", sel("s-br", brOpts(cs.bitrate_min, cs.bitrate_max, s.bitrate), s.bitrate), "Canlı izlemedeki netlik. Yüksek değer daha net ama daha çok internet kullanır.", { changed: ch("bitrate", true) })}
  </div>

  ${camMotionHtml()}

  <div class="card">
    <h2>Tahmini disk kullanımı</h2>
    <p class="sub">Bu ayarlar bağlı ${n} kameranın hepsinde kullanılırsa. ${variable ? "Değişken bit hızı ve akıllı kodlamada gerçek kullanım sahneye göre çok daha az olur; aşağıdakiler en kötü durumdur." : ""}</p>
    <div class="estimate">
      <div><span>Kamera başına ${variable ? "en fazla" : ""}</span><b>${gbDay.toFixed(0)} GB / gün</b></div>
      <div><span>Disk</span><b>${cfg.disk_bytes ? (cfg.disk_bytes / 1e12).toFixed(2) + " TB" : "—"}</b></div>
      <div><span>Kayıt süresi ${variable ? "(en az)" : "(yaklaşık)"}</span><b>${days ? days.toFixed(1) + " gün" : "—"}</b></div>
    </div>
  </div>

  <div class="sticky-actions">
    <button class="btn" id="c-reset">${icon("refresh")}Önerilen ayarlara dön</button>
    <button class="btn" id="c-all">Tüm kameralara uygula</button>
    <button class="btn" id="c-undo"${camChanged() ? "" : " disabled"}>Değişiklikleri geri al</button>
    <button class="btn primary" id="c-save"${camChanged() && !CAM.busy ? "" : " disabled"}>${CAM.busy ? '<div class="spinner" style="width:16px;height:16px;border-width:2px"></div>' : icon("check")}Kaydet</button>
  </div>`;

  // önizleme yeniden oluşturulmasın diye yalnızca form kısmını değiştir
  const box = $("#cam-form");
  const keepPreview = !fresh && CAM.previewEl && box.contains(CAM.previewEl);
  const prev = keepPreview ? CAM.previewEl : null;
  const scroll = $("#page-settings .content").scrollTop;
  box.innerHTML = html;
  if (prev) $("#cam-preview").replaceWith(prev);
  else camStartPreview(info);
  $("#page-settings .content").scrollTop = scroll;
  camBind();
}

function escapeHtml(t) { const d = document.createElement("div"); d.textContent = t; return d.innerHTML; }

function camStartPreview(info) {
  camClosePreview();
  const el = $("#cam-preview");
  if (!el || !info) return;
  CAM.previewEl = el;
  el.innerHTML = `<div class="tile"><div class="vwrap"><video muted playsinline></video></div><div class="overlay"></div><div class="tag"></div><div class="label">${icon("cam")}<span></span></div></div>`;
  $(".label span", el).textContent = info.name;
  const tile = $(".tile", el);
  fitAspect(tile, () => displayAspect({ ...camInfo(CAM.ch), prefs: CAM.form.prefs }, $("video", tile)));
  CAM.player = new Player(info.id, tile);
  CAM.player.open({ start: null, speed: 1, single: false });
}

function camClosePreview() {
  if (CAM.player) { CAM.player.close(); CAM.player = null; }
}

function camBind() {
  const f = CAM.form;
  const on = (id, fn) => { const e = $("#" + id); if (e) e.addEventListener("change", () => { fn(e); camRender(); }); };
  on("c-mp", (e) => {
    f.prefs.mp = +e.value;
    const opts = camResOptions(f.prefs.mp, CAM.cfg.caps);
    if (!opts.includes(f.main.resolution)) f.main.resolution = opts[0];
    camSavePrefs();
  });
  on("c-aspect", (e) => { f.prefs.aspect = e.value; camSavePrefs(); });
  on("c-res", (e) => (f.main.resolution = e.value));
  on("c-fps", (e) => (f.main.fps = +e.value));
  on("c-comp", (e) => (f.main.compression = e.value));
  on("c-ctl", (e) => (f.main.control = e.value));
  on("c-q", (e) => (f.main.quality = +e.value));
  on("c-br", (e) => (f.main.bitrate = +e.value));
  on("c-gop", (e) => (f.main.gop = +e.value));
  on("c-smart", (e) => (f.smart = e.checked));
  on("c-ai", (e) => (f.ai = e.checked));
  on("c-audio", (e) => (f.main.audio = e.checked));
  on("s-res", (e) => (f.sub.resolution = e.value));
  on("s-fps", (e) => (f.sub.fps = +e.value));
  on("s-br", (e) => (f.sub.bitrate = +e.value));
  $("#c-save").onclick = () => camSave();
  camMotionBind();
  const enhEl = $("#c-enh");
  if (enhEl) enhEl.onchange = async () => {
    const on = enhEl.checked;
    if (!confirm(`Kodlama Geliştirme ${on ? "açılsın" : "kapatılsın"} mı?

Kayıt cihazı yeniden başlayacak; yaklaşık 1–2 dakika kayıt ve görüntü olmaz.`)) { enhEl.checked = !on; return; }
    try {
      await api("/api/encode-enhance", { method: "POST", body: { enable: on } });
      CAM.cfg.enhance = on;
      toast("Kayıt cihazı yeniden başlıyor. Bağlantı kendiliğinden yeniden kurulacak.", { time: 8000 });
      camRender();
    } catch (e) { enhEl.checked = !on; toast(e.message, { err: true }); }
  };
  $("#c-undo").onclick = () => { CAM.form = { ...camFormFrom(CAM.cfg), prefs: f.prefs }; camRender(); };
  $("#c-reset").onclick = () => {
    if (!confirm("Bu kamera önerilen ayarlara döndürülsün mü?\n\nEn yüksek çözünürlük, önerilen kare hızı (5M-N'de 10 fps), değişken bit hızı (kalite 6, en yüksek bit hızı), GOP = fps, akıllı kodlama açık, canlı akış D1. Görüntü oranı otomatiğe döner.")) return;
    CAM.form = { ...camRecommended(CAM.cfg, f.prefs.mp), prefs: { ...f.prefs, aspect: "auto" } };
    camSavePrefs();
    camSave();
  };
  $("#c-all").onclick = () => camApplyAll();
}

async function camSavePrefs() {
  const ch = CAM.ch, prefs = { ...CAM.form.prefs };
  try {
    await api(`/api/camera/${ch}`, { method: "POST", body: { prefs } });
    const c = camInfo(ch);
    if (c) c.prefs = prefs;
    CAM.previewEl?.querySelector(".tile")?._fit?.();
    S.chanKey = null;                         // canlı görünüm yeni oranla yeniden kurulsun
  } catch (e) { toast(e.message, { err: true }); }
}

function camDevicePayload(f) {
  return { main: { ...f.main }, sub: { resolution: f.sub.resolution, fps: f.sub.fps, bitrate: f.sub.bitrate }, smart: f.smart, ai: f.ai };
}

async function camSave() {
  if (CAM.busy) return;
  CAM.busy = true;
  camRender();
  const ch = CAM.ch;
  try {
    const r = await api(`/api/camera/${ch}`, { method: "POST", body: { device: camDevicePayload(CAM.form) } });
    CAM.cfg = { ...CAM.cfg, ...r };
    CAM.warnings = r.warnings || [];
    CAM.form = { ...camFormFrom(CAM.cfg), prefs: CAM.form.prefs };
    toast(CAM.warnings.length ? "Kaydedildi, bazı değerler cihaz tarafından ayarlandı" : "Ayarlar cihaza kaydedildi");
    S.chanKey = null;
    setTimeout(() => CAM.player && CAM.player.open({ start: null, speed: 1, single: false }), 1500);
  } catch (e) { toast("Kaydedilemedi: " + e.message, { err: true }); }
  CAM.busy = false;
  if (CAM.ch === ch) camRender();
}

async function camApplyAll() {
  const chans = camChannels().map((c) => c.id);
  if (chans.length < 2) return toast("Başka kamera yok");
  if (!confirm([
    `Bu sayfadaki kayıt ayarları ${chans.length} kameranın hepsine uygulansın mı?`, "",
    "• Uygulananlar: çözünürlük, kare hızı, sıkıştırma, bit hızı, kalite, GOP, ses, canlı akış, megapiksel ve görüntü oranı.",
    "• Akıllı kodlama ve AI kodlama her kameranın kendi ayarında kalır.",
    "• Kayıt cihazının gücü yetmezse kare hızı kameralar arasında eşit paylaştırılır.", "",
    "Yaklaşık 1 dakika sürer."].join("\n"))) return;
  if (camChanged()) await camSave();
  const f = CAM.form;
  const device = { main: { ...f.main }, sub: { resolution: f.sub.resolution, fps: f.sub.fps, bitrate: f.sub.bitrate } };
  const ov = document.createElement("div");
  ov.style.cssText = "position:fixed;inset:0;background:rgba(5,6,9,.72);backdrop-filter:blur(3px);z-index:60;display:grid;place-items:center";
  ov.innerHTML = `<div class="card" style="min-width:360px;text-align:center"><div class="spinner" style="margin:6px auto 14px"></div>
    <h2>Kameralar ayarlanıyor</h2><p class="sub" id="apply-st" style="margin:6px 0 0">Başlıyor…</p>
    <p class="sub" style="margin:10px 0 0;font-size:12px">Bu sırada kayıt devam eder; görüntü birkaç saniye takılabilir.</p></div>`;
  document.body.appendChild(ov);
  const poll = setInterval(async () => { try { $("#apply-st").textContent = (await api("/api/cameras/apply-status")).status || "…"; } catch (e) { } }, 1000);
  try {
    const r = await api("/api/cameras/apply", { method: "POST", body: { channels: chans, device, prefs: { ...f.prefs } } });
    const lines = camChannels().map((c) => { const x = r.results[c.id]; return x ? `${c.name}: ${RES_LABEL[x.resolution] || x.resolution}, ${x.fps} fps, GOP ${x.gop}, ${x.control} ${x.bitrate} kbps` : null; }).filter(Boolean);
    CAM.warnings = [...r.notes, ...lines];
    for (const c of camChannels()) c.prefs = { ...f.prefs };
    toast(r.notes.length ? "Uygulandı, kare hızı eşit paylaştırıldı" : "Tüm kameralar ayarlandı");
  } catch (e) { toast("Uygulanamadı: " + e.message, { err: true }); }
  clearInterval(poll);
  ov.remove();
  S.chanKey = null;
  const keep = CAM.warnings;
  await camLoad(CAM.ch);                 // cihazdaki güncel değerleri göster
  CAM.warnings = keep;
  camRender();
}

camTabs();


// ------------------------------------------------------------------ hareket algılama ve uyarılar
const SENS_LEVELS = [[20, "Çok düşük"], [40, "Düşük"], [60, "Orta"], [80, "Yüksek"], [100, "Çok yüksek"]];

function camMotionChanged() {
  const a = CAM.motion, b = CAM.cfg?.motion;
  if (!a || !b) return false;
  return ["md", "md_sens", "smd", "human", "vehicle", "smd_sens", "light", "siren", "beep", "log", "record"].some((k) => String(a[k]) !== String(b[k]));
}

function camMotionHtml() {
  const m = CAM.motion, o = CAM.cfg?.motion;
  if (!m) return `<div class="card"><h2>Hareket algılama ve uyarılar</h2><p class="sub" style="margin:0">Bu kameranın hareket ayarları okunamadı.</p></div>`;
  const ch = (k) => o && String(m[k]) !== String(o[k]);
  const sensLabel = (v) => (SENS_LEVELS.find(([x]) => v <= x) || SENS_LEVELS[4])[1];
  return `
  <div class="card">
    <h2>Hareket algılama ve uyarılar</h2>
    <p class="sub">Bu ayarlar görüntü kalitesini etkilemez; kendi düğmesiyle kaydedilir ve kayıt durmaz.</p>
    ${row("Hareket algılama", sw("m-md", m.md),
      "Görüntüde hareket olunca cihaz o anı <b>olay</b> olarak işaretler. Disk sekmesindeki olay listesi, zaman çizelgesindeki işaretler ve olay logları bunun sayesinde oluşur. 7/24 kayıt bundan bağımsız olarak devam eder.", { changed: ch("md") })}
    ${row("Hareket hassasiyeti", `<div style="display:flex;align-items:center;gap:12px"><input type="range" id="m-sens" min="1" max="100" value="${m.md_sens}" style="width:220px;accent-color:var(--accent)" ${m.md ? "" : "disabled"}><b id="m-sens-v">${m.md_sens} – ${sensLabel(m.md_sens)}</b></div>`,
      "Yüksek değer küçük hareketleri de yakalar ama rüzgârda sallanan ağaç, far ışığı, yağmur gibi gereksiz olaylar artar. Düşük değer yalnızca belirgin hareketleri yakalar. Sokak kameraları için <b>50–70</b> genelde iyidir.", { off: !m.md, changed: ch("md_sens") })}
    ${m.smd_supported ? `
    ${row("Akıllı hareket (insan / araç)", sw("m-smd", m.smd),
      "Cihazın yapay zekâsı hareketin <b>insan mı araç mı</b> olduğunu ayırır; yaprak, gölge, böcek gibi gereksiz olayları büyük ölçüde eler. Olaylar listede İnsan / Araç olarak ayrılır. Hareket algılama açıkken çalışır.", { off: !m.md, changed: ch("smd") })}
    ${row("Algılanacak nesneler", `<div class="chips"><label class="chip"><input type="checkbox" id="m-human" ${m.human ? "checked" : ""}> İnsan</label><label class="chip"><input type="checkbox" id="m-vehicle" ${m.vehicle ? "checked" : ""}> Araç</label></div>`,
      "Örneğin yalnızca kişileri takip etmek istiyorsanız Araç'ı kapatabilirsiniz.", { off: !m.md || !m.smd, changed: ch("human") || ch("vehicle") })}
    ${row("Akıllı hassasiyet", sel("m-smdsens", [["Low", "Düşük"], ["Middle", "Orta (önerilen)"], ["High", "Yüksek"]], m.smd_sens),
      "Yüksek: uzaktaki ve küçük görünen insan/araçları da yakalar, yanlış algılama biraz artar. Düşük: yalnızca net görünenleri yakalar.", { off: !m.md || !m.smd, changed: ch("smd_sens") })}` : ""}
    ${row("Olayları logla", sw("m-log", m.log), "Her olay cihazın sistem loguna da yazılır (Kullanıcılar › Sistem olayları'nda görünür).", { off: !m.md, changed: ch("log") })}
    ${row("Uyarı ışığı (kırmızı-mavi çakar)", sw("m-light", m.light),
      "Hareket algılanınca kameranın üzerindeki kırmızı-mavi ışık polis çakarı gibi yanıp söner. Hırsızı caydırır ama sokaktan her geçişte yanacağı için komşuları rahatsız edebilir. <b>Önerilen: kapalı.</b>", { off: !m.md, changed: ch("light") })}
    ${row("Siren", sw("m-siren", m.siren),
      "Hareket algılanınca kameranın hoparlöründen alarm sesi çalar. <b>Önerilen: kapalı.</b>", { off: !m.md, changed: ch("siren") })}
    ${row("Kayıt cihazı bipi", sw("m-beep", m.beep), "Hareket algılanınca kayıt cihazının kendisi bip sesi çıkarır.", { off: !m.md, changed: ch("beep") })}
    <div class="form-actions">
      <button class="btn" id="m-undo" ${camMotionChanged() ? "" : "disabled"}>Değişiklikleri geri al</button>
      <button class="btn primary" id="m-save" ${camMotionChanged() && !CAM.mbusy ? "" : "disabled"}>${icon("check")}Hareket ayarlarını kaydet</button>
    </div>
  </div>`;
}

function camMotionBind() {
  const m = CAM.motion;
  if (!m) return;
  const on = (id, fn) => { const e = $("#" + id); if (e) e.addEventListener("change", () => { fn(e); camRender(); }); };
  on("m-md", (e) => (m.md = e.checked));
  on("m-smd", (e) => (m.smd = e.checked));
  on("m-human", (e) => (m.human = e.checked));
  on("m-vehicle", (e) => (m.vehicle = e.checked));
  on("m-smdsens", (e) => (m.smd_sens = e.value));
  on("m-log", (e) => (m.log = e.checked));
  on("m-light", (e) => (m.light = e.checked));
  on("m-siren", (e) => (m.siren = e.checked));
  on("m-beep", (e) => (m.beep = e.checked));
  const r = $("#m-sens");
  if (r) {
    r.addEventListener("input", () => { m.md_sens = +r.value; $("#m-sens-v").textContent = `${r.value} – ${(SENS_LEVELS.find(([x]) => +r.value <= x) || SENS_LEVELS[4])[1]}`; });
    r.addEventListener("change", () => camRender());
  }
  $("#m-undo").onclick = () => { CAM.motion = { ...CAM.cfg.motion }; camRender(); };
  $("#m-save").onclick = async () => {
    if ((m.light || m.siren) && !(CAM.cfg.motion.light || CAM.cfg.motion.siren) &&
        !confirm("Uyarı ışığı ve/veya siren açılacak. Hareket algılandığında (sokaktan geçen her araç ve kişi dahil) kamera çakar ve/veya ses çıkaracak. Devam edilsin mi?")) return;
    CAM.mbusy = true; camRender();
    const keys = ["md", "md_sens", "smd", "human", "vehicle", "smd_sens", "light", "siren", "beep", "log"];
    const body = {};
    for (const k of keys) if (String(m[k]) !== String(CAM.cfg.motion[k])) body[k] = m[k];
    try {
      const r = await api(`/api/camera/${CAM.ch}`, { method: "POST", body: { motion: body } });
      CAM.cfg.motion = r.motion; CAM.motion = { ...r.motion };
      CAM.warnings = r.warnings || [];
      toast(CAM.warnings.length ? "Kaydedildi, bazı ayarlar cihaz tarafından değiştirildi" : "Hareket ayarları kaydedildi");
    } catch (e) { toast("Kaydedilemedi: " + e.message, { err: true }); }
    CAM.mbusy = false; camRender();
  };
}
