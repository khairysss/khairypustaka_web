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

/* Saring kode error mentah server jadi kalimat manusiawi di semua jalur
   fetch, supaya kode seperti "db_sibuk" tidak pernah tampil mentah ke
   pengguna di fitur apa pun. Kode yang dipakai logika UI (kuota_habis,
   kuota_tamu_habis) SENGAJA tidak diubah. */
const PESAN_SERVER_GANGGUAN = "Server lagi bermasalah, coba lagi beberapa saat ya";
const KODE_ERR_MENTAH = ["db_sibuk", "kesalahan server", "server_sibuk", "rate_limit", "file_terlalu_besar"];
function saringErrData(data) {
  if (data && KODE_ERR_MENTAH.indexOf(data.error) >= 0) {
    data.error = data.pesan || PESAN_SERVER_GANGGUAN;
    data._gangguanServer = true;
  }
  return data;
}

async function apiJSON(path, opts) {
  const o = Object.assign({ method: "GET", headers: { "Content-Type": "application/json" } }, opts || {});
  // timeout: 30 dtk umum; 80 dtk untuk /ai, /tanya, /topik (budget server
  // 40 dtk — timeout client HARUS di atasnya supaya kuota tidak hangus diam-diam)
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
    data = saringErrData(data);
    if (!r.ok) return { ok: false, status: r.status, data: data, error: "HTTP " + r.status };
    // Fase 3: pemakaian fitur member yang berhasil bisa memicu popup
    // penilaian (pengecekan sendiri dibatasi jeda dan status server).
    try {
      const mth = (o.method || "GET").toUpperCase();
      if (mth === "POST" && /^\/(cari|carifile|sitasi|rekomendasi|publisher|penulis|topik|matriks|statistik|parafrase|kemiripan|terjemah|ai|tanya|convert|sitasi-pdf|koleksi-impor|export)/.test(_p)) {
        setTimeout(cekPopupFeedback, 1200);
      }
    } catch (e) {}
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

/* Pesan error manusiawi untuk alur akun: kode mentah server (mis. "db_sibuk"
   saat basis data tak terjangkau, 500 generik, atau koneksi putus) jangan
   sampai tampil ke pengguna, bisa dikira kesalahan mereka sendiri.
   konteks = kalimat pengganti yang sesuai halamannya. */
function pesanErrAkun(res, konteks) {
  const e = (res && res.data && res.data.error) || "";
  if (!res || res.status === 0 || (res.data && res.data._gangguanServer) ||
      e === "db_sibuk" || e === "kesalahan server") {
    return konteks;
  }
  return e || konteks;
}

/* ---------- Auth (username+password / tamu) ---------- */
let AUTH = { login: false };
let AUTH_SIAP = false; // R2 (audit 2026-10-13): true setelah /auth/status pertama resolve
let _riwAntre = [];    // R2: entri tamu yang dicatat sebelum status auth pasti

/* Jatah harian: tamu = ember gabungan 5x/hari buat semua fitur
   (AI + non-AI, keputusan Sul 2026-10-19); login = jatah AI 15x/hari. */
function aiHarian() { return 15; }
function tamuHarian() { return 5; }

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
  flushRefPending(); // K-1: klaim referral tertunda bila sudah login
  const area = $("#authArea");
  if (!area) return;
  if (AUTH.login) {
    const nama = esc(String(AUTH.nama || "Akun").split(" ")[0]);
    area.innerHTML = '<a href="#/akun" class="user-chip">' +
      (AUTH.foto ? '<img src="' + safeUrl(AUTH.foto) + '" alt="foto">' : '<img src="' + avatarURL(AUTH.avatar) + '" alt="foto profil">') +
      "<span>" + nama + "</span>" +
      (AUTH.admin ? '<span class="badge-admin">ADMIN</span>' : "") + "</a>";
  } else {
    area.innerHTML = '<a href="#/masuk" class="btn-login">' + tic("key") + ' Masuk</a>' +
      '<a href="#/daftar" class="btn-daftar">Daftar</a>';
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
        try { const j = saringErrData(await r.json()); if (j && j.error) code = j.error; if (j && j.pesan) pesan = j.pesan; } catch (e) {}
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
    box.innerHTML = '<div class="warn-box">' + esc(pesan || "Jatah harian tamu habis (5x/hari buat semua fitur).") +
      '<br><a class="btn btn-primary" style="margin-top:10px;display:inline-block" href="#/masuk">Masuk / Daftar — gratis! ' + tic("spark") + '</a></div>';
    popupJatahHabis();
    return true;
  }
  return false;
}
function kuotaTamuToast(res) {
  const pesan = _kuotaTamuMatch(res);
  if (pesan != null) {
    toast(" " + (pesan || "Jatah harian tamu habis (5x/hari buat semua fitur).") + " Login gratis biar sepuasnya! ");
    popupJatahHabis();
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
/* Tamu: koleksi hanya disimpan maks 24 jam lalu otomatis terhapus
   (keputusan Sul 2026-10-18). Member login tidak kena batas ini. */
const KOLEKSI_TTL_TAMU = 24 * 60 * 60 * 1000;
function koleksiGet() {
  const v = store.get(KOLEKSI_KEY, []);
  const arr = Array.isArray(v) ? v : [];
  if (!AUTH_SIAP || AUTH.login) return arr; // TTL hanya berlaku untuk tamu
  const kini = Date.now();
  let berubah = false;
  const bersih = arr.filter(x => {
    if (!x || typeof x !== "object") { berubah = true; return false; }
    if (!x.ts) { x.ts = kini; berubah = true; return true; } // data lama: hitung 24 jam mulai sekarang
    if (kini - x.ts >= KOLEKSI_TTL_TAMU) { berubah = true; return false; }
    return true;
  });
  if (berubah) koleksiSet(bersih);
  return bersih;
}
function koleksiSet(arr) { return store.set(KOLEKSI_KEY, arr); }
function koleksiTambah(item) {
  const arr = koleksiGet();
  const judul = (item.judul || "").trim().toLowerCase();
  if (judul && arr.some(x => (x.judul || "").trim().toLowerCase() === judul)) return "duplikat";
  item.ts = Date.now(); // cap waktu simpan (dasar TTL 24 jam untuk tamu)
  arr.unshift(item);
  return koleksiSet(arr) ? "ok" : "gagal";
}
function koleksiHapus(i) {
  const arr = koleksiGet();
  arr.splice(i, 1);
  koleksiSet(arr);
}
function koleksiImpor(items) {
  const arr = koleksiGet();
  const ada = new Set(arr.map(x => (x.judul || "").trim().toLowerCase()).filter(Boolean));
  const baru = [];
  let duplikat = 0;
  for (const it of items || []) {
    const j = (it.judul || "").trim().toLowerCase();
    if (!j) continue;
    if (ada.has(j)) { duplikat++; continue; }
    ada.add(j);
    it.ts = Date.now();
    baru.push(it);
  }
  if (baru.length) koleksiSet(baru.concat(arr));
  return { tambah: baru.length, duplikat: duplikat };
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
  const pdfBtn = (pdf && pdf !== url) ? '<a class="btn btn-ghost" href="' + esc(pdf) + '" target="_blank" rel="noopener">' + tic("doc") + ' PDF</a>' : "";
  return '<article class="card hasil">' +
    "<h3>" + judul + "</h3>" +
    '<div class="meta">' + penulis + " | " + tahun + " | " + jurnal + "</div>" +
    abstrak +
    '<div class="aksi-row">' +
      '<button class="btn" data-aksi="sitasi" data-idx="' + idx + '">Sitasi</button>' +
      '<button class="btn" data-aksi="simpan" data-idx="' + idx + '">Simpan ke Koleksi</button>' +
      (item.abstrak ? '<button class="btn" data-aksi="ringkas" data-idx="' + idx + '">' + tic("spark") + ' Ringkas AI</button>' : "") +
      (item.abstrak ? '<button class="btn" data-aksi="terjemah" data-idx="' + idx + '">' + tic("globe") + ' Terjemah</button>' : "") +
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
    '<div class="card"><p class="muted">Isi detail sumbernya, pilih gaya sitasi, langsung jadi.</p>' +
    '<div style="border:1.5px dashed #75909e;border-radius:10px;padding:10px 12px;margin-bottom:12px">' +
    '<label for="sitFile" style="margin-top:0">' + tic("doc") + ' Isi otomatis dari file jurnal (.pdf/.docx/.txt)</label>' +
    '<input type="file" id="sitFile" accept="' + ACCEPT_DOK + '">' +
    '<p class="muted tiny" style="margin:6px 0 0">Judul, penulis, tahun, dan jurnal dibaca otomatis dari file. Hasil baca otomatis bisa meleset, jadi <b>wajib cek lagi</b> isinya sebelum dipakai ya.</p>' +
    '<div id="sitCek"></div></div>' +
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
    '<button class="btn btn-primary btn-block" id="btnBuatSit">' + tic("doc") + ' Buatkan Sitasi</button></div>' +
    '<div id="sitHasil"></div>';
  let gaya = "harvard";
  $("#sitFile").addEventListener("change", async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const cek = $("#sitCek");
    cek.innerHTML = '<p class="muted tiny" style="margin-top:8px">Lagi membaca file...</p>';
    const fd = new FormData();
    fd.append("file", f);
    // FIX 2026-11-11: handler ini dulu memakai variabel "btn" yang tidak
    // pernah didefinisikan di sini, jadi ReferenceError meledak SETELAH
    // teks "Lagi membaca file..." tampil tetapi SEBELUM unggahan jalan;
    // halaman macet diam diam selamanya (temuan audit gelombang 3).
    const btnSit = $("#btnBuatSit");
    if (btnSit) btnSit.disabled = true;
    let res = await unggahForm("/api/sitasi-pdf/job", fd);
    if (btnSit) btnSit.disabled = false;
    if (!res.ok) { cek.innerHTML = ""; toast(" " + errFile(res)); return; }
    const sjid = res.data && res.data.id;
    if (!sjid) { cek.innerHTML = ""; toast(" " + errFile(res)); return; }
    // LAPIS 4 (2026-10-29): pemrosesan jalan di antrean worker; poll
    // status tiap 3 detik sampai selesai/gagal.
    let sdata = null;
    // FIX 2026-11-08 (audit putaran 2): polling diberi batas 100 putaran
    // (5 menit) + toleransi 3 gagal jaringan beruntun. Pola lama
    // while(true): pekerjaan yang hilang di server membuat pengguna
    // menunggu selamanya tanpa pesan.
    let gagalBeruntun = 0;
    for (let putaran = 0; putaran < 100 && !sdata; putaran++) {
      await new Promise(r => setTimeout(r, 3000));
      if (!cek.isConnected) return;
      const st = await apiJSON("/sitasi-pdf/job-status?id=" + encodeURIComponent(sjid));
      const sd = st.data || {};
      if (st.ok && sd.status === "selesai") { sdata = sd; break; }
      if (st.ok && sd.status === "gagal") { cek.innerHTML = ""; toast(" " + (sd.error || errFile(st))); return; }
      if (!st.ok) {
        gagalBeruntun++;
        if (gagalBeruntun >= 3) {
          cek.innerHTML = "";
          toast(" Pekerjaan membaca PDF ini sepertinya hilang di server. Coba unggah lagi ya.");
          return;
        }
      } else {
        gagalBeruntun = 0;
      }
    }
    if (!sdata) {
      cek.innerHTML = "";
      toast(" Membaca PDF ini kelamaan. Pekerjaannya mungkin hilang, coba unggah lagi ya.");
      return;
    }
    res = { ok: true, data: sdata };
    const w = (res.data && res.data.work) || {};
    if (w.judul) $("#sitJudul").value = w.judul;
    if (Array.isArray(w.penulis) && w.penulis.length) $("#sitPenulis").value = w.penulis.join(", ");
    if (w.tahun) $("#sitTahun").value = w.tahun;
    if (w.jurnal) $("#sitJurnal").value = w.jurnal;
    if (w.volume) $("#sitVolume").value = w.volume;
    if (w.pages) $("#sitPages").value = w.pages;
    const dariDoi = res.data.sumber === "doi";
    cek.innerHTML = '<div class="warn-box" style="margin-top:10px;text-align:left">' + tic("doc") + ' Kolom di bawah sudah diisi otomatis dari file' +
      (dariDoi ? " (metadata resmi dari database jurnal, ketemu lewat DOI di file itu)." : " (ditebak dari teks halaman pertama file).") +
      ' <b>Cek lagi satu per satu ya</b>, terutama judul, nama penulis, dan tahunnya, sebelum klik Buatkan Sitasi.</div>';
    toast("Kolom terisi dari file. Cek lagi isinya ya ");
  });
  $$("#sitGaya .chip").forEach(c => c.addEventListener("click", () => {
    $$("#sitGaya .chip").forEach(x => x.classList.remove("active"));
    c.classList.add("active"); gaya = c.dataset.gaya;
  }));
  $("#btnBuatSit").addEventListener("click", async (ev) => {
    // FIX 2026-11-08 (audit putaran 2): kunci tombol selama request
    // berjalan; klik ganda sebelumnya membakar kuota tamu dua kali.
    const btnSit = ev.currentTarget;
    if (btnSit.disabled) return;
    btnSit.disabled = true;
    try {
    const work = {
      judul: $("#sitJudul").value.trim(),
      penulis: $("#sitPenulis").value.trim(),
      tahun: $("#sitTahun").value.trim(),
      jurnal: $("#sitJurnal").value.trim(),
      volume: $("#sitVolume").value.trim(),
      pages: $("#sitPages").value.trim()
    };
    if (!work.judul) { toast("Isi dulu judulnya "); return; }
    const box = $("#sitHasil");
    box.innerHTML = '<p class="loading">Lagi nyusun sitasi...</p>';
    const res = await apiPOST("/sitasi", { work: work, gaya: gaya });
    if (!res.ok) { if (!kuotaTamuBox(box, res)) box.innerHTML = '<div class="err-box">Gagal bikin sitasi, coba lagi ya.</div>'; return; }
    const teks = res.data && (res.data.sitasi || res.data.hasil || res.data.text);
    if (!teks) { box.innerHTML = '<div class="err-box">Server nggak ngasih sitasi, coba lagi ya.</div>'; return; }
    box.innerHTML = '<div class="card hasil"><h3>Hasil sitasi (' + esc((res.data && res.data.gaya) || gaya) + ")</h3>" +
      '<div class="sitasi-hasil"><pre>' + esc(teks) + "</pre></div>" +
      '<button class="btn btn-accent" id="btnSalinSit">' + tic("clipboard") + ' Salin</button></div>';
    riwTamuCatat("sitasi", work.judul || "", { gaya: (res.data && res.data.gaya) || gaya, sitasi: teks, work: work });
    $("#btnSalinSit").addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(teks); toast("Disalin! "); }
      catch (e) { toast("Gagal nyalin "); }
    });
    } finally { btnSit.disabled = false; }
  });
}

