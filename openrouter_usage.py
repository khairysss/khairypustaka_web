"""Pemakaian OpenRouter untuk tab Sistem panel admin (2026-11-08).

Sumber data: OpenRouter API resmi (dokumentasi di openrouter.ai,
diperiksa 2026-11-08):

  GET https://openrouter.ai/api/v1/key
    Autentikasi Bearer dengan kunci API biasa (env OPENROUTER_API_KEY,
    kunci yang sama dengan yang dipakai rotasi AI situs; tidak dibuat
    kunci baru). Respons {"data": {...}} memuat pemakaian kunci dalam
    USD: usage (total), usage_daily, usage_weekly, usage_monthly,
    batas belanja kunci limit dan limit_remaining (null berarti kunci
    tanpa batas belanja, bukan nol), limit_reset, is_free_tier, dan
    kuota request model gratis harian free_model_daily_requests
    {used, limit, remaining} yang dihitung per hari UTC dan reset
    tengah malam UTC.

  GET https://openrouter.ai/api/v1/credits
    Tambahan best effort: {"data": {"total_credits": ..., 
    "total_usage": ...}} untuk saldo akun, sisa adalah selisihnya.
    Menurut dokumentasi OpenRouter endpoint ini meminta management
    key, jadi kunci biasa dapat ditolak HTTP 401 atau 403. Penolakan
    itu dinyatakan apa adanya dan tidak menggagalkan kartu.

Hasil berhasil di-cache di memori proses 10 menit, meniru pola
render_metrics, supaya panel tidak menghantam API tiap dibuka.
Nilai kunci tidak pernah ditulis ke log, respons, atau laporan.
Semua kegagalan jatuh ke status jujur berisi kategori kegagalan,
tanpa angka karangan. Modul ini tidak pernah melempar pengecualian
ke pemanggil.
"""
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("kpweb.openrouter_usage")

_API = "https://openrouter.ai/api/v1"
_TTL = 600  # detik
_TIMEOUT = 8  # detik
_cache = {"ts": 0.0, "data": None}
_lock = threading.Lock()


def _angka(v):
    """Float dari nilai JSON; None bila bukan angka yang valid."""
    if isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ambil(path, key):
    req = urllib.request.Request(_API + path, headers={
        "Authorization": "Bearer " + key,
        "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def urai_kunci(data):
    """Urai objek data dari GET /key menjadi blok kunci kartu.

    Return None bila data tidak berbentuk dict. Angka pemakaian yang
    hilang dianggap 0 sesuai skema resmi (field itu selalu ada).
    limit dan limit_remaining null dipertahankan sebagai None supaya
    antarmuka bisa melabeli kunci sebagai tanpa batas, bukan nol.
    """
    if not isinstance(data, dict):
        return None
    kuota = None
    fmdr = data.get("free_model_daily_requests")
    if isinstance(fmdr, dict):
        terpakai = _angka(fmdr.get("used"))
        batas = _angka(fmdr.get("limit"))
        sisa = _angka(fmdr.get("remaining"))
        if terpakai is not None and batas is not None and sisa is not None:
            kuota = {"terpakai": int(terpakai),
                     "batas": int(batas),
                     "sisa": int(sisa)}
    limit = _angka(data.get("limit"))
    limit_sisa = _angka(data.get("limit_remaining"))
    return {
        "usage_total_usd": _angka(data.get("usage")) or 0.0,
        "usage_harian_usd": _angka(data.get("usage_daily")) or 0.0,
        "usage_mingguan_usd": _angka(data.get("usage_weekly")) or 0.0,
        "usage_bulanan_usd": _angka(data.get("usage_monthly")) or 0.0,
        "limit_usd": limit,
        "limit_sisa_usd": limit_sisa,
        "limit_reset": data.get("limit_reset")
        if isinstance(data.get("limit_reset"), str) else None,
        "tanpa_batas": limit is None,
        "tier_gratis": bool(data.get("is_free_tier")),
        "kuota_harian": kuota,
    }


def urai_kredit(data):
    """Urai objek data dari GET /credits menjadi blok saldo akun.

    Return None bila bentuknya tidak sesuai skema resmi.
    """
    if not isinstance(data, dict):
        return None
    total = _angka(data.get("total_credits"))
    terpakai = _angka(data.get("total_usage"))
    if total is None or terpakai is None:
        return None
    return {"tersedia": True,
            "total_kredit_usd": total,
            "total_terpakai_usd": terpakai,
            "sisa_usd": round(total - terpakai, 6)}


def _saldo_akun(key):
    """Best effort GET /credits. Tidak pernah melempar; kegagalan
    dinyatakan jujur sebagai saldo yang tidak tersedia."""
    try:
        mentah = _ambil("/credits", key)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"tersedia": False,
                    "catatan": "Saldo akun tidak tersedia untuk kunci "
                               "ini karena endpoint kredit OpenRouter "
                               "meminta management key."}
        return {"tersedia": False,
                "catatan": "Saldo akun belum terbaca saat ini: "
                           "HTTP %d." % e.code}
    except Exception as e:
        log.warning("openrouter_usage: ambil credits gagal: %s", e)
        return {"tersedia": False,
                "catatan": "Saldo akun belum terbaca saat ini."}
    blok = urai_kredit(mentah.get("data") if isinstance(mentah, dict)
                       else None)
    if blok is None:
        return {"tersedia": False,
                "catatan": "Saldo akun belum terbaca saat ini."}
    return blok


def _gagal(status, catatan):
    return {"terhubung": False, "status": status, "catatan": catatan}


def data_openrouter():
    """Blok openrouter untuk /api/admin/sistem. Tidak pernah
    melempar."""
    try:
        return _data_openrouter()
    except Exception as e:  # jaring terakhir, endpoint sistem wajib aman
        log.warning("openrouter_usage: galat tak terduga: %s", e)
        return _gagal("galat",
                      "Data pemakaian OpenRouter belum terbaca saat "
                      "ini.")


def _data_openrouter():
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not key:
        return _gagal(
            "belum_ada_kunci",
            "Pemakaian OpenRouter belum terhubung, kunci API "
            "OpenRouter belum terpasang di server saat ini.")
    with _lock:
        if (_cache["data"] is not None
                and time.time() - _cache["ts"] < _TTL):
            return _cache["data"]
    try:
        mentah = _ambil("/key", key)
    except urllib.error.HTTPError as e:
        return _gagal(
            "galat_http",
            "Kunci API OpenRouter terpasang, tetapi data pemakaian "
            "belum terbaca: HTTP %d." % e.code)
    except Exception as e:
        log.warning("openrouter_usage: ambil key gagal: %s", e)
        return _gagal(
            "galat_jaringan",
            "Kunci API OpenRouter terpasang, tetapi server "
            "OpenRouter belum bisa dihubungi saat ini.")
    kunci = urai_kunci(mentah.get("data") if isinstance(mentah, dict)
                       else None)
    if kunci is None:
        return _gagal(
            "data_kosong",
            "Kunci API OpenRouter terpasang, tetapi OpenRouter belum "
            "mengembalikan data pemakaian.")
    hasil = {"terhubung": True, "status": "ok"}
    hasil.update(kunci)
    hasil["saldo_akun"] = _saldo_akun(key)
    hasil["diperbarui"] = int(time.time())
    hasil["catatan"] = (
        "Model OpenRouter yang dipakai situs semuanya gratis, jadi "
        "pemakaian normal tidak mengurangi saldo akun. Saldo akun "
        "sengaja dibiarkan sekitar $0,20.")
    with _lock:
        _cache["data"] = hasil
        _cache["ts"] = time.time()
    return hasil
