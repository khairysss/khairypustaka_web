"""Kode redeem jatah AI (giveaway admin).

Admin generate via /kodeai (pilih 25/50/100x, ketik jumlah).
User redeem via tombol di halaman /topup (1 kode = 1x pakai,
siapa cepat dia dapat). Boleh numpuk: redeem banyak kode ->
saldo jatah AI bertambah (via skripsi_ai.ai_topup).

Migrasi: kode lama yang menyimpan "days" dikonversi 1 hari = 10x AI.

Data: data/redeem.json -> {
  "codes": {"KHAIRY-A3F9K2": {"uses": 50, "used_by": "123"|None,
                              "used_at": ts|None, "created": ts}}
}
"""
import json
import logging
import os
import re
import secrets
import threading
import time

import dbstore  # Postgres (DATABASE_URL) atau file lokal, lihat dbstore.py

log = logging.getLogger("jurnalbot")

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_FILE = os.path.join(_DIR, "redeem.json")
_LOCK = threading.Lock()

# alfabet tanpa karakter ambigu (0/O, 1/I/L)
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
PREFIX = "KHAIRY"
MAX_GEN = 200


def _load():
    d = dbstore.load_json(_FILE, {})
    return d if isinstance(d, dict) else {}


def _save(d):
    dbstore.save_json(_FILE, d)


def _norm(raw):
    """Normalisasi kode: huruf besar, buang non-alfanumerik."""
    return re.sub(r"[^A-Z0-9]", "", (raw or "").upper())


def _new_code(existing):
    for _ in range(50):
        code = PREFIX + "-" + "".join(
            secrets.choice(_ALPHABET) for _ in range(6))
        if code not in existing:
            return code
    raise RuntimeError("gagal generate kode unik")


def generate(uses, n):
    """Buat n kode unik, masing-masing senilai uses x AI. Return [kode]."""
    uses, n = int(uses), int(n)
    if uses <= 0 or not 1 <= n <= MAX_GEN:
        raise ValueError("parameter generate tidak valid")
    with _LOCK:
        d = _load()
        codes = d.setdefault("codes", {})
        out = []
        now = time.time()
        for _ in range(n):
            code = _new_code(codes)
            codes[code] = {"uses": uses, "used_by": None,
                           "used_at": None, "created": now}
            out.append(code)
        _save(d)
    return out


def _uses_of(info):
    """Nilai x AI sebuah kode (migrasi: days lama -> 1 hari = 10x)."""
    try:
        uses = int(info.get("uses", 0) or 0)
    except (TypeError, ValueError):
        uses = 0
    if uses > 0:
        return uses
    try:
        days = int(info.get("days", 0) or 0)
    except (TypeError, ValueError):
        days = 0
    return max(10, days * 10)


def redeem(raw_code, chat_id):
    """Tukar kode. Return (True, uses) | (False, 'used'|'notfound')."""
    norm = _norm(raw_code)
    if not norm:
        return False, "notfound"
    with _LOCK:
        d = _load()
        codes = d.get("codes", {})
        for code, info in codes.items():
            if _norm(code) == norm:
                if info.get("used_by"):
                    return False, "used"
                info["used_by"] = str(chat_id)
                info["used_at"] = time.time()
                _save(d)
                return True, _uses_of(info)
        return False, "notfound"


def revoke(raw_code):
    """Hapus kode (admin). Return True bila ada."""
    norm = _norm(raw_code)
    with _LOCK:
        d = _load()
        codes = d.get("codes", {})
        for code in list(codes):
            if _norm(code) == norm:
                del codes[code]
                _save(d)
                return True
    return False


def stats():
    """(total, terpakai, tersedia) untuk info admin."""
    codes = _load().get("codes", {})
    used = sum(1 for v in codes.values() if v.get("used_by"))
    return len(codes), used, len(codes) - used


def list_codes():
    """Semua kode: [(kode, uses, used_by, used_at, created)], urut pembuatan."""
    codes = _load().get("codes", {})
    out = [(c, _uses_of(v), v.get("used_by"), v.get("used_at"),
            v.get("created", 0)) for c, v in codes.items()]
    out.sort(key=lambda x: x[4] or 0)
    return out
