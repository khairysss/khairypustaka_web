"""Pantau topik: langganan kata kunci, cek berkala, notifikasi artikel baru.

Penyimpanan: data/pantau.json -> {user_id: [{query, seen: [key]}]}.
"""
import html
import json
import logging
import os
import re
import threading
import time

from search_intl import search_openalex

log = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
PANTAU_FILE = os.path.join(DATA_DIR, "pantau.json")

# lock read-modify-write lintas thread (command vs background loop)
_LOCK = threading.Lock()
MAX_SUBS = 10  # batas langganan per pengguna (lindungi kuota API)


def _load():
    try:
        with open(PANTAU_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(d):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = PANTAU_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PANTAU_FILE)


def _key(w):
    if w.get("openalex_id"):
        return "ox:" + w["openalex_id"]
    if w.get("doi"):
        return "doi:" + w["doi"].lower()
    t = re.sub(r"\s+", " ", (w.get("title") or "").lower()).strip()
    return f"t:{t}|{w.get('year')}"


def subscribe(user_id, query):
    """Daftarkan pantauan. Return (ok, pesan). Baseline = hasil saat ini."""
    query = (query or "").strip()
    if not query:
        return False, "Kata kunci kosong."
    uid = str(user_id)
    with _LOCK:
        d = _load()
        subs = d.get(uid, [])
        if any(s["query"].lower() == query.lower() for s in subs):
            return False, f"Topik <b>{html.escape(query)}</b> sudah dipantau."
        if len(subs) >= MAX_SUBS:
            return False, (f"Maksimal {MAX_SUBS} topik per pengguna. "
                           "Hapus dulu topik lama sebelum tambah baru.")
    # pencarian baseline di luar lock (network, lambat)
    try:
        works = search_openalex(query, 10)
    except Exception as e:
        log.warning("baseline pantau gagal: %s", e)
        works = []
    with _LOCK:
        d = _load()
        subs = d.get(uid, [])
        if any(s["query"].lower() == query.lower() for s in subs):
            return False, f"Topik <b>{html.escape(query)}</b> sudah dipantau."
        if len(subs) >= MAX_SUBS:
            return False, f"Maksimal {MAX_SUBS} topik per pengguna."
        subs.append({"query": query, "since": int(time.time()),
                     "seen": [_key(w) for w in works]})
        d[uid] = subs
        _save(d)
    return True, (f"🔔 <b>Pemantauan dimulai</b> untuk <b>{html.escape(query)}</b>.\n"
                  f"Aku akan memberi tahu kalau ada artikel baru (cek tiap 6 jam).")


def unsubscribe(user_id, idx):
    with _LOCK:
        d = _load()
        uid = str(user_id)
        subs = d.get(uid, [])
        if 0 <= idx < len(subs):
            s = subs.pop(idx)
            d[uid] = subs
            _save(d)
            return s
    return None


def unsubscribe_all(user_id):
    with _LOCK:
        d = _load()
        uid = str(user_id)
        n = len(d.get(uid, []))
        d[uid] = []
        _save(d)
    return n


def list_subs(user_id):
    return _load().get(str(user_id), [])


def count_all():
    d = _load()
    return sum(len(v) for v in d.values())


def poll():
    """Cek semua langganan sekali putaran.

    Return [(user_id(int), query, [work baru])]. seen diperbarui & disimpan.

    Pencarian network di luar lock; penggabungan seen memakai union di
    dalam lock supaya langganan baru yang masuk di tengah polling tidak
    tertimpa hilang (lost update).
    """
    d = _load()  # snapshot untuk iterasi
    out = []
    merged = {}  # (uid, query) -> set kunci seen baru
    for uid, subs in d.items():
        for sub in subs:
            try:
                works = search_openalex(sub["query"], 10)
            except Exception as e:
                log.warning("poll pantau '%s' gagal: %s", sub["query"], e)
                continue
            seen = set(sub.get("seen", []))
            fresh = [w for w in works if _key(w) not in seen]
            new_seen = seen | {_key(w) for w in works}
            merged.setdefault((uid, sub["query"]), set()).update(new_seen)
            if fresh:
                try:
                    out.append((int(uid), sub["query"], fresh))
                except ValueError:
                    pass
    with _LOCK:
        cur = _load()
        for (uid, query), keys in merged.items():
            for sub in cur.get(uid, []):
                if sub["query"] == query:
                    sub["seen"] = sorted(set(sub.get("seen", [])) | keys)[-80:]
        _save(cur)
    return out
