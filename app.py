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
from datetime import date, datetime
from io import BytesIO

from flask import Flask, jsonify, request, g, send_file, Response, redirect

import auth as authmod
import dbstore
import mailer as mailmod
import skripsi_ai as sai
import collection as koleksi
import deadline as tenggat
import stats as stats_mod
import matrix as matriks_mod
import template_doc
import topics as topik_mod
import tanya as tanya_mod
import history as histmod
from cite import STYLE_ORDER, cite_in, style_label
from citefind import parse_citation, split_citations, title_match
from metafetch import fetch_metadata_from_url
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

try:
    import premium_pay as ppay
except Exception as e:  # pembayaran opsional di web
    ppay = None
    logging.getLogger("kpweb").warning("premium_pay tidak bisa diimport: %s", e)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("kpweb")

app = Flask(__name__)
# Batas ukuran body request (2 MB) — tolak payload raksasa dengan 413
# sebelum Werkzeug menelannya ke RAM (mitigasi OOM/DoS).
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

SID_COOKIE = "kp_sid"
SID_TTL = 2 * 365 * 24 * 3600  # 2 tahun

AI_KINDS = ("kerangka", "abstrak", "kuesioner", "latarbelakang", "simpulan",
            "sidang", "kisi", "metode", "pembahasan", "bab2",
            "ringkas", "interpretasi")  # STRATEGI1: AI tempel

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
            dbstore.save_json(_USAGE_PATH, d)
    except Exception as e:
        log.warning("track_penggunaan gagal: %s", e)


# ------------------------------------------- kuota harian tamu fitur non-AI
_GUEST_USAGE_PATH = "data/guest_usage.json"
_GUEST_USAGE_LOCK = threading.Lock()
KUOTA_TAMU_HARIAN = 10  # per fitur non-AI, khusus tamu; login = unlimited


def _is_tamu():
    """True bila request ini dari tamu (belum login)."""
    try:
        return (getattr(g, "uid", "") or "").startswith("web_")
    except Exception:
        return False


def pesan_ai_habis():
    """Pesan ramah saat jatah AI gratis habis (tamu 5x / login 10x)."""
    if _is_tamu():
        return ("Jatah AI gratis harianmu habis (5x/hari). "
                "Login gratis biar dapat 10x/hari! ✨")
    return ("Jatah AI gratis harianmu habis (10x/hari). "
            "Besok reset lagi.")


def kuota_tamu(tool):
    """Batasi tamu 10x/hari per fitur non-AI.

    Kembalikan None bila lolos (sekaligus catat hit untuk tamu),
    atau tuple (response, 429) bila kuota tamu habis.
    Login = unlimited (langsung None, tanpa pencatatan).
    Fail-safe: gagal baca/tulis -> lolos, tidak pernah 500.
    """
    try:
        name = (tool or "").strip().lower()
        if not name or not _is_tamu():
            return None
        uid = g.uid
        today = _today_wita()
        with _GUEST_USAGE_LOCK:
            d = dbstore.load_json(_GUEST_USAGE_PATH, {})
            if not isinstance(d, dict):
                d = {}
            u = d.get(uid)
            if not isinstance(u, dict):
                u = {}
            t = u.get(name)
            n = int((t or {}).get(today, 0) or 0) if isinstance(t, dict) else 0
            if n >= KUOTA_TAMU_HARIAN:
                return jsonify({"error": "kuota_tamu_habis",
                                "pesan": "Jatah harian tamu untuk fitur ini habis (10x/hari). Login gratis biar pakai sepuasnya!"}), 429
            u[name] = {today: n + 1}
            d[uid] = u
            dbstore.save_json(_GUEST_USAGE_PATH, d)
        return None
    except Exception as e:
        log.warning("kuota_tamu gagal: %s", e)
        return None


# ---------------------------------------------------------------- sesi
_SID_RE = re.compile(r"^[0-9a-f]{32}$")


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


