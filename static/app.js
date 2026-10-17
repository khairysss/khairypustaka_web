/* KhairyPustaka Web - SPA vanilla JS, hash routing.
   Backend mungkin belum jadi: semua pemanggilan API defensif (cek field sebelum dipakai). */
"use strict";

/* ---------- util ---------- */
const $ = (sel, el) => (el || document).querySelector(sel);
const $$ = (sel, el) => Array.from((el || document).querySelectorAll(sel));

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function tic(n){return '<svg class="tic" aria-hidden="true"><use href="#i-'+n+'"></use></svg>';}
function richText(s) {
  // Sanitasi ringan: escape SEMUA lalu kembalikan tag aman milik panduan KB
  // (<b>, <i>, <u>, <code>, <br>). KB server-side tidak pernah berisi atribut/
  // script, tapi pertahanan berlapis bila KB suatu saat bisa diedit.
  return esc(s).replace(/&lt;(\/?)(b|i|u|code|br)\s*&gt;/gi, "<$1$2>");
}
/* Markdown subset yang AMAN (temuan user 2026-10-13: jawaban AI tampil mentah
   sebagai **teks**). Escape HTML DULU via esc(), baru konversi: **bold**,
   *italic*, `code`, header #, list bernomor/bullet, dan line-break.
   Jangan pernah innerHTML dari teks mentah tanpa escape. */
