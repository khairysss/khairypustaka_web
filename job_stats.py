"""Pencatatan penyelesaian pekerjaan antrean yang persisten.

Latar: tab Sistem panel admin awalnya menghitung pekerjaan selesai
dan gagal dari dict job di memori proses, jadi angkanya hilang tiap
server mulai ulang dan panel hampir selalu menampilkan nol. Modul
ini mencatat tiap penyelesaian pekerjaan (selesai atau gagal) per
jenis antrean, per hari (kalender WITA) dan total sepanjang
pencatatan, plus cap waktu aktivitas terakhir per antrean. Disimpan
lewat dbstore sebagai kp_kv data/job-stats.json (Postgres di
produksi, file lokal di mode file). Angka yang sedang jalan dan
antre tetap kondisi langsung dari memori; yang dicatat permanen di
sini hanya hasil akhirnya. Pola mengikuti cerebras_usage:
fail-safe total, galat hanya dicatat di log dan tidak pernah
mengganggu pekerjaan yang sedang berjalan.
"""
import copy
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import dbstore

log = logging.getLogger("kpweb.job_stats")

_PATH = "data/job-stats.json"
_WITA = timezone(timedelta(hours=8))
_LOCK = threading.Lock()
_SIMAPAN_HARI = 92  # hari kalender terakhir yang disimpan
_ANTREAN_SAH = ("ai", "sitasi", "convert")


def _nol():
    return {"selesai": 0, "gagal": 0}


def _muat():
    d = None
    try:
        d = dbstore.load_json(_PATH, None)
    except Exception as e:
        log.warning("job_stats: muat gagal: %s", e)
    if not isinstance(d, dict):
        d = {}
    if not isinstance(d.get("hari"), dict):
        d["hari"] = {}
    if not isinstance(d.get("total"), dict):
        d["total"] = {}
    if not isinstance(d.get("terakhir"), dict):
        d["terakhir"] = {}
    return d


def catat(antrean, ok):
    """Catat satu penyelesaian pekerjaan. Fail-safe total."""
    try:
        q = str(antrean or "").strip().lower()
        if q not in _ANTREAN_SAH:
            return
        hari_ini = datetime.now(_WITA).strftime("%Y-%m-%d")
        with _LOCK:
            d = _muat()
            for wadah in (d["hari"].setdefault(hari_ini, {}), d["total"]):
                s = wadah.get(q)
                if not isinstance(s, dict):
                    s = _nol()
                    wadah[q] = s
                if ok:
                    s["selesai"] = int(s.get("selesai") or 0) + 1
                else:
                    s["gagal"] = int(s.get("gagal") or 0) + 1
            d["terakhir"][q] = time.time()
            kunci_hari = sorted(d["hari"].keys())
            for lama in kunci_hari[:-_SIMAPAN_HARI]:
                d["hari"].pop(lama, None)
            dbstore.save_json_best_effort(_PATH, d)
    except Exception as e:
        log.warning("job_stats: catat gagal: %s", e)


def ringkasan():
    """Ringkasan tercatat: total, hari ini (WITA), aktivitas terakhir.

    Bentuk: {"total": {antrean: {"selesai": n, "gagal": n}},
             "hari_ini": {antrean: {"selesai": n, "gagal": n}},
             "terakhir": {antrean: ts}}
    """
    d = _muat()
    hari_ini = datetime.now(_WITA).strftime("%Y-%m-%d")
    return {"total": copy.deepcopy(d["total"]),
            "hari_ini": copy.deepcopy(d["hari"].get(hari_ini) or {}),
            "terakhir": copy.deepcopy(d["terakhir"])}
