"""Riwayat penggunaan per user login (fitur #/riwayat di web).

Data: data/riwayat.json -> {uid: {tipe: [entri]}}
entri = {"id": str, "tipe": "chat|sitasi|jurnal|topik",
         "judul": str, "waktu": "ISO-8601 WITA", "data": {...}}

- Hanya dicatat untuk user LOGIN (app.py mengecek g.user sebelum memanggil
  catat(); tamu tidak disimpan di server sama sekali).
- Batas 100 entri per tipe per user — yang terlama dibuang otomatis.
- Tulis atomik (tmp + os.replace) seperti modul lain.
"""
import json
import logging
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_FILE = os.path.join(_DIR, "riwayat.json")
WITA = timezone(timedelta(hours=8))

MAX_PER_TIPE = 100
TIPE_VALID = ("chat", "sitasi", "jurnal", "topik")
_JUDUL_MAKS = 80
# S2 (audit 2026-10-13): batasi blob data agar riwayat.json tidak membengkak.
# 100 entri/tipe x puluhan KB = belasan MB per user dalam satu file JSON
# yang di-parse penuh setiap request. Pangkas saat mencatat.
_MAKS_JAWABAN = 3000   # jawaban/hasil AI
_MAKS_ABSTRAK = 500    # abstrak per karya
_MAKS_WORKS = 12       # karya tersimpan per entri jurnal


def _load():
    try:
        with open(_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data):
    try:
        os.makedirs(_DIR, exist_ok=True)
        tmp = _FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _FILE)
    except Exception as e:
        log.warning("gagal simpan riwayat: %s", e)


def _judul(text, maks=_JUDUL_MAKS):
    t = " ".join((text or "").split())
    if not t:
        return "(tanpa judul)"
    if len(t) > maks:
        return t[:maks - 1].rstrip() + "…"
    return t


def _sekarang():
    return datetime.now(WITA).isoformat(timespec="seconds")


def _potong(s, maks):
    if isinstance(s, str) and len(s) > maks:
        return s[:maks].rstrip() + "…"
    return s


def _ramping(tipe, data):
    """Pangkas blob data sebelum disimpan (S2). Return dict baru."""
    if not isinstance(data, dict):
        return {}
    d = dict(data)
    if tipe == "chat":
        for k in ("jawaban", "hasil", "text", "q"):
            if k in d:
                d[k] = _potong(d[k], _MAKS_JAWABAN)
    elif tipe == "jurnal":
        hsl = d.get("hasil")
        if isinstance(hsl, list):
            nb = []
            for w in hsl[:_MAKS_WORKS]:
                if isinstance(w, dict):
                    w2 = dict(w)
                    for ak in ("abstrak", "abstract", "ringkasan"):
                        if ak in w2:
                            w2[ak] = _potong(w2[ak], _MAKS_ABSTRAK)
                    nb.append(w2)
                else:
                    nb.append(w)
            d["hasil"] = nb
        if isinstance(d.get("catatan"), list):
            d["catatan"] = d["catatan"][:10]
    elif tipe == "topik":
        if "ide" in d:
            d["ide"] = _potong(d["ide"], _MAKS_JAWABAN)
    elif tipe == "sitasi":
        if "sitasi" in d:
            d["sitasi"] = _potong(d["sitasi"], 2000)
    return d


def catat(uid, tipe, judul, data=None):
    """Catat 1 entri riwayat. Return id entri, atau None bila gagal.

    Hanya dipanggil untuk user login (tamu -> jangan panggil sama sekali).
    """
    if tipe not in TIPE_VALID or not uid:
        return None
    try:
        with _LOCK:
            d = _load()
            u = d.setdefault(str(uid), {})
            lst = u.setdefault(tipe, [])
            entri = {
                "id": uuid.uuid4().hex[:12],
                "tipe": tipe,
                "judul": _judul(judul),
                "waktu": _sekarang(),
                "data": _ramping(tipe, data),
            }
            lst.insert(0, entri)  # terbaru dulu
            del lst[MAX_PER_TIPE:]
            _save(d)
        return entri["id"]
    except Exception as e:
        log.warning("gagal catat riwayat: %s", e)
        return None


def daftar(uid, tipe=None, q=None, limit=50):
    """List entri (ringan, tanpa blob data). Urut terbaru dulu."""
    try:
        limit = max(1, min(200, int(limit or 50)))
    except (TypeError, ValueError):
        limit = 50
    d = _load().get(str(uid), {})
    tipes = [tipe] if tipe in TIPE_VALID else list(TIPE_VALID)
    out = []
    for t in tipes:
        out.extend(d.get(t, []) or [])
    if q:
        ql = q.strip().lower()
        if ql:
            out = [e for e in out if ql in (e.get("judul") or "").lower()]
    out.sort(key=lambda e: e.get("waktu") or "", reverse=True)
    return out[:limit]


def ambil(uid, entry_id):
    """Ambil 1 entri lengkap (termasuk data). Return None bila tak ketemu."""
    if not entry_id:
        return None
    u = _load().get(str(uid), {})
    for t in TIPE_VALID:
        for e in (u.get(t, []) or []):
            if e.get("id") == entry_id:
                return e
    return None


def hapus(uid, entry_id):
    """Hapus 1 entri. Return True bila ketemu & terhapus."""
    if not entry_id:
        return False
    with _LOCK:
        d = _load()
        u = d.get(str(uid), {})
        for t in TIPE_VALID:
            lst = u.get(t, []) or []
            for i, e in enumerate(lst):
                if e.get("id") == entry_id:
                    lst.pop(i)
                    _save(d)
                    return True
    return False


def hapus_tipe(uid, tipe=None):
    """Hapus riwayat per tipe; tipe=None -> hapus SEMUA. Return jumlah."""
    with _LOCK:
        d = _load()
        u = d.get(str(uid), {})
        tipes = [tipe] if tipe in TIPE_VALID else list(TIPE_VALID)
        n = 0
        for t in tipes:
            n += len(u.get(t, []) or [])
            u[t] = []
        _save(d)
    return n
