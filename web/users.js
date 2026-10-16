"use strict";
/* Ayarlar › Kullanıcılar sekmesi: cihaz kullanıcıları, yetkiler ve kullanıcı logları */

const AUTH = {
  AuthUserMag: ["Kullanıcı yönetimi", "Kullanıcı ekleme, silme, düzenleme ve logları görme"],
  AuthSysCfg: ["Sistem ayarları", "Kamera, kayıt kalitesi ve cihaz ayarlarını değiştirme"],
  AuthStoreCfg: ["Depolama ayarları", "Disk ve kayıt planı ayarları"],
  AuthEventCfg: ["Olay ayarları", "Hareket algılama ve alarm ayarları"],
  AuthNetCfg: ["Ağ ayarları", "IP, P2P ve port ayarları"],
  AuthBackup: ["Yedekleme / indirme", "Kayıtları dışarı aktarma ve indirme"],
  AuthManuCtr: ["Manuel kontrol", "Kaydı ve alarmı elle başlatma, kamera döndürme"],
  AuthSysInfo: ["Sistem bilgisi", "Cihaz bilgilerini ve durumunu görme"],
  AuthRmtDevice: ["Uzak cihazlar", "IP kamera ekleme ve yönetme"],
  AuthSecurity: ["Güvenlik", "Güvenlik ve şifre kuralları"],
  AuthMaintence: ["Bakım", "Yazılım güncelleme, yeniden başlatma, fabrika ayarı"],
};
const U = { data: null };
const authLabel = (a) => {
  if (AUTH[a]) return AUTH[a][0];
  const m = a.match(/^(Monitor|Replay)_(\d+)$/);
  if (m) return `${m[1] === "Monitor" ? "Canlı" : "Kayıt"} ${+m[2]}`;
  return a;
};
const camName = (n) => (S.state?.all_channels || []).find((c) => c.id === n)?.name || `Kanal ${n}`;

function usersOpen() { usersLoad(); }

async function usersLoad() {
  const box = $("#u-list");
  box.innerHTML = `<div style="display:flex;gap:12px;align-items:center;padding:10px"><div class="spinner"></div>Kullanıcılar cihazdan okunuyor…</div>`;
  try { U.data = await api("/api/users"); usersRender(); }
  catch (e) { box.innerHTML = ""; box.textContent = "Kullanıcılar okunamadı: " + e.message; }
}

function summarizeAuth(list) {
  list = list.filter((a) => a !== "Monitor" && a !== "Replay");
  const general = list.filter((a) => !/^(Monitor|Replay)_/.test(a)).map(authLabel);
  const mon = list.filter((a) => a.startsWith("Monitor_")).length;
  const rep = list.filter((a) => a.startsWith("Replay_")).length;
  const n = U.data?.channels || 8;
  const ch = [];
  if (mon) ch.push(`Canlı izleme: ${mon >= n ? "tüm kanallar" : mon + " kanal"}`);
  if (rep) ch.push(`Kayıt izleme: ${rep >= n ? "tüm kanallar" : rep + " kanal"}`);
  return [...ch, ...general];
}

function usersRender() {
  const box = $("#u-list");
  box.innerHTML = "";
  const me = S.state?.device?.user;
  for (const u of U.data.users) {
    const el = document.createElement("div");
    el.className = "u-row";
    el.innerHTML = `<div class="avatar"></div>
      <div><div class="u-name"><span class="nm"></span>
        <span class="pill ${u.group === "admin" ? "admin" : ""}">${u.group === "admin" ? "Yönetici" : "Kullanıcı"}</span>
        ${u.sessions ? `<span class="pill on">● ${u.sessions} açık oturum</span>` : ""}
        ${u.name === me ? `<span class="pill">siz</span>` : ""}</div>
        <div class="u-meta"></div><div class="auth-chips"></div></div>
      <div class="dl-actions"></div>`;
    $(".avatar", el).textContent = (u.name || "?")[0];
    $(".nm", el).textContent = u.name;
    $(".u-meta", el).textContent = [u.memo, u.password_changed ? `Şifre değişimi: ${u.password_changed}` : ""].filter(Boolean).join(" · ");
    const chips = $(".auth-chips", el);
    for (const t of summarizeAuth(u.authorities)) { const s = document.createElement("span"); s.textContent = t; chips.appendChild(s); }
    const acts = $(".dl-actions", el);
    const b = (label, ic, fn, cls = "") => { const x = document.createElement("button"); x.className = `btn small ${cls}`; x.innerHTML = `${icon(ic)}${label}`; x.onclick = (e) => { e.stopPropagation(); fn(); }; acts.appendChild(x); };
    b("Geçmiş", "history", () => logsOpen(u.name, u.name));
    b("Düzenle", "settings", () => userEdit(u));
    if (!u.reserved && u.name !== me) b("", "x", () => userDelete(u), "icon ghost danger");
    el.onclick = () => logsOpen(u.name, u.name);
    box.appendChild(el);
  }
}

