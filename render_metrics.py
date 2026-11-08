"""Metrik pemakaian Render untuk tab Sistem panel admin (2026-11-08).

Sumber data: Render Public API. Skema dibaca dari spek OpenAPI resmi
https://api-docs.render.com/v1.0/openapi/render-public-api-1.json
(diunduh dan diperiksa 2026-11-08):
  GET https://api.render.com/v1/metrics/bandwidth
  GET https://api.render.com/v1/metrics/instance-count
Parameter query keduanya: startTime dan endTime (format date-time),
resource (id service). Autentikasi: header Authorization Bearer berisi
API key. Respons 200: array seri waktu, tiap seri berbentuk
{labels: [{field, value}], values: [{timestamp, value}], unit}.

Kunci dibaca dari env RENDER_API_KEY, id service dari env
RENDER_SERVICE_ID. Hasil berhasil di-cache di memori proses 10 menit
supaya panel tidak menghantam API tiap dibuka. Render tidak menyediakan
API billing, jadi modul ini hanya menyajikan data metrik apa adanya:
total bandwidth dari penjumlahan titik data, dan perkiraan jam instance
dari integrasi jumlah instance terhadap waktu. Semua kegagalan jatuh ke
status jujur berisi kategori kegagalan, tanpa angka karangan. Modul ini
tidak pernah melempar pengecualian ke pemanggil.
"""
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger("kpweb.render_metrics")

_API = "https://api.render.com/v1"
_TTL = 600  # detik
_cache = {"ts": 0.0, "data": None}
_lock = threading.Lock()
_BATAS_DT = 7200.0  # detik; langkah integrasi maksimum per titik data


def _parse_ts(teks):
    """Epoch dari stempel waktu ISO 8601 metrik; None bila tidak valid."""
    if not teks:
        return None
    try:
        s = str(teks).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _faktor_gb(unit):
    u = str(unit or "GB").strip().upper()
    if u in ("B", "BYTE", "BYTES"):
        return 1.0 / (1024 ** 3)
    if u in ("KB", "KIB"):
        return 1.0 / (1024 ** 2)
    if u in ("MB", "MIB"):
        return 1.0 / 1024
    if u in ("TB", "TIB"):
        return 1024.0
    return 1.0  # GB dan satuan tak dikenal dianggap GB sesuai contoh spek


def agregat_bandwidth(daftar_seri):
    """Jumlahkan semua titik data bandwidth menjadi total GB.

    Return (total_gb, periode_mulai_epoch, periode_selesai_epoch);
    periode None bila tidak ada titik data valid.
    """
    total = 0.0
    tmin = None
    tmax = None
    for seri in daftar_seri or []:
        if not isinstance(seri, dict):
            continue
        faktor = _faktor_gb(seri.get("unit"))
        for titik in seri.get("values") or []:
            if not isinstance(titik, dict):
                continue
            try:
                total += float(titik.get("value") or 0) * faktor
            except (TypeError, ValueError):
                continue
            ts = _parse_ts(titik.get("timestamp"))
            if ts is not None:
                tmin = ts if tmin is None else min(tmin, ts)
                tmax = ts if tmax is None else max(tmax, ts)
    return total, tmin, tmax


def integrasi_jam_instance(daftar_seri, akhir_epoch):
    """Perkiraan jam instance: integral langkah maju jumlah instance
    dikali selang waktu antar titik data, dalam jam.

    Selang per titik dibatasi _BATAS_DT detik supaya celah data yang
    panjang tidak menggelembungkan angka. Return (jam, mulai, selesai).
    """
    total = 0.0
    tmin = None
    tmax = None
    for seri in daftar_seri or []:
        if not isinstance(seri, dict):
            continue
        titik = []
        for v in seri.get("values") or []:
            if not isinstance(v, dict):
                continue
            ts = _parse_ts(v.get("timestamp"))
            if ts is None:
                continue
            try:
                nilai = float(v.get("value") or 0)
            except (TypeError, ValueError):
                continue
            titik.append((ts, nilai))
        titik.sort()
        for i, (ts, nilai) in enumerate(titik):
            if i + 1 < len(titik):
                dt = titik[i + 1][0] - ts
            elif i > 0:
                dt = ts - titik[i - 1][0]
            else:
                dt = min(max(akhir_epoch - ts, 0.0), _BATAS_DT)
            dt = min(max(dt, 0.0), _BATAS_DT)
            total += nilai * dt
            tmin = ts if tmin is None else min(tmin, ts)
            tmax = ts if tmax is None else max(tmax, ts)
    return total / 3600.0, tmin, tmax


def _ambil_metrik(path, key, params):
    url = _API + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + key,
        "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _gagal(status, catatan):
    return {"terhubung": False, "status": status, "catatan": catatan}


def data_render():
    """Blok render untuk /api/admin/sistem. Tidak pernah melempar."""
    try:
        return _data_render()
    except Exception as e:  # jaring terakhir, endpoint sistem wajib aman
        log.warning("render_metrics: galat tak terduga: %s", e)
        return _gagal("galat", "Data metrik Render belum terbaca saat ini.")


def _data_render():
    key = (os.environ.get("RENDER_API_KEY") or "").strip()
    if not key:
        return _gagal(
            "belum_ada_kunci",
            "Pemakaian Render belum terhubung, kunci API Render belum "
            "terpasang di server saat ini.")
    with _lock:
        if (_cache["data"] is not None
                and time.time() - _cache["ts"] < _TTL):
            return _cache["data"]
    sid = (os.environ.get("RENDER_SERVICE_ID") or "").strip() \
        or "srv-db9l6njrjlhs73f894f0"
    now = datetime.now(timezone.utc)
    awal = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    params = {"startTime": awal.strftime(fmt),
              "endTime": now.strftime(fmt),
              "resource": sid}
    try:
        bw = _ambil_metrik("/metrics/bandwidth", key, params)
        ic = _ambil_metrik("/metrics/instance-count", key, params)
    except urllib.error.HTTPError as e:
        return _gagal(
            "galat_http",
            "Kunci API Render terpasang, tetapi data metrik belum "
            "terbaca: HTTP %d." % e.code)
    except Exception as e:
        log.warning("render_metrics: ambil metrik gagal: %s", e)
        return _gagal(
            "galat_jaringan",
            "Kunci API Render terpasang, tetapi server Render belum "
            "bisa dihubungi saat ini.")
    total_gb, b1, b2 = agregat_bandwidth(bw)
    jam, i1, i2 = integrasi_jam_instance(ic, now.timestamp())
    ujung = [t for t in (b1, b2, i1, i2) if t is not None]
    if not ujung:
        return _gagal(
            "data_kosong",
            "Kunci API Render terpasang, tetapi metrik bulan ini belum "
            "mengembalikan titik data.")
    hasil = {
        "terhubung": True,
        "status": "ok",
        "bandwidth_gb": round(total_gb, 3),
        "jam_instance_perkiraan": round(jam, 2),
        "periode_mulai": int(min(ujung)),
        "periode_selesai": int(max(ujung)),
        "diperbarui": int(time.time()),
        "catatan": "Data asli dari metrik Render untuk periode yang "
                   "tercantum. Jam instance adalah perkiraan dari "
                   "integrasi metrik jumlah instance.",
    }
    with _lock:
        _cache["data"] = hasil
        _cache["ts"] = time.time()
    return hasil
