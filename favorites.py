"""Jurnal favorit: simpan jurnal + notifikasi artikel baru.

Data: data/favorit.json -> {chat_id: [{source_id, name, added_at, last_check}]}
Pengecekan artikel baru via OpenAlex (source_new_works).
"""
import json
import logging
import os
import threading
from datetime import date

log = logging.getLogger(__name__)

# lock read-modify-write lintas thread (command vs background loop)
_LOCK = threading.Lock()

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_FILE = os.path.join(_DIR, "favorit.json")


def _load():
    try:
        with open(_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data):
    """Simpan atomik (tmp + os.replace) agar file tak korup bila dua
    penulis (command & background loop) menulis bersamaan."""
    try:
        os.makedirs(_DIR, exist_ok=True)
        tmp = _FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _FILE)
    except Exception as e:
        log.warning("gagal simpan favorit: %s", e)


def list_favs(chat_id):
    return _load().get(str(chat_id), [])


def add(chat_id, source_id, name):
    """Tambah favorit. Return (ok, pesan)."""
    if not source_id:
        return False, "Jurnal ini tidak punya ID terindeks, tidak bisa dipantau."
    with _LOCK:
        data = _load()
        key = str(chat_id)
        favs = data.setdefault(key, [])
        if any(f.get("source_id") == source_id for f in favs):
            return False, f"⭐ <b>{name}</b> sudah ada di favoritmu."
        favs.append({"source_id": source_id, "name": name,
                     "added_at": date.today().isoformat(),
                     "last_check": date.today().isoformat()})
        _save(data)
    return True, (f"⭐ <b>{name}</b> ditambahkan ke favorit!\n"
                  "Kamu akan dapat notifikasi kalau ada artikel baru.")


def remove(chat_id, idx):
    with _LOCK:
        data = _load()
        favs = data.get(str(chat_id), [])
        if 0 <= idx < len(favs):
            gone = favs.pop(idx)
            _save(data)
            return gone
    return None


def check_new():
    """Cek artikel baru semua favorit. Return [(chat_id, journal_name, [works])].

    Dipanggil dari background loop; last_check di-update per jurnal.
    Pencarian network di luar lock; update last_check digabung di dalam
    lock supaya favorit yang ditambah/dihapus di tengah cek tidak hilang.
    """
    from search_intl import source_new_works
    data = _load()
    today = date.today().isoformat()
    hasil = []
    touched = []  # (cid, source_id) yang berhasil dicek
    for cid, favs in data.items():
        for f in favs:
            since = f.get("last_check") or f.get("added_at") or today
            try:
                works = source_new_works(f["source_id"], since, per_page=3)
            except Exception as e:
                log.warning("cek favorit gagal %s: %s", f.get("name"), e)
                works = None
            if works is None:
                # sumber gagal (mis. rate-limit): JANGAN majukan last_check
                # agar artikel periode ini tetap dicek di siklus berikut
                continue
            touched.append((cid, f["source_id"]))
            if works:
                hasil.append((int(cid), f.get("name"), works))
    if touched:
        with _LOCK:
            cur = _load()
            for cid, sid in touched:
                for f in cur.get(cid, []):
                    if f.get("source_id") == sid:
                        f["last_check"] = today
            _save(cur)
    return hasil