def _ip():
    # Urutan kepercayaan: CF-Connecting-IP dulu — header ini di-set
    # Cloudflare dari IP pengunjung asli dan tidak bisa di-spoof klien
    # selama trafik lewat Cloudflare. (Cara lama mengambil entri TERAKHIR
    # X-Forwarded-For = IP egress Cloudflare yang dipakai BERSAMA banyak
    # pengguna, sehingga rate limit & kuota tamu ketukar antar pengguna.)
    # Fallback XFF entri terakhir dipertahankan untuk jalur non-CF.
    cf = (request.headers.get("CF-Connecting-IP") or "").strip()
    if cf:
        return cf
    fwd = request.headers.get("X-Forwarded-For", "")
    parts = [p.strip() for p in fwd.split(",") if p.strip()]
    return (parts[-1] if parts else "") or request.remote_addr or "unknown"


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
    pool = _ai_consume_uid(g.uid)
    if pool:
        try:
            ide = _run(topik_mod.ide_topik_ai(
                keyword, top, gaps, premium=_ai_premium_uid(g.uid)))
        except Exception as e:
            log.warning("topik ide gagal: %s", e)
            ide = None
        if not ide:
            sai.ai_refund(g.uid, pool)
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


# ---------------------------------------------------------------- deadline
@app.post("/api/deadline")
@with_rl
def deadline():
    body = request.get_json(silent=True) or {}
    aksi = (body.get("aksi") or "list").strip().lower()
    uid = g.uid
    # validasi dulu, baru catat kuota (request invalid tidak membakar jatah tamu)
    if aksi not in ("tambah", "list", "hapus"):
        return _err("aksi harus tambah|list|hapus")
    if aksi == "tambah":
        nama = (body.get("nama") or "").strip()
        tgl = tenggat.parse_tanggal(body.get("tanggal") or "")
        if not nama or not tgl:
            return _err("nama & tanggal (contoh: 20/12/2026) wajib diisi")
    elif aksi == "hapus":
        try:
            idx = int(body.get("idx"))
        except (TypeError, ValueError):
            return _err("idx tidak valid")
    _qt = kuota_tamu("deadline")
    if _qt is not None:
        return _qt
    track_penggunaan("deadline")
    if aksi == "tambah":
        tenggat.add(uid, nama, tgl)
    elif aksi == "hapus":
        if tenggat.remove(uid, idx) is None:
            return _err("idx tidak ada")
    items = tenggat.list_all(uid)
    out = [{"idx": i, "nama": it["nama"], "tanggal": it["tanggal"],
            "tanggal_id": tenggat.fmt_id(it["tanggal"])}
           for i, it in enumerate(items)]
    return jsonify({"deadline": out})


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
def ai():
    body = request.get_json(silent=True) or {}
    kind = (body.get("kind") or "").strip().lower()
    if kind not in AI_KINDS:
        return _err(f"kind harus salah satu: {', '.join(AI_KINDS)}")
    text = (body.get("text") or "").strip()
    if not text:
        return _err("text kosong")
    if len(text) > 8000:
        return _err("teks kepanjangan (maksimal 8000 karakter)", 400)
    extra = (body.get("extra") or "").strip()
    pool = _ai_consume_uid(g.uid)
    if not pool:
        return jsonify({"error": "kuota_habis",
                        "pesan": pesan_ai_habis()}), 429
    try:
        hasil = _run(sai.generate(kind, text, extra, chat_id=g.uid))
    except Exception as e:
        log.warning("ai %s error: %s", kind, e)
        hasil = None
    if not hasil:
        sai.ai_refund(g.uid, pool)
        return _err("AI-nya lagi gangguan, coba lagi bentar ya", 502)
    sai.track_tool("ai", g.uid)
    if g.user:
        histmod.catat(g.uid, "chat", text,
                      {"sumber": "ai", "kind": kind,
                       "text": text[:2000], "hasil": hasil})
    return jsonify({"hasil": hasil, "kind": kind,
                    "sisa": _ai_left_uid(g.uid)})


