"""Pencatatan pemakaian Cerebras yang persisten + perkiraan biaya.

Latar: API Cerebras tidak menyediakan endpoint saldo atau kredit sama
sekali (saldo hanya tampil di konsol cloud Cerebras), jadi situs
mencatat sendiri tiap panggilan Cerebras yang berhasil: per hari
(kalender WITA) per model, jumlah panggilan, prompt_tokens, dan
completion_tokens dari field usage yang memang dikembalikan respons
Cerebras. Dokumen disimpan lewat dbstore sebagai kp_kv
data/cerebras-usage.json (Postgres di produksi, file lokal di mode
file). Pencatatan bersifat fail-safe total: galat apa pun hanya dicatat
di log, tidak pernah mengganggu panggilan AI.

TARIF PERKIRAAN (USD per 1 juta token), riset 2026-11-08 dari sumber
resmi Cerebras: endpoint publik tanpa kunci
https://api.cerebras.ai/public/v1/models (format huggingface memuat
pricing.input dan pricing.output per model) dan halaman resmi
https://www.cerebras.ai/pricing :
  gpt-oss-120b : input 0.35, output 0.75
  qwen-3.8-27b : input 0.99, output 1.49
Dua model itu adalah seluruh katalog Shared Inference Cerebras saat
riset. Model di luar tabel memakai tarif gpt-oss-120b sebagai
pendekatan. Semua angka biaya dari modul ini adalah PERKIRAAN.
"""
import copy
import logging
import threading
from datetime import datetime, timedelta, timezone

import dbstore

log = logging.getLogger("kpweb.cerebras_usage")

_PATH = "data/cerebras-usage.json"
_WITA = timezone(timedelta(hours=8))
_LOCK = threading.Lock()
_SIMAPAN_HARI = 92  # hari kalender terakhir yang disimpan

TARIF = {
    "gpt-oss-120b": (0.35, 0.75),
    "qwen-3.8-27b": (0.99, 1.49),
}
_TARIF_DEFAULT = TARIF["gpt-oss-120b"]


def _sekarang():
    return datetime.now(_WITA)


def _nol():
    return {"panggilan": 0, "prompt": 0, "completion": 0}


def _muat():
    d = None
    try:
        d = dbstore.load_json(_PATH, None)
    except Exception as e:
        log.warning("cerebras_usage: muat gagal: %s", e)
    if not isinstance(d, dict):
        d = {}
    if not isinstance(d.get("hari"), dict):
        d["hari"] = {}
    if not isinstance(d.get("total"), dict):
        d["total"] = {}
    return d


def catat(model, prompt_tokens, completion_tokens):
    """Catat satu panggilan Cerebras yang berhasil. Fail-safe total."""
    try:
        mid = str(model or "").strip() or "tidak dikenal"
        try:
            p = max(0, int(prompt_tokens or 0))
        except (TypeError, ValueError):
            p = 0
        try:
            c = max(0, int(completion_tokens or 0))
        except (TypeError, ValueError):
            c = 0
        hari_ini = _sekarang().strftime("%Y-%m-%d")
        with _LOCK:
            d = _muat()
            for wadah in (d["hari"].setdefault(hari_ini, {}), d["total"]):
                s = wadah.get(mid)
                if not isinstance(s, dict):
                    s = _nol()
                    wadah[mid] = s
                s["panggilan"] = int(s.get("panggilan") or 0) + 1
                s["prompt"] = int(s.get("prompt") or 0) + p
                s["completion"] = int(s.get("completion") or 0) + c
            kunci_hari = sorted(d["hari"].keys())
            for lama in kunci_hari[:-_SIMAPAN_HARI]:
                d["hari"].pop(lama, None)
            dbstore.save_json_best_effort(_PATH, d)
    except Exception as e:
        log.warning("cerebras_usage: catat gagal: %s", e)


def _jumlah(per_model):
    """Gabung dict per model menjadi satu ringkasan jumlah."""
    out = {"panggilan": 0, "prompt_tokens": 0, "completion_tokens": 0}
    for s in (per_model or {}).values():
        if not isinstance(s, dict):
            continue
        out["panggilan"] += int(s.get("panggilan") or 0)
        out["prompt_tokens"] += int(s.get("prompt") or 0)
        out["completion_tokens"] += int(s.get("completion") or 0)
    out["token"] = out["prompt_tokens"] + out["completion_tokens"]
    return out


def ringkasan():
    """Ringkasan pemakaian tercatat: hari ini, bulan ini, sepanjang
    pencatatan. Hari dan bulan mengikuti kalender WITA."""
    d = _muat()
    now = _sekarang()
    kunci_hari = now.strftime("%Y-%m-%d")
    awalan_bulan = now.strftime("%Y-%m")
    per_model_bulan = {}
    for tgl, per_model in d["hari"].items():
        if not str(tgl).startswith(awalan_bulan):
            continue
        for mid, s in (per_model or {}).items():
            if not isinstance(s, dict):
                continue
            t = per_model_bulan.setdefault(mid, _nol())
            t["panggilan"] += int(s.get("panggilan") or 0)
            t["prompt"] += int(s.get("prompt") or 0)
            t["completion"] += int(s.get("completion") or 0)
    return {
        "tanggal_hari_ini": kunci_hari,
        "bulan_ini_label": awalan_bulan,
        "hari_ini": _jumlah(d["hari"].get(kunci_hari) or {}),
        "bulan_ini": _jumlah(per_model_bulan),
        "total": _jumlah(d["total"]),
        "total_per_model": copy.deepcopy(d["total"]),
    }


def snapshot_total():
    """Salinan total sepanjang pencatatan, untuk titik nol acuan."""
    return copy.deepcopy(_muat()["total"])


def _biaya_dari(per_model):
    total = 0.0
    for mid, s in (per_model or {}).items():
        if not isinstance(s, dict):
            continue
        tarif_in, tarif_out = TARIF.get(mid, _TARIF_DEFAULT)
        total += int(s.get("prompt") or 0) / 1e6 * tarif_in
        total += int(s.get("completion") or 0) / 1e6 * tarif_out
    return total


def biaya_sejak(snap_total):
    """Perkiraan biaya (USD) dari pemakaian tercatat sejak snapshot
    titik nol diambil, dihitung per model dari selisih total."""
    kini = _muat()["total"]
    delta = {}
    for mid in set(list(kini.keys()) + list((snap_total or {}).keys())):
        a = kini.get(mid) or {}
        b = (snap_total or {}).get(mid) or {}
        delta[mid] = {
            "panggilan": int(a.get("panggilan") or 0)
            - int(b.get("panggilan") or 0),
            "prompt": max(0, int(a.get("prompt") or 0)
                          - int(b.get("prompt") or 0)),
            "completion": max(0, int(a.get("completion") or 0)
                              - int(b.get("completion") or 0)),
        }
    return _biaya_dari(delta)