/* ----- Masuk ----- */
function pageMasuk() {
  const q = (location.hash.split("?")[1] || "");
  const err = new URLSearchParams(q).get("err");
  const errMsg = err === "batal" ? "Login Google dibatalkan. "
    : err === "noconfig" ? "Login Google belum dikonfigurasi admin. "
    : err ? "Login gagal, coba lagi ya. " : "";
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
      if (!ident) { toast("Isi dulu username atau email kamu "); if (identEl) identEl.focus(); return; }
      setLoading(resendBtn, true, "Mengirim...");
      var r = await apiPOST("/auth/kirim-ulang", { email: ident });
      setLoading(resendBtn, false);
      toast(r.ok ? "Dikirim! Cek email ya " : " " + pesanErrAkun(r, "Server lagi bermasalah, gagal mengirim. Coba lagi beberapa saat ya"));
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
      if (!ident || !pass || !mail) { toast("Lengkapi username/email, password, dan email baru ya "); return; }
      if (msgEl) msgEl.textContent = "Lagi mengganti email dan mengirim link baru...";
      setLoading(submitBtn, true, "Memproses...");
      var r = await apiPOST("/auth/ganti-email-pending", { username: ident, password: pass, email_baru: mail });
      setLoading(submitBtn, false);
      if (r.ok) {
        opts.email = mail;
        if (msgEl) msgEl.textContent = "Berhasil! Link verifikasi baru sudah dikirim ke " + mail + ". Cek Inbox dan Spam ya.";
        if (passEl) passEl.value = "";
        if (opts.onChanged) opts.onChanged(mail);
        toast("Email berhasil diganti ");
      } else {
        var emsg = pesanErrAkun(r, "Server lagi bermasalah, gagal mengganti email. Coba lagi beberapa saat ya");
        if (msgEl) msgEl.textContent = emsg;
        toast(" " + emsg);
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
    '<p class="muted">Login biar saldomu aman & bisa dipakai di semua perangkat.</p>' +
    '<div class="chip-row" style="justify-content:center;margin-bottom:10px">' +
    '<button class="chip active" id="tabPwMasuk">Masuk</button>' +
    '<button class="chip" id="tabPwDaftar">Daftar</button></div>' +
    '<div id="pwMasukBox">' +
    '<input type="text" id="pwUserIn" placeholder="username atau email" autocomplete="username" autocapitalize="off" spellcheck="false">' +
    '<div class="pw-wrap"><input type="password" id="pwPassIn" placeholder="password" autocomplete="current-password">' +
    '<button type="button" class="pw-eye" data-for="pwPassIn" aria-label="Tampilkan password">' + tic("eye") + '</button></div>' +
    '<button class="btn btn-primary btn-block" id="btnPwMasuk">Masuk</button>' +
    '<p class="center tiny" style="margin-top:8px"><a href="#/lupa">Lupa password?</a> · <a href="#" id="linkResend">Belum terima email verifikasi?</a></p></div>' +
    '<div id="loginVerifyHelp" class="hidden">' + verifyHelpHTML("lv") + '</div>' +
    '<div id="pwDaftarBox" class="hidden">' +
    '<input type="text" id="pwUserUp" placeholder="pilih username" autocomplete="username" autocapitalize="off" spellcheck="false">' +
    '<input type="email" id="pwMailUp" placeholder="email aktif" autocomplete="email" autocapitalize="off" spellcheck="false">' +
    '<div class="pw-wrap"><input type="password" id="pwPassUp" placeholder="password (min. 8 karakter)" autocomplete="new-password">' +
    '<button type="button" class="pw-eye" data-for="pwPassUp" aria-label="Tampilkan password">' + tic("eye") + '</button></div>' +
    '<button class="btn btn-accent btn-block" id="btnPwDaftar">Daftar Akun</button>' +
    '<p class="muted tiny">Kami kirim link verifikasi ke emailmu</p></div>' +
    '<div class="login-or"><span>atau</span></div>' +
    '<a class="btn btn-block" href="#/beranda">' + tic("person") + ' Lanjut sebagai tamu</a>' +
    '<p class="muted tiny">Mode tamu tetap bisa pakai semua fitur gratis. Tapi top-up wajib login biar saldonya nggak hilang.</p></div>';
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
  // toggle intip password (ikon mata SVG)
  $$(".pw-eye", view()).forEach(b => b.addEventListener("click", () => {
    const inp = $("#" + b.dataset.for, view());
    if (!inp) return;
    const show = inp.type === "password";
    inp.type = show ? "text" : "password";
    b.innerHTML = show ? tic("eye-off") : tic("eye");
    b.setAttribute("aria-label", show ? "Sembunyikan password" : "Tampilkan password");
  }));
  let pwSibuk = false;
  async function pwAuth(mode) {
    // FIX 2026-11-08 (audit putaran 2): cegah dobel kirim login/daftar.
    if (pwSibuk) return;
    const u = $(mode === "in" ? "#pwUserIn" : "#pwUserUp").value.trim();
    const p = $(mode === "in" ? "#pwPassIn" : "#pwPassUp").value;
    const m = mode === "up" ? $("#pwMailUp").value.trim() : "";
    if (!u || !p || (mode === "up" && !m)) { toast("Lengkapi dulu ya "); return; }
    const payload = mode === "in" ? { username: u, password: p }
                                  : { username: u, password: p, email: m };
    // K-1: bawa kode referral (bila ada) supaya diproses saat verifikasi email
    if (mode === "up") {
      try {
        const rk = localStorage.getItem("kp_ref_pending");
        if (rk) payload.ref = rk;
      } catch (e) {}
    }
    pwSibuk = true;
    const btnAuth = $(mode === "in" ? "#btnPwMasuk" : "#btnPwDaftar");
    if (btnAuth) btnAuth.disabled = true;
    const res = await apiPOST(mode === "in" ? "/auth/login" : "/auth/register", payload)
      .finally(() => { pwSibuk = false; if (btnAuth) btnAuth.disabled = false; });
    if (res.ok) {
      if (mode === "up" && res.data && res.data.need_verify) {
        view().innerHTML =
          "<h2>"+tic("mail")+" Cek Email</h2>" +
          '<div class="card center"><p>Akun <b>' + esc(u) + '</b> dibuat!</p>' +
          '<p class="muted">Kami kirim link verifikasi ke <b id="regEmail">' + esc(m) + '</b>. ' +
          "Klik linknya, terus masuk deh.</p>" +
          verifyHelpHTML("rv") +
          '<a class="btn btn-primary" href="#/masuk">Ke halaman Masuk</a></div>';
        wireVerifyHelp("rv", { ident: u, email: m, onChanged: function (mail) {
          var el = document.getElementById("regEmail");
          if (el) el.textContent = mail;
        }});
        return;
      }
      await refreshAuth();
      toast(mode === "in" ? "Selamat datang kembali! " : "Akun dibuat! ");
      location.hash = "#/akun";
    } else {
      const konteks = mode === "in"
        ? "Belum bisa masuk sekarang, server lagi bermasalah. Coba lagi beberapa saat ya"
        : "Belum bisa daftar sekarang, server lagi bermasalah. Coba lagi beberapa saat ya";
      const msg = pesanErrAkun(res, konteks);
      if (mode === "in" && res.status === 403 && msg.indexOf("verifikasi email") >= 0) {
        showLoginVerifyHelp(u);
      }
      toast(msg);
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
    "Kalau terdaftar, kami kirim link reset ke sana.</p>" +
    '<input type="email" id="lupaMail" placeholder="email" autocomplete="email" autocapitalize="off" spellcheck="false">' +
    '<button class="btn btn-primary btn-block" id="btnLupa" style="margin-top:12px">Kirim Link Reset</button>' +
    '<p class="center tiny" style="margin-top:8px"><a href="#/masuk">← Balik ke Masuk</a></p></div>';
  const go = async () => {
    const m = $("#lupaMail").value.trim();
    if (!m) { toast("Isi emailnya dulu "); return; }
    const btn = $("#btnLupa"); setLoading(btn, true, "Ngirim...");
    const res = await apiPOST("/auth/lupa", { email: m });
    setLoading(btn, false);
    toast(res.ok ? " " + ((res.data && res.data.msg) || "dikirim!") : " coba lagi ya");
  };
  $("#btnLupa").addEventListener("click", go);
  $("#lupaMail").addEventListener("keydown", e => { if (e.key === "Enter") go(); });
}

/* ----- Reset password (dari link email) ----- */
function pageReset() {
  const token = new URLSearchParams((location.hash.split("?")[1] || "")).get("token") || "";
  if (!token) {
    view().innerHTML = "<h2>"+tic("key")+" Reset Password</h2>" +
      '<div class="err-box">Link resetnya nggak lengkap Minta link baru dari halaman <a href="#/lupa">Lupa Password</a> ya.</div>';
    return;
  }
  view().innerHTML =
    "<h2>"+tic("key")+" Password Baru</h2>" +
    '<div class="card center"><p class="muted">Bikin password baru buat akunmu (min. 8 karakter).</p>' +
    '<div class="pw-wrap"><input type="password" id="rstPw1" placeholder="password baru" autocomplete="new-password">' +
    '<button type="button" class="pw-eye" data-for="rstPw1" aria-label="Tampilkan password">' + tic("eye") + '</button></div>' +
    '<div class="pw-wrap"><input type="password" id="rstPw2" placeholder="ulangi password baru" autocomplete="new-password">' +
    '<button type="button" class="pw-eye" data-for="rstPw2" aria-label="Tampilkan password">' + tic("eye") + '</button></div>' +
    '<button class="btn btn-primary btn-block" id="btnReset" style="margin-top:12px">Ganti Password</button></div>';
  $$(".pw-eye", view()).forEach(b => b.addEventListener("click", () => {
    const inp = $("#" + b.dataset.for, view());
    if (!inp) return;
    const show = inp.type === "password";
    inp.type = show ? "text" : "password";
    b.innerHTML = show ? tic("eye-off") : tic("eye");
  }));
  $("#btnReset").addEventListener("click", async () => {
    const p1 = $("#rstPw1").value, p2 = $("#rstPw2").value;
    if (p1 !== p2) { toast("Passwordnya nggak sama "); return; }
    if (p1.length < 8) { toast("Password minimal 8 karakter ya "); return; }
    const btn = $("#btnReset"); setLoading(btn, true, "Nyimpen...");
    const res = await apiPOST("/auth/reset", { token: token, password: p1 });
    setLoading(btn, false);
    if (res.ok) {
      view().innerHTML = "<h2>"+tic("check")+" Berhasil!</h2>" +
        '<div class="card center"><p>Password berhasil diganti! Silakan masuk pakai password barumu.</p>' +
        '<a class="btn btn-primary btn-block" href="#/masuk">Masuk</a></div>';
    } else {
      toast(" " + pesanErrAkun(res, "gagal, coba minta link baru"));
    }
  });
}

/* ----- Verifikasi email (dari link email) ----- */
function pageVerifikasi() {
  const token = new URLSearchParams((location.hash.split("?")[1] || "")).get("token") || "";
  view().innerHTML = "<h2>"+tic("mail")+" Verifikasi Email</h2>" +
    '<div class="card center"><p class="loading">Lagi verifikasi...</p></div>';
  (async () => {
    if (!token) {
      view().innerHTML = "<h2>"+tic("mail")+" Verifikasi Email</h2>" +
        '<div class="err-box">Link verifikasinya nggak lengkap</div>';
      return;
    }
    const res = await apiPOST("/auth/verifikasi", { token: token });
    if (res.ok) {
      await refreshAuth();
      flushRefPending(); // K-1: klaim referral tepat setelah verifikasi email
      view().innerHTML = "<h2>"+tic("check")+" Email Terverifikasi!</h2>" +
        '<div class="card center"><p>Akunmu aktif! Selamat datang di KhairyPustaka</p>' +
        '<a class="btn btn-primary btn-block" href="#/akun">Ke Akun Saya</a></div>';
    } else {
      view().innerHTML = "<h2>"+tic("mail")+" Verifikasi Email</h2>" +
        '<div class="err-box"> ' + esc(pesanErrAkun(res, "verifikasi gagal")) + "</div>" +
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
  el.style.color = ok ? "#2dd4d8" : "#ff6b6f";
}

function pageAkun() {
  if (!AUTH.login) {
    view().innerHTML =
      "<h2>"+tic("person")+" Akun</h2>" +
      '<div class="card center"><img src="' + avatarURL("teal") + '" alt="maskot" class="akun-foto">' +
      '<p class="muted">Kamu lagi mode tamu. Login biar saldo & datamu aman di semua perangkat.</p>' +
      '<a class="btn btn-primary btn-block" href="#/masuk">' + tic("key") + ' Masuk / Daftar</a></div>' + kartuPasangAppHTML();
    ikatTombolPasang();
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
    (u.admin ? '<div><span class="badge-admin">ADMIN</span></div>' : "") +
    (isPw
      ? '<p class="pf-akun-info">' + tic("person") + ' @' + esc(u.username || "") + "</p>" +
        '<p class="pf-akun-info">' + tic("mail") + ' ' + esc(u.email || "") + "</p>"
      : (via ? '<p class="muted">' + esc(via) + "</p>" : "")) +
    (u.admin ? "" : '<a class="btn btn-primary btn-block" href="#/topup">' + tic("bolt") + ' Jatah AI</a>') +
    (u.admin ? '<a class="btn btn-block" href="#/admin">' + tic("shield") + ' Panel Admin</a>' : "") +
    '<button class="btn btn-block" id="btnLogout">' + tic("logout") + ' Keluar</button></div>' + kartuPasangAppHTML() +
    (!u.foto ?
      '<div class="card"><h3>' + tic("image") + ' Foto Profil</h3><p class="muted">Pilih maskot favoritmu:</p>' +
      '<div class="avatar-grid">' + AVATAR_PILIHAN.map(function(p) {
        return '<button class="avatar-opt' + (avatarURL(u.avatar) === avatarURL(p) ? " active" : "") +
          '" data-av="' + p + '" aria-label="Avatar ' + p + '"><img src="' + avatarURL(p) + '" alt="avatar ' + p + '"></button>';
      }).join("") + '</div><p class="pf-msg" id="pfAvMsg"></p></div>'
      : "") +
    (isPw ?
      '<div class="card"><h3>' + tic("pencil") + ' Ubah Username</h3>' +
      '<input type="text" id="pfUserBaru" placeholder="username baru (3–20 karakter)" maxlength="20" autocapitalize="off" autocomplete="off">' +
      '<p class="pf-msg" id="pfUserMsg"></p>' +
      '<button class="btn btn-primary btn-block" id="btnUbahUser">Simpan Username</button></div>' +
      '<div class="card"><h3>' + tic("key") + ' Ubah Password</h3>' +
      '<div class="pw-wrap"><input type="password" id="pfPwLama" placeholder="password lama" autocomplete="current-password">' +
      '<button type="button" class="pw-eye" data-for="pfPwLama" aria-label="Tampilkan password">' + tic("eye") + '</button></div>' +
      '<div class="pw-wrap"><input type="password" id="pfPwBaru" placeholder="password baru (min. 8 karakter)" autocomplete="new-password">' +
      '<button type="button" class="pw-eye" data-for="pfPwBaru" aria-label="Tampilkan password">' + tic("eye") + '</button></div>' +
      '<p class="pf-msg" id="pfPwMsg"></p>' +
      '<button class="btn btn-primary btn-block" id="btnUbahPw">Simpan Password</button></div>' +
      '<div class="card"><h3>' + tic("mail") + ' Ubah Email</h3>' +
      '<p class="muted">Link verifikasi dikirim ke email barumu (berlaku 24 jam). Email lama tetap aktif sampai kamu klik link itu.</p>' +
      '<input type="email" id="pfMailBaru" placeholder="email baru" autocapitalize="off" autocomplete="off">' +
      '<div class="pw-wrap"><input type="password" id="pfMailPw" placeholder="konfirmasi password" autocomplete="current-password">' +
      '<button type="button" class="pw-eye" data-for="pfMailPw" aria-label="Tampilkan password">' + tic("eye") + '</button></div>' +
      '<p class="pf-msg" id="pfMailMsg"></p>' +
      '<button class="btn btn-primary btn-block" id="btnUbahMail">Kirim Link Verifikasi</button></div>'
      : '<div class="card"><p class="muted">Akun ini login ' + esc(via || "via Google") +
        " — ubah username, password & email hanya tersedia untuk akun username+password.</p></div>");
  ikatTombolPasang();
  $("#btnLogout").addEventListener("click", async () => {
    await apiPOST("/auth/logout", {});
    AUTH = { login: false };
    await refreshAuth();
    toast("Sampai jumpa! ");
    location.hash = "#/beranda";
  });
  // pilih avatar maskot  (klik = langsung simpan)
  $$(".avatar-opt", view()).forEach(function(b) {
    b.addEventListener("click", async () => {
      const p = b.getAttribute("data-av");
      const res = await apiPOST("/auth/ubah-avatar", { avatar: p });
      if (res.ok) {
        await refreshAuth();
        pageAkun();
        toast("Foto profil diganti ");
      } else {
        pfMsg("pfAvMsg", false, " " + ((res.data && res.data.error) || "gagal, coba lagi ya"));
      }
    });
  });
  if (!isPw) return;

  // toggle intip password (ikon mata SVG)
  $$(".pw-eye", view()).forEach(b => b.addEventListener("click", () => {
    const inp = $("#" + b.dataset.for, view());
    if (!inp) return;
    const show = inp.type === "password";
    inp.type = show ? "text" : "password";
    b.innerHTML = show ? tic("eye-off") : tic("eye");
    b.setAttribute("aria-label", show ? "Sembunyikan password" : "Tampilkan password");
  }));

  $("#btnUbahUser").addEventListener("click", async () => {
    const v = $("#pfUserBaru", view()).value.trim();
    if (!v) { pfMsg("pfUserMsg", false, "isi username barunya dulu "); return; }
    const btn = $("#btnUbahUser"); setLoading(btn, true, "Nyimpen...");
    const res = await apiPOST("/auth/ubah-username", { username_baru: v });
    setLoading(btn, false);
    if (res.ok) {
      await refreshAuth();
      pageAkun();
      toast("Username berhasil diganti ");
    } else {
      pfMsg("pfUserMsg", false, " " + ((res.data && res.data.error) || "gagal"));
    }
  });

  $("#btnUbahPw").addEventListener("click", async () => {
    const lama = $("#pfPwLama", view()).value, baru = $("#pfPwBaru", view()).value;
    if (!lama || !baru) { pfMsg("pfPwMsg", false, "isi password lama & barunya "); return; }
    if (baru.length < 8) { pfMsg("pfPwMsg", false, "password baru minimal 8 karakter ya "); return; }
    const btn = $("#btnUbahPw"); setLoading(btn, true, "Nyimpen...");
    const res = await apiPOST("/auth/ubah-password", { password_lama: lama, password_baru: baru });
    setLoading(btn, false);
    if (res.ok) {
      pfMsg("pfPwMsg", true, (res.data && res.data.msg) || "password diganti! ");
      $("#pfPwLama", view()).value = ""; $("#pfPwBaru", view()).value = "";
    } else {
      pfMsg("pfPwMsg", false, " " + ((res.data && res.data.error) || "gagal"));
    }
  });

  $("#btnUbahMail").addEventListener("click", async () => {
    const v = $("#pfMailBaru", view()).value.trim();
    if (!v) { pfMsg("pfMailMsg", false, "isi email barunya dulu "); return; }
    const pw = $("#pfMailPw", view()).value;
    if (!pw) { pfMsg("pfMailMsg", false, "isi password buat konfirmasi "); return; }
    const btn = $("#btnUbahMail"); setLoading(btn, true, "Ngirim...");
    const res = await apiPOST("/auth/ubah-email", { email_baru: v, password: pw });
    setLoading(btn, false);
    $("#pfMailPw", view()).value = "";
    if (res.ok) {
      pfMsg("pfMailMsg", true, (res.data && res.data.msg) || "link terkirim! ");
      $("#pfMailBaru", view()).value = "";
    } else {
      pfMsg("pfMailMsg", false, " " + ((res.data && res.data.error) || "gagal"));
    }
  });
}

/* ----- Konfirmasi ganti email (dari link email) ----- */
function pageKonfirmasiEmail() {
  const token = new URLSearchParams((location.hash.split("?")[1] || "")).get("token") || "";
  view().innerHTML = "<h2>"+tic("mail")+" Konfirmasi Email</h2>" +
    '<div class="card center"><p class="loading">Lagi konfirmasi...</p></div>';
  (async () => {
    if (!token) {
      view().innerHTML = "<h2>"+tic("mail")+" Konfirmasi Email</h2>" +
        '<div class="err-box">Link konfirmasinya nggak lengkap</div>';
      return;
    }
    const res = await apiJSON("/auth/konfirmasi-email?token=" + encodeURIComponent(token));
    if (res.ok) {
      await refreshAuth();
      view().innerHTML = "<h2>"+tic("check")+" Email Diganti!</h2>" +
        '<div class="card center"><p>Email akunmu berhasil diganti!</p>' +
        '<a class="btn btn-primary btn-block" href="#/akun">Ke Akun Saya</a></div>';
    } else {
      view().innerHTML = "<h2>"+tic("mail")+" Konfirmasi Email</h2>" +
        '<div class="err-box"> ' + esc((res.data && res.data.error) || "konfirmasi gagal") + "</div>" +
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
      ["#/mendalam", "spark", "Cari Jurnal Mendalam", "Cari, bedah, sampai laporan riset utuh", "premium"],
      ["#/cari", "search", "Cari Jurnal", "OpenAlex, Crossref, Sinta, Garuda"],
      ["#/sitasi", "quote", "Buat Sitasi", "Sitasi 6 gaya otomatis"],
      ["#/ai", "spark", "AI Studio", "14 generator naskah tugas & skripsi"],
      ["#/kemiripan", "check", "Cek Plagiarisme", "Cek kemiripan naskah vs sumbermu"],
      ["#/convert?alat=word_pdf", "doc", "Word ke PDF", "Ubah file Word jadi PDF"]
    ]],
    ["riset", tic("search")+" Riset", [
      ["#/cari", "search", "Cari Jurnal", "OpenAlex, Crossref, Sinta, Garuda"],
      ["#/carifile", "doc", "Cari dari Sitasi", "Paste sitasi, cari filenya"],
      ["#/rekomendasi", "target", "Rekomendasi Jurnal", "Jurnal buat publikasi"],
      ["#/publisher", "building", "Publisher", "Jelajahi penerbit & jurnal"],
      ["#/penulis", "person", "Info Penulis", "H-index, karya, afiliasi"],
      ["#/matriks", "grid", "Matriks", "Tabel sintesis literatur"],
      ["#/koleksi", "books", "Koleksiku", "Simpan & export referensi"]
    ]],
    ["ai", tic("spark")+" AI", [
      ["#/ai", "spark", "AI Studio", "14 generator naskah tugas & skripsi"],
      ["#/tanya", "chat", "Tanya AI", "Tanya apa aja soal tugas & skripsi"],
      ["#/parafrase", "refresh", "Parafrase", "Ubah kalimat biar fresh"],
      ["#/terjemah", "globe", "Terjemah Abstrak", "Abstrak Inggris jadi Indonesia"],
      ["#/topik", "bulb", "Ide Topik", "Tren & research gap"]
    ]],
    ["convert", tic("doc")+" Convert", [
      ["#/convert?alat=word_pdf", "doc", "Word ke PDF", ""],
      ["#/convert?alat=pdf_word", "doc", "PDF ke Word", ""],
      ["#/convert?alat=gabung", "doc", "Gabung PDF", ""],
      ["#/convert?alat=pisah", "doc", "Pisah PDF", ""],
      ["#/convert?alat=kompres", "doc", "Kompres PDF", ""],
      ["#/convert?alat=gambar_pdf", "doc", "Gambar ke PDF", ""],
      ["#/convert?alat=pdf_gambar", "doc", "PDF ke Gambar", ""],
      ["#/ekstrak-teks", "doc", "Ekstrak Teks", "Ambil teks dari PDF, Word, TXT"]
    ]],
    ["nulis", tic("pencil")+" Nulis", [
      ["#/sitasi", "quote", "Buat Sitasi", "Sitasi 6 gaya otomatis"],
      ["#/kemiripan", "check", "Cek Plagiarisme", "Cek kemiripan naskah vs sumbermu"],
      ["#/template", "layout", "Template", "Template proposal & skripsi"],
      ["#/statistik", "chart", "Statistik", "Panduan pilih uji statistik"]
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
  function saringFeat(feats) {
    // Admin & tamu: sembunyikan SEMUA pintu masuk halaman Jatah AI
    // (admin: instruksi Sul; tamu: perintah Sul 2026-10-18).
    if (AUTH.admin || !AUTH.login) return feats.filter(f => f[0] !== "#/topup");
    return feats;
  }
  // FIX 2026-11-08 (audit putaran 2): peta href kartu -> nama sakelar
  // tool di server, supaya kartuHTML bisa menerbitkan data-tool dan
  // tandaiToolNonaktif() bekerja (sebelumnya tidak ada kartu yang punya
  // atribut itu, jadi penanda nonaktif tidak pernah menempel).
  function toolDariHref(href) {
    const h = String(href || "").split("?")[0];
    if (h === "#/convert") return "convert";
    return {
      "#/cari": "cari", "#/carifile": "cari", "#/rekomendasi": "cari",
      "#/publisher": "cari", "#/penulis": "cari",
      "#/sitasi": "sitasi", "#/ai": "ai", "#/tanya": "ai",
      "#/topik": "idejudul", "#/statistik": "statistik",
      "#/terjemah": "terjemah"
    }[h] || "";
  }
  function kartuHTML(feats) {
    return feats.map((f, i) => {
      const tool = toolDariHref(f[0]);
      const prem = f[4] === "premium";
      return '<a class="feat' + (prem ? " premium" : "") + '" style="animation-delay:' + (i * 55) + 'ms" href="' + f[0] + '"' +
        (tool ? ' data-tool="' + tool + '"' : "") + '>' +
        (prem ? '<span class="feat-badge">PREMIUM</span>' : "") +
        '<span class="ico">' + tic(f[1]) + "</span><b>" + f[2] + "</b><small>" + f[3] + "</small></a>";
    }).join("");
  }
  function gridHTML() {
    return kartuHTML(saringFeat(KATS.find(k => k[0] === aktif)[2]));
  }
  function setNote(n) {
    const el = $("#featNote");
    if (el) el.textContent = "Menampilkan " + n + " alat";
  }
  view().innerHTML =
    '<div id="bannerPengumuman"></div>' +
    '<section class="hero">' +
      '<div class="hero-text">' +
        '<span class="pill"><span class="dot"></span> Gratis untuk semua mahasiswa</span>' +
        "<h1>Teman seperjuangan nugas dan <em>skripsimu</em></h1>" +
        '<p class="hero-sub">Asisten skripsi dan nugas anti begadang: cari jurnal, bikin sitasi, sampai nulis bab, semua bisa dari HP.</p>' +
        '<div class="hero-cta">' +
          '<a class="btn btn-primary btn-hero" href="#/cari">' + tic("search") + ' Mulai Cari Jurnal</a>' +
          '<a class="btn btn-ghost btn-hero" href="/panduan">Lihat Panduan</a>' +
        "</div>" +
      "</div>" +
      '<div class="hero-mascot"><img src="maskot.jpg" alt="Maskot KhairyPustaka"></div>' +
    "</section>" +
    '<section class="fakta-strip" aria-label="Fakta KhairyPustaka">' +
      '<div class="fakta"><b>24</b><span>Alat</span></div>' +
      '<div class="fakta"><b>5</b><span>Model AI</span></div>' +
      '<div class="fakta"><b>6</b><span>Gaya Sitasi</span></div>' +
      '<div class="fakta fakta-free"><b>100% Gratis</b><span>Untuk semua mahasiswa</span></div>' +
    "</section>" +
    '<div class="filters-wrap">' +
    '<div class="filter-row" role="tablist">' +
      KATS.map(k =>
        '<button class="filter-chip' + (k[0] === aktif ? " active" : "") + '" data-kat="' + k[0] + '">' + k[1] + "</button>"
      ).join("") +
    "</div>" +
    '<span class="result-note" id="featNote"></span>' +
    "</div>" +
    '<div class="grid" id="featGrid">' + gridHTML() + "</div>" +
    '<div class="card kp-show-card" style="margin-top:16px">' +
      "<h3>"+tic("spark")+" Postingan terbaru KhairyPustaka</h3>" +
      '<p class="muted">Video dan postingan edukasi dari akun resmi kami. Ketuk gambarnya buat buka langsung.</p>' +
      '<div class="kp-show-strip" id="kpShowStrip"><div class="kp-show-track" id="kpShowTrack">' + showcaseHTML() + "</div></div>" +
    "</div>";
  function tampilKategori() {
    const html = gridHTML();
    $("#featGrid").innerHTML = html;
    setNote(saringFeat(KATS.find(k => k[0] === aktif)[2]).length);
  }
  $$("#view .filter-chip").forEach(ch => ch.addEventListener("click", () => {
    aktif = ch.dataset.kat;
    try { sessionStorage.setItem("kp_home_tab", aktif); } catch (e) {}
    $$("#view .filter-chip").forEach(c => c.classList.toggle("active", c === ch));
    tampilKategori();
  }));
  setNote(saringFeat(KATS.find(k => k[0] === aktif)[2]).length);
  muatBannerPengumuman();
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

/* ----- Banner pengumuman (Fase 1 tema glass; sumber data Fase 4) -----
   Mengambil pengumuman aktif dari /api/pengumuman. Bila endpoint belum
   ada atau tidak ada pengumuman aktif, area ini tetap kosong: tidak ada
   contoh yang ditampilkan di situs live. */
async function muatBannerPengumuman() {
  const box = $("#bannerPengumuman");
  if (!box) return;
  try {
    const r = await apiJSON("/pengumuman");
    if (!r.ok || !r.data) return;
    _infoSitus = r.data;
    _infoSitusTs = Date.now();
    terapkanInfoSitus();
    if (!r.data.pengumuman) return;
    const p = r.data.pengumuman;
    const kunci = String(p.id || p.diperbarui || "");
    try {
      if (sessionStorage.getItem("kp_banner_tutup") === kunci) return;
    } catch (e) {}
    box.innerHTML =
      '<div class="banner-pengumuman" role="note">' +
        '<span class="banner-tag">Pengumuman</span>' +
        "<p>" + (p.judul ? "<b>" + esc(p.judul) + "</b> " : "") + esc(p.isi || "") + "</p>" +
        '<button class="banner-tutup" type="button" aria-label="Tutup pengumuman">✕</button>' +
      "</div>";
    const btn = box.querySelector(".banner-tutup");
    if (btn) btn.addEventListener("click", () => {
      try { sessionStorage.setItem("kp_banner_tutup", kunci); } catch (e) {}
      const el = box.querySelector(".banner-pengumuman");
      if (el) el.classList.add("tutup");
      setTimeout(() => { box.innerHTML = ""; }, 190);
    });
  } catch (e) { /* diam saja: banner bukan fitur kritis */ }
}

/* ----- Motion: riak teal saat elemen diklik (tema glass) -----
   Ringan: satu span per klik, transform + opacity saja, dan mati total
   bila pengguna memilih gerakan dikurangi. */
const KP_REDUCE_MOTION = !!(window.matchMedia &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches);
function kpRiak(host, x, y) {
  if (KP_REDUCE_MOTION || !host) return;
  try {
    const r = host.getBoundingClientRect();
    if (!r.width || !r.height) return;
    const d = Math.max(r.width, r.height) * 1.6;
    const s = document.createElement("span");
    s.className = "kp-ripple";
    s.style.width = s.style.height = d + "px";
    s.style.left = ((x == null ? r.width / 2 : x)) + "px";
    s.style.top = ((y == null ? r.height / 2 : y)) + "px";
    host.appendChild(s);
    setTimeout(() => { s.remove(); }, 300);
  } catch (e) {}
}
document.addEventListener("pointerdown", (e) => {
  if (KP_REDUCE_MOTION) return;
  const host = e.target && e.target.closest
    ? e.target.closest(".btn, .feat, .filter-chip, .chip, .tab, .banner-tutup")
    : null;
  if (!host) return;
  const r = host.getBoundingClientRect();
  kpRiak(host, e.clientX - r.left, e.clientY - r.top);
}, { passive: true });
document.addEventListener("keydown", (e) => {
  if (KP_REDUCE_MOTION || (e.key !== "Enter" && e.key !== " ")) return;
  const t = e.target;
  if (t && t.closest && t.closest(".btn, .feat, .filter-chip, .chip, .tab")) {
    kpRiak(t.closest(".btn, .feat, .filter-chip, .chip, .tab"), null, null);
  }
});

/* ----- Cari Jurnal ----- */
let cariState = { tahun: 0, hasil: [] };
const TAHUN_CHIPS = [[5, tic("clock")+" 5 thn terakhir"], [10, tic("calendar")+" 10 thn terakhir"], [0, tic("globe")+" Semua"]];

function pageCari() {
  view().innerHTML =
    "<h2>"+tic("search")+" Cari Jurnal</h2>" +
    '<p class="muted">Pencarian cepat dari katalog jurnal, untuk member bisa dipakai sepuasnya. Beda dari Cari Jurnal Mendalam yang membaca isi jurnalnya dan punya kuota harian.</p>' +
    '<div class="chip-row">' +
      TAHUN_CHIPS.map(c =>
        '<button class="filter-chip' + (cariState.tahun === c[0] ? " active" : "") +
        '" data-thn="' + c[0] + '">' + c[1] + "</button>").join("") +
    "</div>" +
    '<div class="card"><label for="q">Kata kunci</label>' +
    '<input type="text" id="q" placeholder="cth: stunting balita, employee performance, ..." autocomplete="off">' +
    '<button class="btn btn-primary btn-block" id="btnCari" style="margin-top:12px">' + tic("search") + ' Cari</button></div>' +
    '<div id="hasilCari">' + (cariState.hasil.length ? "" : '<p class="empty">Ketik kata kunci terus tekan Cari. Jurnal dari OpenAlex, Crossref, Sinta &amp; Garuda siap membantu.</p>') + "</div>";

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
  if (!cariState.hasil.length) { box.innerHTML = '<p class="empty">Nggak ketemu hasil. Coba kata kunci lain ya.</p>'; return; }
  const cat = Array.isArray(cariState.catatan) && cariState.catatan.length
    ? '<div class="warn-box tiny">' + cariState.catatan.map(esc).join("<br>") + "</div>" : "";
  box.innerHTML = cat +
    '<p class="muted">Ditemukan <b>' + cariState.hasil.length + "</b> hasil</p>" +
    cariState.hasil.map((h, i) => kartuHasil(h, i)).join("");
  wireKartu(box);
}

async function doCari() {
  const q = ($("#q").value || "").trim();
  if (!q) { toast("Ketik dulu kata kuncinya dong "); return; }
  const box = $("#hasilCari");
  box.innerHTML = '<p class="loading">Lagi nyari jurnal terbaik buatmu...</p>';
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
  if (teks.length < 30) { toast("Jurnal ini nggak punya abstrak buat diringkas "); return; }
  setLoading(btn, true, "AI meringkas...");
  const res = await apiPOST("/ai", { kind: "ringkas", text: teks.slice(0, 3000) });
  setLoading(btn, false);
  const d = res.data || {};
  if (!res.ok && (d.error === "kuota_habis" || d.kuota_habis)) { popupJatahHabis(); return; }
  if (!res.ok) { toast("AI-nya lagi gangguan, coba lagi ya "); return; }
  const hasil = d.hasil || d.result || d.text;
  if (!hasil) { toast("AI-nya bengong, coba lagi ya "); return; }
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
  if (!p) { toast("Jurnal ini nggak punya abstrak "); return; }
  if (btn.dataset.mode === "asli") {
    p.textContent = btn.dataset.asli || p.textContent;
    btn.innerHTML = tic("globe") + " Terjemah";
    btn.dataset.mode = "";
    return;
  }
  if (btn.dataset.terjemahan) {
    btn.dataset.asli = p.textContent;
    p.textContent = btn.dataset.terjemahan;
    btn.innerHTML = "↩ Lihat Asli";
    btn.dataset.mode = "asli";
    det.open = true;
    return;
  }
  const teks = (item.abstrak || "").trim();
  if (teks.length < 20) { toast("Abstraknya terlalu pendek buat diterjemahkan "); return; }
  setLoading(btn, true, "Menerjemahkan...");
  const res = await apiPOST("/terjemah", { teks: teks.slice(0, 3000) });
  setLoading(btn, false);
  const d = res.data || {};
  if (!res.ok) { toast("Terjemahannya gagal, coba lagi ya "); return; }
  if (d.sumber === "sudah_indonesia") { toast("Abstraknya sudah berbahasa Indonesia, tidak perlu diterjemahkan "); return; }
  if (!d.hasil) { toast("Terjemahannya kosong, coba lagi ya "); return; }
  btn.dataset.terjemahan = d.hasil;
  btn.dataset.asli = p.textContent;
  p.textContent = d.hasil;
  btn.innerHTML = "↩ Lihat Asli";
  btn.dataset.mode = "asli";
  det.open = true;
  toast(d.sumber === "argos" ? "Diterjemahkan (terjemahan offline)" : "Diterjemahkan (terjemahan mesin)");
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
      toast(r === "ok" ? "Kesimpen di Koleksiku! " : (r === "duplikat" ? "Udah ada di koleksimu " : "Gagal nyimpen (penyimpanan browser diblokir?). "));
    }
  }));
}

/* ----- Unggah file -> teks (dipakai banyak fitur) ----- */
const ACCEPT_DOK = ".docx,.txt,.pdf,text/plain,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document";
async function ekstrakTeksFile(file) {
  const fd = new FormData();
  fd.append("file", file);
  return unggahForm("/api/ekstrak-teks", fd);
}
async function unggahForm(path, fd) {
  try {
    const r = await fetch(path, { method: "POST", body: fd });
    const data = await r.json().catch(() => null);
    return { ok: r.ok, status: r.status, data: saringErrData(data) };
  } catch (e) { return { ok: false, status: 0, data: null }; }
}
function errFile(res) { return (res.data && res.data.error) || "file-nya tidak bisa dibaca"; }

/* ----- Cari dari Sitasi ----- */
let carifileHasil = []; // state lokal halaman ini (jangan pakai cariState)
function pageCarifile() {
  view().innerHTML =
    "<h2>"+tic("doc")+" Cari dari Sitasi</h2>" +
    '<div class="card"><p class="muted">Punya sitasi tapi nggak tahu filenya di mana? Paste aja di sini, biar aku yang lacak.</p>' +
    '<label for="teksSit">Teks sitasi</label>' +
    '<textarea id="teksSit" placeholder="cth: Sugiyono. (2019). Metode Penelitian Kuantitatif. Bandung: Alfabeta."></textarea>' +
    '<label for="cfFile" style="margin-top:10px">Atau unggah file daftar pustaka (.docx/.pdf/.txt)</label>' +
    '<input type="file" id="cfFile" accept="' + ACCEPT_DOK + '">' +
    '<p class="muted tiny">Teks dari file dimasukkan ke kotak di atas. Cek dulu isinya, baru klik Lacak File.</p>' +
    '<button class="btn btn-primary btn-block" id="btnCarifile" style="margin-top:12px">' + tic("search") + ' Lacak File</button></div>' +
    '<div id="hasilCarifile"></div>';
  $("#cfFile").addEventListener("change", async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    toast("Lagi membaca file... ");
    const res = await ekstrakTeksFile(f);
    if (!res.ok) { toast(" " + errFile(res)); return; }
    let teks = (res.data.teks || "").trim();
    let potong = "";
    if (teks.length > 30000) { teks = teks.slice(0, 30000); potong = " (dipotong ke 30.000 karakter pertama)"; }
    $("#teksSit").value = teks;
    toast("Teks dari file sudah dimasukkan" + potong + ". Cek dulu, lalu klik Lacak File ");
  });
  $("#btnCarifile").addEventListener("click", async () => {
    const teks = ($("#teksSit").value || "").trim();
    if (!teks) { toast("Paste dulu sitasinya "); return; }
    const box = $("#hasilCarifile");
    box.innerHTML = '<p class="loading">Lagi ngelacak...</p>';
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
      html += '<p class="muted">Ditemukan <b>' + found.length + "</b> karya</p>" +
        found.map((h, i) => kartuHasil(h, "cf" + i)).join("");
    }
    const miss = Array.isArray(d.tidak_ketemu) ? d.tidak_ketemu : [];
    if (miss.length) {
      html += '<div class="card"><h3>Tidak ketemu ' + tic("search") + '</h3><p class="muted tiny">Sitasi di bawah ini nggak kelacak sumbernya:</p><ul class="bersih">' +
        miss.map(m => "<li>" + esc(typeof m === "string" ? m : JSON.stringify(m)) + "</li>").join("") + "</ul></div>";
    }
    if (d.parsial) html += '<p class="muted tiny"> Sebagian sitasi belum diproses (waktu habis) — coba lagi dengan teks lebih pendek.</p>';
    if (!html) html = '<div class="warn-box">Hmm, belum nemu petunjuk file dari sitasi itu. Coba cek lagi teksnya udah lengkap atau belum.</div>';
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
        toast(r === "ok" ? "Kesimpen di Koleksiku! " : (r === "duplikat" ? "Udah ada di koleksimu " : "Gagal nyimpen (penyimpanan browser diblokir?). "));
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
    '<div class="card"><p class="muted">Mau publikasi? Kasih tahu topikmu, aku carikan jurnal yang cocok.</p>' +
    '<label for="topikRek">Topik / judul penelitian</label>' +
    '<input type="text" id="topikRek" placeholder="cth: pengaruh digital marketing terhadap UMKM">' +
    '<button class="btn btn-primary btn-block" id="btnRek" style="margin-top:12px">' + tic("target") + ' Carikan Jurnal</button></div>' +
    '<div id="hasilRek"></div>';
  $("#btnRek").addEventListener("click", async () => {
    const topik = ($("#topikRek").value || "").trim();
    if (!topik) { toast("Isi dulu topiknya "); return; }
    const box = $("#hasilRek");
    box.innerHTML = '<p class="loading">Lagi nyari jurnal yang cocok...</p>';
    const btn = $("#btnRek"); setLoading(btn, true, "Lagi nyari...");
    const res = await apiPOST("/rekomendasi", { topik: topik });
    setLoading(btn, false);
    if (!res.ok) { netErr(box, res); return; }
    const d = res.data || {};
    const daftar = Array.isArray(d.daftar) ? d.daftar : (Array.isArray(d.hasil) ? d.hasil : []);
    box.innerHTML = daftar.length
      ? '<div class="card"><h3>Jurnal yang cocok buatmu ' + tic("spark") + '</h3><ul class="bersih">' + daftar.map(daftarItemHTML).join("") + "</ul></div>"
      : '<div class="warn-box">Belum nemu rekomendasi buat topik itu. Coba kata kunci yang lebih umum ya.</div>';
  });
}

function pagePublisher() {
  view().innerHTML =
    "<h2>"+tic("building")+" Publisher</h2>" +
    '<div class="card"><p class="muted">Jelajahi penerbit &amp; jurnal per indeks. Tulis bidangnya dulu ya.</p>' +
    '<label for="bidangPub">Bidang *</label>' +
    '<input type="text" id="bidangPub" placeholder="cth: kesehatan, pendidikan, teknik">' +
    '<button class="btn btn-primary btn-block" id="btnPub" style="margin-top:12px">' + tic("building") + ' Tampilkan</button></div>' +
    '<div id="hasilPub"></div>';
  $("#btnPub").addEventListener("click", async () => {
    const bidang = ($("#bidangPub").value || "").trim();
    if (!bidang) { toast("Isi dulu bidangnya "); return; }
    const box = $("#hasilPub");
    box.innerHTML = '<p class="loading">Lagi ngumpulin data publisher...</p>';
    const btn = $("#btnPub"); setLoading(btn, true, "Lagi ngumpulin...");
    const res = await apiPOST("/publisher", { bidang: bidang });
    setLoading(btn, false);
    if (!res.ok) { netErr(box, res); return; }
    const d = res.data || {};
    const daftar = Array.isArray(d.daftar) ? d.daftar : [];
    box.innerHTML = daftar.length
      ? '<div class="card"><h3>Daftar publisher &amp; jurnal ' + tic("building") + '</h3><ul class="bersih">' + daftar.map(daftarItemHTML).join("") + "</ul></div>"
      : '<div class="warn-box">Datanya lagi kosong. Coba lagi nanti ya.</div>';
  });
}

function pagePenulis() {
  view().innerHTML =
    "<h2>"+tic("person")+" Info Penulis</h2>" +
    '<div class="card"><p class="muted">Kepoin profil peneliti: H-index, karya, sitasi, afiliasi.</p>' +
    '<label for="namaPen">Nama penulis</label>' +
    '<input type="text" id="namaPen" placeholder="cth: Budi Santoso">' +
    '<button class="btn btn-primary btn-block" id="btnPen" style="margin-top:12px">' + tic("person") + ' Cari Penulis</button></div>' +
    '<div id="hasilPen"></div>';
  $("#btnPen").addEventListener("click", async () => {
    const nama = ($("#namaPen").value || "").trim();
    if (!nama) { toast("Isi dulu namanya "); return; }
    const box = $("#hasilPen");
    box.innerHTML = '<p class="loading">Lagi nyari profilnya...</p>';
    const btn = $("#btnPen"); setLoading(btn, true, "Lagi nyari...");
    const res = await apiPOST("/penulis", { nama: nama });
    setLoading(btn, false);
    if (!res.ok) { netErr(box, res); return; }
    const d = res.data || {};
    const list = Array.isArray(d.daftar) ? d.daftar : (Array.isArray(d) ? d : []);
    if (!list.length) {
      box.innerHTML = '<div class="warn-box">Penulisnya nggak ketemu. Coba ejaan nama yang lain ya.</div>';
      return;
    }
    box.innerHTML = '<p class="muted">Ditemukan <b>' + list.length + "</b> penulis</p>" +
      list.map((a, i) => kartuPenulis(a, i)).join("");
  });
}

function kartuPenulis(a) {
  a = a || {};
  const afil = (a.affiliations || []).filter(Boolean).join(", ");
  const topik = (a.topics || []).filter(Boolean).join(", ");
  const profUrl = safeUrl(a.url);
  return '<div class="card"><h3>' + tic("person") + ' ' + esc(a.name || "-") + "</h3>" +
    '<ul class="bersih">' +
    (afil ? "<li>" + tic("building") + " <b>Afiliasi:</b> " + esc(afil) + "</li>" : "") +
    (a.h_index != null ? "<li>" + tic("chart") + " <b>H-index:</b> " + esc(a.h_index) + "</li>" : "") +
    (a.cited_by_count != null ? "<li>" + tic("doc") + " <b>Total sitasi:</b> " + esc(a.cited_by_count) + "</li>" : "") +
    (a.works_count != null ? "<li>" + tic("books") + " <b>Jumlah karya:</b> " + esc(a.works_count) + "</li>" : "") +
    (a.i10_index != null ? "<li><b>i10-index:</b> " + esc(a.i10_index) + "</li>" : "") +
    (topik ? "<li><b>Topik:</b> " + esc(topik) + "</li>" : "") +
    "</ul>" +
    (profUrl ? '<p class="tiny"><a href="' + esc(profUrl) + '" target="_blank" rel="noopener">Lihat profil lengkap ↗</a></p>' : "") +
    "</div>";
}

/* ----- Ide Topik ----- */
function pageTopik() {
  view().innerHTML =
    "<h2>"+tic("bulb")+" Ide Topik</h2>" +
    '<div class="card"><p class="muted">Buntu ide? Kasih keyword, aku kasih tren publikasi + saran topik &amp; research gap.</p>' +
    '<label for="kwTopik">Keyword</label>' +
    '<input type="text" id="kwTopik" placeholder="cth: kecerdasan buatan pendidikan">' +
    pickerHTML("tp") +
    '<button class="btn btn-primary btn-block" id="btnTopik" style="margin-top:12px">' + tic("bulb") + ' Kasih Ide Dong</button></div>' +
    '<div id="hasilTopik"></div>';
  pickerPasang("tp");
  ambilInfoKuota().then(() => pickerSegarkan("tp"));
  const kirimTopik = async (modelKey) => {
    const keyword = ($("#kwTopik").value || "").trim();
    if (!keyword) { toast("Isi dulu keywordnya "); return; }
    const mk = modelKey || modelPilihan();
    const box = $("#hasilTopik");
    box.innerHTML = '<p class="loading">Lagi ngulik tren publikasi...</p>';
    const btn = $("#btnTopik"); setLoading(btn, true, "Lagi ngulik...");
    const res = await apiPOST("/topik", { keyword: keyword, model: mk });
    setLoading(btn, false);
    if (!res.ok) { netErr(box, res); return; }
    const d = res.data || {};
    if (d.kuota_habis || d.error === "kuota_habis") {
      box.innerHTML = '<div class="warn-box">Yah, kuota AI harianmu habis. Tenang, besok reset lagi kok! Sementara pakai fitur non-AI dulu ya.</div>';
      popupJatahHabis();
      return;
    }
    let html = "";
    const tren = d.tren || d.trends;
    if (tren) html += '<div class="card"><h3>Tren publikasi ' + tic("chart") + '</h3>' + trenHtml(tren) + "</div>";
    // d.ide = string ide judul (selalu ada bila sukses); d.saran = array topik
    // alternatif (sering [] kosong yang truthy!) — render ide TERPISAH agar
    // tidak tertutup array kosong.
    if (typeof d.ide === "string" && d.ide)
      html += '<div class="card"><h3>Ide judul dari AI ' + tic("bulb") + '</h3><div>' + mdRapi(d.ide) + "</div>" + labelModelHTML(d) + "</div>";
    const saran = (Array.isArray(d.saran) && d.saran.length) ? d.saran : (d.topik || []);
    if (Array.isArray(saran) && saran.length)
      html += '<div class="card"><h3>Saran topik &amp; research gap ' + tic("bulb") + '</h3><ul class="bersih">' + saran.map(s => "<li>" + esc(typeof s === "string" ? s : (s.judul || s.topik || JSON.stringify(s))) + "</li>").join("") + "</ul></div>";
    if (!html) html = '<div class="warn-box">Belum ada ide yang keluar. Coba keyword lain ya.</div>';
    box.innerHTML = html;
    if (d.ide) ambilInfoKuota().then(() => pickerSegarkan("tp"));
    // R5 (audit 2026-10-13): jangan catat entri zonk
    if (d.ide || saran.length) riwTamuCatat("topik", keyword, { keyword: keyword, ide: d.ide, saran: saran, gaps: d.gaps });
  };
  $("#btnTopik").addEventListener("click", () => kirimTopik(null));
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
    (AUTH.login
      ? '<p class="muted">Tersimpan di browser HP-mu. Total: <b>' + arr.length + "</b> referensi.</p>"
      : '<p class="muted">' + tic("clock") + ' Mode tamu: koleksi tersimpan di browser HP-mu maksimal <b>24 jam</b>, setelah itu otomatis terhapus. Amankan koleksimu: pakai tombol <b>Export</b> di bawah buat simpan jadi file, atau <a href="#/masuk"><b>login gratis</b></a> biar tidak kena batas waktu. Total: <b>' + arr.length + "</b> referensi.</p>") +
    (arr.length ?
      '<div class="card"><h3>Export Daftar Pustaka ' + tic("download") + '</h3><p class="tiny muted">Pilih gaya sitasi & format, filenya langsung keunduh. Urutan entri otomatis sesuai kaidah gaya (A-Z untuk APA/Harvard/MLA/Chicago).</p>' +
      '<label class="muted" for="expGaya">Gaya sitasi</label>' +
      '<select id="expGaya"><option value="apa">APA 7</option><option value="harvard">Harvard</option><option value="mla">MLA 9</option><option value="chicago">Chicago</option><option value="vancouver">Vancouver</option><option value="ieee">IEEE</option></select>' +
      '<div style="margin-top:10px"><button class="btn" data-exp="txt">TXT</button>' +
      '<button class="btn" data-exp="docx">DOCX</button>' +
      '<button class="btn" data-exp="bibtex">BibTeX</button></div></div>' : "") +
    '<div class="card"><h3>Impor Referensi ' + tic("download") + '</h3><p class="tiny muted">Punya daftar referensi di Mendeley, Zotero, atau Google Scholar? Export dari sana sebagai file <b>.bib</b> (BibTeX) atau <b>.ris</b>, lalu unggah di sini. Referensinya langsung masuk koleksi, siap di-export jadi daftar pustaka.</p>' +
    '<input type="file" id="impFile" accept=".bib,.ris,.txt">' +
    '<button class="btn btn-primary btn-block" id="btnImpor" style="margin-top:10px">' + tic("download") + ' Impor ke Koleksi</button>' +
    '<div id="imporHasil"></div></div>' +
    '<div id="listKoleksi">' +
      (arr.length ? arr.map((it, i) =>
        '<div class="card hasil"><h3>' + esc(it.judul || "(tanpa judul)") + "</h3>" +
        '<div class="meta">' + esc(Array.isArray(it.penulis) ? it.penulis.join(", ") : (it.penulis || "-")) +
        " | " + esc(it.tahun || "-") + " | " + esc(it.jurnal || "-") + "</div>" +
        '<div class="aksi-row"><button class="btn btn-danger" data-hapus="' + i + '">Hapus</button></div></div>'
      ).join("") : '<p class="empty">Koleksimu masih kosong. <br>Cari jurnal dulu, terus tekan <b>Simpan ke Koleksi</b> di tiap hasil. <a href="#/cari">Gas cari →</a></p>') +
    "</div>";

  $$("[data-hapus]", view()).forEach(b => b.addEventListener("click", () => {
    if (!confirm("Hapus dari koleksi? Tindakan ini permanen. ")) return;
    koleksiHapus(parseInt(b.dataset.hapus, 10));
    toast("Dihapus dari koleksi. ");
    pageKoleksi();
  }));
  $("#btnImpor").addEventListener("click", async () => {
    const f = $("#impFile").files[0];
    const box = $("#imporHasil");
    if (!f) { toast("Pilih dulu file.bib atau.ris-nya "); return; }
    const btn = $("#btnImpor"); setLoading(btn, true, "Lagi mengimpor...");
    const fd = new FormData();
    fd.append("file", f);
    const res = await unggahForm("/api/koleksi-impor", fd);
    setLoading(btn, false);
    if (!res.ok) { box.innerHTML = '<div class="err-box" style="margin-top:10px"> ' + esc(errFile(res)) + "</div>"; return; }
    const r = koleksiImpor((res.data && res.data.items) || []);
    toast("Impor selesai: " + r.tambah + " referensi masuk koleksi" + (r.duplikat ? ", " + r.duplikat + " duplikat dilewati" : "") + " ");
    pageKoleksi();
  });
  $$("[data-exp]", view()).forEach(b => b.addEventListener("click", async () => {
    const format = b.dataset.exp;
    setLoading(b, true, "Lagi nyiapin file...");
    const ext = format === "docx" ? "docx" : (format === "bibtex" ? "bib" : "txt");
    const gayaEl = $("#expGaya");
    const res = await apiDownload("/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: koleksiGet(), format: format, style: gayaEl ? gayaEl.value : "apa" })
    }, "daftar-pustaka-khairypustaka." + ext);
    setLoading(b, false);
    toast(res.ok ? "File keunduh! Cek folder download ya. " : "Gagal export, coba lagi ya. ");
  }));
}

/* ----- Riwayat ----- */
const RIWAYAT_FILTERS = [
  ["", "Semua"], ["chat", "Chat AI"], ["sitasi", "Sitasi"],
  ["jurnal", "Cari Jurnal"], ["topik", "Ide Topik"]
];
const RIWAYAT_TIC = { chat: "chat", sitasi: "quote", jurnal: "search", topik: "bulb" };
const RIWAYAT_NAMA = { chat: "Chat AI", sitasi: "Sitasi", jurnal: "Cari Jurnal", topik: "Ide Topik" };

/* ----- Riwayat tamu: localStorage saja (tidak ke server).
   Semua pencarian & pemakaian tamu tercatat, tapi hanya disimpan maks
   24 jam lalu otomatis terhapus (keputusan Sul 2026-10-18). ----- */
const RIW_TAMU_KEY = "kp_riwayat_tamu";
const RIW_TAMU_MAKS = 200; // batas aman teknis; batas utama = umur 24 jam
const RIW_TAMU_TTL = 24 * 60 * 60 * 1000;
function riwTamuGet() {
  let a;
  try { a = JSON.parse(localStorage.getItem(RIW_TAMU_KEY) || "[]"); } catch (e) { return []; }
  if (!Array.isArray(a)) return [];
  const kini = Date.now();
  const bersih = a.filter(e => {
    const w = e && e.waktu ? new Date(e.waktu).getTime() : NaN;
    return isFinite(w) && (kini - w) < RIW_TAMU_TTL;
  });
  if (bersih.length !== a.length) {
    try { localStorage.setItem(RIW_TAMU_KEY, JSON.stringify(bersih)); } catch (e) {}
  }
  return bersih;
}
let _riwTamuGagalPernah = false; // I3: toast gagal simpan cukup sekali
function riwTamuSet(a) {
  try { localStorage.setItem(RIW_TAMU_KEY, JSON.stringify(a)); }
  catch (e) {
    // I3 (audit 2026-10-13): jangan gagal diam-diam
    if (!_riwTamuGagalPernah) {
      _riwTamuGagalPernah = true;
      toast("Riwayat tamu gagal disimpan (penyimpanan browser penuh?) ");
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
/* FIX 2026-11-08 (audit putaran 2): definisi kedua riwTamuCatat yang
   polos (tanpa antrean pra-auth + tanpa pemangkasan data S2) DIHAPUS.
   Definisi pertama di atas yang berlaku. */
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
      '<button class="btn btn-accent" data-salin-sitasi="' + esc(e.id) + '">' + tic("clipboard") + ' Salin</button>';
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
        "</div>"; }).join("") : '<p class="empty">Nggak ada hasil tersimpan.</p>');
  }
  if (t === "topik") {
    const saran = Array.isArray(d.saran) ? d.saran : [];
    let h = '<p class="muted">Keyword: <b>' + esc(d.keyword || "") + "</b></p>";
    if (d.ide) h += '<div class="card"><h3>Ide judul dari AI ' + tic("bulb") + '</h3><div>' + mdRapi(d.ide) + "</div></div>";
    if (saran.length) h += '<div class="card"><h3>Saran topik &amp; research gap ' + tic("bulb") + '</h3><ul class="bersih">' +
      saran.map(s => "<li>" + esc(typeof s === "string" ? s : (s.judul || s.topik || JSON.stringify(s))) + "</li>").join("") + "</ul></div>";
    if (!d.ide && !saran.length) h += '<p class="empty">Nggak ada ide tersimpan.</p>';
    return h;
  }
  return "";
}

function pageRiwayat() {
  const isTamu = !AUTH.login; // tamu: data dari localStorage, tanpa request ke server
  let tipe = "", q = "", bukaId = null;
  const namaAktif = () => tipe ? RIWAYAT_NAMA[tipe] : "semua riwayat";

  const bannerTamu = isTamu
    ? '<div class="warn-box" style="margin-bottom:12px">' + tic("clock") + ' <b>Mode tamu:</b> semua pencarian & pemakaianmu tercatat di sini, tapi cuma disimpan maksimal <b>24 jam</b> di HP-mu, habis itu <b>otomatis terhapus</b> dan nggak dikirim ke server.<br><br><b>Amankan riwayatmu dari sekarang:</b> export jadi file pakai tombol di bawah ini, atau <a href="#/masuk"><b>login gratis</b></a> biar tersimpan permanen di akunmu tanpa batas waktu.</div>' +
      '<div style="margin-bottom:12px"><button class="btn btn-primary btn-block" id="btnRiwExport">' + tic("download") + ' Export Riwayat (TXT)</button></div>'
    : "";

  view().innerHTML =
    "<h2>"+tic("clock")+" Riwayat</h2>" +
    bannerTamu +
    '<p class="muted">Semua jejak nugas & skripsianmu — chat AI, sitasi, pencarian jurnal, ide topik. Klik <b>Buka</b> buat lihat lagi.</p>' +
    '<div class="chip-row" id="riwFilter">' +
      RIWAYAT_FILTERS.map(f =>
        '<button class="filter-chip' + (f[0] === tipe ? " active" : "") + '" data-tipe="' + f[0] + '">' + f[1] + "</button>"
      ).join("") +
    "</div>" +
    '<div class="card"><label for="riwQ">Cari riwayat</label>' +
    '<input type="text" id="riwQ" placeholder="cth: stunting, sitasi APA, ..." autocomplete="off"></div>' +
    '<div id="listRiwayat"><p class="loading">Lagi ngambil riwayat...</p></div>' +
    '<div class="center" style="margin:14px 0"><button class="btn btn-danger" id="btnRiwHapusSemua"></button></div>';

  const aturTombolHapus = () => {
    $("#btnRiwHapusSemua").innerHTML = tic("trash") + " Hapus " + namaAktif();
  };
  aturTombolHapus();

  // Export riwayat tamu jadi file TXT (pengingat Sul 2026-10-18: beri jalan
  // mengamankan riwayat sebelum batas 24 jam menghapusnya otomatis).
  const bExpRiw = $("#btnRiwExport");
  if (bExpRiw) bExpRiw.addEventListener("click", () => {
    const arr = riwTamuGet();
    if (!arr.length) { toast("Riwayatmu masih kosong. "); return; }
    let teks = "RIWAYAT KhairyPustaka (mode tamu)\nDiexport: " + new Date().toLocaleString("id-ID") +
      "\nTotal: " + arr.length + " entri\n" + "=".repeat(50) + "\n\n";
    arr.slice().reverse().forEach((e, i) => {
      teks += (i + 1) + ". [" + (RIWAYAT_NAMA[e.tipe] || e.tipe) + "] " + (e.judul || "") + "\n";
      teks += "    Waktu: " + fmtWaktuRiwayat(e.waktu) + "\n";
      const d = e.data || {};
      if (d.sitasi) teks += "    Sitasi: " + d.sitasi + "\n";
      if (d.q) teks += "    Pencarian: " + d.q + "\n";
      if (d.keyword) teks += "    Kata kunci: " + d.keyword + "\n";
      if (Array.isArray(d.hasil)) {
        teks += "    Hasil jurnal:\n";
        d.hasil.slice(0, 12).forEach(w => { if (w && w.judul) teks += "    - " + w.judul + (w.tahun ? " (" + w.tahun + ")" : "") + "\n"; });
      } else if (typeof d.hasil === "string" && d.hasil) {
        teks += "    Hasil: " + d.hasil.slice(0, 1000) + "\n";
      }
      if (d.jawaban) teks += "    Jawaban: " + String(d.jawaban).slice(0, 1000) + "\n";
      teks += "\n";
    });
    const blob = new Blob([teks], { type: "text/plain;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "riwayat-khairypustaka.txt";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    toast("Riwayat keexport! Simpan filenya baik-baik ya. ");
  });

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
        box.innerHTML = '<div class="err-box">Gagal ngambil riwayat, coba refresh ya.</div>';
        return;
      }
      arr = (res.data && res.data.riwayat) || [];
    }
    if (!arr.length) {
      box.innerHTML = '<p class="empty">Masih kosong nih. <br>' +
        (q ? "Coba kata kunci lain ya." : "Pakai fitur-fiturnya dulu — riwayatmu otomatis kecatat di sini. ") + "</p>";
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
      if (!confirm("Hapus riwayat ini? Tindakan ini permanen. ")) return;
      if (isTamu) {
        riwTamuSet(riwTamuGet().filter(e => e.id !== bHapus.dataset.riwHapus));
        toast("Riwayat dihapus. "); if (bukaId) bukaId = null; muat();
      } else {
        const r = await apiJSON("/history/" + encodeURIComponent(bHapus.dataset.riwHapus), { method: "DELETE" });
        if (r.ok) { toast("Riwayat dihapus. "); if (bukaId) bukaId = null; muat(); }
        else toast("Gagal hapus, coba lagi ya. ");
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
        try { await navigator.clipboard.writeText(dd.sitasi); toast("Disalin! "); }
        catch (e) { toast("Gagal nyalin "); }
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
      if (!ent) { toast("Gagal buka riwayat "); return; }
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
    if (!confirm("Hapus " + namaAktif() + "? Tindakan ini permanen. ")) return;
    if (isTamu) {
      riwTamuSet(tipe ? riwTamuGet().filter(e => e.tipe !== tipe) : []);
      toast("Beres, udah dihapus semua. "); bukaId = null; muat();
    } else {
      const r = await apiJSON("/history" + (tipe ? "?type=" + encodeURIComponent(tipe) : ""), { method: "DELETE" });
      if (r.ok) { toast("Beres, udah dihapus semua. "); bukaId = null; muat(); }
      else toast("Gagal hapus, coba lagi ya. ");
    }
  });

  muat();
}

/* ----- Matriks ----- */
function pageMatriks() {
  const arr = koleksiGet();
  view().innerHTML =
    "<h2>"+tic("grid")+" Matriks Literatur</h2>" +
    '<div class="card"><p class="muted">Bikin tabel sintesis (matriks) dari koleksimu — format DOCX, siap tempel ke Bab 2.</p>' +
    "<p>Koleksi saat ini: <b>" + arr.length + "</b> referensi.</p>" +
    (arr.length
      ? '<button class="btn btn-primary btn-block" id="btnMatriks">' + tic("grid") + ' Buat Matriks (DOCX)</button>'
      : '<p class="empty">Koleksimu kosong. <a href="#/cari">Cari &amp; simpan jurnal dulu →</a></p>') +
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
    if (!res.ok && !kuotaTamuToast(res)) toast("Gagal bikin matriks, coba lagi ya. ");
    else if (res.ok) toast("Matriks keunduh! ");
  });
}

/* ----- Util tanggal (dipakai halaman Jadwal) ----- */
function fmtTanggal(iso) {
  if (!iso) return "-";
  try {
    const d = new Date(iso.length <= 10 ? iso + "T00:00:00" : iso);
    return d.toLocaleDateString("id-ID", { day: "numeric", month: "long", year: "numeric" });
  } catch (e) { return iso; }
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
    '<p class="muted">Bingung pilih uji statistik? Jawab aja pertanyaannya, aku tunjukin yang cocok.</p>' +
    '<div id="statBox"></div>';
  statLangkah({ restart: true });
}
function statTanya(d) {
  const qid = d.qid || "";
  const ops = Array.isArray(d.opsi) ? d.opsi : [];
  const keys = Object.keys(statAnswers);
  let html = keys.length ? '<button class="btn btn-ghost" id="statBack">← Kembali</button>' : "";
  // d.teks dibuat backend dan boleh mengandung <b>/<i> -> render sebagai HTML
  html += '<div class="card"><h3>' + (d.teks || "Pilih salah satu:") + "</h3>" +
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
    '<div class="ok-box"><h3 style="margin-top:0">Hasil ' + tic("target") + '</h3>' +
    "<p><b>" + esc(r.nama || "Uji statistik") + "</b></p>" +
    (r.kapan ? "<p>" + esc(r.kapan) + "</p>" : "") +
    (syarat.length ? "<p><b>Syarat:</b></p><ul>" + syarat.map(s => "<li>" + esc(s) + "</li>").join("") + "</ul>" : "") +
    (r.baca ? "<p><b>Cara baca hasil:</b> " + esc(r.baca) + "</p>" : "") +
    (r.contoh ? '<p class="muted tiny"><b>Contoh:</b> ' + esc(r.contoh) + "</p>" : "") +
    "</div>" +
    '<button class="btn btn-primary btn-block" id="statUlang">' + tic("refresh") + ' Mulai dari awal</button>' +
    '<button class="btn btn-block" id="statAi" style="margin-top:8px">' + tic("spark") + ' Bantu tuliskan interpretasinya (AI)</button>' +
    '<div id="statAiOut" style="margin-top:10px"></div>';
  $("#statUlang").addEventListener("click", () => pageStatistik());
  const btnAi = $("#statAi");
  if (btnAi) btnAi.addEventListener("click", async () => {
    const out = $("#statAiOut");
    out.innerHTML = '<p class="loading">AI lagi nulis...</p>';
    setLoading(btnAi, true, "AI lagi nulis...");
    const teks = [r.nama, r.kapan, r.baca, r.contoh].filter(Boolean).join("\n");
    const res = await apiPOST("/ai", { kind: "interpretasi", text: teks });
    setLoading(btnAi, false);
    const d = res.data || {};
    if (!res.ok && (d.error === "kuota_habis" || d.kuota_habis)) { out.innerHTML = kuotaHabisBox(); popupJatahHabis(); return; }
    if (!res.ok) { out.innerHTML = ""; netErr(out, res); return; }
    const hasil = d.hasil || d.result || d.text;
    out.innerHTML = hasil ? '<div class="hasil-ai">' + mdRapi(hasil) + "</div>" : '<div class="warn-box">AI-nya bengong. Coba lagi ya.</div>';
  });
}
async function statLangkah(body) {
  const box = $("#statBox");
  if (!box) return;
  box.innerHTML = '<p class="loading">Lagi mikir...</p>';
  const res = await apiPOST("/statistik", body || {});
  if (!res.ok) { netErr(box, res); return; }
  const d = res.data || {};
  if (d.tipe === "hasil") statHasil(d);
  else if (d.tipe === "pertanyaan") statTanya(d);
  else box.innerHTML = '<div class="warn-box">Responsnya nggak kebaca. Coba ulangi ya.</div>';
}

/* ----- Template ----- */
function pageTemplate() {
  view().innerHTML =
    "<h2>"+tic("pencil")+" Template Skripsi</h2>" +
    '<div class="card"><p class="muted">Template proposal &amp; skripsi format DOCX, tinggal isi. Pilih jenis penelitianmu:</p>' +
    '<button class="btn btn-primary btn-block" data-tmpl="kuantitatif">' + tic("chart") + ' Template Kuantitatif</button>' +
    '<button class="btn btn-primary btn-block" data-tmpl="kualitatif">' + tic("books") + ' Template Kualitatif</button></div>';
  $$("[data-tmpl]", view()).forEach(b => b.addEventListener("click", async () => {
    const jenis = b.dataset.tmpl;
    setLoading(b, true, "Lagi nyiapin template...");
    const res = await apiDownload("/template?jenis=" + encodeURIComponent(jenis), {}, "template-" + jenis + ".docx");
    setLoading(b, false);
    toast(res.ok ? "Template keunduh! Gas nulis! " : "Gagal unduh template, coba lagi ya. ");
  }));
}

/* ----- Parafrase ----- */
/* ----- Terjemah Abstrak (halaman mandiri + unggah PDF jurnal) ----- */
function pageTerjemah() {
  view().innerHTML =
    "<h2>"+tic("globe")+" Terjemah Abstrak</h2>" +
    '<div class="card"><p class="muted">Terjemahkan abstrak jurnal berbahasa Inggris ke Bahasa Indonesia. Tempel teksnya, atau unggah PDF jurnalnya: teks awal dokumen diambil otomatis, lalu kamu pangkas sampai tersisa abstraknya saja.</p>' +
    '<label for="tjTeks">Abstrak bahasa Inggris</label><textarea id="tjTeks" rows="8" placeholder="Tempel abstrak berbahasa Inggris di sini..."></textarea>' +
    '<p class="muted tiny"><span id="tjHitung">0</span>/3.000 karakter</p>' +
    '<label for="tjFile" style="margin-top:8px">Atau unggah file jurnal (.pdf/.docx/.txt)</label>' +
    '<input type="file" id="tjFile" accept="' + ACCEPT_DOK + '">' +
    '<button class="btn btn-primary btn-block" id="btnTj" style="margin-top:12px">' + tic("globe") + ' Terjemahkan</button></div>' +
    '<div id="hasilTj"></div>';
  const ta = $("#tjTeks");
  const hitung = () => { const el = $("#tjHitung"); if (el) el.textContent = ta.value.length; };
  ta.addEventListener("input", hitung);
  $("#tjFile").addEventListener("change", async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    toast("Lagi membaca file... ");
    const res = await ekstrakTeksFile(f);
    if (!res.ok) { toast(" " + errFile(res)); return; }
    let teks = (res.data.teks || "").replace(/\s+/g, " ").trim();
    const m = teks.match(/\babstract\b/i);
    if (m && m.index < 2500) teks = teks.slice(m.index + m[0].length).replace(/^[:\s]+/, "");
    ta.value = teks.slice(0, 3000);
    hitung();
    toast("Teks dari file sudah dimasukkan. Pangkas sampai tersisa abstraknya saja, lalu klik Terjemahkan ");
  });
  $("#btnTj").addEventListener("click", async () => {
    const teks = (ta.value || "").trim();
    if (teks.length < 20) { toast("Teksnya terlalu pendek buat diterjemahkan "); return; }
    if (teks.length > 3000) { toast("Kepanjangan, pangkas ke 3.000 karakter ya "); return; }
    const box = $("#hasilTj");
    box.innerHTML = '<p class="loading">Lagi menerjemahkan...</p>';
    const btn = $("#btnTj"); setLoading(btn, true, "Menerjemahkan...");
    const res = await apiPOST("/terjemah", { teks: teks });
    setLoading(btn, false);
    const d = res.data || {};
    if (!res.ok) { if (!kuotaTamuBox(box, res)) netErr(box, res); return; }
    if (d.sumber === "sudah_indonesia") { box.innerHTML = '<div class="warn-box">Teksnya sudah berbahasa Indonesia, tidak perlu diterjemahkan</div>'; return; }
    if (!d.hasil) { box.innerHTML = '<div class="warn-box">Hasil terjemahannya kosong, coba lagi ya</div>'; return; }
    box.innerHTML = '<div class="card"><h3>Hasil terjemahan ' + tic("spark") + '</h3><p style="white-space:pre-wrap">' + esc(d.hasil) + '</p><button class="btn" id="tjCopy">' + tic("clipboard") + ' Salin</button></div>';
    $("#tjCopy").addEventListener("click", () => copyText(d.hasil));
    toast(d.sumber === "mymemory" ? "Diterjemahkan (terjemahan mesin)" : "Diterjemahkan ");
  });
}

function pecahTeksParafrase(teks, maks) {
  /* Pecah teks panjang jadi potongan <= maks karakter di batas kalimat,
     supaya bisa diproses /api/parafrase (maks 800) berurutan. */
  maks = maks || 750;
  const kalimat = (teks.replace(/\s+/g, " ").match(/[^.!?…]+[.!?…]+["')\]]*\s*|[^.!?…]+$/g) || [teks])
    .map(s => s.trim()).filter(Boolean);
  const potongan = [];
  let cur = "";
  const dorong = (s) => {
    while (s.length > maks) {
      let cut = s.lastIndexOf(" ", maks);
      if (cut < 100) cut = maks;
      potongan.push(s.slice(0, cut).trim());
      s = s.slice(cut).trim();
    }
    if (s) potongan.push(s);
  };
  for (const k of kalimat) {
    if (cur && (cur + " " + k).length > maks) { dorong(cur); cur = k; }
    else cur = cur ? cur + " " + k : k;
  }
  if (cur) dorong(cur);
  return potongan.filter(Boolean);
}

function pageParafrase() {
  view().innerHTML =
    "<h2>"+tic("refresh")+" Parafrase</h2>" +
    '<div class="card"><p class="muted">Tempel kalimat yang mau diubah, biar nggak kepentok plagiarisme.</p>' +
    '<label for="pfTeks">Teks asli</label><textarea id="pfTeks" placeholder="Tempel kalimat/paragraf di sini..."></textarea>' +
    '<label for="pfFile" style="margin-top:10px">Atau unggah file satu bab (.docx/.pdf/.txt)</label>' +
    '<input type="file" id="pfFile" accept="' + ACCEPT_DOK + '">' +
    '<p class="muted tiny">Teks panjang (dari tempel atau file) otomatis diproses per potongan ±750 karakter secara berurutan. Tiap potongan dihitung 1x pemakaian parafrase.</p>' +
    '<button class="btn btn-primary btn-block" id="btnPf" style="margin-top:12px">' + tic("refresh") + ' Parafrase-kan!</button></div>' +
    '<div id="hasilPf"></div>';
  $("#pfFile").addEventListener("change", async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    toast("Lagi membaca file... ");
    const res = await ekstrakTeksFile(f);
    if (!res.ok) { toast(" " + errFile(res)); return; }
    $("#pfTeks").value = (res.data.teks || "").trim();
    toast("Teks dari file sudah dimasukkan (" + (res.data.karakter || 0) + " karakter). Klik Parafrase-kan buat memproses ");
  });
  $("#btnPf").addEventListener("click", async () => {
    const teks = ($("#pfTeks").value || "").trim();
    if (!teks) { toast("Tempel dulu teksnya, atau unggah file-nya "); return; }
    const box = $("#hasilPf");
    const btn = $("#btnPf");
    const potongan = pecahTeksParafrase(teks, 750);
    const hasilBagian = [];
    let gagalDi = 0;
    setLoading(btn, true, "Lagi muter kata...");
    for (let i = 0; i < potongan.length; i++) {
      box.innerHTML = '<p class="loading">Lagi muter-muter kata... ' +
        (potongan.length > 1 ? " (potongan " + (i + 1) + " dari " + potongan.length + ")" : "") + "</p>";
      const res = await apiPOST("/parafrase", { teks: potongan[i] });
      if (!res.ok) {
        if (kuotaTamuBox(box, res)) {
          if (hasilBagian.length) box.innerHTML += '<div class="card"><h3>Hasil parafrase sebagian ' + tic("spark") + '</h3><p style="white-space:pre-wrap">' + esc(hasilBagian.join("\n\n")) + "</p></div>";
          gagalDi = i + 1;
          break;
        }
        box.innerHTML = '<div class="warn-box">Potongan ke-' + (i + 1) + " gagal diproses. " + (hasilBagian.length ? "Bagian sebelumnya tetap ditampilkan di bawah." : "Coba lagi ya.") + "</div>" +
          (hasilBagian.length ? '<div class="card"><h3>Hasil parafrase sebagian ' + tic("spark") + '</h3><p style="white-space:pre-wrap">' + esc(hasilBagian.join("\n\n")) + "</p></div>" : "");
        gagalDi = i + 1;
        break;
      }
      const d = res.data || {};
      const h = d.hasil || d.parafrase || d.text;
      if (!h) {
        box.innerHTML = '<div class="warn-box">Hasil potongan ke-' + (i + 1) + ' kosong. ' + (hasilBagian.length ? " Bagian sebelumnya tetap ditampilkan di bawah." : "") + "</div>" +
          (hasilBagian.length ? '<div class="card"><h3>Hasil parafrase sebagian ' + tic("spark") + '</h3><p style="white-space:pre-wrap">' + esc(hasilBagian.join("\n\n")) + "</p></div>" : "");
        gagalDi = i + 1;
        break;
      }
      hasilBagian.push(h);
    }
    setLoading(btn, false);
    if (!hasilBagian.length) { if (!gagalDi) box.innerHTML = '<div class="warn-box">Hasilnya kosong. Coba teks yang lebih panjang ya.</div>'; return; }
    const gabung = hasilBagian.join("\n\n");
    if (!gagalDi) {
      box.innerHTML = '<div class="card"><h3>Hasil parafrase ' + tic("spark") + '</h3>' +
        (potongan.length > 1 ? '<p class="muted tiny">Diproses dalam ' + potongan.length + " potongan.</p>" : "") +
        '<p style="white-space:pre-wrap">' + esc(gabung) + '</p><button class="btn" id="pfCopy">' + tic("clipboard") + ' Salin</button></div>';
      // I4 (audit 2026-10-13): parafrase dicatat sebagai riwayat chat
      riwTamuCatat("chat", teks, { sumber: "parafrase", text: teks, hasil: gabung });
      $("#pfCopy").addEventListener("click", () => copyText(gabung));
    }
  });
}

/* ----- Ekstrak Teks (alat mandiri, memakai /api/ekstrak-teks) ----- */
function pageEkstrakTeks() {
  view().innerHTML =
    "<h2>" + tic("doc") + " Ekstrak Teks</h2>" +
    '<div class="card">' +
    "<p style=\"margin-top:0\">Ambil isi teks dari file <b>PDF, Word (.docx), atau TXT</b>, buat ditempel ke naskah, dicek kemiripannya, atau diolah lagi di alat lain. File dibaca seketika dan <b>tidak disimpan</b>.</p>" +
    '<label for="etFile">Pilih file (.pdf, .docx, .txt, maks 2 MB)</label>' +
    '<input type="file" id="etFile" accept="' + ACCEPT_DOK + '">' +
    '<button class="btn btn-primary btn-block" id="btnEt" style="margin-top:12px">' + tic("doc") + ' Ambil Teks dari File</button>' +
    '<p class="muted" style="margin-top:8px">PDF hasil pindai (scan gambar) tidak punya lapisan teks, jadi hasilnya bisa kosong. Teks ditampilkan maksimal 150.000 karakter pertama.</p></div>' +
    '<div id="hasilEt"></div>';
  let namaFile = "";
  $("#btnEt").addEventListener("click", async () => {
    const f = $("#etFile").files[0];
    if (!f) { toast("Pilih file-nya dulu ya"); return; }
    namaFile = f.name || "dokumen";
    const box = $("#hasilEt");
    box.innerHTML = '<p class="loading">Lagi membaca file...</p>';
    const btn = $("#btnEt"); setLoading(btn, true, "Lagi membaca...");
    const res = await ekstrakTeksFile(f);
    setLoading(btn, false);
    if (!res.ok) { box.innerHTML = ""; toast(" " + errFile(res)); return; }
    const d = res.data || {};
    const teks = d.teks || "";
    let info = "Dari file <b>" + esc(d.nama || namaFile) + "</b>: " +
      (d.karakter || teks.length).toLocaleString("id-ID") + " karakter, " +
      (d.kata || 0).toLocaleString("id-ID") + " kata.";
    if (d.terpotong) info += " Teksnya panjang banget, jadi yang ditampilkan 150.000 karakter pertama.";
    box.innerHTML =
      '<div class="card"><p class="muted" style="margin-top:0">' + info + "</p>" +
      '<label for="etTeks">Hasil teksnya</label>' +
      '<textarea id="etTeks" rows="14"></textarea>' +
      '<div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">' +
      '<button class="btn btn-primary" id="btnEtSalin">' + tic("quote") + ' Salin Teks</button>' +
      '<button class="btn" id="btnEtUnduh">' + tic("doc") + ' Unduh .txt</button>' +
      "</div></div>";
    $("#etTeks").value = teks;
    $("#btnEtSalin").addEventListener("click", async () => {
      const t = $("#etTeks").value || "";
      if (!t) { toast("Teksnya masih kosong"); return; }
      try { await navigator.clipboard.writeText(t); toast("Teks tersalin, tinggal tempel di mana aja"); }
      catch (e) { $("#etTeks").select(); toast("Blok teksnya lalu salin manual ya"); }
    });
    $("#btnEtUnduh").addEventListener("click", () => {
      const t = $("#etTeks").value || "";
      if (!t) { toast("Teksnya masih kosong"); return; }
      const dasar = (namaFile || "dokumen").replace(/\.(pdf|docx|txt)$/i, "");
      const blob = new Blob([t], { type: "text/plain;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob); a.download = dasar + ".txt"; a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    });
    box.scrollIntoView({ behavior: "smooth", block: "start" });
  });
}

/* ----- Cek Plagiarisme (kemiripan vs sumber, Jalur A) ----- */
function pageKemiripan() {
  view().innerHTML =
    "<h2>" + tic("check") + " Cek Plagiarisme</h2>" +
    '<div class="warn-box" style="border:2px solid #f0a832;background:rgba(232,147,12,.12);font-size:1rem;line-height:1.65">' +
    "<b>" + tic("warn") + " Baca Dulu Sebelum Pakai, Penting Banget</b><br>" +
    "Alat ini membandingkan naskahmu <b>hanya dengan teks sumber yang kamu tempel di halaman ini</b>. Alat ini tidak memindai internet, tidak terhubung ke database Turnitin mana pun, dan tidak bisa melihat skripsi atau tugas mahasiswa lain.<br><br>" +
    "Kalau skormu rendah di sini, artinya naskahmu sudah cukup berbeda dari sumber yang kamu tempel itu. <b>Skor ini bukan jaminan lolos cek Turnitin di kampusmu</b>, karena hasil Turnitin juga dipengaruhi sumber lain yang tidak kamu tempel di sini.<br><br>" +
    "Pakai alat ini buat memastikan hasil parafrasemu sudah aman dari sumber aslinya, dan jangan pernah lupa mencantumkan sitasi. Parafrase yang baik plus sitasi yang benar adalah cara paling aman menghindari plagiarisme.</div>" +
    '<div class="card">' +
    '<label for="kmNaskah">Naskahmu</label>' +
    '<textarea id="kmNaskah" rows="7" placeholder="Tempel potongan naskahmu di sini (maks 10.000 karakter)..."></textarea>' +
    '<label for="kmSumber" style="margin-top:10px">Teks sumber yang kamu pakai</label>' +
    '<textarea id="kmSumber" rows="7" placeholder="Tempel teks asli dari jurnal/buku yang kamu parafrase di sini. Boleh gabung beberapa sumber sekaligus (maks 30.000 karakter)..."></textarea>' +
    '<p class="muted" style="margin-top:12px"> Atau unggah file (boleh dicampur: satu sisi tempel teks, sisi lain file). File naskah maks 50.000 karakter, file sumber maks 150.000 karakter, ukuran file maks 2 MB.</p>' +
    '<label for="kmFileNaskah">File naskah (.docx/.txt/.pdf)</label>' +
    '<input type="file" id="kmFileNaskah" accept=".docx,.txt,.pdf,text/plain,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document">' +
    '<label for="kmFileSumber" style="margin-top:8px">File sumber (.docx/.txt/.pdf)</label>' +
    '<input type="file" id="kmFileSumber" accept=".docx,.txt,.pdf,text/plain,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document">' +
    '<button class="btn btn-primary btn-block" id="btnKm" style="margin-top:12px">' + tic("search") + ' Cek Kemiripan</button>' +
    '<p class="muted" style="margin-top:8px">Teksmu diproses seketika dan tidak disimpan. Tips parafrase: <a href="/panduan/cara-parafrase-yang-benar">panduan parafrase yang benar</a>.</p></div>' +
    '<div id="hasilKm"></div>';
  $("#btnKm").addEventListener("click", async () => {
    const naskah = ($("#kmNaskah").value || "").trim();
    const sumber = ($("#kmSumber").value || "").trim();
    const fNaskah = $("#kmFileNaskah").files[0];
    const fSumber = $("#kmFileSumber").files[0];
    if ((!naskah && !fNaskah) || (!sumber && !fSumber)) { toast("Isi naskah dan teks sumbernya dulu ya, tempel teks atau unggah file "); return; }
    const box = $("#hasilKm");
    box.innerHTML = '<p class="loading">Lagi membandingkan kata per kata...</p>';
    const btn = $("#btnKm"); setLoading(btn, true, "Lagi mengecek...");
    let res;
    if (fNaskah || fSumber) {
      const fd = new FormData();
      fd.append("naskah", naskah);
      fd.append("sumber", sumber);
      if (fNaskah) fd.append("file_naskah", fNaskah);
      if (fSumber) fd.append("file_sumber", fSumber);
      try {
        const r = await fetch("/api/kemiripan-file", { method: "POST", body: fd });
        res = { ok: r.ok, status: r.status, data: saringErrData(await r.json().catch(() => null)) };
      } catch (e) {
        res = { ok: false, status: 0, data: null };
      }
    } else {
      res = await apiPOST("/kemiripan", { naskah: naskah, sumber: sumber });
    }
    setLoading(btn, false);
    if (!res.ok) { if (kuotaTamuBox(box, res)) return; netErr(box, res); return; }
    const h = (res.data || {}).hasil || {};
    const skor = (h.skor != null ? h.skor : 0);
    const skorTampil = String(skor).replace(".", ",");
    let warna = "#4ade80";
    if (skor >= 40) warna = "#ff6b6f"; else if (skor >= 25) warna = "#f0a832";
    let kalimatHTML = (h.kalimat || []).map(k => {
      if (k.level === "tinggi") return '<span style="background:rgba(229,72,77,.35);border-radius:4px;padding:1px 2px">' + esc(k.teks) + "</span> <b style='color:#ff8589'>(" + k.skor + "%)</b>";
      if (k.level === "sedang") return '<span style="background:rgba(240,168,50,.30);border-radius:4px;padding:1px 2px">' + esc(k.teks) + "</span> <b style='color:#f5c26b'>(" + k.skor + "%)</b>";
      return esc(k.teks);
    }).join(" ");
    let catatan;
    if (skor < 25) {
      catatan = "Hasilmu <b>" + skorTampil + "%</b> terhadap sumber yang kamu tempel. Angka ini tidak menjamin hasil yang sama di Turnitin, karena Turnitin membandingkan naskahmu dengan jauh lebih banyak sumber, termasuk arsip tugas mahasiswa dari berbagai kampus. Kalau kampusmu memakai Turnitin sebagai standar, tetap lakukan pengecekan resmi lewat kampusmu sebelum mengumpulkan. Alat ini membantumu memastikan parafrasemu aman dari sumber yang kamu pakai, bukan menggantikan pengecekan resmi.";
    } else {
      catatan = "Hasilmu <b>" + skorTampil + "%</b> terhadap sumber yang kamu tempel, dan itu masih tergolong tinggi. Perbaiki dulu kalimat yang disorot di atas dengan parafrase ulang: tutup sumbernya, tulis kembali dari pemahamanmu, lalu cek lagi di sini sampai turun. Setelah itu pun tetap verifikasi lewat jalur resmi kampusmu bila tersedia, karena standar akhir yang menentukan adalah standar kampusmu.";
    }
    box.innerHTML =
      '<div class="card"><p class="muted" style="margin:0">Skor kemiripan terhadap sumber yang kamu tempel:</p>' +
      '<p style="font-size:2.4rem;font-weight:800;margin:2px 0;color:' + warna + '">' + skorTampil + '%</p>' +
      '<p class="muted" style="margin:0 0 10px"><b>Ini bukan skor Turnitin.</b> Dari ' + (h.total_kata || 0) + ' kata naskahmu, ' + (h.kata_mirip || 0) + ' kata berada dalam rangkaian yang sama persis dengan sumber.</p>' +
      '<p style="line-height:1.8">' + kalimatHTML + "</p>" +
      '<p class="muted">Kalimat berwarna <span style="background:rgba(229,72,77,.35);padding:0 6px;border-radius:4px">merah</span> kemiripannya tinggi, <span style="background:rgba(240,168,50,.30);padding:0 6px;border-radius:4px">kuning</span> sedang. Angka di belakang kalimat adalah persen kemiripan kalimat itu dengan sumber.</p></div>' +
      '<div class="warn-box" style="border:2px solid #f0a832;background:rgba(232,147,12,.12);line-height:1.65">' + catatan + "</div>";
    box.scrollIntoView({ behavior: "smooth", block: "start" });
  });
}

/* ----- Convert File (halaman mandiri, hybrid lokal + iLoveAPI) ----- */
const CONVERT_ALAT = [
  {kode:"word_pdf", nama:"Word ke PDF",
   desk:"Dokumen Word (.doc, .docx) jadi PDF yang rapi. Dikerjakan server iLovePDF.",
   accept:".doc,.docx,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document", api:true},
  {kode:"pdf_word", nama:"PDF ke Word",
   desk:"PDF jadi dokumen Word (.docx) yang bisa diedit. Hasilnya teks rapi siap disunting, tata letak visual bisa berbeda dari PDF aslinya.",
   accept:".pdf,application/pdf", api:true},
  {kode:"gabung", nama:"Gabung PDF",
   desk:"Satukan 2 sampai 5 file PDF jadi satu file. Dikerjakan di server kami.",
   accept:".pdf,application/pdf", multi:true, label:"Pilih 2 sampai 5 file PDF"},
  {kode:"pisah", nama:"Pisah PDF",
   desk:"Ambil halaman tertentu dari PDF. Tulis rentangnya, contoh 1-3,5 artinya halaman 1 sampai 3 plus halaman 5.",
   accept:".pdf,application/pdf", rentang:true},
  {kode:"kompres", nama:"Kompres PDF",
   desk:"Perkecil ukuran PDF biar lolos batas unggah kampus. Dikerjakan di server kami.",
   accept:".pdf,application/pdf"},
  {kode:"gambar_pdf", nama:"Gambar ke PDF",
   desk:"Satukan sampai 10 gambar (JPG, PNG) jadi satu file PDF. Dikerjakan di server kami.",
   accept:"image/jpeg,image/png,image/webp,.jpg,.jpeg,.png,.webp", multi:true, label:"Pilih sampai 10 gambar"},
  {kode:"pdf_gambar", nama:"PDF ke Gambar",
   desk:"Tiap halaman PDF jadi gambar JPG (maksimal 30 halaman). Kalau halamannya banyak, hasilnya dikemas ZIP.",
   accept:".pdf,application/pdf"}
];

let _cvTimer = null;

function cvWaktu(detik) {
  detik = Math.max(0, Math.round(detik || 0));
  if (detik < 60) return "sekitar " + detik + " detik";
  const mnt = Math.round(detik / 60);
  return "sekitar " + mnt + " menit";
}

function cvUkuran(byte) {
  byte = byte || 0;
  if (byte < 1024) return byte + " B";
  if (byte < 1024 * 1024) return (byte / 1024).toFixed(1).replace(".", ",") + " KB";
  return (byte / (1024 * 1024)).toFixed(1).replace(".", ",") + " MB";
}

function cvStatusHTML(d) {
  const pill = {antre:"Mengantre", memproses:"Diproses", selesai:"Selesai",
                gagal:"Gagal", dibatalkan:"Dibatalkan"}[d.status] || d.status;
  let h = '<div class="card"><b>' + esc(d.nama_alat) + '</b> ' +
    '<span class="chip">' + esc(pill) + "</span>" +
    '<p class="muted tiny">' + esc(d.nama_file) + "</p>" +
    "<p>" + esc(d.tahap) + "</p>";
  if (d.status === "antre" || d.status === "memproses") {
    h += '<p class="muted tiny">Sudah ' + d.detik_berjalan + " detik sejak dibuat</p>";
  }
  if (d.status === "antre") {
    h += "<p>Posisi antrean: <b>ke " + d.posisi + "</b> (" + d.di_depan +
      " convert di depanmu)</p>";
    if (d.estimasi_mulai_detik > 0) {
      h += '<p class="muted tiny">Perkiraan kasar mulai diproses: ' +
        cvWaktu(d.estimasi_mulai_detik) + "</p>";
    }
    h += '<button class="btn" data-cv="batal">Batalkan</button>';
  }
  if (d.status === "selesai") {
    if (d.hasil_hilang) {
      h += '<div class="warn-box">Hasilnya sudah dibersihkan otomatis (lebih dari 30 menit). Convert ulang ya.</div>';
    } else {
      h += "<p>Hasil: <b>" + esc(d.hasil_nama) + "</b> (" +
        cvUkuran(d.hasil_ukuran) + ")</p>" +
        '<a class="btn btn-primary" href="/api/convert/unduh?id=' +
        encodeURIComponent(d.id) + '">Unduh Hasil</a>' +
        '<p class="muted tiny" style="margin-top:8px">Hasil otomatis dibersihkan setelah 30 menit.</p>';
    }
  }
  if (d.status === "gagal") {
    h += '<div class="err-box">' + esc(d.error || "Convertnya gagal, coba lagi ya.") + "</div>";
  }
  if (d.status === "dibatalkan") {
    h += '<p class="muted">Convertnya dibatalkan. Mulai lagi kapan pun kamu siap.</p>';
  }
  return h + "</div>";
}

function cvMulaiPoll(id) {
  // FIX 2026-11-08 (audit putaran 2): hitung gagal beruntun + batas
  // total; pola lama menulis pesan galat lalu interval jalan TERUS
  // selamanya. Sekarang polling berhenti dengan pesan jujur bahwa
  // pekerjaan mungkin hilang dan pengguna bisa mulai lagi.
  let gagalBeruntun = 0, putaran = 0;
  const berhenti = (pesan) => {
    if (_cvTimer) { clearInterval(_cvTimer); _cvTimer = null; }
    const box = $("#cvStatus");
    if (box) box.innerHTML = '<div class="err-box">' + pesan + "</div>";
  };
  const cek = async () => {
    const box = $("#cvStatus");
    if (!box) return;
    putaran++;
    if (putaran > 300) {
      berhenti("Convertnya kelamaan. Pekerjaannya mungkin hilang, mulai lagi ya.");
      return;
    }
    let r = null, d = null;
    try {
      r = await fetch("/api/convert/status?id=" + encodeURIComponent(id));
      d = await r.json();
    } catch (e) { d = null; }
    if (!r || !r.ok || !d || !d.id) {
      gagalBeruntun++;
      if (gagalBeruntun >= 3) {
        berhenti("Tidak bisa mengecek status convert dan pekerjaannya sepertinya hilang. Mulai lagi ya.");
      }
      return;
    }
    gagalBeruntun = 0;
    box.innerHTML = cvStatusHTML(d);
    const b = box.querySelector('[data-cv="batal"]');
    if (b) b.addEventListener("click", () => cvBatalkan(d.id));
    if (d.status === "selesai" || d.status === "gagal" || d.status === "dibatalkan") {
      if (_cvTimer) { clearInterval(_cvTimer); _cvTimer = null; }
    }
  };
  cek();
  _cvTimer = regTimer(setInterval(cek, 3000));
}

async function cvBatalkan(id) {
  const box = $("#cvStatus");
  const res = await apiPOST("/convert/batal", { id: id });
  if (!res.ok) { toast(" " + ((res.data && (res.data.pesan || res.data.error)) || "Tidak bisa dibatalkan")); return; }
  toast("Convertnya dibatalkan ");
  if (box) box.innerHTML = '<p class="muted">Convertnya dibatalkan. Mulai lagi kapan pun kamu siap.</p>';
  if (_cvTimer) { clearInterval(_cvTimer); _cvTimer = null; }
}

async function cvKirim(alat, inputFile, inputRentang, btn) {
  const files = Array.from((inputFile && inputFile.files) || []);
  if (!files.length) { toast("Pilih filenya dulu ya "); return; }
  const box = $("#cvStatus");
  box.innerHTML = '<p class="loading">Masuk antrean...</p>';
  const fd = new FormData();
  fd.append("alat", alat);
  files.forEach(f => fd.append("file", f));
  if (inputRentang) fd.append("rentang", inputRentang.value || "");
  setLoading(btn, true, "Mengirim...");
  const res = await unggahForm("/api/convert/buat", fd);
  setLoading(btn, false);
  const d = res.data || {};
  if (!res.ok) {
    if (d.error === "provider_belum_aktif") {
      box.innerHTML = '<div class="warn-box">' + esc(d.pesan || "Alat ini lagi disiapkan.") + "</div>";
      return;
    }
    if (!kuotaTamuBox(box, res)) netErr(box, res);
    return;
  }
  cvMulaiPoll(d.id);
}

/* Menu pilihan convert: semua 7 alat terlihat dulu sebagai kartu,
   tiap kartu membuka formulirnya sendiri. */
function cvTampilMenu() {
  if (_cvTimer) { clearInterval(_cvTimer); _cvTimer = null; }
  const kartu = CONVERT_ALAT.map(a =>
    '<button class="feat" data-cv-alat="' + a.kode + '" style="text-align:left;width:100%">' +
    '<span class="ico">' + tic("doc") + "</span><b>" + esc(a.nama) + "</b><small>" + esc(a.desk) + "</small></button>"
  ).join("");
  view().innerHTML =
    "<h2>" + tic("doc") + " Convert File</h2>" +
    '<div class="card"><p class="muted">Pilih dulu mau convert apa. ' +
    "Word ke PDF dikerjakan server iLovePDF, alat lainnya dikerjakan langsung di server kami. " +
    "Semua convert masuk antrean yang sama (maksimal 2 jalan bareng, sisanya menunggu giliran) dan statusnya kelihatan jelas. " +
    "File tidak disimpan permanen, hasil otomatis dibersihkan setelah 30 menit.</p></div>" +
    '<div class="grid">' + kartu + "</div>";
  view().querySelectorAll("[data-cv-alat]").forEach(b =>
    b.addEventListener("click", () => cvBukaAlat(b.getAttribute("data-cv-alat"))));
}

/* Formulir satu alat convert. Navigasi kembali memakai tombol Kembali
   global situs (satu pola di semua halaman). */
function cvBukaAlat(kode) {
  const a = CONVERT_ALAT.find(x => x.kode === kode);
  if (!a) { cvTampilMenu(); return; }
  if (_cvTimer) { clearInterval(_cvTimer); _cvTimer = null; }
  // Tanpa tombol kembali internal: tombol Kembali global situs sudah
  // tampil di semua halaman selain Beranda (temuan Sul 2026-10-19:
  // dua tombol Kembali menumpuk di formulir convert itu dobel).
  let h = "<h2>" + tic("doc") + " " + esc(a.nama) + "</h2>" +
    '<div id="cvStatus"></div>' +
    '<div class="card">' +
    '<p class="muted">' + esc(a.desk) + "</p>" +
    '<label for="cvF">' + esc(a.label || "Pilih file") + ' <span class="tiny muted">(maks 10 MB)</span></label>' +
    '<input type="file" id="cvF" accept="' + a.accept + '"' + (a.multi ? " multiple" : "") + ">";
  if (a.rentang) {
    h += '<label for="cvR" style="margin-top:8px">Rentang halaman</label>' +
      '<input type="text" id="cvR" placeholder="misal: 1-3,5">';
  }
  h += '<button class="btn btn-primary btn-block" id="cvB" style="margin-top:12px">Proses</button>' +
    '<p class="tiny muted" id="cvCatatan" style="margin-top:6px"></p></div>';
  view().innerHTML = h;
  const btn = $("#cvB");
  btn.addEventListener("click", () =>
    cvKirim(a.kode, $("#cvF"), a.rentang ? $("#cvR") : null, btn));
  // Tandai alat API bila kuncinya belum dipasang.
  if (a.api) {
    apiJSON("/convert/siap").then(res => {
      const d = (res && res.data) || {};
      const note = $("#cvCatatan");
      if (res.ok && !d.ilovepdf_aktif) {
        if (note) note.textContent = "Alat ini lagi disiapkan, kuncinya belum dipasang. Coba lagi nanti ya.";
        btn.disabled = true;
      }
    }).catch(() => {});
  }
}

function pageConvert() {
  // Dari tab Convert di Beranda: tile tiap alat membawa ?alat=<kode>
  // supaya formulirnya langsung terbuka tanpa klik tambahan (perintah
  // Sul 2026-10-19). Tanpa parameter, tampilkan menu 7 pilihan.
  const alat = new URLSearchParams(
    (location.hash.split("?")[1] || "")).get("alat") || "";
  if (alat && CONVERT_ALAT.some(a => a.kode === alat)) cvBukaAlat(alat);
  else cvTampilMenu();
}

/* ----- AI Studio ----- */
const AI_KINDS = [
  { kind: "kerangka",      ico: "layout", nama: "Kerangka",        desc: "Outline bab 1-5 biar nulisnya nggak ngawang.", ph: "cth: Pengaruh literasi digital terhadap prestasi mahasiswa..." },
  { kind: "abstrak",       ico: "doc", nama: "Abstrak (ID+EN)", desc: "Abstrak bilingual Indonesia + Inggris.", ph: "Tempel poin-poin penting penelitianmu..." },
  { kind: "kuesioner",     ico: "clipboard", nama: "Kuesioner",       desc: "Draf item kuesioner dari variabelmu.", ph: "cth: Variabel X: motivasi belajar (5 indikator)..." },
  { kind: "latarbelakang", ico: "spark", nama: "Latar Belakang",  desc: "Draf latar belakang Bab 1.", ph: "cth: Topik: stunting di Manado, fenomena: ..." },
  { kind: "simpulan",      ico: "check", nama: "Simpulan",        desc: "Merangkum hasil jadi simpulan Bab 5.", ph: "Tempel hasil penelitianmu..." },
  { kind: "sidang",        ico: "chat", nama: "Simulasi Sidang", desc: "Prediksi pertanyaan per bab + contoh jawaban.", ph: "Tempel judul + abstrak atau ringkasan tiap bab skripsimu..." },
  { kind: "kisi",          ico: "grid", nama: "Kisi-kisi",       desc: "Kisi-kisi instrumen penelitian.", ph: "cth: Topik/materi yang mau diukur..." },
  { kind: "metode",        ico: "chart", nama: "Metode",          desc: "Draf Bab 3: desain, populasi, teknik analisis.", ph: "cth: Kuantitatif, populasi mahasiswa..." },
  { kind: "pembahasan",    ico: "quote", nama: "Pembahasan",      desc: "Bantu ngebahas hasil penelitianmu.", ph: "Tempel hasil + teori pendukung..." },
  { kind: "bab2",          ico: "books", nama: "Bab 2",           desc: "Draf tinjauan pustaka per variabel.", ph: "cth: Topik + variabel penelitian..." },
  /* PORT 2026-10-28: 4 kind baru dari patch khairypustaka-web. */
  { kind: "idejudul",      ico: "spark", nama: "Ide Judul + Gap", desc: "3 ide judul konkret + research gap tiap judul.", ph: "cth: Jurusan + topik yang kamu minati..." },
  { kind: "bahasa",        ico: "doc", nama: "Bahasa Akademik", desc: "Perbaiki kalimat jadi bahasa akademik baku tanpa ubah maksud.", ph: "Tempel kalimat yang mau diperbaiki..." },
  { kind: "sidanglanjut",  ico: "chat", nama: "Latihan Sidang Lanjutan", desc: "Feedback atas jawabanmu + 1 pertanyaan lanjutan penguji.", ph: "Tempel judul + jawabanmu atas pertanyaan penguji..." },
  { kind: "konsep",        ico: "books", nama: "Penjelasan Konsep", desc: "Definisi, contoh, kesalahan umum, istilah yang tertukar.", ph: "cth: Konsep/istilah yang membingungkan..." }
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
  if (sisa == null) { b.textContent = "Kuota nggak kebaca"; return; }
  if (sisa >= 999) { b.innerHTML = "Kuota unlimited " + tic("bolt"); return; }
  b.innerHTML = sisa > 0 ? "Sisa kuota hari ini: " + sisa + "x " + tic("bolt") : "Kuota hari ini habis";
  b.classList.toggle("habis", sisa <= 0);
}

function kuotaHabisBox() {
  return '<div class="warn-box">Yah, kuota AI harianmu habis (' + aiHarian() + 'x/hari). ' +
    (AUTH.login
      ? 'Besok reset otomatis kok. Semangat ngerjainnya! '
      : '<a href="#/masuk">Login gratis</a> biar fitur non-AI sepuasnya! ') + '</div>';
}

/* Pop up jatah habis (disetujui Sul 2026-10-18, menggantikan sekadar
   kotak peringatan): member diarahkan top up, tamu diarahkan daftar.
   Muncul sekali per kejadian kuota habis; admin tidak pernah kena. */
function popupJatahHabis() {
  if (AUTH.admin) return;
  if (document.getElementById("kpJatahPopup")) return;
  const login = !!AUTH.login;
  const el = document.createElement("div");
  el.className = "modal";
  el.id = "kpJatahPopup";
  el.style.alignItems = "center";
  el.style.padding = "16px";
  el.innerHTML =
    '<div class="modal-box" style="border-radius:20px;max-width:420px;text-align:center">' +
      '<div style="width:52px;height:52px;margin:2px auto 0;border-radius:50%;background:#102c44;display:flex;align-items:center;justify-content:center">' +
        '<svg width="26" height="26" viewBox="0 0 24 24" fill="#f0a832" aria-hidden="true"><path d="M13 2 4.5 13.5H11L9.8 22 19 10h-6.5L13 2z"/></svg></div>' +
      '<h3 style="margin:10px 0 0">' + (login ? "Jatah AI Harianmu Habis" : "Jatah Tamu Hari Ini Habis") + '</h3>' +
      '<div style="margin-top:12px;background:#102c44;border-radius:10px;padding:8px 10px;text-align:left">' +
        '<div style="display:flex;justify-content:space-between;font-size:.8rem"><span>' + (login ? "Jatah gratis hari ini" : "Jatah tamu hari ini") + '</span><span><b>' + (login ? "15 dari 15" : "5 dari 5") + '</b> terpakai</span></div>' +
        '<div style="height:8px;background:#0a1f30;border-radius:99px;margin-top:6px;overflow:hidden"><div style="width:100%;height:100%;background:#f0a832"></div></div></div>' +
      (login
        ? '<p class="muted" style="text-align:left">Jatah AI gratis memang cuma <b>15x per hari</b>. Tenang, jatahnya reset otomatis <b>tengah malam pukul 00.00 WITA</b>, jadi besok bisa kamu pakai lagi seperti biasa.</p>' +
          '<p class="muted" style="text-align:left">Tidak mau menunggu? Top up saja. Mulai <b>Rp5.000</b> kamu dapat <b>25x</b> jatah AI. Saldonya tidak hangus dan bisa dipakai kapan saja.</p>'
        : '<p class="muted" style="text-align:left">Limit harian mode tamu sudah terpakai semua. Tamu dapat <b>5x per hari</b> buat semua fitur, AI dan non-AI dihitung bareng. Tenang, jatahnya balik lagi otomatis <b>tengah malam pukul 00.00 WITA</b>, jadi besok kamu bisa lanjut lagi.</p>' +
          '<p class="muted" style="text-align:left">Tidak mau dibatasi limit harian? <b>Daftar gratis</b> saja, tidak sampai semenit. Begitu punya akun, fitur non-AI seperti cari jurnal, sitasi, convert, dan cek kemiripan bisa kamu pakai <b>sepuasnya</b> tanpa limit harian, jatah AI tetap 15x per hari, plus koleksi dan riwayatmu tersimpan aman di akun.</p>') +
      '<a class="btn btn-primary btn-block" id="kpJatahCta" href="' + (login ? "#/topup" : "#/masuk") + '">' + (login ? "" + tic('bolt') + " Top Up Jatah AI" : "" + tic('key') + " Daftar Gratis") + '</a>' +
      '<button class="btn btn-block" id="kpJatahTutup" type="button" style="margin-top:9px">Nanti Saja</button>' +
    '</div>';
  document.body.appendChild(el);
  const tutup = () => { el.remove(); window.removeEventListener("hashchange", tutup); };
  el.addEventListener("click", e => { if (e.target === el) tutup(); });
  el.querySelector("#kpJatahTutup").addEventListener("click", tutup);
  el.querySelector("#kpJatahCta").addEventListener("click", tutup);
  window.addEventListener("hashchange", tutup);
}

/* ----- Pemilih model AI (keputusan Sul 2026-10-19; daftar diperbarui
   port 2026-10-28) -----
   Satu komponen yang sama di AI Studio, Tanya AI, dan Ide Topik.
   Pilihan terakhir disimpan (kp_model); nilai lama yang tidak ada di
   daftar ini (llama/claude/sonar) otomatis jatuh ke "auto" lewat
   modelPilihan() di bawah. TIDAK ADA lagi opsi premium: semua model di
   sini gratis, dan gerbang sebenarnya tetap di server (pilihan di luar
   daftar dipetakan ke auto, tidak pernah ke model berbayar).
   REVOKASI 2026-10-28: handler premium lama (popup/kartu/lock premium)
   DIHAPUS TOTAL dari file ini, bukan lagi kode mati. */
const MODEL_OPSI = [
  { k: "auto",   n: "Otomatis",        d: "Rekomendasi. Sistem pilihkan model terbaik yang lagi sehat." },
  { k: "gptoss", n: "GPT OSS 120B",    d: "Cepat dan serba bisa buat nulis dan analisis." },
  { k: "gemini", n: "Gemini Flash",  d: "Model Google, enak buat tanya jawab cepat." },
  { k: "nemotron_ultra", n: "Nemotron 3 Ultra", d: "Penalaran berat, dokumen panjang." },
  { k: "nemotron_lightning", n: "Nemotron 3.5 Lightning", d: "Kecil dan cepat buat penjelasan singkat." },
  { k: "nemotron_nano", n: "Nemotron 3 Nano Reasoning", d: "Penalaran terstruktur dan interpretasi hasil uji." }
];
let KP_INFO = { sisa: null, saldo: 0, admin: false };
async function ambilInfoKuota() {
  const res = await apiJSON("/kuota");
  if (res.ok && res.data) KP_INFO = { sisa: res.data.sisa, saldo: Number(res.data.saldo || 0), admin: !!res.data.admin };
  return KP_INFO;
}
function modelPilihan() {
  try { const k = localStorage.getItem("kp_model"); return MODEL_OPSI.some(o => o.k === k) ? k : "auto"; }
  catch (e) { return "auto"; }
}
function modelSimpan(k) { try { localStorage.setItem("kp_model", k); } catch (e) {} }
function modelOp(k) { return MODEL_OPSI.find(o => o.k === k) || MODEL_OPSI[0]; }
function pickerListHTML() {
  const aktif = modelPilihan();
  return MODEL_OPSI.map(o => {
    return '<button type="button" class="model-opt' + (o.k === aktif ? " aktif" : "") + '" data-model="' + o.k + '">' +
      '<span class="mo-n">' + esc(o.n) + '</span>' +
      '<span class="mo-d">' + esc(o.d) + '</span></button>';
  }).join("");
}
function pickerHTML(prefix) {
  const cur = modelOp(modelPilihan());
  return '<div class="model-picker" id="' + prefix + 'Picker">' +
    '<button type="button" class="model-btn" id="' + prefix + 'Btn">Model: <b>' + esc(cur.n) + '</b> ▾</button>' +
    '<div class="model-list" id="' + prefix + 'List" style="display:none">' + pickerListHTML() + '</div></div>';
}
function pickerSegarkan(prefix) {
  const list = document.getElementById(prefix + "List");
  if (list) list.innerHTML = pickerListHTML();
  const btn = document.getElementById(prefix + "Btn");
  if (btn) {
    const cur = modelOp(modelPilihan());
    btn.innerHTML = 'Model: <b>' + esc(cur.n) + '</b> ▾';
  }
}
function pickerPasang(prefix) {
  const btn = document.getElementById(prefix + "Btn");
  const list = document.getElementById(prefix + "List");
  if (!btn || !list) return;
  btn.addEventListener("click", () => { list.style.display = list.style.display === "none" ? "block" : "none"; });
  list.addEventListener("click", e => {
    const b = e.target.closest("[data-model]");
    if (!b) return;
    const k = b.dataset.model;
    modelSimpan(k);
    list.style.display = "none";
    pickerSegarkan(prefix);
  });
}
function labelModelHTML(d) {
  if (!d || !d.model_label) return "";
  if (d.dialihkan) return '<div class="model-label warn">Model pilihanmu mengalami kendala saat generate, hasil ini dibuat dengan <b>' + esc(d.model_label) + '</b>.</div>';
  return '<div class="model-label">Dijawab oleh <b>' + esc(d.model_label) + '</b></div>';
}
function pageAI() {
  view().innerHTML =
    "<h2>"+tic("spark")+" AI Studio</h2>" +
    '<p><span class="kuota" id="kuotaBadge">cek kuota...</span></p>' +
    '<p class="muted tiny">' + (AUTH.login
      ? 'Gratis <b>15x/hari</b> buat akun login. Pakai bijak ya! '
      : 'Tamu dapat <b>5x/hari</b> buat semua fitur (AI dan non-AI digabung). <a href="#/masuk">Login gratis</a> biar fitur non-AI sepuasnya! ') + '</p>' +
    pickerHTML("ai") +
    '<div class="ai-grid">' + AI_KINDS.map((k, i) =>
      '<div class="card ai-card"><h3>' + tic(k.ico) + " " + esc(k.nama) + "</h3>" +
      '<p class="muted tiny">' + esc(k.desc) + "</p>" +
      (k.kind === "abstrak" ? '<input type="text" id="aiJudul' + i + '" placeholder="Judul penelitian (opsional, buat abstrak)" autocomplete="off" style="margin-bottom:8px">' : "") +
      '<textarea id="aiIn' + i + '" placeholder="' + esc(k.ph) + '"></textarea>' +
      '<label class="muted tiny" style="display:block;margin-top:8px">Atau isi dari file draf (.docx/.pdf/.txt)<input type="file" class="aiFile" data-aii="' + i + '" accept="' + ACCEPT_DOK + '" style="margin-top:4px"></label>' +
      '<button class="btn btn-primary btn-block" data-aigen="' + i + '" style="margin-top:10px">' + tic("spark") + ' Generate</button>' +
      '<div id="aiOut' + i + '"></div></div>'
    ).join("") + "</div>";

  ambilKuota().then(kuotaBadgeUpdate);
  pickerPasang("ai");
  ambilInfoKuota().then(() => pickerSegarkan("ai"));

  $$(".aiFile", view()).forEach(inp => inp.addEventListener("change", async () => {
    const i = parseInt(inp.dataset.aii, 10);
    const f = inp.files[0];
    if (!f) return;
    toast("Lagi membaca file... ");
    const res = await ekstrakTeksFile(f);
    if (!res.ok) { toast(" " + errFile(res)); inp.value = ""; return; }
    const k = AI_KINDS[i];
    const cap = (k.kind === "pembahasan" || k.kind === "bab2") ? 4000 : 2000;
    let teks = (res.data.teks || "").trim();
    let potong = "";
    if (teks.length > cap) { teks = teks.slice(0, cap); potong = " (dipotong ke " + cap.toLocaleString("id-ID") + " karakter pertama, batas generator ini)"; }
    $("#aiIn" + i).value = teks;
    toast("Draf dari file sudah dimasukkan ke " + k.nama + potong + ". Cek dan edit dulu kalau perlu, baru klik Generate ");
    inp.value = "";
  }));

  const kirimAI = async (i, modelKey, btn) => {
    const k = AI_KINDS[i];
    const teks = ($("#aiIn" + i).value || "").trim();
    if (!teks) { toast("Isi dulu inputnya "); return; }
    const mk = modelKey || modelPilihan();
    const out = $("#aiOut" + i);
    out.innerHTML = '<p class="loading">AI lagi nulis... sabar ya</p>';
    if (btn) setLoading(btn, true, "AI lagi nulis...");
    const body = { kind: k.kind, text: teks, model: mk };
    if (k.kind === "abstrak") {
      const judul = ($("#aiJudul" + i) || {}).value;
      if (judul && judul.trim()) body.extra = judul.trim();
    }
    const resJob = await apiPOST("/ai/job", body);
    if (btn) setLoading(btn, false);
    const dj = resJob.data || {};
    // 429 kuota_habis harus dicek SEBELUM !res.ok (apiJSON memetakan semua
    // non-2xx ke ok:false, jadi cabang ini mati bila ditaruh setelahnya)
    if (!resJob.ok && (dj.error === "kuota_habis" || dj.kuota_habis)) {
      out.innerHTML = kuotaHabisBox(); popupJatahHabis();
      kuotaBadgeUpdate(0);
      return;
    }
    if (!resJob.ok) { out.innerHTML = ""; netErr(out, resJob); return; }
    if (dj.error === "kuota_habis" || dj.kuota_habis) {
      out.innerHTML = kuotaHabisBox(); popupJatahHabis();
      kuotaBadgeUpdate(0);
      return;
    }
    const jid = dj.id;
    if (!jid) { out.innerHTML = ""; netErr(out, resJob); return; }
    // LAPIS 4 (2026-10-29): generasi jalan di antrean worker, bukan di
    // thread web. Poll status tiap 3 detik sampai selesai/gagal; bila
    // kartu sudah tidak tampil (pindah halaman), polling berhenti.
    if (btn) setLoading(btn, true, "AI lagi nulis...");
    out.innerHTML = '<p class="loading">Masuk antrean... sabar ya</p>';
    let d = null;
    let gagalPesan = null;
    // FIX 2026-11-08 (audit putaran 2): polling dibatasi 200 putaran
    // (10 menit) + 3 gagal jaringan beruntun. Pola lama while(true)
    // tanpa batas: pekerjaan yang hilang di server (mis. server
    // restart) membuat tombol terkunci "AI lagi nulis..." selamanya.
    let gagalBeruntun = 0;
    let putaranAI = 0;
    while (true) {
      await new Promise(r => setTimeout(r, 3000));
      if (!out.isConnected) { if (btn) setLoading(btn, false); return; }
      putaranAI++;
      if (putaranAI > 200) {
        gagalPesan = "Pekerjaan AI ini kelamaan dan sepertinya hilang di server. Coba lagi ya.";
        break;
      }
      const st = await apiJSON("/ai/job-status?id=" + encodeURIComponent(jid));
      const sd = st.data || {};
      if (!st.ok) {
        gagalBeruntun++;
        if (gagalBeruntun >= 3) {
          gagalPesan = "Pekerjaan AI ini sepertinya hilang di server. Coba lagi ya.";
          break;
        }
        continue;
      }
      gagalBeruntun = 0;
      if (sd.status === "antre") {
        out.innerHTML = '<p class="loading">Antrean ke-' + (sd.posisi_antrean || 1) + '... sabar ya</p>';
        continue;
      }
      if (sd.status === "memproses") {
        out.innerHTML = '<p class="loading">AI lagi nulis... sabar ya</p>';
        continue;
      }
      if (sd.status === "selesai") { d = sd; break; }
      gagalPesan = sd.error || "AI-nya lagi gangguan, coba lagi bentar ya";
      break;
    }
    if (btn) setLoading(btn, false);
    if (!d) {
      out.innerHTML = '<div class="warn-box">' + esc(gagalPesan) + '</div>';
      ambilKuota().then(kuotaBadgeUpdate);
      return;
    }
    const hasil = d.hasil || d.result || d.text;
    if (!hasil) { out.innerHTML = '<div class="warn-box">AI-nya bengong, nggak ngasih hasil. Coba lagi ya.</div>'; return; }
    const hid = "aiCopy" + i;
    out.innerHTML = '<div class="hasil-ai">' + mdRapi(hasil) + '</div>' + labelModelHTML(d) + '<button class="btn" id="' + hid + '" style="margin-top:8px">' + tic("clipboard") + ' Salin Hasil</button>';
    riwTamuCatat("chat", teks, { sumber: "ai", kind: k.kind, text: teks, hasil: hasil });
    $("#" + hid).addEventListener("click", () => copyText(hasil));
    ambilKuota().then(kuotaBadgeUpdate);
    ambilInfoKuota().then(() => pickerSegarkan("ai"));
    toast("Jadi! Scroll dikit buat lihat hasilnya. ");
  };

  $$("[data-aigen]", view()).forEach(btn => btn.addEventListener("click", () => {
    kirimAI(parseInt(btn.dataset.aigen, 10), null, btn);
  }));
}

/* ----- Tanya AI (chat) ----- */
let tanyaRiwayat = [];
function pageTanya() {
  view().innerHTML =
    "<h2>"+tic("chat")+" Tanya AI</h2>" +
    '<p class="muted">Tanya apa aja soal tugas & skripsi — dari sitasi sampai sidang. Dijawab AI + panduan.</p>' +
    '<div class="chat" id="chatBox"></div>' +
    pickerHTML("ty") +
    '<div class="chat-form"><input type="text" id="tanyaIn" placeholder="Ketik pertanyaanmu..." autocomplete="off">' +
    '<button class="btn btn-primary" id="tanyaBtn">Kirim</button></div>';

  const render = () => {
    const box = $("#chatBox");
    if (!tanyaRiwayat.length) {
      box.innerHTML = '<div class="bubble bot">Halo! Aku AI-nya KhairyPustaka. Tanya apa aja soal tugas & skripsi — sitasi, metode, Bab 2, persiapan sidang, gas!</div>';
    } else {
      box.innerHTML = tanyaRiwayat.map(m => '<div class="bubble ' + m.dari + '">' + (m.html ? richText(m.teks) : (m.md ? mdRapi(m.teks) : esc(m.teks))) + "</div>").join("");
    }
    box.scrollTop = box.scrollHeight;
  };

  let tanyaTerkini = "";
  let tanyaSibuk = false;
  const prosesTanya = async (q, mk) => {
    tanyaTerkini = q;
    // FIX 2026-11-08 (audit putaran 2): placeholder disimpan sebagai
    // REFERENSI OBJEK dan dibuang via indexOf, bukan pop() buta. Pop
    // bisa membuang pesan yang salah bila ada yang menyisip di tengah
    // (kiriman kedua), menukar urutan riwayat.
    const placeholder = { dari: "bot", teks: "Lagi mikir... " };
    tanyaRiwayat.push(placeholder);
    render();
    const btn = $("#tanyaBtn"); setLoading(btn, true, "...");
    const res = await apiPOST("/tanya", { q: q, model: mk });
    setLoading(btn, false);
    const ixPh = tanyaRiwayat.indexOf(placeholder);
    if (ixPh >= 0) tanyaRiwayat.splice(ixPh, 1);
    const d = res.data || {};
    // 429 kuota_habis harus dicek SEBELUM !res.ok (lihat pageAI)
    if (!res.ok && (d.error === "kuota_habis" || d.kuota_habis)) {
      popupJatahHabis(); tanyaRiwayat.push({ dari: "bot", teks: "Yah, kuota AI harianmu habis (" + aiHarian() + "x/hari). " + (AUTH.login ? "Besok reset lagi kok! " : "Login gratis biar fitur non-AI sepuasnya! ") });
    } else if (!res.ok) {
      tanyaRiwayat.push({ dari: "bot", teks: (d.pesan || d.error) ? String(d.pesan || d.error) : "Waduh koneksinya bermasalah. Coba kirim lagi ya!" });
    } else {
      if (d.error === "kuota_habis" || d.kuota_habis) {
        popupJatahHabis(); tanyaRiwayat.push({ dari: "bot", teks: "Yah, kuota AI harianmu habis (" + aiHarian() + "x/hari). " + (AUTH.login ? "Besok reset lagi kok! " : "Login gratis biar fitur non-AI sepuasnya! ") });
      } else {
        // kontrak backend: {tipe:"panduan"|"ai", jawaban, judul, terkait:[{id,title}]}
        let teks = d.jawaban || d.hasil || d.text || "Hmm, aku nggak nemu jawabannya. Coba tanya dengan kata lain ya.";
        const isPanduan = d.tipe === "panduan";
        if (isPanduan) teks += "\n\n(dari panduan)";
        if (d.judul) teks = d.judul + "\n\n" + teks;
        if (!isPanduan && d.model_label) {
          if (d.dialihkan) teks += "\n\n• Model pilihanmu mengalami kendala saat generate, jawaban ini dibuat dengan " + d.model_label + ".";
          else teks += "\n\n• Dijawab oleh " + d.model_label + ".";
        }
        tanyaRiwayat.push({ dari: "bot", teks: teks, html: isPanduan, md: !isPanduan });
        riwTamuCatat("chat", q, { sumber: "tanya", q: q, jawaban: teks });
        if (d.model_label) ambilInfoKuota().then(() => pickerSegarkan("ty"));
        const rel = Array.isArray(d.terkait) ? d.terkait : [];
        if (rel.length) tanyaRiwayat.push({ dari: "bot", teks: "Mungkin kamu juga mau tanya:\n" + rel.slice(0, 4).map(r => "- " + (r && r.title ? r.title : r)).join("\n") });
      }
    }
    render();
  };

  const kirimTanya = async () => {
    // FIX 2026-11-08: satu pertanyaan dalam satu waktu; tombol/Enter
    // selama proses berjalan diabaikan (dulu Enter ganda mengirim dua
    // request dan riwayatnya tertukar).
    if (tanyaSibuk) return;
    const inp = $("#tanyaIn");
    const q = (inp.value || "").trim();
    if (!q) return;
    const mk = modelPilihan();
    tanyaRiwayat.push({ dari: "user", teks: q });
    inp.value = "";
    tanyaSibuk = true;
    const btnKirim = $("#tanyaBtn");
    if (btnKirim) btnKirim.disabled = true;
    try {
      await prosesTanya(q, mk);
    } finally {
      tanyaSibuk = false;
      if (btnKirim) btnKirim.disabled = false;
    }
  };

  render();
  pickerPasang("ty");
  ambilInfoKuota().then(() => pickerSegarkan("ty"));
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
  // Tamu (perintah Sul 2026-10-18): halaman ini HANYA berisi info jatah
  // gratis + ajakan login. Form top-up, kartu redeem, dan kode referral
  // khusus member login karena saldo & kode terikat akun — menampilkan
  // ketiganya ke tamu hanya membingungkan (dan saldonya pasti hilang).
  if (!AUTH.login) {
    view().innerHTML =
      "<h2>" + tic("bolt") + " Jatah AI</h2>" +
      '<div class="card"><h3>Sisa jatahmu</h3>' +
      '<p>' + tic("ticket") + ' Gratis hari ini: <b id="tamuSisa">' + tamuHarian() + 'x</b> <span class="muted tiny">(reset tiap tengah malam WITA)</span></p>' +
      '<p class="muted">Kamu lagi mode tamu, jadi belum punya saldo top-up. Jatah tamu 5x/hari itu gabungan semua fitur (AI dan non-AI). Login gratis biar fitur non-AI sepuasnya, plus bisa top-up saldo yang tidak hangus, redeem kode, dan dapat kode referral buat ajak teman.</p></div>' +
      '<div class="card center"><a class="btn btn-primary btn-block" href="#/masuk">' + tic("key") + ' Masuk / Daftar — gratis!</a></div>';
    // Samakan angka dengan sisa asli dari server (tamu bisa sudah memakai sebagian).
    apiJSON("/topup").then(res => {
      const el = $("#tamuSisa");
      if (el && res && res.ok && res.data && res.data.sisa_harian != null) {
        el.textContent = Number(res.data.sisa_harian) + "x";
      }
    });
    return;
  }
  view().innerHTML =
    "<h2>"+tic("bolt")+" Jatah AI</h2>" +
    '<div id="topInfo"><p class="loading">Lagi ngecek jatah AI...</p></div>' +
    '<div id="topPay"></div>' +
    '<div class="card" id="redCard"><h3>Punya kode redeem? ' + tic("ticket") + '</h3>' +
    '<label for="kodeRed">Kode redeem</label><input type="text" id="kodeRed" placeholder="Tempel kodemu di sini" autocomplete="off">' +
    '<button class="btn btn-accent btn-block" id="btnRedeem" style="margin-top:12px">' + tic("ticket") + ' Redeem Sekarang</button></div>' +
    '<div class="card"><h3>' + tic("ticket") + ' Kode referralmu</h3><p class="muted tiny">Ajak teman pakai KhairyPustaka, tiap 3 teman dapat bonus jatah AI!</p>' +
    '<div id="refBox"><p class="loading">Lagi ngambil kode...</p></div></div>';

  function refreshInfo() {
    apiJSON("/topup").then(res => {
      const box = $("#topInfo");
      if (!box) return;
      if (!res.ok) { box.innerHTML = '<div class="warn-box">Jatah AI lagi nggak kebaca. Coba refresh ya.</div>'; return; }
      const d = res.data || {};
      const isAdm = d.admin === true;
      const harian = d.sisa_harian != null ? d.sisa_harian : 0;
      const saldo = d.saldo != null ? d.saldo : 0;
      const harga = d.harga_rp || 5000, uses = d.uses || 25;
      const minU = d.min_uses || 25, perU = d.harga_per_use || 200;
      _topMinU = minU; // FIX A12: dipakai topupCreate menyamakan angka
      let html = '<div class="card"><h3>Sisa jatahmu</h3>' +
        (isAdm
          ? '<p>' + tic("ticket") + ' Akun admin: jatah AI tanpa batas, pakai sepuasnya</p>'
          : '<p>' + tic("ticket") + ' Gratis hari ini: <b>' + harian + 'x</b> <span class="muted tiny">(reset tiap tengah malam WITA)</span></p>') +
        '<p>' + tic("bolt") + ' Saldo top-up: <b>' + saldo + 'x</b> <span class="muted tiny">(tidak hangus)</span></p></div>';
      if (d.pay_enabled && !isAdm) {
        html += '<div class="card"><h3>Tambah jatah ' + tic("bolt") + '</h3>' +
          '<p class="muted">Rp' + Number(perU).toLocaleString("id-ID") + ' per 1x pakai AI, minimal ' + minU + 'x. Bayar via QRIS, masuk otomatis.</p>' +
          '<label for="topUses">Jumlah jatah (x)</label><input type="number" id="topUses" min="' + minU + '" value="' + minU + '" step="1" inputmode="numeric">' +
          '<p class="muted tiny">Total bayar: <b id="topTotal"></b></p>' +
          '<button class="btn btn-primary btn-block" id="btnTopup">' + tic("bolt") + ' Top-up via QRIS</button></div>';
      }
      box.innerHTML = html;
      // Admin: kartu redeem disembunyikan (kuota admin tanpa batas).
      const redCard = $("#redCard");
      if (redCard) redCard.style.display = isAdm ? "none" : "";
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

  let _topMinU = 25;
  async function topupCreate() {
    const payBox = $("#topPay");
    const btn = $("#btnTopup"); setLoading(btn, true, "Bikin kode bayar...");
    const inpU = $("#topUses");
    // FIX 2026-11-08 (audit putaran 2): angka TERKIRIM harus sama dengan
    // angka TAMPIL. Pola lama: tampilan dijepit ke minimum oleh
    // updTotal() tapi yang dikirim nilai mentah input, jadi tagihan
    // bisa berbeda dari yang dilihat pengguna. Sekarang nilai dijepit
    // dulu dan input ditulis ulang supaya keduanya sama.
    let wantUses = inpU ? parseInt(inpU.value || "0", 10) || 0 : 0;
    if (wantUses < _topMinU) {
      wantUses = _topMinU;
      if (inpU) inpU.value = String(wantUses);
      toast("Jumlah minimumnya " + _topMinU + "x, disamakan ya.");
    }
    const res = await apiPOST("/topup/create", {uses: wantUses});
    setLoading(btn, false);
    if (!res.ok) {
      const rd = res.data || {};
      if (rd.need_login || res.status === 401) {
        toast(" Login dulu biar saldomu aman ya.");
        location.hash = "#/masuk";
        return;
      }
      toast(" " + ((res.data && (res.data.error || res.data.pesan)) || "Gagal bikin kode bayar, coba lagi ya."));
      return;
    }
    const d = res.data || {};
    if (!d.ref_id) { toast("Gagal bikin kode bayar, coba lagi ya. "); return; }
    const total = d.total ? "Rp" + Number(d.total).toLocaleString("id-ID") : "";
    tampilkanQR(payBox, d.ref_id, total, d.qr_b64);
    payBox.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  // tampilkan QR + polling status; dipakai juga untuk resume invoice pending
  function tampilkanQR(payBox, refId, total, qrB64) {
    const qr = amanB64(qrB64);
    payBox.innerHTML =
      '<div class="card center"><h3>' + tic("grid") + ' Scan QRIS buat bayar ' + esc(total) + '</h3>' +
      (qr ? '<img src="data:image/png;base64,' + qr + '" alt="QRIS" style="max-width:260px;width:100%;border-radius:12px">' : '<p class="muted">QR nggak kebaca, coba lagi ya.</p>') +
      '<p class="muted tiny" id="payStat">Menunggu pembayaran... (otomatis dicek tiap 8 detik)</p></div>';
    const timer = regTimer(setInterval(async () => {
      const st = await apiJSON("/topup/status/" + encodeURIComponent(refId));
      const sd = (st && st.data) || {};
      if (sd.paid) {
        clearInterval(timer);
        payBox.innerHTML = '<div class="ok-box center"> <b>Pembayaran diterima!</b><br>+' + Number(sd.uses || 0) + 'x Jatah AI ditambahkan. Saldo: <b>' + Number(sd.saldo || 0) + 'x</b></div>';
        refreshInfo();
        toast("Jatah AI masuk! ");
      }
    }, 8000));
    // berhenti polling setelah 65 menit (invoice kedaluwarsa 1 jam).
    // FIX 2026-11-08 (audit putaran 2): sebelumnya polling berhenti
    // DIAM-DIAM; sekarang tampilkan pesan kedaluwarsa yang jujur bila
    // QR masih tampil dan belum terbayar.
    setTimeout(() => {
      clearInterval(timer);
      const st = document.getElementById("payStat");
      if (st) st.textContent = "Kode bayar ini sudah kedaluwarsa (lebih dari 1 jam). Klik Top-up via QRIS lagi buat bikin kode baru ya.";
    }, 65 * 60 * 1000);
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
        toast("Kamu punya pembayaran yang belum selesai ");
      }
    });
  }

  apiJSON("/kode-referral").then(res => {
    const box = $("#refBox");
    if (!box) return;
    if (!res.ok || !res.data) { box.innerHTML = '<p class="muted tiny">Kode referral lagi nggak kebaca.</p>'; return; }
    const kode = res.data.kode || res.data.referral || res.data.code || "";
    const reward = res.data.reward_uses || 30;
    box.innerHTML = kode
      ? '<p><b class="ref-code">' + esc(kode) + '</b></p>' +
        '<p class="muted tiny">Tiap 3 teman gabung = +' + reward + 'x Jatah AI</p>' +
        '<button class="btn" id="refCopy">' + tic("clipboard") + ' Salin Link Referral</button>'
      : '<p class="muted tiny">Belum ada kode referral.</p>';
    const cp = $("#refCopy");
    if (cp) cp.addEventListener("click", () => copyText(location.origin + "/?ref=" + kode));
  });

  $("#btnRedeem").addEventListener("click", async () => {
    const kode = ($("#kodeRed").value || "").trim();
    if (!kode) { toast("Tempel dulu kodenya "); return; }
    const btn = $("#btnRedeem"); setLoading(btn, true, "Lagi dicek...");
    const res = await apiPOST("/redeem", { kode: kode });
    setLoading(btn, false);
    if (!res.ok) {
      const rd = res.data || {};
      if (rd.need_login || res.status === 401) {
        toast(" Login dulu biar saldomu aman ya.");
        location.hash = "#/masuk";
        return;
      }
      toast(res.error === "network" ? "Koneksi bermasalah, coba lagi ya. " : "Gagal redeem, coba lagi ya. ");
      return;
    }
    const d = res.data || {};
    const ok = d.ok === true || d.sukses === true || d.success === true;
    if (ok) {
      toast("Kode valid! +" + (d.uses || "") + "x Jatah AI! ");
      $("#kodeRed").value = ""; refreshInfo();
      return;
    }
    const alasan = d.alasan || d.reason || "";
    const msg = alasan === "used" ? "Kode ini sudah dipakai. "
      : alasan === "notfound" ? "Kode nggak ketemu. Cek lagi ya. "
      : (d.pesan || d.message || d.error || "Kode nggak valid / udah dipakai.");
    toast(" " + msg);
  });
}

/* ----- Bantuan ----- */
function pageBantuan() {
  const faq = [
    ["KhairyPustaka itu apa? ", "Asisten skripsi & nugas buat mahasiswa semua jurusan: cari jurnal, bikin sitasi 6 gaya, matriks literatur, sampai 14 generator AI naskah. Gratis! "],
    ["Beneran gratis? ", "Iya! Buat tamu, semua fitur berbagi jatah gabungan " + tamuHarian() + "x/hari (AI dan non-AI dihitung bareng). Login gratis bikin fitur non-AI sepuasnya, jatah AI tetap 15x/hari, dan datamu tersimpan. "],
    ["Koleksiku kesimpen di mana? ", "Di browser HP-mu sendiri (localStorage). Buat tamu, koleksi otomatis terhapus setelah 24 jam; login gratis biar tidak kena batas waktu itu. Catatan: kalau hapus data browser / ganti HP, koleksinya ikut hilang, jadi rajin-rajin export ya!"],
    ["Sitasi gaya apa aja yang didukung? ", "6 gaya: Harvard (default), APA, MLA, Chicago, Vancouver, IEEE. Tinggal pilih, salin, tempel ke tugas/skripsimu."],
    ["AI-nya pakai apa? ", "Otomatis pilih yang terbaik & gratis: Gemini, Cerebras, OpenRouter. Butuh lebih banyak? Top-up jatah AI di menu Jatah AI — Rp200/x, minimal 25x (Rp5.000), nggak hangus."],
    ["Error / nggak jalan, gimana? ", "Coba refresh dulu. Kalau masih error, cek koneksi internetmu. Masih bandel juga? Hubungi admin lewat tombol Hubungi Admin di bagian bawah halaman. "]
  ];
  view().innerHTML =
    "<h2>"+tic("help")+" Bantuan</h2>" +
    faq.map(f => '<div class="card"><h3>' + f[0] + "</h3><p>" + f[1] + "</p></div>").join("") +
    '<div class="card center"><h3>Masih bingung? ' + tic("chat") + '</h3><p class="muted">Chat admin langsung, fast respon di jam kerja. ' + tic("spark") + '</p>' +
    '<a class="btn btn-primary" href="https://instagram.com/khairypustaka" target="_blank" rel="noopener">' + tic("ig") + ' Hubungi Admin</a></div>';
}

/* FIX 2026-11-08 (audit putaran 2): maskotSVG sebelumnya TIDAK pernah
   didefinisikan, jadi terapkanInfoSitus() melempar ReferenceError dan
   overlay pemeliharaan tidak pernah tampil. Definisi ini memakai aset
   maskot resmi yang sama dengan logo/hero (maskot.jpg). */
function maskotSVG(ukuran) {
  const u = ukuran || 64;
  return '<img src="maskot.jpg" alt="Maskot KhairyPustaka" width="' + u +
    '" height="' + u + '" style="border-radius:18px">';
}

/* ---------- Info situs: pemeliharaan + sakelar tool (Fase 4) ---------- */
let _infoSitus = null;
let _infoSitusTs = 0;
const KP_RUTE_AUTH = ["/masuk", "/daftar", "/lupa", "/reset", "/verifikasi", "/konfirmasi-email"];

async function muatInfoSitus() {
  if (_infoSitus && Date.now() - _infoSitusTs < 30000) {
    terapkanInfoSitus();
    return;
  }
  try {
    const r = await apiJSON("/pengumuman");
    if (r.ok && r.data) {
      _infoSitus = r.data;
      _infoSitusTs = Date.now();
    }
  } catch (e) {}
  terapkanInfoSitus();
}

function terapkanInfoSitus() {
  const h = (location.hash || "#/beranda").replace(/^#/, "").split("?")[0];
  let ov = document.getElementById("kpPemeliharaan");
  const aktif = !!(_infoSitus && _infoSitus.pemeliharaan && !AUTH.admin &&
    KP_RUTE_AUTH.indexOf(h) < 0);
  if (aktif) {
    if (!ov) {
      ov = document.createElement("div");
      ov.id = "kpPemeliharaan";
      ov.className = "kp-maint";
      ov.innerHTML =
        '<div class="kp-maint-box">' + maskotSVG(64) +
        "<h2>Situs lagi pemeliharaan sebentar</h2>" +
        "<p>Kami lagi menyiapkan perbaikan biar pengalamanmu memakai KhairyPustaka makin nyaman. Coba lagi nanti ya, terima kasih sudah menunggu dan tetap semangat ngerjain tugasnya.</p>" +
        (AUTH.login ? "" : '<a class="btn btn-ghost" href="#/masuk">Masuk</a>') +
        "</div>";
      document.body.appendChild(ov);
    }
  } else if (ov) {
    ov.remove();
  }
  tandaiToolNonaktif();
}

function tandaiToolNonaktif() {
  const off = (_infoSitus && _infoSitus.tool_off) || [];
  $$(".feat[data-tool]").forEach(function(el) {
    const mati = off.indexOf(el.dataset.tool) >= 0;
    el.classList.toggle("tool-off", mati);
    let b = el.querySelector(".tool-off-badge");
    if (mati && !b) {
      b = document.createElement("span");
      b.className = "tool-off-badge";
      b.textContent = "Lagi nonaktif";
      el.appendChild(b);
    } else if (!mati && b) {
      b.remove();
    }
    if (!el.dataset.kpOffGuard) {
      el.dataset.kpOffGuard = "1";
      el.addEventListener("click", function(e) {
        if (el.classList.contains("tool-off")) {
          e.preventDefault();
          toast("Fitur ini lagi dinonaktifkan sementara buat perbaikan. Fitur lain tetap bisa dipakai ya.");
        }
      });
    }
  });
}

/* ---------- Kritik dan Saran + popup penilaian (Fase 3) ---------- */
const _bintangPilih = {};
function bintangHTML(idGrup) {
  return '<div class="bintang-row" data-bintang="' + idGrup + '">' +
    [1, 2, 3, 4, 5].map(function(n) {
      return '<button type="button" class="bintang-btn" data-nilai="' + n +
        '" aria-label="' + n + ' bintang">★</button>';
    }).join("") +
    '<span class="bintang-note" id="bintangNote-' + idGrup + '">Belum memilih</span></div>';
}
function ikatBintang(idGrup) {
  _bintangPilih[idGrup] = 0;
  $$('.bintang-row[data-bintang="' + idGrup + '"] .bintang-btn').forEach(function(b) {
    b.addEventListener("click", function() {
      const n = parseInt(b.dataset.nilai, 10) || 0;
      _bintangPilih[idGrup] = n;
      $$('.bintang-row[data-bintang="' + idGrup + '"] .bintang-btn').forEach(function(x) {
        x.classList.toggle("aktif", (parseInt(x.dataset.nilai, 10) || 0) <= n);
      });
      const note = document.getElementById("bintangNote-" + idGrup);
      if (note) note.textContent = n + " dari 5 bintang";
      b.classList.remove("pop");
      void b.offsetWidth;
      b.classList.add("pop");
    });
  });
}
function nilaiBintang(idGrup) { return _bintangPilih[idGrup] || 0; }

const _katPilih = {};
function ikatKategori(idRow, awal) {
  _katPilih[idRow] = awal;
  $$("#" + idRow + " .chip").forEach(function(c) {
    c.addEventListener("click", function() {
      _katPilih[idRow] = c.dataset.kat;
      $$("#" + idRow + " .chip").forEach(function(x) {
        x.classList.toggle("active", x === c);
      });
    });
  });
}
function kategoriHTML(idRow) {
  const opsi = [["kritik", "Kritik"], ["saran", "Saran"],
                ["fitur", "Permintaan Fitur"], ["penilaian", "Penilaian"]];
  return '<div class="chip-row" id="' + idRow + '">' + opsi.map(function(o, i) {
    return '<button type="button" class="chip' + (i === 0 ? " active" : "") +
      '" data-kat="' + o[0] + '">' + o[1] + "</button>";
  }).join("") + "</div>";
}

function pageKritikSaran() {
  if (!AUTH.login) {
    view().innerHTML =
      "<h2>" + tic("chat") + " Kritik dan Saran</h2>" +
      '<div class="card center"><p class="muted">Kirim kritik, saran, masukan, atau permintaan fitur buat KhairyPustaka. Biar tercatat rapi, kirimnya setelah login ya.</p>' +
      '<a class="btn btn-primary btn-block" href="#/masuk">' + tic("key") + ' Masuk Dulu</a></div>';
    return;
  }
  if (AUTH.admin) {
    view().innerHTML =
      "<h2>" + tic("chat") + " Kritik dan Saran</h2>" +
      '<div class="card center"><p class="muted">Kamu masuk sebagai admin. Feedback dari member bisa dibaca di Panel Admin, tab Log.</p>' +
      '<a class="btn btn-primary btn-block" href="#/admin">Buka Panel Admin</a></div>';
    return;
  }
  view().innerHTML =
    "<h2>" + tic("chat") + " Kritik dan Saran</h2>" +
    '<div class="card">' +
      '<p class="muted">Ceritakan pengalamanmu memakai KhairyPustaka. Masukanmu membantu kami memperbaiki layanan dan menentukan fitur berikutnya yang paling dibutuhkan mahasiswa.</p>' +
      "<label>Pilih kategori masukan</label>" +
      kategoriHTML("ksKat") +
      "<label>Beri penilaian untuk KhairyPustaka</label>" +
      bintangHTML("ks") +
      '<label for="ksSaran">Tulis masukanmu</label>' +
      '<textarea id="ksSaran" maxlength="1000" placeholder="Tulis kritik, saran, atau fitur yang kamu inginkan di sini"></textarea>' +
      '<label for="ksFitur">Fitur tambahan yang kamu inginkan (boleh kosong)</label>' +
      '<textarea id="ksFitur" maxlength="1000" style="min-height:76px" placeholder="Tulis fitur tambahan yang menurutmu perlu ada di KhairyPustaka"></textarea>' +
      '<button class="btn btn-primary btn-block" id="ksKirim" type="button">Kirim</button>' +
      '<p class="pf-msg" id="ksMsg"></p>' +
      '<div id="ksThanks"></div>' +
    "</div>";
  ikatKategori("ksKat", "kritik");
  ikatBintang("ks");
  $("#ksKirim").addEventListener("click", async function() {
    // FIX 2026-11-08: kunci tombol selama pengiriman feedback berjalan.
    const btnKs = this;
    if (btnKs.disabled) return;
    btnKs.disabled = true;
    try {
    const msg = $("#ksMsg");
    msg.textContent = "";
    const bintang = nilaiBintang("ks");
    const saran = ($("#ksSaran").value || "").trim();
    const fitur = ($("#ksFitur").value || "").trim();
    if (!bintang) { msg.textContent = "Pilih bintang dulu ya."; return; }
    if (!saran && !fitur) { msg.textContent = "Tulis dulu masukanmu sebelum mengirim."; return; }
    const r = await apiPOST("/feedback", {
      kategori: _katPilih["ksKat"] || "kritik",
      bintang: bintang, saran: saran, fitur: fitur
    });
    if (!r.ok) {
      msg.textContent = (r.data && r.data.error) || "Gagal mengirim. Coba lagi ya.";
      return;
    }
    $("#ksThanks").innerHTML =
      '<div class="ok-box"><b>Terima kasih banyak.</b> Kritik dan saranmu sudah kami terima dan akan dibaca untuk perbaikan KhairyPustaka.</div>';
    $("#ksSaran").value = "";
    $("#ksFitur").value = "";
    toast("Feedback terkirim. Terima kasih! ");
    } finally { btnKs.disabled = false; }
  });
}

let _popupFbTampil = false;
let _popupFbCekTerakhir = 0;
async function cekPopupFeedback() {
  if (_popupFbTampil || !AUTH.login || AUTH.admin) return;
  const now = Date.now();
  if (now - _popupFbCekTerakhir < 15000) return;
  _popupFbCekTerakhir = now;
  try {
    const r = await apiJSON("/feedback/status");
    if (r.ok && r.data && r.data.perlu) tampilPopupFeedback();
  } catch (e) {}
}

function tampilPopupFeedback() {
  if (_popupFbTampil || document.getElementById("kpPopupFb")) return;
  _popupFbTampil = true;
  apiPOST("/feedback/ditanya", {}).catch(function() {});
  const el = document.createElement("div");
  el.className = "modal";
  el.id = "kpPopupFb";
  el.style.alignItems = "center";
  el.style.padding = "16px";
  el.innerHTML =
    '<div class="modal-box" style="border-radius:20px;max-width:520px">' +
      '<div class="modal-head"><h3>Bagaimana pengalamanmu?</h3>' +
      '<button class="x" id="pfTutup" type="button" aria-label="Tutup">✕</button></div>' +
      '<p class="muted">Kamu sudah memakai beberapa alat di KhairyPustaka. Luangkan sebentar untuk memberi penilaian dan masukan agar kami bisa melayani kebutuhan tugas dan skripsimu lebih baik lagi.</p>' +
      '<div id="pfIsi">' +
        "<label>Penilaianmu untuk KhairyPustaka</label>" +
        bintangHTML("pf") +
        '<label for="pfSaran">Saran atau masukan</label>' +
        '<textarea id="pfSaran" maxlength="1000" placeholder="Tulis saran atau tanggapanmu tentang KhairyPustaka"></textarea>' +
        '<label for="pfFitur">Fitur tambahan yang kamu inginkan</label>' +
        '<textarea id="pfFitur" maxlength="1000" style="min-height:76px" placeholder="Tulis fitur tambahan yang menurutmu perlu ada di KhairyPustaka"></textarea>' +
        '<p class="pf-msg" id="pfMsg"></p>' +
        '<button class="btn btn-primary btn-block" id="pfKirim" type="button">Kirim</button>' +
        '<button class="btn btn-ghost btn-block" id="pfNanti" type="button" style="margin-top:8px">Nanti saja</button>' +
      "</div>" +
    "</div>";
  document.body.appendChild(el);
  ikatBintang("pf");
  function tutup(tunda) {
    if (tunda) apiPOST("/feedback/tunda", {}).catch(function() {});
    el.remove();
    _popupFbTampil = false;
  }
  el.querySelector("#pfTutup").addEventListener("click", function() { tutup(true); });
  el.querySelector("#pfNanti").addEventListener("click", function() {
    tutup(true);
    toast("Baik, penilaian bisa kamu berikan kapan saja lewat menu Kritik dan Saran.");
  });
  el.addEventListener("click", function(e) { if (e.target === el) tutup(true); });
  el.querySelector("#pfKirim").addEventListener("click", async function() {
    // FIX 2026-11-08: kunci tombol kirim popup selama request berjalan.
    const btnPf = this;
    if (btnPf.disabled) return;
    btnPf.disabled = true;
    try {
    const msg = el.querySelector("#pfMsg");
    const bintang = nilaiBintang("pf");
    const saran = (el.querySelector("#pfSaran").value || "").trim();
    const fitur = (el.querySelector("#pfFitur").value || "").trim();
    if (!bintang) { msg.textContent = "Pilih bintang dulu ya."; return; }
    if (!saran && !fitur) { msg.textContent = "Tulis dulu masukanmu ya."; return; }
    const r = await apiPOST("/feedback", {
      kategori: "penilaian", bintang: bintang, saran: saran, fitur: fitur
    });
    if (!r.ok) {
      msg.textContent = (r.data && r.data.error) || "Gagal mengirim. Coba lagi ya.";
      return;
    }
    el.querySelector("#pfIsi").innerHTML =
      '<div class="center" style="padding:10px 0"><h3>Terima kasih atas penilaianmu</h3>' +
      '<p class="muted">Kritik dan saranmu sudah kami terima dan akan dibaca untuk perbaikan KhairyPustaka.</p>' +
      '<button class="btn btn-primary" id="pfSelesai" type="button">Tutup</button></div>';
    el.querySelector("#pfSelesai").addEventListener("click", function() { tutup(false); });
    } finally { btnPf.disabled = false; }
  });
}

/* ================= Cari Jurnal Mendalam (Exa) =================
   Menyali struktur demo final demo-cari-jurnal-mendalam, dengan mesin
   live: /api/mendalam/*. Penegakan kuota (3x/hari gabungan, 5x/hari
   bila pernah top up >= Rp5.000) dan kredit Laporan Riset semua di
   server; halaman ini hanya menampilkan keadaan dari /mendalam/info. */
const MDL_MODES = {
  cari: {nama: "Cari Jurnal Mendalam", desc: "Daftar paper yang relevan dengan konteksmu, lengkap dengan cuplikan bagian yang paling berkaitan.", waktu: {kilat: "Sekitar 2 sampai 3 detik", standar: "Sekitar 3 sampai 5 detik", mendalam: "Sekitar 5 sampai 8 detik"}},
  cepat: {nama: "Jawaban Cepat", desc: "Pertanyaanmu dijawab langsung dalam satu paragraf ringkas dan setiap klaimnya ditempeli sitasi.", waktu: {kilat: "Sekitar 3 sampai 4 detik", standar: "Sekitar 5 detik", mendalam: "Sekitar 6 sampai 8 detik"}},
  bedah: {nama: "Bedah Mendalam", desc: "Jawaban terstruktur per subtopik, membandingkan isi beberapa sumber, dengan sitasi di tiap klaim penting.", waktu: {kilat: "Sekitar 4 sampai 8 detik", standar: "Sekitar 8 sampai 12 detik", mendalam: "Sekitar 10 sampai 15 detik"}},
  laporan: {nama: "Laporan Riset", desc: "Laporan utuh berisi latar belakang, perbandingan sumber, temuan per subtopik, dan kesimpulan. Berjalan di latar belakang dan ada pemberitahuan saat selesai.", waktu: {kilat: "Sekitar 1 sampai 2 menit", standar: "Sekitar 1 sampai 3 menit", mendalam: "Sekitar 2 sampai 3 menit"}}
};
const MDL_PV_INCLUDES = {
  cari: ["Daftar paper bernomor sitasi", "Cuplikan bagian paling relevan dari tiap paper", "Label akses tiap sumber, ketahuan sebelum diklik", "Tombol Baca, Ringkas, Buat Sitasi, Simpan, dan Bandingkan"],
  cepat: ["Satu paragraf jawaban bersitasi", "Sitasi bisa diklik dan melompat ke sumbernya", "Daftar sumber plus label akses di bawah jawaban", "Kotak Tanya Lanjut tanpa memotong kuota mendalam"],
  bedah: ["Jawaban terstruktur per subtopik", "Perbandingan isi beberapa sumber", "Sitasi di tiap klaim penting", "Daftar sumber plus label akses di bawah jawaban"],
  laporan: ["Laporan utuh 4 blok: latar belakang, perbandingan sumber, temuan per subtopik, kesimpulan", "Berjalan di latar belakang, ada pemberitahuan saat selesai", "Tersimpan di riwayat member", "Daftar sumber plus label akses"]
};
const MDL_CONTOH_KONTEKS = {
  kesmas: "Saya mahasiswa kesehatan masyarakat sedang menyusun tinjauan pustaka tentang pencegahan risiko kardiovaskular pada dewasa muda. Saya membutuhkan jurnal tentang hubungan pengetahuan, sikap, dan tindakan pencegahan penyakit jantung, terutama studi di Indonesia dalam 5 tahun terakhir.",
  bisnis: "Saya mahasiswa manajemen sedang menyusun skripsi tentang pengaruh pemasaran digital terhadap penjualan UMKM kuliner. Saya membutuhkan jurnal tentang strategi media sosial, kepercayaan konsumen, dan keputusan pembelian, dalam 5 tahun terakhir.",
  informatika: "Saya mahasiswa informatika sedang menyusun tugas akhir tentang klasifikasi risiko penyakit dengan machine learning. Saya membutuhkan paper tentang algoritma klasifikasi, evaluasi model, dan contoh kode atau repositori GitHub yang relevan."
};
const MDL_TEMPLAT = {
  "Latar Belakang": "Saya sedang menyusun latar belakang skripsi tentang pencegahan risiko kardiovaskular pada dewasa muda. Carikan jurnal yang menunjukkan besarnya masalah, faktor risiko utama, dan celah penelitian yang masih terbuka.",
  "Tinjauan Pustaka": "Saya sedang menyusun tinjauan pustaka tentang hubungan pengetahuan, sikap, dan tindakan pencegahan penyakit kardiovaskular. Carikan jurnal yang bisa dibandingkan temuan dan metodenya satu sama lain.",
  "Metode": "Saya sedang menyusun bab metode penelitian kuantitatif tentang pencegahan risiko kardiovaskular. Carikan jurnal dengan desain potong lintang, survei, atau intervensi yang bisa jadi rujukan metode dan instrumen.",
  "Pembahasan": "Saya sedang menyusun pembahasan skripsi tentang pencegahan risiko kardiovaskular pada dewasa muda. Carikan jurnal yang temuannya bisa mendukung atau menantang hasil penelitian saya, lengkap dengan penjelasan mekanismenya.",
  "Research Gap": "Saya sedang mencari research gap tentang pencegahan risiko kardiovaskular pada dewasa muda. Carikan jurnal terbaru dan tunjukkan sisi yang belum banyak diteliti, misalnya populasi, wilayah, atau metode yang jarang dipakai."
};
const MDL_AKSES = {
  full: {label: "Teks Penuh Terbuka", desc: "Seluruh isi bisa dibaca langsung tanpa langganan."},
  abstract: {label: "Abstrak Saja", desc: "Hanya abstrak yang terbuka. Teks penuh di balik langganan penerbit."},
  partial: {label: "Pratinjau Terbatas", desc: "Sebagian halaman bisa dibaca, sisanya terkunci."},
  metadata: {label: "Metadata Saja", desc: "Hanya judul, penulis, dan info terbit yang terbuka. Isi tidak tersedia terbuka."}
};
/* Data contoh HANYA untuk panel preview (persis pola demo: gambaran
   bentuk hasil sebelum memilih mode, bukan hasil pencarian asli). */
const MDL_CONTOH_PAPERS = [
  {id: 1, relevansi: "Paling Relevan", relevansi_kelas: "rel1", alasan_relevansi: "Dasarnya: judulnya memuat frasa persis 'risiko kardiovaskular' dan 'dewasa muda', cocok langsung dengan yang kamu ceritakan. Mesin pencarian makna menempatkannya di peringkat 1 dari 3 hasil.", type: "Jurnal", year: "2024", access: "full", access_label: "Teks Penuh Terbuka", access_note: "Sistem kami berhasil membaca isi sumber ini secara substantial (4.000 karakter terambil).", title: "Pengetahuan, Sikap, dan Tindakan Pencegahan Risiko Kardiovaskular pada Dewasa Muda", author: "Sari, Wulandari, & Pratama", domain: "ejournal.undip.ac.id", doi: "10.32832/pro.v5i4.6973", snippet: "Pengetahuan yang baik berhubungan dengan tindakan pencegahan yang lebih konsisten pada dewasa muda.", text: "Penelitian potong lintang pada 214 responden dewasa muda menunjukkan pengetahuan berhubungan signifikan dengan tindakan pencegahan risiko kardiovaskular. Sikap berperan sebagai penghubung antara pengetahuan dan tindakan. Responden dengan pengetahuan baik memiliki peluang tindakan pencegahan yang lebih konsisten dibanding responden berpengetahuan rendah.", url: "#"},
  {id: 2, relevansi: "Sangat Relevan", relevansi_kelas: "rel2", alasan_relevansi: "Dasarnya: cuplikan isinya membahas 'preventive behaviors' dan 'health literacy' pada dewasa muda, cocok langsung dengan yang kamu ceritakan. Ia berada di peringkat 2 dari 3 hasil.", type: "Jurnal", year: "2023", access: "full", access_label: "Teks Penuh Terbuka", access_note: "Sistem kami berhasil membaca isi sumber ini secara substantial (3.850 karakter terambil).", title: "Cardiovascular Risk Prevention Behaviors Among Young Adults: A Cross-Sectional Study", author: "Nguyen, Lee, & Carter", domain: "pmc.ncbi.nlm.nih.gov", doi: "10.3390/ijerph20021543", snippet: "Preventive behaviors were strongly associated with health literacy and family support.", text: "This cross-sectional study of 386 young adults found that preventive behaviors were strongly associated with health literacy, family support, and perceived susceptibility. Participants who received structured health education reported significantly better dietary and physical activity patterns.", url: "#"},
  {id: 3, relevansi: "Sangat Relevan", relevansi_kelas: "rel2", alasan_relevansi: "Topik utamanya seputar 'perilaku cerdik' dan 'penyakit jantung', sejalur dengan kebutuhanmu. Ia berada di peringkat 3 dari 3 hasil.", type: "Jurnal", year: "2023", access: "abstract", access_label: "Abstrak Saja", access_note: "Yang berhasil terambil hanya seukuran abstrak (1.150 karakter). Teks penuhnya kemungkinan di balik langganan penerbit.", title: "Perilaku CERDIK sebagai Upaya Pencegahan Penyakit Jantung pada Usia Produktif", author: "Rahmawati & Hidayat", domain: "doi.org", doi: "10.3961/jpmph.23.118", snippet: "Program CERDIK berhubungan dengan perbaikan perilaku hidup sehat pada usia produktif.", text: "Abstrak: Penelitian ini menilai hubungan perilaku CERDIK (cek kesehatan berkala, enyahkan asap rokok, rajin aktivitas fisik, diet sehat, istirahat cukup, kelola stres) dengan pencegahan penyakit jantung pada usia produktif. Hasil menunjukkan hubungan yang bermakna pada sebagian besar komponen perilaku.", url: "#"}
];
let MDL = null;

function mdlBaruState() {
  return {info: null, mode: "bedah", start: "konteks",
    saringan: {fokus: "paper", tahunMode: "semua", rentang: 5, dari: 2018, sampai: 2026, bahasa: "semua", jumlah: 10, urutan: "relevan", kedalaman: "standar", domain: "", isi_penuh: true},
    jobId: null, hasil: null, hasilMode: null, kunci: null, queryAktif: "",
    beliIv: null,
    banding: {}, simpan: {}, saringAkses: "semua", saringTahun: "semua", previewMode: null};
}

function mdlSaringanKirim() {
  const s = MDL.saringan;
  let tahun = {mode: "semua"};
  if (s.tahunMode === "relatif") tahun = {mode: "relatif", rentang: s.rentang};
  else if (s.tahunMode === "bebas") tahun = {mode: "bebas", dari: s.dari, sampai: s.sampai};
  return {fokus: s.fokus, jumlah: s.jumlah, tahun: tahun, bahasa: s.bahasa,
    urutan: s.urutan, kedalaman: s.kedalaman, domain: s.domain, isi_penuh: s.isi_penuh};
}

function mdlQueryForm() {
  if (MDL.start === "doi") return ($("#mdlDoi").value || "").trim();
  return ($("#mdlKonteks").value || "").trim();
}

function mdlSetStage(n) {
  [1, 2, 3].forEach(i => {
    const p = $("#mdlStage" + i);
    if (p) p.classList.toggle("hidden", i !== n);
    const pill = $("#mdlPill" + i);
    if (pill) {
      pill.classList.toggle("on", i === n);
      pill.classList.toggle("done", i < n);
      if (i < n) pill.textContent = "✓ " + pill.dataset.label;
      else pill.textContent = pill.dataset.label;
    }
  });
  window.scrollTo({top: 0, behavior: "smooth"});
}

function mdlChipInfo() {
  const i = MDL.info || {};
  const total = i.kuota_ringan_total != null ? i.kuota_ringan_total : 3;
  if (i.admin) return `<span class="mdl-chip amber">Mode admin: kuota dan kredit tidak dipotong</span>` +
    `<span class="mdl-chip">Kuota mode ringan: <b>∞</b></span>` +
    `<span class="mdl-chip">Kredit laporan akun ini: <b>∞</b></span>` +
    `<span class="mdl-chip">Hasil pencarian disimpan di cache <b>7 hari</b></span>`;
  let tierNote;
  if (i.tier >= 2) tierNote = "Kamu di Tier 2 berkat total top up Rp10.000 atau lebih. Limit harianmu sudah yang tertinggi.";
  else if (i.tier >= 1) tierNote = "Limitmu naik permanen berkat top up (Tier 1). Total top up sampai Rp10.000 menaikkannya lagi jadi 10x sehari.";
  else tierNote = "Top up Kredit Laporan sekali saja (Rp5.000) menaikkan limit harianmu permanen jadi 5x sehari, dan total Rp10.000 menaikkannya jadi 10x sehari.";
  return `<span class="mdl-chip">Kuota mode ringan hari ini tersisa <b>${i.kuota_sisa != null ? i.kuota_sisa : 0} kali</b> (limit harianmu <b>${total}x</b>, gabungan Cari Jurnal Mendalam, Jawaban Cepat, dan Bedah Mendalam)</span>` +
    `<span class="mdl-chip">Kredit laporan: <b>${i.kredit_tersedia != null ? i.kredit_tersedia : (i.kredit_saldo != null ? i.kredit_saldo : 0)}</b></span>` +
    `<span class="mdl-chip">Hasil pencarian disimpan di cache <b>7 hari</b></span>` +
    `<div class="mdl-chip amber" style="width:100%">${esc(tierNote)}</div>`;
}

function pageMendalam() {
  if (!AUTH.login) {
    view().innerHTML = `<div class="page-head"><h1>${tic("spark")} Cari Jurnal Mendalam</h1></div>
      <div class="card glass center">
        <p class="muted">Fitur ini khusus member. Masuk dulu untuk memakai Cari Jurnal Mendalam: pencarian jurnal mendalam dengan label akses tiap sumber, jawaban bersitasi, bedah terstruktur, dan Laporan Riset.</p>
        <button class="btn btn-primary" id="mdlLogin">Masuk / Daftar</button>
      </div>`;
    $("#mdlLogin").onclick = () => openAuth();
    return;
  }
  MDL = mdlBaruState();
  const F = MDL.saringan;
  const pill = (grup, val, label) => `<button type="button" class="mdl-pill ${F[grup] === val ? "on" : ""}" data-mgrup="${grup}" data-mval="${esc(String(val))}">${esc(label)}</button>`;
  view().innerHTML = `
  <div class="mdl-wrap">
    <div class="back-row"><button class="btn-back" type="button">${tic("back")} Kembali</button></div>
    <div class="mdl-stages">
      <span class="mdl-stage on" id="mdlPill1" data-label="1. Formulir">1. Formulir</span>
      <span class="mdl-stage" id="mdlPill2" data-label="2. Proses">2. Proses</span>
      <span class="mdl-stage" id="mdlPill3" data-label="3. Hasil">3. Hasil</span>
    </div>

    <div class="mdl-panel" id="mdlStage1">
      <div class="mdl-head"><h1>Cari Jurnal Mendalam</h1><span class="mdl-badge">Fitur Member</span></div>
      <p class="muted" style="margin-top:6px">Ceritakan kebutuhan risetmu. Mesin pencari khusus riset akan mencarikan papernya, membaca isinya, lalu menyusun jawaban bersitasi. Tiap sumber diberi label akses, jadi kamu tahu mana yang bisa langsung dibaca penuh dan mana yang hanya abstrak, sebelum mengklik apa pun. Bedanya dari Cari Jurnal biasa: yang biasa mencari dari katalog jurnal pakai kata kunci, cepat dan bisa dipakai sepuasnya. Yang mendalam membaca ceritamu, memahami konteksnya, lalu membaca isi jurnalnya sekalian, jadi hasilnya lebih dalam, tapi kuotanya dibatasi tiap hari.</p>
      <div class="mdl-chips" id="mdlInfoChips"><span class="mdl-chip">Memuat kuota dan kredit akunmu…</span></div>

      <div class="mdl-example-bar">
        <div><b>Baru pertama kali?</b><div class="muted" style="font-size:13px">Isi semua field dengan contoh lengkap dalam sekali klik, lalu tinggal jalankan.</div></div>
        <button class="mdl-btn primary" id="mdlIsiContoh">Isi Contoh Lengkap</button>
      </div>

      <div class="mdl-section-h">Cara Mulai</div>
      <div class="mdl-tabs">
        <button type="button" class="mdl-tab on" data-mtab="konteks">Ceritakan Konteks</button>
        <button type="button" class="mdl-tab" data-mtab="doi">Tempel Link atau DOI</button>
        <button type="button" class="mdl-tab" data-mtab="templat">Templat Siap Pakai</button>
      </div>
      <div id="mdlStartKonteks">
        <label style="font-weight:700;font-size:14px">Ceritakan kebutuhan risetmu</label>
        <textarea id="mdlKonteks" maxlength="2000" placeholder="Contoh: Saya mahasiswa kesehatan masyarakat sedang menyusun tinjauan pustaka tentang pencegahan risiko kardiovaskular pada dewasa muda. Saya membutuhkan jurnal tentang pengetahuan, sikap, dan tindakan pencegahan penyakit jantung di Indonesia dalam 5 tahun terakhir."></textarea>
        <div class="mdl-char-note" id="mdlCharNote">Jelaskan topik, kebutuhan bab, dan kriteria jurnalmu. Minimal sekitar 30 karakter supaya hasilnya tajam.</div>
        <div class="mdl-mini-chips">
          <button type="button" class="mdl-mini" data-mcontoh="kesmas">Contoh: kesehatan masyarakat</button>
          <button type="button" class="mdl-mini" data-mcontoh="bisnis">Contoh: manajemen dan bisnis</button>
          <button type="button" class="mdl-mini" data-mcontoh="informatika">Contoh: informatika</button>
        </div>
      </div>
      <div id="mdlStartDoi" style="display:none">
        <label style="font-weight:700;font-size:14px">Mulai dari satu paper yang sudah kamu punya</label>
        <input type="text" id="mdlDoi" placeholder="Tempel DOI (10.xxxx/...) atau link artikel jurnal di sini">
        <div class="mdl-char-note">Sistem akan memakai paper itu sebagai titik mulai, lalu mencarikan paper lain yang serupa dan berkaitan.</div>
        <div class="mdl-mini-chips">
          <button type="button" class="mdl-mini" data-mdoi="10.32832/pro.v5i4.6973">Contoh DOI: 10.32832/pro.v5i4.6973</button>
          <button type="button" class="mdl-mini" data-mdoi="https://doi.org/10.3961/jpmph.23.118">Contoh link DOI</button>
        </div>
      </div>
      <div id="mdlStartTemplat" style="display:none">
        <p class="muted" style="font-size:13.5px;margin:0 0 8px">Pilih kebutuhan babmu. Templatnya terisi otomatis ke kotak konteks dan bisa kamu sunting lagi sebelum jalan.</p>
        <div style="display:flex;gap:7px;flex-wrap:wrap">${Object.keys(MDL_TEMPLAT).map(k => `<button type="button" class="mdl-pick" data-mtemplat="${esc(k)}">${esc(k)}</button>`).join("")}</div>
      </div>

      <div class="mdl-section-h" style="margin-top:22px">Mode Kerja</div>
      <div class="mdl-section-hint">Pilih cara mesin mengolah hasil pencarianmu. Makin berat modenya, makin dalam hasilnya dan makin lama prosesnya.</div>
      <div class="mdl-mode-grid" id="mdlModeGrid">
        ${Object.keys(MDL_MODES).map(m => `
        <div class="mdl-mode-cell">
          <button type="button" class="mdl-mode ${MDL.mode === m ? "on" : ""}" data-mmode="${m}">
            <span class="check">✓</span>
            <span class="mname">${MDL_MODES[m].nama}${m === "laporan" ? ' <span class="mdl-badge2" style="background:rgba(251,191,36,.15);color:#fde68a;border-color:rgba(251,191,36,.4)">Berbayar</span>' : ""}</span>
            <span class="mdesc" style="display:block">${MDL_MODES[m].desc}</span>
            <span class="mtime" data-mtime="${m}" style="display:block">${MDL_MODES[m].waktu[F.kedalaman]}</span>
          </button>
          <button type="button" class="mdl-preview-btn" data-mpreview="${m}">Lihat Contoh Hasil</button>
        </div>`).join("")}
      </div>
      <div class="mdl-chip" style="margin-top:10px">Yang berbayar hanya Laporan Riset (1 kredit per laporan selesai). Tiga mode lain memakai kuota harian gabungan.</div>
      <div id="mdlPreviewHost"></div>

      <details class="mdl-details" id="mdlDetailSaring">
        <summary id="mdlSaringRingkasan"><span class="mdl-sum-label">Saringan Lanjutan</span><span class="mdl-sum-chips" id="mdlSaringChips"></span><span class="mdl-sum-chev" aria-hidden="true">▾</span></summary>
        <div class="mdl-fgrid">
          <div><div class="mdl-flabel">Fokus Sumber</div><div class="mdl-pills">
            ${pill("fokus", "semua", "Semua sumber")}${pill("fokus", "paper", "Hanya paper ilmiah")}${pill("fokus", "umum", "Paper plus situs umum")}${pill("fokus", "berita", "Paper plus berita")}${pill("fokus", "perusahaan", "Paper plus data perusahaan")}${pill("fokus", "kode", "Paper plus kode dan GitHub")}
          </div></div>
          <div><div class="mdl-flabel">Rentang Tahun</div><div class="mdl-pills">
            ${pill("tahunMode", "semua", "Semua tahun")}${pill("tahunMode", "relatif", "Relatif")}${pill("tahunMode", "bebas", "Rentang bebas")}
          </div>
            <div class="mdl-pills" id="mdlRentangPills" style="margin-top:6px;${F.tahunMode !== "relatif" ? "display:none" : ""}">
              ${[3, 5, 10].map(n => `<button type="button" class="mdl-pill ${F.rentang === n ? "on" : ""}" data-mrentang="${n}">${n} tahun terakhir</button>`).join("")}
            </div>
            <div id="mdlTahunBebas" style="display:${F.tahunMode === "bebas" ? "flex" : "none"};gap:7px;margin-top:6px;align-items:center">
              <input type="number" id="mdlThnDari" value="${F.dari}" min="1900" max="2100" style="max-width:110px"> <span class="muted">sampai</span>
              <input type="number" id="mdlThnSampai" value="${F.sampai}" min="1900" max="2100" style="max-width:110px">
            </div>
          </div>
          <div><div class="mdl-flabel">Bahasa Sumber</div><div class="mdl-pills">
            ${pill("bahasa", "semua", "Semua bahasa")}${pill("bahasa", "id", "Indonesia")}${pill("bahasa", "en", "Inggris")}
          </div></div>
          <div><div class="mdl-flabel">Jumlah Hasil</div><div class="mdl-pills">
            ${[3, 5, 10, 20, 30].map(n => pill("jumlah", n, "Maks " + n + " hasil")).join("")}
          </div></div>
          <div><div class="mdl-flabel">Urutan Hasil</div><div class="mdl-pills">
            ${pill("urutan", "relevan", "Paling relevan")}${pill("urutan", "baru", "Paling baru")}
          </div></div>
          <div><div class="mdl-flabel">Kedalaman Proses</div><div class="mdl-pills">
            ${pill("kedalaman", "kilat", "Kilat")}${pill("kedalaman", "standar", "Standar")}${pill("kedalaman", "mendalam", "Mendalam")}
          </div></div>
          <div><div class="mdl-flabel">Batas Domain (opsional)</div>
            <input type="text" id="mdlDomain" placeholder="mis. kemkes.go.id" value="${esc(F.domain)}">
            <div class="mdl-mini-chips">
              <button type="button" class="mdl-mini" data-mdomain="kemkes.go.id">kemkes.go.id</button>
              <button type="button" class="mdl-mini" data-mdomain="github.com">github.com</button>
              <button type="button" class="mdl-mini" data-mdomain="telkom.co.id">telkom.co.id</button>
            </div>
          </div>
          <div><div class="mdl-flabel">Ambil Isi Penuh</div>
            <label class="mdl-switch"><input type="checkbox" id="mdlIsiPenuh" ${F.isi_penuh ? "checked" : ""}> Ambil isi penuh tiap sumber (lebih lambat, label akses lebih akurat)</label>
          </div>
        </div>
      </details>

      <div id="mdlBeliHost"></div>
      <div id="mdlFormError"></div>
      <div class="mdl-estimate" id="mdlEstimate"></div>
      <button class="mdl-run" id="mdlRun">Jalankan Pencarian Mendalam</button>
    </div>

    <div class="mdl-panel hidden" id="mdlStage2" style="margin-top:16px">
      <h2>Memproses pencarianmu…</h2>
      <p class="muted" id="mdlTahapTeks" style="font-size:14px">Menyiapkan pencarian.</p>
      <ul class="mdl-steps">
        <li data-step="1">Menyusun kata kunci dari konteks</li>
        <li data-step="2">Mencari sumber</li>
        <li data-step="3">Membaca isi sumber</li>
        <li data-step="4">Menyusun jawaban</li>
      </ul>
      <p class="muted" id="mdlProcNote" style="font-size:13px"></p>
    </div>

    <div class="hidden" id="mdlStage3" style="margin-top:16px">
      <div id="mdlHasilHost"></div>
      <div style="display:flex;gap:9px;flex-wrap:wrap;margin-top:16px">
        <button class="mdl-btn" id="mdlUbah">Ubah Pencarian (kembali ke formulir, isian tetap)</button>
        <button class="mdl-btn primary" id="mdlBaru">Pencarian Baru</button>
      </div>
    </div>

    <div class="mdl-history" id="mdlRiwayatHost"><h2 style="font-size:16px">Riwayat Pencarian Mendalam</h2><div class="muted" style="font-size:13.5px">Memuat riwayat…</div></div>

    <div style="margin-top:26px">
      <h2>Kemampuan</h2>
      <p class="muted" style="font-size:13.5px">Mesin di balik fitur ini dibangun khusus untuk riset, jadi cara kerjanya beda dari pencarian biasa.</p>
      <div class="mdl-cap-grid">
        ${[["PENCARIAN NEURAL", "Neural Search", "Memahami maksud pertanyaanmu, bukan sekadar mencocokkan kata, jadi paper yang nyambung secara makna ikut ketemu."], ["KATEGORI SUMBER", "Kategori Paper Ilmiah", "Pencarian bisa dikunci ke kategori paper riset, berita, data perusahaan, sampai repositori kode."], ["ISI PENUH", "Contents / Isi Penuh", "Sistem mengambil dan membaca isi sumber, bukan cuma judul dan abstraknya."], ["PENCARIAN MENDALAM", "Deep Search", "Pertanyaan dipecah menjadi beberapa subpertanyaan terarah, lalu hasilnya dibandingkan."], ["JAWABAN BERSITASI", "Answer Bersitasi", "Pertanyaan dijawab langsung dalam satu paragraf dan setiap klaimnya ditempeli sitasi."], ["SKEMA TERSTRUKTUR", "Output Terstruktur", "Bedah Mendalam memakai skema keluaran resmi: ringkasan, faktor penyebab, dampak, rekomendasi."], ["AGEN RISET", "Agent / Laporan Riset", "Agen riset otonom yang merencanakan, mencari berlapis, membaca, lalu menulis laporan utuh."], ["PAPER SERUPA", "Find Similar", "Dari satu paper atau DOI, sistem mencarikan paper lain yang serupa."], ["SARING DOMAIN", "Saring Domain dan Tanggal", "Hasil bisa dikunci ke domain tertentu seperti jurnal kampus atau situs resmi, dan ke rentang tahun."], ["DASAR SITASI", "Grounding Sitasi", "Klaim di jawaban Bedah Mendalam ditempeli bukti dari sumbernya lewat grounding per bidang."]].map(c => `<div class="mdl-cap"><span class="mdl-cap-tag">${c[0]}</span><b>${c[1]}</b>${c[2]}</div>`).join("")}
      </div>
    </div>

    <div style="margin-top:26px">
      <h2>Yang Diperlukan</h2>
      <div class="mdl-need-grid">
        <div class="mdl-need"><b>Akun member KhairyPustaka</b>Fitur ini khusus member. Tamu diarahkan masuk atau daftar dulu, gratis.</div>
        <div class="mdl-need"><b>Kuota harian untuk mode ringan</b>Cari Jurnal Mendalam, Jawaban Cepat, dan Bedah Mendalam berbagi satu kuota gabungan per hari, pulih pukul 00.00 WITA. Hasil yang tersimpan di cache tidak memotong kuota.</div>
        <div class="mdl-need"><b>Kredit khusus untuk Laporan Riset</b>Laporan Riset memakai kredit laporan: Rp5.000 dapat 2 kredit dan 1 kredit terpotong per laporan yang selesai. Kredit tidak kedaluwarsa.</div>
      </div>
    </div>

    <div style="margin-top:26px">
      <h2>Di Balik Layar</h2>
      <div class="mdl-cap-grid" style="grid-template-columns:1fr 1fr">
        ${[["URUTAN PROSES", "Pencarian otomatis berantai", "Konteksmu disusun menjadi kata kunci, dicari memakai mesin pencari riset, isi sumber dibaca, lalu jawaban disusun. Semua berjalan otomatis."], ["BIAYA MESIN", "Biaya mesin dijaga", "Ada plafon belanja harian untuk mode ringan dan hasil pencarian disimpan di cache 7 hari, jadi pertanyaan yang sama tidak dicari dua kali."], ["KENAPA PERLU WAKTU", "Hasil dicari saat itu juga", "Hasil tidak diambil dari stok lama. Laporan Riset paling lama karena agennya mencari berlapis dan menulis laporan utuh."], ["PENYIMPANAN", "Hasilmu tersimpan di akunmu", "Laporan selesai dan riwayat pencarian tersimpan di akunmu, bisa dibuka lagi kapan pun."]].map(c => `<div class="mdl-cap"><span class="mdl-cap-tag">${c[0]}</span><b>${c[1]}</b>${c[2]}</div>`).join("")}
      </div>
    </div>
  </div>`;
  mdlWireForm();
  mdlMuatInfo();
  mdlMuatRiwayat();
}

function mdlTab(nama) {
  MDL.start = nama;
  $$(".mdl-tab").forEach(t => t.classList.toggle("on", t.dataset.mtab === nama));
  $("#mdlStartKonteks").style.display = nama === "konteks" ? "" : "none";
  $("#mdlStartDoi").style.display = nama === "doi" ? "" : "none";
  $("#mdlStartTemplat").style.display = nama === "templat" ? "" : "none";
  mdlUpdateEstimate();
}

function mdlFokusLabel(k) {
  return {semua: "Semua sumber", paper: "Hanya paper ilmiah", umum: "Paper plus situs umum", berita: "Paper plus berita", perusahaan: "Paper plus data perusahaan", kode: "Paper plus kode dan GitHub"}[k] || k;
}

function mdlRingkasanSaringan() {
  const F = MDL.saringan;
  let tahun = "Semua tahun";
  if (F.tahunMode === "relatif") tahun = F.rentang + " tahun terakhir";
  else if (F.tahunMode === "bebas") tahun = F.dari + " sampai " + F.sampai;
  const bahasa = {semua: "Semua bahasa", id: "Indonesia", en: "Inggris"}[F.bahasa];
  const chips = $("#mdlSaringChips");
  if (chips) {
    chips.innerHTML = [mdlFokusLabel(F.fokus), tahun, bahasa, "Maks " + F.jumlah + " hasil"]
      .map(t => '<span class="mdl-sum-chip">' + esc(t) + "</span>").join("");
  } else {
    const sum = $("#mdlSaringRingkasan");
    if (sum) sum.textContent = "Saringan Lanjutan · " + mdlFokusLabel(F.fokus) + " · " + tahun + " · " + bahasa + " · Maks " + F.jumlah + " hasil";
  }
}

function mdlRefreshPills() {
  const F = MDL.saringan;
  $$(".mdl-pill[data-mgrup]").forEach(p => {
    const g = p.dataset.mgrup;
    let on = String(F[g]) === p.dataset.mval;
    p.classList.toggle("on", on);
  });
  $$("[data-mrentang]").forEach(p => p.classList.toggle("on", F.rentang === +p.dataset.mrentang));
  const rp = $("#mdlRentangPills"); if (rp) rp.style.display = F.tahunMode === "relatif" ? "" : "none";
  const tb = $("#mdlTahunBebas"); if (tb) tb.style.display = F.tahunMode === "bebas" ? "flex" : "none";
  $$("[data-mtime]").forEach(el => { el.textContent = MDL_MODES[el.dataset.mtime].waktu[F.kedalaman]; });
  mdlRingkasanSaringan();
}

function mdlCukup() {
  const i = MDL.info || {};
  if (i.admin) return true;
  if (MDL.mode === "laporan") return (i.kredit_tersedia || 0) >= 1;
  return (i.kuota_sisa || 0) >= 1;
}

function mdlUpdateEstimate() {
  const host = $("#mdlEstimate"); if (!host) return;
  const i = MDL.info || {};
  const mode = MDL_MODES[MDL.mode];
  let potong, sisa;
  if (MDL.mode === "laporan") {
    potong = "1 kredit laporan";
    sisa = i.admin ? "mode admin, kredit tidak dipotong" : ("sisa kredit setelah jalan: " + Math.max(0, (i.kredit_tersedia || 0) - 1));
  } else {
    potong = "1 kuota mode ringan";
    sisa = i.admin ? "mode admin, kuota tidak dipotong" : ("sisa kuota setelah jalan: " + Math.max(0, (i.kuota_sisa || 0) - 1) + " dari " + (i.kuota_ringan_total || 3));
  }
  host.innerHTML = `<span>Perkiraan waktu: <b>${mode.waktu[MDL.saringan.kedalaman]}</b></span><span>·</span><span>Potongan: <b>${potong}</b></span><span>·</span><span>${sisa}</span>` +
    (mdlCukup() ? "" : `<span class="mdl-chip amber">${MDL.mode === "laporan" ? "Kredit laporanmu habis. Beli kredit dulu lewat kartu Beli Kredit Laporan di atas." : "Kuota mode ringan hari ini habis. Hasil dari cache tetap bisa dibuka dari riwayat."}</span>`);
  const run = $("#mdlRun"); if (run) run.disabled = !mdlCukup();
}

async function mdlMuatInfo() {
  const r = await apiJSON("/mendalam/info");
  if (r.ok) {
    MDL.info = r.data;
    const host = $("#mdlInfoChips"); if (host) host.innerHTML = mdlChipInfo();
    mdlRenderBeli();
    mdlUpdateEstimate();
  }
}

function mdlRenderBeli() {
  const host = $("#mdlBeliHost"); if (!host) return;
  const i = MDL.info || {};
  if (!i.pay_enabled) {
    host.innerHTML = MDL.mode === "laporan" || (i.kredit_tersedia || 0) < 1
      ? `<div class="mdl-buy"><b>Beli Kredit Laporan</b><div class="muted" style="font-size:13.5px;margin-top:4px">Pembayaran online untuk kredit laporan belum aktif di situs saat ini. Kredit bisa diberikan manual oleh admin. Kamu punya <b>${i.admin ? "∞" : (i.kredit_tersedia || 0)} kredit</b> sekarang.</div></div>` : "";
    return;
  }
  host.innerHTML = `
  <div class="mdl-buy">
    <div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:flex-start">
      <div><b style="font-size:15.5px">Beli Kredit Laporan</b>
      <div class="muted" style="font-size:13.5px;margin-top:3px">Rp5.000 dapat <b>2 kredit laporan</b> (1 kredit = 1 Laporan Riset yang selesai). Kredit tidak kedaluwarsa. Total top up-mu juga menaikkan limit harian secara permanen: Tier 1 di total Rp5.000 (mode ringan 5x sehari, kuota AI 20x sehari) dan Tier 2 di total Rp10.000 (mode ringan 10x sehari, kuota AI 25x sehari).</div></div>
      <span class="mdl-chip">Kreditmu sekarang: <b>${i.admin ? "∞" : (i.kredit_tersedia || 0)}</b></span>
    </div>
    <div id="mdlBeliArea" style="margin-top:12px"><button class="mdl-btn primary" id="mdlBeliBtn">Beli 2 Kredit (Rp5.000, QRIS)</button></div>
  </div>`;
  $("#mdlBeliBtn").onclick = mdlBeliMulai;
}

async function mdlBeliMulai() {
  const area = $("#mdlBeliArea");
  area.innerHTML = `<span class="muted">Menyiapkan pembayaran…</span>`;
  const r = await apiPOST("/mendalam/beli/create", {});
  if (!r.ok) {
    area.innerHTML = `<span class="muted">${esc((r.data && (r.data.pesan || r.data.error)) || "Pembayaran belum bisa disiapkan.")}</span> <button class="mdl-btn" id="mdlBeliLagi">Coba Lagi</button>`;
    const bl = $("#mdlBeliLagi");
    if (bl) bl.onclick = () => mdlRenderBeli();
    return;
  }
  const d = r.data;
  const rp = n => "Rp" + Number(n || 0).toLocaleString("id-ID");
  area.innerHTML = `
    <div style="display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start">
      ${d.qr_b64 ? `<img class="topup-qr" alt="Kode QR pembayaran" src="data:image/png;base64,${d.qr_b64}">` : ""}
      <div style="flex:1;min-width:220px">
        <div style="font-weight:800;font-size:15px">Pindai QRIS ini untuk membayar ${rp(d.total)}</div>
        <div class="muted" style="font-size:13px;margin-top:3px">Referensi: ${esc(d.ref_id)}</div>
        <div class="muted" style="font-size:13px;margin:4px 0 10px">Bisa dari aplikasi bank atau e-wallet apa pun (GoPay, OVO, DANA, ShopeePay, m-banking). Kredit masuk otomatis begitu pembayaran terdeteksi, biasanya dalam hitungan detik. Kode berlaku sekitar 1 jam.</div>
        <div class="mdl-chip" id="mdlBeliStatus">Menunggu pembayaran…</div>
        <div style="margin-top:10px"><button class="mdl-btn" id="mdlBeliBatal">Batalkan</button></div>
      </div>
    </div>`;
  const bb = $("#mdlBeliBatal");
  if (bb) bb.onclick = () => {
    if (MDL.beliIv) { clearInterval(MDL.beliIv); MDL.beliIv = null; }
    mdlRenderBeli();
  };
  let tries = 0;
  const iv = setInterval(async () => {
    tries++;
    const st = $("#mdlBeliStatus");
    if (!st || tries > 120) { clearInterval(iv); MDL.beliIv = null; return; }
    const s = await apiJSON("/mendalam/beli/status/" + encodeURIComponent(d.ref_id));
    if (s.ok && s.data.paid) {
      clearInterval(iv);
      toast("Pembayaran diterima. Kredit laporanmu sekarang " + ((MDL.info && MDL.info.admin) ? "∞" : s.data.saldo_kredit) + ".");
      await mdlMuatInfo();
    } else if (s.ok && s.data.state === "expired") {
      clearInterval(iv);
      st.textContent = "Kode pembayaran kedaluwarsa. Buat yang baru ya.";
    } else if (s.ok && s.data.state === "failed") {
      clearInterval(iv);
      st.textContent = "Pembayaran gagal atau dibatalkan. Silakan coba lagi.";
    }
  }, 4000);
  MDL.beliIv = iv;
  regTimer(iv);
}

function mdlJawabContoh(mode) {
  const p1 = MDL_CONTOH_PAPERS[0], p3 = MDL_CONTOH_PAPERS[2];
  if (mode === "cari") return `<p style="margin:0 0 8px"><b>Ringkasan Hasil Pencarian</b></p><p style="margin:0">Ditemukan 3 paper yang paling berkaitan. Paper [${p1.id}] dari ${esc(p1.domain)} (${p1.year}) menemukan pengetahuan berhubungan signifikan dengan tindakan pencegahan pada dewasa muda. Paper [${p3.id}] membahas program CERDIK pada usia produktif tetapi aksesnya hanya abstrak. Rincian tiap paper ada di daftar sumber di bawah.</p>`;
  if (mode === "cepat") return `<p style="margin:0">Pengetahuan, sikap, dan tindakan pencegahan saling berkaitan pada dewasa muda: pengetahuan yang baik berhubungan dengan tindakan pencegahan yang lebih konsisten <span style="color:var(--mteal);font-weight:700">[1]</span>, dan program perilaku hidup sehat seperti CERDIK berhubungan dengan perbaikan perilaku pencegahan pada usia produktif <span style="color:var(--mteal);font-weight:700">[3]</span>. Bukti terkuat berasal dari studi potong lintang di Indonesia dan studi literasi kesehatan pada dewasa muda.</p>`;
  if (mode === "bedah") return `<p style="margin:0 0 6px"><b>Ringkasan.</b> Bukti yang terkumpul menunjukkan pencegahan risiko kardiovaskular pada dewasa muda ditopang tiga lapis: pengetahuan, sikap, dan dukungan perilaku.</p><p style="margin:0 0 4px"><b>Faktor dan Penyebab:</b> pengetahuan rendah, literasi kesehatan terbatas, dan persepsi risiko yang lemah.</p><p style="margin:0 0 4px"><b>Dampak:</b> perilaku pencegahan yang tidak konsisten dan risiko yang baru terdeteksi saat sudah lanjut.</p><p style="margin:0"><b>Rekomendasi:</b> edukasi terstruktur, penguatan sikap lewat konseling singkat, dan program perilaku hidup sehat di kampus dan tempat kerja.</p>`;
  return `<p style="margin:0 0 6px"><b>Laporan Riset: Pencegahan Risiko Kardiovaskular pada Dewasa Muda</b></p><p style="margin:0 0 6px"><b>Latar belakang.</b> Penyakit kardiovaskular mulai mengincar usia produktif, sementara perilaku pencegahannya belum konsisten.</p><p style="margin:0 0 6px"><b>Perbandingan sumber.</b> Studi Indonesia menekankan pengetahuan dan sikap, studi internasional menambah literasi kesehatan dan dukungan keluarga.</p><p style="margin:0 0 6px"><b>Temuan per subtopik.</b> Pengetahuan, sikap, program CERDIK, dan literasi kesehatan dibahas satu per satu dengan sitasi.</p><p style="margin:0"><b>Kesimpulan.</b> Intervensi paling menjanjikan menggabungkan edukasi, penguatan sikap, dan dukungan lingkungan.</p>`;
}

function mdlTogglePreview(mode) {
  const host = $("#mdlPreviewHost"); if (!host) return;
  if (MDL.previewMode === mode) { host.innerHTML = ""; MDL.previewMode = null; return; }
  MDL.previewMode = mode;
  const info = MDL_MODES[mode];
  const inc = MDL_PV_INCLUDES[mode].map(x => `<li>${esc(x)}</li>`).join("");
  const papers = (mode === "cari" ? MDL_CONTOH_PAPERS : [MDL_CONTOH_PAPERS[0], MDL_CONTOH_PAPERS[2]])
    .map(p => mdlPaperHTML(p, true)).join("");
  const potong = mode === "laporan" ? "Potongan: 1 kredit laporan" : "Potongan: 1 kuota mode ringan";
  host.innerHTML = `
  <div class="mdl-preview">
    <div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:center">
      <b style="font-size:15.5px">Contoh Hasil: ${info.nama}</b>
      <span class="mdl-chip">${info.waktu[MDL.saringan.kedalaman]} · ${potong}</span>
    </div>
    <div class="mdl-flabel" style="margin-top:12px">Yang ditampilkan mode ini:</div>
    <ul class="mdl-pv-inc">${inc}</ul>
    <div class="mdl-answer" style="margin-top:0">${mdlJawabContoh(mode)}</div>
    <div class="mdl-flabel" style="margin-top:14px">Sumber pada contoh ini</div>
    ${papers}
    <p class="muted" style="font-size:12.5px;margin-top:12px">Ini contoh tampilan memakai data contoh, bukan hasil pencarian asli. Melihat contoh tidak memotong kuota atau kredit apa pun.</p>
    <div style="display:flex;gap:9px;flex-wrap:wrap;margin-top:10px">
      <button class="mdl-btn" id="mdlPvTutup">Tutup Contoh</button>
      <button class="mdl-btn primary" id="mdlPvPakai">Pakai Mode Ini</button>
    </div>
  </div>`;
  $("#mdlPvTutup").onclick = () => { host.innerHTML = ""; MDL.previewMode = null; };
  $("#mdlPvPakai").onclick = () => { mdlPilihMode(mode); host.innerHTML = ""; MDL.previewMode = null; };
  mdlWirePapers(host);
}

function mdlPilihMode(mode) {
  MDL.mode = mode;
  $$(".mdl-mode").forEach(b => b.classList.toggle("on", b.dataset.mmode === mode));
  mdlRenderBeli();
  mdlUpdateEstimate();
}

function mdlWireForm() {
  $$(".mdl-tab").forEach(t => t.onclick = () => mdlTab(t.dataset.mtab));
  $$("[data-mcontoh]").forEach(b => b.onclick = () => {
    $("#mdlKonteks").value = MDL_CONTOH_KONTEKS[b.dataset.mcontoh];
    mdlCekKonteks(); mdlUpdateEstimate();
  });
  $$("[data-mdoi]").forEach(b => b.onclick = () => { $("#mdlDoi").value = b.dataset.mdoi; mdlUpdateEstimate(); });
  $$("[data-mtemplat]").forEach(b => b.onclick = () => {
    mdlTab("konteks");
    $("#mdlKonteks").value = MDL_TEMPLAT[b.dataset.mtemplat];
    mdlCekKonteks(); mdlUpdateEstimate();
  });
  $$(".mdl-mode").forEach(b => b.onclick = () => mdlPilihMode(b.dataset.mmode));
  $$("[data-mpreview]").forEach(b => b.onclick = () => mdlTogglePreview(b.dataset.mpreview));
  $$(".mdl-pill[data-mgrup]").forEach(p => p.onclick = () => {
    const g = p.dataset.mgrup;
    let v = p.dataset.mval;
    if (g === "jumlah") v = +v;
    MDL.saringan[g] = v;
    mdlRefreshPills(); mdlUpdateEstimate();
  });
  $$("[data-mrentang]").forEach(p => p.onclick = () => { MDL.saringan.rentang = +p.dataset.mrentang; mdlRefreshPills(); mdlUpdateEstimate(); });
  $$("[data-mdomain]").forEach(b => b.onclick = () => { $("#mdlDomain").value = b.dataset.mdomain; MDL.saringan.domain = b.dataset.mdomain; mdlUpdateEstimate(); });
  $("#mdlDomain").oninput = e => { MDL.saringan.domain = e.target.value.trim(); mdlUpdateEstimate(); };
  $("#mdlIsiPenuh").onchange = e => { MDL.saringan.isi_penuh = e.target.checked; mdlUpdateEstimate(); };
  $("#mdlThnDari").onchange = e => { MDL.saringan.dari = +e.target.value || 1900; mdlRingkasanSaringan(); };
  $("#mdlThnSampai").onchange = e => { MDL.saringan.sampai = +e.target.value || 2100; mdlRingkasanSaringan(); };
  $("#mdlKonteks").oninput = () => { mdlCekKonteks(); };
  $("#mdlDoi").oninput = () => mdlUpdateEstimate();
  $("#mdlIsiContoh").onclick = () => {
    mdlTab("konteks");
    $("#mdlKonteks").value = MDL_CONTOH_KONTEKS.kesmas;
    mdlPilihMode("bedah");
    MDL.saringan.fokus = "paper"; MDL.saringan.tahunMode = "relatif"; MDL.saringan.rentang = 5;
    MDL.saringan.jumlah = 5; MDL.saringan.urutan = "relevan"; MDL.saringan.kedalaman = "standar";
    MDL.saringan.domain = ""; $("#mdlDomain").value = "";
    MDL.saringan.isi_penuh = true; $("#mdlIsiPenuh").checked = true;
    const d = $("#mdlDetailSaring"); if (d) d.open = true;
    mdlRefreshPills(); mdlCekKonteks(); mdlUpdateEstimate();
    toast("Contoh lengkap terisi. Tinggal klik Jalankan Pencarian Mendalam.");
  };
  $("#mdlRun").onclick = mdlJalankan;
  $("#mdlUbah").onclick = () => mdlSetStage(1);
  $("#mdlBaru").onclick = () => { location.hash = "#/mendalam"; pageMendalam(); };
  mdlRefreshPills();
  mdlUpdateEstimate();
}

function mdlCekKonteks() {
  const v = ($("#mdlKonteks").value || "");
  const note = $("#mdlCharNote"); if (!note) return;
  if (v.trim().length > 0 && v.trim().length < 30) {
    note.innerHTML = `<span style="color:var(--mrose)">Konteksmu baru ${v.trim().length} karakter. Tambahkan sedikit lagi, minimal sekitar 30 karakter, supaya hasil pencariannya tajam.</span>`;
  } else {
    note.textContent = "Jelaskan topik, kebutuhan bab, dan kriteria jurnalmu. Minimal sekitar 30 karakter supaya hasilnya tajam.";
  }
}

async function mdlMuatRiwayat() {
  const host = $("#mdlRiwayatHost"); if (!host) return;
  const r = await apiJSON("/mendalam/riwayat");
  if (!r.ok || !r.data.riwayat || !r.data.riwayat.length) {
    host.innerHTML = `<h2 style="font-size:16px">Riwayat Pencarian Mendalam</h2><div class="muted" style="font-size:13.5px">Belum ada pencarian mendalam. Hasil pencarianmu nanti tersimpan di sini dan bisa dibuka lagi tanpa mencari ulang.</div>`;
    return;
  }
  host.innerHTML = `<h2 style="font-size:16px">Riwayat Pencarian Mendalam</h2>` + r.data.riwayat.map(it => `
    <button type="button" class="mdl-hist-item" data-mhist="${esc(it.kunci)}">
      <span class="mdl-hist-top"><span>${esc(it.mode_nama || (MDL_MODES[it.mode] || {}).nama || "")}</span><span>${esc(it.waktu_label || "")}</span></span>
      <span class="mdl-hist-q" style="display:block">${esc(it.query)}</span>
      <span class="mdl-hist-meta">${it.mode === "laporan" ? "Laporan tersimpan. Klik untuk memuat lagi." : ((it.jumlah_sumber != null ? it.jumlah_sumber : (it.jumlah != null ? it.jumlah : 0)) + " sumber ditemukan. Klik untuk memuat lagi.")}</span>
    </button>`).join("");
  $$("[data-mhist]").forEach(b => b.onclick = () => mdlBukaHasil(b.dataset.mhist));
}

async function mdlBukaHasil(kunci) {
  const r = await apiJSON("/mendalam/hasil?kunci=" + encodeURIComponent(kunci));
  if (!r.ok) {
    toast("Hasil tersimpan ini sudah lewat masa cache 7 hari. Jalankan pencarian yang sama lagi ya.");
    return;
  }
  MDL.hasil = r.data.hasil; MDL.hasilMode = r.data.mode; MDL.kunci = kunci;
  if (r.data.dari_cache && MDL.hasil) MDL.hasil.dari_cache = true;
  MDL.queryAktif = r.data.query || "";
  MDL.banding = {}; MDL.simpan = {}; MDL.saringAkses = "semua"; MDL.saringTahun = "semua";
  mdlRenderHasil();
  mdlSetStage(3);
}

function mdlPaperTeksBaca(p) {
  const t = (p.text || "").trim();
  if (p.access === "full") return t || p.snippet || "";
  if (p.access === "abstract") return (t || p.snippet || "") + "\n\nCatatan: yang terbuka dari sumber ini hanya abstraknya. Teks penuhnya kemungkinan di balik langganan penerbit, jadi wajar bila isi di sini terasa ringkas.";
  if (p.access === "partial") return (t || p.snippet || "") + "\n\nCatatan: hanya sebagian isi yang berhasil dibaca. Sisanya kemungkinan terkunci di situs penerbitnya.";
  return "";
}

function mdlLabelBaca(p) {
  if (p.access === "full") return MDL.saringan && MDL.saringan.isi_penuh ? "Baca Isi Penuh" : "Baca Abstrak";
  if (p.access === "abstract") return "Baca Abstrak";
  if (p.access === "partial") return "Lihat Pratinjau";
  return "Lihat Metadata";
}

function mdlPaperHTML(p, contoh) {
  const acc = MDL_AKSES[p.access] ? MDL_AKSES[p.access].label : p.access_label;
  return `
  <article class="mdl-paper" id="mdlPaper${p.id}">
    <div class="mdl-badges">
      ${p.relevansi ? `<span class="mdl-badge2 ${esc(p.relevansi_kelas || "")}">${esc(p.relevansi)}</span>` : ""}
      <span class="mdl-badge2">${esc(p.type || "Jurnal")}</span>
      <span class="mdl-badge2">${esc(p.year || "Tahun tidak terbaca")}</span>
      <span class="mdl-badge2 acc-${esc(p.access)}">${esc(acc)}</span>
      <span class="mdl-pnum">[${p.id}]</span>
    </div>
    <div class="mdl-ptitle">${contoh ? esc(p.title) : `<a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.title)}</a>`}</div>
    <div class="mdl-pmeta">${esc(p.author || "")}</div>
    ${p.alasan_relevansi ? `<div class="mdl-rel-line"><b>${esc(p.relevansi || "Relevan")}.</b> ${esc(p.alasan_relevansi)}</div>` : ""}
    <div class="mdl-access-line"><b>Akses: ${esc(acc)}.</b> ${esc(p.access_note || "")}</div>
    ${p.snippet ? `<div class="mdl-highlight"><span class="hl-lab">BAGIAN PALING RELEVAN</span><br>${esc(p.snippet)}</div>` : ""}
    <div class="mdl-pmeta" style="margin-top:9px">Sumber: ${esc(p.domain || "")} · DOI: ${p.doi ? esc(p.doi) : "tidak ada (sumber nonjurnal)"}</div>
    <div class="mdl-pactions">
      ${contoh ? "" : `<a class="mdl-pact" href="${esc(p.url)}" target="_blank" rel="noopener">Buka Sumber Asli</a>`}
      <button type="button" class="mdl-pact" data-mact="baca" data-mid="${p.id}">${mdlLabelBaca(p)}</button>
      <button type="button" class="mdl-pact" data-mact="ringkas" data-mid="${p.id}">Ringkas</button>
      <button type="button" class="mdl-pact" data-mact="sitasi" data-mid="${p.id}">Buat Sitasi</button>
      <button type="button" class="mdl-pact ${MDL && MDL.simpan[p.id] ? "on" : ""}" data-mact="simpan" data-mid="${p.id}">${MDL && MDL.simpan[p.id] ? "Tersimpan ✓" : "Simpan"}</button>
      <button type="button" class="mdl-pact ${MDL && MDL.banding[p.id] ? "on" : ""}" data-mact="banding" data-mid="${p.id}">${MDL && MDL.banding[p.id] ? "Dibandingkan ✓" : "Tandai Bandingkan"}</button>
    </div>
    <div data-mexp="${p.id}"></div>
  </article>`;
}

function mdlSitasi(p, gaya) {
  const auth = (p.author || "Anonim").replace(/&/g, "and");
  const parts = auth.split(/,\s*| and /).map(s => s.trim()).filter(Boolean);
  const last = parts[0] || "Anonim", first = parts[1] || "";
  const ini = first ? first.split(/\s+/).map(w => w[0] + ".").join(" ") : "";
  const yr = p.year || "n.d.", ttl = p.title || "", doi = p.doi || "", url = p.url || "";
  const doiUrl = doi ? "https://doi.org/" + doi : url;
  switch (gaya) {
    case "apa": return `${last}, ${ini} (${yr}). ${ttl}. ${doiUrl}`;
    case "mla": return `${last}, ${first}. "${ttl}." ${yr}. ${doiUrl}`;
    case "chicago": return `${last}, ${first}. ${yr}. "${ttl}." ${doiUrl}`;
    case "ieee": return `${ini} ${last}, "${ttl}," ${yr}. [Online]. Tersedia: ${doiUrl}`;
    case "vancouver": return `${last} ${ini}. ${ttl}. ${yr}. Tersedia dari: ${doiUrl}`;
    default: return `${last}, ${ini} (${yr}) '${ttl}', tersedia di: ${doiUrl} (diakses dari sumber asli).`;
  }
}

function mdlWirePapers(root) {
  (root || document).querySelectorAll("[data-mact]").forEach(btn => {
    btn.onclick = () => {
      const id = +btn.dataset.mid, act = btn.dataset.mact;
      const papers = MDL && MDL.hasil ? MDL.hasil.papers : MDL_CONTOH_PAPERS;
      const p = papers.find(x => x.id === id) || MDL_CONTOH_PAPERS.find(x => x.id === id);
      if (!p) return;
      const exp = btn.closest(".mdl-paper").querySelector("[data-mexp]");
      if (act === "simpan") {
        MDL.simpan[id] = !MDL.simpan[id];
        btn.classList.toggle("on", !!MDL.simpan[id]);
        btn.textContent = MDL.simpan[id] ? "Tersimpan ✓" : "Simpan";
        const c = $("#mdlSimpanCount");
        if (c) c.textContent = "Paper tersimpan: " + Object.keys(MDL.simpan).filter(k => MDL.simpan[k]).length;
        return;
      }
      if (act === "banding") {
        MDL.banding[id] = !MDL.banding[id];
        btn.classList.toggle("on", !!MDL.banding[id]);
        btn.textContent = MDL.banding[id] ? "Dibandingkan ✓" : "Tandai Bandingkan";
        if (MDL.hasil) mdlRenderBanding();
        return;
      }
      if (exp.dataset.open === act) { exp.innerHTML = ""; exp.dataset.open = ""; return; }
      exp.dataset.open = act;
      if (act === "baca") {
        if (p.access === "metadata") {
          exp.innerHTML = `<div class="mdl-expand">Isi paper ini tidak tersedia terbuka. Yang bisa dipastikan dari sumbernya hanya judul, penulis, dan info terbit berikut ini.\n\nJudul: ${esc(p.title)}\nPenulis: ${esc(p.author || "")}\nTahun: ${esc(p.year || "")}\nSumber: ${esc(p.domain || "")}${p.doi ? "\nDOI: " + esc(p.doi) : ""}</div><div class="mdl-note-amber">Untuk membaca isinya, buka sumber aslinya dan manfaatkan akses perpustakaan kampusmu bila ada.</div>`;
        } else {
          exp.innerHTML = `<div class="mdl-expand">${esc(mdlPaperTeksBaca(p))}</div>`;
        }
      } else if (act === "ringkas") {
        exp.innerHTML = `<div class="mdl-expand"><b>Ringkasan singkat.</b> ${esc(p.snippet || p.text || "Cuplikan relevan tidak tersedia dari sumber ini.")}</div>`;
      } else if (act === "sitasi") {
        exp.innerHTML = `<div class="mdl-expand"><div class="mdl-pills" style="margin-bottom:9px">${[["apa", "APA 7"], ["mla", "MLA"], ["chicago", "Chicago"], ["ieee", "IEEE"], ["vancouver", "Vancouver"], ["harvard", "Harvard"]].map(g => `<button type="button" class="mdl-pill" data-mgaya="${g[0]}">${g[1]}</button>`).join("")}</div><div data-msitasi style="white-space:normal">${esc(mdlSitasi(p, "apa"))}</div><button type="button" class="mdl-pact" style="margin-top:9px" data-msalin>Salin Sitasi</button></div>`;
        exp.querySelectorAll("[data-mgaya]").forEach(g => g.onclick = () => {
          exp.querySelector("[data-msitasi]").textContent = mdlSitasi(p, g.dataset.mgaya);
          exp.querySelectorAll("[data-mgaya]").forEach(x => x.classList.toggle("on", x === g));
        });
        const onG = exp.querySelector('[data-mgaya="apa"]'); if (onG) onG.classList.add("on");
        exp.querySelector("[data-msalin]").onclick = () => copyText(exp.querySelector("[data-msitasi]").textContent);
      }
    };
  });
}

function mdlLangkahSet(n, tahapTeks) {
  $$("#mdlStage2 [data-step]").forEach(li => {
    const s = +li.dataset.step;
    li.classList.toggle("done", s < n);
    li.classList.toggle("doing", s === n);
  });
  if (tahapTeks) { const t = $("#mdlTahapTeks"); if (t) t.textContent = tahapTeks; }
}

async function mdlJalankan() {
  const q = mdlQueryForm();
  $("#mdlFormError").innerHTML = "";
  if (MDL.start === "konteks" && q.length < 30) {
    $("#mdlFormError").innerHTML = `<div class="mdl-form-error">Ceritakan kebutuhanmu sedikit lebih lengkap dulu (baru ${q.length} karakter, minimal sekitar 30) supaya hasil pencariannya tajam.</div>`;
    return;
  }
  if (MDL.start === "doi" && !(q.includes("10.") || q.includes("http"))) {
    $("#mdlFormError").innerHTML = `<div class="mdl-form-error">Yang kamu tempel belum terlihat seperti DOI atau link artikel. DOI biasanya diawali 10. dan link diawali http.</div>`;
    return;
  }
  if (!q) { toast("Isi dulu konteks pencarianmu ya."); return; }
  const btn = $("#mdlRun"); setLoading(btn, true, "Menyiapkan…");
  const r = await apiPOST("/mendalam/job", {mode: MDL.mode, query: q, start: MDL.start, saringan: mdlSaringanKirim()});
  setLoading(btn, false);
  if (!r.ok) {
    const e = r.data || {};
    if (e.error === "perlu_login") { openAuth("Fitur ini khusus member. Masuk dulu ya."); return; }
    $("#mdlFormError").innerHTML = `<div class="mdl-form-error">${esc(e.pesan || "Pencarian belum bisa dijalankan. Coba lagi sebentar lagi.")}</div>`;
    mdlMuatInfo();
    return;
  }
  const d = r.data;
  MDL.queryAktif = q;
  if (d.status === "selesai") {
    MDL.hasil = d.hasil; MDL.hasilMode = d.mode; MDL.kunci = d.kunci;
    if (d.dari_cache && MDL.hasil) MDL.hasil.dari_cache = true;
    MDL.banding = {}; MDL.simpan = {}; MDL.saringAkses = "semua"; MDL.saringTahun = "semua";
    toast("Hasil yang sama ditemukan di cache (tersimpan 7 hari), jadi kuotamu tidak terpotong.");
    mdlRenderHasil(); mdlSetStage(3); mdlMuatInfo(); mdlMuatRiwayat();
    return;
  }
  MDL.jobId = d.id;
  mdlSetStage(2);
  mdlLangkahSet(1, "Menyusun kata kunci dari konteksmu…");
  $("#mdlProcNote").textContent = MDL.mode === "laporan"
    ? "Perkiraan waktu Laporan Riset: " + MDL_MODES.laporan.waktu[MDL.saringan.kedalaman] + ". Laporan berjalan di latar belakang di server. Hasil dan laporannya masuk ke Riwayat Pencarian Mendalam di halaman ini begitu selesai, dan kredit terpotong hanya bila laporan selesai."
    : "Perkiraan waktu mode ini: " + MDL_MODES[MDL.mode].waktu[MDL.saringan.kedalaman] + ". Jangan tutup halaman ini dulu.";
  mdlPollJob();
}

function mdlPollJob() {
  // FIX 2026-11-10: polling diberi batas 200 putaran seperti polling AI.
  // Tanpa batas, job yang hilang di server (mis. server restart) membuat
  // tampilan proses berputar selamanya tanpa pesan apa pun ke pengguna.
  let putaranMdl = 0;
  const iv = setInterval(async () => {
    if (!MDL || !MDL.jobId) { clearInterval(iv); return; }
    putaranMdl++;
    if (putaranMdl > 200) {
      clearInterval(iv); MDL.jobId = null;
      mdlSetStage(1);
      $("#mdlFormError").innerHTML = `<div class="mdl-form-error">Prosesnya kelamaan dan sepertinya terhenti di server. Coba cek Riwayat Pencarian Mendalam sebentar lagi, siapa tahu hasilnya tetap masuk; kalau belum ada, jalankan lagi ya.</div>`;
      mdlMuatInfo(); mdlMuatRiwayat();
      return;
    }
    const r = await apiJSON("/mendalam/job-status?id=" + encodeURIComponent(MDL.jobId));
    if (!r.ok) { clearInterval(iv); toast("Status proses tidak terbaca. Cek lagi dari riwayat sebentar lagi."); mdlSetStage(1); return; }
    const d = r.data;
    mdlLangkahSet(d.langkah || 1, d.tahap || "");
    if (d.status === "selesai") {
      clearInterval(iv);
      MDL.hasil = d.hasil; MDL.hasilMode = d.mode; MDL.kunci = d.kunci; MDL.jobId = null;
      if (d.dari_cache && MDL.hasil) MDL.hasil.dari_cache = true;
      MDL.banding = {}; MDL.simpan = {}; MDL.saringAkses = "semua"; MDL.saringTahun = "semua";
      mdlRenderHasil(); mdlSetStage(3); mdlMuatInfo(); mdlMuatRiwayat();
    } else if (d.status === "gagal") {
      clearInterval(iv); MDL.jobId = null;
      mdlSetStage(1);
      $("#mdlFormError").innerHTML = `<div class="mdl-form-error">${esc(d.error || "Pencarian gagal.")} Kuota atau kreditmu tidak jadi terpotong.</div>`;
      mdlMuatInfo(); mdlMuatRiwayat();
    }
  }, MDL && MDL.mode === "laporan" ? 5000 : 2500);
  regTimer(iv);
}

function mdlPapersTampil() {
  if (!MDL.hasil) return [];
  return MDL.hasil.papers.filter(p => {
    if (MDL.saringAkses !== "semua" && p.access !== MDL.saringAkses) return false;
    if (MDL.saringTahun === "2024" && !(+p.year >= 2024)) return false;
    if (MDL.saringTahun === "2023" && !(+p.year >= 2023)) return false;
    if (MDL.saringTahun === "2022" && !(+p.year >= 2022)) return false;
    return true;
  });
}

function mdlJawabanHTML() {
  const h = MDL.hasil, mode = MDL.hasilMode;
  if (!h) return "";
  if (mode === "laporan") {
    const teks = (h.laporan && h.laporan.text) || "";
    return `<div class="mdl-answer"><div class="mode-note">MODE: LAPORAN RISET</div><h3>Laporan Riset Selesai</h3><div style="white-space:pre-wrap;font-size:14.5px">${mdRapi ? mdRapi(teks) : esc(teks)}</div><div class="mdl-followup">${mdlTanyaHTML()}</div></div>`;
  }
  if (mode === "cepat") {
    const a = h.jawaban || {};
    let teks = esc(a.answer || "");
    teks = teks.replace(/\[(\d+)\]/g, (m, n) => `<button type="button" class="mdl-pact" style="padding:1px 8px" data-mcite="${n}">[${n}]</button>`);
    return `<div class="mdl-answer"><div class="mode-note">MODE: JAWABAN CEPAT</div><h3>Jawaban Langsung</h3><p style="margin:0">${teks}</p><div class="mdl-followup">${mdlTanyaHTML()}</div></div>`;
  }
  if (mode === "bedah") {
    const b = h.bedah || {};
    const ground = (bidang) => {
      const g = ((b.grounding || {})[bidang]) || [];
      if (!g.length) return "";
      return `<div class="mdl-ground">Dasar klaim: ${g.map(x => `<a href="${esc(x.url || "#")}" target="_blank" rel="noopener">${esc((x.domain || "sumber"))}</a>`).join(", ")}</div>`;
    };
    const list = (arr) => `<ul>${(arr || []).map(x => `<li>${esc(x)}</li>`).join("")}</ul>`;
    return `<div class="mdl-answer"><div class="mode-note">MODE: BEDAH MENDALAM</div><h3>Bedah Mendalam</h3>
      <p style="margin:0 0 4px">${esc(b.ringkasan || "")}</p>${ground("ringkasan")}
      <h4 style="margin:10px 0 4px">Faktor dan Penyebab</h4>${list(b.faktor_penyebab)}${ground("faktor_penyebab")}
      <h4 style="margin:10px 0 4px">Dampak</h4>${list(b.dampak)}${ground("dampak")}
      <h4 style="margin:10px 0 4px">Rekomendasi</h4>${list(b.rekomendasi)}${ground("rekomendasi")}
      <div class="mdl-followup">${mdlTanyaHTML()}</div></div>`;
  }
  const top = (h.papers || []).slice(0, 3);
  return `<div class="mdl-answer"><div class="mode-note">MODE: CARI JURNAL</div><h3>Ringkasan Hasil Pencarian</h3>
    <p style="margin:0">Ditemukan ${h.jumlah || 0} sumber yang paling berkaitan dengan konteksmu. ${top.map(p => `Paper [${p.id}] (${esc(p.domain || "")}, ${esc(p.year || "")}) membahas ${esc((p.snippet || p.title || "").slice(0, 140))}`).join(". ")}${top.length ? "." : ""} Rincian tiap sumber, label aksesnya, dan cuplikan paling relevannya ada di daftar sumber di bawah.</p>
    <div class="mdl-followup">${mdlTanyaHTML()}</div></div>`;
}

function mdlTanyaHTML() {
  return `<div class="mdl-flabel">Tanya Lanjut (tidak memotong kuota mendalam)</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap"><input type="text" id="mdlTanyaInput" placeholder="Tanya apa pun soal hasil ini…" style="flex:1;min-width:200px"><button class="mdl-btn primary" id="mdlTanyaBtn">Tanya</button></div>
    <div id="mdlTanyaThread"></div>`;
}

function mdlRenderHasil() {
  const host = $("#mdlHasilHost"); if (!host || !MDL.hasil) return;
  const h = MDL.hasil, mode = MDL.hasilMode;
  const meta = [`Mode: <b>${MDL_MODES[mode].nama}</b>`, mode === "laporan" ? "Laporan lengkap" : `${h.jumlah || 0} sumber`];
  if (h.dari_cache) meta.push("dari cache 7 hari");
  if (h.penyaring) {
    if (h.penyaring.fokus) meta.push("Fokus: " + mdlFokusLabel(h.penyaring.fokus));
    if (h.penyaring.domain) meta.push("Domain: " + esc(h.penyaring.domain));
  }
  if (h.titik_mulai_doi) meta.push("Titik mulai: " + esc(h.titik_mulai_doi));
  const counts = {full: 0, abstract: 0, partial: 0, metadata: 0};
  (h.papers || []).forEach(p => { if (counts[p.access] != null) counts[p.access]++; });
  const tampil = mdlPapersTampil();
  const kosong = (h.papers || []).length === 0;
  host.innerHTML = `
    <div class="mdl-chips">${meta.map(m => `<span class="mdl-chip">${m}</span>`).join("")}${mode === "laporan" ? "" : `<span class="mdl-chip" id="mdlSimpanCount">Paper tersimpan: 0</span>`}</div>
    ${mdlJawabanHTML()}
    ${kosong ? "" : `
    <div class="mdl-actionbar">
      <div class="row"><span class="lab">Tampilkan menurut akses</span>
        ${[["semua", "Semua akses"], ["full", "Teks Penuh saja"], ["abstract", "Abstrak Saja"], ["partial", "Pratinjau Terbatas"], ["metadata", "Metadata Saja"]].map(a => `<button type="button" class="mdl-pill ${MDL.saringAkses === a[0] ? "on" : ""}" data-mfakses="${a[0]}">${a[1]}</button>`).join("")}
      </div>
      <div class="row"><span class="lab">Saring ulang menurut tahun terbit</span>
        ${[["semua", "Semua tahun"], ["2024", "2024 ke atas"], ["2023", "2023 ke atas"], ["2022", "2022 ke atas"]].map(a => `<button type="button" class="mdl-pill ${MDL.saringTahun === a[0] ? "on" : ""}" data-mftahun="${a[0]}">${a[1]}</button>`).join("")}
        <span class="muted" style="font-size:12.5px">ini menyaring daftar yang sudah ada, tidak mencari ulang</span>
      </div>
      <div class="row"><span class="lab">Preset cepat</span>
        <button type="button" class="mdl-pick" id="mdlPersempit">Persempit Hasil</button>
        <button type="button" class="mdl-pick" id="mdlPerluas">Perluas Hasil</button>
        <span class="muted" style="font-size:12.5px">Persempit = terbit 2022 ke atas dan teks penuh saja</span>
      </div>
      <div class="row"><span class="lab">Ekspor</span>
        <button type="button" class="mdl-pick" id="mdlEkspor">Ekspor Daftar Referensi</button>
        <span class="muted" style="font-size:12.5px">Mengunduh berkas teks berisi sitasi APA 7 tiap paper yang sedang tampil, lengkap dengan keterangan aksesnya.</span>
      </div>
    </div>
    <div class="mdl-legend">
      <b>Transparansi Akses</b> <span class="muted" style="font-size:13px">Label ini dihitung dari isi yang benar benar berhasil diambil sistem kami dari tiap sumber, bukan janji dari penerbitnya.</span>
      <div class="mdl-legend-grid">
        ${Object.keys(MDL_AKSES).map(k => `<div class="mdl-legend-item"><span class="mdl-badge2 acc-${k}">${MDL_AKSES[k].label}</span> ${MDL_AKSES[k].desc}</div>`).join("")}
      </div>
      <div class="mdl-counts">
        <span class="mdl-count">Total: ${h.jumlah || 0}</span>
        ${Object.keys(MDL_AKSES).filter(k => counts[k] > 0).map(k => `<span class="mdl-count">${MDL_AKSES[k].label}: ${counts[k]}</span>`).join("")}
      </div>
    </div>`}
    <div id="mdlPapersHost">
      ${kosong ? "" : tampil.map(p => mdlPaperHTML(p, false)).join("")}
    </div>
    ${!kosong && tampil.length === 0 ? `<div class="mdl-empty"><b>Tidak ada sumber yang cocok dengan saringan tampilan ini.</b><p style="margin:6px 0 0;font-size:13.5px">Longgarkan saringannya lewat tombol Perluas Hasil di atas. Saringan tampilan tidak mengubah hasil pencarian aslinya.</p></div>` : ""}
    <div id="mdlBandingHost"></div>
    ${kosong && mode !== "laporan" ? `
    <div class="mdl-empty">
      <b>Pencarianmu selesai, tetapi tidak ada jurnal yang cocok ditemukan.</b>
      <p style="margin:6px 0 4px;font-size:13.5px">Beberapa penyebab yang paling sering terjadi:</p>
      <ul style="margin:4px 0 8px;padding-left:18px">
        <li>Jurnalnya belum terindeks di sumber terbuka yang dipakai sistem ini.</li>
        <li>Jurnalnya hanya tersimpan di basis data berbayar tertutup seperti Scopus atau Web of Science.</li>
        <li>Kata kunci di konteksmu belum cocok dengan judul atau abstrak yang tersimpan.</li>
        <li>Artikelnya terlalu baru sehingga belum terindeks.</li>
        <li>Layanan pencarian sedang mencapai batas sesaat.</li>
      </ul>
      <p style="margin:0 0 6px;font-size:13.5px">Coba persempit atau ganti sudut katanya, misalnya:</p>
      <div class="mdl-mini-chips">${["pencegahan penyakit kardiovaskular", "pengetahuan sikap tindakan kesehatan", "faktor risiko penyakit jantung", "perilaku hidup sehat usia produktif"].map(s => `<button type="button" class="mdl-mini" data-msaran="${esc(s)}">${esc(s)}</button>`).join("")}</div>
    </div>` : ""}
  `;
  mdlWirePapers(host);
  host.querySelectorAll("[data-mcite]").forEach(b => b.onclick = () => {
    const el = document.getElementById("mdlPaper" + b.dataset.mcite);
    if (el) { el.scrollIntoView({behavior: "smooth", block: "center"}); el.classList.add("flash"); setTimeout(() => el.classList.remove("flash"), 1600); }
  });
  host.querySelectorAll("[data-mfakses]").forEach(b => b.onclick = () => { MDL.saringAkses = b.dataset.mfakses; mdlRenderHasil(); });
  host.querySelectorAll("[data-mftahun]").forEach(b => b.onclick = () => { MDL.saringTahun = b.dataset.mftahun; mdlRenderHasil(); });
  host.querySelectorAll("[data-msaran]").forEach(b => b.onclick = () => {
    mdlTab("konteks");
    const k = $("#mdlKonteks"); if (k) k.value = "Saya sedang mencari jurnal tentang " + b.dataset.msaran + " untuk kebutuhan skripsi dan tinjauan pustaka. Utamakan studi di Indonesia dalam 5 tahun terakhir.";
    mdlSetStage(1);
  });
  const per = $("#mdlPersempit"); if (per) per.onclick = () => { MDL.saringAkses = "full"; MDL.saringTahun = "2022"; mdlRenderHasil(); };
  const plu = $("#mdlPerluas"); if (plu) plu.onclick = () => { MDL.saringAkses = "semua"; MDL.saringTahun = "semua"; mdlRenderHasil(); };
  const eks = $("#mdlEkspor"); if (eks) eks.onclick = mdlEkspor;
  const tb = $("#mdlTanyaBtn"); if (tb) tb.onclick = mdlTanyaKirim;
  const ti = $("#mdlTanyaInput"); if (ti) ti.onkeydown = e => { if (e.key === "Enter") mdlTanyaKirim(); };
  mdlRenderBanding();
}

function mdlRenderBanding() {
  const host = $("#mdlBandingHost"); if (!host || !MDL.hasil) return;
  const ids = Object.keys(MDL.banding).filter(k => MDL.banding[k]).map(Number);
  if (ids.length === 1) {
    host.innerHTML = `<div class="mdl-note-amber">Satu paper lagi perlu ditandai supaya tabel perbandingannya muncul. Tandai minimal 2 paper.</div>`;
    return;
  }
  if (ids.length < 2) { host.innerHTML = ""; return; }
  const ps = MDL.hasil.papers.filter(p => ids.includes(p.id));
  const baris = (label, fn) => `<tr><td class="rowhead">${label}</td>${ps.map(p => `<td>${fn(p)}</td>`).join("")}</tr>`;
  host.innerHTML = `<div class="mdl-compare"><h3 style="margin:14px 0 8px">Perbandingan Paper</h3>
    <table><thead><tr><th>Aspek</th>${ps.map(p => `<th>[${p.id}] ${esc(p.title)}</th>`).join("")}</tr></thead>
    <tbody>
      ${baris("Tahun", p => esc(p.year || "-"))}
      ${baris("Akses", p => esc((MDL_AKSES[p.access] || {}).label || p.access) + ". " + esc(p.access_note || ""))}
      ${baris("Metode", () => "Tidak terbaca dari sumber")}
      ${baris("Temuan utama", p => esc(p.snippet || "Tidak terbaca dari sumber"))}
    </tbody></table>
    <p class="muted" style="font-size:12.5px">Metode tidak selalu tertulis di cuplikan yang terambil, jadi baris itu diisi apa adanya bila memang tidak terbaca.</p></div>`;
  mdlWirePapers(host);
}

function mdlEkspor() {
  const list = mdlPapersTampil();
  if (!list.length) { toast("Tidak ada paper yang sedang tampil untuk diekspor."); return; }
  const lines = list.map(p => {
    const gaya = mdlSitasi(p, "apa");
    return `[${p.id}] ${gaya}\n    [Akses: ${(MDL_AKSES[p.access] || {}).label || p.access_label} — ${p.access_note || ""}]`;
  });
  const blob = new Blob(["Daftar Referensi — Cari Jurnal Mendalam KhairyPustaka\nKonteks: " + (MDL.queryAktif || "") + "\n\n" + lines.join("\n\n") + "\n"], {type: "text/plain;charset=utf-8"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = "daftar-referensi-mendalam.txt"; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  toast("Daftar referensi terunduh (" + list.length + " paper).");
}

async function mdlTanyaKirim() {
  const inp = $("#mdlTanyaInput");
  const tanya = (inp.value || "").trim();
  if (!tanya) return;
  const btn = $("#mdlTanyaBtn"); setLoading(btn, true, "Menjawab…");
  const r = await apiPOST("/mendalam/tanya-lanjut", {tanya: tanya, konteks: MDL.queryAktif || ""});
  setLoading(btn, false);
  const thread = $("#mdlTanyaThread"); if (!thread) return;
  if (!r.ok) {
    thread.insertAdjacentHTML("beforeend", `<div class="mdl-fu-item"><span class="q">Kamu:</span> ${esc(tanya)}<br><span style="color:var(--mrose)">${esc((r.data && r.data.pesan) || "Tanya lanjut belum bisa dijawab sekarang.")}</span></div>`);
    return;
  }
  inp.value = "";
  const d = r.data;
  const src = (d.citations || []).slice(0, 5).map((c, ix) => `<div class="mdl-ground">[${ix + 1}] <a href="${esc(c.url || "#")}" target="_blank" rel="noopener">${esc(c.title || c.url || "sumber")}</a></div>`).join("");
  thread.insertAdjacentHTML("beforeend", `<div class="mdl-fu-item"><span class="q">Kamu:</span> ${esc(tanya)}<br><span>${esc(d.answer || "")}</span>${src}</div>`);
}

/* ---------- Router ---------- */
const ROUTES = {
  "/beranda": pageBeranda,
  "/cari": pageCari,
  "/mendalam": pageMendalam,
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
  "/kritik-saran": pageKritikSaran,
  "/matriks": pageMatriks,
  "/statistik": pageStatistik,
  "/template": pageTemplate,
  "/parafrase": pageParafrase,
  "/terjemah": pageTerjemah,
  "/kemiripan": pageKemiripan,
  "/ekstrak-teks": pageEkstrakTeks,
  "/convert": pageConvert,
  "/ai": pageAI,
  "/tanya": pageTanya,
  "/topup": pageTopup,
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
      a.innerHTML = tic("shield") + " Panel Admin";
      nav.appendChild(a);
    }
  } else if (a) {
    a.remove();
  }
  // Menu Jatah AI khusus member login: tamu & admin tidak menampilkannya
  // (perintah Sul 2026-10-18 — tamu tidak boleh melihat menu top-up).
  const nt = $("#navTopup");
  if (nt) nt.style.display = (AUTH.login && !AUTH.admin) ? "" : "none";
}

function admKartu(angka, label, ikon) {
  return '<div class="stat-card"><span class="stat-label">' +
    (ikon ? tic(ikon) : "") + esc(label) + "</span><b>" +
    esc(String(angka == null ? 0 : angka)) + "</b></div>";
}

function admTabel(rows) {
  if (!rows.length) return '<p class="muted">Belum ada data.</p>';
  const maks = Math.max.apply(null, rows.map(r => r.total || 0).concat([1]));
  return '<div class="adm-bars">' + rows.map(function(r) {
    const persen = Math.max(2, Math.round((r.total || 0) / maks * 100));
    return '<div class="bar-row"><span class="bname">' + esc(r.nama) +
      '<small>Hari ini ' + esc(String(r.hari_ini)) + '</small></span>' +
      '<span class="track"><span class="fill" style="width:' + persen + '%"></span></span>' +
      '<span class="bnum">' + esc(String(r.total)) + "</span></div>";
  }).join("") + "</div>";
}

let ADM_STAT = null;   // cache statistik terakhir (tren + ekspor CSV)
let ADM_TREN_HARI = 7; // rentang tren yang sedang ditampilkan

function fmtWita(ts) {
  try {
    const d = new Date((Number(ts) || 0) * 1000);
    if (isNaN(d.getTime())) return "";
    return new Intl.DateTimeFormat("id-ID", {
      timeZone: "Asia/Makassar", day: "numeric", month: "short",
      hour: "2-digit", minute: "2-digit", hour12: false
    }).format(d).replace(".", ":") + " WITA";
  } catch (e) { return ""; }
}

function admPill(teks, kelas) {
  return '<span class="pill ' + esc(kelas || "grey") + '">' + esc(teks) + "</span>";
}

async function admMuatStatistik() {
  const box = $("#admStat");
  if (!box) return;
  try {
    const r = await apiJSON("/admin/statistik");
    if (!r.ok) throw 0;
    const d = r.data || {}, m = d.member || {}, a = d.ai || {}, n = d.nonai || {};
    ADM_STAT = d;
    box.innerHTML =
      '<div class="admin-grid">' +
      admKartu(m.total, "Total Member", "person") +
      admKartu(m.baru_hari_ini, "Member Baru Hari Ini", "plus") +
      admKartu(a.hari_ini, "Pemakaian AI Hari Ini", "spark") +
      admKartu(n.hari_ini, "Pemakaian Tool Hari Ini", "grid") +
      admKartu(d.saldo_ai_web_total, "Saldo AI Web (kredit)", "ticket") +
      admKartu(a.total, "Pemakaian AI Total", "spark") +
      admKartu(n.total, "Pemakaian Tool Total", "chart") +
      "</div>" +
      "<h4>Pemakaian Tool Terlaris</h4>" + admTabel(
        ((a.per_fitur || []).concat(n.per_fitur || []))
          .sort((x, y) => (y.total || 0) - (x.total || 0)).slice(0, 8)) +
      "<h4>Tren Harian</h4>" +
      '<div class="chip-row">' +
        '<button class="chip' + (ADM_TREN_HARI === 7 ? " active" : "") + '" data-tren="7">7 hari</button>' +
        '<button class="chip' + (ADM_TREN_HARI === 30 ? " active" : "") + '" data-tren="30">30 hari</button>' +
      "</div>" +
      '<div id="admTren"></div>' +
      '<button class="btn btn-block" id="admEksporBtn">' + tic("download") + " Ekspor Laporan (CSV)</button>" +
      '<p class="muted tiny">Unduh ringkasan statistik, pemakaian per fitur, dan tren 30 hari sebagai file CSV.</p>';
    admRenderTren();
    $$("#admStat [data-tren]").forEach(function(b) {
      b.addEventListener("click", function() {
        ADM_TREN_HARI = parseInt(b.dataset.tren, 10) || 7;
        $$("#admStat [data-tren]").forEach(function(x) {
          x.classList.toggle("active", x === b);
        });
        admRenderTren();
      });
    });
    const eb = $("#admEksporBtn");
    if (eb) eb.addEventListener("click", admEksporCSV);
  } catch (e) {
    box.innerHTML = '<p class="pf-msg">Gagal memuat statistik. Muat ulang halamannya ya.</p>';
  }
}

function admRenderTren() {
  const box = $("#admTren");
  if (!box || !ADM_STAT || !ADM_STAT.tren) return;
  const t = ADM_STAT.tren;
  const n = Math.min(ADM_TREN_HARI, (t.tanggal || []).length);
  if (!n) { box.innerHTML = '<p class="muted">Belum ada data tren.</p>'; return; }
  const tgl = t.tanggal.slice(-n), ai = t.ai.slice(-n),
        nonai = t.nonai.slice(-n), mb = t.member_baru.slice(-n);
  const total = ai.map((v, i) => (v || 0) + (nonai[i] || 0));
  const maks = Math.max.apply(null, total.concat([1]));
  let totPakai = 0, totBaru = 0;
  const cols = total.map(function(v, i) {
    totPakai += v; totBaru += mb[i] || 0;
    const h = Math.max(3, Math.round(v / maks * 100));
    const tglSingkat = (tgl[i] || "").slice(5).split("-").reverse().join("/");
    return '<div class="tcol"><span class="tval">' + (mb[i] || "") + '</span>' +
      '<div class="tbar" style="height:' + h + '%"></div>' +
      '<span class="tday">' + esc(tglSingkat) + "</span></div>";
  }).join("");
  box.innerHTML =
    '<div class="tren-chart">' + cols + "</div>" +
    '<p class="muted tiny"><span class="tren-kunci tren-kunci-teal"></span> Pemakaian per hari' +
    ' &nbsp; <span class="tren-kunci tren-kunci-emas"></span> Member baru per hari (angka emas di atas batang)</p>' +
    '<p class="muted tiny">Total ' + n + ' hari terakhir: ' + totPakai +
    ' kali pemakaian, ' + totBaru + " member baru.</p>";
}

function admEksporCSV() {
  if (!ADM_STAT) return;
  const d = ADM_STAT, m = d.member || {}, a = d.ai || {}, n = d.nonai || {};
  function q(v) { return '"' + String(v == null ? "" : v).replace(/"/g, '""') + '"'; }
  const L = [];
  L.push(["Laporan KhairyPustaka", d.hari_ini || ""].map(q).join(","));
  L.push(["Total Member", m.total || 0].map(q).join(","));
  L.push(["Member Baru Hari Ini", m.baru_hari_ini || 0].map(q).join(","));
  L.push(["Pemakaian AI Hari Ini", a.hari_ini || 0].map(q).join(","));
  L.push(["Pemakaian AI Total", a.total || 0].map(q).join(","));
  L.push(["Pemakaian Tool Hari Ini", n.hari_ini || 0].map(q).join(","));
  L.push(["Pemakaian Tool Total", n.total || 0].map(q).join(","));
  L.push(["Saldo AI Web Total", d.saldo_ai_web_total || 0].map(q).join(","));
  L.push("");
  L.push(["Fitur AI", "Hari Ini", "Total"].map(q).join(","));
  (a.per_fitur || []).forEach(function(r) { L.push([r.nama, r.hari_ini, r.total].map(q).join(",")); });
  L.push("");
  L.push(["Fitur Non AI", "Hari Ini", "Total"].map(q).join(","));
  (n.per_fitur || []).forEach(function(r) { L.push([r.nama, r.hari_ini, r.total].map(q).join(",")); });
  if (d.tren && d.tren.tanggal) {
    L.push("");
    L.push(["Tanggal", "Pemakaian AI", "Pemakaian Tool", "Member Baru"].map(q).join(","));
    d.tren.tanggal.forEach(function(t, i) {
      L.push([t, d.tren.ai[i] || 0, d.tren.nonai[i] || 0, d.tren.member_baru[i] || 0].map(q).join(","));
    });
  }
  const blob = new Blob(["\ufeff" + L.join("\n")], { type: "text/csv;charset=utf-8" });
  const a2 = document.createElement("a");
  a2.href = URL.createObjectURL(blob);
  a2.download = "laporan-khairypustaka-" + (d.hari_ini || "hari-ini") + ".csv";
  document.body.appendChild(a2);
  a2.click();
  a2.remove();
  setTimeout(function() { URL.revokeObjectURL(a2.href); }, 4000);
  toast("Laporan terunduh ");
}

async function admMuatSistem() {
  const box = $("#admSistem");
  if (!box) return false;
  try {
    const r = await apiJSON("/admin/sistem");
    if (!r.ok) throw 0;
    const d = r.data || {}, s = d.server || {}, an = d.antrean || {};
    function barisAntre(nama, j) {
      j = j || {};
      let t = '<div class="list-row"><div class="lr-main"><b>' + esc(nama) + "</b>" +
        '<div class="muted tiny">Jalan ' + (j.jalan || 0) + " · Antre " + (j.antre || 0) +
        " · Gagal " + (j.gagal || 0) + " · Selesai " + (j.selesai || 0) + "</div>";
      if (j.terakhir) {
        t += '<div class="muted tiny">Hari ini selesai ' + (j.selesai_hari_ini || 0) +
          " · gagal " + (j.gagal_hari_ini || 0) + " · aktivitas terakhir " +
          esc(fmtWita(j.terakhir)) + "</div>";
      }
      t += "</div>" +
        admPill(String((j.jalan || 0) + (j.antre || 0)) + " pekerjaan aktif",
          (j.gagal_hari_ini || 0) > 0 ? "warn" : "grey") + "</div>";
      return t;
    }
    function fmtAngka(x) {
      try { return Number(x || 0).toLocaleString("id-ID", { maximumFractionDigits: 2 }); }
      catch (e) { return String(x || 0); }
    }
    const rd = d.render || {};
    let renderPill = admPill("Belum terhubung", "warn");
    let renderDetail = esc(rd.catatan || "");
    if (rd.terhubung) {
      renderPill = admPill("Terhubung", "ok");
      renderDetail =
        "Bandwidth bulan ini <b>" + fmtAngka(rd.bandwidth_gb) + " GB</b> · perkiraan jam instance <b>" +
        fmtAngka(rd.jam_instance_perkiraan) + " jam</b>" +
        '<div class="muted tiny">Periode data ' + esc(fmtWita(rd.periode_mulai)) + " s.d. " +
        esc(fmtWita(rd.periode_selesai)) + " · diperbarui " + esc(fmtWita(rd.diperbarui)) + "</div>" +
        '<div class="muted tiny">' + esc(rd.catatan || "") + "</div>";
    }
    const cb = d.cerebras || {};
    let cerebrasPill = admPill("Belum terbaca", "warn");
    let cerebrasDetail = esc(cb.catatan || "");
    if (cb.tercatat) {
      cerebrasPill = admPill("Tercatat", "ok");
      const hi = cb.hari_ini || {}, bi = cb.bulan_ini || {};
      let t = "Hari ini <b>" + (hi.panggilan || 0) + " panggilan</b> · " + fmtAngka(hi.token) + " token" +
        '<div class="muted tiny">Bulan ini ' + (bi.panggilan || 0) + " panggilan · " +
        fmtAngka(bi.token) + " token tercatat oleh situs</div>";
      if (cb.acuan) {
        t += '<div class="muted tiny">Saldo acuan <b>$' + fmtAngka(cb.acuan.saldo_usd) + "</b> per " +
          esc(fmtWita(cb.acuan.waktu)) + " · perkiraan terpakai sejak acuan $" +
          fmtAngka(cb.perkiraan_biaya_sejak_acuan_usd) + "</div>" +
          "<div><b>Perkiraan sisa $" + fmtAngka(cb.perkiraan_sisa_usd) + "</b></div>";
      }
      t += '<div class="muted tiny">' + esc(cb.catatan || "") + "</div>";
      cerebrasDetail = t;
    }
    function fmtUsd(x) {
      if (x === null || x === undefined) return "";
      try { return "$" + Number(x).toLocaleString("id-ID", { minimumFractionDigits: 2, maximumFractionDigits: 4 }); }
      catch (e) { return "$" + String(x); }
    }
    const orr = d.openrouter || {};
    let orPill = admPill("Belum terhubung", "warn");
    let orDetail = esc(orr.catatan || "");
    if (orr.terhubung) {
      orPill = admPill("Terhubung", "ok");
      let t = "";
      const kh = orr.kuota_harian || null;
      if (kh) {
        t += "Kuota model gratis hari ini: terpakai <b>" + fmtAngka(kh.terpakai) +
          " dari " + fmtAngka(kh.batas) + " request</b> · sisa <b>" + fmtAngka(kh.sisa) + " request</b>" +
          '<div class="muted tiny">Kuota dihitung per hari UTC dan reset tengah malam UTC, pukul 08.00 WITA</div>';
      }
      t += '<div class="muted tiny">Pemakaian kunci: hari ini ' + fmtUsd(orr.usage_harian_usd) +
        " · minggu ini " + fmtUsd(orr.usage_mingguan_usd) +
        " · bulan ini " + fmtUsd(orr.usage_bulanan_usd) +
        " · total " + fmtUsd(orr.usage_total_usd) + "</div>";
      if (orr.tanpa_batas) {
        t += '<div class="muted tiny">Batas belanja kunci: tanpa batas</div>';
      } else {
        t += '<div class="muted tiny">Batas belanja kunci ' + fmtUsd(orr.limit_usd) +
          " · sisa " + fmtUsd(orr.limit_sisa_usd) + "</div>";
      }
      t += '<div class="muted tiny">Status tier: ' +
        (orr.tier_gratis ? "gratis, belum pernah membeli kredit" : "berbayar, pernah membeli kredit") + "</div>";
      const sa = orr.saldo_akun || {};
      if (sa.tersedia) {
        t += "<div>Saldo akun: total kredit <b>" + fmtUsd(sa.total_kredit_usd) + "</b> · terpakai " +
          fmtUsd(sa.total_terpakai_usd) + " · <b>sisa " + fmtUsd(sa.sisa_usd) + "</b></div>";
      } else if (sa.catatan) {
        t += '<div class="muted tiny">' + esc(sa.catatan) + "</div>";
      }
      t += '<div class="muted tiny">Diperbarui ' + esc(fmtWita(orr.diperbarui)) + "</div>" +
        '<div class="muted tiny">' + esc(orr.catatan || "") + "</div>";
      orDetail = t;
    }
    const md = d.mendalam || {};
    let mdPill = admPill("Belum terbaca", "warn");
    let mdDetail = esc(md.catatan || "");
    if (md.aktif) {
      mdPill = md.kunci_terpasang ? admPill("Kunci terpasang", "ok")
        : admPill("Kunci belum terpasang", "warn");
      const j = md.job_hari_ini || {};
      const jSelesai = function(m) { return (j[m] || {}).selesai || 0; };
      const lap = j.laporan || {};
      mdDetail =
        '<div class="muted tiny">Status kunci: ' +
          (md.kunci_terpasang ? "terpasang" : "belum terpasang") + "</div>" +
        "<div>Belanja hari ini <b>" + fmtUsd(md.belanja_hari_ini_usd) + "</b>" +
          " · Sisa plafon harian <b>" + fmtUsd(md.sisa_cap_harian_usd) + "</b>" +
          " dari " + fmtUsd(md.cap_harian_usd) + "</div>" +
        '<div class="muted tiny">Belanja bulan ini ' + fmtUsd(md.belanja_bulan_ini_usd) +
          " · Sisa plafon bulanan " + fmtUsd(md.sisa_cap_bulanan_usd) +
          " dari " + fmtUsd(md.cap_bulanan_usd) + "</div>" +
        '<div class="muted tiny">Job hari ini: Cari Jurnal Mendalam ' + jSelesai("cari") +
          " · Jawaban Cepat " + jSelesai("cepat") +
          " · Bedah Mendalam " + jSelesai("bedah") +
          " · Laporan Riset selesai " + (lap.selesai || 0) +
          ", gagal " + (lap.gagal || 0) + "</div>" +
        "<div>Total belanja <b>" + fmtUsd(md.total_belanja_usd) + "</b>" +
          " · <b>Perkiraan sisa kredit " + fmtUsd(md.perkiraan_sisa_kredit_usd) + "</b></div>" +
        '<div class="muted tiny">Perkiraan dihitung dari basis kredit ' +
          fmtUsd(md.saldo_basis_usd) + " dikurangi total belanja yang " +
          "tercatat situs sejak fitur berjalan.</div>";
    }
    const modelRows = (d.model || []).map(function(mo) {
      const kelas = mo.status === "sehat" ? "ok" : mo.status === "error" ? "err" :
        mo.status === "lambat" ? "warn" : "grey";
      const label = mo.status === "sehat" ? "Sehat" : mo.status === "error" ? "Error" :
        mo.status === "lambat" ? "Lambat" : "Belum ada data";
      return '<div class="list-row"><div class="lr-main"><b>' + esc(mo.label) + "</b>" +
        '<div class="muted tiny">' + esc(mo.provider) + " · berhasil " + (mo.ok || 0) +
        "x · gagal " + (mo.gagal || 0) + "x · tercatat permanen</div></div>" +
        admPill(label, kelas) + "</div>";
    }).join("");
    box.innerHTML =
      "<h4>Kesehatan Sistem</h4>" +
      '<div class="list-row"><div class="lr-main"><b>Server</b>' +
      '<div class="muted tiny">Terakhir dicek ' + esc(fmtWita(s.waktu)) + "</div></div>" +
      // FIX 2026-11-08 (audit putaran 2): pill membaca field status
      // SEBENARNYA dari respons server, bukan hardcoded sehat.
      (s.status === "normal" ? admPill("Berjalan normal", "ok")
        : admPill("Perlu dicek: " + (s.status || "tidak diketahui"), "warn")) + "</div>" +
      '<div class="list-row"><div class="lr-main"><b>Basis Data</b>' +
      '<div class="muted tiny">' + (s.basis_data === "postgres" ? "Terhubung ke Postgres (Neon)" : "Mode file lokal") + "</div></div>" +
      (s.basis_data === "postgres" ? admPill("Terhubung", "ok")
        : admPill("Mode file", "warn")) + "</div>" +
      "<h4>Model AI yang Dirotasi</h4>" +
      (modelRows || '<p class="muted">Belum ada data model.</p>') +
      "<h4>Sumber Daya</h4>" +
      '<div class="list-row"><div class="lr-main"><b>Jam dan bandwidth Render</b>' +
      "<div>" + renderDetail + "</div></div>" +
      renderPill + "</div>" +
      '<div class="list-row"><div class="lr-main"><b>Kredit Cerebras</b>' +
      "<div>" + cerebrasDetail + "</div></div>" +
      cerebrasPill + "</div>" +
      '<div style="margin:10px 0 4px"><label for="admCbSaldo">Saldo acuan Cerebras (USD)</label>' +
      '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:6px">' +
      '<input type="number" id="admCbSaldo" min="0" step="0.01" inputmode="decimal" placeholder="contoh: 245.00" style="flex:1;min-width:140px">' +
      '<button class="btn btn-primary" id="admCbSimpan" type="button">Simpan Saldo Acuan</button></div>' +
      '<p class="muted tiny" style="margin-top:6px">Lihat saldo terbaru di konsol Cerebras, lalu isi di sini. Total pemakaian tercatat saat disimpan menjadi titik nol perkiraan sisa.</p></div>' +
      '<div class="list-row"><div class="lr-main"><b>OpenRouter</b>' +
      "<div>" + orDetail + "</div></div>" +
      orPill + "</div>" +
      '<div class="list-row"><div class="lr-main"><b>Mesin Pencarian Mendalam</b>' +
      "<div>" + mdDetail + "</div></div>" +
      mdPill + "</div>" +
      "<h4>Antrean Pekerjaan</h4>" +
      '<p class="muted tiny">Yang jalan dan antre adalah kondisi saat panel dibuka; selesai dan gagal tercatat permanen di basis data.</p>' +
      barisAntre("AI Studio dan Tanya AI", an.ai) +
      barisAntre("Sitasi dari PDF", an.sitasi) +
      barisAntre("Convert File", an.convert) +
      "<h4>Pengaturan Kuota</h4>" +
      '<p class="muted">Jatah tamu: <b>' + esc(String((d.kuota || {}).tamu_per_hari || 5)) +
      'x per hari</b> untuk semua fitur. Jatah AI member: <b>' +
      esc(String((d.kuota || {}).member_ai_per_hari || 15)) +
      "x per hari</b>, reset pukul 00.00 WITA. Angka ini ketetapan sistem saat ini.</p>";
    const btnCb = $("#admCbSimpan");
    if (btnCb) btnCb.addEventListener("click", async function() {
      const inp = $("#admCbSaldo");
      const v = inp ? parseFloat(String(inp.value || "").replace(",", ".")) : NaN;
      if (isNaN(v) || v < 0) { toast("Isi saldo acuan yang valid dulu ya."); return; }
      btnCb.disabled = true;
      try {
        const rr = await apiPOST("/admin/cerebras-acuan", { saldo_usd: v });
        if (!rr.ok) throw 0;
        toast("Saldo acuan Cerebras tersimpan.");
        admMuatSistem();
      } catch (e2) {
        toast("Gagal menyimpan saldo acuan. Coba lagi ya.");
        btnCb.disabled = false;
      }
    });
    return true;
  } catch (e) {
    box.innerHTML = '<p class="pf-msg">Gagal memuat data sistem. Coba lagi ya.</p>';
    return false;
  }
}

async function admMuatPertanyaan() {
  const box = $("#admPertanyaan");
  if (!box) return false;
  try {
    const r = await apiJSON("/admin/pertanyaan");
    if (!r.ok) throw 0;
    const rows = r.data.pertanyaan || [];
    const badge = $("#admLogBadge");
    if (badge) badge.textContent = rows.length ? String(rows.length) : "";
    if (!rows.length) {
      box.innerHTML = '<p class="muted">Belum ada pertanyaan yang tercatat belum terjawab.</p>';
      return true;
    }
    box.innerHTML = rows.map(function(p, i) {
      return '<div class="q-item"><button class="q-head" type="button" data-q="' + i + '">' +
        '<span class="q-prev">' + esc(p.q) + '</span>' +
        '<span class="q-waktu">' + esc(fmtWita(p.ts)) + "</span></button>" +
        '<div class="q-full hidden" id="admQFull' + i + '">' + esc(p.q) +
        '<div class="muted tiny" style="margin-top:6px">Masuk ' + esc(fmtWita(p.ts)) +
        " · dicatat dari Tanya AI sebagai bahan perbaikan jawaban</div></div></div>";
    }).join("");
    $$("#admPertanyaan .q-head").forEach(function(h) {
      h.addEventListener("click", function() {
        const f = $("#admQFull" + h.dataset.q);
        if (f) f.classList.toggle("hidden");
      });
    });
    return true;
  } catch (e) {
    box.innerHTML = '<p class="pf-msg">Gagal memuat pertanyaan masuk.</p>';
    return false;
  }
}

let ADM_FB_ALL = null;
let ADM_FB_KAT = "";
const ADM_FB_LABEL = { kritik: "Kritik", saran: "Saran", fitur: "Permintaan Fitur", penilaian: "Penilaian" };
const ADM_FB_STATUS_LABEL = { baru: "Baru", dibaca: "Dibaca", ditindaklanjuti: "Ditindaklanjuti" };

async function admMuatFeedback() {
  const box = $("#admFeedback");
  if (!box) return;
  $$("#admFbFilter .chip").forEach(function(c) {
    if (c.dataset.fbikat) return;
    c.dataset.fbikat = "1";
    c.addEventListener("click", function() {
      ADM_FB_KAT = c.dataset.fbkat || "";
      $$("#admFbFilter .chip").forEach(function(x) {
        x.classList.toggle("active", x === c);
      });
      admRenderFeedback();
    });
  });
  try {
    const r = await apiJSON("/admin/feedback");
    if (!r.ok) throw 0;
    ADM_FB_ALL = r.data.feedback || [];
    admRenderFeedback();
  } catch (e) {
    box.innerHTML = '<p class="muted">Gagal memuat feedback.</p>';
  }
}

function admRenderFeedback() {
  const box = $("#admFeedback");
  if (!box || !ADM_FB_ALL) return;
  const semua = ADM_FB_ALL;
  let tot = 0, nB = 0, nFitur = 0;
  semua.forEach(function(f) {
    if (f.bintang) { tot += f.bintang; nB++; }
    if (f.kategori === "fitur") nFitur++;
  });
  const ring = $("#admFbRingkasan");
  if (ring) {
    ring.innerHTML = '<div class="fb-summary">' +
      '<div class="fb-sum"><b>' + semua.length + '</b><span>Feedback masuk</span></div>' +
      '<div class="fb-sum"><b>' + (nB ? String(Math.round(tot / nB * 10) / 10).replace(".", ",") : "0") + '</b><span>Rata rata penilaian dari 5</span></div>' +
      '<div class="fb-sum"><b>' + nFitur + '</b><span>Permintaan fitur</span></div>' +
      "</div>";
  }
  const rows = semua.filter(function(f) {
    return !ADM_FB_KAT || f.kategori === ADM_FB_KAT;
  });
  if (!rows.length) {
    box.innerHTML = '<p class="muted">Belum ada feedback di kategori ini.</p>';
    return;
  }
  box.innerHTML = rows.map(function(f, i) {
    const stars = f.bintang
      ? '<span class="fb-stars">' + "★".repeat(f.bintang) +
        '<span class="fb-stars-off">' + "★".repeat(5 - f.bintang) + "</span></span>"
      : "";
    const stKelas = f.status === "baru" ? "st-baru"
      : f.status === "dibaca" ? "st-dibaca" : "st-tindak";
    const opsi = ["baru", "dibaca", "ditindaklanjuti"].map(function(s) {
      return '<option value="' + s + '"' + (f.status === s ? " selected" : "") +
        ">" + ADM_FB_STATUS_LABEL[s] + "</option>";
    }).join("");
    return '<div class="fb-row">' +
      '<div class="fb-meta"><b>' + esc(f.username) + "</b> " +
        admPill(ADM_FB_LABEL[f.kategori] || f.kategori, f.kategori === "kritik" ? "warn" : "") +
        " " + stars +
        '<span class="q-waktu">' + esc(fmtWita(f.ts)) + "</span>" +
        '<select class="fb-status ' + stKelas + '" data-fbstatus="' + f.id + '">' + opsi + "</select>" +
      "</div>" +
      '<div class="fb-prev">' + esc(f.saran || f.fitur || "") + "</div>" +
      '<button class="dtoggle" type="button" data-fbdetail="' + i + '">Lihat Detail <span class="dchev">▾</span></button>' +
      '<div class="q-full hidden" id="admFbFull' + i + '">' +
        (f.saran ? "<p><b>Saran atau masukan:</b><br>" + esc(f.saran) + "</p>" : "") +
        (f.fitur ? "<p><b>Fitur yang diinginkan:</b><br>" + esc(f.fitur) + "</p>" : "") +
        '<div class="muted tiny">Kategori ' + esc(ADM_FB_LABEL[f.kategori] || f.kategori) +
          (f.bintang ? " · Penilaian " + f.bintang + " dari 5" : "") +
          " · Masuk " + esc(fmtWita(f.ts)) +
          ' · Status saat ini: <span id="admFbSt' + i + '">' +
          esc(ADM_FB_STATUS_LABEL[f.status] || f.status) + "</span></div>" +
      "</div></div>";
  }).join("");
  $$("#admFeedback [data-fbdetail]").forEach(function(b) {
    b.addEventListener("click", function() {
      const el = $("#admFbFull" + b.dataset.fbdetail);
      if (el) el.classList.toggle("hidden");
    });
  });
  $$("#admFeedback [data-fbstatus]").forEach(function(sel) {
    sel.addEventListener("change", async function() {
      const r = await apiPOST("/admin/feedback/status", {
        id: parseInt(sel.dataset.fbstatus, 10), status: sel.value
      });
      if (!r.ok) {
        toast("Gagal mengubah status feedback.");
        return;
      }
      sel.className = "fb-status " + (sel.value === "baru" ? "st-baru"
        : sel.value === "dibaca" ? "st-dibaca" : "st-tindak");
      const f = (ADM_FB_ALL || []).find(function(x) {
        return String(x.id) === String(sel.dataset.fbstatus);
      });
      if (f) f.status = sel.value;
      toast("Status feedback diperbarui.");
    });
  });
}

let ADM_PENG = null;
const ADM_TOOL_LABEL = { cari: "Cari Jurnal", sitasi: "Sitasi Otomatis", ai: "AI Bantu Nulis (AI Studio dan Tanya AI)", idejudul: "Ide Judul dan Research Gap", statistik: "Panduan Uji Statistik", sidang: "Simulasi Sidang", convert: "Convert File", terjemah: "Terjemah Abstrak" };

async function admMuatPengaturan() {
  try {
    const r = await apiJSON("/admin/pengaturan");
    if (!r.ok) throw 0;
    ADM_PENG = r.data.pengaturan;
    $("#admPgJudul").value = ADM_PENG.pengumuman.judul || "";
    $("#admPgIsi").value = ADM_PENG.pengumuman.isi || "";
    $("#admPgAktif").checked = !!ADM_PENG.pengumuman.aktif;
    admPratinjauPengumuman();
    admRenderSakelar();
    return true;
  } catch (e) {
    const m = $("#admPgMsg");
    if (m) m.textContent = "Gagal memuat pengaturan.";
    return false;
  }
}

function admPratinjauPengumuman() {
  const box = $("#admPgPratinjau");
  if (!box) return;
  const judul = ($("#admPgJudul").value || "").trim();
  const isi = ($("#admPgIsi").value || "").trim();
  const aktif = $("#admPgAktif").checked;
  if (!aktif || (!judul && !isi)) {
    box.innerHTML = '<p class="muted tiny">Pratinjau: banner tidak tampil di beranda (belum aktif atau masih kosong).</p>';
    return;
  }
  box.innerHTML = '<p class="muted tiny" style="margin-bottom:6px">Pratinjau banner:</p>' +
    '<div class="banner-pengumuman"><span class="banner-tag">Pengumuman</span><p>' +
    (judul ? "<b>" + esc(judul) + "</b> " : "") + esc(isi) + "</p></div>";
}

async function admSimpanPengumuman() {
  const msg = $("#admPgMsg");
  const r = await apiPOST("/admin/pengaturan", {
    pengumuman: {
      aktif: $("#admPgAktif").checked,
      judul: $("#admPgJudul").value,
      isi: $("#admPgIsi").value
    }
  });
  if (!r.ok) {
    msg.textContent = (r.data && r.data.error) || "Gagal menyimpan pengumuman.";
    return;
  }
  ADM_PENG = r.data.pengaturan;
  _infoSitusTs = 0;
  msg.textContent = ADM_PENG.pengumuman.aktif
    ? "Pengumuman tersimpan dan tampil di beranda."
    : "Pengumuman tersimpan (banner tidak ditampilkan).";
  toast("Pengaturan pengumuman tersimpan.");
}

async function admUbahPengaturan(ubah, konfirmasiMaint) {
  if (konfirmasiMaint && ubah.pemeliharaan) {
    if (!window.confirm("Nyalakan mode pemeliharaan? Member dan tamu tidak bisa memakai situs selama menyala. Admin tetap bisa masuk. Matikan lagi dari panel ini setelah selesai.")) return;
  }
  const r = await apiPOST("/admin/pengaturan", ubah);
  const msg = $("#admSwMsg");
  if (!r.ok) {
    if (msg) msg.textContent = (r.data && r.data.error) || "Gagal menyimpan pengaturan.";
    return;
  }
  ADM_PENG = r.data.pengaturan;
  _infoSitusTs = 0;
  admRenderSakelar();
  if (msg) msg.textContent = "Pengaturan tersimpan.";
}

function admRenderSakelar() {
  if (!ADM_PENG) return;
  const maint = $("#admSwMaint");
  if (maint) {
    maint.classList.toggle("on", !!ADM_PENG.pemeliharaan);
    maint.setAttribute("aria-checked", ADM_PENG.pemeliharaan ? "true" : "false");
  }
  const box = $("#admSwTools");
  if (!box) return;
  box.innerHTML = Object.keys(ADM_TOOL_LABEL).map(function(k) {
    const mati = !!ADM_PENG.tool_off[k];
    return '<div class="switch-row"><div class="lr-main"><b>' + ADM_TOOL_LABEL[k] + "</b>" +
      '<div class="muted tiny">' + (mati ? "Lagi nonaktif. Member melihat pemberitahuan tidak tersedia." : "Aktif dan bisa dipakai member.") + "</div></div>" +
      '<button class="kp-switch' + (mati ? "" : " on") + '" type="button" role="switch" aria-checked="' + (!mati) + '" data-toolswitch="' + k + '" aria-label="Sakelar ' + ADM_TOOL_LABEL[k] + '"></button></div>';
  }).join("");
  $$("#admSwTools [data-toolswitch]").forEach(function(sw) {
    sw.addEventListener("click", function() {
      const k = sw.dataset.toolswitch;
      const off = {};
      off[k] = !((ADM_PENG.tool_off || {})[k]);
      admUbahPengaturan({ tool_off: off }, false);
    });
  });
}

function admSalinTeks(t) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(t).then(
      function() { toast("Kode tersalin! "); },
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
    toast("Kode tersalin! ");
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
        '<div class="muted">Saldo AI: <b>' + esc(String(m.saldo)) + "</b> · sisa harian: " + esc(String(m.sisa_harian)) + "</div>" +
        '<div class="muted">Aktif terakhir: ' + esc(fmtWita(m.terakhir_login)) + "</div></div>" +
        '<input type="number" class="adm-jml" data-uid="' + esc(m.uid) + '" value="25" min="1" max="100000" aria-label="jumlah jatah AI">' +
        '<button class="btn btn-primary" data-grant="' + esc(m.uid) + '" data-nama="' + esc(nama) + '">Tambah</button></div>';
    }).join("");
  } catch (e) {
    box.innerHTML = '<p class="pf-msg">Pencarian gagal. Coba lagi ya.</p>';
  }
}

const _admGrantSibuk = {};
async function admGrant(uid, nama, btnEl) {
  // FIX 2026-11-08: satu grant per member dalam satu waktu; tombol
  // dinonaktifkan selama request (grant bernilai uang tidak boleh
  // terkirim dua kali karena klik ganda).
  if (_admGrantSibuk[uid]) return;
  const msg = $("#admGrantMsg");
  const inp = document.querySelector('.adm-jml[data-uid="' + uid + '"]');
  const jumlah = parseInt(inp && inp.value, 10) || 0;
  if (!(jumlah >= 1)) {
    msg.textContent = "Isi jumlah yang valid dulu ya.";
    return;
  }
  if (!confirm("Tambah " + jumlah + " jatah AI ke " + nama + "?")) return;
  _admGrantSibuk[uid] = true;
  if (btnEl) btnEl.disabled = true;
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
  } finally {
    delete _admGrantSibuk[uid];
    if (btnEl) btnEl.disabled = false;
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
    admGrant(grant.getAttribute("data-grant"), grant.getAttribute("data-nama"), grant);
  }
}

/* Pop up konfirmasi hapus akun di panel admin (perintah Sul 2026-10-19:
   menggantikan pola klik dua kali yang membingungkan). */
function popupKonfirmasiHapusAkun(uname, onYa) {
  if (document.getElementById("kpHapusPopup")) return;
  const el = document.createElement("div");
  el.className = "modal";
  el.id = "kpHapusPopup";
  el.style.alignItems = "center";
  el.style.padding = "16px";
  el.innerHTML =
    '<div class="modal-box" style="border-radius:20px;max-width:420px;text-align:center">' +
      '<div style="font-size:40px">' + tic("trash") + '</div>' +
      '<h3 style="margin:8px 0 0">Hapus akun @' + esc(uname) + "?</h3>" +
      '<p class="muted" style="text-align:left">Akun ini akan dihapus <b>permanen</b> beserta data terkaitnya (token, kuota, koleksi, riwayat). Tindakan ini <b>tidak bisa dibatalkan</b>.</p>' +
      '<button class="btn btn-danger btn-block" id="kpHapusYa" type="button">Ya, Hapus Permanen</button>' +
      '<button class="btn btn-block" id="kpHapusBatal" type="button" style="margin-top:9px">Batal</button>' +
    "</div>";
  document.body.appendChild(el);
  const tutup = () => { el.remove(); window.removeEventListener("hashchange", tutup); };
  el.addEventListener("click", e => { if (e.target === el) tutup(); });
  el.querySelector("#kpHapusBatal").addEventListener("click", tutup);
  el.querySelector("#kpHapusYa").addEventListener("click", function() {
    tutup();
    onYa();
  });
  window.addEventListener("hashchange", tutup);
}

function pageAdmin() {
  if (!AUTH.login) {
    view().innerHTML =
      "<h2>" + tic('shield') + " Panel Admin</h2>" +
      '<div class="card center"><p class="muted">Kamu lagi mode tamu. Halaman admin hanya bisa dibuka setelah login.</p>' +
      '<a class="btn btn-primary btn-block" href="#/masuk">' + tic("key") + ' Masuk</a></div>';
    return;
  }
  if (!AUTH.admin) {
    view().innerHTML =
      "<h2>" + tic('shield') + " Panel Admin</h2>" +
      '<div class="card center"><p class="muted">Halaman ini khusus akun admin. Kalau kamu merasa ini salah, hubungi admin lewat halaman Bantuan ya.</p>' +
      '<a class="btn btn-primary btn-block" href="#/beranda">Ke Beranda</a></div>';
    return;
  }
  view().innerHTML =
    '<div class="adm-topbar">' +
      '<img src="maskot.jpg" class="adm-mascot" alt="Maskot KhairyPustaka">' +
      '<div class="adm-brand"><h2>Panel Admin KhairyPustaka</h2>' +
      '<p class="muted">Kelola member, sistem, pengumuman, dan log</p></div>' +
      '<span class="badge-admin">ADMIN</span>' +
      '<a class="btn btn-ghost adm-keluar" href="#/beranda">Keluar</a>' +
    "</div>" +
    '<div class="tabs adm-tabs" role="tablist">' +
      '<button class="tab active" data-atab="ringkasan" type="button">Ringkasan</button>' +
      '<button class="tab" data-atab="member" type="button">Member</button>' +
      '<button class="tab" data-atab="sistem" type="button">Sistem</button>' +
      '<button class="tab" data-atab="pengumuman" type="button">Pengumuman</button>' +
      '<button class="tab" data-atab="log" type="button">Log <span class="adm-log-badge" id="admLogBadge"></span></button>' +
    "</div>" +

    '<div class="adm-pane" id="admPane-ringkasan">' +
      '<div class="card"><h3>' + tic("chart") + ' Ringkasan</h3><div id="admStat"><p class="muted">Memuat data...</p></div></div>' +
    "</div>" +

    '<div class="adm-pane hidden" id="admPane-member">' +
      '<div class="card"><h3>' + tic("ticket") + ' Kode Jatah AI</h3>' +
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
      '<div class="card"><h3>' + tic("doc") + ' Daftar Kode</h3><div id="admKodeList"><p class="muted">Memuat...</p></div>' +
      '<button class="btn btn-block" id="admKodeRefresh">Muat Ulang</button></div>' +
      '<div class="card"><h3>' + tic("plus") + ' Tambah Jatah AI</h3>' +
      '<p class="muted">Cari member lalu tambah saldo jatah AI langsung ke akunnya.</p>' +
      '<input type="text" id="admCari" placeholder="ketik username, nama, atau email" autocomplete="off">' +
      '<button class="btn btn-block" id="admCariBtn">Cari Member</button>' +
      '<div id="admCariHasil"></div>' +
      '<p class="pf-msg" id="admGrantMsg"></p></div>' +
      '<div class="card"><h3>' + tic("trash") + ' Hapus Akun</h3>' +
      '<p class="muted">Hapus permanen satu akun dari username-nya, misalnya akun uji. Data terkaitnya (token, kuota, koleksi, riwayat) ikut dibersihkan. Akun admin tidak bisa dihapus. Tindakan ini tidak bisa dibatalkan.</p>' +
      '<input type="text" id="admHapusUser" placeholder="username akun yang mau dihapus" autocomplete="off">' +
      '<button class="btn btn-danger btn-block" id="admHapusBtn">Hapus Akun Ini</button>' +
      '<p class="pf-msg" id="admHapusMsg"></p></div>' +
    "</div>" +

    '<div class="adm-pane hidden" id="admPane-sistem">' +
      '<div class="card"><h3>' + tic("shield") + ' Sistem</h3><div id="admSistem"><p class="muted">Memuat data...</p></div></div>' +
      '<div class="card"><h3>' + tic("download") + ' Backup Data</h3>' +
      '<p class="muted">Unduh seluruh data aplikasi (member, kuota, koleksi, riwayat, kode) sebagai satu file JSON. Simpan di tempat aman sebagai cadangan.</p>' +
      '<button class="btn btn-block" id="admBackupBtn">' + tic("download") + ' Download Backup Sekarang</button>' +
      '<p class="pf-msg" id="admBackupMsg"></p></div>' +
    "</div>" +

    '<div class="adm-pane hidden" id="admPane-pengumuman">' +
      '<div class="card"><h3>' + tic("bell") + ' Pengumuman Beranda</h3>' +
      '<div class="field"><label for="admPgJudul">Judul pengumuman</label>' +
      '<input id="admPgJudul" maxlength="120" placeholder="Contoh: Jadwal pemeliharaan singkat"></div>' +
      '<div class="field"><label for="admPgIsi">Isi pengumuman</label>' +
      '<textarea id="admPgIsi" maxlength="500" style="min-height:76px" placeholder="Tulis isi pengumuman yang tampil di banner beranda"></textarea></div>' +
      '<label class="checkline"><input type="checkbox" id="admPgAktif"> Tampilkan banner pengumuman di beranda</label>' +
      '<div id="admPgPratinjau" style="margin:10px 0"></div>' +
      '<button class="btn" id="admPgSimpan" type="button">Simpan Pengumuman</button>' +
      '<p class="pf-msg" id="admPgMsg"></p></div>' +
      '<div class="card"><h3>' + tic("wrench") + ' Mode Pemeliharaan dan Sakelar Tool</h3>' +
      '<div class="switch-row"><div class="lr-main"><b>Mode pemeliharaan</b>' +
      '<div class="muted tiny">Saat menyala, member dan tamu tidak bisa memakai situs. Admin tetap bisa masuk dan memakai situs seperti biasa.</div></div>' +
      '<button class="kp-switch danger" id="admSwMaint" type="button" role="switch" aria-checked="false" aria-label="Mode pemeliharaan"></button></div>' +
      '<div id="admSwTools"><p class="muted">Memuat sakelar...</p></div>' +
      '<p class="pf-msg" id="admSwMsg"></p></div>' +
    "</div>" +

    '<div class="adm-pane hidden" id="admPane-log">' +
      '<div class="card"><h3>' + tic("chat") + ' Feedback Member</h3>' +
      '<div class="chip-row" id="admFbFilter">' +
        '<button type="button" class="chip active" data-fbkat="">Semua</button>' +
        '<button type="button" class="chip" data-fbkat="kritik">Kritik</button>' +
        '<button type="button" class="chip" data-fbkat="saran">Saran</button>' +
        '<button type="button" class="chip" data-fbkat="fitur">Permintaan Fitur</button>' +
        '<button type="button" class="chip" data-fbkat="penilaian">Penilaian</button>' +
      '</div>' +
      '<div id="admFbRingkasan"></div>' +
      '<div id="admFeedback"><p class="muted">Memuat...</p></div></div>' +
      '<div class="card"><h3>' + tic("help") + ' Pertanyaan Masuk</h3>' +
      '<p class="muted">Pertanyaan yang belum bisa dijawab Tanya AI, dicatat otomatis sebagai bahan perbaikan. Ketuk satu pertanyaan buat membaca teks lengkapnya.</p>' +
      '<div id="admPertanyaan"><p class="muted">Memuat...</p></div></div>' +
    "</div>";
  let admSistemDimuat = false, admPertanyaanDimuat = false, admPengaturanDimuat = false;
  $$("#view .adm-tabs .tab").forEach(function(tb) {
    tb.addEventListener("click", function() {
      $$("#view .adm-tabs .tab").forEach(function(x) {
        x.classList.toggle("active", x === tb);
      });
      ["ringkasan", "member", "sistem", "pengumuman", "log"].forEach(function(n) {
        const p = $("#admPane-" + n);
        if (p) p.classList.toggle("hidden", n !== tb.dataset.atab);
      });
      // FIX 2026-11-08 (audit putaran 2): flag sekali-muat diset
      // HANYA setelah muatan BERHASIL. Pola lama menyetel flag sebelum
      // muatan async selesai, jadi satu kegagalan jaringan mengunci
      // tab sampai halaman dimuat ulang.
      if (tb.dataset.atab === "sistem" && !admSistemDimuat) {
        admMuatSistem().then(function(ok) {
          if (ok !== false) admSistemDimuat = true;
        });
      }
      if (tb.dataset.atab === "log" && !admPertanyaanDimuat) {
        admMuatPertanyaan().then(function(ok) {
          if (ok !== false) admPertanyaanDimuat = true;
        });
        admMuatFeedback();
      }
      if (tb.dataset.atab === "pengumuman" && !admPengaturanDimuat) {
        admMuatPengaturan().then(function(ok) {
          if (ok !== false) admPengaturanDimuat = true;
        });
      }
    });
  });
  admMuatStatistik();
  admMuatKode();
  admMuatPertanyaan(); // sekalian buat lencana hitung di tab Log
  // Fase 4: pratinjau + simpan pengumuman dan sakelar pemeliharaan
  ["#admPgJudul", "#admPgIsi", "#admPgAktif"].forEach(function(sel) {
    const el = $(sel);
    if (el) {
      el.addEventListener("input", admPratinjauPengumuman);
      el.addEventListener("change", admPratinjauPengumuman);
    }
  });
  const pgSimpan = $("#admPgSimpan");
  if (pgSimpan) pgSimpan.addEventListener("click", admSimpanPengumuman);
  const swMaint = $("#admSwMaint");
  if (swMaint) swMaint.addEventListener("click", function() {
    admUbahPengaturan({ pemeliharaan: !(ADM_PENG && ADM_PENG.pemeliharaan) }, true);
  });
  $$("#admNomChips .chip").forEach(function(c) {
    c.addEventListener("click", function() {
      $("#admKodeNom").value = c.dataset.nom;
    });
  });
  $("#admKodeBtn").addEventListener("click", async function() {
    // FIX 2026-11-08: kunci tombol selama generate kode berjalan
    // (klik ganda sebelumnya bisa membuat dua batch kode).
    const btnKode = this;
    if (btnKode.disabled) return;
    btnKode.disabled = true;
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
    } finally {
      btnKode.disabled = false;
    }
  });
  $("#admKodeRefresh").addEventListener("click", admMuatKode);
  $("#admCariBtn").addEventListener("click", admCariMember);
  $("#admBackupBtn").addEventListener("click", async function() {
    const msg = $("#admBackupMsg");
    msg.textContent = "Menyiapkan backup...";
    const r = await apiDownload("/admin/backup", {}, "khairypustaka-backup.json");
    msg.textContent = r && r.ok ? "Backup terunduh. Simpan file-nya baik-baik ya. " : "Gagal membuat backup. Coba lagi ya.";
  });
  $("#admHapusBtn").addEventListener("click", function() {
    const msg = $("#admHapusMsg");
    const uname = ($("#admHapusUser").value || "").trim().toLowerCase();
    if (!uname) { msg.textContent = "Isi dulu username-nya."; return; }
    msg.textContent = "";
    popupKonfirmasiHapusAkun(uname, async function() {
      msg.textContent = "Menghapus akun @" + uname + "...";
      const r = await apiPOST("/admin/hapus-user", { username: uname });
      if (!r.ok) { msg.textContent = (r.data && (r.data.pesan || r.data.error)) || "Gagal menghapus."; return; }
      msg.innerHTML = tic("check") + " Akun @" + uname + " sudah dihapus permanen.";
      $("#admHapusUser").value = "";
      admMuatStatistik();
    });
  });
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
    view().innerHTML = '<div class="err-box">Yah, halamannya error. Coba refresh ya.</div>';
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
  muatInfoSitus();
  setTimeout(cekPopupFeedback, 800);
}

/* K-1 (audit 2026-10-17): referral HANYA dihitung untuk akun yang login
   dan emailnya terverifikasi. Kode dari ?ref= disimpan dulu di localStorage;
   diklaim via flushRefPending() setelah user login, atau otomatis saat
   verifikasi email (titik di mana backend memproses pending_ref). Tamu
   tidak pernah memicu penghitungan. */
function flushRefPending() {
  let kode = null;
  try { kode = localStorage.getItem("kp_ref_pending"); } catch (e) {}
  if (!kode || !AUTH.login) return;
  apiJSON("/ref?kode=" + encodeURIComponent(kode)).then(d => {
    // bersihkan bila server sudah memproses (ok) ATAU menolak final
    // (kode tak dikenal / duplikat / milik sendiri). 401/403 (belum
    // login / belum verifikasi) -> simpan, coba lagi nanti.
    if (d && d.ok) {
      try { localStorage.removeItem("kp_ref_pending"); } catch (e) {}
      if (d.milestone) toast("Yeay, temanmu dapat bonus referral! ");
    }
  }).catch(() => {});
}

/* ---------- PWA: service worker + pasang aplikasi (2026-10-18) ---------- */
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  });
}
let _pwaPrompt = null;
window.addEventListener("beforeinstallprompt", e => {
  e.preventDefault();
  _pwaPrompt = e;
});
window.addEventListener("appinstalled", () => { _pwaPrompt = null; });

async function pasangAplikasi() {
  if (_pwaPrompt) {
    _pwaPrompt.prompt();
    try { await _pwaPrompt.userChoice; } catch (e) {}
    _pwaPrompt = null;
    return;
  }
  const ios = /iphone|ipad|ipod/i.test(navigator.userAgent || "");
  toast(ios
    ? "Ketuk tombol Bagikan di Safari, lalu pilih \"Tambah ke Layar Utama\" "
    : "Buka menu titik tiga di browser, lalu pilih \"Pasang aplikasi\" atau \"Tambah ke Layar Utama\" ");
}

function kartuPasangAppHTML() {
  return '<div class="card center"><h3>' + tic("download") + ' Pasang Aplikasi</h3>' +
    '<p class="muted">Pasang KhairyPustaka ke layar utama HP-mu. Dibuka kayak aplikasi beneran, lebih cepat dan praktis.</p>' +
    '<button class="btn btn-primary btn-block" id="btnPasangApp" type="button">' + tic("download") + ' Pasang Sekarang</button>' +
    '<p class="muted tiny" id="pasangAppInfo"></p></div>';
}

function ikatTombolPasang() {
  const b = $("#btnPasangApp");
  if (!b) return;
  if (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) {
    b.innerHTML = tic("check") + " Sudah Terpasang";
    b.disabled = true;
    return;
  }
  const info = $("#pasangAppInfo");
  if (info && /iphone|ipad|ipod/i.test(navigator.userAgent || "")) {
    info.textContent = "Di iPhone: ketuk Bagikan di Safari, lalu Tambah ke Layar Utama.";
  }
  b.addEventListener("click", pasangAplikasi);
}

/* ---------- init ---------- */
document.addEventListener("DOMContentLoaded", async () => {
  // referral: ?ref=KP-XXXXXX di URL -> simpan, klaim setelah login/verifikasi
  try {
    const refKode = new URLSearchParams(location.search).get("ref");
    if (refKode) {
      localStorage.setItem("kp_ref_pending", refKode.trim().slice(0, 24));
      const u = new URL(location.href);
      u.searchParams.delete("ref");
      history.replaceState(null, "", u.pathname + u.search + location.hash);
    }
  } catch (e) {}
  $("#menuBtn").addEventListener("click", () => $("#mainNav").classList.toggle("open"));
  const bk = document.getElementById("btnBack");
  if (bk) bk.addEventListener("click", pergiKembali);
  // Tombol Kembali di dalam konten halaman (kelas .btn-back selain
  // tombol global #btnBack, mis. di halaman Mendalam): delegasi klik
  // dokumen. Menggantikan atribut onclick inline lama yang diblokir
  // CSP situs (script-src tanpa 'unsafe-inline').
  document.addEventListener("click", e => {
    const bb = e.target && e.target.closest ? e.target.closest(".btn-back") : null;
    if (bb && bb.id !== "btnBack") pergiKembali();
  });
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


/* ---------- Banner versi baru (buat aplikasi terpasang/PWA) ----------
   Mengecek berkala apakah index.html di server sudah menunjuk app.js versi
   lain dari yang sedang jalan. Bila beda, tampilkan banner berisi tombol
   Perbarui yang memuat ulang aplikasi, jadi pengguna tidak perlu refresh
   manual (di aplikasi terpasang memang tidak ada tombol refresh). */
(function () {
  function versiDari(teks) {
    var m = /app\.js\?v=(\d+)/.exec(teks || "");
    return m ? m[1] : null;
  }
  var skrip = document.querySelector('script[src*="app.js"]');
  var versiIni = skrip ? versiDari(skrip.getAttribute("src")) : null;
  if (!versiIni) return;
  var sedangTampil = false;

  function tampilkanBanner(versiBaru) {
    if (sedangTampil || document.getElementById("kpBannerVersi")) return;
    try { if (sessionStorage.getItem("kp_ver_ditutup") === versiBaru) return; } catch (e) {}
    sedangTampil = true;
    var b = document.createElement("div");
    b.id = "kpBannerVersi";
    b.setAttribute("style", "position:fixed;left:12px;bottom:calc(14px + env(safe-area-inset-bottom, 0px));z-index:400;background:#0d2438;color:#fff;border:1px solid rgba(169,195,207,.35);border-radius:16px;padding:10px 12px;display:flex;align-items:center;gap:10px;box-shadow:0 10px 28px rgba(0,0,0,.28);font-size:14px;max-width:calc(100vw - 96px);");
    var label = document.createElement("span");
    label.textContent = "Ada versi baru KhairyPustaka";
    label.setAttribute("style", "font-weight:600;line-height:1.3;");
    var btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "Perbarui";
    btn.setAttribute("style", "background:#2dd4d8;color:#04262e;border:0;border-radius:10px;padding:8px 12px;font-weight:700;font-size:14px;cursor:pointer;white-space:nowrap;");
    btn.addEventListener("click", function () { location.reload(); });
    var tutup = document.createElement("button");
    tutup.type = "button";
    tutup.textContent = "✕";
    tutup.setAttribute("aria-label", "Tutup");
    tutup.setAttribute("style", "background:transparent;color:#fff;border:0;font-size:15px;padding:4px 6px;cursor:pointer;opacity:.85;");
    tutup.addEventListener("click", function () {
      try { sessionStorage.setItem("kp_ver_ditutup", versiBaru); } catch (e) {}
      if (b.parentNode) b.parentNode.removeChild(b);
      sedangTampil = false;
    });
    b.appendChild(label);
    b.appendChild(btn);
    b.appendChild(tutup);
    document.body.appendChild(b);
  }

  function cekVersi() {
    fetch("/", { credentials: "same-origin" })
      .then(function (r) { return r.text(); })
      .then(function (html) {
        var v = versiDari(html);
        if (v && v !== versiIni) tampilkanBanner(v);
      })
      .catch(function () {});
  }
  function cekDuaKali() { cekVersi(); setTimeout(cekVersi, 8000); }
  document.addEventListener("visibilitychange", function () { if (!document.hidden) cekDuaKali(); });
  window.addEventListener("focus", cekDuaKali);
  setInterval(cekVersi, 15 * 60 * 1000);
  setTimeout(cekVersi, 30000);
})();
