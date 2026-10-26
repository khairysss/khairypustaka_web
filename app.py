#!/usr/bin/env python3
"""KhairyPustaka Web — backend Flask.

Satu otak dengan bot Telegram: mengimpor modul bot di /khairypustaka-web
apa adanya (tanpa lapisan Telegram). Dipasang di VPS via systemd,
disajikan Caddy di pustaka.khairys.web.id -> /api/*, port 8100.

PENTING: os.chdir ke direktori app DILAKUKAN PALING AWAL, sebelum import
modul bot, karena skripsi_ai memakai path relatif "data/ai_quota.json".
"""
import os
import re
import sys
import hashlib

_HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(_HERE)
sys.path.insert(0, _HERE)

import asyncio
import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime
from io import BytesIO

from flask import Flask, jsonify, request, g, send_file, Response, redirect

import auth as authmod
import dbstore
import mailer as mailmod
import skripsi_ai as sai
import collection as koleksi
import stats as stats_mod
import kemiripan as kemiripan_mod
import matrix as matriks_mod
import template_doc
import topics as topik_mod
import tanya as tanya_mod
import history as histmod
from cite import STYLE_ORDER, cite_in, style_label
from citefind import parse_citation, split_citations, title_match
from metafetch import fetch_metadata_from_url, doi_in_text
import impor_ref
from translate import looks_indonesian, paraphrase_id, translate_en_id
from publisher import aggregate_publishers, enrich_journal_profiles
from search_id import (
    enrich_sinta_ranks, search_garuda, search_indonesia,
)
from search_intl import (
    fetch_by_doi, recommend_journals, search_international, search_openalex,
    search_authors, publisher_homepages,
)
import referral as refmod
import redeem as redmod
import jadwal_web
import convert as convert_mod
import ilovepdf_api as ilovepdf

try:
    import premium_pay as ppay
except Exception as e:  # pembayaran opsional di web
    ppay = None
    logging.getLogger("kpweb").warning("premium_pay tidak bisa diimport: %s", e)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("kpweb")

app = Flask(__name__, static_folder=None)
# Batas ukuran body request (2 MB) — tolak payload raksasa dengan 413
# sebelum Werkzeug menelannya ke RAM (mitigasi OOM/DoS).
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

SID_COOKIE = "kp_sid"
SID_TTL = 2 * 365 * 24 * 3600  # 2 tahun

AI_KINDS = ("kerangka", "abstrak", "kuesioner", "latarbelakang", "simpulan",
            "sidang", "kisi", "metode", "pembahasan", "bab2",
            "ringkas", "interpretasi")  # STRATEGI1: AI tempel

# Ekonomi AI (audit 2026-10-17): batas input per kind di endpoint.
# _prompt() di skripsi_ai memakai maksimal 1000 karakter teks/extra, jadi
# batas di sini longgar di atas pemakaian nyata — hanya memotong input
# raksasa yang tidak menambah kualitas jawaban.
AI_KIND_INPUT_CAP = {
    "kerangka": 2000, "abstrak": 2000, "kuesioner": 2000,
    "latarbelakang": 2000, "simpulan": 2000, "sidang": 2000,
    "kisi": 2000, "metode": 2000,
    "pembahasan": 4000, "bab2": 4000, "ringkas": 4000, "interpretasi": 4000,
}

# peta bidang Indonesia -> Inggris untuk publisher internasional (dari bot.py)
_BIDANG_EN = {
    "kesehatan": "health", "kedokteran": "medicine", "keperawatan": "nursing",
    "kebidanan": "midwifery", "farmasi": "pharmacy", "gizi": "nutrition",
    "pendidikan": "education", "hukum": "law", "ekonomi": "economics",
    "akuntansi": "accounting", "manajemen": "management",
    "teknik": "engineering", "informatika": "computer science",
    "komputer": "computer science", "pertanian": "agriculture",
    "peternakan": "animal husbandry", "perikanan": "fisheries",
    "kehutanan": "forestry", "lingkungan": "environment",
    "psikologi": "psychology", "sosiologi": "sociology",
    "antropologi": "anthropology", "politik": "political science",
    "sosial": "social sciences", "komunikasi": "communication",
    "pariwisata": "tourism", "matematika": "mathematics", "fisika": "physics",
    "kimia": "chemistry", "biologi": "biology", "bahasa": "language",
    "sastra": "literature", "sejarah": "history", "filsafat": "philosophy",
    "seni": "arts", "olahraga": "sports", "arsitektur": "architecture",
    "pangan": "food science", "administrasi": "administration",
}


# ------------------------------------------- catat pemakaian fitur non-AI
_USAGE_PATH = "data/usage.json"
_USAGE_LOCK = threading.Lock()