# ---------------------------------------------------------------- tanya
@app.post("/api/tanya")
@with_rl
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
    pool = _ai_consume_uid(g.uid)
    if not pool:
        return jsonify({"error": "kuota_habis",
                        "pesan": pesan_ai_habis()}), 429
    try:
        jawaban = _run(tanya_mod.gemini_answer(
            q[:500], premium=_ai_premium_uid(g.uid)))
    except Exception as e:
        log.warning("tanya ai error: %s", e)
        jawaban = None
    if not jawaban:
        sai.ai_refund(g.uid, pool)
        tanya_mod.log_unanswered(q[:500])
        return _err("belum bisa menjawab sekarang, coba lagi nanti", 502)
    sai.track_tool("tanya", g.uid)
    if g.user:
        histmod.catat(g.uid, "chat", q,
                      {"sumber": "tanya", "tipe_jawab": "ai",
                       "q": q[:500], "jawaban": jawaban})
    return jsonify({"tipe": "ai", "jawaban": jawaban,
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
def kuota():
    harian, saldo = _ai_info_uid(g.uid)
    return jsonify({"sisa": harian, "saldo": saldo,
                    "premium": _ai_premium_uid(g.uid),
                    "admin": _is_web_admin()})


# ---------------------------------------------------------------- topup jatah AI
@app.get("/api/topup")
def topup_info():
    harian, saldo = _ai_info_uid(g.uid)
    return jsonify({
        "harga_rp": ppay.TOPUP_PRICE if ppay else 5000,
        "uses": ppay.TOPUP_USES if ppay else 50,
        "min_uses": ppay.TOPUP_MIN_USES if ppay else 50,
        "harga_per_use": ppay.TOPUP_PRICE_PER_USE if ppay else 100,
        "pay_enabled": ppay.pay_enabled() if ppay else False,
        "sisa_harian": harian,
        "saldo": saldo,
        "admin": _is_web_admin(),
    })


@app.post("/api/topup/create")
@with_rl
def topup_create():
    """Buat invoice QRIS top-up. Jumlah jatah bebas (minimal 50x),
    Rp100 per 1x. Frontend polling /api/topup/status/<ref_id>.

    Wajib login: saldo yang dibeli harus nempel ke akun, bukan sesi tamu.
    """
    if not ppay or not ppay.pay_enabled():
        return _err("pembayaran belum aktif", 503)
    if not g.user:
        return jsonify({"error": "login dulu biar saldomu aman",
                        "need_login": True}), 401
    if ppay.pending_for_user(g.uid):
        return _err("kamu sudah punya kode bayar aktif", 409)
    body = request.get_json(silent=True) or {}
    try:
        uses = int(body.get("uses") or 0)
    except (TypeError, ValueError):
        uses = 0
    min_uses = ppay.TOPUP_MIN_USES
    max_uses = 10000  # Rp1 juta; cegah nominal raksasa yang lolos gross-up
    if uses < min_uses:
        return _err(f"minimal top-up {min_uses}x jatah AI", 400)
    if uses > max_uses:
        return _err(f"maksimal top-up {max_uses}x jatah AI per transaksi", 400)
    harga = uses * ppay.TOPUP_PRICE_PER_USE
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


@app.post("/api/redeem")
@with_rl
def redeem():
    # Wajib login: saldo redeem harus nempel ke akun (bukan sesi tamu yang
    # hilang saat cookie berganti), sekaligus menutup brute-force kode anonim.
    if not g.user:
        return jsonify({"error": "login dulu biar saldomu aman",
                        "need_login": True}), 401
    body = request.get_json(silent=True) or {}
    kode = (body.get("kode") or "").strip()
    if not kode:
        return _err("kode kosong")
    ok, res = redmod.redeem(kode, g.uid)
    if not ok:
        return jsonify({"ok": False, "alasan": res})
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
    """True bila user cocok dengan salah satu nama di ADMIN_USERNAMES."""
    if not user:
        return False
    allowed = _admin_usernames()
    if not allowed:
        return False
    for cand in (user.get("pid"), user.get("nama"), user.get("email")):
        if cand and str(cand).strip().lower() in allowed:
            return True
    return False


def _need_admin():
    if not g.user or not is_admin_user(g.user):
        return jsonify({"ok": False, "error": "khusus admin"}), 403
    return None


def _is_web_admin():
    """True bila request saat ini datang dari akun admin web."""
    return bool(g.user) and is_admin_user(g.user)


def _ai_consume_uid(uid):
    """Reservasi 1 jatah AI untuk request web.

    Admin web tidak pernah kehabisan: kembalikan "harian" tanpa
    mengurangi apa pun (pool ini dikenali semua call site sebagai sukses).
    Non-admin memakai jalur normal sai.ai_try_consume apa adanya.
    """
    if _is_web_admin():
        return "harian"
    return sai.ai_try_consume(uid)


def _ai_left_uid(uid):
    """Sisa jatah AI harian; admin web selalu 999 (tanpa batas)."""
    if _is_web_admin():
        return 999
    return sai.ai_quota_left(uid)


def _ai_info_uid(uid):
    """(sisa_harian, saldo); admin web selalu (999, saldo)."""
    if _is_web_admin():
        return 999, sai.ai_balance(uid)
    return sai.ai_quota_info(uid)


def _ai_premium_uid(uid):
    """Admin web diperlakukan premium, seperti owner di bot Telegram."""
    return _is_web_admin() or sai.is_premium(uid)


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


@app.get("/api/kode-referral")
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
    """Daftarkan kunjungan via link referral ?ref=KH-xxx (dipanggil JS sekali)."""
    kode = (request.args.get("kode") or "").strip()
    if not kode:
        return jsonify({"ok": False})
    try:
        rid, milestone = refmod.register(g.uid, kode)
    except Exception:
        return jsonify({"ok": False})
    if milestone:
        new_bal = sai.ai_topup(rid, refmod.REWARD_USES)
        return jsonify({"ok": True, "milestone": True,
                        "reward": refmod.REWARD_USES, "saldo": new_bal})
    return jsonify({"ok": True, "milestone": False})


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
    # Sama seperti _ip(): prioritaskan CF-Connecting-IP (IP pengunjung
    # asli dari Cloudflare); fallback XFF entri terakhir untuk non-CF.
    cf = (request.headers.get("CF-Connecting-IP") or "").strip()
    if cf:
        return cf
    fwd = request.headers.get("X-Forwarded-For", "")
    parts = [p.strip() for p in fwd.split(",") if p.strip()]
    return (parts[-1] if parts else "") or request.remote_addr or "?"


@app.post("/api/auth/register")
@with_rl
def auth_register():
    """Daftar akun username+password+email. Akun aktif setelah verifikasi email."""
    if not mailmod.mail_configured():
        return _err("pendaftaran belum aktif (email admin belum dikonfigurasi)", 503)
    body = request.get_json(silent=True) or {}
    uid, err = authmod.register_password(body.get("username"),
                                         body.get("password") or "",
                                         body.get("email") or "")
    if err:
        return _err(err, 400)
    tok = authmod.issue_token(uid, "verify", authmod.VERIFY_TTL)
    user = authmod.get_user(uid)
    ok, merr = mailmod.send_verify_email(user.get("email"), user.get("nama") or "kamu", tok)
    if not ok:
        log.warning("email verifikasi gagal untuk %s: %s", uid, merr)
        return _err("akun dibuat tapi email verifikasi gagal terkirim — coba kirim ulang dari halaman Masuk", 502)
    return jsonify({"ok": True, "need_verify": True,
                    "msg": "cek email kamu buat verifikasi 📧"})


@app.post("/api/auth/verifikasi")
def auth_verifikasi():
    """Verifikasi email dari link. Langsung login setelah berhasil."""
    body = request.get_json(silent=True) or {}
    uid = authmod.consume_token(body.get("token"), "verify")
    if not uid:
        return _err("link verifikasi nggak valid / sudah kedaluwarsa 😅", 400)
    authmod.set_email_verified(uid)
    _do_login(uid, "web_" + g.sid if not g.user else g.uid)
    g.user = authmod.get_user(uid)
    g.uid = uid
    resp = jsonify({"ok": True, **_auth_status_payload()})
    _set_login_cookie(resp, uid)
    return resp


@app.post("/api/auth/kirim-ulang")
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
    if not authmod.login_allowed(ip):
        return _err("kebanyakan percobaan, coba lagi 10 menit ya", 429)
    if not _mail_allowed(ip):
        return _err("kebanyakan permintaan email, coba lagi 10 menit ya", 429)
    if not mailmod.mail_configured():
        return _err("email belum dikonfigurasi admin", 503)
    body = request.get_json(silent=True) or {}
    ident = body.get("username") or body.get("email") or body.get("identifier")
    uid = authmod.login_password(ident, body.get("password") or "")
    if not uid:
        authmod.login_hit(ip)
        return _err("username/email atau password salah", 401)
    mail, err = authmod.replace_unverified_email(uid, body.get("email_baru") or "")
    if err:
        return _err(err, 400)
    _mail_hit(ip)
    user = authmod.get_user(uid) or {}
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


@app.post("/api/auth/login")
def auth_login():
    """Masuk pakai username/email + password (rate-limited)."""
    ip = _pw_client_ip()
    if not authmod.login_allowed(ip):
        return _err("kebanyakan percobaan, coba lagi 10 menit ya", 429)
    body = request.get_json(silent=True) or {}
    uid = authmod.login_password(body.get("username"), body.get("password"))
    if not uid:
        # catat HANYA saat gagal: login sukses beruntun tidak boleh kena 429
        authmod.login_hit(ip)
        return _err("username/email atau password salah", 401)
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
    ok, merr = mailmod.send_change_email(mail, user.get("nama") or "kamu", tok)
    if not ok:
        log.warning("email konfirmasi ubah-email gagal untuk %s: %s", g.uid, merr)
        return _err("link gagal terkirim — coba lagi nanti", 502)
    return jsonify({"ok": True,
                    "msg": "link verifikasi dikirim ke email barumu 📧 (berlaku 24 jam)"})


@app.post("/api/auth/ubah-avatar")
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


@app.errorhandler(500)
def _internal(e):
    log.exception("internal error")
    return jsonify({"error": "kesalahan server"}), 500


# RENDER-FREE: layani file statis dari Flask (di produksi VPS ini ditangani
# Caddy; di Render tidak ada reverse proxy, jadi Flask yang melayani SPA).
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@app.get("/")
def _spa_root():
    return send_file(os.path.join(_STATIC_DIR, "index.html"))


@app.route("/<path:path>", methods=["GET", "HEAD", "POST", "PUT", "DELETE",
                                    "PATCH", "OPTIONS"])
def _spa_static(path):
    # route API yang tidak ada (mis. endpoint Telegram yang sudah dihapus)
    # harus menjawab 404 JSON untuk method apa pun, bukan 405 HTML bawaan.
    if path.startswith("api/"):
        return jsonify({"error": "tidak ditemukan"}), 404
    # anti path traversal: hanya file DI DALAM static/ yang boleh diserve.
    # "../app.py", "%2e%2e/" dsb -> jatuh ke index.html, bukan ke source code.
    base = os.path.realpath(_STATIC_DIR)
    cand = os.path.realpath(os.path.join(_STATIC_DIR, path))
    if not cand.startswith(base + os.sep):
        return send_file(os.path.join(_STATIC_DIR, "index.html"))
    if os.path.isfile(cand):
        return send_file(cand)
    return send_file(os.path.join(_STATIC_DIR, "index.html"))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8100, threaded=True)
