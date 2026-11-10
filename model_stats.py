"""Pencatatan status panggilan model AI yang persisten.

Latar: penghitung berhasil/gagal per model untuk tab Sistem panel
admin awalnya hanya disimpan di memori proses (ai_providers
_MODEL_STATS), jadi setiap server mulai ulang angkanya kembali nol
dan panel menampilkan "belum ada data" walau modelnya dipakai tiap
hari. Modul ini mencatat tiap hasil panggilan per hari (kalender
WITA) per model, plus total sepanjang pencatatan dan cap waktu hasil
terakhir per model, disimpan lewat dbstore sebagai kp_kv
data/model-stats.json (Postgres di produksi, file lokal di mode
file). Pola mengikuti cerebras_usage: fail-safe total, galat hanya
dicatat di log dan tidak pernah mengganggu panggilan AI.
"""
import copy
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import dbstore

log = logging.getLogger("kpweb.model_stats")

_PATH = "data/model-stats.json"
_WITA = timezone(timedelta(hours=8))
_LOCK = threading.Lock()
_SIMAPAN_HARI = 92  # hari kalender terakhir yang disimpan


def _nol():
    return {"ok": 0, "gagal": 0}


def _muat():
    d = None
    try:
        d = dbstore.load_json(_PATH, None)
    except Exception as e:
        log.warning("model_stats: muat gagal: %s", e)
    if not isinstance(d, dict):
        d = {}
    if not isinstance(d.get("hari"), dict):
        d["hari"] = {}
    if not isinstance(d.get("total"), dict):
        d["total"] = {}
    if not isinstance(d.get("terakhir"), dict):
        d["terakhir"] = {}
    return d


def catat(model_id, ok, lambat=False):
    """Catat satu hasil panggilan model. Fail-safe total."""
    try:
        mid = str(model_id or "").strip()
        if not mid:
            return
        hari_ini = datetime.now(_WITA).strftime("%Y-%m-%d")
        kini = time.time()
        with _LOCK:
            d = _muat()
            for wadah in (d["hari"].setdefault(hari_ini, {}), d["total"]):
                s = wadah.get(mid)
                if not isinstance(s, dict):
                    s = _nol()
                    wadah[mid] = s
                if ok:
                    s["ok"] = int(s.get("ok") or 0) + 1
                else:
                    s["gagal"] = int(s.get("gagal") or 0) + 1
            t = d["terakhir"].get(mid)
            if not isinstance(t, dict):
                t = {"ok": 0, "gagal": 0, "lama": 0}
                d["terakhir"][mid] = t
            if ok:
                t["ok"] = kini
            else:
                t["gagal"] = kini
            if lambat:
                t["lama"] = kini
            kunci_hari = sorted(d["hari"].keys())
            for lama in kunci_hari[:-_SIMAPAN_HARI]:
                d["hari"].pop(lama, None)
            dbstore.save_json_best_effort(_PATH, d)
    except Exception as e:
        log.warning("model_stats: catat gagal: %s", e)


def ringkasan():
    """Total sepanjang pencatatan + cap waktu hasil terakhir per model.

    Bentuk: {"total": {model_id: {"ok": n, "gagal": n}},
             "terakhir": {model_id: {"ok": ts, "gagal": ts, "lama": ts}}}
    """
    d = _muat()
    return {"total": copy.deepcopy(d["total"]),
            "terakhir": copy.deepcopy(d["terakhir"])}