def _today_wita():
    """Tanggal hari ini zona WITA (ISO 'YYYY-MM-DD')."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Makassar")).date().isoformat()
    except Exception:
        return date.today().isoformat()


def track_penggunaan(tool):
    """Catat pemakaian fitur non-AI ke data/usage.json.

    Bentuk: {tool: {"total": N, "days": {"YYYY-MM-DD": n}}}.
    Fail-safe: tidak pernah raise, tidak mengganggu request.
    """
    try:
        name = (tool or "").strip().lower()
        if not name:
            return
        with _USAGE_LOCK:
            d = dbstore.load_json(_USAGE_PATH, {})
            if not isinstance(d, dict):
                d = {}
            t = d.get(name)
            if not isinstance(t, dict):
                t = {"total": 0, "days": {}}
            days = t.get("days")
            if not isinstance(days, dict):
                days = {}
            today = _today_wita()
            days[today] = int(days.get(today, 0) or 0) + 1
            t["total"] = int(t.get("total", 0) or 0) + 1
            t["days"] = days
            d[name] = t
            # S-2: statistik pemakaian = best-effort (kehilangan satu sampel
            # tidak merusak konsistensi; divergensi tercatat ERROR di log).
            dbstore.save_json_best_effort(_USAGE_PATH, d)
    except Exception as e:
        log.warning("track_penggunaan gagal: %s", e)


# ------------------------------------------- kuota harian tamu (gabungan)
# Keputusan Sul 2026-10-18: tamu memakai SATU ember gabungan
# untuk SEMUA fitur (AI + non-AI), disimpan di skripsi_ai (guest_all_*).
# Revisi Sul 2026-10-19: ember tamu diturunkan jadi 5x/hari.
# Login = non-AI unlimited + jatah AI 10x/hari (jalur terpisah).


def _is_tamu():
    """True bila request ini dari tamu (belum login)."""
    try:
        return (getattr(g, "uid", "") or "").startswith("web_")
    except Exception:
        return False


def pesan_ai_habis():
    """Pesan ramah saat jatah habis (tamu: ember gabungan 5x/hari
    semua fitur; login: jatah AI 10x/hari)."""
    if _is_tamu():
        return ("Jatah harian tamu habis (5x/hari buat semua fitur). "
                "Login gratis biar fitur non-AI sepuasnya! ✨")
    return ("Jatah AI gratis harianmu habis (10x/hari). "
            "Besok reset lagi.")


def kuota_tamu(tool):
    """Batasi tamu: SATU ember gabungan 5x/hari untuk semua fitur
    (keputusan Sul 2026-10-18, angka direvisi 2026-10-19). Pemakaian non-AI di sini dan pemakaian
    AI lewat _ai_consume_uid mengurangi ember yang sama di skripsi_ai
    (guest_all_*).

    Kembalikan None bila lolos (sekaligus catat hit untuk tamu),
    atau tuple (response, 429) bila ember gabungan habis.
    Login = unlimited (langsung None, tanpa pencatatan).
    DB yang gagal total melempar DBUnavailable -> 503 (S-2 audit);
    error lain fail-open seperti perilaku lama.
    """
    name = (tool or "").strip().lower()
    if not name or not _is_tamu():
        return None
    try:
        if sai.guest_all_try_consume(g.uid, _ip()):
            return None
    except dbstore.DBUnavailable:
        raise
    except Exception as e:
        log.warning("kuota_tamu gagal: %s", e)
        return None
    return (jsonify({"error": "kuota_tamu_habis",
                     "pesan": "Jatah harian tamu habis (5x/hari buat semua fitur). Login gratis biar fitur non-AI sepuasnya!"}), 429)


# ---------------------------------------------------------------- sesi
# Format sid tamu yang sah: uuid 32 hex ATAU sid per-IP "ip"+32 hex.
# (Perbaikan audit fungsi 2026-10-19: pola lama hanya menerima 32 hex,
# sehingga cookie tamu per-IP yang diterbitkan server sendiri SELALU
# ditolak dan identitas tamu dihitung ulang dari IP tiap request —
# job convert tamu lalu "tidak ditemukan" saat IP klien berubah.)
_SID_RE = re.compile(r"^(?:[0-9a-f]{32}|ip[0-9a-f]{32})$")


@app.before_request
def _session():
    raw = request.cookies.get(SID_COOKIE)
    g.new_sid = False
    if raw and _SID_RE.match(raw):
        sid = raw
    else:
        # Tanpa cookie valid: identitas per-IP (bukan uuid acak).
        # Sebelumnya tiap request tanpa cookie dapat uuid baru -> kuota tamu
        # (AI 5x/hari, fitur 10x/hari) bisa di-bypass total hanya dengan
        # membuang cookie. Dengan identitas IP, pembuang cookie berbagi
        # 1 ember kuota per IP. Batas bisnisnya tetap sama.
        iph = hashlib.sha256(_ip().encode()).hexdigest()
        sid = "ip" + iph[2:34]  # 32 hex, prefix "ip" = namespace tamu-tanpa-cookie
        g.new_sid = True
    g.sid = sid
    # Login: cookie kp_login valid -> identitas user_<id>; kalau tidak -> tamu.
    g.user = None
    g.login_uid = authmod.parse_login_cookie(request.cookies.get(authmod.LOGIN_COOKIE))
    if g.login_uid:
        g.user = authmod.get_user(g.login_uid)
    if g.user:
        g.uid = g.login_uid
    else:
        g.uid = "web_" + sid


@app.after_request
def _set_sid(resp):
    if getattr(g, "new_sid", False):
        resp.set_cookie(SID_COOKIE, g.sid, max_age=SID_TTL,
                        httponly=True, samesite="Lax", secure=True)
    # Security headers — meniru set Caddy di VPS lama (audit 2026-10-12),
    # karena di Render tidak ada reverse proxy yang memasangnya.
    resp.headers.setdefault("Strict-Transport-Security",
                            "max-age=31536000; includeSubDomains")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy",
                            "camera=(), microphone=(), geolocation=()")
    return resp


def _set_login_cookie(resp, uid):
    resp.set_cookie(authmod.LOGIN_COOKIE, authmod.make_login_cookie(uid),
                    max_age=authmod.LOGIN_TTL, httponly=True,
                    samesite="Lax", secure=True)


def _do_login(uid, guest_uid):
    """Migrasi data tamu -> user, catat login. Dipakai semua jalur login."""
    try:
        authmod.migrate_guest(guest_uid, uid)
    except Exception as e:
        log.warning("migrasi tamu->user gagal: %s", e)
    authmod.touch_login(uid)


def _google_redirect_uri():
    base = (os.environ.get("WEB_BASE_URL") or "https://pustaka.khairys.web.id").rstrip("/")
    return base + "/api/auth/google/callback"


# ---------------------------------------------------------------- rate limit
_RL = {}          # {ip: [timestamps]}
_RL_LOCK = threading.Lock()
RL_LIMIT, RL_WINDOW = 60, 3600  # 60x/jam/IP


def rate_limited(ip):
    now = time.time()
    with _RL_LOCK:
        hits = [t for t in _RL.get(ip, []) if now - t < RL_WINDOW]
        if len(hits) >= RL_LIMIT:
            _RL[ip] = hits
            return True
        hits.append(now)
        _RL[ip] = hits
        # I2 (audit 2026-10-13): sapu key kedaluwarsa bila tabel membesar
        if len(_RL) > 10000:
            for k in [k for k, v in _RL.items()
                      if not v or now - v[-1] >= RL_WINDOW]:
                del _RL[k]
        return False


# Rentang IP publik Cloudflare (sumber: https://www.cloudflare.com/ips,
# disalin 2026-10-17). Header CF-Connecting-IP HANYA dipercaya bila TCP peer
# (request.remote_addr) berasal dari rentang ini — yaitu koneksi langsung
# dari edge Cloudflare. Koneksi dari IP privat/loopback (routing internal
# Render) juga diterima karena di topologi Render container hanya bisa
# dihubungi lewat router internal Render; Cloudflare menimpa header ini
# dengan IP pengunjung asli selama proxying aktif, sehingga di domain
# resmi nilainya benar. Peer internet lain yang mengirim header ini
# manual -> DIABAIKAN (T-1 audit 2026-10-17).
#
# Catatan X-Forwarded-For: waitress >= 2.0 menghapus SEMUA header
# X-Forwarded-* secara default (clear_untrusted_proxy_headers=True) kecuali
# trusted_proxy dikonfigurasi — jadi kode di sini SENGAJA tidak membaca XFF
# sama sekali (S-8 audit 2026-10-17: komentar lama yang mengklaim fallback
# XFF sudah tidak akurat dan dihapus).
_CF_NETWORKS = [
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22",
    "103.31.4.0/22", "141.101.64.0/18", "108.162.192.0/18",
    "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22",
    "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32",
    "2405:b500::/32", "2405:8100::/32", "2a06:98c0::/29",
    "2c0f:f248::/32",
]


def _cf_networks():
    import ipaddress
    if not hasattr(_cf_networks, "_nets"):
        _cf_networks._nets = [ipaddress.ip_network(c) for c in _CF_NETWORKS]
    return _cf_networks._nets


def _peer_may_set_cf_header():
    """True bila peer TCP boleh dipercaya memasang CF-Connecting-IP."""
    import ipaddress
    try:
        peer = ipaddress.ip_address((request.remote_addr or "").strip())
    except ValueError:
        return False
    if peer.is_private or peer.is_loopback:
        return True  # routing internal (Render) / dev lokal
    return any(peer in net for net in _cf_networks())


def _cf_trusted_ip():
    """Nilai CF-Connecting-IP bila peer-nya tepercaya, else None."""
    cf = (request.headers.get("CF-Connecting-IP") or "").strip()
    if not cf:
        return None
    import ipaddress
    try:
        ipaddress.ip_address(cf)  # validasi format; nilai aneh -> tolak
    except ValueError:
        return None
    return cf if _peer_may_set_cf_header() else None


def _ip():
    # Identitas klien untuk kuota tamu & rate limit. Header CF-Connecting-IP
    # hanya dipakai bila peer TCP tepercaya (lihat _peer_may_set_cf_header);
    # kalau tidak, pakai remote_addr apa adanya. X-Forwarded-For SENGAJA
    # tidak dibaca (dihapus waitress; lihat catatan di atas).
    return _cf_trusted_ip() or request.remote_addr or "unknown"


def with_rl(view):
    def wrapped(*a, **kw):
        if rate_limited(_ip()):
            return jsonify({"error": "rate_limit",
                            "pesan": "Terlalu banyak permintaan, coba lagi nanti."}), 429
        return view(*a, **kw)
    wrapped.__name__ = view.__name__
    return wrapped


def _run(coro):
    """Jalankan coroutine async dari view Flask sync."""
    return asyncio.run(coro)


# ------------------------------------------------- batas konkurensi berat
# T-2 (audit 2026-10-17): endpoint mahal (AI & pencarian fan-out) bisa
# menahan 1 thread waitress puluhan detik. Tanpa batas konkurensi,
# segelintir request lambat melumpuhkan seluruh situs (DoS) sekaligus
# membakar kredit AI. Penjaga ini menolak (429) BUKAN menggantung.
_HEAVY = {}            # ip -> jumlah request mahal yang sedang berjalan
_HEAVY_LOCK = threading.Lock()
_HEAVY_GLOBAL = 0      # total request mahal yang sedang berjalan
_HEAVY_PER_IP = 3      # longgar untuk manusia (3 tab bersamaan masih lolos)
_HEAVY_GLOBAL_MAX = 5  # sisakan thread untuk endpoint murah & health check


def _heavy_masuk():
    global _HEAVY_GLOBAL
    ip = _ip()
    with _HEAVY_LOCK:
        n_ip = _HEAVY.get(ip, 0)
        if n_ip >= _HEAVY_PER_IP or _HEAVY_GLOBAL >= _HEAVY_GLOBAL_MAX:
            return False
        _HEAVY[ip] = n_ip + 1
        _HEAVY_GLOBAL += 1
        return True


def _heavy_keluar():
    global _HEAVY_GLOBAL
    ip = _ip()
    with _HEAVY_LOCK:
        n_ip = _HEAVY.get(ip, 0)
        if n_ip <= 1:
            _HEAVY.pop(ip, None)
        else:
            _HEAVY[ip] = n_ip - 1
        _HEAVY_GLOBAL = max(0, _HEAVY_GLOBAL - 1)


def with_heavy(view):
    """Tolak 429 bila terlalu banyak request mahal bersamaan (T-2)."""
    def wrapped(*a, **kw):
        if not _heavy_masuk():
            return jsonify({"ok": False, "error": "server_sibuk",
                            "pesan": "Server lagi sibuk, coba lagi sebentar ya."}), 429
        try:
            return view(*a, **kw)
        finally:
            _heavy_keluar()
    wrapped.__name__ = view.__name__
    return wrapped


def _deadline_run(fn, secs):
    """Jalankan fn di thread daemon, tunggu maksimal `secs` detik.

    Untuk pekerjaan best-effort (enrichment): bila timeout, thread dibiarkan
    selesai sendiri (daemon) dan hasil parsial dipakai apa adanya, supaya
    request tidak digantung melebihi timeout frontend.
    """
    t = threading.Thread(target=fn, daemon=True)
    t.start()
    t.join(timeout=secs)


# ---------------------------------------------------------------- format work
def to_bot_work(w):
    """Normalisasi dict work dari web/bot -> format internal bot."""
    if not isinstance(w, dict):
        return None
    authors = w.get("authors") or w.get("penulis") or []
    if isinstance(authors, str):
        authors = [x.strip() for x in authors.split(",") if x.strip()]
    return {
        "title": w.get("title") or w.get("judul"),
        "authors": authors,
        "year": w.get("year") or w.get("tahun"),
        "journal": w.get("journal") or w.get("jurnal"),
        "abstract": w.get("abstract") or w.get("abstrak"),
        "doi": w.get("doi"),
        "url": w.get("url"),
        "volume": w.get("volume"),
        "issue": w.get("issue"),
        "pages": w.get("pages"),
        "is_oa": w.get("is_oa"),
        "pdf_url": w.get("pdf_url"),
    }


def to_web_work(w):
    return {
        "judul": w.get("title"),
        "penulis": w.get("authors") or [],
        "tahun": w.get("year"),
        "jurnal": w.get("journal"),
        "abstrak": w.get("abstract"),
        "doi": w.get("doi"),
        "url": w.get("url"),
        "is_oa": bool(w.get("is_oa")),
        "pdf_url": w.get("pdf_url"),
        "volume": w.get("volume"),
        "issue": w.get("issue"),
        "pages": w.get("pages"),
    }


def _style_or_default(gaya):
    g = (gaya or "harvard").strip().lower()
    return g if g in STYLE_ORDER else "harvard"


def _err(msg, code=400):
    return jsonify({"error": msg}), code


# ---------------------------------------------------------------- health
@app.get("/api/health")
def health():
    return jsonify({"ok": True})


# ---------------------------------------------------------------- cari
@app.post("/api/cari")
@with_rl
@with_heavy
def cari():
    body = request.get_json(silent=True) or {}
    q = (body.get("q") or "").strip()
    if not q:
        return _err("kata kunci kosong")
    _qt = kuota_tamu("cari")
    if _qt is not None:
        return _qt
    track_penggunaan("cari")
    try:
        tahun = int(body.get("tahun") or 0)
    except (TypeError, ValueError):
        tahun = 0
    if tahun not in (0, 5, 10):
        tahun = 0
    try:
        per = max(1, min(12, int(body.get("limit") or 6)))
    except (TypeError, ValueError):
        per = 6
    try:
        cutoff = date.today().year - tahun if tahun else None
        works_intl = search_international(q, per)
        works_id, notes = search_indonesia(q, per)
        if works_id:
            try:
                enrich_sinta_ranks(works_id)
            except Exception:
                pass

        def _fresh(ws):
            if not cutoff:
                return ws or []
            out = []
            for w in (ws or []):
                try:
                    y = int(w.get("year"))
                except (TypeError, ValueError):
                    # tahun tak diketahui -> lolos filter (jangan dibuang diam-diam)
                    out.append(w)
                    continue
                if y >= cutoff:
                    out.append(w)
            return out

        works = (_fresh(works_intl) + _fresh(works_id))[: per * 2]
        hasil_web = [to_web_work(w) for w in (works or [])]
        # R5 (audit 2026-10-13): jangan catat entri zonk (0 hasil)
        if g.user and hasil_web:
            # riwayat: hanya user login (tamu tidak disimpan di server)
            histmod.catat(g.uid, "jurnal", q,
                          {"q": q, "tahun": tahun, "hasil": hasil_web,
                           "catatan": notes})
        return jsonify({
            "hasil": hasil_web,
            "catatan": notes,
            "tahun": tahun,
        })
    except Exception as e:
        log.warning("cari gagal: %s", e)
        return _err("pencarian gagal, coba lagi nanti", 502)


# ---------------------------------------------------------------- sitasi
@app.post("/api/sitasi")
@with_rl
def sitasi():
    body = request.get_json(silent=True) or {}
    bw = to_bot_work(body.get("work") or {})
    if not bw or not bw.get("title"):
        return _err("work tidak valid (butuh judul)")
    _qt = kuota_tamu("sitasi")
    if _qt is not None:
        return _qt
    track_penggunaan("sitasi")
    gaya = _style_or_default(body.get("gaya"))
    try:
        teks_sitasi = cite_in(bw, gaya)
        if g.user:
            histmod.catat(g.uid, "sitasi", bw.get("title") or "",
                          {"gaya": style_label(gaya), "gaya_id": gaya,
                           "sitasi": teks_sitasi, "work": bw})
        return jsonify({
            "sitasi": teks_sitasi,
            "gaya": style_label(gaya),
            "semua_gaya": {s: style_label(s) for s in STYLE_ORDER},
        })
    except Exception as e:
        log.warning("sitasi gagal: %s", e)
        return _err("gagal membuat sitasi", 502)


# ---------------------------------------------------------------- carifile
@app.post("/api/carifile")
@with_rl
@with_heavy
def carifile():
    body = request.get_json(silent=True) or {}
    teks = (body.get("teks") or "").strip()
    entries = split_citations(teks)
    if not entries:
        return _err("paste sitasi / daftar pustakanya dulu")
    found, missed = [], []
    parsial = False
    t0 = time.time()
    # Deadline global 22 dtk: tiap entri bisa makan puluhan detik (DOI ->
    # OpenAlex -> S2 -> Crossref). Tanpa deadline, frontend (abort 30 dtk)
    # menampilkan "timeout" padahal sebagian entri sudah ketemu.
    for e in entries:
        if time.time() - t0 > 22:
            parsial = True
            break
        p = parse_citation(e)
        work = None
        if p.get("doi"):
            try:
                work = fetch_by_doi(p["doi"])
            except Exception as ex:
                log.warning("carifile doi error: %s", ex)
        if not work and p.get("title"):
            try:
                cands = search_openalex(p["title"], 5)
            except Exception as ex:
                log.warning("carifile title error: %s", ex)
                cands = []
            best, best_s = None, 0.0
            for c in cands:
                s = title_match(p["title"], c.get("title"))
                if s > best_s:
                    best, best_s = c, s
            if best and best_s >= 0.6:
                work = best
        if work:
            found.append(work)
        elif p.get("doi"):
            # DOI dikenali tapi tidak ter-resolve (mis. preprint yang belum
            # masuk OpenAlex/Crossref): tetap beri link langsung ke doi.org
            # agar user minimal dapat akses ke sumbernya.
            doi = p["doi"]
            found.append({
                "title": p.get("title") or ("DOI: " + doi),
                "authors": p.get("authors") or [],
                "year": p.get("year"),
                "journal": None,
                "abstract": None,
                "doi": doi,
                "url": "https://doi.org/" + doi,
                "is_oa": False,
                "pdf_url": None,
            })
        else:
            short = e[:90] + ("…" if len(e) > 90 else "")
            missed.append(short)
    hasil_web = [to_web_work(w) for w in found]
    # S3 (audit 2026-10-13): Cari dari Sitasi adalah pencarian jurnal -> catat
    # riwayat (R5: skip bila tidak ada hasil)
    if g.user and hasil_web:
        histmod.catat(g.uid, "jurnal",
                      "Cari dari Sitasi: " + (teks[:60] + ("…" if len(teks) > 60 else "")),
                      {"q": teks[:200], "sumber": "carifile",
                       "hasil": hasil_web, "tidak_ketemu": missed})
    return jsonify({
        "hasil": hasil_web,
        "tidak_ketemu": missed,
        "parsial": parsial,
    })


# ---------------------------------------------------------------- rekomendasi
@app.post("/api/rekomendasi")
@with_rl
def rekomendasi():
    body = request.get_json(silent=True) or {}
    topic = (body.get("topik") or "").strip()
    if not topic:
        return _err("topik kosong")
    try:
        query = topic
        t = query
        if "/" in t and ("10." in t or t.lower().startswith("doi")):
            try:
                w = fetch_by_doi(t)
            except Exception:
                w = None
            if w and w.get("title"):
                query = w["title"]
        recs = recommend_journals(query, 8)
        if not recs:
            en = _run(topik_mod.translate_keyword_en(topic))
            if en and en.lower() != topic.lower():
                recs = recommend_journals(en, 8)
        if recs:
            # Enrichment best-effort dengan deadline 20 dtk: tanpanya,
            # cold-cache hampir pasti melebihi timeout frontend 30 dtk.
            try:
                _deadline_run(lambda: enrich_journal_profiles(recs), 20)
            except Exception as ex:
                log.warning("enrich rekomendasi gagal: %s", ex)
        return jsonify({"daftar": recs})
    except Exception as e:
        log.warning("rekomendasi gagal: %s", e)
        return _err("rekomendasi gagal, coba lagi nanti", 502)


# ---------------------------------------------------------------- publisher
@app.post("/api/publisher")
@with_rl
def publisher():
    body = request.get_json(silent=True) or {}
    bidang = (body.get("bidang") or "").strip()
    if not bidang:
        return _err("bidang kosong")
    scope = (body.get("scope") or "sinta").strip().lower()
    if scope not in ("sinta", "intl"):
        scope = "sinta"
    kw_note = None
    try:
        if scope == "sinta":
            g_res = search_garuda(bidang, 30)
            works = g_res[0] if isinstance(g_res, tuple) else g_res
            pubs = aggregate_publishers(works)
        else:
            kw = _BIDANG_EN.get(bidang.lower(), bidang)
            works = search_openalex(kw, 60)
            pubs = aggregate_publishers(works)
            if kw != bidang:
                kw_note = kw
        if pubs:
            # Homepage lookup best-effort dengan deadline 20 dtk.
            homes = {}
            try:
                _box = {}
                _deadline_run(lambda: _box.update(
                    {"h": publisher_homepages([p["name"] for p in pubs])}), 20)
                homes = _box.get("h") or {}
            except Exception:
                homes = {}
            for p in pubs:
                if p["name"] in homes:
                    p["homepage"] = homes[p["name"]]
        return jsonify({"daftar": pubs, "bidang": bidang,
                        "scope": scope, "kw_note": kw_note})
    except Exception as e:
        log.warning("publisher gagal: %s", e)
        return _err("publisher gagal, coba lagi nanti", 502)


# ---------------------------------------------------------------- penulis
@app.post("/api/penulis")
@with_rl
def penulis():
    body = request.get_json(silent=True) or {}
    nama = (body.get("nama") or "").strip()
    if not nama:
        return _err("nama kosong")
    _qt = kuota_tamu("penulis")
    if _qt is not None:
        return _qt
    track_penggunaan("penulis")
    try:
        authors = search_authors(nama, 5)
        return jsonify({"daftar": authors or []})
    except Exception as e:
        log.warning("penulis gagal: %s", e)
        return _err("pencarian penulis gagal, coba lagi nanti", 502)


# ---------------------------------------------------------------- topik
@app.post("/api/topik")
@with_rl
@with_heavy
def topik():
    body = request.get_json(silent=True) or {}
    keyword = (body.get("keyword") or "").strip()
    if not keyword:
        return _err("keyword kosong")
    _qt = kuota_tamu("topik")
    if _qt is not None:
        return _qt
    track_penggunaan("topik")
    try:
        top, gaps = topik_mod.trending_topics(keyword)
        if not top:
            en = _run(topik_mod.translate_keyword_en(keyword))
            if en and en.lower() != keyword.lower():
                top, gaps = topik_mod.trending_topics(en)
    except Exception as e:
        log.warning("topik tren gagal: %s", e)
        return _err("gagal mengambil tren publikasi", 502)
    ide, kuota_habis = None, False
    pilihan = _pilihan_model(body)
    info, premium_gagal, premium_terkunci = {}, False, False
    if pilihan in _MODEL_PREMIUM:
        poolp, errp = _gerbang_premium(g.uid)
        if errp:
            premium_terkunci = True
        else:
            try:
                ide, info = _run(topik_mod.ide_topik_ai_pilihan(
                    keyword, top, gaps, pilihan=pilihan))
            except Exception as e:
                log.warning("topik ide premium gagal: %s", e)
                ide, info = None, {}
            if not ide:
                _refund_premium(g.uid, poolp)
                premium_gagal = True
            else:
                sai.track_tool("topik", g.uid)
    else:
        pool = _ai_consume_uid(g.uid)
        if pool:
            try:
                ide, info = _run(topik_mod.ide_topik_ai_pilihan(
                    keyword, top, gaps, pilihan=pilihan))
            except Exception as e:
                log.warning("topik ide gagal: %s", e)
                ide, info = None, {}
            if not ide:
                _ai_refund_uid(g.uid, pool)
            else:
                sai.track_tool("topik", g.uid)
        else:
            kuota_habis = True
    saran = [] if ide else topik_mod.topic_suggestions(keyword, top, gaps)
    # R5 (audit 2026-10-13): jangan catat entri zonk (ide None dan saran kosong)
    if g.user and (ide or saran):
        histmod.catat(g.uid, "topik", keyword,
                      {"keyword": keyword, "ide": ide, "saran": saran,
                       "gaps": gaps})
    return jsonify({
        "tren": top, "gaps": gaps, "ide": ide, "saran": saran,
        "kuota_habis": kuota_habis,
        "premium_gagal": premium_gagal,
        "premium_terkunci": premium_terkunci,
        "premium": bool(ide) and pilihan in _MODEL_PREMIUM,
        "model_label": info.get("label") if ide else None,
        "dialihkan": bool(info.get("dialihkan")) if ide else False,
        "model_pilihan": _MODEL_LABEL[pilihan],
        "sisa": _ai_left_uid(g.uid),
    })


# ---------------------------------------------------------------- export
@app.post("/api/export")
@with_rl
def export():
    body = request.get_json(silent=True) or {}
    items = body.get("items") or []
    if not items:
        return _err("items kosong")
    if len(items) > 1000:
        return _err("maksimal 1000 item per export ya", 400)
    fmt = (body.get("format") or "txt").strip().lower()
    if fmt not in ("txt", "docx", "bibtex"):
        return _err("format harus txt|docx|bibtex")
    style = _style_or_default(body.get("style"))
    _qt = kuota_tamu("export")
    if _qt is not None:
        return _qt
    track_penggunaan("export")
    tmp_uid = "web_tmp_" + uuid.uuid4().hex
    try:
        for w in items:
            bw = to_bot_work(w)
            if bw and bw.get("title"):
                koleksi.add(tmp_uid, bw)
        if fmt == "txt":
            txt = koleksi.export_txt(tmp_uid, style)
            return Response(
                txt, mimetype="text/plain; charset=utf-8",
                headers={"Content-Disposition":
                         f"attachment; filename=daftar-pustaka-{style}.txt"})
        if fmt == "docx":
            data = koleksi.export_docx(tmp_uid, style)
            return send_file(BytesIO(data),
                             mimetype="application/vnd.openxmlformats-"
                                      "officedocument.wordprocessingml.document",
                             as_attachment=True,
                             download_name=f"daftar-pustaka-{style}.docx")
        bib = koleksi.export_bibtex(tmp_uid)
        return Response(
            bib, mimetype="text/plain; charset=utf-8",
            headers={"Content-Disposition":
                     "attachment; filename=koleksi.bib"})
    finally:
        koleksi.clear(tmp_uid)


# ---------------------------------------------------------------- matriks
@app.post("/api/matriks")
@with_rl
def matriks():
    body = request.get_json(silent=True) or {}
    items = body.get("items") or []
    if not items:
        return _err("items kosong")
    works = [bw for bw in (to_bot_work(w) for w in items)
             if bw and bw.get("title")]
    if not works:
        return _err("tidak ada item valid")
    if len(works) > 200:
        return _err("maksimal 200 item per matriks ya", 400)
    _qt = kuota_tamu("matriks")
    if _qt is not None:
        return _qt
    track_penggunaan("matriks")
    try:
        data = matriks_mod.build_matrix_docx(works)
        return send_file(
            BytesIO(data),
            mimetype="application/vnd.openxmlformats-"
                     "officedocument.wordprocessingml.document",
            as_attachment=True, download_name="matriks-literatur.docx")
    except Exception as e:
        log.warning("matriks gagal: %s", e)
        return _err("gagal membuat matriks", 502)


# ---------------------------------------------------------------- statistik
@app.post("/api/statistik")
@with_rl
def statistik():
    body = request.get_json(silent=True) or {}
    answers = body.get("answers") or {}
    if body.get("restart"):
        answers = {}
    # validasi bentuk dulu sebelum kuota dicatat (payload rusak tidak
    # membakar jatah tamu)
    if not isinstance(answers, dict):
        return _err("format jawaban tidak valid", 400)
    _qt = kuota_tamu("statistik")
    if _qt is not None:
        return _qt
    track_penggunaan("statistik")
    step = stats_mod.next_step(answers)
    if step[0] == "hasil":
        r = stats_mod.get_result(step[1])
        if not r:
            return _err("hasil tidak dikenal", 502)
        return jsonify({"tipe": "hasil", "hasil": r})
    _, qid, teks, opsi = step
    return jsonify({"tipe": "pertanyaan", "qid": qid, "teks": teks,
                    "opsi": [[k, label] for k, label in opsi]})


# ---------------------------------------------------------------- template
@app.get("/api/template")
@with_rl
def template():
    jenis = (request.args.get("jenis") or "kuantitatif").strip().lower()
    if jenis not in template_doc.TYPES:
        jenis = "kuantitatif"
    _qt = kuota_tamu("template")
    if _qt is not None:
        return _qt
    track_penggunaan("template")
    try:
        data = template_doc.build_template_docx(jenis)
        return send_file(
            BytesIO(data),
            mimetype="application/vnd.openxmlformats-"
                     "officedocument.wordprocessingml.document",
            as_attachment=True,
            download_name=f"template-skripsi-{jenis}.docx")
    except Exception as e:
        log.warning("template gagal: %s", e)
        return _err("gagal membuat template", 502)


# ---------------------------------------------------------------- parafrase
@app.post("/api/parafrase")
@with_rl
def parafrase():
    body = request.get_json(silent=True) or {}
    teks = (body.get("teks") or "").strip()
    if not teks:
        return _err("teks kosong")
    # validasi murah dulu sebelum kuota dicatat (request invalid tidak
    # membakar jatah tamu) — cek bahasa Indonesia tetap setelah kuota
    # karena itu bagian dari upaya parafrase yang sesungguhnya.
    if len(teks) > 800:
        return _err("maksimal 800 karakter ya, potong jadi beberapa bagian.", 422)
    _qt = kuota_tamu("parafrase")
    if _qt is not None:
        return _qt
    track_penggunaan("parafrase")
    try:
        hasil, err = paraphrase_id(teks)
    except Exception as e:
        log.warning("parafrase gagal: %s", e)
        return _err("parafrase gagal, coba lagi nanti", 502)
    if err:
        return _err(err, 422)
    # I4 (audit 2026-10-13): parafrase dicatat sebagai riwayat chat
    if g.user:
        histmod.catat(g.uid, "chat", teks,
                      {"sumber": "parafrase", "text": teks, "hasil": hasil})
    return jsonify({"hasil": hasil})


# ------------------------------------------------------- cek plagiarisme
# Jalur A (keputusan Sul 2026-10-18): kemiripan naskah vs sumber yang
# ditempel pengguna, dihitung lokal (n-gram) TANPA AI & tanpa menyimpan
# teks. Nama fitur di UI "Cek Plagiarisme" dengan disclaimer 3 lapis.
@app.post("/api/kemiripan")
@with_rl
def kemiripan():
    body = request.get_json(silent=True) or {}
    naskah = (body.get("naskah") or "").strip()
    sumber = (body.get("sumber") or "").strip()
    if not naskah or not sumber:
        return _err("naskah dan teks sumber dua-duanya wajib diisi ya")
    if len(naskah) > 10000:
        return _err("naskahnya kepanjangan, maksimal 10.000 karakter ya. Potong jadi beberapa bagian.", 422)
    if len(sumber) > 30000:
        return _err("teks sumbernya kepanjangan, maksimal 30.000 karakter ya.", 422)
    if kemiripan_mod.jumlah_kata(naskah) < 10:
        return _err("naskahnya terlalu pendek, minimal sekitar 10 kata biar bisa dicek.", 422)
    if kemiripan_mod.jumlah_kata(sumber) < 10:
        return _err("teks sumbernya terlalu pendek, minimal sekitar 10 kata ya.", 422)
    _qt = kuota_tamu("kemiripan")
    if _qt is not None:
        return _qt
    track_penggunaan("kemiripan")
    try:
        hasil = kemiripan_mod.cek(naskah, sumber)
    except Exception as e:
        log.warning("kemiripan gagal: %s", e)
        return _err("pengecekan gagal, coba lagi nanti", 502)
    return jsonify({"hasil": hasil})


def _teks_dari_file(fs):
    """Ekstrak teks dari file unggahan (.docx/.txt/.pdf). File TIDAK
    disimpan: dibaca di memori lalu dibuang. Return (teks, None) /
    (None, pesan)."""
    nama = (fs.filename or "").lower()
    raw = fs.read()
    if len(raw) > 2 * 1024 * 1024:
        return None, "ukuran file maksimal 2 MB ya"
    if nama.endswith(".pdf") or raw[:4] == b"%PDF":
        try:
            from pypdf import PdfReader
            reader = PdfReader(BytesIO(raw))
            if reader.is_encrypted:
                return None, ("PDF-nya terkunci kata sandi, jadi teksnya "
                              "tidak bisa dibaca. Lepas kuncinya dulu ya")
            bagian = [(hal.extract_text() or "")
                      for hal in reader.pages[:200]]
            teks = "\n".join(bagian)
        except Exception:
            return None, ("file PDF-nya tidak bisa dibaca, mungkin rusak. "
                          "Coba file lain, atau salin teksnya lalu tempel "
                          "manual ya")
        if kemiripan_mod.jumlah_kata(teks) < 10:
            return None, ("teks di PDF ini tidak terbaca, kemungkinan PDF "
                          "hasil pindai atau gambar tanpa lapisan teks. "
                          "Salin teksnya manual, atau pakai versi "
                          ".docx/.txt-nya ya")
        return teks, None
    if nama.endswith(".docx") or raw[:2] == b"PK":
        try:
            from docx import Document
            doc = Document(BytesIO(raw))
            bagian = [p.text for p in doc.paragraphs]
            for tabel in doc.tables:
                for baris in tabel.rows:
                    bagian.append(" ".join(c.text for c in baris.cells))
            return "\n".join(bagian), None
        except Exception:
            return None, ("file .docx-nya tidak bisa dibaca, mungkin rusak "
                          "atau formatnya bukan Word asli")
    try:
        return raw.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace"), None


@app.post("/api/kemiripan-file")
@with_rl
def kemiripan_file():
    """Cek Plagiarisme versi unggah file: naskah/sumber boleh berupa
    teks tempel atau file .docx/.txt (boleh dicampur). Batas versi file
    lebih longgar: naskah 50.000 karakter, sumber 150.000 karakter."""
    naskah = (request.form.get("naskah") or "").strip()
    sumber = (request.form.get("sumber") or "").strip()
    for kunci in ("file_naskah", "file_sumber"):
        fs = request.files.get(kunci)
        if fs and fs.filename:
            teks, errm = _teks_dari_file(fs)
            if errm:
                return _err(errm, 422)
            if kunci == "file_naskah":
                naskah = (teks or "").strip()
            else:
                sumber = (teks or "").strip()
    if not naskah or not sumber:
        return _err("naskah dan teks sumber dua-duanya wajib ada ya: "
                    "tempel teksnya atau unggah file-nya")
    if len(naskah) > 50000:
        return _err(f"naskah dari file kepanjangan ({len(naskah)} karakter), "
                    "maksimal 50.000 karakter. Potong jadi beberapa bagian ya.", 422)
    if len(sumber) > 150000:
        return _err(f"teks sumber dari file kepanjangan ({len(sumber)} karakter), "
                    "maksimal 150.000 karakter ya.", 422)
    if kemiripan_mod.jumlah_kata(naskah) < 10:
        return _err("naskahnya terlalu pendek, minimal sekitar 10 kata biar bisa dicek.", 422)
    if kemiripan_mod.jumlah_kata(sumber) < 10:
        return _err("teks sumbernya terlalu pendek, minimal sekitar 10 kata ya.", 422)
    _qt = kuota_tamu("kemiripan")
    if _qt is not None:
        return _qt
    track_penggunaan("kemiripan")
    try:
        hasil = kemiripan_mod.cek(naskah, sumber)
    except Exception as e:
        log.warning("kemiripan-file gagal: %s", e)
        return _err("pengecekan gagal, coba lagi nanti", 502)
    return jsonify({"hasil": hasil})


# ------------------------------------------------- unggah file (umum)
@app.post("/api/ekstrak-teks")
@with_rl
def ekstrak_teks():
    """Ekstrak teks dari file .docx/.txt/.pdf untuk diisikan ke kolom
    fitur lain (Cari dari Sitasi, AI Studio, Terjemah Abstrak, dsb).
    File dibaca di memori dan TIDAK disimpan; pemakaian fitur tujuan
    yang menghitung kuota, bukan ekstraksinya."""
    fs = request.files.get("file")
    if not fs or not fs.filename:
        return _err("pilih file-nya dulu ya")
    teks, errm = _teks_dari_file(fs)
    if errm:
        return _err(errm, 422)
    teks = (teks or "").strip()
    if not teks:
        return _err("teks di file itu kosong", 422)
    terpotong = False
    if len(teks) > 150000:
        teks = teks[:150000]
        terpotong = True
    track_penggunaan("ekstrak")
    return jsonify({
        "teks": teks,
        "nama": fs.filename,
        "karakter": len(teks),
        "kata": kemiripan_mod.jumlah_kata(teks),
        "terpotong": terpotong,
    })


# ------------------------------------------------- impor BibTeX / RIS
@app.post("/api/koleksi-impor")
@with_rl
def koleksi_impor():
    """Impor referensi massal dari file .bib / .ris (ekspor Mendeley,
    Zotero, Google Scholar). Hasil parse dikembalikan sebagai daftar
    work format web; penyimpanan ke Koleksi dilakukan frontend
    (localStorage), sama seperti hasil pencarian."""
    fs = request.files.get("file")
    if not fs or not fs.filename:
        return _err("pilih file .bib atau .ris-nya dulu ya")
    nama = fs.filename.lower()
    if not (nama.endswith(".bib") or nama.endswith(".ris")
            or nama.endswith(".txt")):
        return _err("format file-nya .bib atau .ris ya "
                    "(hasil ekspor Mendeley/Zotero/Google Scholar)", 422)
    raw = fs.read()
    if len(raw) > 2 * 1024 * 1024:
        return _err("ukuran file maksimal 2 MB ya", 422)
    try:
        teks = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        teks = raw.decode("latin-1", errors="replace")
    works, fmt = impor_ref.parse_referensi(teks, fs.filename)
    if fmt is None:
        return _err("isi file-nya tidak dikenali sebagai BibTeX atau RIS. "
                    "Pastikan file hasil ekspor dari Mendeley, Zotero, "
                    "atau Google Scholar ya", 422)
    if not works:
        return _err("tidak ada referensi berjudul yang ketemu di file itu",
                    422)
    if len(works) > 500:
        works = works[:500]
    track_penggunaan("impor")
    return jsonify({
        "items": [to_web_work(w) for w in works],
        "jumlah": len(works),
        "format": fmt,
    })


# ------------------------------------------ sitasi dari file jurnal
def _tebak_meta_pdf(teks):
    """Tebak judul/penulis/tahun/jurnal dari teks halaman pertama.
    Hanya dipakai bila DOI tidak ketemu; hasilnya WAJIB dicek pengguna
    (frontend menampilkan label peringatan). Return work / None."""
    head = (teks or "")[:3500]
    mentah = [ln.strip() for ln in head.splitlines()]
    lines = [ln for ln in mentah if len(ln) >= 3]

    def _sampah(ln):
        low = ln.lower()
        return any(k in low for k in (
            "issn", "http", "www.", "@", "copyright", "doi:",
            "p-issn", "e-issn", "available online", "received",
            "accepted", "published", "license", "creative commons",
            "keywords", "kata kunci", "abstract", "abstrak"))

    cand = [ln for ln in lines[:16] if not _sampah(ln)]
    # Judul dicari di baris-baris awal saja; kalimat isi (umumnya
    # berakhir titik) disingkirkan lebih dulu, lalu ambil yang terpanjang.
    judul, idx_judul = None, -1
    for i, ln in enumerate(cand[:6]):
        if ln.endswith("."):
            continue
        if len(ln.split()) >= 3 and (judul is None or len(ln) > len(judul)):
            judul, idx_judul = ln, i
    if not judul:
        return None
    # Judul kadang terpotong dua baris lebih: sambung selama baris
    # berikutnya bukan baris penulis (tanpa koma), bukan kalimat isi
    # (tanpa titik akhir), dan tidak memuat tahun.
    while 0 <= idx_judul < len(cand) - 1:
        samb = cand[idx_judul + 1]
        if ("," in samb or samb.endswith(".")
                or re.search(r"\b(19|20)\d{2}\b", samb)
                or len(samb.split()) < 3
                or len(judul) + len(samb) >= 280):
            break
        judul = judul + " " + samb
        idx_judul += 1
    penulis = []
    for ln in cand[idx_judul + 1:idx_judul + 4]:
        low = ln.lower()
        if any(k in low for k in ("universitas", "institut", "fakultas",
                                  "departemen", "department", "indonesia",
                                  "university", "prodi", "program studi")):
            continue
        if ":" in ln:
            continue
        if "," in ln or " dan " in low or "&" in ln:
            bersih = re.sub(r"[*†\d]+", " ", ln)
            penulis = [p.strip(" .") for p in re.split(
                r",|&|\sdan\s+", bersih, flags=re.I)
                if 3 <= len(p.strip(" .")) <= 60]
            if penulis:
                break
    tahun = None
    head_tanpa_doi = re.sub(r"10\.\d{4,9}/[^\s]+", " ", head)
    ym = re.search(r"\b(19|20)\d{2}\b", head_tanpa_doi)
    if ym:
        tahun = int(ym.group(0))
    jurnal = None
    for ln in lines[:6]:
        if (re.search(r"jurnal|journal", ln, flags=re.I)
                and len(ln) <= 120 and ln != judul):
            jurnal = ln
            break
    return {"title": judul, "authors": penulis, "year": tahun,
            "journal": jurnal, "doi": None, "url": None,
            "abstract": None, "volume": None, "issue": None,
            "pages": None, "is_oa": False, "pdf_url": None}


@app.post("/api/sitasi-pdf")
@with_rl
def sitasi_pdf():
    """Buat Sitasi dari file jurnal: cari DOI di teks file lalu ambil
    metadata akurat dari database jurnal; bila DOI tidak ada, tebak
    metadata dari halaman pertama (hasilnya dilabeli perlu dicek)."""
    fs = request.files.get("file")
    if not fs or not fs.filename:
        return _err("pilih file jurnalnya dulu ya")
    teks, errm = _teks_dari_file(fs)
    if errm:
        return _err(errm, 422)
    work, sumber = None, "teks"
    doi = doi_in_text(teks or "")
    if doi:
        try:
            w = fetch_by_doi(doi)
        except Exception as e:
            log.warning("sitasi-pdf doi gagal: %s", e)
            w = None
        if w and w.get("title"):
            work, sumber = w, "doi"
    if not work:
        work = _tebak_meta_pdf(teks or "")
    if not work or not work.get("title"):
        return _err("judulnya tidak ketemu dari file itu, "
                    "isi manual saja ya", 422)
    track_penggunaan("sitasi-pdf")
    return jsonify({"work": to_web_work(work), "sumber": sumber})


# ---------------------------------------------------------------- terjemah
@app.post("/api/terjemah")
@with_rl
def terjemah():
    body = request.get_json(silent=True) or {}
    teks = (body.get("teks") or "").strip()
    if not teks:
        return _err("teks kosong")
    if len(teks) > 3000:
        return _err("abstraknya kepanjangan, maksimal 3000 karakter ya.", 422)
    if looks_indonesian(teks):
        return jsonify({"hasil": None, "sumber": "sudah_indonesia"})
    _qt = kuota_tamu("terjemah")
    if _qt is not None:
        return _qt
    track_penggunaan("terjemah")
    try:
        hasil, sumber = translate_en_id(teks)
    except Exception as e:
        log.warning("terjemah gagal: %s", e)
        return _err("terjemahan gagal, coba lagi nanti", 502)
    if not hasil:
        return _err("terjemahan gagal, coba lagi nanti", 502)
    return jsonify({"hasil": hasil, "sumber": sumber})


# ---------------------------------------------------------------- jadwal
@app.post("/api/jadwal")
@with_rl
def jadwal():
    body = request.get_json(silent=True) or {}
    tanggal = (body.get("tanggal") or "").strip()
    if not tanggal:
        return _err("tanggal kosong")
    _qt = kuota_tamu("jadwal")
    if _qt is not None:
        return _qt
    track_penggunaan("jadwal")
    ok, data = jadwal_web.build_timeline(tanggal)
    if not ok:
        return _err(data, 422)
    return jsonify(data)


# ---------------------------------------------------------------- AI studio
@app.post("/api/ai")
@with_rl
@with_heavy
def ai():
    body = request.get_json(silent=True) or {}
    kind = (body.get("kind") or "").strip().lower()
    if kind not in AI_KINDS:
        return _err(f"kind harus salah satu: {', '.join(AI_KINDS)}")
    text = (body.get("text") or "").strip()
    if not text:
        return _err("text kosong")
    cap = AI_KIND_INPUT_CAP.get(kind, 2000)
    if len(text) > cap:
        return _err(f"teks kepanjangan (maksimal {cap} karakter)", 400)
    extra = (body.get("extra") or "").strip()
    pilihan = _pilihan_model(body)
    if pilihan in _MODEL_PREMIUM:
        poolp, errp = _gerbang_premium(g.uid)
        if errp:
            return errp
        try:
            hasil, info = _run(sai.generate_pilihan(
                kind, text, extra, chat_id=g.uid, pilihan=pilihan))
        except Exception as e:
            log.warning("ai premium %s error: %s", kind, e)
            hasil, info = None, {}
        if not hasil:
            _refund_premium(g.uid, poolp)
            return jsonify({
                "error": "premium_gagal", "model": _MODEL_LABEL[pilihan],
                "pesan": _MODEL_LABEL[pilihan] + " lagi bermasalah saat "
                         "ini. Saldo kamu tidak terpotong."}), 503
        sai.track_tool("ai", g.uid)
        if g.user:
            histmod.catat(g.uid, "chat", text,
                          {"sumber": "ai", "kind": kind,
                           "text": text[:2000], "hasil": hasil})
        return jsonify({"hasil": hasil, "kind": kind, "premium": True,
                        "model_label": info.get("label"),
                        "saldo": sai.ai_balance(g.uid),
                        "sisa": _ai_left_uid(g.uid)})
    pool = _ai_consume_uid(g.uid)
    if not pool:
        return jsonify({"error": "kuota_habis",
                        "pesan": pesan_ai_habis()}), 429
    try:
        hasil, info = _run(sai.generate_pilihan(
            kind, text, extra, chat_id=g.uid, pilihan=pilihan))
    except Exception as e:
        log.warning("ai %s error: %s", kind, e)
        hasil, info = None, {}
    if not hasil:
        _ai_refund_uid(g.uid, pool)
        return _err("AI-nya lagi gangguan, coba lagi bentar ya", 502)
    sai.track_tool("ai", g.uid)
    if g.user:
        histmod.catat(g.uid, "chat", text,
                      {"sumber": "ai", "kind": kind,
                       "text": text[:2000], "hasil": hasil})
    return jsonify({"hasil": hasil, "kind": kind,
                    "model_label": info.get("label"),
                    "dialihkan": bool(info.get("dialihkan")),
                    "model_pilihan": _MODEL_LABEL[pilihan],
                    "sisa": _ai_left_uid(g.uid)})


# ---------------------------------------------------------------- tanya
@app.post("/api/tanya")
@with_rl
@with_heavy
def tanya():
    body = request.get_json(silent=True) or {}
    q = (body.get("q") or "").strip()
    if len(q) < 3:
        return _err("pertanyaannya kurang jelas, tulis lebih lengkap")
    entry, _ = tanya_mod.match(q[:500])
    if entry:
        if g.user:
            histmod.catat(g.uid, "chat", q,
                          {"sumber": "tanya", "tipe_jawab": "panduan",
                           "q": q[:500], "judul_panduan": entry["title"],
                           "jawaban": entry["answer"]})
        return jsonify({
            "tipe": "panduan",
            "jawaban": entry["answer"],
            "judul": entry["title"],
            "terkait": [{"id": e["id"], "title": e["title"]}
                        for e in tanya_mod.related(entry)],
        })
    pilihan = _pilihan_model(body)
    if pilihan in _MODEL_PREMIUM:
        poolp, errp = _gerbang_premium(g.uid)
        if errp:
            return errp
        try:
            jawaban, info = _run(tanya_mod.gemini_answer_pilihan(
                q[:500], pilihan=pilihan))
        except Exception as e:
            log.warning("tanya premium error: %s", e)
            jawaban, info = None, {}
        if not jawaban:
            _refund_premium(g.uid, poolp)
            return jsonify({
                "error": "premium_gagal", "model": _MODEL_LABEL[pilihan],
                "pesan": _MODEL_LABEL[pilihan] + " lagi bermasalah saat "
                         "ini. Saldo kamu tidak terpotong."}), 503
        sai.track_tool("tanya", g.uid)
        if g.user:
            histmod.catat(g.uid, "chat", q,
                          {"sumber": "tanya", "tipe_jawab": "ai",
                           "q": q[:500], "jawaban": jawaban})
        return jsonify({"tipe": "ai", "jawaban": jawaban, "premium": True,
                        "model_label": info.get("label"),
                        "saldo": sai.ai_balance(g.uid),
                        "sisa": _ai_left_uid(g.uid)})
    pool = _ai_consume_uid(g.uid)
    if not pool:
        return jsonify({"error": "kuota_habis",
                        "pesan": pesan_ai_habis()}), 429
    try:
        jawaban, info = _run(tanya_mod.gemini_answer_pilihan(
            q[:500], pilihan=pilihan))
    except Exception as e:
        log.warning("tanya ai error: %s", e)
        jawaban, info = None, {}
    if not jawaban:
        _ai_refund_uid(g.uid, pool)
        tanya_mod.log_unanswered(q[:500])
        return _err("belum bisa menjawab sekarang, coba lagi nanti", 502)
    sai.track_tool("tanya", g.uid)
    if g.user:
        histmod.catat(g.uid, "chat", q,
                      {"sumber": "tanya", "tipe_jawab": "ai",
                       "q": q[:500], "jawaban": jawaban})
    return jsonify({"tipe": "ai", "jawaban": jawaban,
                    "model_label": info.get("label"),
                    "dialihkan": bool(info.get("dialihkan")),
                    "model_pilihan": _MODEL_LABEL[pilihan],
                    "sisa": _ai_left_uid(g.uid)})


# ---------------------------------------------------------------- riwayat
def _riwayat_need_login():
    """401 + flag need_login bila tamu (biar frontend tampilkan ajakan login)."""
    if not g.user:
        return jsonify({"error": "login dulu ya 🔐",
                        "need_login": True}), 401
    return None


@app.get("/api/history")
@with_rl  # S1 (audit 2026-10-13): endpoint riwayat wajib rate-limited
def history_list():
    err = _riwayat_need_login()
    if err:
        return err
    tipe_raw = request.args.get("type")
    tipe = (tipe_raw or "").strip().lower() or None
    # R1 (audit 2026-10-13): type invalid -> 400, jangan fallback ke semua tipe
    if tipe_raw and tipe not in histmod.TIPE_VALID:
        return _err("tipe tidak dikenal", 400)
    q = (request.args.get("q") or "").strip()
    try:
        limit = int(request.args.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    entri = histmod.daftar(g.uid, tipe=tipe, q=q, limit=limit)
    # daftar ringan: tanpa blob data (detail diambil per id)
    ringan = [{"id": e.get("id"), "tipe": e.get("tipe"),
               "judul": e.get("judul"), "waktu": e.get("waktu")}
              for e in entri]
    return jsonify({"riwayat": ringan, "total": len(ringan)})


@app.get("/api/history/<eid>")
@with_rl  # S1 (audit 2026-10-13)
def history_detail(eid):
    err = _riwayat_need_login()
    if err:
        return err
    e = histmod.ambil(g.uid, eid)
    if not e:
        return _err("riwayat tidak ketemu", 404)
    return jsonify({"riwayat": e})


@app.delete("/api/history/<eid>")
@with_rl  # S1 (audit 2026-10-13)
def history_delete(eid):
    err = _riwayat_need_login()
    if err:
        return err
    if not histmod.hapus(g.uid, eid):
        return _err("riwayat tidak ketemu", 404)
    return jsonify({"ok": True})


@app.delete("/api/history")
@with_rl  # S1 (audit 2026-10-13)
def history_clear():
    err = _riwayat_need_login()
    if err:
        return err
    tipe_raw = request.args.get("type")
    tipe = (tipe_raw or "").strip().lower() or None
    # R1 (audit 2026-10-13): type invalid -> 400 (jangan hapus semua tipe diam-diam)
    if tipe_raw and tipe not in histmod.TIPE_VALID:
        return _err("tipe tidak dikenal", 400)
    n = histmod.hapus_tipe(g.uid, tipe)
    return jsonify({"ok": True, "dihapus": n})


# ---------------------------------------------------------------- kuota
@app.get("/api/kuota")
@with_rl  # R-2 (audit 2026-10-17)
def kuota():
    harian, saldo = _ai_info_uid(g.uid)
    return jsonify({"sisa": harian, "saldo": saldo,
                    "premium": _ai_premium_uid(g.uid),
                    "admin": _is_web_admin()})


# ---------------------------------------------------------------- topup jatah AI
@app.get("/api/topup")
@with_rl  # R-2 (audit 2026-10-17)
def topup_info():
    harian, saldo = _ai_info_uid(g.uid)
    return jsonify({
        "harga_rp": ppay.TOPUP_PRICE if ppay else 5000,
        "uses": ppay.TOPUP_USES if ppay else 25,
        "min_uses": ppay.TOPUP_MIN_USES if ppay else 25,
        "harga_per_use": ppay.TOPUP_PRICE_PER_USE if ppay else 200,
        "pay_enabled": ppay.pay_enabled() if ppay else False,
        "sisa_harian": harian,
        "saldo": saldo,
        "admin": _is_web_admin(),
    })


@app.post("/api/topup/create")
@with_rl
def topup_create():
    """Buat invoice QRIS top-up. Jumlah jatah bebas (minimal 25x),
    Rp200 per 1x. Frontend polling /api/topup/status/<ref_id>.

    Wajib login: saldo yang dibeli harus nempel ke akun, bukan sesi tamu.
    """
    if not ppay or not ppay.pay_enabled():
        return _err("pembayaran belum aktif", 503)
    if not g.user:
        return jsonify({"error": "login dulu biar saldomu aman",
                        "need_login": True}), 401
    if ppay.pending_for_user(g.uid):
        return _err("kamu sudah punya kode bayar aktif", 409)
    # S-6: plafon 5 invoice / akun / hari (di samping 1 pending / jam).
    if ppay.creates_hari_ini(g.uid) >= ppay._INVOICE_HARIAN_MAX:
        return _err("kamu sudah mencapai batas pembuatan kode bayar hari ini, coba lagi besok ya", 429)
    body = request.get_json(silent=True) or {}
    try:
        uses = int(body.get("uses") or 0)
    except (TypeError, ValueError):
        uses = 0
    min_uses = ppay.TOPUP_MIN_USES
    max_uses = 10000  # Rp2 juta; cegah nominal raksasa yang lolos gross-up
    if uses < min_uses:
        return _err(f"minimal top-up {min_uses}x jatah AI", 400)
    if uses > max_uses:
        return _err(f"maksimal top-up {max_uses}x jatah AI per transaksi", 400)
    harga = uses * ppay.TOPUP_PRICE_PER_USE
    ppay.catat_create(g.uid)  # S-6: hitung ke plafon harian
    inv = _run(ppay.create_invoice(
        harga, f"KhairyPustaka Web +{uses}x AI",
        g.uid, "web"))
    if not inv:
        return _err("gagal membuat kode bayar", 502)
    ref_id, qr_image_field, qris_string, final = inv
    ppay.pending_add(ref_id, g.uid, "web", uses=uses, amount=harga)
    png = _run(ppay.qr_png_from_api(qr_image_field, qris_string))
    import base64 as _b64
    qr_b64 = _b64.b64encode(png).decode() if png else None
    if qr_b64:
        # simpan QR agar bisa ditampilkan ulang bila user menutup tab
        try:
            ppay.pending_set(ref_id, qr_b64=qr_b64)
        except Exception as e:
            log.warning("simpan qr pending gagal: %s", e)
    return jsonify({"ok": True, "ref_id": ref_id, "qr_b64": qr_b64,
                    "total": final, "harga": harga,
                    "uses": uses})


@app.get("/api/topup/pending")
@with_rl  # R-2 (audit 2026-10-17)
def topup_pending():
    """Kembalikan invoice pending milik user (untuk resume bila tab tertutup).

    Wajib login. Return {"pending": {...}} atau {"pending": None}.
    """
    if not ppay:
        return jsonify({"pending": None})
    if not g.user:
        return jsonify({"error": "login dulu biar saldomu aman",
                        "need_login": True}), 401
    ref_id = ppay.pending_for_user(g.uid)
    if not ref_id:
        return jsonify({"pending": None})
    p = (ppay.pending_all() or {}).get(ref_id) or {}
    return jsonify({"pending": {
        "ref_id": ref_id,
        "uses": p.get("uses"),
        "amount": p.get("amount"),
        "qr_b64": p.get("qr_b64"),
        "ts": p.get("ts"),
    }})


# Throttle cek status invoice per ref_id: tiap hit = 1x HTTPS outbound ke
# WijayaPay. Tanpa throttle, attacker bisa loop endpoint ini tanpa batas dan
# membakar kuota API merchant. Polling legit frontend tiap 8 detik tetap jalan
# karena hasil "belum bayar" di-cache 10 detik (deteksi bayar telat maks ~10 dtk).
_STATUS_THROTTLE = {}
_STATUS_THROTTLE_LOCK = threading.Lock()
_STATUS_THROTTLE_SECS = 10


@app.get("/api/topup/status/<ref_id>")
@with_rl  # R-2 (audit 2026-10-17)
def topup_status(ref_id):
    if not ppay:
        return _err("pembayaran belum aktif", 503)
    p = (ppay.pending_all() or {}).get(ref_id)
    if not p or str(p.get("chat_id")) != str(g.uid):
        return _err("invoice tidak ditemukan", 404)
    now = time.time()
    with _STATUS_THROTTLE_LOCK:
        last = _STATUS_THROTTLE.get(ref_id, 0)
        if now - last < _STATUS_THROTTLE_SECS:
            return jsonify({"ok": True, "paid": False})
        _STATUS_THROTTLE[ref_id] = now
    data = _run(ppay.check_invoice(ref_id)) or {}
    status = str(data.get("status", "")).lower()
    if status in ("paid", "success", "settlement", "completed"):
        # klaim atomik: hanya 1 request yang boleh mengkredit invoice ini
        if not ppay.pending_claim(ref_id):
            return jsonify({"ok": True, "paid": False,
                            "pesan": "pembayaran sudah diproses di tab lain"})
        uses = int(p.get("uses") or ppay.TOPUP_USES)
        new_bal = sai.ai_topup(g.uid, uses)
        ppay.pending_drop(ref_id)
        with _STATUS_THROTTLE_LOCK:
            _STATUS_THROTTLE.pop(ref_id, None)
        return jsonify({"ok": True, "paid": True,
                        "uses": uses, "saldo": new_bal})
    return jsonify({"ok": True, "paid": False})


# R-1 (audit 2026-10-17): perlambat tebakan kode redeem per akun.
# Ruang kode 30^6 sudah besar, tapi pola ini menutup skrip yang menebak
# beruntun dari satu akun curian/terbatas: 10x salah dalam 10 menit ->
# jeda paksa (429) sampai jendela bergeser. Sukses me-reset hitungan.
_REDEEM_FAIL = {}
_REDEEM_FAIL_MAX, _REDEEM_FAIL_WINDOW = 10, 600


def _redeem_gagal_terakhir(uid):
    now = time.time()
    lst = [t for t in _REDEEM_FAIL.get(uid, []) if now - t < _REDEEM_FAIL_WINDOW]
    _REDEEM_FAIL[uid] = lst
    return len(lst)


def _redeem_cat_gagal(uid):
    _REDEEM_FAIL.setdefault(uid, []).append(time.time())
    if len(_REDEEM_FAIL) > 5000:
        now = time.time()
        for k in [k for k, v in _REDEEM_FAIL.items()
                  if not v or now - v[-1] >= _REDEEM_FAIL_WINDOW]:
            del _REDEEM_FAIL[k]


def _redeem_reset(uid):
    _REDEEM_FAIL.pop(uid, None)


@app.post("/api/redeem")
@with_rl
def redeem():
    # Wajib login: saldo redeem harus nempel ke akun (bukan sesi tamu yang
    # hilang saat cookie berganti), sekaligus menutup brute-force kode anonim.
    if not g.user:
        return jsonify({"error": "login dulu biar saldomu aman",
                        "need_login": True}), 401
    if _redeem_gagal_terakhir(g.uid) >= _REDEEM_FAIL_MAX:
        return _err("kebanyakan tebakan salah, coba lagi 10 menit ya", 429)
    body = request.get_json(silent=True) or {}
    _kode = body.get("kode")
    kode = _kode.strip() if isinstance(_kode, str) else ""
    if not kode:
        return _err("kode kosong")
    ok, res = redmod.redeem(kode, g.uid)
    if not ok:
        _redeem_cat_gagal(g.uid)
        return jsonify({"ok": False, "alasan": res})
    _redeem_reset(g.uid)
    new_bal = sai.ai_topup(g.uid, res)
    return jsonify({"ok": True, "uses": res, "saldo": new_bal})


# ------------------------------------------------------------------- admin
# Panel Admin: halaman statistik + generate kode jatah AI + tambah saldo AI
# member. Pengganti tools admin bot Telegram yang sudah dipensiunkan.
# Identitas admin = daftar username di env ADMIN_USERNAMES (dipisah koma,
# tidak peka huruf besar kecil). Kosong/tidak diset -> tidak ada yang admin.
def _admin_usernames():
    raw = os.environ.get("ADMIN_USERNAMES", "") or ""
    return {n.strip().lower() for n in raw.split(",") if n.strip()}


def is_admin_user(user):
    """True bila user cocok dengan salah satu nama di ADMIN_USERNAMES.

    Hanya pid (username ternormalisasi) yang dicocokkan. nama/email
    SENGAJA tidak dipakai: kolom itu bebas diisi sehingga suatu hari bisa
    dipakai eskalasi bila provider login nama-bebas diaktifkan
    (S-1 audit 2026-10-17).
    """
    if not user:
        return False
    allowed = _admin_usernames()
    if not allowed:
        return False
    pid = str(user.get("pid") or "").strip().lower()
    return bool(pid) and pid in allowed


def _need_admin():
    if not g.user or not is_admin_user(g.user):
        return jsonify({"ok": False, "error": "khusus admin"}), 403
    return None


def _is_web_admin():
    """True bila request saat ini datang dari akun admin web."""
    return bool(g.user) and is_admin_user(g.user)


def _tamu_web_uid(uid):
    """True bila uid adalah tamu web ("web_*"), bukan member login."""
    return isinstance(uid, str) and uid.startswith("web_")


def _ai_consume_uid(uid):
    """Reservasi 1 jatah AI untuk request web.

    Admin web tidak pernah kehabisan: kembalikan "harian" tanpa
    mengurangi apa pun (pool ini dikenali semua call site sebagai sukses).
    Tamu web memakai hitung ganda (ember identitas + ember alamat IP).
    Member login memakai jalur normal sai.ai_try_consume apa adanya.
    """
    if _is_web_admin():
        return "harian"
    if _tamu_web_uid(uid):
        # Ember gabungan tamu (AI + non-AI): keputusan Sul 2026-10-18.
        return "harian" if sai.guest_all_try_consume(uid, _ip()) else None
    return sai.ai_try_consume(uid)


def _ai_refund_uid(uid, pool):
    """Kembalikan 1 jatah AI yang gagal dipakai.

    Tamu web: kembalikan ke ember identitas DAN ember alamat IP
    (cerminan hitung ganda di _ai_consume_uid). Member: jalur normal.
    """
    if _tamu_web_uid(uid):
        sai.guest_all_refund(uid, _ip())
    else:
        sai.ai_refund(uid, pool)


def _ai_left_uid(uid):
    """Sisa jatah AI harian; admin web selalu 999 (tanpa batas).

    Tamu: min(ember identitas, ember alamat IP) supaya tampilan tidak
    menjanjikan pemakaian yang akan diblokir plafon IP.
    """
    if _is_web_admin():
        return 999
    if _tamu_web_uid(uid):
        return sai.guest_all_left(uid, _ip())
    return sai.ai_quota_left(uid)


def _ai_info_uid(uid):
    """(sisa_harian, saldo); admin web selalu (999, saldo).

    Tamu: sisa_harian = min(identitas, IP) — lihat _ai_left_uid.
    """
    if _is_web_admin():
        return 999, sai.ai_balance(uid)
    if _tamu_web_uid(uid):
        return sai.guest_all_left(uid, _ip()), sai.ai_balance(uid)
    return sai.ai_quota_info(uid)


def _ai_premium_uid(uid):
    """Admin web diperlakukan premium, seperti owner di bot Telegram."""
    return _is_web_admin() or sai.is_premium(uid)


# ------------------------------------------------------------ pemilih model
# Fitur pemilih model (keputusan Sul 2026-10-19). Kunci pilihan dari klien
# divalidasi terhadap daftar putih ini; nilai apa pun di luar daftar
# diperlakukan sebagai "auto" dan TIDAK PERNAH bisa menjadi premium.
_MODEL_PILIHAN = {"auto", "gptoss", "qwen", "gemini", "llama",
                  "claude", "sonar"}
_MODEL_PREMIUM = {"claude", "sonar"}
_MODEL_LABEL = {"auto": "Otomatis", "gptoss": "GPT-OSS 120B",
                "qwen": "Qwen 3.8 27B", "gemini": "Gemini",
                "llama": "Llama 3.3 70B", "claude": "Claude Haiku 4.5",
                "sonar": "Perplexity Sonar"}


def _pilihan_model(body):
    v = str((body or {}).get("model") or "auto").strip().lower()
    return v if v in _MODEL_PILIHAN else "auto"


def _gerbang_premium(uid):
    """Gerbang server jalur premium. Return (pool, err_response).

    Lolos -> pool "saldo" (1x saldo SUDAH direservasi atomik) atau "admin"
    (tanpa potongan). Gagal -> err_response 403 premium_terkunci.
    Ini SATU-SATUNYA pintu model premium: wajib login + saldo top-up > 0.
    Reservasi dikembalikan pemanggil via _refund_premium bila generate
    premium gagal, jadi saldo hanya terpotong bila premium benar-benar
    berhasil melayani.
    """
    if _is_web_admin():
        return "admin", None
    if not g.user or _tamu_web_uid(uid):
        return None, (jsonify({
            "error": "premium_terkunci",
            "pesan": "Model premium khusus member yang punya saldo top-up. "
                     "Login dulu yuk."}), 403)
    pool = sai.ai_try_consume_saldo(uid)
    if not pool:
        return None, (jsonify({
            "error": "premium_terkunci",
            "pesan": "Saldo top-up kamu kosong. Top up dulu buat pakai "
                     "model premium ya."}), 403)
    return pool, None


def _refund_premium(uid, pool):
    if pool == "saldo":
        sai.ai_refund(uid, "saldo")


def _wita_midnight_ts():
    """Epoch detik awal hari ini (zona WITA)."""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.now(ZoneInfo("Asia/Makassar")).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return dt.timestamp()
    except Exception:
        return 0


def _agregasi_pakai(store, today):
    """{tool: {total, days}} -> (per_fitur list, hari_ini, total)."""
    rows, h, tot = [], 0, 0
    if isinstance(store, dict):
        for nama, t in store.items():
            if not isinstance(t, dict):
                continue
            days = t.get("days") or {}
            th = int(days.get(today, 0) or 0)
            tt = int(t.get("total", 0) or 0)
            h += th
            tot += tt
            rows.append({"nama": nama, "hari_ini": th, "total": tt})
    rows.sort(key=lambda r: r["total"], reverse=True)
    return rows, h, tot


@app.get("/api/admin/statistik")
@with_rl
def admin_statistik():
    err = _need_admin()
    if err:
        return err
    today = _today_wita()
    users = authmod.list_users() or {}
    per_provider = {}
    baru_hari_ini = 0
    midnight = _wita_midnight_ts()
    for u in users.values():
        if not isinstance(u, dict):
            continue
        prov = u.get("provider") or "lain"
        per_provider[prov] = per_provider.get(prov, 0) + 1
        try:
            if int(u.get("created") or 0) >= midnight > 0:
                baru_hari_ini += 1
        except (TypeError, ValueError):
            pass
    qd = sai._load() if hasattr(sai, "_load") else {}
    tools_ai = qd.get("tools") or {}
    ai_rows, ai_h, ai_t = _agregasi_pakai(tools_ai, today)
    prem = qd.get("premium") or {}
    premium_n = sum(1 for exp in (prem.values() if isinstance(prem, dict)
                                 else []) if isinstance(exp, str) and exp >= today)
    saldo_total = 0
    bal = qd.get("ai_balance") or {}
    if isinstance(bal, dict):
        for v in bal.values():
            try:
                saldo_total += int(v or 0)
            except (TypeError, ValueError):
                pass
    nonai_store = dbstore.load_json(_USAGE_PATH, {})
    nonai_rows, nonai_h, nonai_t = _agregasi_pakai(nonai_store, today)
    return jsonify({"ok": True, "hari_ini": today,
                    "member": {"total": len(users),
                               "per_provider": per_provider,
                               "baru_hari_ini": baru_hari_ini,
                               "premium": premium_n},
                    "ai": {"hari_ini": ai_h, "total": ai_t,
                           "per_fitur": ai_rows},
                    "nonai": {"hari_ini": nonai_h, "total": nonai_t,
                              "per_fitur": nonai_rows},
                    "saldo_ai_web_total": saldo_total})


@app.post("/api/admin/kode")
@with_rl
def admin_kode_generate():
    err = _need_admin()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    try:
        jumlah = int(body.get("jumlah") or 0)
    except (TypeError, ValueError):
        jumlah = 0
    try:
        nominal = int(body.get("nominal") or 0)
    except (TypeError, ValueError):
        nominal = 0
    if not 1 <= jumlah <= redmod.MAX_GEN:
        return _err("jumlah harus 1 sampai 200", 400)
    if not 1 <= nominal <= 10000:
        return _err("nominal harus 1 sampai 10000", 400)
    try:
        codes = redmod.generate(nominal, jumlah)
    except (ValueError, RuntimeError):
        return _err("gagal generate kode, coba lagi", 500)
    return jsonify({"ok": True, "kode": codes, "jumlah": len(codes),
                    "nominal": nominal})


@app.get("/api/admin/kode")
@with_rl
def admin_kode_list():
    err = _need_admin()
    if err:
        return err
    out = []
    for kode, nominal, used_by, used_at, created in reversed(redmod.list_codes()):
        dipakai_oleh = ""
        if used_by:
            u = authmod.get_user(used_by)
            dipakai_oleh = ((u.get("nama") or u.get("pid")) if u
                            else used_by) or used_by
        out.append({"kode": kode, "nominal": nominal,
                    "status": "sudah dipakai" if used_by else "belum dipakai",
                    "dipakai_oleh": dipakai_oleh, "dipakai_pada": used_at,
                    "dibuat_pada": created})
    return jsonify({"ok": True, "kode": out, "total": len(out)})


@app.get("/api/admin/member")
@with_rl
def admin_member_cari():
    err = _need_admin()
    if err:
        return err
    q = (request.args.get("q") or "").strip().lower()
    users = authmod.list_users() or {}
    out = []
    for uid, u in users.items():
        if not isinstance(u, dict):
            continue
        if q and q not in str(u.get("pid") or "").lower() \
                and q not in str(u.get("nama") or "").lower() \
                and q not in str(u.get("email") or "").lower():
            continue
        out.append({"uid": uid, "username": u.get("pid") or "",
                    "nama": u.get("nama") or "", "email": u.get("email") or "",
                    "provider": u.get("provider") or "",
                    "saldo": sai.ai_balance(uid),
                    "sisa_harian": sai.ai_quota_left(uid)})
        if len(out) >= 50:
            break
    return jsonify({"ok": True, "member": out, "total": len(out)})


@app.post("/api/admin/grant")
@with_rl
def admin_grant():
    err = _need_admin()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    uid = (body.get("uid") or "").strip()
    try:
        jumlah = int(body.get("jumlah") or 0)
    except (TypeError, ValueError):
        jumlah = 0
    if not uid:
        return _err("uid kosong", 400)
    if not 1 <= jumlah <= 100000:
        return _err("jumlah harus 1 sampai 100000", 400)
    u = authmod.get_user(uid)
    if not u:
        return jsonify({"ok": False,
                        "error": "member tidak ditemukan"}), 404
    new_bal = sai.ai_topup(uid, jumlah)
    return jsonify({"ok": True, "saldo": new_bal, "jumlah": jumlah,
                    "username": u.get("pid") or "",
                    "nama": u.get("nama") or ""})


# ------------------------------------------------- admin: backup & hapus
_BACKUP_STORES = ("users.json", "tokens.json", "ai_quota.json", "usage.json",
                  "guest_usage.json", "koleksi.json", "riwayat.json",
                  "referral.json", "redeem.json", "favorit.json",
                  "pantau.json", "premium_pending.json", "deadline.json",
                  "homepages.json")


@app.get("/api/admin/backup")
@with_rl
def admin_backup():
    """Unduh seluruh data aplikasi sebagai satu file JSON (khusus admin).
    Cadangan manual; pasangan backup otomatis mingguan via GitHub Action."""
    err = _need_admin()
    if err:
        return err
    sentinel = object()
    isi = {}
    for name in _BACKUP_STORES:
        try:
            doc = dbstore.load_json("data/" + name, sentinel)
        except Exception:
            doc = sentinel
        if doc is not sentinel and doc is not None:
            isi["data/" + name] = doc
    paket = {"aplikasi": "KhairyPustaka", "versi_backup": 1,
             "dibuat": _today_wita(), "data": isi}
    return send_file(
        BytesIO(json.dumps(paket, ensure_ascii=False, indent=1).encode()),
        mimetype="application/json", as_attachment=True,
        download_name="khairypustaka-backup-%s.json" % _today_wita())


@app.post("/api/backup-snapshot")
@with_rl
def backup_snapshot():
    """Snapshot seluruh kp_kv untuk cadangan eksternal otomatis.

    Berkunci env BACKUP_KEY lewat header X-Backup-Key (bukan sesi):
    dipakai cron harian di luar Render supaya pemulihan tidak
    bergantung riwayat 6 jam Neon Free (insiden 2026-10-19). Tanpa
    kunci terpasang atau kunci salah -> 404 seperti path sensitif lain.
    """
    kunci = (os.environ.get("BACKUP_KEY") or "").strip()
    if not kunci:
        return _err("tidak ditemukan", 404)
    import hmac as _hmac
    diberi = request.headers.get("X-Backup-Key") or ""
    if not _hmac.compare_digest(diberi, kunci):
        return _err("tidak ditemukan", 404)
    data = dbstore.dump_all()
    return jsonify({"aplikasi": "KhairyPustaka", "versi_backup": 2,
                    "dibuat": _today_wita(), "data": data})


def _sweep_uid(doc, uid, depth=0):
    """Hapus semua entri berkunci uid dari dokumen (rekursif maks 2 lapis).
    Return True bila ada perubahan."""
    berubah = False
    if isinstance(doc, dict):
        if uid in doc:
            del doc[uid]
            berubah = True
        if depth < 2:
            for v in list(doc.values()):
                if isinstance(v, dict) and _sweep_uid(v, uid, depth + 1):
                    berubah = True
    return berubah


@app.post("/api/admin/hapus-user")
@with_rl
def admin_hapus_user():
    """Hapus permanen satu akun password dari username-nya (khusus admin).
    Membersihkan juga token login serta entri berkunci uid di store lain.
    Akun admin dan akun sendiri tidak bisa dihapus."""
    err = _need_admin()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    uname = (body.get("username") or "").strip().lower()
    if not uname:
        return _err("username wajib diisi", 400)
    users = authmod.list_users() or {}
    target_uid, target = None, None
    for uid, u in users.items():
        if isinstance(u, dict) and u.get("provider") == "password" \
                and (u.get("pid") or "").lower() == uname:
            target_uid, target = uid, u
            break
    if not target_uid:
        return _err("akun dengan username itu tidak ditemukan", 404)
    if is_admin_user(target):
        return _err("akun admin tidak boleh dihapus", 403)
    if target_uid == getattr(g, "login_uid", None):
        return _err("tidak bisa menghapus akun sendiri", 403)
    dibersihkan = []
    # token login/verifikasi milik uid ini
    try:
        tok = dbstore.load_json(authmod._TOKENS_FILE, {})
        if isinstance(tok, dict):
            sisa = {k: v for k, v in tok.items()
                    if not (isinstance(v, dict) and v.get("uid") == target_uid)}
            if len(sisa) != len(tok):
                dbstore.save_json(authmod._TOKENS_FILE, sisa)
                dibersihkan.append("tokens")
    except Exception as e:
        log.warning("hapus-user: sapu token gagal: %s", e)
    for name in ("ai_quota.json", "referral.json", "riwayat.json",
                 "koleksi.json", "favorit.json"):
        try:
            doc = dbstore.load_json("data/" + name, {})
            if isinstance(doc, dict) and _sweep_uid(doc, target_uid):
                dbstore.save_json("data/" + name, doc)
                dibersihkan.append(name)
        except Exception as e:
            log.warning("hapus-user: sapu %s gagal: %s", name, e)
    if not authmod.delete_user(target_uid):
        return _err("gagal menghapus akun, coba lagi", 502)
    return jsonify({"ok": True, "username": uname, "uid": target_uid,
                    "dibersihkan": dibersihkan})


@app.get("/api/kode-referral")
@with_rl  # R-2 (audit 2026-10-17)
def kode_referral():
    # Satukan hitungan referral lama ke identitas Telegram (idempoten;
    # menutup celah user yang login Telegram sebelum patch sync terpasang).
    try:
        tg = authmod.linked_tg_id(g.uid)
        if tg:
            refmod.merge_referral_to_tg([g.uid], tg)
    except Exception as e:
        log.warning("merge referral otomatis gagal: %s", e)
    return jsonify({"kode": refmod.my_code(g.uid),
                    "terkumpul": refmod.count(g.uid),
                    "butuh_berikutnya": refmod.need_for_next(g.uid),
                    "reward_uses": refmod.REWARD_USES})


@app.get("/api/ref")
@with_rl
def ref_register():
    """Daftarkan referral ?ref=KP-XXXXXX.

    K-1 (audit 2026-10-17): WAJIB login DAN email terverifikasi. Tamu
    tidak pernah dihitung dan tidak pernah memicu reward — menutup
    farming referral tanpa akun. Frontend menyimpan kode di localStorage
    dan memanggil endpoint ini setelah user login/verifikasi.
    """
    if not g.user:
        return jsonify({"ok": False, "need_login": True}), 401
    if g.user.get("provider") != "password" or not g.user.get("email_verified"):
        return jsonify({"ok": False, "perlu_verifikasi": True}), 403
    kode = (request.args.get("kode") or "").strip()
    if not kode:
        return jsonify({"ok": False})
    try:
        rid, milestone = refmod.register(g.uid, kode)
    except Exception:
        return jsonify({"ok": False})
    tercatat = rid is not None
    if tercatat:
        # Event share: join ini juga dihitung untuk tangga event referrer
        # (kantong terpisah; reward referral lama di atas tetap jalan).
        try:
            refmod.event_record_join(rid, refmod.event_key(g.uid))
        except Exception as e:
            log.warning("event share join gagal dicatat: %s", e)
    if milestone:
        new_bal = sai.ai_topup(rid, refmod.REWARD_USES)
        return jsonify({"ok": True, "tercatat": tercatat, "milestone": True,
                        "reward": refmod.REWARD_USES, "saldo": new_bal})
    return jsonify({"ok": True, "tercatat": tercatat, "milestone": False})


@app.get("/api/event-share")
@with_rl
def event_share_status():
    """Status event share untuk sesi ini + kode referral pemiliknya."""
    return jsonify({**refmod.event_status(g.uid),
                    "kode": refmod.my_code(g.uid)})


@app.post("/api/event-share/share")
@with_rl
def event_share_claim():
    """Catat share event (sekali per akun; share ulang tidak nambah).

    Wajib login: klaim tamu tidak dicatat, supaya satu orang tidak bisa
    mengulang klaim lewat sesi tamu baru. Tamu yang login akan membawa
    data eventnya lewat migrate_guest.
    """
    if not g.user:
        return jsonify({"ok": False, "need_login": True}), 401
    st, baru = refmod.event_claim_share(g.uid)
    return jsonify({"ok": True, "baru": baru, **st})


# ------------------------------------------------------------------- auth
def _auth_status_payload():
    if g.user:
        prov = g.user.get("provider")
        return {"login": True, "nama": g.user.get("nama") or "Pengguna",
                "provider": prov, "foto": g.user.get("foto") or "",
                "username": g.user.get("pid") if prov == "password" else "",
                "email": g.user.get("email") or "",
                "avatar": authmod.user_avatar(g.user),
                "tg_linked": bool(authmod.linked_tg_id(g.user)),
                "admin": is_admin_user(g.user)}
    return {"login": False}


@app.get("/api/auth/status")
def auth_status():
    return jsonify(_auth_status_payload())


@app.get("/api/auth/google")
def auth_google():
    """Login Google DINONAKTIFKAN (keputusan Sul 2026-10-11).

    Route sengaja dimatikan total, bukan sekadar tombol dicabut, agar tidak
    ada jalur login CSRF via callback tanpa verifikasi state."""
    return _err("login Google dinonaktifkan", 404)


@app.get("/api/auth/google/callback")
def auth_google_callback():
    """Login Google DINONAKTIFKAN (keputusan Sul 2026-10-11). Lihat auth_google."""
    return _err("login Google dinonaktifkan", 404)


@app.post("/api/auth/logout")
def auth_logout():
    resp = jsonify({"ok": True})
    # hapus dengan atribut yang SAMA seperti saat cookie dibuat (Secure),
    # kalau tidak browser bisa menolak menghapus cookie Secure.
    resp.delete_cookie(authmod.LOGIN_COOKIE, samesite="Lax", secure=True)
    return resp


def _pw_client_ip():
    # Sama seperti _ip() (CF-Connecting-IP hanya bila peer tepercaya,
    # tanpa fallback X-Forwarded-For — lihat catatan di _ip).
    return _cf_trusted_ip() or request.remote_addr or "?"


@app.post("/api/auth/register")
@with_rl
def auth_register():
    """Daftar akun username+password+email. Akun aktif setelah verifikasi email."""
    if not mailmod.mail_configured():
        return _err("pendaftaran belum aktif (email admin belum dikonfigurasi)", 503)
    body = request.get_json(silent=True) or {}
    uid, err = authmod.register_password(body.get("username"),
                                         body.get("password") or "",
                                         body.get("email") or "",
                                         ref_code=body.get("ref"))
    if err:
        return _err(err, 400)
    tok = authmod.issue_token(uid, "verify", authmod.VERIFY_TTL)
    user = authmod.get_user(uid)
    if not _mail_addr_cek(user.get("email")):
        return _err("batas pengiriman email ke alamat ini tercapai, coba lagi sejam ya", 429)
    ok, merr = mailmod.send_verify_email(user.get("email"), user.get("nama") or "kamu", tok)
    if not ok:
        log.warning("email verifikasi gagal untuk %s: %s", uid, merr)
        return _err("akun dibuat tapi email verifikasi gagal terkirim — coba kirim ulang dari halaman Masuk", 502)
    return jsonify({"ok": True, "need_verify": True,
                    "msg": "cek email kamu buat verifikasi 📧"})


@app.post("/api/auth/verifikasi")
@with_rl  # R-2 (audit 2026-10-17)
def auth_verifikasi():
    """Verifikasi email dari link. Langsung login setelah berhasil."""
    body = request.get_json(silent=True) or {}
    uid = authmod.consume_token(body.get("token"), "verify")
    if not uid:
        return _err("link verifikasi nggak valid / sudah kedaluwarsa 😅", 400)
    authmod.set_email_verified(uid)
    # K-1: referral HANYA dihitung untuk akun yang emailnya terverifikasi.
    # Kode yang dibawa saat daftar diproses tepat di titik ini (sekali saja).
    try:
        kode_ref = authmod.consume_pending_ref(uid)
        if kode_ref:
            rid, milestone = refmod.register(uid, kode_ref)
            if rid:
                # Event share: join via verifikasi email juga dihitung.
                try:
                    refmod.event_record_join(rid, refmod.event_key(uid))
                except Exception as e:
                    log.warning("event share join gagal dicatat: %s", e)
            if milestone:
                sai.ai_topup(rid, refmod.REWARD_USES)
    except Exception as e:
        log.warning("proses pending_ref gagal untuk %s: %s", uid, e)
    # Email sambutan member baru (2026-10-18): sekali saja, tepat di titik
    # verifikasi berhasil (token hanya bisa dipakai sekali). Gagal kirim
    # tidak boleh menggagalkan verifikasi.
    try:
        _u = authmod.get_user(uid)
        if _u and _u.get("email"):
            mailmod.send_welcome_email(_u["email"],
                                       _u.get("nama") or "kamu")
    except Exception as e:
        log.warning("email sambutan gagal untuk %s: %s", uid, e)
    _do_login(uid, "web_" + g.sid if not g.user else g.uid)
    g.user = authmod.get_user(uid)
    g.uid = uid
    resp = jsonify({"ok": True, **_auth_status_payload()})
    _set_login_cookie(resp, uid)
    return resp


@app.post("/api/auth/kirim-ulang")
@with_rl  # R-2 (audit 2026-10-17)
def auth_kirim_ulang():
    """Kirim ulang email verifikasi (rate-limited)."""
    ip = _pw_client_ip()
    if not _mail_allowed(ip):
        return _err("kebanyakan permintaan, coba lagi 10 menit ya", 429)
    if not mailmod.mail_configured():
        return _err("email belum dikonfigurasi admin", 503)
    body = request.get_json(silent=True) or {}
    _mail_hit(ip)
    ident = body.get("email") or body.get("username") or body.get("identifier")
    uid, user = authmod.find_password_user_by_identifier(ident)
    if uid and not user.get("email_verified"):
        if not _mail_addr_cek(user.get("email")):
            return _err("batas pengiriman email ke alamat ini tercapai, coba lagi sejam ya", 429)
        tok = authmod.issue_token(uid, "verify", authmod.VERIFY_TTL)
        mailmod.send_verify_email(user.get("email"), user.get("nama") or "kamu", tok)
    return jsonify({"ok": True, "msg": "kalau email terdaftar & belum verifikasi, link dikirim 📧"})


@app.post("/api/auth/ganti-email-pending")
def auth_ganti_email_pending():
    """Ganti email akun password yang belum terverifikasi.

    Wajib username/email lama + password yang benar. Setelah alamat diganti,
    token verifikasi lama dibatalkan dan link baru dikirim ke alamat baru.
    """
    ip = _pw_client_ip()
    if not _mail_allowed(ip):
        return _err("kebanyakan permintaan email, coba lagi 10 menit ya", 429)
    if not mailmod.mail_configured():
        return _err("email belum dikonfigurasi admin", 503)
    body = request.get_json(silent=True) or {}
    ident = body.get("username") or body.get("email") or body.get("identifier")
    if not authmod.login_allowed(ip) or \
            not authmod.login_user_allowed(ident):
        return _err("kebanyakan percobaan, coba lagi 10 menit ya", 429)
    uid = authmod.login_password(ident, body.get("password") or "")
    if not uid:
        authmod.login_hit(ip)
        authmod.login_user_hit(ident)
        return _err("username/email atau password salah", 401)
    authmod.login_user_clear(ident)
    mail, err = authmod.replace_unverified_email(uid, body.get("email_baru") or "")
    if err:
        return _err(err, 400)
    _mail_hit(ip)
    user = authmod.get_user(uid) or {}
    if not _mail_addr_cek(mail):
        return _err("batas pengiriman email ke alamat ini tercapai, coba lagi sejam ya", 429)
    tok = authmod.issue_token(uid, "verify", authmod.VERIFY_TTL)
    ok, merr = mailmod.send_verify_email(mail, user.get("nama") or "kamu", tok)
    if not ok:
        log.warning("email verifikasi ke alamat baru gagal untuk %s: %s", uid, merr)
        return _err("email berhasil diganti, tapi link verifikasi gagal terkirim — "
                    "coba kirim ulang dari halaman Masuk", 502)
    return jsonify({"ok": True, "email_baru": mail,
                    "msg": "email diganti — link verifikasi baru sudah dikirim 📧"})


@app.post("/api/auth/lupa")
def auth_lupa():
    """Minta link reset password (rate-limited, anti-enumerasi)."""
    ip = _pw_client_ip()
    if not _mail_allowed(ip):
        return _err("kebanyakan permintaan, coba lagi 10 menit ya", 429)
    if not mailmod.mail_configured():
        return _err("email belum dikonfigurasi admin", 503)
    body = request.get_json(silent=True) or {}
    _mail_hit(ip)
    uid, user = authmod.find_password_user_by_email(body.get("email"))
    if uid:
        # S-5: batas per alamat; bila tercapai, tetap balas OK generik
        # (anti-enumerasi) tapi email tidak dikirim.
        if _mail_addr_cek(user.get("email")):
            tok = authmod.issue_token(uid, "reset", authmod.RESET_TTL)
            mailmod.send_reset_email(user.get("email"), user.get("nama") or "kamu", tok)
    # selalu OK biar nggak bisa ditebak email siapa yang terdaftar
    return jsonify({"ok": True, "msg": "kalau email terdaftar, link reset dikirim 📧"})


@app.post("/api/auth/reset")
def auth_reset():
    """Set password baru pakai token reset."""
    body = request.get_json(silent=True) or {}
    # Validasi password DULU sebelum token dihanguskan: bila password lemah,
    # user bisa coba lagi dengan link yang sama (bukan minta link baru).
    pw = body.get("password") or ""
    perr = authmod.valid_password(pw)
    if perr:
        return _err(perr, 400)
    uid = authmod.consume_token(body.get("token"), "reset")
    if not uid:
        return _err("link reset nggak valid / sudah kedaluwarsa 😅", 400)
    err = authmod.update_password(uid, pw)
    if err:
        return _err(err, 400)
    return jsonify({"ok": True, "msg": "password berhasil diganti! Silakan masuk 🎉"})


# rate limit endpoint email: {ip: [timestamp]} — maks 5 / 10 menit
_MAIL_ATTEMPTS = {}


def _mail_allowed(ip):
    now = time.time()
    lst = [t for t in _MAIL_ATTEMPTS.get(ip, []) if now - t < 600]
    _MAIL_ATTEMPTS[ip] = lst
    return len(lst) < 5


def _mail_hit(ip):
    _MAIL_ATTEMPTS.setdefault(ip, []).append(time.time())
    # I2 (audit 2026-10-13): sapu key kedaluwarsa bila tabel membesar
    if len(_MAIL_ATTEMPTS) > 5000:
        now = time.time()
        for k in [k for k, v in _MAIL_ATTEMPTS.items()
                  if not v or now - v[-1] >= 600]:
            del _MAIL_ATTEMPTS[k]


# S-5 (audit 2026-10-17): batas per ALAMAT TUJUAN — maks 3 email / jam /
# alamat, di samping batas per IP. Menutup bom email reset ke satu korban
# (rate limit per IP saja bisa diputar via T-1).
_MAIL_BY_ADDR = {}
_MAIL_ADDR_LIMIT, _MAIL_ADDR_WINDOW = 3, 3600


def _mail_addr_allowed(addr):
    now = time.time()
    lst = [t for t in _MAIL_BY_ADDR.get((addr or "").lower(), [])
           if now - t < _MAIL_ADDR_WINDOW]
    _MAIL_BY_ADDR[(addr or "").lower()] = lst
    return len(lst) < _MAIL_ADDR_LIMIT


def _mail_addr_hit(addr):
    _MAIL_BY_ADDR.setdefault((addr or "").lower(), []).append(time.time())
    if len(_MAIL_BY_ADDR) > 5000:
        now = time.time()
        for k in [k for k, v in _MAIL_BY_ADDR.items()
                  if not v or now - v[-1] >= _MAIL_ADDR_WINDOW]:
            del _MAIL_BY_ADDR[k]


def _mail_addr_cek(addr):
    """True bila boleh kirim ke addr (sekaligus catat hit bila boleh)."""
    if not _mail_addr_allowed(addr):
        log.warning("batas email per alamat tercapai: %s",
                    (addr or "")[:40])
        return False
    _mail_addr_hit(addr)
    return True


@app.post("/api/auth/login")
def auth_login():
    """Masuk pakai username/email + password (rate-limited)."""
    ip = _pw_client_ip()
    body = request.get_json(silent=True) or {}
    if not authmod.login_allowed(ip) or \
            not authmod.login_user_allowed(body.get("username")):
        return _err("kebanyakan percobaan, coba lagi 10 menit ya", 429)
    uid = authmod.login_password(body.get("username"), body.get("password"))
    if not uid:
        # catat HANYA saat gagal: login sukses beruntun tidak boleh kena 429
        authmod.login_hit(ip)
        authmod.login_user_hit(body.get("username"))
        return _err("username/email atau password salah", 401)
    authmod.login_user_clear(body.get("username"))
    user = authmod.get_user(uid) or {}
    if (user.get("provider") == "password" and user.get("email")
            and not user.get("email_verified")):
        return _err("verifikasi email kamu dulu ya 📧 (cek inbox/spam)", 403)
    _do_login(uid, "web_" + g.sid if not g.user else g.uid)
    g.user = authmod.get_user(uid)
    g.uid = uid
    resp = jsonify({"ok": True, **_auth_status_payload()})
    _set_login_cookie(resp, uid)
    return resp


def _need_login():
    """Return response error bila belum login, else None."""
    if not g.user:
        return _err("login dulu ya 🔐", 401)
    return None


@app.post("/api/auth/ubah-password")
@with_rl  # R-2 (audit 2026-10-17)
def auth_ubah_password():
    """Ganti password (wajib tahu password lama)."""
    err = _need_login()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    msg = authmod.change_password(g.uid, body.get("password_lama") or "",
                                  body.get("password_baru") or "")
    if msg:
        return _err(msg, 400)
    return jsonify({"ok": True, "msg": "password berhasil diganti 🎉"})


@app.post("/api/auth/ubah-username")
@with_rl  # R-2 (audit 2026-10-17)
def auth_ubah_username():
    """Ganti username."""
    err = _need_login()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    msg = authmod.change_username(g.uid, body.get("username_baru") or "")
    if msg:
        return _err(msg, 400)
    g.user = authmod.get_user(g.uid)
    return jsonify({"ok": True, **_auth_status_payload(),
                    "msg": "username berhasil diganti 🎉"})


@app.post("/api/auth/ubah-email")
@with_rl  # R-2 (audit 2026-10-17)
def auth_ubah_email():
    """Minta ganti email: kirim link verifikasi ke alamat BARU (rate-limited).

    Email lama tetap aktif sampai link diklik. Untuk akun username+password,
    wajib konfirmasi password (mencegah takeover bila sesi dibajak)."""
    err = _need_login()
    if err:
        return err
    ip = _pw_client_ip()
    if not _mail_allowed(ip):
        return _err("kebanyakan permintaan, coba lagi 10 menit ya", 429)
    if not mailmod.mail_configured():
        return _err("email belum dikonfigurasi admin", 503)
    body = request.get_json(silent=True) or {}
    if g.user.get("provider") == "password":
        if not authmod.verify_password(body.get("password") or "",
                                       g.user.get("pw_hash") or ""):
            return _err("password salah 😅", 401)
    _mail_hit(ip)
    tok, mail, msg = authmod.request_email_change(g.uid, body.get("email_baru") or "")
    if msg:
        return _err(msg, 400)
    user = authmod.get_user(g.uid) or {}
    if not _mail_addr_cek(mail):
        return _err("batas pengiriman email ke alamat ini tercapai, coba lagi sejam ya", 429)
    ok, merr = mailmod.send_change_email(mail, user.get("nama") or "kamu", tok)
    if not ok:
        log.warning("email konfirmasi ubah-email gagal untuk %s: %s", g.uid, merr)
        return _err("link gagal terkirim — coba lagi nanti", 502)
    return jsonify({"ok": True,
                    "msg": "link verifikasi dikirim ke email barumu 📧 (berlaku 24 jam)"})


@app.post("/api/auth/ubah-avatar")
@with_rl  # R-2 (audit 2026-10-17)
def auth_ubah_avatar():
    """Pilih avatar maskot profil."""
    err = _need_login()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    msg = authmod.set_avatar(g.uid, body.get("avatar") or "")
    if msg:
        return _err(msg, 400)
    g.user = authmod.get_user(g.uid)
    return jsonify({"ok": True, **_auth_status_payload(),
                    "msg": "foto profil diganti 🎉"})


@app.get("/api/auth/konfirmasi-email")
@with_rl  # R-2 (audit 2026-10-17)
def auth_konfirmasi_email():
    """Konfirmasi ganti email dari link (token 'ubah_email')."""
    token = (request.args.get("token") or "").strip()
    rec = authmod.consume_token_data(token, "ubah_email")
    if not rec:
        return _err("link konfirmasi nggak valid / sudah kedaluwarsa 😅", 400)
    msg = authmod.apply_email_change(rec.get("uid"), rec.get("email_baru") or "")
    if msg:
        return _err(msg, 400)
    return jsonify({"ok": True, "msg": "email berhasil diganti! 🎉"})


# ------------------------------------------------------- convert file
# Halaman "Convert File" (keputusan Sul 2026-10-19): hybrid.
# - API iLovePDF khusus 2 tool berat: Word ke PDF dan PDF ke Word.
# - Sisanya dikerjakan lokal: Gabung PDF, Pisah PDF, Kompres PDF,
#   Gambar ke PDF, PDF ke Gambar (pypdf / PyMuPDF / Pillow).
# Semua convert masuk SATU antrean async: maksimal 2 job jalan
# bersamaan, sisanya menunggu giliran. Hasil disimpan di memori
# ±30 menit lalu dibersihkan otomatis. File tidak pernah disimpan
# permanen di mana pun.

_CONVERT_JALAN_BARENG = 2
_CONVERT_HASIL_TTL = 30 * 60          # detik
_CONVERT_MAKS_FILE = 10 * 1024 * 1024  # 10 MB per file
_CONVERT_MAKS_BODY = 60 * 1024 * 1024  # 60 MB total body request
_CONVERT_GAMBAR_MAKS_FILE = 10
_CONVERT_GAMBAR_MAKS_TOTAL = 15 * 1024 * 1024  # 15 MB total gambar
_CONVERT_AKTIF_PER_USER = 3           # job antre/memproses per pengguna
_CONVERT_ESTIMASI_PER_JOB = 45        # detik, perkiraan kasar per antrean
_CONVERT_HASIL_TERSIMPAN = 30          # job selesai yang hasilnya ditahan

_CONVERT_ALAT = {
    "word_pdf":   {"nama": "Word ke PDF", "provider": "api",
                   "ext": (".doc", ".docx"), "multi": False},
    "pdf_word":   {"nama": "PDF ke Word", "provider": "api",
                   "ext": (".pdf",), "multi": False},
    "gabung":     {"nama": "Gabung PDF", "provider": "lokal",
                   "ext": (".pdf",), "multi": True, "min": 2, "maks": 5},
    "pisah":      {"nama": "Pisah PDF", "provider": "lokal",
                   "ext": (".pdf",), "multi": False, "rentang": True},
    "kompres":    {"nama": "Kompres PDF", "provider": "lokal",
                   "ext": (".pdf",), "multi": False},
    "gambar_pdf": {"nama": "Gambar ke PDF", "provider": "lokal",
                   "ext": (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"),
                   "multi": True, "min": 1, "maks": 10},
    "pdf_gambar": {"nama": "PDF ke Gambar", "provider": "lokal",
                   "ext": (".pdf",), "multi": False},
}

_CONVERT_LOCK = threading.Lock()
_CONVERT_JOBS = {}
_CONVERT_POOL = ThreadPoolExecutor(
    max_workers=_CONVERT_JALAN_BARENG,
    thread_name_prefix="convert")


@contextmanager
def _batas_request(byte):
    """Naikkan sementara batas body request (default 2 MB) untuk satu
    endpoint, mis. convert yang menerima file sampai 10 MB."""
    lama = app.config["MAX_CONTENT_LENGTH"]
    app.config["MAX_CONTENT_LENGTH"] = byte
    try:
        yield
    finally:
        app.config["MAX_CONTENT_LENGTH"] = lama


@app.errorhandler(413)
def _badan_terlalu_besar(e):
    return jsonify({
        "error": "file_terlalu_besar",
        "pesan": ("total file yang dikirim terlalu besar. "
                  "Maksimal 10 MB per file ya"),
    }), 413


def _convert_bersih():
    """Buang job selesai/gagal/dibatalkan yang kedaluwarsa, dan batasi
    jumlah hasil yang ditahan di memori. WAJIB dipanggil dengan
    _CONVERT_LOCK sudah dipegang."""
    kini = time.time()
    for jid in [k for k, j in _CONVERT_JOBS.items()
                if j["status"] in ("selesai", "gagal", "dibatalkan")
                and kini - (j["selesai_ts"] or kini) > _CONVERT_HASIL_TTL]:
        _CONVERT_JOBS.pop(jid, None)
    # Rem memori: tahan hasil maksimal N job selesai terbaru.
    selesai = sorted(
        (j for j in _CONVERT_JOBS.values()
         if j["status"] == "selesai" and j["hasil"] is not None),
        key=lambda j: j["selesai_ts"] or 0)
    for job in selesai[:-_CONVERT_HASIL_TERSIMPAN]:
        job["hasil"] = None
        job["hasil_hilang"] = True


def _convert_validasi(alat, info, files, rentang):
    """Validasi input SEBELUM kuota dicatat (request invalid tidak
    membakar jatah tamu). Kembalikan (daftar_bytes, pesan_error)."""
    if info.get("multi"):
        bmin = info.get("min", 1)
        bmaks = info.get("maks", 10)
        if not bmin <= len(files) <= bmaks:
            return None, (f"alat {info['nama']} butuh {bmin} sampai "
                          f"{bmaks} file ya")
    elif len(files) != 1:
        return None, f"alat {info['nama']} butuh tepat 1 file ya"
    if info.get("rentang") and not (rentang or "").strip():
        return None, ("tulis dulu rentang halamannya, contoh: 1-3,5 "
                      "(halaman 1 sampai 3, lalu halaman 5)")
    daftar = []
    total = 0
    for fs in files:
        nama = (fs.filename or "").lower()
        if not nama.endswith(info["ext"]):
            return None, (f"format file untuk {info['nama']} belum tepat. "
                          f"Yang diterima: {', '.join(info['ext'])}")
        raw = fs.read()
        if not raw:
            return None, "ada file yang kosong, pilih ulang filenya ya"
        if len(raw) > _CONVERT_MAKS_FILE:
            return None, ("ukuran file maksimal 10 MB ya. "
                          "Kecilkan dulu filenya")
        total += len(raw)
        daftar.append((fs.filename, raw))
    if alat == "gambar_pdf" and total > _CONVERT_GAMBAR_MAKS_TOTAL:
        return None, ("total gambar maksimal 15 MB ya. "
                      "Kurangi jumlah gambarnya")
    return daftar, None


def _convert_lokal(job, tahap):
    """Jalankan convert lokal sesuai alat. Kembalikan
    (hasil_bytes, nama_hasil, mime)."""
    alat = job["alat"]
    masuk = job["masukan"]
    if alat == "gabung":
        tahap("Menggabungkan halaman PDF")
        keluar = convert_mod.gabung_pdf([b for _, b in masuk])
        return keluar, convert_mod.nama_hasil(masuk[0][0], "pdf"), \
            "application/pdf"
    if alat == "pisah":
        tahap("Memisahkan halaman PDF")
        keluar = convert_mod.pisah_pdf(masuk[0][1],
                                       job["opsi"].get("rentang", ""))
        return keluar, convert_mod.nama_hasil(masuk[0][0], "pdf"), \
            "application/pdf"
    if alat == "kompres":
        tahap("Mengompres PDF")
        keluar = convert_mod.kompres_pdf(masuk[0][1])
        return keluar, convert_mod.nama_hasil(masuk[0][0], "pdf"), \
            "application/pdf"
    if alat == "gambar_pdf":
        tahap("Menyusun gambar jadi PDF")
        keluar = convert_mod.gambar_ke_pdf([b for _, b in masuk])
        return keluar, convert_mod.nama_hasil(masuk[0][0], "pdf"), \
            "application/pdf"
    if alat == "pdf_gambar":
        tahap("Merender halaman jadi gambar")
        keluar, fmt = convert_mod.pdf_ke_gambar(masuk[0][1])
        if fmt == "zip":
            return keluar, convert_mod.nama_hasil(masuk[0][0], "zip"), \
                "application/zip"
        return keluar, convert_mod.nama_hasil(masuk[0][0], "jpg"), \
            "image/jpeg"
    raise convert_mod.ConvertError("alat convert tidak dikenal")


def _convert_kerja(jid):
    """Pekerja antrean: dijalankan di thread pool (maks 2 bersamaan)."""
    with _CONVERT_LOCK:
        job = _CONVERT_JOBS.get(jid)
        if not job or job["status"] != "antre":
            return
        if job["stop"].is_set():
            job["status"] = "dibatalkan"
            job["selesai_ts"] = time.time()
            return
        job["status"] = "memproses"
        job["mulai"] = time.time()

    def tahap(teks):
        try:
            with _CONVERT_LOCK:
                if job["status"] == "memproses":
                    job["tahap"] = teks
        except Exception:
            pass

    try:
        info = _CONVERT_ALAT[job["alat"]]
        if info["provider"] == "api":
            try:
                hasil, nama_hasil, mime = ilovepdf.convert(
                    job["masukan"][0][1], job["masukan"][0][0],
                    job["alat"], tahap=tahap)
            except ilovepdf.ToolTidakDidukung:
                # iLoveAPI resmi tidak menyediakan PDF ke Word; alihkan
                # job yang sama ke mode lokal teks rapi (transparan via
                # tahap) supaya fiturnya tetap benar benar bisa dipakai.
                if job["alat"] != "pdf_word":
                    raise
                tahap("Dikerjakan mode lokal teks rapi")
                keluar = convert_mod.pdf_ke_word(job["masukan"][0][1])
                hasil = keluar
                nama_hasil = convert_mod.nama_hasil(
                    job["masukan"][0][0], "docx")
                mime = ("application/vnd.openxmlformats-officedocument"
                        ".wordprocessingml.document")
        else:
            hasil, nama_hasil, mime = _convert_lokal(job, tahap)
        with _CONVERT_LOCK:
            job["status"] = "selesai"
            job["tahap"] = "Selesai"
            job["hasil"] = hasil
            job["hasil_nama"] = nama_hasil
            job["hasil_mime"] = mime
            job["hasil_ukuran"] = len(hasil)
            job["selesai_ts"] = time.time()
    except (convert_mod.ConvertError, ilovepdf.ILoveError) as e:
        with _CONVERT_LOCK:
            job["status"] = "gagal"
            job["tahap"] = "Gagal"
            job["error"] = str(e)
            job["selesai_ts"] = time.time()
    except Exception as e:
        log.warning("convert gagal tak terduga %s: %s", jid, e)
        with _CONVERT_LOCK:
            job["status"] = "gagal"
            job["tahap"] = "Gagal"
            job["error"] = "terjadi kendala teknis, coba lagi nanti ya"
            job["selesai_ts"] = time.time()
    finally:
        with _CONVERT_LOCK:
            _convert_bersih()


def _convert_status_json(jid):
    """Bentuk publik status job: transparan soal antrean, tahap, dan
    waktu. Tanpa persen persenan palsu."""
    with _CONVERT_LOCK:
        job = _CONVERT_JOBS.get(jid)
        if not job:
            return None
        antrean = sorted(
            (j for j in _CONVERT_JOBS.values()
             if j["status"] in ("antre", "memproses")),
            key=lambda j: j["dibuat"])
        posisi = next((i + 1 for i, j in enumerate(antrean)
                       if j["id"] == jid), 0)
        depan = max(0, posisi - 1)
        return {
            "id": job["id"],
            "alat": job["alat"],
            "nama_alat": _CONVERT_ALAT[job["alat"]]["nama"],
            "nama_file": job["nama_asli"],
            "status": job["status"],
            "tahap": job["tahap"],
            "posisi": posisi,
            "di_depan": depan,
            "detik_berjalan": int(time.time() - job["dibuat"]),
            "estimasi_mulai_detik": (depan * _CONVERT_ESTIMASI_PER_JOB
                                     if job["status"] == "antre" else 0),
            "bisa_batal": job["status"] == "antre",
            "error": job["error"],
            "hasil_nama": job["hasil_nama"],
            "hasil_ukuran": job["hasil_ukuran"],
            "hasil_hilang": bool(job.get("hasil_hilang")),
        }


def _convert_milik(jid):
    """Ambil job milik request ini, atau None bila tidak ada / bukan milik."""
    with _CONVERT_LOCK:
        job = _CONVERT_JOBS.get((jid or "").strip())
        if not job or job["uid"] != g.uid:
            return None
        return job


@app.post("/api/convert/buat")
@with_rl
def convert_buat():
    """Buat job convert baru (masuk antrean). Validasi dulu, baru catat
    kuota tamu, lalu serahkan ke worker. File dibaca di memori."""
    with _batas_request(_CONVERT_MAKS_BODY):
        alat = (request.form.get("alat") or "").strip()
        info = _CONVERT_ALAT.get(alat)
        if not info:
            return _err("pilih dulu alat convertnya ya")
        if info["provider"] == "api" and not ilovepdf.aktif():
            return jsonify({
                "error": "provider_belum_aktif",
                "pesan": (f"{info['nama']} lagi disiapkan, kunci API nya "
                          "belum dipasang. Coba lagi nanti ya"),
            }), 409
        files = [f for f in request.files.getlist("file")
                 if f and f.filename]
        if not files:
            return _err("pilih filenya dulu ya")
        daftar, emsg = _convert_validasi(
            alat, info, files, request.form.get("rentang") or "")
        if emsg:
            return _err(emsg, 422)
        _qt = kuota_tamu("convert")
        if _qt is not None:
            return _qt
        track_penggunaan("convert")
        jid = "cv_" + uuid.uuid4().hex[:16]
        with _CONVERT_LOCK:
            _convert_bersih()
            aktif_user = sum(1 for j in _CONVERT_JOBS.values()
                             if j["uid"] == g.uid
                             and j["status"] in ("antre", "memproses"))
            if aktif_user >= _CONVERT_AKTIF_PER_USER:
                return _err("tunggu convert yang lagi jalan selesai dulu ya, "
                            "baru mulai yang baru", 429)
            _CONVERT_JOBS[jid] = {
                "id": jid, "uid": g.uid, "alat": alat,
                "nama_asli": daftar[0][0],
                "status": "antre", "tahap": "Menunggu giliran",
                "dibuat": time.time(), "mulai": None, "selesai_ts": None,
                "masukan": daftar,
                "opsi": {"rentang": (request.form.get("rentang") or "")
                         .strip()},
                "stop": threading.Event(),
                "hasil": None, "hasil_nama": None, "hasil_mime": None,
                "hasil_ukuran": 0, "hasil_hilang": False, "error": None,
            }
        _CONVERT_POOL.submit(_convert_kerja, jid)
        return jsonify(_convert_status_json(jid))


@app.get("/api/convert/status")
@with_rl
def convert_status():
    """Status job untuk polling frontend (±3 detik). Transparan:
    posisi antrean, tahap nyata, waktu berjalan, estimasi kasar."""
    data = _convert_status_json((request.args.get("id") or "").strip())
    if not data:
        return _err("job convert tidak ditemukan", 404)
    job = _convert_milik((request.args.get("id") or "").strip())
    if not job:
        return _err("job convert tidak ditemukan", 404)
    return jsonify(data)


@app.get("/api/convert/unduh")
@with_rl
def convert_unduh():
    """Unduh hasil convert milik sendiri."""
    job = _convert_milik(request.args.get("id"))
    if not job:
        return _err("job convert tidak ditemukan", 404)
    with _CONVERT_LOCK:
        if job["status"] != "selesai":
            return _err("hasilnya belum siap", 409)
        if job.get("hasil_hilang") or not job["hasil"]:
            return _err("hasilnya sudah dibersihkan otomatis, "
                        "convert ulang ya", 410)
        data, nama, mime = job["hasil"], job["hasil_nama"], job["hasil_mime"]
    return send_file(BytesIO(data), mimetype=mime, as_attachment=True,
                     download_name=nama)


@app.post("/api/convert/batal")
@with_rl
def convert_batal():
    """Batalkan job yang masih mengantre (belum mulai diproses)."""
    body = request.get_json(silent=True) or {}
    job = _convert_milik(body.get("id"))
    if not job:
        return _err("job convert tidak ditemukan", 404)
    with _CONVERT_LOCK:
        if job["status"] != "antre":
            return _err("convertnya sudah mulai diproses, "
                        "tidak bisa dibatalkan", 409)
        job["stop"].set()
        job["status"] = "dibatalkan"
        job["tahap"] = "Dibatalkan"
        job["selesai_ts"] = time.time()
    return jsonify({"ok": True})


@app.get("/api/convert/siap")
@with_rl
def convert_siap():
    """Status kesiapan provider untuk UI (tanpa membocorkan apa pun)."""
    return jsonify({
        "ilovepdf_aktif": ilovepdf.aktif(),
        "alat": {k: {"nama": v["nama"], "provider": v["provider"]}
                 for k, v in _CONVERT_ALAT.items()},
    })


@app.errorhandler(500)
def _internal(e):
    log.exception("internal error")
    return jsonify({"error": "kesalahan server"}), 500


@app.errorhandler(dbstore.DBUnavailable)
def _db_tidak_tersedia(e):
    # S-2 (audit 2026-10-17): tulisan ke Postgres gagal -> 503 eksplisit,
    # bukan 500 dan bukan penulisan diam-diam ke file ephemeral.
    log.error("DBUnavailable di request %s: %s", request.path, e)
    return jsonify({"ok": False, "error": "db_sibuk",
                    "pesan": "Basis data sedang sibuk, coba lagi sebentar ya."}), 503


# RENDER-FREE: layani file statis dari Flask (di produksi VPS ini ditangani
# Caddy; di Render tidak ada reverse proxy, jadi Flask yang melayani SPA).
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@app.get("/")
def _spa_root():
    res = send_file(os.path.join(_STATIC_DIR, "index.html"))
    res.headers["Cache-Control"] = "no-cache"
    return res


# PWA (2026-10-18): manifest + service worker wajib MIME benar, dan
# sw.js tidak boleh di-cache lama supaya pembaruannya cepat menyebar.
@app.get("/manifest.webmanifest")
def _pwa_manifest():
    return send_file(os.path.join(_STATIC_DIR, "manifest.webmanifest"),
                     mimetype="application/manifest+json")


# SEO (2026-10-18): robots.txt + sitemap.xml wajib MIME benar supaya
# Google membacanya sebagai file penunjuk, bukan halaman HTML.
# Halaman panduan statis (SEO, 2026-10-18): konten asli yang bisa dibaca
# Google tanpa JavaScript, jembatan dari pencarian ke aplikasi.
_PANDUAN_PAGES = {
    "panduan": "panduan-index.html",
    "panduan/cara-membuat-sitasi-apa-7": "panduan-sitasi-apa-7.html",
    "panduan/cara-memilih-uji-statistik": "panduan-uji-statistik.html",
    "panduan/cara-mencari-research-gap": "panduan-research-gap.html",
    "panduan/cara-parafrase-yang-benar": "panduan-parafrase.html",
    "panduan/contoh-latar-belakang-skripsi": "panduan-latar-belakang.html",
    "panduan/beda-sempro-dan-sidang": "panduan-sempro-sidang.html",
    "panduan/cara-membuat-daftar-pustaka-otomatis": "panduan-daftar-pustaka.html",
    "panduan/cek-plagiarisme": "panduan-cek-plagiarisme.html",
}


@app.get("/panduan")
@app.get("/panduan/<slug>")
def _panduan(slug=None):
    key = "panduan" if not slug else "panduan/" + slug
    fname = _PANDUAN_PAGES.get(key)
    if not fname:
        return send_file(os.path.join(_STATIC_DIR, "index.html"))
    res = send_file(os.path.join(_STATIC_DIR, fname))
    res.headers["Cache-Control"] = "public, max-age=86400"
    return res


@app.get("/robots.txt")
def _seo_robots():
    return send_file(os.path.join(_STATIC_DIR, "robots.txt"),
                     mimetype="text/plain")


@app.get("/sitemap.xml")
def _seo_sitemap():
    return send_file(os.path.join(_STATIC_DIR, "sitemap.xml"),
                     mimetype="application/xml")


@app.get("/sw.js")
def _pwa_sw():
    res = send_file(os.path.join(_STATIC_DIR, "sw.js"),
                    mimetype="application/javascript")
    res.headers["Cache-Control"] = "no-cache"
    return res


# R-3 (audit 2026-10-17): pola path yang jelas-jelas bukan rute SPA
# (dotfile, .git, arsip data, suffix backup) menjawab 404 polos, bukan
# 200 berisi index.html — supaya pemindai otomatis tidak salah sangka
# ada sesuatu, dan tidak ada informasi yang bocor via fallback.
# Segmen ".." (path traversal) SENGAJA tidak dicakup: ia sudah
# dinetralkan pemeriksaan realpath di bawah dan tetap jatuh ke index.html
# (perilaku lama, diuji test_fungsional_pasca_telegram).
_SENSITIVE_PATH_RE = re.compile(
    r"(^|/)\.(?!\.)"                 # segmen diawali SATU titik: .env, .git/...
    r"|(^|/)data/"                   # arsip data internal
    r"|\.(zip|tar\.gz|tgz|bak|old|sql|env|pem|key|sqlite3?)$",
    re.IGNORECASE)


def _kirim_statis(cand):
    """Kirim file statis dengan header cache hemat bandwidth.

    Alasan dibuat (2026-10-26): rute statis bawaan Flask mengirim
    no-cache sehingga Cloudflare selalu BYPASS dan tiap pengunjung
    mengunduh ulang dari Render. File berversi (?v=) aman di-cache
    setahun + immutable; gambar/ikon/video tanpa versi 7 hari;
    sisanya 1 hari.
    """
    res = send_file(cand)
    ext = os.path.splitext(cand)[1].lower()
    if request.args.get("v"):
        res.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif ext in (".js", ".css", ".png", ".jpg", ".jpeg", ".webp",
                 ".svg", ".ico", ".mp4", ".webmanifest"):
        res.headers["Cache-Control"] = "public, max-age=604800"
    else:
        res.headers["Cache-Control"] = "public, max-age=86400"
    return res


@app.get("/static/<path:fname>")
def _static_file(fname):
    if _SENSITIVE_PATH_RE.search(fname):
        return "tidak ditemukan", 404
    base = os.path.realpath(_STATIC_DIR)
    cand = os.path.realpath(os.path.join(_STATIC_DIR, fname))
    if not cand.startswith(base + os.sep):
        return "tidak ditemukan", 404
    if os.path.isfile(cand):
        return _kirim_statis(cand)
    return "tidak ditemukan", 404


@app.route("/<path:path>", methods=["GET", "HEAD", "POST", "PUT", "DELETE",
                                    "PATCH", "OPTIONS"])
def _spa_static(path):
    # route API yang tidak ada (mis. endpoint Telegram yang sudah dihapus)
    # harus menjawab 404 JSON untuk method apa pun, bukan 405 HTML bawaan.
    if path.startswith("api/"):
        return jsonify({"error": "tidak ditemukan"}), 404
    if _SENSITIVE_PATH_RE.search(path):
        return "tidak ditemukan", 404
    # anti path traversal: hanya file DI DALAM static/ yang boleh diserve.
    # "../app.py", "%2e%2e/" dsb -> jatuh ke index.html, bukan ke source code.
    base = os.path.realpath(_STATIC_DIR)
    cand = os.path.realpath(os.path.join(_STATIC_DIR, path))
    if not cand.startswith(base + os.sep):
        return send_file(os.path.join(_STATIC_DIR, "index.html"))
    if os.path.isfile(cand):
        return _kirim_statis(cand)
    res = send_file(os.path.join(_STATIC_DIR, "index.html"))
    res.headers["Cache-Control"] = "no-cache"
    return res


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8100, threaded=True)