// ------------------------------------------------------------------ modal
function modal(html) {
  $("#modal-box").innerHTML = html;
  $("#modal").hidden = false;
  return $("#modal-box");
}
function modalClose() { $("#modal").hidden = true; $("#modal-box").innerHTML = ""; }
$("#modal").addEventListener("mousedown", (e) => { if (e.target.id === "modal") modalClose(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#modal").hidden) modalClose(); });

// ------------------------------------------------------------------ ekle / düzenle / sil
function userEdit(u) {
  const isNew = !u;
  u = u || { name: "", group: "user", memo: "", authorities: [], reserved: false };
  const groups = U.data.groups;
  const box = modal(`
    <div class="modal-head"><h2>${isNew ? "Yeni kullanıcı" : "Kullanıcıyı düzenle"}</h2><button class="btn icon ghost" data-x>${icon("x")}</button></div>
    <div class="modal-body">
      <div class="form">
        <label>Kullanıcı adı</label><input type="text" id="ue-name" maxlength="31" ${isNew ? "" : "readonly"} autocomplete="off" spellcheck="false">
        <label>Grup</label><select id="ue-group" ${u.reserved ? "disabled" : ""}>${groups.map((g) => `<option value="${g.name}">${g.name === "admin" ? "Yönetici (admin)" : g.name === "user" ? "Kullanıcı (user)" : g.name}</option>`).join("")}</select>
        <label>Açıklama</label><input type="text" id="ue-memo" maxlength="31" placeholder="Ör. Gece bekçisi">
        <label>${isNew ? "Şifre" : "Yeni şifre"}</label><input type="password" id="ue-pass" autocomplete="new-password" placeholder="${isNew ? "En az 8 karakter, harf ve rakam" : "Değiştirmeyecekseniz boş bırakın"}">
        <label>Şifre (tekrar)</label><input type="password" id="ue-pass2" autocomplete="new-password">
        ${isNew ? "" : `<label>Mevcut şifre</label><div><input type="password" id="ue-old" autocomplete="off" placeholder="Biliyorsanız (isteğe bağlı)">
          <div class="hint">Bu kayıt cihazı, yöneticinin başka bir kullanıcının şifresini doğrudan değiştirmesine izin vermiyor. Mevcut şifreyi biliyorsanız girin. Bilmiyorsanız kullanıcı aynı ad, grup ve açıklamayla yeni şifreyle yeniden oluşturulur.</div></div>`}
      </div>
      <p class="sub" style="margin:18px 0 8px">Bu gruptaki kullanıcının yetkileri</p>
      <div class="auth-chips" id="ue-rights"></div>
      <p class="hint" style="margin-top:10px">Bu kayıt cihazı yetkileri kullanıcıya göre değil <b>gruba göre</b> verir; kanal kanal yetki seçmeyi ve yeni grup oluşturmayı desteklemiyor. <b>Yönetici</b> her şeyi yapabilir. <b>Kullanıcı</b> tüm kameraları canlı ve kayıttan izleyebilir, ayarları ve kullanıcıları göremez.</p>
    </div>
    <div class="modal-foot"><button class="btn ghost" data-x>Vazgeç</button><button class="btn primary" id="ue-save">${icon("check")}${isNew ? "Kullanıcıyı ekle" : "Kaydet"}</button></div>`);
  $("#ue-name").value = u.name;
  $("#ue-memo").value = u.memo || "";
  $("#ue-group").value = u.group;
  const rights = () => {
    const g = groups.find((x) => x.name === $("#ue-group").value) || { authorities: [] };
    const box2 = $("#ue-rights");
    box2.innerHTML = "";
    for (const t of summarizeAuth(g.authorities)) { const s = document.createElement("span"); s.textContent = t; box2.appendChild(s); }
  };
  rights();
  $("#ue-group").onchange = rights;
  $$("[data-x]", box).forEach((b) => (b.onclick = modalClose));
  $("#ue-save").onclick = async () => {
    const name = $("#ue-name").value.trim(), p1 = $("#ue-pass").value, p2 = $("#ue-pass2").value;
    const old = $("#ue-old")?.value || "";
    if (!name || !/^[A-Za-z0-9_.@-]+$/.test(name)) return toast("Kullanıcı adı yalnızca harf, rakam ve _ . @ - içerebilir", { err: true });
    if (isNew && !p1) return toast("Şifre girin", { err: true });
    if (p1 && p1.length < 8) return toast("Şifre en az 8 karakter olmalı", { err: true });
    if (p1 !== p2) return toast("Şifreler aynı değil", { err: true });
    if (!isNew && p1 && !old && name !== S.state?.device?.user &&
        !confirm(`"${name}" kullanıcısının şifresi, kullanıcı silinip aynı ad, grup ve açıklamayla yeniden oluşturularak değiştirilecek.

O an bu kullanıcıyla açık olan oturumlar (DMSS vb.) kapanır. Devam edilsin mi?`)) return;
    const body = { group: $("#ue-group").value, memo: $("#ue-memo").value.trim() };
    if (p1) { body.password = p1; if (old) body.old_password = old; }
    const btn = $("#ue-save");
    btn.disabled = true;
    try {
      U.data = isNew ? await api("/api/users", { method: "POST", body: { ...body, name } })
                     : await api(`/api/users/${encodeURIComponent(name)}`, { method: "PUT", body });
      modalClose();
      usersRender();
      toast(isNew ? `"${name}" eklendi` : p1 ? "Kullanıcı ve şifre güncellendi" : "Kullanıcı güncellendi");
    } catch (e) { btn.disabled = false; toast(e.message, { err: true, time: 9000 }); }
  };
}

async function userDelete(u) {
  if (!confirm(`"${u.name}" kullanıcısı cihazdan silinsin mi?\n\nBu kullanıcı artık kayıt cihazına, DMSS'e ve bu programa giriş yapamaz.`)) return;
  try { U.data = await api(`/api/users/${encodeURIComponent(u.name)}`, { method: "DELETE" }); usersRender(); toast("Kullanıcı silindi"); }
  catch (e) { toast(e.message, { err: true, time: 8000 }); }
}

// ------------------------------------------------------------------ loglar
const LOG_CAT = [
  ["motion", /hareket|algılama|alarm|motion/i, "live", "Hareket algılama"],
  ["login", /giriş|çıkış|oturum|login|logout/i, "eye", "Oturum"],
  ["play", /oynat|kayıt ara|duraklat|hızlı|yavaş|geri oynat|playback|search/i, "play", "Kayıt izleme"],
  ["backup", /yedek|backup|indir/i, "download", "Yedekleme / indirme"],
  ["config", /yapılandırma|ayar|kaydet|config/i, "settings", "Ayar değişikliği"],
  ["system", /kapatma|başlat|güncelle|reboot|shutdown|upgrade/i, "refresh", "Sistem"],
  ["disk", /disk|s\.m\.a\.r\.t|hdd/i, "folder", "Disk"],
];
const logCat = (t) => LOG_CAT.find(([, re]) => re.test(t || "")) || ["other", null, "file", "Diğer"];

function logSource(detail) {
  if (/Yerel Oturum/i.test(detail)) return "cihazın kendi ekranından";
  const ip = (detail.match(/IP Adresi\s*:\s*([\d.]+)/i) || [])[1];
  if (ip === "127.0.0.1") return "internet üzerinden (P2P: DMSS veya bu program)";
  if (ip) return `ağdan (${ip})`;
  return "";
}

function logDetail(type, detail) {
  const skip = /^(KullanıcıAdı|Kullanıcı Adı|Grup|Tip|Cihaz Türü|Zaman|IP Adresi)\s*:/i;
  return detail.split("\n").map((l) => l.trim()).filter((l) => l && !skip.test(l))
    .map((l) => l.replace(/-->/g, " → ").replace(/\bHyr\b/g, "Hayır").replace(/^:/, "Aralık:").replace(/---/g, " – ").replace(/ R$/, ""))
    .slice(0, 6).join("\n");
}

async function logsOpen(user, title) {
  const box = modal(`
    <div class="modal-head"><h2></h2>
      <div class="log-filter"><select id="lg-days" class="date-input" style="height:32px">
        <option value="1">Son 24 saat</option><option value="3">Son 3 gün</option><option value="7">Son 7 gün</option></select>
        <label class="switch" style="font-size:12.5px"><input type="checkbox" id="lg-noise"> Tek tek göster</label></div>
      <button class="btn icon ghost" data-x>${icon("x")}</button></div>
    <div class="modal-body" id="lg-body"></div>`);
  $(".modal-head h2", box).textContent = user === "" ? "Sistem olayları" : `${title} – yaptıkları`;
  $("[data-x]", box).onclick = modalClose;
  let data = null, gen = 0;
  // Cihaz logları eskiden yeniye verir ve yavaştır: dönem 1 saatlik dilimlere bölünür, en yeniden
  // başlanarak her dilim geldikçe ekrana eklenir.
  const load = async () => {
    const my = ++gen;
    const days = +$("#lg-days").value;
    const end = devNow(), start = end - days * DAY;
    data = { items: [] };
    $("#lg-body").innerHTML = "";
    const status = document.createElement("div");
    status.style.cssText = "display:flex;gap:10px;align-items:center;color:var(--muted);font-size:12.5px;margin-bottom:10px";
    let t = end;
    while (t > start && my === gen && !$("#modal").hidden) {
      const a = Math.max(start, t - 3600000);
      status.innerHTML = `<div class="spinner" style="width:16px;height:16px;border-width:2px"></div><span>${fmtTime(a)} ${fmtDate(a)} öncesi okunuyor…</span>`;
      $("#lg-body").prepend(status);
      try {
        const r = await api(`/api/logs?user=${encodeURIComponent(user)}&start=${fmtISO(a)}&end=${fmtISO(t)}`);
        if (my !== gen) return;
        data.items = r.items.concat(data.items);
        draw();
      } catch (e) { status.textContent = "Loglar okunamadı: " + e.message; return; }
      t = a;
    }
    if (my === gen) { status.remove(); draw(true); }
  };
  const draw = (done = false) => {
    const body = $("#lg-body");
    const st = body.firstElementChild && !body.firstElementChild.classList.contains("log-summary") && body.firstElementChild.querySelector?.(".spinner") ? body.firstElementChild : null;
    const items = data.items.slice().reverse();     // yeniden eskiye
    const showNoise = $("#lg-noise").checked;
    const counts = {};
    for (const it of items) { const c = logCat(it.type)[0]; counts[c] = (counts[c] || 0) + 1; }
    // art arda gelen oturum açma/kapamaları tek satırda topla
    const rows = [];
    for (const it of items) {
      const cat = logCat(it.type)[0];
      const last = rows[rows.length - 1];
      if (cat === "motion" && !showNoise) {
        const ch = (it.detail.match(/Kanal:\s*<?(\d+)/) || [])[1] || "?";
        if (last && last.motion && last.day === it.time.slice(0, 10) && Math.abs(parseT(last.from) - parseT(it.time)) < 10 * 60000
            && parseT(last.to) - parseT(it.time) < 3600000) {
          last.n++; last.from = it.time; last.chans.add(ch); continue;
        }
        rows.push({ motion: true, n: 1, to: it.time, from: it.time, day: it.time.slice(0, 10), chans: new Set([ch]) });
        continue;
      }
      if (cat === "login" && !showNoise && last && last.group && last.day === it.time.slice(0, 10)) {
        last.n++; last.from = it.time; last.sources.add(logSource(it.detail)); continue;
      }
      if (cat === "login" && !showNoise) rows.push({ group: true, n: 1, to: it.time, from: it.time, day: it.time.slice(0, 10), sources: new Set([logSource(it.detail)]) });
      else rows.push({ it, day: it.time.slice(0, 10) });
    }
    const label = { motion: "Hareket algılama", login: "Oturum", play: "Kayıt izleme", backup: "Yedekleme", config: "Ayar değişikliği", system: "Sistem", disk: "Disk", other: "Diğer" };
    body.innerHTML = `<div class="log-summary">${Object.entries(counts).map(([k, v]) => `<div><b>${v}</b>${label[k]}</div>`).join("") || `<div>${done ? "Bu dönemde kayıt yok" : "Henüz kayıt yok"}</div>`}</div>`;
    if (st) body.prepend(st);
    let day = null;
    for (const r of rows.slice(0, 400)) {
      if (r.day !== day) {
        day = r.day;
        const h = document.createElement("div");
        h.className = "log-day";
        const d = parseT(day + "T00:00:00");
        h.textContent = dayKey(devNow()) === day ? "Bugün" : dayKey(devNow() - DAY) === day ? "Dün" : fmtDate(d);
        body.appendChild(h);
      }
      const el = document.createElement("div");
      if (r.motion) {
        el.className = "log-item c-play";
        el.innerHTML = `<div class="t"></div><div class="log-ico">${icon("live")}</div><div><div class="ttl"></div><div class="det"></div></div>`;
        $(".t", el).textContent = r.to.slice(11, 16);
        $(".ttl", el).textContent = r.n === 1 ? "Hareket algılandı" : `${r.n} hareket olayı`;
        $(".det", el).textContent = `${r.n > 1 ? `${r.from.slice(11, 16)} – ${r.to.slice(11, 16)} arası · ` : ""}Kanal ${[...r.chans].sort().join(", ")} · ayrıntı için Disk › Kayıt dosyaları ve olaylar`;
      } else if (r.group) {
        el.className = "log-item c-login";
        el.innerHTML = `<div class="t"></div><div class="log-ico">${icon("eye")}</div><div><div class="ttl"></div><div class="det"></div></div>`;
        $(".t", el).textContent = r.to.slice(11, 16);
        $(".ttl", el).textContent = r.n === 1 ? "Oturum açtı / kapattı" : `${r.n} kez oturum açıp kapattı`;
        $(".det", el).textContent = [r.n > 1 ? `${r.from.slice(11, 16)} – ${r.to.slice(11, 16)} arası` : "", [...r.sources].filter(Boolean).join(", ")].filter(Boolean).join(" · ");
      } else {
        const it = r.it, [cat, , ic] = logCat(it.type);
        el.className = `log-item c-${cat}`;
        el.innerHTML = `<div class="t"></div><div class="log-ico">${icon(ic)}</div><div><div class="ttl"></div><div class="det"></div></div>`;
        $(".t", el).textContent = it.time.slice(11, 16);
        const src = logSource(it.detail);
        $(".ttl", el).textContent = it.type.replace(/\.$/, "") + (user === "" && it.user && it.user !== "default" ? ` (${it.user})` : "");
        $(".det", el).textContent = [logDetail(it.type, it.detail), src ? `Kaynak: ${src}` : ""].filter(Boolean).join("\n");
      }
      body.appendChild(el);
    }
    if (rows.length > 400) { const m = document.createElement("p"); m.className = "sub"; m.textContent = `İlk 400 satır gösteriliyor (toplam ${rows.length}). Daha kısa bir dönem seçin.`; body.appendChild(m); }
    if (!rows.length && done) body.insertAdjacentHTML("beforeend", `<p class="sub">Bu dönemde bu kullanıcıya ait kayıt yok.</p>`);
  };
  $("#lg-days").onchange = load;
  $("#lg-noise").onchange = () => data && draw();
  load();
}

$("#u-add").onclick = () => userEdit(null);
$("#u-syslog").onclick = () => logsOpen("", "Sistem");