function mdRapi(s) {
  let t = esc(String(s == null ? "" : s));
  t = t.replace(/^#{1,6}\s+(.+)$/gm, "<b>$1</b>");
  // bold: isi boleh mengandung * tunggal (mis. **pakai *Anura* ini?**),
  // tapi tidak boleh ada ** atau newline di dalamnya (cegah pairing nyasar)
  t = t.replace(/\*\*((?:[^*\n]|\*(?!\*))+?)\*\*/g, "<b>$1</b>");
  t = t.replace(/(^|[\s(>])\*([^*\n]+?)\*/g, "$1<i>$2</i>");
  t = t.replace(/`([^`\n]+?)`/g, "<code>$1</code>");
  const baris = t.split("\n");
  let html = "", diOL = false, diUL = false;
  const tutup = () => {
    if (diOL) { html += "</ol>"; diOL = false; }
    if (diUL) { html += "</ul>"; diUL = false; }
  };
  for (const b of baris) {
    let m;
    if ((m = b.match(/^\s*\d+[.)]\s+(.*)$/))) {
      if (!diOL) { tutup(); html += "<ol>"; diOL = true; }
      html += "<li>" + m[1] + "</li>";
    } else if ((m = b.match(/^\s*[-*•]\s+(.*)$/))) {
      if (!diUL) { tutup(); html += "<ul>"; diUL = true; }
      html += "<li>" + m[1] + "</li>";
    } else {
      tutup();
      if (b.trim()) html += b + "<br>";
    }
  }
  tutup();
  return html.replace(/(<br>)+$/, "");
}

/* localStorage aman (ada browser HP yang memblokirnya) */
const store = {
  get(k, fb) {
    try { const v = localStorage.getItem(k); return v == null ? fb : JSON.parse(v); }
    catch (e) { return fb; }
  },
  set(k, v) {
    try { localStorage.setItem(k, JSON.stringify(v)); return true; }
    catch (e) { return false; }
  }
};

let toastTimer = null;
function toast(msg, ms) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), ms || 2600);
}

/* ---------- API ---------- */
const API = "/api";

/* Timer polling per halaman: didaftarkan via regTimer agar otomatis berhenti
   saat user pindah halaman (tidak ada request sia-sia ke server). */
const _pageTimers = [];
function regTimer(t) { _pageTimers.push(t); return t; }
function clearPageTimers() {
  while (_pageTimers.length) { try { clearInterval(_pageTimers.pop()); } catch (e) {} }
}

async function apiJSON(path, opts) {
  const o = Object.assign({ method: "GET", headers: { "Content-Type": "application/json" } }, opts || {});
  // timeout: 30 dtk umum; 80 dtk untuk /ai, /tanya, /topik (budget server
  // 75 dtk — timeout client HARUS di atasnya supaya kuota tidak hangus diam-diam)
  const _p = String(path);
  const ms = o.timeoutMs || ((_p === "/ai" || _p === "/tanya" || _p === "/topik") ? 80000 : 30000);
  delete o.timeoutMs;
  const ctl = (typeof AbortController !== "undefined") ? new AbortController() : null;
  let tmr = null;
  if (ctl) {
    o.signal = ctl.signal;
    tmr = setTimeout(() => { try { ctl.abort(); } catch (e) {} }, ms);
  }
  try {
    const r = await fetch(API + path, o);
    const ct = r.headers.get("content-type") || "";
    let data = null;
    if (ct.indexOf("application/json") >= 0) {
      try { data = await r.json(); } catch (e) { data = null; }
    } else {
      try { data = { _text: await r.text() }; } catch (e) { data = null; }
    }
    if (!r.ok) return { ok: false, status: r.status, data: data, error: "HTTP " + r.status };
    return { ok: true, status: r.status, data: data };
  } catch (e) {
    const timeout = e && e.name === "AbortError";
    return { ok: false, status: 0, data: null, error: timeout ? "timeout" : "network" };
  } finally {
    if (tmr) clearTimeout(tmr);
  }
}

function apiPOST(path, body) {
  return apiJSON(path, { method: "POST", body: JSON.stringify(body || {}) });
}

/* ---------- Auth (username+password / tamu) ---------- */
let AUTH = { login: false };
let AUTH_SIAP = false; // R2 (audit 2026-10-13): true setelah /auth/status pertama resolve
let _riwAntre = [];    // R2: entri tamu yang dicatat sebelum status auth pasti

/* Jatah AI gratis harian sesuai status login: tamu 5x, login 10x. */
function aiHarian() { return AUTH.login ? 10 : 5; }

/* ---------- Avatar maskot ---------- */
const AVATAR_PILIHAN = ["teal", "orange", "navy", "red", "purple"];
function avatarURL(p) {
  p = AVATAR_PILIHAN.indexOf(p) >= 0 ? p : "teal";
  return "avatar/avatar-" + p + ".svg";
}

async function refreshAuth() {
  try {
    const r = await apiJSON("/auth/status");
    if (r && r.ok && r.data) AUTH = r.data;
  } catch (e) {}
  // R2: tandai siap + siram antrean pencatatan tamu
  AUTH_SIAP = true;
  aturMenuAdmin(); // tampilkan/sembunyikan link Panel Admin
  _siramRiwAntre();
  const area = $("#authArea");
  if (!area) return;
  if (AUTH.login) {
    const nama = esc(String(AUTH.nama || "Akun").split(" ")[0]);
    area.innerHTML = '<a href="#/akun" class="user-chip">' +
      (AUTH.foto ? '<img src="' + safeUrl(AUTH.foto) + '" alt="foto">' : '<img src="' + avatarURL(AUTH.avatar) + '" alt="foto profil">') +
      "<span>" + nama + "</span></a>";
  } else {
    area.innerHTML = '<a href="#/masuk" class="btn-login">🔑 Masuk</a>';
  }
}

/* unduh file dari endpoint (response = file biasa) */
async function apiDownload(path, opts, fallbackName) {
  const o = Object.assign({ method: "GET" }, opts || {});
  const ctl = (typeof AbortController !== "undefined") ? new AbortController() : null;
  let tmr = null;
  if (ctl) {
    o.signal = ctl.signal;
    tmr = setTimeout(() => { try { ctl.abort(); } catch (e) {} }, 60000);
  }
  try {
    const r = await fetch(API + path, o);
    if (!r.ok) {
      let code = "HTTP " + r.status, pesan = "";
      const ct2 = r.headers.get("content-type") || "";
      if (ct2.indexOf("application/json") >= 0) {
        try { const j = await r.json(); if (j && j.error) code = j.error; if (j && j.pesan) pesan = j.pesan; } catch (e) {}
      }
      return { ok: false, status: r.status, error: code, pesan: pesan };
    }
    const blob = await r.blob();
    let name = fallbackName || "khairypustaka-download";
    const cd = r.headers.get("content-disposition") || "";
    const m = cd.match(/filename\*?=(?:UTF-8'')?"?([^";\n]+)"?/i);
    if (m && m[1]) { try { name = decodeURIComponent(m[1]); } catch (e) { name = m[1]; } }
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = name;
    document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 1500);
    return { ok: true };
  } catch (e) {
    const timeout = e && e.name === "AbortError";
    return { ok: false, error: timeout ? "timeout" : "network" };
  } finally {
    if (tmr) clearTimeout(tmr);
  }
}

function netErr(box, res) {
  if (kuotaTamuBox(box, res)) return;
  const d = (res && res.data) || {};
  // tampilkan pesan backend yang actionable/ramah (mis. input salah 422,
  // rate_limit, kuota AI habis) sebelum fallback ke pesan koneksi generik
  const msg = d.pesan || d.error ||
    (res && res.error === "timeout"
      ? "Lama banget nggak ada respons (timeout). Coba lagi ya!"
      : "Waduh, koneksinya lagi bermasalah. Cek internet kamu terus coba lagi ya!");
  box.innerHTML = '<div class="err-box">' + esc(msg) + "</div>";
}

/* Kuota tamu non-AI habis: pesan ramah + ajakan login. Kembalikan true bila ditangani. */
function _kuotaTamuMatch(res) {
  if (!res || res.ok) return null;
  const d = res.data || {};
  // bentuk apiJSON: {data:{error,pesan}} ; bentuk apiDownload: {error,pesan} langsung
  if (d.error === "kuota_tamu_habis") return d.pesan;
  if (res.error === "kuota_tamu_habis") return res.pesan;
  return null;
}
function kuotaTamuBox(box, res) {
  const pesan = _kuotaTamuMatch(res);
  if (pesan != null) {
    box.innerHTML = '<div class="warn-box">' + esc(pesan || "Jatah harian tamu untuk fitur ini habis (10x/hari).") +
      '<br><a class="btn btn-primary" style="margin-top:10px;display:inline-block" href="#/masuk">Masuk / Daftar — gratis! ✨</a></div>';
    return true;
  }
  return false;
}
function kuotaTamuToast(res) {
  const pesan = _kuotaTamuMatch(res);
  if (pesan != null) {
    toast("🔒 " + (pesan || "Jatah harian tamu habis (10x/hari).") + " Login gratis biar sepuasnya! ✨");
    return true;
  }
  return false;
}

/* URL aman untuk link: hanya http(s), cegah javascript: dkk. */
function safeUrl(u) {
  u = String(u || "").trim();
  if (/^https?:\/\//i.test(u)) return u;
  return "";
}

/* base64 valid untuk data URL gambar (cegah break-out atribut src) */
function amanB64(s) {
  s = String(s || "");
  return /^[A-Za-z0-9+/=]+$/.test(s) && s.length % 4 === 0 ? s : "";
}

function setLoading(btn, on, label) {
  if (!btn) return;
  if (on) { btn.dataset.label = btn.innerHTML; btn.disabled = true; btn.innerHTML = "LOADING " + (label || "Lagi diproses..."); }
  else { btn.disabled = false; if (btn.dataset.label) btn.innerHTML = btn.dataset.label; }
}

async function copyText(teks) {
  try {
    await navigator.clipboard.writeText(teks);
    toast("Udah disalin! Tinggal tempel.");
    return true;
  } catch (e) {
    try {
      const ta = document.createElement("textarea");
      ta.value = teks; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      document.execCommand("copy"); ta.remove();
      toast("Udah disalin! Tinggal tempel.");
      return true;
    } catch (e2) {
      toast("Gagal nyalin otomatis, salin manual ya.");
      return false;
    }
  }
}

/* ---------- Koleksi (localStorage fase 1) ---------- */
const KOLEKSI_KEY = "kp_koleksi_v1";
function koleksiGet() { const v = store.get(KOLEKSI_KEY, []); return Array.isArray(v) ? v : []; }
function koleksiSet(arr) { return store.set(KOLEKSI_KEY, arr); }
function koleksiTambah(item) {
  const arr = koleksiGet();
  const judul = (item.judul || "").trim().toLowerCase();
  if (judul && arr.some(x => (x.judul || "").trim().toLowerCase() === judul)) return "duplikat";
  arr.unshift(item);
  return koleksiSet(arr) ? "ok" : "gagal";
}
function koleksiHapus(i) {
  const arr = koleksiGet();
  arr.splice(i, 1);
  koleksiSet(arr);
}

function kartuHasil(item, idx) {
  const judul = esc(item.judul || "(tanpa judul)");
  const penulis = esc(Array.isArray(item.penulis) ? item.penulis.join(", ") : (item.penulis || "-"));
  const tahun = esc(item.tahun || "-");
  const jurnal = esc(item.jurnal || "-");
  const abstrak = item.abstrak
    ? '<details class="abstrak"><summary>Lihat abstrak</summary><p>' + esc(item.abstrak) + "</p></details>" : "";
  const url = safeUrl(item.url || (item.doi ? "https://doi.org/" + encodeURIComponent(item.doi) : ""));
  const linkBtn = url ? '<a class="btn btn-ghost" href="' + esc(url) + '" target="_blank" rel="noopener">Buka Link</a>' : "";
  const pdf = safeUrl(item.pdf_url);
  const pdfBtn = (pdf && pdf !== url) ? '<a class="btn btn-ghost" href="' + esc(pdf) + '" target="_blank" rel="noopener">📄 PDF</a>' : "";
  return '<article class="card hasil">' +
    "<h3>" + judul + "</h3>" +
    '<div class="meta">' + penulis + " | " + tahun + " | " + jurnal + "</div>" +
    abstrak +
    '<div class="aksi-row">' +
      '<button class="btn" data-aksi="sitasi" data-idx="' + idx + '">Sitasi</button>' +
      '<button class="btn" data-aksi="simpan" data-idx="' + idx + '">Simpan ke Koleksi</button>' +
      (item.abstrak ? '<button class="btn" data-aksi="ringkas" data-idx="' + idx + '">✨ Ringkas AI</button>' : "") +
      (item.abstrak ? '<button class="btn" data-aksi="terjemah" data-idx="' + idx + '">🌐 Terjemah</button>' : "") +
      linkBtn + pdfBtn +
    "</div></article>";
}

/* ---------- Modal sitasi ---------- */
let sitasiItem = null;
function bukaModalSit(item) {
  sitasiItem = item || {};
  $("#modalSitJudul").textContent = sitasiItem.judul || "";
  $("#modalSitHasil").classList.add("hidden");
  $("#modalSitLoading").classList.add("hidden");
  $$("#modalSitGaya .chip").forEach(c => c.classList.remove("active"));
  $("#modalSit").classList.remove("hidden");
}
function tutupModalSit() { $("#modalSit").classList.add("hidden"); sitasiItem = null; }

async function buatSitasi(gaya, chipEl) {
  if (!sitasiItem) return;
  $$("#modalSitGaya .chip").forEach(c => c.classList.remove("active"));
  if (chipEl) chipEl.classList.add("active");
  $("#modalSitHasil").classList.add("hidden");
  const load = $("#modalSitLoading");
  load.classList.remove("hidden");
  const res = await apiPOST("/sitasi", { work: sitasiItem, gaya: gaya });
  load.classList.add("hidden");
  if (!res.ok) {
    if (kuotaTamuToast(res)) return;
    toast(res.error === "network" ? "Koneksi bermasalah, coba lagi ya." : "Gagal bikin sitasi, server lagi sibuk?");
    return;
  }
  const teks = res.data && (res.data.sitasi || res.data.hasil || res.data.text);
  if (!teks) { toast("Server nggak ngasih sitasi, coba gaya lain ya."); return; }
  $("#modalSitTeks").textContent = teks;
  $("#modalSitHasil").classList.remove("hidden");
  riwTamuCatat("sitasi", (sitasiItem && (sitasiItem.title || sitasiItem.judul)) || "", { gaya: (res.data && res.data.gaya) || gaya, sitasi: teks, work: sitasiItem });
}


/* ---------- Halaman ---------- */
const view = () => $("#view");

/* ----- Buat Sitasi (halaman mandiri) ----- */
function pageSitasi() {
  const GAYA = [["harvard", "Harvard"], ["apa", "APA"], ["mla", "MLA"],
                ["chicago", "Chicago"], ["vancouver", "Vancouver"], ["ieee", "IEEE"]];
  view().innerHTML =
    "<h2>"+tic("quote")+" Buat Sitasi</h2>" +
    '<div class="card"><p class="muted">Isi detail sumbernya, pilih gaya sitasi, langsung jadi. ✨</p>' +
    '<label for="sitJudul">Judul *</label><input type="text" id="sitJudul" placeholder="Judul artikel/jurnal" autocomplete="off">' +
    '<label for="sitPenulis">Penulis</label><input type="text" id="sitPenulis" placeholder="Nama penulis, pisahkan dengan koma" autocomplete="off">' +
    '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">' +
    '<div><label for="sitTahun">Tahun</label><input type="text" id="sitTahun" inputmode="numeric" placeholder="2024" autocomplete="off"></div>' +
    '<div><label for="sitVolume">Volume</label><input type="text" id="sitVolume" placeholder="12" autocomplete="off"></div></div>' +
    '<label for="sitJurnal">Jurnal / Penerbit</label><input type="text" id="sitJurnal" placeholder="Nama jurnal" autocomplete="off">' +
    '<label for="sitPages">Halaman</label><input type="text" id="sitPages" placeholder="1-15" autocomplete="off">' +
    '<label>Gaya sitasi</label><div class="chip-row" id="sitGaya">' +
    GAYA.map((g, i) => '<button class="chip' + (i === 0 ? " active" : "") + '" data-gaya="' + g[0] + '">' + g[1] + "</button>").join("") +
    "</div>" +
    '<button class="btn btn-primary btn-block" id="btnBuatSit">📑 Buatkan Sitasi</button></div>' +
    '<div id="sitHasil"></div>';
  let gaya = "harvard";
  $$("#sitGaya .chip").forEach(c => c.addEventListener("click", () => {
    $$("#sitGaya .chip").forEach(x => x.classList.remove("active"));
    c.classList.add("active"); gaya = c.dataset.gaya;
  }));
  $("#btnBuatSit").addEventListener("click", async () => {
    const work = {
      judul: $("#sitJudul").value.trim(),
      penulis: $("#sitPenulis").value.trim(),
      tahun: $("#sitTahun").value.trim(),
      jurnal: $("#sitJurnal").value.trim(),
      volume: $("#sitVolume").value.trim(),
      pages: $("#sitPages").value.trim()
    };
    if (!work.judul) { toast("Isi dulu judulnya 😄"); return; }
    const box = $("#sitHasil");
    box.innerHTML = '<p class="loading">Lagi nyusun sitasi... ⏳</p>';
    const res = await apiPOST("/sitasi", { work: work, gaya: gaya });
    if (!res.ok) { if (!kuotaTamuBox(box, res)) box.innerHTML = '<div class="err-box">Gagal bikin sitasi, coba lagi ya. 😅</div>'; return; }
    const teks = res.data && (res.data.sitasi || res.data.hasil || res.data.text);
    if (!teks) { box.innerHTML = '<div class="err-box">Server nggak ngasih sitasi, coba lagi ya.</div>'; return; }
    box.innerHTML = '<div class="card hasil"><h3>Hasil sitasi (' + esc((res.data && res.data.gaya) || gaya) + ")</h3>" +
      '<div class="sitasi-hasil"><pre>' + esc(teks) + "</pre></div>" +
      '<button class="btn btn-accent" id="btnSalinSit">📋 Salin</button></div>';
    riwTamuCatat("sitasi", work.judul || "", { gaya: (res.data && res.data.gaya) || gaya, sitasi: teks, work: work });
    $("#btnSalinSit").addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(teks); toast("Disalin! 📋"); }
      catch (e) { toast("Gagal nyalin 😅"); }
    });
  });
}

/* ----- Masuk ----- */
function pageMasuk() {
  const q = (location.hash.split("?")[1] || "");
  const err = new URLSearchParams(q).get("err");
  const errMsg = err === "batal" ? "Login Google dibatalkan. 😅"
    : err === "noconfig" ? "Login Google belum dikonfigurasi admin. 🙏"
    : err ? "Login gagal, coba lagi ya. 😅" : "";
  if (AUTH.login) { location.hash = "#/akun"; return; }
  function verifyHelpHTML(idp) {
    return '<div class="warn-box" style="text-align:left">' +
      '<b>Masih tidak mendapatkan email konfirmasi?</b>' +
      '<p class="tiny" style="margin:6px 0 10px">Silakan cek folder <b>Inbox</b> dan <b>Spam</b>. Pastikan juga penyimpanan Gmail tidak penuh dan alamat email yang dimasukkan sudah benar.</p>' +
      '<button type="button" class="btn" id="' + idp + 'Resend">Kirim Ulang Email</button> ' +
      '<button type="button" class="btn btn-ghost" id="' + idp + 'Toggle">Ganti Email</button>' +
      '<div id="' + idp + 'Box" class="hidden" style="margin-top:10px">' +
      '<input type="text" id="' + idp + 'Ident" placeholder="username atau email lama" autocomplete="username" autocapitalize="off" spellcheck="false">' +
      '<input type="password" id="' + idp + 'Pass" placeholder="password akun" autocomplete="current-password">' +
      '<input type="email" id="' + idp + 'Mail" placeholder="email baru" autocomplete="email" autocapitalize="off" spellcheck="false">' +
      '<button type="button" class="btn btn-primary btn-block" id="' + idp + 'Submit">Kirim Link ke Email Baru</button>' +
      '<p class="tiny" id="' + idp + 'Msg" style="margin-top:8px"></p>' +
      '</div></div>';
  }
  function wireVerifyHelp(idp, opts) {
    opts = opts || {};
    var identEl = document.getElementById(idp + "Ident");
    var passEl = document.getElementById(idp + "Pass");
    var mailEl = document.getElementById(idp + "Mail");
    var msgEl = document.getElementById(idp + "Msg");
    var boxEl = document.getElementById(idp + "Box");
    if (identEl && opts.ident) identEl.value = opts.ident;
    var resendBtn = document.getElementById(idp + "Resend");
    if (resendBtn) resendBtn.addEventListener("click", async function () {
      var ident = identEl && identEl.value.trim() ? identEl.value.trim() : (opts.email || "");
      if (!ident) { toast("Isi dulu username atau email kamu 😄"); if (identEl) identEl.focus(); return; }
      setLoading(resendBtn, true, "Mengirim...");
      var r = await apiPOST("/auth/kirim-ulang", { email: ident });
      setLoading(resendBtn, false);
      toast(r.ok ? "Dikirim! Cek email ya 📧" : "😅 " + ((r.data && r.data.error) || "gagal mengirim"));
    });
    var toggleBtn = document.getElementById(idp + "Toggle");
    if (toggleBtn && boxEl) toggleBtn.addEventListener("click", function () {
      boxEl.classList.toggle("hidden");
      if (!boxEl.classList.contains("hidden") && identEl) identEl.focus();
    });
    var submitBtn = document.getElementById(idp + "Submit");
    if (submitBtn) submitBtn.addEventListener("click", async function () {
      var ident = identEl ? identEl.value.trim() : "";
      var pass = passEl ? passEl.value : "";
      var mail = mailEl ? mailEl.value.trim() : "";
      if (!ident || !pass || !mail) { toast("Lengkapi username/email, password, dan email baru ya 😄"); return; }
      if (msgEl) msgEl.textContent = "Lagi mengganti email dan mengirim link baru...";
      setLoading(submitBtn, true, "Memproses...");
      var r = await apiPOST("/auth/ganti-email-pending", { username: ident, password: pass, email_baru: mail });
      setLoading(submitBtn, false);
      if (r.ok) {
        opts.email = mail;
        if (msgEl) msgEl.textContent = "Berhasil! Link verifikasi baru sudah dikirim ke " + mail + ". Cek Inbox dan Spam ya.";
        if (passEl) passEl.value = "";
        if (opts.onChanged) opts.onChanged(mail);
        toast("Email berhasil diganti 📧");
      } else {
        var emsg = (r.data && r.data.error) || "gagal mengganti email";
        if (msgEl) msgEl.textContent = emsg;
        toast("😅 " + emsg);
      }
    });
  }
  function showLoginVerifyHelp(ident) {
    var wrap = document.getElementById("loginVerifyHelp");
    if (!wrap) return;
    wrap.classList.remove("hidden");
    var identEl = document.getElementById("lvIdent");
    if (identEl && ident) identEl.value = ident;
    if (wrap.scrollIntoView) wrap.scrollIntoView({ behavior: "smooth", block: "center" });
  }
  view().innerHTML =
    "<h2>"+tic("key")+" Masuk</h2>" +
    (errMsg ? '<div class="err-box">' + esc(errMsg) + "</div>" : "") +
    '<div class="card center">' +
    '<div class="kp-brand-block"><img src="maskot.jpg" class="kp-brand-maskot" alt="Maskot KhairyPustaka">' +
    '<div class="kp-brand-text"><h3 class="kp-brand-name">KhairyPustaka</h3>' +
    '<p class="kp-brand-tag">Asisten skripsi dan nugas, seratus persen gratis</p></div></div>' +
    '<p class="muted">Login biar saldomu aman & bisa dipakai di semua perangkat. ✨</p>' +
    '<div class="chip-row" style="justify-content:center;margin-bottom:10px">' +
    '<button class="chip active" id="tabPwMasuk">Masuk</button>' +
    '<button class="chip" id="tabPwDaftar">Daftar</button></div>' +
    '<div id="pwMasukBox">' +
    '<input type="text" id="pwUserIn" placeholder="username atau email" autocomplete="username" autocapitalize="off" spellcheck="false">' +
    '<div class="pw-wrap"><input type="password" id="pwPassIn" placeholder="password" autocomplete="current-password">' +
    '<button type="button" class="pw-eye" data-for="pwPassIn" aria-label="Tampilkan password">👁️</button></div>' +
    '<button class="btn btn-primary btn-block" id="btnPwMasuk">Masuk</button>' +
    '<p class="center tiny" style="margin-top:8px"><a href="#/lupa">Lupa password? 🤔</a> · <a href="#" id="linkResend">Belum terima email verifikasi? 📧</a></p></div>' +
    '<div id="loginVerifyHelp" class="hidden">' + verifyHelpHTML("lv") + '</div>' +
    '<div id="pwDaftarBox" class="hidden">' +
    '<input type="text" id="pwUserUp" placeholder="pilih username" autocomplete="username" autocapitalize="off" spellcheck="false">' +
    '<input type="email" id="pwMailUp" placeholder="email aktif" autocomplete="email" autocapitalize="off" spellcheck="false">' +
    '<div class="pw-wrap"><input type="password" id="pwPassUp" placeholder="password (min. 8 karakter)" autocomplete="new-password">' +
    '<button type="button" class="pw-eye" data-for="pwPassUp" aria-label="Tampilkan password">👁️</button></div>' +
    '<button class="btn btn-accent btn-block" id="btnPwDaftar">Daftar Akun</button>' +
    '<p class="muted tiny">Kami kirim link verifikasi ke emailmu 📧</p></div>' +
    '<div class="login-or"><span>atau</span></div>' +
    '<a class="btn btn-block" href="#/beranda">👤 Lanjut sebagai tamu</a>' +
    '<p class="muted tiny">Mode tamu tetap bisa pakai semua fitur gratis. Tapi top-up wajib login biar saldonya nggak hilang. 🔐</p></div>';
  // tab Masuk / Daftar
  const tabM = $("#tabPwMasuk"), tabD = $("#tabPwDaftar");
  const boxM = $("#pwMasukBox"), boxD = $("#pwDaftarBox");
  function pwTab(masuk) {
    tabM.classList.toggle("active", masuk);
    tabD.classList.toggle("active", !masuk);
    boxM.classList.toggle("hidden", !masuk);
    boxD.classList.toggle("hidden", masuk);
  }
  tabM.addEventListener("click", () => pwTab(true));
  tabD.addEventListener("click", () => pwTab(false));
  wireVerifyHelp("lv", {});
  // toggle intip password 👁️/🙈
  $$(".pw-eye", view()).forEach(b => b.addEventListener("click", () => {
    const inp = $("#" + b.dataset.for, view());
    if (!inp) return;
    const show = inp.type === "password";
    inp.type = show ? "text" : "password";
    b.textContent = show ? "🙈" : "👁️";
    b.setAttribute("aria-label", show ? "Sembunyikan password" : "Tampilkan password");
  }));
  async function pwAuth(mode) {
    const u = $(mode === "in" ? "#pwUserIn" : "#pwUserUp").value.trim();
    const p = $(mode === "in" ? "#pwPassIn" : "#pwPassUp").value;
    const m = mode === "up" ? $("#pwMailUp").value.trim() : "";
    if (!u || !p || (mode === "up" && !m)) { toast("Lengkapi dulu ya 😄"); return; }
    const payload = mode === "in" ? { username: u, password: p }
                                  : { username: u, password: p, email: m };
    const res = await apiPOST(mode === "in" ? "/auth/login" : "/auth/register", payload);
    if (res.ok) {
      if (mode === "up" && res.data && res.data.need_verify) {
        view().innerHTML =
          "<h2>"+tic("mail")+" Cek Email</h2>" +
          '<div class="card center"><p>Akun <b>' + esc(u) + '</b> dibuat! 🎉</p>' +
          '<p class="muted">Kami kirim link verifikasi ke <b id="regEmail">' + esc(m) + '</b>. ' +
          "Klik linknya, terus masuk deh. ✨</p>" +
          verifyHelpHTML("rv") +
          '<a class="btn btn-primary" href="#/masuk">Ke halaman Masuk</a></div>';
        wireVerifyHelp("rv", { ident: u, email: m, onChanged: function (mail) {
          var el = document.getElementById("regEmail");
          if (el) el.textContent = mail;
        }});
        return;
      }
      await refreshAuth();
      toast(mode === "in" ? "Selamat datang kembali! 🎉" : "Akun dibuat! 🎉");
      location.hash = "#/akun";
    } else {
      const msg = (res.data && res.data.error) || "Gagal, coba lagi ya. 😅";
      if (mode === "in" && res.status === 403 && msg.indexOf("verifikasi email") >= 0) {
        showLoginVerifyHelp(u);
      }
      toast(res.status === 429 ? "⏳ " + msg : "😅 " + msg);
    }
  }
  // Buka panel bantuan verifikasi: user bisa kirim ulang atau ganti email.
  const linkResend = $("#linkResend");
  if (linkResend) linkResend.addEventListener("click", (e) => {
    e.preventDefault();
    showLoginVerifyHelp($("#pwUserIn").value.trim());
  });
  $("#btnPwMasuk").addEventListener("click", () => pwAuth("in"));
  $("#btnPwDaftar").addEventListener("click", () => pwAuth("up"));
  $("#pwPassIn").addEventListener("keydown", e => { if (e.key === "Enter") pwAuth("in"); });
  $("#pwPassUp").addEventListener("keydown", e => { if (e.key === "Enter") pwAuth("up"); });
}

/* ----- Lupa password ----- */
function pageLupa() {
  if (AUTH.login) { location.hash = "#/akun"; return; }
  view().innerHTML =
    "<h2>"+tic("help")+" Lupa Password</h2>" +
    '<div class="card center"><p class="muted">Masukin email yang dipakai daftar. ' +
    "Kalau terdaftar, kami kirim link reset ke sana. 📧</p>" +
    '<input type="email" id="lupaMail" placeholder="email" autocomplete="email" autocapitalize="off" spellcheck="false">' +
    '<button class="btn btn-primary btn-block" id="btnLupa" style="margin-top:12px">Kirim Link Reset</button>' +
    '<p class="center tiny" style="margin-top:8px"><a href="#/masuk">← Balik ke Masuk</a></p></div>';
  const go = async () => {
    const m = $("#lupaMail").value.trim();
    if (!m) { toast("Isi emailnya dulu 😄"); return; }
    const btn = $("#btnLupa"); setLoading(btn, true, "Ngirim...");
    const res = await apiPOST("/auth/lupa", { email: m });
    setLoading(btn, false);
    toast(res.ok ? "📧 " + ((res.data && res.data.msg) || "dikirim!") : "😅 coba lagi ya");
  };
  $("#btnLupa").addEventListener("click", go);
  $("#lupaMail").addEventListener("keydown", e => { if (e.key === "Enter") go(); });
}

/* ----- Reset password (dari link email) ----- */
function pageReset() {
  const token = new URLSearchParams((location.hash.split("?")[1] || "")).get("token") || "";
  if (!token) {
    view().innerHTML = "<h2>"+tic("key")+" Reset Password</h2>" +
      '<div class="err-box">Link resetnya nggak lengkap 😅 Minta link baru dari halaman <a href="#/lupa">Lupa Password</a> ya.</div>';
    return;
  }
  view().innerHTML =
    "<h2>"+tic("key")+" Password Baru</h2>" +
    '<div class="card center"><p class="muted">Bikin password baru buat akunmu (min. 8 karakter).</p>' +
    '<div class="pw-wrap"><input type="password" id="rstPw1" placeholder="password baru" autocomplete="new-password">' +
    '<button type="button" class="pw-eye" data-for="rstPw1" aria-label="Tampilkan password">👁️</button></div>' +
    '<div class="pw-wrap"><input type="password" id="rstPw2" placeholder="ulangi password baru" autocomplete="new-password">' +
    '<button type="button" class="pw-eye" data-for="rstPw2" aria-label="Tampilkan password">👁️</button></div>' +
    '<button class="btn btn-primary btn-block" id="btnReset" style="margin-top:12px">Ganti Password</button></div>';
  $$(".pw-eye", view()).forEach(b => b.addEventListener("click", () => {
    const inp = $("#" + b.dataset.for, view());
    if (!inp) return;
    const show = inp.type === "password";
    inp.type = show ? "text" : "password";
    b.textContent = show ? "🙈" : "👁️";
  }));
  $("#btnReset").addEventListener("click", async () => {
    const p1 = $("#rstPw1").value, p2 = $("#rstPw2").value;
    if (p1 !== p2) { toast("Passwordnya nggak sama 😅"); return; }
    if (p1.length < 8) { toast("Password minimal 8 karakter ya 😄"); return; }
    const btn = $("#btnReset"); setLoading(btn, true, "Nyimpen...");
    const res = await apiPOST("/auth/reset", { token: token, password: p1 });
    setLoading(btn, false);
    if (res.ok) {
      view().innerHTML = "<h2>"+tic("check")+" Berhasil!</h2>" +
        '<div class="card center"><p>Password berhasil diganti! Silakan masuk pakai password barumu. ✨</p>' +
        '<a class="btn btn-primary btn-block" href="#/masuk">Masuk</a></div>';
    } else {
      toast("😅 " + ((res.data && res.data.error) || "gagal, coba minta link baru"));
    }
  });
}

/* ----- Verifikasi email (dari link email) ----- */
function pageVerifikasi() {
  const token = new URLSearchParams((location.hash.split("?")[1] || "")).get("token") || "";
  view().innerHTML = "<h2>"+tic("mail")+" Verifikasi Email</h2>" +
    '<div class="card center"><p class="loading">Lagi verifikasi... ⏳</p></div>';
  (async () => {
    if (!token) {
      view().innerHTML = "<h2>"+tic("mail")+" Verifikasi Email</h2>" +
        '<div class="err-box">Link verifikasinya nggak lengkap 😅</div>';
      return;
    }
    const res = await apiPOST("/auth/verifikasi", { token: token });
    if (res.ok) {
      await refreshAuth();
      view().innerHTML = "<h2>"+tic("check")+" Email Terverifikasi!</h2>" +
        '<div class="card center"><p>Akunmu aktif! Selamat datang di KhairyPustaka ✨</p>' +
        '<a class="btn btn-primary btn-block" href="#/akun">Ke Akun Saya</a></div>';
    } else {
      view().innerHTML = "<h2>"+tic("mail")+" Verifikasi Email</h2>" +
        '<div class="err-box">😅 ' + esc((res.data && res.data.error) || "verifikasi gagal") + "</div>" +
        '<div class="card center"><p class="muted">Link mungkin sudah dipakai / kedaluwarsa. ' +
        'Minta link baru dari halaman Masuk ya.</p><a class="btn btn-primary" href="#/masuk">Ke halaman Masuk</a></div>';
    }
  })();
}

/* ----- Akun ----- */
function pfMsg(id, ok, text) {
  const el = $("#" + id, view());
  if (!el) return;
  el.textContent = text || "";
  el.style.color = ok ? "#0f766e" : "#b91c1c";
}

function pageAkun() {
  if (!AUTH.login) {
    view().innerHTML =
      "<h2>"+tic("person")+" Akun</h2>" +
      '<div class="card center"><img src="' + avatarURL("teal") + '" alt="maskot" class="akun-foto">' +
      '<p class="muted">Kamu lagi mode tamu. Login biar saldo & datamu aman di semua perangkat. 🔐</p>' +
      '<a class="btn btn-primary btn-block" href="#/masuk">🔑 Masuk / Daftar</a></div>';
    return;
  }
  const u = AUTH;
  const isPw = u.provider === "password";
  const via = u.provider === "google" ? "via Google" : "";
  view().innerHTML =
    "<h2>"+tic("person")+" Akun Saya</h2>" +
    '<div class="card center">' +
    (u.foto ? '<img src="' + esc(u.foto) + '" alt="foto" class="akun-foto">' : '<img src="' + avatarURL(u.avatar) + '" alt="foto profil" class="akun-foto">') +
    "<h3>" + esc(u.nama || "Pengguna") + "</h3>" +
    (isPw
      ? '<p class="pf-akun-info">👤 @' + esc(u.username || "") + "</p>" +
        '<p class="pf-akun-info">📧 ' + esc(u.email || "") + "</p>"
      : (via ? '<p class="muted">' + esc(via) + "</p>" : "")) +
    '<a class="btn btn-primary btn-block" href="#/topup">⚡ Jatah AI</a>' +
    (u.admin ? '<a class="btn btn-block" href="#/admin">🛡️ Panel Admin</a>' : "") +
    '<button class="btn btn-block" id="btnLogout">🚪 Keluar</button></div>' +
    (!u.foto ?
      '<div class="card"><h3>🎨 Foto Profil</h3><p class="muted">Pilih maskot favoritmu:</p>' +
      '<div class="avatar-grid">' + AVATAR_PILIHAN.map(function(p) {
        return '<button class="avatar-opt' + (avatarURL(u.avatar) === avatarURL(p) ? " active" : "") +
          '" data-av="' + p + '" aria-label="Avatar ' + p + '"><img src="' + avatarURL(p) + '" alt="avatar ' + p + '"></button>';
      }).join("") + '</div><p class="pf-msg" id="pfAvMsg"></p></div>'
      : "") +
    (isPw ?
      '<div class="card"><h3>✏️ Ubah Username</h3>' +
      '<input type="text" id="pfUserBaru" placeholder="username baru (3–20 karakter)" maxlength="20" autocapitalize="off" autocomplete="off">' +
      '<p class="pf-msg" id="pfUserMsg"></p>' +
      '<button class="btn btn-primary btn-block" id="btnUbahUser">Simpan Username</button></div>' +
      '<div class="card"><h3>🔑 Ubah Password</h3>' +
      '<div class="pw-wrap"><input type="password" id="pfPwLama" placeholder="password lama" autocomplete="current-password">' +
      '<button type="button" class="pw-eye" data-for="pfPwLama" aria-label="Tampilkan password">👁️</button></div>' +
      '<div class="pw-wrap"><input type="password" id="pfPwBaru" placeholder="password baru (min. 8 karakter)" autocomplete="new-password">' +
      '<button type="button" class="pw-eye" data-for="pfPwBaru" aria-label="Tampilkan password">👁️</button></div>' +
      '<p class="pf-msg" id="pfPwMsg"></p>' +
      '<button class="btn btn-primary btn-block" id="btnUbahPw">Simpan Password</button></div>' +
      '<div class="card"><h3>📧 Ubah Email</h3>' +
      '<p class="muted">Link verifikasi dikirim ke email barumu (berlaku 24 jam). Email lama tetap aktif sampai kamu klik link itu.</p>' +
      '<input type="email" id="pfMailBaru" placeholder="email baru" autocapitalize="off" autocomplete="off">' +
      '<div class="pw-wrap"><input type="password" id="pfMailPw" placeholder="konfirmasi password" autocomplete="current-password">' +
      '<button type="button" class="pw-eye" data-for="pfMailPw" aria-label="Tampilkan password">👁️</button></div>' +
      '<p class="pf-msg" id="pfMailMsg"></p>' +
      '<button class="btn btn-primary btn-block" id="btnUbahMail">Kirim Link Verifikasi</button></div>'
      : '<div class="card"><p class="muted">Akun ini login ' + esc(via || "via Google") +
        " — ubah username, password & email hanya tersedia untuk akun username+password. 🙂</p></div>");
  $("#btnLogout").addEventListener("click", async () => {
    await apiPOST("/auth/logout", {});
    AUTH = { login: false };
    await refreshAuth();
    toast("Sampai jumpa! 👋");
    location.hash = "#/beranda";
  });
  // pilih avatar maskot 🎨 (klik = langsung simpan)
  $$(".avatar-opt", view()).forEach(function(b) {
    b.addEventListener("click", async () => {
      const p = b.getAttribute("data-av");
      const res = await apiPOST("/auth/ubah-avatar", { avatar: p });
      if (res.ok) {
        await refreshAuth();
        pageAkun();
        toast("Foto profil diganti 🎉");
      } else {
        pfMsg("pfAvMsg", false, "😅 " + ((res.data && res.data.error) || "gagal, coba lagi ya"));
      }
    });
  });
  if (!isPw) return;

  // toggle intip password 👁️/🙈
  $$(".pw-eye", view()).forEach(b => b.addEventListener("click", () => {
    const inp = $("#" + b.dataset.for, view());
    if (!inp) return;
    const show = inp.type === "password";
    inp.type = show ? "text" : "password";
    b.textContent = show ? "🙈" : "👁️";
    b.setAttribute("aria-label", show ? "Sembunyikan password" : "Tampilkan password");
  }));

  $("#btnUbahUser").addEventListener("click", async () => {
    const v = $("#pfUserBaru", view()).value.trim();
    if (!v) { pfMsg("pfUserMsg", false, "isi username barunya dulu 😄"); return; }
    const btn = $("#btnUbahUser"); setLoading(btn, true, "Nyimpen...");
    const res = await apiPOST("/auth/ubah-username", { username_baru: v });
    setLoading(btn, false);
    if (res.ok) {
      await refreshAuth();
      pageAkun();
      toast("Username berhasil diganti 🎉");
    } else {
      pfMsg("pfUserMsg", false, "😅 " + ((res.data && res.data.error) || "gagal"));
    }
  });

  $("#btnUbahPw").addEventListener("click", async () => {
    const lama = $("#pfPwLama", view()).value, baru = $("#pfPwBaru", view()).value;
    if (!lama || !baru) { pfMsg("pfPwMsg", false, "isi password lama & barunya 😄"); return; }
    if (baru.length < 8) { pfMsg("pfPwMsg", false, "password baru minimal 8 karakter ya 😄"); return; }
    const btn = $("#btnUbahPw"); setLoading(btn, true, "Nyimpen...");
    const res = await apiPOST("/auth/ubah-password", { password_lama: lama, password_baru: baru });
    setLoading(btn, false);
    if (res.ok) {
      pfMsg("pfPwMsg", true, (res.data && res.data.msg) || "password diganti! 🎉");
      $("#pfPwLama", view()).value = ""; $("#pfPwBaru", view()).value = "";
    } else {
      pfMsg("pfPwMsg", false, "😅 " + ((res.data && res.data.error) || "gagal"));
    }
  });

  $("#btnUbahMail").addEventListener("click", async () => {
    const v = $("#pfMailBaru", view()).value.trim();
    if (!v) { pfMsg("pfMailMsg", false, "isi email barunya dulu 😄"); return; }
    const pw = $("#pfMailPw", view()).value;
    if (!pw) { pfMsg("pfMailMsg", false, "isi password buat konfirmasi 😄"); return; }
    const btn = $("#btnUbahMail"); setLoading(btn, true, "Ngirim...");
    const res = await apiPOST("/auth/ubah-email", { email_baru: v, password: pw });
    setLoading(btn, false);
    $("#pfMailPw", view()).value = "";
    if (res.ok) {
      pfMsg("pfMailMsg", true, (res.data && res.data.msg) || "link terkirim! 📧");
      $("#pfMailBaru", view()).value = "";
    } else {
      pfMsg("pfMailMsg", false, "😅 " + ((res.data && res.data.error) || "gagal"));
    }
  });
}

/* ----- Konfirmasi ganti email (dari link email) ----- */
function pageKonfirmasiEmail() {
  const token = new URLSearchParams((location.hash.split("?")[1] || "")).get("token") || "";
  view().innerHTML = "<h2>"+tic("mail")+" Konfirmasi Email</h2>" +
    '<div class="card center"><p class="loading">Lagi konfirmasi... ⏳</p></div>';
  (async () => {
    if (!token) {
      view().innerHTML = "<h2>"+tic("mail")+" Konfirmasi Email</h2>" +
        '<div class="err-box">Link konfirmasinya nggak lengkap 😅</div>';
      return;
    }
    const res = await apiJSON("/auth/konfirmasi-email?token=" + encodeURIComponent(token));
    if (res.ok) {
      await refreshAuth();
      view().innerHTML = "<h2>"+tic("check")+" Email Diganti!</h2>" +
        '<div class="card center"><p>Email akunmu berhasil diganti! 🎉</p>' +
        '<a class="btn btn-primary btn-block" href="#/akun">Ke Akun Saya</a></div>';
    } else {
      view().innerHTML = "<h2>"+tic("mail")+" Konfirmasi Email</h2>" +
        '<div class="err-box">😅 ' + esc((res.data && res.data.error) || "konfirmasi gagal") + "</div>" +
        '<div class="card center"><p class="muted">Link mungkin sudah dipakai / kedaluwarsa (24 jam). ' +
        'Minta link baru dari halaman Akun Saya ya.</p><a class="btn btn-primary" href="#/akun">Ke Akun Saya</a></div>';
    }
  })();
}

/* Etalase postingan media sosial KhairyPustaka — marquee CSS murni.
   Pengganti kartu pengumuman lama di beranda.
   Cara menambah item baru: tambah satu objek {platform, judul, url, img}
   di bawah ini, lalu taruh file gambarnya di static/showcase/.
   Kalau tidak ada gambar yang cocok, kosongkan img ("") — otomatis
   jadi kartu teks bergaya yang tetap bisa diklik. */
const SHOWCASE_ITEMS = [
  { platform: "Instagram", judul: "Pilot: AI buat nugas, teman atau jebakan?", url: "https://www.instagram.com/reel/DebEwzCj9St/", img: "showcase/cover-pilot.jpg" },
  { platform: "Instagram", judul: "EP02: Salah jurusan?", url: "https://www.instagram.com/reel/DedeF0GkxT2/", img: "showcase/cover-ep02.jpg" },
  { platform: "Instagram", judul: "EP03: Jurnal predator", url: "https://www.instagram.com/reel/DegC3j-j5Z6/", img: "showcase/cover-ep03.jpg" },
  { platform: "Instagram", judul: "EP04: Sidang skripsi", url: "https://www.instagram.com/reel/DelMbxkE8hH/", img: "showcase/cover-ep04.jpg" },
  { platform: "Threads", judul: "Pilot: AI buat nugas, teman atau jebakan?", url: "https://www.threads.com/@khairypustaka/post/DebFzE7jVfo", img: "showcase/cover-pilot.jpg" },
  { platform: "Threads", judul: "EP02: Salah jurusan?", url: "https://www.threads.com/@khairypustaka/post/DedeBZDjWye", img: "showcase/cover-ep02.jpg" },
  { platform: "Threads", judul: "EP03: Jurnal predator", url: "https://www.threads.com/@khairypustaka/post/DegC0mADRM7", img: "showcase/cover-ep03.jpg" },
  { platform: "Threads", judul: "EP04: Sidang skripsi", url: "https://www.threads.com/@khairypustaka/post/DelMZiTjSZy", img: "showcase/cover-ep04.jpg" },
  { platform: "Threads", judul: "Tips memilih uji statistik", url: "https://www.threads.com/@khairypustaka/post/Del9211Fi4W", img: "showcase/harian-threads-statistik.webp" },
  { platform: "X", judul: "Pilot: AI buat nugas, teman atau jebakan?", url: "https://x.com/KhairyPustaka/status/2109883741069844871", img: "showcase/cover-pilot.jpg" },
  { platform: "X", judul: "EP02: Salah jurusan?", url: "https://x.com/KhairyPustaka/status/2110218334688166193", img: "showcase/cover-ep02.jpg" },
  { platform: "X", judul: "EP03: Jurnal predator", url: "https://x.com/KhairyPustaka/status/2110580638860583109", img: "showcase/cover-ep03.jpg" },
  { platform: "X", judul: "EP04: Sidang skripsi", url: "https://x.com/KhairyPustaka/status/2111180868404629997", img: "showcase/cover-ep04.jpg" },
  { platform: "X", judul: "EP05: Nyari jurnal jangan cuma Google", url: "https://x.com/KhairyPustaka/status/2111305714472014119", img: "showcase/cover-ep05.jpg" },
  { platform: "X", judul: "Update KhairyPustaka minggu ini", url: "https://x.com/KhairyPustaka/status/2111308490987778428", img: "showcase/harian-x-update.webp" }
];

function showcaseHTML() {
  // Acak urutan tiap halaman dimuat (Fisher-Yates).
  var items = SHOWCASE_ITEMS.slice();
  for (var i = items.length - 1; i > 0; i--) {
    var j = Math.floor(Math.random() * (i + 1));
    var tmp = items[i]; items[i] = items[j]; items[j] = tmp;
  }
  function kartu(it) {
    var isi = it.img
      ? '<img src="' + esc(it.img) + '" alt="' + esc(it.judul) + '" loading="lazy">'
      : '<span class="kp-show-teks"><b>' + esc(it.platform) + "</b><i>" + esc(it.judul) + "</i></span>";
    return '<a class="kp-show-item" href="' + esc(it.url) + '" target="_blank" rel="noopener">' +
      isi + '<span class="kp-show-label">' + esc(it.platform) + "</span>" +
      '<span class="kp-show-judul">' + esc(it.judul) + "</span></a>";
  }
  var sekali = items.map(kartu).join("");
  return sekali + sekali; // digandakan agar loop marquee mulus
}

function pageBeranda() {
  const KATS = [
    ["populer", tic("star")+" Populer", [
      ["#/statistik", "chart", "Statistik", "Panduan pilih uji statistik"],
      ["#/cari", "search", "Cari Jurnal", "OpenAlex, Crossref, Sinta, Garuda"],
      ["#/deadline", "alarm", "Deadline", "Catat tenggat tugas & skripsimu"],
      ["#/sitasi", "quote", "Buat Sitasi", "Sitasi 6 gaya otomatis"]
    ]],
    ["riset", tic("search")+" Riset", [
      ["#/cari", "search", "Cari Jurnal", "OpenAlex, Crossref, Sinta, Garuda"],
      ["#/carifile", "doc", "Cari dari Sitasi", "Paste sitasi, cari filenya"],
      ["#/rekomendasi", "target", "Rekomendasi Jurnal", "Jurnal buat publikasi"],
      ["#/publisher", "building", "Publisher", "Jelajahi penerbit & jurnal"],
      ["#/penulis", "person", "Info Penulis", "H-index, karya, afiliasi"],
      ["#/matriks", "grid", "Matriks", "Tabel sintesis literatur"]
    ]],
    ["ai", tic("spark")+" AI", [
      ["#/ai", "spark", "AI Studio", "10 generator naskah tugas & skripsi"],
      ["#/tanya", "chat", "Tanya AI", "Tanya apa aja soal tugas & skripsi"],
      ["#/parafrase", "refresh", "Parafrase", "Ubah kalimat biar fresh"],
      ["#/topik", "bulb", "Ide Topik", "Tren & research gap"]
    ]],
    ["nulis", tic("pencil")+" Nulis", [
      ["#/sitasi", "quote", "Buat Sitasi", "Sitasi 6 gaya otomatis"],
      ["#/template", "layout", "Template", "Template proposal & skripsi"],
      ["#/statistik", "chart", "Statistik", "Panduan pilih uji statistik"]
    ]],
    ["kelola", tic("folder")+" Kelola", [
      ["#/koleksi", "books", "Koleksiku", "Simpan & export referensi"],
      ["#/favorit", "star", "Favorit", "Pantau jurnal favoritmu"],
      ["#/deadline", "alarm", "Deadline", "Catat tenggat tugas & skripsimu"],
      ["#/jadwal", "calendar", "Jadwal", "Timeline mundur skripsi"]
    ]],
    ["akun", tic("bolt")+" Akun", [
      ["#/akun", "usercard", "Akun Saya", "Login & profil"],
      ["#/topup", "bolt", "Jatah AI", "Top-up & redeem jatah AI"],
      ["#/bantuan", "help", "Bantuan", "Cara pakai & FAQ"]
    ]]
  ];
  let aktif = "populer";
  try {
    const t = sessionStorage.getItem("kp_home_tab");
    if (t && KATS.some(k => k[0] === t)) aktif = t; // tab terakhir diingat (tombol Kembali)
  } catch (e) {}
  function gridHTML() {
    const feats = KATS.find(k => k[0] === aktif)[2];
    return feats.map((f, i) =>
      '<a class="feat" style="animation-delay:' + (i * 55) + 'ms" href="' + f[0] + '">' +
      '<span class="ico">' + tic(f[1]) + "</span><b>" + f[2] + "</b><small>" + f[3] + "</small></a>"
    ).join("");
  }
  view().innerHTML =
    '<section class="hero">' +
      '<img src="maskot.jpg" class="hero-maskot" alt="Maskot KhairyPustaka">' +
      "<h1>KhairyPustaka</h1>" +
      "<p>Asisten skripsi & nugas anti begadang: cari jurnal, bikin sitasi, sampai nulis bab — semua dari HP.</p>" +
      '<a class="btn btn-accent" href="#/cari">' + tic("search") + ' Mulai Cari Jurnal</a> ' +
    "</section>" +
    '<div class="filter-row" role="tablist">' +
      KATS.map(k =>
        '<button class="filter-chip' + (k[0] === aktif ? " active" : "") + '" data-kat="' + k[0] + '">' + k[1] + "</button>"
      ).join("") +
    "</div>" +
    '<div class="grid" id="featGrid">' + gridHTML() + "</div>" +
    '<div class="card kp-show-card" style="margin-top:16px">' +
      "<h3>"+tic("spark")+" Postingan terbaru KhairyPustaka</h3>" +
      '<p class="muted">Video dan postingan edukasi dari akun resmi kami. Ketuk gambarnya buat buka langsung. 🎓</p>' +
      '<div class="kp-show-strip" id="kpShowStrip"><div class="kp-show-track" id="kpShowTrack">' + showcaseHTML() + "</div></div>" +
    "</div>";
  $$("#view .filter-chip").forEach(ch => ch.addEventListener("click", () => {
    aktif = ch.dataset.kat;
    try { sessionStorage.setItem("kp_home_tab", aktif); } catch (e) {}
    $$("#view .filter-chip").forEach(c => c.classList.toggle("active", c === ch));
    $("#featGrid").innerHTML = gridHTML();
  }));
  // Etalase: jeda animasi saat disentuh, lanjut lagi setelah dilepas.
  var strip = $("#kpShowStrip"), track = $("#kpShowTrack"), tmrJeda = null;
  if (strip && track) {
    strip.addEventListener("touchstart", function () {
      track.style.animationPlayState = "paused";
      if (tmrJeda) clearTimeout(tmrJeda);
    }, { passive: true });
    strip.addEventListener("touchend", function () {
      if (tmrJeda) clearTimeout(tmrJeda);
      tmrJeda = setTimeout(function () { track.style.animationPlayState = ""; }, 1600);
    }, { passive: true });
  }
}

/* ----- Cari Jurnal ----- */
let cariState = { tahun: 0, hasil: [] };
const TAHUN_CHIPS = [[5, tic("clock")+" 5 thn terakhir"], [10, tic("calendar")+" 10 thn terakhir"], [0, tic("globe")+" Semua"]];

function pageCari() {
  view().innerHTML =
    "<h2>"+tic("search")+" Cari Jurnal</h2>" +
    '<div class="chip-row">' +
      TAHUN_CHIPS.map(c =>
        '<button class="filter-chip' + (cariState.tahun === c[0] ? " active" : "") +
        '" data-thn="' + c[0] + '">' + c[1] + "</button>").join("") +
    "</div>" +
    '<div class="card"><label for="q">Kata kunci</label>' +
    '<input type="text" id="q" placeholder="cth: stunting balita, employee performance, ..." autocomplete="off">' +
    '<button class="btn btn-primary btn-block" id="btnCari" style="margin-top:12px">🔍 Cari</button></div>' +
    '<div id="hasilCari">' + (cariState.hasil.length ? "" : '<p class="empty">Ketik kata kunci terus tekan Cari. Jurnal dari OpenAlex, Crossref, Sinta &amp; Garuda siap membantu. 🚀</p>') + "</div>";

  if (cariState.hasil.length) renderHasilCari();

  $$(".filter-chip", view()).forEach(ch => ch.addEventListener("click", () => {
    cariState.tahun = parseInt(ch.dataset.thn, 10); cariState.hasil = [];
    pageCari();
  }));
  const go = () => doCari();
  $("#btnCari").addEventListener("click", go);
  $("#q").addEventListener("keydown", e => { if (e.key === "Enter") go(); });
}

function renderHasilCari() {
  const box = $("#hasilCari");
  if (!cariState.hasil.length) { box.innerHTML = '<p class="empty">Nggak ketemu hasil. Coba kata kunci lain ya. 😅</p>'; return; }
  const cat = Array.isArray(cariState.catatan) && cariState.catatan.length
    ? '<div class="warn-box tiny">' + cariState.catatan.map(esc).join("<br>") + "</div>" : "";
  box.innerHTML = cat +
    '<p class="muted">Ditemukan <b>' + cariState.hasil.length + "</b> hasil 🎉</p>" +
    cariState.hasil.map((h, i) => kartuHasil(h, i)).join("");
  wireKartu(box);
}

async function doCari() {
  const q = ($("#q").value || "").trim();
  if (!q) { toast("Ketik dulu kata kuncinya dong 😄"); return; }
  const box = $("#hasilCari");
  box.innerHTML = '<p class="loading">Lagi nyari jurnal terbaik buatmu... ⏳</p>';
  const btn = $("#btnCari"); setLoading(btn, true, "Lagi nyari...");
  const res = await apiPOST("/cari", { q: q, tahun: cariState.tahun });
  setLoading(btn, false);
  if (!res.ok) { netErr(box, res); return; }
  const d = res.data || {};
  const arr = Array.isArray(d.hasil) ? d.hasil : (Array.isArray(d.data) ? d.data : (Array.isArray(d) ? d : []));
  cariState.hasil = arr;
  cariState.catatan = Array.isArray(d.catatan) ? d.catatan : [];
  renderHasilCari();
  // R5 (audit 2026-10-13): jangan catat entri zonk (0 hasil)
  if (arr.length) riwTamuCatat("jurnal", q, { q: q, tahun: cariState.tahun, hasil: arr.slice(0, 20), catatan: cariState.catatan });
}

async function ringkasAI(item, btn) {
  const teks = ((item.judul || "") + "\n\n" + (item.abstrak || "")).trim();
  if (teks.length < 30) { toast("Jurnal ini nggak punya abstrak buat diringkas 😅"); return; }
  setLoading(btn, true, "AI meringkas...");
  const res = await apiPOST("/ai", { kind: "ringkas", text: teks.slice(0, 3000) });
  setLoading(btn, false);
  const d = res.data || {};
  if (!res.ok && (d.error === "kuota_habis" || d.kuota_habis)) { toast("Kuota AI harianmu habis 🥲 Login/besok reset ya"); return; }
  if (!res.ok) { toast("AI-nya lagi gangguan, coba lagi ya 😅"); return; }
  const hasil = d.hasil || d.result || d.text;
  if (!hasil) { toast("AI-nya bengong, coba lagi ya 😅"); return; }
  const card = btn.closest("article");
  if (card) {
    let box = card.querySelector(".ringkas-ai");
    if (!box) { box = document.createElement("div"); box.className = "ringkas-ai"; card.appendChild(box); }
    box.innerHTML = '<div class="hasil-ai">' + mdRapi(hasil) + "</div>";
  }
}

async function terjemahAbstrak(item, btn) {
  const card = btn.closest("article");
  const det = card ? card.querySelector(".abstrak") : null;
  const p = det ? det.querySelector("p") : null;
  if (!p) { toast("Jurnal ini nggak punya abstrak 😅"); return; }
  if (btn.dataset.mode === "asli") {
    p.textContent = btn.dataset.asli || p.textContent;
    btn.innerHTML = "🌐 Terjemah";
    btn.dataset.mode = "";
    return;
  }
  if (btn.dataset.terjemahan) {
    btn.dataset.asli = p.textContent;
    p.textContent = btn.dataset.terjemahan;
    btn.innerHTML = "↩️ Lihat Asli";
    btn.dataset.mode = "asli";
    det.open = true;
    return;
  }
  const teks = (item.abstrak || "").trim();
  if (teks.length < 20) { toast("Abstraknya terlalu pendek buat diterjemahkan 😅"); return; }
  setLoading(btn, true, "Menerjemahkan...");
  const res = await apiPOST("/terjemah", { teks: teks.slice(0, 3000) });
  setLoading(btn, false);
  const d = res.data || {};
  if (!res.ok) { toast("Terjemahannya gagal, coba lagi ya 😅"); return; }
  if (d.sumber === "sudah_indonesia") { toast("Abstraknya sudah berbahasa Indonesia, tidak perlu diterjemahkan 😉"); return; }
  if (!d.hasil) { toast("Terjemahannya kosong, coba lagi ya 😅"); return; }
  btn.dataset.terjemahan = d.hasil;
  btn.dataset.asli = p.textContent;
  p.textContent = d.hasil;
  btn.innerHTML = "↩️ Lihat Asli";
  btn.dataset.mode = "asli";
  det.open = true;
  toast(d.sumber === "mymemory" ? "Diterjemahkan 🤖 (terjemahan mesin)" : "Diterjemahkan 🤖 (terjemahan offline)");
}

function wireKartu(scope) {
  $$("[data-aksi]", scope).forEach(b => b.addEventListener("click", () => {
    const i = parseInt(b.dataset.idx, 10);
    const item = cariState.hasil[i];
    if (!item) return;
    if (b.dataset.aksi === "sitasi") bukaModalSit(item);
    else if (b.dataset.aksi === "ringkas") ringkasAI(item, b);
    else if (b.dataset.aksi === "terjemah") terjemahAbstrak(item, b);
    else if (b.dataset.aksi === "simpan") {
      const r = koleksiTambah(item);
      toast(r === "ok" ? "Kesimpen di Koleksiku! 💾" : (r === "duplikat" ? "Udah ada di koleksimu 😉" : "Gagal nyimpen (penyimpanan browser diblokir?). 😅"));
    }
  }));
}

/* ----- Cari dari Sitasi ----- */
let carifileHasil = []; // state lokal halaman ini (jangan pakai cariState)
function pageCarifile() {
  view().innerHTML =
    "<h2>"+tic("doc")+" Cari dari Sitasi</h2>" +
    '<div class="card"><p class="muted">Punya sitasi tapi nggak tahu filenya di mana? Paste aja di sini, biar aku yang lacak. 🕵️</p>' +
    '<label for="teksSit">Teks sitasi</label>' +
    '<textarea id="teksSit" placeholder="cth: Sugiyono. (2019). Metode Penelitian Kuantitatif. Bandung: Alfabeta."></textarea>' +
    '<button class="btn btn-primary btn-block" id="btnCarifile" style="margin-top:12px">🔎 Lacak File</button></div>' +
    '<div id="hasilCarifile"></div>';
  $("#btnCarifile").addEventListener("click", async () => {
    const teks = ($("#teksSit").value || "").trim();
    if (!teks) { toast("Paste dulu sitasinya 😄"); return; }
    const box = $("#hasilCarifile");
    box.innerHTML = '<p class="loading">Lagi ngelacak... ⏳</p>';
    const btn = $("#btnCarifile"); setLoading(btn, true, "Lagi ngelacak...");
    const res = await apiPOST("/carifile", { teks: teks });
    setLoading(btn, false);
    if (!res.ok) { netErr(box, res); return; }
    const d = res.data || {};
    let html = "";
    // backend: {"hasil": [array karya], "tidak_ketemu": [...]}
    const found = Array.isArray(d.hasil) ? d.hasil : [];
    if (found.length) {
      carifileHasil = found;
      html += '<p class="muted">Ditemukan <b>' + found.length + "</b> karya 🎉</p>" +
        found.map((h, i) => kartuHasil(h, "cf" + i)).join("");
    }
    const miss = Array.isArray(d.tidak_ketemu) ? d.tidak_ketemu : [];
    if (miss.length) {
      html += '<div class="card"><h3>Tidak ketemu 🔍</h3><p class="muted tiny">Sitasi di bawah ini nggak kelacak sumbernya:</p><ul class="bersih">' +
        miss.map(m => "<li>" + esc(typeof m === "string" ? m : JSON.stringify(m)) + "</li>").join("") + "</ul></div>";
    }
    if (d.parsial) html += '<p class="muted tiny">⏱️ Sebagian sitasi belum diproses (waktu habis) — coba lagi dengan teks lebih pendek.</p>';
    if (!html) html = '<div class="warn-box">Hmm, belum nemu petunjuk file dari sitasi itu. 🤔 Coba cek lagi teksnya udah lengkap atau belum.</div>';
    else if (d.pesan || d.message) html += '<p class="muted">' + esc(d.pesan || d.message) + "</p>";
    box.innerHTML = html;
    // S3 (audit 2026-10-13): Cari dari Sitasi = pencarian jurnal -> catat riwayat tamu
    if (found.length) riwTamuCatat("jurnal", "Cari dari Sitasi: " + teks.slice(0, 60),
      { q: teks.slice(0, 80), sumber: "carifile", hasil: found.slice(0, 20), tidak_ketemu: miss });
    // wire tombol Sitasi/Simpan ke state lokal (bukan cariState)
    $$("[data-aksi]", box).forEach(b => b.addEventListener("click", () => {
      const i = parseInt(String(b.dataset.idx || "").replace(/^cf/, ""), 10);
      const item = carifileHasil[i];
      if (!item) return;
      if (b.dataset.aksi === "sitasi") bukaModalSit(item);
      else if (b.dataset.aksi === "ringkas") ringkasAI(item, b);
      else if (b.dataset.aksi === "terjemah") terjemahAbstrak(item, b);
      else if (b.dataset.aksi === "simpan") {
        const r = koleksiTambah(item);
        toast(r === "ok" ? "Kesimpen di Koleksiku! 💾" : (r === "duplikat" ? "Udah ada di koleksimu 😉" : "Gagal nyimpen (penyimpanan browser diblokir?). 😅"));
      }
    }));
  });
}

/* ----- daftar generik ----- */
function daftarItemHTML(x) {
  if (typeof x === "string") return "<li>" + esc(x) + "</li>";
  if (x && typeof x === "object") {
    // backend rekomendasi/publisher mengirim key Inggris (name, field, publisher...)
    const nama = esc(x.nama || x.judul || x.title || x.name || "(tanpa nama)");
    const det = [x.bidang || x.field, x.indeks, x.skor, x.penerbit || x.publisher, x.issn, x.deskripsi || x.description, x.alasan || x.reason]
      .filter(Boolean).map(esc).join(" · ");
    const url = safeUrl(x.url || x.link || x.website || x.homepage);
    const isi = (url ? '<a href="' + esc(url) + '" target="_blank" rel="noopener"><b>' + nama + "</b></a>" : "<b>" + nama + "</b>") +
      (det ? "<br><span class='muted tiny'>" + det + "</span>" : "");
    return "<li>" + isi + "</li>";
  }
  return "";
}

function pageRekomendasi() {
  view().innerHTML =
    "<h2>"+tic("target")+" Rekomendasi Jurnal</h2>" +
    '<div class="card"><p class="muted">Mau publikasi? Kasih tahu topikmu, aku carikan jurnal yang cocok. 📝</p>' +
    '<label for="topikRek">Topik / judul penelitian</label>' +
    '<input type="text" id="topikRek" placeholder="cth: pengaruh digital marketing terhadap UMKM">' +
    '<button class="btn btn-primary btn-block" id="btnRek" style="margin-top:12px">🎯 Carikan Jurnal</button></div>' +
    '<div id="hasilRek"></div>';
  $("#btnRek").addEventListener("click", async () => {
    const topik = ($("#topikRek").value || "").trim();
    if (!topik) { toast("Isi dulu topiknya 😄"); return; }
    const box = $("#hasilRek");
    box.innerHTML = '<p class="loading">Lagi nyari jurnal yang cocok... ⏳</p>';
    const btn = $("#btnRek"); setLoading(btn, true, "Lagi nyari...");
    const res = await apiPOST("/rekomendasi", { topik: topik });
    setLoading(btn, false);
    if (!res.ok) { netErr(box, res); return; }
    const d = res.data || {};
    const daftar = Array.isArray(d.daftar) ? d.daftar : (Array.isArray(d.hasil) ? d.hasil : []);
    box.innerHTML = daftar.length
      ? '<div class="card"><h3>Jurnal yang cocok buatmu 🎉</h3><ul class="bersih">' + daftar.map(daftarItemHTML).join("") + "</ul></div>"
      : '<div class="warn-box">Belum nemu rekomendasi buat topik itu. 😅 Coba kata kunci yang lebih umum ya.</div>';
  });
}

function pagePublisher() {
  view().innerHTML =
    "<h2>"+tic("building")+" Publisher</h2>" +
    '<div class="card"><p class="muted">Jelajahi penerbit &amp; jurnal per indeks. Tulis bidangnya dulu ya. 📚</p>' +
    '<label for="bidangPub">Bidang *</label>' +
    '<input type="text" id="bidangPub" placeholder="cth: kesehatan, pendidikan, teknik">' +
    '<button class="btn btn-primary btn-block" id="btnPub" style="margin-top:12px">🏢 Tampilkan</button></div>' +
    '<div id="hasilPub"></div>';
  $("#btnPub").addEventListener("click", async () => {
    const bidang = ($("#bidangPub").value || "").trim();
    if (!bidang) { toast("Isi dulu bidangnya 😄"); return; }
    const box = $("#hasilPub");
    box.innerHTML = '<p class="loading">Lagi ngumpulin data publisher... ⏳</p>';
    const btn = $("#btnPub"); setLoading(btn, true, "Lagi ngumpulin...");
    const res = await apiPOST("/publisher", { bidang: bidang });
    setLoading(btn, false);
    if (!res.ok) { netErr(box, res); return; }
    const d = res.data || {};
    const daftar = Array.isArray(d.daftar) ? d.daftar : [];
    box.innerHTML = daftar.length
      ? '<div class="card"><h3>Daftar publisher &amp; jurnal 🏢</h3><ul class="bersih">' + daftar.map(daftarItemHTML).join("") + "</ul></div>"
      : '<div class="warn-box">Datanya lagi kosong. 😅 Coba lagi nanti ya.</div>';
  });
}

function pagePenulis() {
  view().innerHTML =
    "<h2>"+tic("person")+" Info Penulis</h2>" +
    '<div class="card"><p class="muted">Kepoin profil peneliti: H-index, karya, sitasi, afiliasi. 🔎</p>' +
    '<label for="namaPen">Nama penulis</label>' +
    '<input type="text" id="namaPen" placeholder="cth: Budi Santoso">' +
    '<button class="btn btn-primary btn-block" id="btnPen" style="margin-top:12px">👤 Cari Penulis</button></div>' +
    '<div id="hasilPen"></div>';
  $("#btnPen").addEventListener("click", async () => {
    const nama = ($("#namaPen").value || "").trim();
    if (!nama) { toast("Isi dulu namanya 😄"); return; }
    const box = $("#hasilPen");
    box.innerHTML = '<p class="loading">Lagi nyari profilnya... ⏳</p>';
    const btn = $("#btnPen"); setLoading(btn, true, "Lagi nyari...");
    const res = await apiPOST("/penulis", { nama: nama });
    setLoading(btn, false);
    if (!res.ok) { netErr(box, res); return; }
    const d = res.data || {};
    const list = Array.isArray(d.daftar) ? d.daftar : (Array.isArray(d) ? d : []);
    if (!list.length) {
      box.innerHTML = '<div class="warn-box">Penulisnya nggak ketemu. 😅 Coba ejaan nama yang lain ya.</div>';
      return;
    }
    box.innerHTML = '<p class="muted">Ditemukan <b>' + list.length + "</b> penulis 🎉</p>" +
      list.map((a, i) => kartuPenulis(a, i)).join("");
  });
}

function kartuPenulis(a) {
  a = a || {};
  const afil = (a.affiliations || []).filter(Boolean).join(", ");
  const topik = (a.topics || []).filter(Boolean).join(", ");
  const profUrl = safeUrl(a.url);
  return '<div class="card"><h3>👤 ' + esc(a.name || "-") + "</h3>" +
    '<ul class="bersih">' +
    (afil ? "<li>🏛️ <b>Afiliasi:</b> " + esc(afil) + "</li>" : "") +
    (a.h_index != null ? "<li>📊 <b>H-index:</b> " + esc(a.h_index) + "</li>" : "") +
    (a.cited_by_count != null ? "<li>📑 <b>Total sitasi:</b> " + esc(a.cited_by_count) + "</li>" : "") +
    (a.works_count != null ? "<li>📚 <b>Jumlah karya:</b> " + esc(a.works_count) + "</li>" : "") +
    (a.i10_index != null ? "<li>🔟 <b>i10-index:</b> " + esc(a.i10_index) + "</li>" : "") +
    (topik ? "<li>🏷️ <b>Topik:</b> " + esc(topik) + "</li>" : "") +
    "</ul>" +
    (profUrl ? '<p class="tiny"><a href="' + esc(profUrl) + '" target="_blank" rel="noopener">Lihat profil lengkap ↗</a></p>' : "") +
    "</div>";
}

/* ----- Ide Topik ----- */
function pageTopik() {
  view().innerHTML =
    "<h2>"+tic("bulb")+" Ide Topik</h2>" +
    '<div class="card"><p class="muted">Buntu ide? Kasih keyword, aku kasih tren publikasi + saran topik &amp; research gap. 🧠</p>' +
    '<label for="kwTopik">Keyword</label>' +
    '<input type="text" id="kwTopik" placeholder="cth: kecerdasan buatan pendidikan">' +
    '<button class="btn btn-primary btn-block" id="btnTopik" style="margin-top:12px">💡 Kasih Ide Dong</button></div>' +
    '<div id="hasilTopik"></div>';
  $("#btnTopik").addEventListener("click", async () => {
    const keyword = ($("#kwTopik").value || "").trim();
    if (!keyword) { toast("Isi dulu keywordnya 😄"); return; }
    const box = $("#hasilTopik");
    box.innerHTML = '<p class="loading">Lagi ngulik tren publikasi... ⏳</p>';
    const btn = $("#btnTopik"); setLoading(btn, true, "Lagi ngulik...");
    const res = await apiPOST("/topik", { keyword: keyword });
    setLoading(btn, false);
    if (!res.ok) { netErr(box, res); return; }
    const d = res.data || {};
    if (d.kuota_habis || d.error === "kuota_habis") {
      box.innerHTML = '<div class="warn-box">Yah, kuota AI harianmu habis. 🥲 Tenang, besok reset lagi kok! Sementara pakai fitur non-AI dulu ya. 💪</div>';
      return;
    }
    let html = "";
    const tren = d.tren || d.trends;
    if (tren) html += '<div class="card"><h3>Tren publikasi 📈</h3>' + trenHtml(tren) + "</div>";
    // d.ide = string ide judul (selalu ada bila sukses); d.saran = array topik
    // alternatif (sering [] kosong yang truthy!) — render ide TERPISAH agar
    // tidak tertutup array kosong.
    if (typeof d.ide === "string" && d.ide)
      html += '<div class="card"><h3>Ide judul dari AI 💡</h3><div>' + mdRapi(d.ide) + "</div></div>";
    const saran = (Array.isArray(d.saran) && d.saran.length) ? d.saran : (d.topik || []);
    if (Array.isArray(saran) && saran.length)
      html += '<div class="card"><h3>Saran topik &amp; research gap 💡</h3><ul class="bersih">' + saran.map(s => "<li>" + esc(typeof s === "string" ? s : (s.judul || s.topik || JSON.stringify(s))) + "</li>").join("") + "</ul></div>";
    if (!html) html = '<div class="warn-box">Belum ada ide yang keluar. 😅 Coba keyword lain ya.</div>';
    box.innerHTML = html;
    // R5 (audit 2026-10-13): jangan catat entri zonk
    if (d.ide || saran.length) riwTamuCatat("topik", keyword, { keyword: keyword, ide: d.ide, saran: saran, gaps: d.gaps });
  });
}

/* Render tren publikasi sebagai daftar bar terbaca (BUKAN JSON mentah).
   Bentuk data backend: array [nama_topik, jumlah] dari OpenAlex.
   Defensif: terima juga array objek / objek map / string. */
function trenHtml(tren) {
  if (typeof tren === "string")
    return '<p style="white-space:pre-wrap">' + esc(tren) + "</p>";
  let items = [];
  if (Array.isArray(tren)) {
    items = tren.map(t => Array.isArray(t)
      ? [t[0], t[1]]
      : [(t && (t.nama || t.topik || t.name)) || "", (t && (t.jumlah != null ? t.jumlah : t.count)) || 0]);
  } else if (tren && typeof tren === "object") {
    items = Object.keys(tren).map(k => [k, tren[k]]);
  }
  items = items.filter(x => x[0]);
  if (!items.length)
    return '<p class="muted">Belum ada data tren untuk keyword ini.</p>';
  const max = Math.max.apply(null, items.map(x => Number(x[1]) || 0).concat([1]));
  return items.map(x => {
    const n = Number(x[1]) || 0;
    const w = Math.max(6, Math.round(n / max * 100));
    return '<div class="tren-row"><span class="tren-nama">' + esc(String(x[0])) +
      '</span><span class="tren-bar"><i style="width:' + w + '%"></i></span><b>' + n + "</b></div>";
  }).join("");
}

/* ----- Koleksi ----- */
function pageKoleksi() {
  const arr = koleksiGet();
  view().innerHTML =
    "<h2>"+tic("books")+" Koleksiku</h2>" +
    '<p class="muted">Tersimpan di browser HP-mu (tanpa login). Total: <b>' + arr.length + "</b> referensi.</p>" +
    (arr.length ?
      '<div class="card"><h3>Export ⬇️</h3><p class="tiny muted">Pilih format, filenya langsung keunduh.</p>' +
      '<button class="btn" data-exp="txt">TXT</button>' +
      '<button class="btn" data-exp="docx">DOCX</button>' +
      '<button class="btn" data-exp="bibtex">BibTeX</button></div>' : "") +
    '<div id="listKoleksi">' +
      (arr.length ? arr.map((it, i) =>
        '<div class="card hasil"><h3>' + esc(it.judul || "(tanpa judul)") + "</h3>" +
        '<div class="meta">' + esc(Array.isArray(it.penulis) ? it.penulis.join(", ") : (it.penulis || "-")) +
        " | " + esc(it.tahun || "-") + " | " + esc(it.jurnal || "-") + "</div>" +
        '<div class="aksi-row"><button class="btn btn-danger" data-hapus="' + i + '">Hapus</button></div></div>'
      ).join("") : '<p class="empty">Koleksimu masih kosong. 📭<br>Cari jurnal dulu, terus tekan <b>Simpan ke Koleksi</b> di tiap hasil. <a href="#/cari">Gas cari →</a></p>') +
    "</div>";

  $$("[data-hapus]", view()).forEach(b => b.addEventListener("click", () => {
    if (!confirm("Hapus dari koleksi? Tindakan ini permanen. 🗑️")) return;
    koleksiHapus(parseInt(b.dataset.hapus, 10));
    toast("Dihapus dari koleksi. 🗑️");
    pageKoleksi();
  }));
  $$("[data-exp]", view()).forEach(b => b.addEventListener("click", async () => {
    const format = b.dataset.exp;
    setLoading(b, true, "Lagi nyiapin file...");
    const ext = format === "docx" ? "docx" : (format === "bibtex" ? "bib" : "txt");
    const res = await apiDownload("/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: koleksiGet(), format: format })
    }, "koleksi-khairypustaka." + ext);
    setLoading(b, false);
    toast(res.ok ? "File keunduh! Cek folder download ya. ⬇️" : "Gagal export, coba lagi ya. 😅");
  }));
}

/* ----- Riwayat ----- */
const RIWAYAT_FILTERS = [
  ["", "Semua"], ["chat", "Chat AI"], ["sitasi", "Sitasi"],
  ["jurnal", "Cari Jurnal"], ["topik", "Ide Topik"]
];
const RIWAYAT_TIC = { chat: "chat", sitasi: "quote", jurnal: "search", topik: "bulb" };
const RIWAYAT_NAMA = { chat: "Chat AI", sitasi: "Sitasi", jurnal: "Cari Jurnal", topik: "Ide Topik" };

/* ----- Riwayat tamu: localStorage saja (maks 10 total, tidak ke server) ----- */
const RIW_TAMU_KEY = "kp_riwayat_tamu";
const RIW_TAMU_MAKS = 10;
function riwTamuGet() {
  try { const a = JSON.parse(localStorage.getItem(RIW_TAMU_KEY) || "[]"); return Array.isArray(a) ? a : []; }
  catch (e) { return []; }
}
let _riwTamuGagalPernah = false; // I3: toast gagal simpan cukup sekali
function riwTamuSet(a) {
  try { localStorage.setItem(RIW_TAMU_KEY, JSON.stringify(a)); }
  catch (e) {
    // I3 (audit 2026-10-13): jangan gagal diam-diam
    if (!_riwTamuGagalPernah) {
      _riwTamuGagalPernah = true;
      toast("Riwayat tamu gagal disimpan (penyimpanan browser penuh?) 😅");
    }
  }
}
/* S2 (audit 2026-10-13): ramping data tamu seperti di server (history.py)
   agar localStorage (±5MB) tidak jebol oleh jawaban AI / abstrak penuh. */
function riwRampingData(tipe, data) {
  const d = (data && typeof data === "object") ? Object.assign({}, data) : {};
  const potong = (s, maks) => (typeof s === "string" && s.length > maks)
    ? s.slice(0, maks).trimEnd() + "…" : s;
  if (tipe === "chat") {
    ["jawaban", "hasil", "text", "q"].forEach(k => { if (k in d) d[k] = potong(d[k], 3000); });
  } else if (tipe === "jurnal") {
    if (Array.isArray(d.hasil)) d.hasil = d.hasil.slice(0, 12).map(w => {
      if (w && typeof w === "object") {
        const w2 = Object.assign({}, w);
        ["abstrak", "abstract", "ringkasan"].forEach(k => { if (k in w2) w2[k] = potong(w2[k], 500); });
        return w2;
      }
      return w;
    });
    if (Array.isArray(d.catatan)) d.catatan = d.catatan.slice(0, 10);
  } else if (tipe === "topik") {
    if ("ide" in d) d.ide = potong(d.ide, 3000);
  } else if (tipe === "sitasi") {
    if ("sitasi" in d) d.sitasi = potong(d.sitasi, 2000);
  }
  return d;
}
function _siramRiwAntre() {
  // R2: entri yang dicatat sebelum status auth pasti. Kalau ternyata login,
  // server sudah mencatat via g.user -> antrean tamu dibuang (hindari duplikat).
  const a = _riwAntre; _riwAntre = [];
  if (AUTH.login) return;
  for (const e of a) riwTamuCatat(e[0], e[1], e[2]);
}
function riwTamuCatat(tipe, judul, data) {
  if (!AUTH_SIAP) { _riwAntre.push([tipe, judul, data]); return; } // R2
  if (AUTH.login) return; // tamu saja — user login dicatat server
  const arr = riwTamuGet();
  arr.unshift({
    id: "t" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8),
    tipe: tipe, judul: riwTamuJudul(judul), waktu: new Date().toISOString(),
    data: riwRampingData(tipe, data)
  });
  while (arr.length > RIW_TAMU_MAKS) arr.pop();
  riwTamuSet(arr);
}
function riwTamuJudul(t) {
  t = String(t == null ? "" : t).replace(/\s+/g, " ").trim();
  if (!t) return "(tanpa judul)";
  return t.length > 80 ? t.slice(0, 79).trimEnd() + "…" : t;
}
function riwTamuCatat(tipe, judul, data) {
  if (AUTH.login) return; // tamu saja — user login dicatat server
  const arr = riwTamuGet();
  arr.unshift({
    id: "t" + Date.now().toString(36) + Math.random().toString(36).slice(2, 8),
    tipe: tipe, judul: riwTamuJudul(judul), waktu: new Date().toISOString(), data: data || {}
  });
  while (arr.length > RIW_TAMU_MAKS) arr.pop();
  riwTamuSet(arr);
}
function riwTamuCari(id) {
  const arr = riwTamuGet();
  for (let i = 0; i < arr.length; i++) if (arr[i].id === id) return arr[i];
  return null;
}
let riwayatChatLanjut = null; // {q, jawaban} -> dibuka di halaman Tanya AI

function fmtWaktuRiwayat(iso) {
  try {
    return new Date(iso).toLocaleString("id-ID", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  } catch (e) { return ""; }
}

function riwayatDetailHTML(e) {
  const d = (e && e.data) || {};
  const t = e.tipe;
  if (t === "chat") {
    const q = d.q || d.text || "";
    const j = d.jawaban || d.hasil || "";
    const potong = j.length > 1500;
    // markdown jawaban AI dirender rapi (temuan user 2026-10-13)
    const jHTML = mdRapi(potong ? j.slice(0, 1500) + "…" : j);
    return '<p><b>Kamu:</b></p><p style="white-space:pre-wrap">' + esc(q) + "</p>" +
      '<p><b>AI:</b></p><div>' + jHTML + "</div>" +
      '<button class="btn btn-primary" data-lanjut-chat="' + esc(e.id) + '">Lanjutkan di Tanya AI →</button>';
  }
  if (t === "sitasi") {
    return '<p class="muted">Gaya: <b>' + esc(d.gaya || "-") + "</b></p>" +
      '<div class="sitasi-hasil"><pre>' + esc(d.sitasi || "") + "</pre></div>" +
      '<button class="btn btn-accent" data-salin-sitasi="' + esc(e.id) + '">📋 Salin</button>';
  }
  if (t === "jurnal") {
    const hsl = Array.isArray(d.hasil) ? d.hasil : [];
    return '<p class="muted">Kata kunci: <b>' + esc(d.q || "") + "</b> · " + hsl.length + " hasil</p>" +
      (hsl.length ? hsl.slice(0, 20).map(w => {
        // R3 (audit 2026-10-13): URL eksternal wajib lewat safeUrl()
        const wu = safeUrl(w.url);
        return '<div class="card hasil"><h3>' + esc(w.judul || "(tanpa judul)") + "</h3>" +
        '<div class="meta">' + esc(Array.isArray(w.penulis) ? w.penulis.join(", ") : (w.penulis || "-")) +
        " | " + esc(w.tahun || "-") + " | " + esc(w.jurnal || "-") + "</div>" +
        (wu ? '<a href="' + esc(wu) + '" target="_blank" rel="noopener">Buka sumber ↗</a>' : "") +
        "</div>"; }).join("") : '<p class="empty">Nggak ada hasil tersimpan. 😅</p>');
  }
  if (t === "topik") {
    const saran = Array.isArray(d.saran) ? d.saran : [];
    let h = '<p class="muted">Keyword: <b>' + esc(d.keyword || "") + "</b></p>";
    if (d.ide) h += '<div class="card"><h3>Ide judul dari AI 💡</h3><div>' + mdRapi(d.ide) + "</div></div>";
    if (saran.length) h += '<div class="card"><h3>Saran topik &amp; research gap 💡</h3><ul class="bersih">' +
      saran.map(s => "<li>" + esc(typeof s === "string" ? s : (s.judul || s.topik || JSON.stringify(s))) + "</li>").join("") + "</ul></div>";
    if (!d.ide && !saran.length) h += '<p class="empty">Nggak ada ide tersimpan. 😅</p>';
    return h;
  }
  return "";
}

function pageRiwayat() {
  const isTamu = !AUTH.login; // tamu: data dari localStorage, tanpa request ke server
  let tipe = "", q = "", bukaId = null;
  const namaAktif = () => tipe ? RIWAYAT_NAMA[tipe] : "semua riwayat";

  const bannerTamu = isTamu
    ? '<div class="warn-box" style="margin-bottom:12px">🔒 <b>Mode tamu:</b> riwayatmu disimpen di HP-mu aja (maks 10) — nggak dikirim ke server. <a href="#/masuk"><b>Login gratis</b></a> biar kesimpen permanen tanpa batas. ✨</div>'
    : "";

  view().innerHTML =
    "<h2>"+tic("clock")+" Riwayat</h2>" +
    bannerTamu +
    '<p class="muted">Semua jejak nugas & skripsianmu — chat AI, sitasi, pencarian jurnal, ide topik. Klik <b>Buka</b> buat lihat lagi. 👇</p>' +
    '<div class="chip-row" id="riwFilter">' +
      RIWAYAT_FILTERS.map(f =>
        '<button class="filter-chip' + (f[0] === tipe ? " active" : "") + '" data-tipe="' + f[0] + '">' + f[1] + "</button>"
      ).join("") +
    "</div>" +
    '<div class="card"><label for="riwQ">Cari riwayat</label>' +
    '<input type="text" id="riwQ" placeholder="cth: stunting, sitasi APA, ..." autocomplete="off"></div>' +
    '<div id="listRiwayat"><p class="loading">Lagi ngambil riwayat... ⏳</p></div>' +
    '<div class="center" style="margin:14px 0"><button class="btn btn-danger" id="btnRiwHapusSemua"></button></div>';

  const aturTombolHapus = () => {
    $("#btnRiwHapusSemua").textContent = "🗑️ Hapus " + namaAktif();
  };
  aturTombolHapus();

  const muat = async () => {
    const box = $("#listRiwayat");
    if (!box) return;
    let arr;
    if (isTamu) {
      // tamu: filter client-side dari localStorage, tanpa request server
      const ql = q.trim().toLowerCase();
      arr = riwTamuGet().filter(e =>
        (!tipe || e.tipe === tipe) &&
        (!ql || String(e.judul || "").toLowerCase().indexOf(ql) !== -1));
    } else {
      const qs = "/history?limit=100" +
        (tipe ? "&type=" + encodeURIComponent(tipe) : "") +
        (q ? "&q=" + encodeURIComponent(q) : "");
      const res = await apiJSON(qs);
      if (!res.ok) {
        if (res.data && res.data.need_login) { pageRiwayat(); return; }
        box.innerHTML = '<div class="err-box">Gagal ngambil riwayat, coba refresh ya. 😅</div>';
        return;
      }
      arr = (res.data && res.data.riwayat) || [];
    }
    if (!arr.length) {
      box.innerHTML = '<p class="empty">Masih kosong nih. 📭<br>' +
        (q ? "Coba kata kunci lain ya." : "Pakai fitur-fiturnya dulu — riwayatmu otomatis kecatat di sini. ✨") + "</p>";
      return;
    }
    box.innerHTML = arr.map(e =>
      '<div class="card hasil"><h3>' + tic(RIWAYAT_TIC[e.tipe] || "clock") + " " + esc(e.judul) + "</h3>" +
      '<div class="meta">' + esc(RIWAYAT_NAMA[e.tipe] || e.tipe) + " · " + esc(fmtWaktuRiwayat(e.waktu)) + "</div>" +
      '<div class="aksi-row"><button class="btn" data-riw-buka="' + esc(e.id) + '">Buka</button>' +
      '<button class="btn btn-danger" data-riw-hapus="' + esc(e.id) + '">Hapus</button></div>' +
      '<div class="riw-detail hidden" id="riwDet-' + esc(e.id) + '"></div>' +
      "</div>"
    ).join("");
  };

  $("#listRiwayat").addEventListener("click", async (ev) => {
    const bBuka = ev.target.closest("[data-riw-buka]");
    const bHapus = ev.target.closest("[data-riw-hapus]");
    const bLanjut = ev.target.closest("[data-lanjut-chat]");
    const bSalin = ev.target.closest("[data-salin-sitasi]");
    if (bHapus) {
      if (!confirm("Hapus riwayat ini? Tindakan ini permanen. 🗑️")) return;
      if (isTamu) {
        riwTamuSet(riwTamuGet().filter(e => e.id !== bHapus.dataset.riwHapus));
        toast("Riwayat dihapus. 🗑️"); if (bukaId) bukaId = null; muat();
      } else {
        const r = await apiJSON("/history/" + encodeURIComponent(bHapus.dataset.riwHapus), { method: "DELETE" });
        if (r.ok) { toast("Riwayat dihapus. 🗑️"); if (bukaId) bukaId = null; muat(); }
        else toast("Gagal hapus, coba lagi ya. 😅");
      }
      return;
    }
    if (bLanjut) {
      let dd;
      if (isTamu) {
        dd = ((riwTamuCari(bLanjut.dataset.lanjutChat) || {}).data) || {};
      } else {
        const r = await apiJSON("/history/" + encodeURIComponent(bLanjut.dataset.lanjutChat));
        dd = (r.ok && r.data && r.data.riwayat && r.data.riwayat.data) || {};
      }
      riwayatChatLanjut = { q: dd.q || dd.text || "", jawaban: dd.jawaban || dd.hasil || "" };
      location.hash = "#/tanya";
      return;
    }
    if (bSalin) {
      let dd;
      if (isTamu) {
        dd = ((riwTamuCari(bSalin.dataset.salinSitasi) || {}).data) || {};
      } else {
        const r = await apiJSON("/history/" + encodeURIComponent(bSalin.dataset.salinSitasi));
        dd = (r.ok && r.data && r.data.riwayat && r.data.riwayat.data) || {};
      }
      if (dd.sitasi) {
        try { await navigator.clipboard.writeText(dd.sitasi); toast("Disalin! 📋"); }
        catch (e) { toast("Gagal nyalin 😅"); }
      }
      return;
    }
    if (bBuka) {
      const id = bBuka.dataset.riwBuka;
      const det = document.getElementById("riwDet-" + id);
      if (!det) return;
      if (bukaId === id) {
        det.classList.add("hidden"); bukaId = null; bBuka.textContent = "Buka";
        return;
      }
      $$("#listRiwayat .riw-detail").forEach(x => x.classList.add("hidden"));
      $$("#listRiwayat [data-riw-buka]").forEach(x => { x.textContent = "Buka"; });
      let ent = null;
      if (isTamu) {
        ent = riwTamuCari(id);
      } else {
        const r = await apiJSON("/history/" + encodeURIComponent(id));
        ent = (r.ok && r.data && r.data.riwayat) || null;
      }
      if (!ent) { toast("Gagal buka riwayat 😅"); return; }
      det.innerHTML = riwayatDetailHTML(ent);
      det.classList.remove("hidden");
      bukaId = id; bBuka.textContent = "Tutup";
    }
  });

  $$("#riwFilter .filter-chip").forEach(ch => ch.addEventListener("click", () => {
    tipe = ch.dataset.tipe; bukaId = null;
    $$("#riwFilter .filter-chip").forEach(c => c.classList.toggle("active", c === ch));
    aturTombolHapus(); muat();
  }));

  let tmr = null;
  $("#riwQ").addEventListener("input", (ev) => {
    clearTimeout(tmr);
    tmr = setTimeout(() => { q = (ev.target.value || "").trim(); bukaId = null; muat(); }, 400);
  });

  $("#btnRiwHapusSemua").addEventListener("click", async () => {
    if (!confirm("Hapus " + namaAktif() + "? Tindakan ini permanen. 🗑️")) return;
    if (isTamu) {
      riwTamuSet(tipe ? riwTamuGet().filter(e => e.tipe !== tipe) : []);
      toast("Beres, udah dihapus semua. 🗑️"); bukaId = null; muat();
    } else {
      const r = await apiJSON("/history" + (tipe ? "?type=" + encodeURIComponent(tipe) : ""), { method: "DELETE" });
      if (r.ok) { toast("Beres, udah dihapus semua. 🗑️"); bukaId = null; muat(); }
      else toast("Gagal hapus, coba lagi ya. 😅");
    }
  });

  muat();
}

/* ----- Favorit ----- */
function pageFavorit() {
  view().innerHTML =
    "<h2>"+tic("star")+" Jurnal Favorit</h2>" +
    '<div class="card"><h3>⭐ Jurnal Favorit</h3>' +
    "<p class='muted'>Kumpulan jurnal yang kamu tandai favorit, biar gampang dibuka lagi kapan pun tanpa nyari ulang. ✨</p></div>" +
    '<div class="card"><h3>Tips 💡</h3><p class="muted">Lagi baca abstrak yang pas banget sama topikmu? Tandai sebagai favorit biar tidak hilang saat ganti halaman. 📚</p></div>';
}

/* ----- Matriks ----- */
function pageMatriks() {
  const arr = koleksiGet();
  view().innerHTML =
    "<h2>"+tic("grid")+" Matriks Literatur</h2>" +
    '<div class="card"><p class="muted">Bikin tabel sintesis (matriks) dari koleksimu — format DOCX, siap tempel ke Bab 2. 📊</p>' +
    "<p>Koleksi saat ini: <b>" + arr.length + "</b> referensi.</p>" +
    (arr.length
      ? '<button class="btn btn-primary btn-block" id="btnMatriks">🧩 Buat Matriks (DOCX)</button>'
      : '<p class="empty">Koleksimu kosong. 📭 <a href="#/cari">Cari &amp; simpan jurnal dulu →</a></p>') +
    "</div>";
  const btn = $("#btnMatriks");
  if (btn) btn.addEventListener("click", async () => {
    setLoading(btn, true, "Lagi nyusun matriks...");
    const res = await apiDownload("/matriks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: koleksiGet() })
    }, "matriks-literatur.docx");
    setLoading(btn, false);
    if (!res.ok && !kuotaTamuToast(res)) toast("Gagal bikin matriks, coba lagi ya. 😅");
    else if (res.ok) toast("Matriks keunduh! 🎉");
  });
}

/* ----- Deadline ----- */
function fmtTanggal(iso) {
  if (!iso) return "-";
  try {
    const d = new Date(iso.length <= 10 ? iso + "T00:00:00" : iso);
    return d.toLocaleDateString("id-ID", { day: "numeric", month: "long", year: "numeric" });
  } catch (e) { return iso; }
}
function sisaHari(iso) {
  try {
    const t = new Date(iso.length <= 10 ? iso + "T00:00:00" : iso);
    const now = new Date(); now.setHours(0, 0, 0, 0);
    return Math.round((t - now) / 86400000);
  } catch (e) { return null; }
}

async function muatDeadline() {
  const box = $("#listDeadline");
  if (!box) return;
  box.innerHTML = '<p class="loading">Lagi ngambil daftar deadline... ⏳</p>';
  const res = await apiPOST("/deadline", { aksi: "list" });
  if (!res.ok) { netErr(box, res); return; }
  const d = res.data || {};
  const arr = Array.isArray(d.deadline) ? d.deadline : (Array.isArray(d.daftar) ? d.daftar : (Array.isArray(d) ? d : []));
  if (!arr.length) { box.innerHTML = '<p class="empty">Belum ada deadline. 🎉 Tambahin di bawah biar nggak kelewat!</p>'; return; }
  box.innerHTML = arr.map((x, i) => {
    const nama = esc(x.nama || x.judul || x.nama_deadline || "(tanpa nama)");
    const tgl = x.tanggal || x.date || x.tenggat || "";
    const sisa = sisaHari(tgl);
    const cls = sisa == null ? "" : (sisa < 0 ? "dl-lewat" : (sisa <= 7 ? "dl-dekat" : ""));
    const infoSisa = sisa == null ? "" : (sisa < 0 ? " — udah lewat " + Math.abs(sisa) + " hari! 😱" : (sisa === 0 ? " — HARI INI! 🔥" : " — " + sisa + " hari lagi ⏳"));
    return '<div class="dl-item ' + cls + '"><div class="nama">' + nama +
      '<div class="date">📅 ' + esc(fmtTanggal(tgl)) + infoSisa + "</div></div>" +
      '<button class="btn btn-danger" data-dlhps="' + i + '">Hapus</button></div>';
  }).join("");
  $$("[data-dlhps]", box).forEach(b => b.addEventListener("click", async () => {
    if (!confirm("Hapus deadline ini? 🗑️")) return;
    const idx = parseInt(b.dataset.dlhps, 10);
    const r = await apiPOST("/deadline", { aksi: "hapus", idx: idx });
    if (!r.ok && kuotaTamuToast(r)) return;
    toast(r.ok ? "Deadline dihapus. 🗑️" : "Gagal hapus, coba lagi ya. 😅");
    muatDeadline();
  }));
}

function pageDeadline() {
  view().innerHTML =
    "<h2>"+tic("alarm")+" Deadline Skripsi</h2>" +
    '<div class="card"><h3>Tambah deadline ➕</h3>' +
    '<label for="dlNama">Nama kegiatan</label><input type="text" id="dlNama" placeholder="cth: Sidang proposal">' +
    '<label for="dlTgl">Tanggal</label><input type="date" id="dlTgl">' +
    '<button class="btn btn-primary btn-block" id="btnDlAdd" style="margin-top:12px">➕ Tambah</button></div>' +
    "<h3>Daftarmu 📋</h3><div id='listDeadline'></div>" +
    '<p class="tiny muted">Belum ada pengingat otomatis di web. Cek halaman ini berkala biar tidak ada yang kelewat. 💡</p>';
  muatDeadline();
  $("#btnDlAdd").addEventListener("click", async () => {
    const nama = ($("#dlNama").value || "").trim();
    const tanggal = $("#dlTgl").value || "";
    if (!nama || !tanggal) { toast("Isi nama + tanggalnya dulu 😄"); return; }
    if (sisaHari(tanggal) < 0 && !confirm("Tanggalnya sudah lewat. Tetap simpan? 📅")) return;
    const btn = $("#btnDlAdd"); setLoading(btn, true, "Lagi nyatet...");
    const res = await apiPOST("/deadline", { aksi: "tambah", nama: nama, tanggal: tanggal });
    setLoading(btn, false);
    if (!res.ok) { if (kuotaTamuToast(res)) return; toast(res.error === "network" ? "Koneksi bermasalah, coba lagi ya. 😭" : "😅 " + ((res.data && (res.data.pesan || res.data.error)) || "Gagal nambah deadline.")); return; }
    $("#dlNama").value = ""; $("#dlTgl").value = "";
    toast("Deadline kecatet! Semangat! 💪");
    muatDeadline();
  });
}

/* ----- Statistik (decision tree) -----
   Kontrak backend /api/statistik:
   - kirim: {answers: {qid: value}, restart: bool}
   - terima pertanyaan: {tipe:"pertanyaan", qid, teks (boleh HTML), opsi:[[value,label],...]}
   - terima hasil: {tipe:"hasil", hasil:{nama,kapan,syarat[],baca,contoh}} */
let statAnswers = {};
function pageStatistik() {
  statAnswers = {};
  view().innerHTML =
    "<h2>"+tic("chart")+" Panduan Uji Statistik</h2>" +
    '<p class="muted">Bingung pilih uji statistik? Jawab aja pertanyaannya, aku tunjukin yang cocok. 🧭</p>' +
    '<div id="statBox"></div>';
  statLangkah({ restart: true });
}
function statTanya(d) {
  const qid = d.qid || "";
  const ops = Array.isArray(d.opsi) ? d.opsi : [];
  const keys = Object.keys(statAnswers);
  let html = keys.length ? '<button class="btn btn-ghost" id="statBack">⬅️ Kembali</button>' : "";
  // d.teks dibuat backend dan boleh mengandung <b>/<i> -> render sebagai HTML
  html += '<div class="card"><h3>' + (d.teks || "Pilih salah satu:") + " ❓</h3>" +
    '<div class="chip-row">' + ops.map(o => {
      const val = Array.isArray(o) ? o[0] : o;
      const label = Array.isArray(o) ? o[1] : o;
      return '<button class="chip" data-qid="' + esc(qid) + '" data-val="' + esc(val) + '">' + esc(label) + "</button>";
    }).join("") + "</div></div>";
  $("#statBox").innerHTML = html;
  const back = $("#statBack");
  if (back) back.addEventListener("click", () => {
    const ks = Object.keys(statAnswers);
    if (ks.length) delete statAnswers[ks[ks.length - 1]];
    statLangkah({ answers: statAnswers });
  });
  $$("#statBox [data-val]").forEach(c => c.addEventListener("click", () => {
    statAnswers[c.dataset.qid] = c.dataset.val;
    statLangkah({ answers: statAnswers });
  }));
}
function statHasil(d) {
  const r = d.hasil || {};
  const syarat = Array.isArray(r.syarat) ? r.syarat : [];
  $("#statBox").innerHTML =
    '<div class="ok-box"><h3 style="margin-top:0">Hasil 🎯</h3>' +
    "<p><b>" + esc(r.nama || "Uji statistik") + "</b></p>" +
    (r.kapan ? "<p>" + esc(r.kapan) + "</p>" : "") +
    (syarat.length ? "<p><b>Syarat:</b></p><ul>" + syarat.map(s => "<li>" + esc(s) + "</li>").join("") + "</ul>" : "") +
    (r.baca ? "<p><b>Cara baca hasil:</b> " + esc(r.baca) + "</p>" : "") +
    (r.contoh ? '<p class="muted tiny"><b>Contoh:</b> ' + esc(r.contoh) + "</p>" : "") +
    "</div>" +
    '<button class="btn btn-primary btn-block" id="statUlang">🔄 Mulai dari awal</button>' +
    '<button class="btn btn-block" id="statAi" style="margin-top:8px">✨ Bantu tuliskan interpretasinya (AI)</button>' +
    '<div id="statAiOut" style="margin-top:10px"></div>';
  $("#statUlang").addEventListener("click", () => pageStatistik());
  const btnAi = $("#statAi");
  if (btnAi) btnAi.addEventListener("click", async () => {
    const out = $("#statAiOut");
    out.innerHTML = '<p class="loading">AI lagi nulis... ⏳</p>';
    setLoading(btnAi, true, "AI lagi nulis...");
    const teks = [r.nama, r.kapan, r.baca, r.contoh].filter(Boolean).join("\n");
    const res = await apiPOST("/ai", { kind: "interpretasi", text: teks });
    setLoading(btnAi, false);
    const d = res.data || {};
    if (!res.ok && (d.error === "kuota_habis" || d.kuota_habis)) { out.innerHTML = kuotaHabisBox(); return; }
    if (!res.ok) { out.innerHTML = ""; netErr(out, res); return; }
    const hasil = d.hasil || d.result || d.text;
    out.innerHTML = hasil ? '<div class="hasil-ai">' + mdRapi(hasil) + "</div>" : '<div class="warn-box">AI-nya bengong. 😅 Coba lagi ya.</div>';
  });
}
async function statLangkah(body) {
  const box = $("#statBox");
  if (!box) return;
  box.innerHTML = '<p class="loading">Lagi mikir... ⏳</p>';
  const res = await apiPOST("/statistik", body || {});
  if (!res.ok) { netErr(box, res); return; }
  const d = res.data || {};
  if (d.tipe === "hasil") statHasil(d);
  else if (d.tipe === "pertanyaan") statTanya(d);
  else box.innerHTML = '<div class="warn-box">Responsnya nggak kebaca. 😅 Coba ulangi ya.</div>';
}

/* ----- Template ----- */
function pageTemplate() {
  view().innerHTML =
    "<h2>"+tic("pencil")+" Template Skripsi</h2>" +
    '<div class="card"><p class="muted">Template proposal &amp; skripsi format DOCX, tinggal isi. Pilih jenis penelitianmu: 👇</p>' +
    '<button class="btn btn-primary btn-block" data-tmpl="kuantitatif">📊 Template Kuantitatif</button>' +
    '<button class="btn btn-primary btn-block" data-tmpl="kualitatif">📖 Template Kualitatif</button></div>';
  $$("[data-tmpl]", view()).forEach(b => b.addEventListener("click", async () => {
    const jenis = b.dataset.tmpl;
    setLoading(b, true, "Lagi nyiapin template...");
    const res = await apiDownload("/template?jenis=" + encodeURIComponent(jenis), {}, "template-" + jenis + ".docx");
    setLoading(b, false);
    toast(res.ok ? "Template keunduh! Gas nulis! ✍️" : "Gagal unduh template, coba lagi ya. 😅");
  }));
}

/* ----- Parafrase ----- */
function pageParafrase() {
  view().innerHTML =
    "<h2>"+tic("refresh")+" Parafrase</h2>" +
    '<div class="card"><p class="muted">Tempel kalimat yang mau diubah, biar nggak kepentok plagiarisme. 😉</p>' +
    '<label for="pfTeks">Teks asli</label><textarea id="pfTeks" placeholder="Tempel kalimat/paragraf di sini..."></textarea>' +
    '<button class="btn btn-primary btn-block" id="btnPf" style="margin-top:12px">🔁 Parafrase-kan!</button></div>' +
    '<div id="hasilPf"></div>';
  $("#btnPf").addEventListener("click", async () => {
    const teks = ($("#pfTeks").value || "").trim();
    if (!teks) { toast("Tempel dulu teksnya 😄"); return; }
    const box = $("#hasilPf");
    box.innerHTML = '<p class="loading">Lagi muter-muter kata... ⏳</p>';
    const btn = $("#btnPf"); setLoading(btn, true, "Lagi muter kata...");
    const res = await apiPOST("/parafrase", { teks: teks });
    setLoading(btn, false);
    if (!res.ok) { netErr(box, res); return; }
    const d = res.data || {};
    const hasil = d.hasil || d.parafrase || d.text;
    if (!hasil) { box.innerHTML = '<div class="warn-box">Hasilnya kosong. 😅 Coba teks yang lebih panjang ya.</div>'; return; }
    box.innerHTML = '<div class="card"><h3>Hasil parafrase ✨</h3><p style="white-space:pre-wrap">' + esc(hasil) + '</p><button class="btn" id="pfCopy">📋 Salin</button></div>';
    // I4 (audit 2026-10-13): parafrase dicatat sebagai riwayat chat
    riwTamuCatat("chat", teks, { sumber: "parafrase", text: teks, hasil: hasil });
    $("#pfCopy").addEventListener("click", () => copyText(hasil));
  });
}

/* ----- Jadwal ----- */
function pageJadwal() {
  view().innerHTML =
    "<h2>"+tic("calendar")+" Jadwal Skripsi</h2>" +
    '<div class="card"><p class="muted">Kasih tahu tanggal target sidang/wisudamu, aku bikinin timeline mundurnya. ⏪</p>' +
    '<label for="jdTgl">Tanggal target</label><input type="date" id="jdTgl">' +
    '<button class="btn btn-primary btn-block" id="btnJd" style="margin-top:12px">🗓️ Buatkan Timeline</button></div>' +
    '<div id="hasilJd"></div>';
  $("#btnJd").addEventListener("click", async () => {
    const tanggal = $("#jdTgl").value || "";
    if (!tanggal) { toast("Pilih dulu tanggalnya 😄"); return; }
    const box = $("#hasilJd");
    box.innerHTML = '<p class="loading">Lagi nyusun timeline... ⏳</p>';
    const btn = $("#btnJd"); setLoading(btn, true, "Lagi nyusun...");
    const res = await apiPOST("/jadwal", { tanggal: tanggal });
    setLoading(btn, false);
    if (!res.ok) { netErr(box, res); return; }
    const d = res.data || {};
    const tl = Array.isArray(d.timeline) ? d.timeline : (Array.isArray(d.jadwal) ? d.jadwal : []);
    if (!tl.length) { box.innerHTML = '<div class="warn-box">Timelinenya kosong. 😅 Coba tanggal lain ya.</div>'; return; }
    box.innerHTML = "<h3>Timeline menuju " + esc(fmtTanggal(tanggal)) + " 🗓️</h3>" +
      '<ul class="timeline">' + tl.map(t => {
        if (typeof t === "string") return "<li><div>" + esc(t) + "</div></li>";
        const det = t.deskripsi || t.detail;
        const cls = t.terlewat ? ' class="tl-lewat"' : (t.hari_h ? ' class="tl-harih"' : "");
        const badge = t.terlewat ? ' <span class="tiny muted">(terlewat ⏰)</span>' : (t.hari_h ? ' <span class="tiny">🔥 hari-H!</span>' : "");
        return '<li' + cls + '><span class="tgl">' + esc(t.tanggal_id || t.tanggal || t.date || "") + "</span><div><b>" +
          esc(t.kegiatan || t.nama || t.judul || "") + "</b>" + badge +
          (det ? '<br><span class="muted tiny">' + esc(det) + "</span>" : "") + "</div></li>";
      }).join("") + "</ul>";
  });
}

/* ----- AI Studio ----- */
const AI_KINDS = [
  { kind: "kerangka",      ico: "layout", nama: "Kerangka",        desc: "Outline bab 1-5 biar nulisnya nggak ngawang.", ph: "cth: Pengaruh literasi digital terhadap prestasi mahasiswa..." },
  { kind: "abstrak",       ico: "doc", nama: "Abstrak (ID+EN)", desc: "Abstrak bilingual Indonesia + Inggris.", ph: "Tempel poin-poin penting penelitianmu..." },
  { kind: "kuesioner",     ico: "clipboard", nama: "Kuesioner",       desc: "Draf item kuesioner dari variabelmu.", ph: "cth: Variabel X: motivasi belajar (5 indikator)..." },
  { kind: "latarbelakang", ico: "spark", nama: "Latar Belakang",  desc: "Draf latar belakang Bab 1.", ph: "cth: Topik: stunting di Manado, fenomena: ..." },
  { kind: "simpulan",      ico: "check", nama: "Simpulan",        desc: "Merangkum hasil jadi simpulan Bab 5.", ph: "Tempel hasil penelitianmu..." },
  { kind: "sidang",        ico: "chat", nama: "Simulasi Sidang", desc: "Prediksi pertanyaan dosen penguji + bocoran jawaban.", ph: "cth: Judul skripsi + ringkasan tiap bab..." },
  { kind: "kisi",          ico: "grid", nama: "Kisi-kisi",       desc: "Kisi-kisi instrumen penelitian.", ph: "cth: Topik/materi yang mau diukur..." },
  { kind: "metode",        ico: "chart", nama: "Metode",          desc: "Draf Bab 3: desain, populasi, teknik analisis.", ph: "cth: Kuantitatif, populasi mahasiswa..." },
  { kind: "pembahasan",    ico: "quote", nama: "Pembahasan",      desc: "Bantu ngebahas hasil penelitianmu.", ph: "Tempel hasil + teori pendukung..." },
  { kind: "bab2",          ico: "books", nama: "Bab 2",           desc: "Draf tinjauan pustaka per variabel.", ph: "cth: Topik + variabel penelitian..." }
];

async function ambilKuota() {
  const res = await apiJSON("/kuota");
  if (res.ok && res.data && typeof res.data.sisa === "number") return res.data.sisa;
  if (res.ok && res.data && typeof res.data.quota === "number") return res.data.quota;
  return null;
}

function kuotaBadgeUpdate(sisa) {
  const b = $("#kuotaBadge");
  if (!b) return;
  if (sisa == null) { b.textContent = "Kuota nggak kebaca ❓"; return; }
  if (sisa >= 999) { b.textContent = "Kuota unlimited ⚡"; return; }
  b.textContent = sisa > 0 ? "Sisa kuota hari ini: " + sisa + "x ⚡" : "Kuota hari ini habis 🥲";
  b.classList.toggle("habis", sisa <= 0);
}

function kuotaHabisBox() {
  return '<div class="warn-box">Yah, kuota AI harianmu habis (' + aiHarian() + 'x/hari). 🥲 ' +
    (AUTH.login
      ? 'Besok reset otomatis kok. Semangat ngerjainnya! 💪'
      : '<a href="#/masuk">Login gratis</a> biar dapat 10x/hari! ✨') + '</div>';
}

function pageAI() {
  view().innerHTML =
    "<h2>"+tic("spark")+" AI Studio</h2>" +
    '<p><span class="kuota" id="kuotaBadge">cek kuota... ⏳</span></p>' +
    '<p class="muted tiny">' + (AUTH.login
      ? 'Gratis <b>10x/hari</b> buat akun login. Pakai bijak ya! 😉'
      : 'Gratis <b>5x/hari</b> buat tamu — <a href="#/masuk">login gratis</a> biar dapat 10x/hari! 😉') + '</p>' +
    '<div class="ai-grid">' + AI_KINDS.map((k, i) =>
      '<div class="card ai-card"><h3>' + tic(k.ico) + " " + esc(k.nama) + "</h3>" +
      '<p class="muted tiny">' + esc(k.desc) + "</p>" +
      (k.kind === "abstrak" ? '<input type="text" id="aiJudul' + i + '" placeholder="Judul penelitian (opsional, buat abstrak)" autocomplete="off" style="margin-bottom:8px">' : "") +
      '<textarea id="aiIn' + i + '" placeholder="' + esc(k.ph) + '"></textarea>' +
      '<button class="btn btn-primary btn-block" data-aigen="' + i + '" style="margin-top:10px">✨ Generate</button>' +
      '<div id="aiOut' + i + '"></div></div>'
    ).join("") + "</div>";

  ambilKuota().then(kuotaBadgeUpdate);

  $$("[data-aigen]", view()).forEach(btn => btn.addEventListener("click", async () => {
    const i = parseInt(btn.dataset.aigen, 10);
    const k = AI_KINDS[i];
    const teks = ($("#aiIn" + i).value || "").trim();
    if (!teks) { toast("Isi dulu inputnya 😄"); return; }
    const out = $("#aiOut" + i);
    out.innerHTML = '<p class="loading">AI lagi nulis... sabar ya ⏳</p>';
    setLoading(btn, true, "AI lagi nulis...");
    const body = { kind: k.kind, text: teks };
    if (k.kind === "abstrak") {
      const judul = ($("#aiJudul" + i) || {}).value;
      if (judul && judul.trim()) body.extra = judul.trim();
    }
    const res = await apiPOST("/ai", body);
    setLoading(btn, false);
    const d = res.data || {};
    // 429 kuota_habis harus dicek SEBELUM !res.ok (apiJSON memetakan semua
    // non-2xx ke ok:false, jadi cabang ini mati bila ditaruh setelahnya)
    if (!res.ok && (d.error === "kuota_habis" || d.kuota_habis)) {
      out.innerHTML = kuotaHabisBox();
      kuotaBadgeUpdate(0);
      return;
    }
    if (!res.ok) { out.innerHTML = ""; netErr(out, res); return; }
    if (d.error === "kuota_habis" || d.kuota_habis) {
      out.innerHTML = kuotaHabisBox();
      kuotaBadgeUpdate(0);
      return;
    }
    const hasil = d.hasil || d.result || d.text;
    if (!hasil) { out.innerHTML = '<div class="warn-box">AI-nya bengong, nggak ngasih hasil. 😅 Coba lagi ya.</div>'; return; }
    const hid = "aiCopy" + i;
    out.innerHTML = '<div class="hasil-ai">' + mdRapi(hasil) + '</div><button class="btn" id="' + hid + '" style="margin-top:8px">📋 Salin Hasil</button>';
    riwTamuCatat("chat", teks, { sumber: "ai", kind: k.kind, text: teks, hasil: hasil });
    $("#" + hid).addEventListener("click", () => copyText(hasil));
    ambilKuota().then(kuotaBadgeUpdate);
    toast("Jadi! Scroll dikit buat lihat hasilnya. ✨");
  }));
}

/* ----- Tanya AI (chat) ----- */
let tanyaRiwayat = [];
function pageTanya() {
  view().innerHTML =
    "<h2>"+tic("chat")+" Tanya AI</h2>" +
    '<p class="muted">Tanya apa aja soal tugas & skripsi — dari sitasi sampai sidang. Dijawab AI + panduan. 🤖</p>' +
    '<div class="chat" id="chatBox"></div>' +
    '<div class="chat-form"><input type="text" id="tanyaIn" placeholder="Ketik pertanyaanmu..." autocomplete="off">' +
    '<button class="btn btn-primary" id="tanyaBtn">Kirim</button></div>';

  const render = () => {
    const box = $("#chatBox");
    if (!tanyaRiwayat.length) {
      box.innerHTML = '<div class="bubble bot">Halo! 👋 Aku AI-nya KhairyPustaka. Tanya apa aja soal tugas & skripsi — sitasi, metode, Bab 2, persiapan sidang, gas! 🚀</div>';
    } else {
      box.innerHTML = tanyaRiwayat.map(m => '<div class="bubble ' + m.dari + '">' + (m.html ? richText(m.teks) : (m.md ? mdRapi(m.teks) : esc(m.teks))) + "</div>").join("");
    }
    box.scrollTop = box.scrollHeight;
  };

  const kirimTanya = async () => {
    const inp = $("#tanyaIn");
    const q = (inp.value || "").trim();
    if (!q) return;
    tanyaRiwayat.push({ dari: "user", teks: q });
    inp.value = "";
    tanyaRiwayat.push({ dari: "bot", teks: "Lagi mikir... ⏳" });
    render();
    const btn = $("#tanyaBtn"); setLoading(btn, true, "...");
    const res = await apiPOST("/tanya", { q: q });
    setLoading(btn, false);
    tanyaRiwayat.pop();
    const d = res.data || {};
    // 429 kuota_habis harus dicek SEBELUM !res.ok (lihat pageAI)
    if (!res.ok && (d.error === "kuota_habis" || d.kuota_habis)) {
      tanyaRiwayat.push({ dari: "bot", teks: "Yah, kuota AI harianmu habis (" + aiHarian() + "x/hari). 🥲 " + (AUTH.login ? "Besok reset lagi kok! 💪" : "Login gratis biar dapat 10x/hari! ✨") });
    } else if (!res.ok) {
      tanyaRiwayat.push({ dari: "bot", teks: (d.pesan || d.error) ? String(d.pesan || d.error) : "Waduh koneksinya bermasalah. 😭 Coba kirim lagi ya!" });
    } else {
      if (d.error === "kuota_habis" || d.kuota_habis) {
        tanyaRiwayat.push({ dari: "bot", teks: "Yah, kuota AI harianmu habis (" + aiHarian() + "x/hari). 🥲 " + (AUTH.login ? "Besok reset lagi kok! 💪" : "Login gratis biar dapat 10x/hari! ✨") });
      } else {
        // kontrak backend: {tipe:"panduan"|"ai", jawaban, judul, terkait:[{id,title}]}
        let teks = d.jawaban || d.hasil || d.text || "Hmm, aku nggak nemu jawabannya. 😅 Coba tanya dengan kata lain ya.";
        const isPanduan = d.tipe === "panduan";
        if (isPanduan) teks += "\n\n(dari panduan 📚)";
        if (d.judul) teks = d.judul + "\n\n" + teks;
        tanyaRiwayat.push({ dari: "bot", teks: teks, html: isPanduan, md: !isPanduan });
        riwTamuCatat("chat", q, { sumber: "tanya", q: q, jawaban: teks });
        const rel = Array.isArray(d.terkait) ? d.terkait : [];
        if (rel.length) tanyaRiwayat.push({ dari: "bot", teks: "Mungkin kamu juga mau tanya:\n" + rel.slice(0, 4).map(r => "- " + (r && r.title ? r.title : r)).join("\n") });
      }
    }
    render();
  };

  render();
  $("#tanyaBtn").addEventListener("click", kirimTanya);
  $("#tanyaIn").addEventListener("keydown", e => { if (e.key === "Enter") kirimTanya(); });
  // lanjutan dari halaman Riwayat: tampilkan Q&A tersimpan sebagai bubble
  if (riwayatChatLanjut) {
    const rc = riwayatChatLanjut; riwayatChatLanjut = null;
    if (rc.q) tanyaRiwayat.push({ dari: "user", teks: rc.q });
    if (rc.jawaban) tanyaRiwayat.push({ dari: "bot", teks: rc.jawaban, md: true });
    render();
  }
}

/* ----- Jatah AI (top-up) ----- */
function pageTopup() {
  _tgNudgeShown = false;
  view().innerHTML =
    "<h2>"+tic("bolt")+" Jatah AI</h2>" +
    '<div id="topInfo"><p class="loading">Lagi ngecek jatah AI... ⏳</p></div>' +
    '<div id="topPay"></div>' +
    '<div class="card"><h3>Punya kode redeem? 🎟️</h3>' +
    '<label for="kodeRed">Kode redeem</label><input type="text" id="kodeRed" placeholder="Tempel kodemu di sini" autocomplete="off">' +
    '<button class="btn btn-accent btn-block" id="btnRedeem" style="margin-top:12px">🎟️ Redeem Sekarang</button></div>' +
    '<div class="card"><h3>Kode referralmu 🎁</h3><p class="muted tiny">Ajak teman pakai KhairyPustaka, tiap 3 teman dapat bonus jatah AI!</p>' +
    '<div id="refBox"><p class="loading">Lagi ngambil kode... ⏳</p></div></div>';

  function refreshInfo() {
    apiJSON("/topup").then(res => {
      const box = $("#topInfo");
      if (!box) return;
      if (!res.ok) { box.innerHTML = '<div class="warn-box">Jatah AI lagi nggak kebaca. 😅 Coba refresh ya.</div>'; return; }
      const d = res.data || {};
      const harian = d.sisa_harian != null ? d.sisa_harian : 0;
      const saldo = d.saldo != null ? d.saldo : 0;
      const harga = d.harga_rp || 5000, uses = d.uses || 50;
      const minU = d.min_uses || 50, perU = d.harga_per_use || 100;
      let html = '<div class="card"><h3>Sisa jatahmu</h3>' +
        '<p>🎁 Gratis hari ini: <b>' + harian + 'x</b> <span class="muted tiny">(reset tiap tengah malam WITA)</span></p>' +
        '<p>💰 Saldo top-up: <b>' + saldo + 'x</b> <span class="muted tiny">(tidak hangus)</span></p></div>';
      if (d.pay_enabled) {
        html += '<div class="card"><h3>Tambah jatah ⚡</h3>' +
          '<p class="muted">Rp' + Number(perU).toLocaleString("id-ID") + ' per 1x pakai AI, minimal ' + minU + 'x. Bayar via QRIS, masuk otomatis.</p>' +
          '<label for="topUses">Jumlah jatah (x)</label><input type="number" id="topUses" min="' + minU + '" value="' + minU + '" step="1" inputmode="numeric">' +
          '<p class="muted tiny">Total bayar: <b id="topTotal"></b></p>' +
          '<button class="btn btn-primary btn-block" id="btnTopup">⚡ Top-up via QRIS</button></div>';
      }
      box.innerHTML = html;
      const bt = $("#btnTopup");
      const inp = $("#topUses"), tot = $("#topTotal");
      function updTotal() {
        const u = Math.max(minU, parseInt((inp && inp.value) || "0", 10) || 0);
        if (tot) tot.textContent = "Rp" + (u * perU).toLocaleString("id-ID") + " = " + u + "x";
      }
      if (inp) { inp.addEventListener("input", updTotal); updTotal(); }
      if (bt) bt.addEventListener("click", () => topupCreate());
    });
  }
  refreshInfo();

  async function topupCreate() {
    const payBox = $("#topPay");
    const btn = $("#btnTopup"); setLoading(btn, true, "Bikin kode bayar...");
    const inpU = $("#topUses");
    const wantUses = inpU ? parseInt(inpU.value || "0", 10) : 0;
    const res = await apiPOST("/topup/create", {uses: wantUses});
    setLoading(btn, false);
    if (!res.ok) {
      const rd = res.data || {};
      if (rd.need_login || res.status === 401) {
        toast("🔑 Login dulu biar saldomu aman ya.");
        location.hash = "#/masuk";
        return;
      }
      toast("😅 " + ((res.data && (res.data.error || res.data.pesan)) || "Gagal bikin kode bayar, coba lagi ya."));
      return;
    }
    const d = res.data || {};
    if (!d.ref_id) { toast("Gagal bikin kode bayar, coba lagi ya. 😅"); return; }
    const total = d.total ? "Rp" + Number(d.total).toLocaleString("id-ID") : "";
    tampilkanQR(payBox, d.ref_id, total, d.qr_b64);
    payBox.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  // tampilkan QR + polling status; dipakai juga untuk resume invoice pending
  function tampilkanQR(payBox, refId, total, qrB64) {
    const qr = amanB64(qrB64);
    payBox.innerHTML =
      '<div class="card center"><h3>📱 Scan QRIS buat bayar ' + esc(total) + '</h3>' +
      (qr ? '<img src="data:image/png;base64,' + qr + '" alt="QRIS" style="max-width:260px;width:100%;border-radius:12px">' : '<p class="muted">QR nggak kebaca, coba lagi ya.</p>') +
      '<p class="muted tiny" id="payStat">Menunggu pembayaran... ⏳ (otomatis dicek tiap 8 detik)</p></div>';
    const timer = regTimer(setInterval(async () => {
      const st = await apiJSON("/topup/status/" + encodeURIComponent(refId));
      const sd = (st && st.data) || {};
      if (sd.paid) {
        clearInterval(timer);
        payBox.innerHTML = '<div class="ok-box center">🎉 <b>Pembayaran diterima!</b><br>+' + Number(sd.uses || 0) + 'x Jatah AI ditambahkan. Saldo: <b>' + Number(sd.saldo || 0) + 'x</b> 🚀</div>';
        refreshInfo();
        toast("Jatah AI masuk! 🎉");
      }
    }, 8000));
    // berhenti polling setelah 65 menit (invoice kedaluwarsa 1 jam)
    setTimeout(() => clearInterval(timer), 65 * 60 * 1000);
  }

  // resume: kalau ada invoice pending (mis. tab tertutup sebelum lunas),
  // tampilkan ulang QR-nya agar pembayaran tetap bisa diselesaikan
  if (AUTH.login) {
    apiJSON("/topup/pending").then(res => {
      const box = $("#topPay");
      if (!box || box.innerHTML.trim()) return; // sudah ada QR baru, jangan timpa
      const p = res && res.ok && res.data && res.data.pending;
      if (p && box) {
        const total = p.amount ? "Rp" + Number(p.amount).toLocaleString("id-ID") : "";
        tampilkanQR(box, p.ref_id, total, p.qr_b64);
        toast("Kamu punya pembayaran yang belum selesai 💳");
      }
    });
  }

  apiJSON("/kode-referral").then(res => {
    const box = $("#refBox");
    if (!box) return;
    if (!res.ok || !res.data) { box.innerHTML = '<p class="muted tiny">Kode referral lagi nggak kebaca. 😅</p>'; return; }
    const kode = res.data.kode || res.data.referral || res.data.code || "";
    const reward = res.data.reward_uses || 30;
    box.innerHTML = kode
      ? '<p><b class="ref-code">' + esc(kode) + '</b></p>' +
        '<p class="muted tiny">Tiap 3 teman gabung = +' + reward + 'x Jatah AI 🎉</p>' +
        '<button class="btn" id="refCopy">📋 Salin Link Referral</button>'
      : '<p class="muted tiny">Belum ada kode referral. 😅</p>';
    const cp = $("#refCopy");
    if (cp) cp.addEventListener("click", () => copyText(location.origin + "/?ref=" + kode));
  });

  $("#btnRedeem").addEventListener("click", async () => {
    const kode = ($("#kodeRed").value || "").trim();
    if (!kode) { toast("Tempel dulu kodenya 😄"); return; }
    const btn = $("#btnRedeem"); setLoading(btn, true, "Lagi dicek...");
    const res = await apiPOST("/redeem", { kode: kode });
    setLoading(btn, false);
    if (!res.ok) {
      const rd = res.data || {};
      if (rd.need_login || res.status === 401) {
        toast("🔑 Login dulu biar saldomu aman ya.");
        location.hash = "#/masuk";
        return;
      }
      toast(res.error === "network" ? "Koneksi bermasalah, coba lagi ya. 😭" : "Gagal redeem, coba lagi ya. 😅");
      return;
    }
    const d = res.data || {};
    const ok = d.ok === true || d.sukses === true || d.success === true;
    if (ok) {
      toast("Kode valid! +" + (d.uses || "") + "x Jatah AI! 🎉⚡");
      $("#kodeRed").value = ""; refreshInfo();
      return;
    }
    const alasan = d.alasan || d.reason || "";
    const msg = alasan === "used" ? "Kode ini sudah dipakai. 😅"
      : alasan === "notfound" ? "Kode nggak ketemu. Cek lagi ya. 🔍"
      : (d.pesan || d.message || d.error || "Kode nggak valid / udah dipakai.");
    toast("😅 " + msg);
  });
}

/* ----- Bantuan ----- */
function pageBantuan() {
  const faq = [
    ["KhairyPustaka itu apa? ❓", "Asisten skripsi & nugas buat mahasiswa semua jurusan: cari jurnal, bikin sitasi 6 gaya, matriks literatur, deadline, sampai 10 generator AI naskah. Gratis! 🎓"],
    ["Beneran gratis? 💰", "Iya! Fitur AI gratis " + aiHarian() + "x/hari" + (AUTH.login ? "." : " buat tamu (login gratis biar dapat 10x/hari).") + " Fitur non-AI gratis tiap hari: tamu 10x/hari per fitur, login sepuasnya. 😉"],
    ["Koleksiku kesimpen di mana? 📚", "Di browser HP-mu sendiri (localStorage), tanpa login. Catatan: kalau hapus data browser / ganti HP, koleksinya ikut hilang — jadi rajin-rajin export ya!"],
    ["Kok nggak ada notifikasi? 🔔", "Belum ada notifikasi otomatis di versi web. Untuk deadline, catat di halaman Deadline dan cek berkala ya. 😉"],
    ["Sitasi gaya apa aja yang didukung? 📑", "6 gaya: Harvard (default), APA, MLA, Chicago, Vancouver, IEEE. Tinggal pilih, salin, tempel ke tugas/skripsimu."],
    ["AI-nya pakai apa? 🤖", "Otomatis pilih yang terbaik & gratis: Gemini, Groq, Cerebras, OpenRouter. Butuh lebih banyak? Top-up jatah AI di menu ⚡ Jatah AI — Rp100/x, minimal 50x (Rp5.000), nggak hangus."],
    ["Error / nggak jalan, gimana? 😭", "Coba refresh dulu. Kalau masih error, cek koneksi internetmu. Masih bandel juga? Hubungi admin lewat tombol Hubungi Admin di bagian bawah halaman. 🛠️"]
  ];
  view().innerHTML =
    "<h2>"+tic("help")+" Bantuan</h2>" +
    faq.map(f => '<div class="card"><h3>' + f[0] + "</h3><p>" + f[1] + "</p></div>").join("") +
    '<div class="card center"><h3>Masih bingung? 💬</h3><p class="muted">Chat admin langsung, fast respon di jam kerja. ✨</p>' +
    '<a class="btn btn-primary" href="https://instagram.com/khairypustaka" target="_blank" rel="noopener">' + tic("ig") + ' Hubungi Admin</a></div>';
}

/* ---------- Router ---------- */
const ROUTES = {
  "/beranda": pageBeranda,
  "/cari": pageCari,
  "/carifile": pageCarifile,
  "/sitasi": pageSitasi,
  "/masuk": pageMasuk,
  "/akun": pageAkun,
  "/lupa": pageLupa,
  "/reset": pageReset,
  "/verifikasi": pageVerifikasi,
  "/konfirmasi-email": pageKonfirmasiEmail,
  "/rekomendasi": pageRekomendasi,
  "/publisher": pagePublisher,
  "/penulis": pagePenulis,
  "/topik": pageTopik,
  "/koleksi": pageKoleksi,
  "/riwayat": pageRiwayat,
  "/favorit": pageFavorit,
  "/matriks": pageMatriks,
  "/deadline": pageDeadline,
  "/statistik": pageStatistik,
  "/template": pageTemplate,
  "/parafrase": pageParafrase,
  "/jadwal": pageJadwal,
  "/ai": pageAI,
  "/tanya": pageTanya,
  "/topup": pageTopup,
  "/premium": pageTopup,
  "/bantuan": pageBantuan,
  "/admin": pageAdmin
};

/* ================= Panel Admin =================
   Khusus akun admin (flag admin:true dari /api/auth/status).
   Backend tetap menegakkan semuanya: semua /api/admin/* menjawab 403
   untuk tamu dan non admin. */
function aturMenuAdmin() {
  const nav = $("#mainNav");
  if (!nav) return;
  let a = $("#navAdmin");
  if (AUTH.login && AUTH.admin) {
    if (!a) {
      a = document.createElement("a");
      a.id = "navAdmin";
      a.href = "#/admin";
      a.textContent = "🛡️ Panel Admin";
      nav.appendChild(a);
    }
  } else if (a) {
    a.remove();
  }
}

function admKartu(angka, label) {
  return '<div class="stat-card"><b>' + esc(String(angka == null ? 0 : angka)) +
    "</b><span>" + esc(label) + "</span></div>";
}

function admTabel(rows) {
  if (!rows.length) return '<p class="muted">Belum ada data.</p>';
  return '<table class="adm-tabel"><tr><th>Fitur</th><th>Hari Ini</th><th>Total</th></tr>' +
    rows.map(function(r) {
      return "<tr><td>" + esc(r.nama) + "</td><td>" + esc(String(r.hari_ini)) +
        "</td><td>" + esc(String(r.total)) + "</td></tr>";
    }).join("") + "</table>";
}

async function admMuatStatistik() {
  const box = $("#admStat");
  if (!box) return;
  try {
    const r = await apiJSON("/admin/statistik");
    if (!r.ok) throw 0;
    const d = r.data || {}, m = d.member || {}, a = d.ai || {}, n = d.nonai || {};
    box.innerHTML =
      '<div class="admin-grid">' +
      admKartu(m.total, "Total Member") +
      admKartu(m.baru_hari_ini, "Member Baru Hari Ini") +
      admKartu(a.hari_ini, "Pemakaian AI Hari Ini") +
      admKartu(a.total, "Pemakaian AI Total") +
      admKartu(n.hari_ini, "Pemakaian Non AI Hari Ini") +
      admKartu(n.total, "Pemakaian Non AI Total") +
      admKartu(d.saldo_ai_web_total, "Total Saldo AI Member") +
      admKartu(m.premium, "Akun Premium") +
      "</div>" +
      "<h4>Per Fitur AI</h4>" + admTabel(a.per_fitur || []) +
      "<h4>Per Fitur Non AI</h4>" + admTabel(n.per_fitur || []);
  } catch (e) {
    box.innerHTML = '<p class="pf-msg">Gagal memuat statistik. Muat ulang halamannya ya.</p>';
  }
}

function admSalinTeks(t) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(t).then(
      function() { toast("Kode tersalin! 📋"); },
      function() { admSalinCadangan(t); });
  } else {
    admSalinCadangan(t);
  }
}

function admSalinCadangan(t) {
  const ta = document.createElement("textarea");
  ta.value = t;
  document.body.appendChild(ta);
  ta.select();
  try {
    document.execCommand("copy");
    toast("Kode tersalin! 📋");
  } catch (e) {
    toast("Salin manual ya: " + t);
  }
  ta.remove();
}

function admBarisKodeBaru(k) {
  return '<div class="kode-row"><code>' + esc(k) + "</code>" +
    '<button class="btn" data-salin="' + esc(k) + '">Salin</button></div>';
}

async function admMuatKode() {
  const box = $("#admKodeList");
  if (!box) return;
  try {
    const r = await apiJSON("/admin/kode");
    if (!r.ok) throw 0;
    const rows = r.data.kode || [];
    if (!rows.length) {
      box.innerHTML = '<p class="muted">Belum ada kode yang dibuat.</p>';
      return;
    }
    box.innerHTML = rows.map(function(k) {
      const bebas = k.status === "belum dipakai";
      return '<div class="kode-row"><code>' + esc(k.kode) + "</code>" +
        '<span class="chip-mini ' + (bebas ? "chip-tunggu" : "chip-ok") + '">' + esc(k.status) + "</span>" +
        (k.dipakai_oleh ? '<span class="muted">' + esc(k.dipakai_oleh) + "</span>" : "") +
        '<button class="btn" data-salin="' + esc(k.kode) + '">Salin</button></div>';
    }).join("");
  } catch (e) {
    box.innerHTML = '<p class="pf-msg">Gagal memuat daftar kode.</p>';
  }
}

async function admCariMember() {
  const box = $("#admCariHasil"), msg = $("#admGrantMsg");
  if (!box) return;
  msg.textContent = "";
  const q = ($("#admCari").value || "").trim();
  try {
    const r = await apiJSON("/admin/member?q=" + encodeURIComponent(q));
    if (!r.ok) throw 0;
    const rows = r.data.member || [];
    if (!rows.length) {
      box.innerHTML = '<p class="muted">Tidak ketemu. Coba kata kunci lain ya.</p>';
      return;
    }
    box.innerHTML = rows.map(function(m) {
      const nama = m.nama || m.username || "member";
      return '<div class="kode-row"><div style="flex:1;min-width:160px"><b>@' + esc(m.username || "?") + "</b>" +
        '<div class="muted">' + esc(m.nama || "") + (m.email ? " · " + esc(m.email) : "") + "</div>" +
        '<div class="muted">Saldo AI: <b>' + esc(String(m.saldo)) + "</b> · sisa harian: " + esc(String(m.sisa_harian)) + "</div></div>" +
        '<input type="number" class="adm-jml" data-uid="' + esc(m.uid) + '" value="25" min="1" max="100000" aria-label="jumlah jatah AI">' +
        '<button class="btn btn-primary" data-grant="' + esc(m.uid) + '" data-nama="' + esc(nama) + '">Tambah</button></div>';
    }).join("");
  } catch (e) {
    box.innerHTML = '<p class="pf-msg">Pencarian gagal. Coba lagi ya.</p>';
  }
}

async function admGrant(uid, nama) {
  const msg = $("#admGrantMsg");
  const inp = document.querySelector('.adm-jml[data-uid="' + uid + '"]');
  const jumlah = parseInt(inp && inp.value, 10) || 0;
  if (!(jumlah >= 1)) {
    msg.textContent = "Isi jumlah yang valid dulu ya.";
    return;
  }
  if (!confirm("Tambah " + jumlah + " jatah AI ke " + nama + "?")) return;
  try {
    const r = await apiPOST("/admin/grant", { uid: uid, jumlah: jumlah });
    if (!r.ok) {
      msg.textContent = (r.data && (r.data.pesan || r.data.error)) || "Gagal menambah saldo.";
      return;
    }
    msg.textContent = "Berhasil! Saldo " + nama + " sekarang " + r.data.saldo + " jatah AI.";
    admCariMember();
  } catch (e) {
    msg.textContent = "Koneksi bermasalah. Coba lagi ya.";
  }
}

function admKlikGlobal(e) {
  const salin = e.target.closest ? e.target.closest("[data-salin]") : null;
  if (salin) {
    admSalinTeks(salin.getAttribute("data-salin"));
    return;
  }
  const grant = e.target.closest ? e.target.closest("[data-grant]") : null;
  if (grant) {
    admGrant(grant.getAttribute("data-grant"), grant.getAttribute("data-nama"));
  }
}

function pageAdmin() {
  if (!AUTH.login) {
    view().innerHTML =
      "<h2>🛡️ Panel Admin</h2>" +
      '<div class="card center"><p class="muted">Kamu lagi mode tamu. Halaman admin hanya bisa dibuka setelah login.</p>' +
      '<a class="btn btn-primary btn-block" href="#/masuk">🔑 Masuk</a></div>';
    return;
  }
  if (!AUTH.admin) {
    view().innerHTML =
      "<h2>🛡️ Panel Admin</h2>" +
      '<div class="card center"><p class="muted">Halaman ini khusus akun admin. Kalau kamu merasa ini salah, hubungi admin lewat halaman Bantuan ya. 🙂</p>' +
      '<a class="btn btn-primary btn-block" href="#/beranda">Ke Beranda</a></div>';
    return;
  }
  view().innerHTML =
    "<h2>🛡️ Panel Admin</h2>" +
    '<div class="card"><h3>📊 Ringkasan</h3><div id="admStat"><p class="muted">Memuat data...</p></div></div>' +
    '<div class="card"><h3>🎟️ Kode Jatah AI</h3>' +
    '<p class="muted">Buat kode redeem buat dibagikan ke member. Satu kode dipakai satu member, sekali pakai.</p>' +
    '<label class="muted" for="admKodeJml">Jumlah kode</label>' +
    '<input type="number" id="admKodeJml" value="5" min="1" max="200">' +
    '<label class="muted" for="admKodeNom">Nominal per kode (jatah AI)</label>' +
    '<div class="chip-row" id="admNomChips">' +
    [25, 50, 100].map(function(n) {
      return '<button class="chip" data-nom="' + n + '">' + n + "</button>";
    }).join("") +
    "</div>" +
    '<input type="number" id="admKodeNom" value="25" min="1" max="10000">' +
    '<button class="btn btn-primary btn-block" id="admKodeBtn">Generate Kode</button>' +
    '<p class="pf-msg" id="admKodeMsg"></p>' +
    '<div id="admKodeBaru"></div></div>' +
    '<div class="card"><h3>📜 Daftar Kode</h3><div id="admKodeList"><p class="muted">Memuat...</p></div>' +
    '<button class="btn btn-block" id="admKodeRefresh">Muat Ulang</button></div>' +
    '<div class="card"><h3>➕ Tambah Jatah AI</h3>' +
    '<p class="muted">Cari member lalu tambah saldo jatah AI langsung ke akunnya.</p>' +
    '<input type="text" id="admCari" placeholder="ketik username, nama, atau email" autocomplete="off">' +
    '<button class="btn btn-block" id="admCariBtn">Cari Member</button>' +
    '<div id="admCariHasil"></div>' +
    '<p class="pf-msg" id="admGrantMsg"></p></div>';
  admMuatStatistik();
  admMuatKode();
  $$("#admNomChips .chip").forEach(function(c) {
    c.addEventListener("click", function() {
      $("#admKodeNom").value = c.dataset.nom;
    });
  });
  $("#admKodeBtn").addEventListener("click", async function() {
    const msg = $("#admKodeMsg"), out = $("#admKodeBaru");
    msg.textContent = "";
    const jumlah = parseInt($("#admKodeJml").value, 10) || 0;
    const nominal = parseInt($("#admKodeNom").value, 10) || 0;
    try {
      const r = await apiPOST("/admin/kode", { jumlah: jumlah, nominal: nominal });
      if (!r.ok) {
        msg.textContent = (r.data && (r.data.pesan || r.data.error)) || "Gagal membuat kode.";
        return;
      }
      const kode = r.data.kode || [];
      out.innerHTML = '<p class="muted">Kode berhasil dibuat (' + kode.length +
        " x " + esc(String(r.data.nominal)) +
        " jatah AI). Simpan baik baik ya, kode hanya ditampilkan sekali di sini.</p>" +
        kode.map(admBarisKodeBaru).join("");
      admMuatKode();
    } catch (e) {
      msg.textContent = "Koneksi bermasalah. Coba lagi ya.";
    }
  });
  $("#admKodeRefresh").addEventListener("click", admMuatKode);
  $("#admCariBtn").addEventListener("click", admCariMember);
}

function page404() {
  view().innerHTML = '<div class="card center"><h2>'+tic("help")+'</h2>' +
    "<p>Halaman <b>" + esc(location.hash || "") + "</b> nggak ketemu.</p>" +
    '<a class="btn btn-primary" href="#/beranda">Ke Beranda</a></div>';
}

/* ---------- FAB Riwayat (tombol melayang ala widget live chat) ---------- */
function initFABRiwayat() {
  if (document.getElementById("fabRiwayat")) return;
  const b = document.createElement("button");
  b.id = "fabRiwayat";
  b.className = "fab-riwayat";
  b.type = "button";
  b.setAttribute("aria-label", "Buka Riwayat");
  b.innerHTML = '<span class="fab-riwayat-label">Riwayat</span>' +
    '<span class="fab-bulat">' + tic("clock") + "</span>";
  b.addEventListener("click", () => { location.hash = "#/riwayat"; });
  document.body.appendChild(b);
  aturFABRiwayat((location.hash || "#/beranda").replace(/^#/, "").split("?")[0]);
}
function aturFABRiwayat(h) {
  const b = document.getElementById("fabRiwayat");
  if (b) b.classList.toggle("hidden", h === "/riwayat");
}

/* ---------- Tombol Kembali (balik ke halaman sebelumnya di dalam web) ---------- */
let NAVSTACK = [];
try { NAVSTACK = JSON.parse(sessionStorage.getItem("kp_nav") || "[]"); } catch (e) { NAVSTACK = []; }
let NAVBACK = false; // true saat navigasi dipicu tombol Kembali
let RUTE_TERAKHIR = null; // rute yang sedang tampil (buat simpan scroll Beranda)
function simpanNav() {
  try { sessionStorage.setItem("kp_nav", JSON.stringify(NAVSTACK)); } catch (e) {}
}
function catatNav(h) {
  if (NAVBACK) { NAVBACK = false; simpanNav(); return; }
  if (NAVSTACK[NAVSTACK.length - 1] !== h) {
    NAVSTACK.push(h);
    if (NAVSTACK.length > 30) NAVSTACK = NAVSTACK.slice(-30);
    simpanNav();
  }
}
function pergiKembali() {
  const cur = NAVSTACK.pop();
  const target = NAVSTACK[NAVSTACK.length - 1];
  simpanNav();
  if (target && target !== cur) {
    NAVBACK = true;
    location.hash = "#" + target;
  } else {
    NAVSTACK = [];
    simpanNav();
    location.hash = "#/beranda";
  }
}
function aturTombolKembali(h) {
  const bar = document.getElementById("backBar");
  if (bar) bar.classList.toggle("hidden", h === "/beranda");
}

function router() {
  clearPageTimers(); // hentikan polling halaman sebelumnya
  // Simpan posisi scroll Beranda sebelum pindah halaman, supaya tombol
  // Kembali bisa mengembalikan tampilan persis seperti ditinggalkan.
  if (RUTE_TERAKHIR === "/beranda") {
    try { sessionStorage.setItem("kp_scroll_home", String(window.scrollY || 0)); } catch (e) {}
  }
  const dariKembali = NAVBACK;
  let h = (location.hash || "#/beranda").replace(/^#/, "").split("?")[0];
  if (!h.startsWith("/")) h = "/beranda";
  const fn = ROUTES[h] || page404;
  aturFABRiwayat(h); // sembunyikan FAB saat lagi di halaman Riwayat
  catatNav(h); // jejak navigasi buat tombol Kembali
  aturTombolKembali(h); // tombol Kembali tampil di semua halaman selain Beranda
  const nav = $("#mainNav");
  if (nav) nav.classList.remove("open");
  $$("#mainNav a").forEach(a => a.classList.toggle("active", a.getAttribute("href") === "#" + h));
  window.scrollTo(0, 0);
  try { fn(); }
  catch (e) {
    view().innerHTML = '<div class="err-box">Yah, halamannya error. 😅 Coba refresh ya.</div>';
  }
  const v = view();
  v.classList.remove("page-in"); void v.offsetWidth; v.classList.add("page-in");
  if (dariKembali && h === "/beranda") {
    let y = 0;
    try { y = parseInt(sessionStorage.getItem("kp_scroll_home") || "0", 10) || 0; } catch (e) {}
    if (y > 0) window.scrollTo(0, y); // pulihkan posisi scroll terakhir di Beranda
  }
  RUTE_TERAKHIR = h;
  refreshAuth();
}

/* ---------- init ---------- */
document.addEventListener("DOMContentLoaded", async () => {
  // referral: ?ref=KH-xxx di URL -> daftarkan sekali per browser
  try {
    const refKode = new URLSearchParams(location.search).get("ref");
    if (refKode && !localStorage.getItem("kp_ref_done")) {
      localStorage.setItem("kp_ref_done", "1");
      apiJSON("/ref?kode=" + encodeURIComponent(refKode)).then(() => {
        toast("Selamat datang! 🎉");
      });
    }
  } catch (e) {}
  $("#menuBtn").addEventListener("click", () => $("#mainNav").classList.toggle("open"));
  const bk = document.getElementById("btnBack");
  if (bk) bk.addEventListener("click", pergiKembali);
  window.addEventListener("hashchange", router);
  initFABRiwayat(); // tombol melayang Riwayat ala live chat (sekali saat boot)
  document.addEventListener("click", admKlikGlobal); // tombol Salin/Tambah panel admin

  $("#modalSitClose").addEventListener("click", tutupModalSit);
  $("#modalSit").addEventListener("click", e => { if (e.target.id === "modalSit") tutupModalSit(); });
  $$("#modalSitGaya .chip").forEach(c => c.addEventListener("click", () => buatSitasi(c.dataset.gaya, c)));
  $("#modalSitCopy").addEventListener("click", () => {
    const t = $("#modalSitTeks").textContent || "";
    if (t) copyText(t);
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") { tutupModalSit(); }
  });

  // cek backend hidup (non-blocking; gagal = diam saja, tiap aksi tetap coba API)
  apiJSON("/health").then(res => {
    if (!res.ok || !(res.data && res.data.ok)) {
      console.warn("KhairyPustaka API belum merespons /health - backend mungkin belum jalan.");
    }
  });

  // R2 (audit 2026-10-13): pastikan status login diketahui SEBELUM halaman
  // pertama dirender, agar riwayat tamu tidak tercatat ganda (tamu + server)
  try { await refreshAuth(); } catch (e) {}
  router();
});
