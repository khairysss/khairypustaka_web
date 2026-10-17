"""Deadline skripsi: catat tenggat (seminar, sidang, bimbingan), bot ingatkan H-7/H-3/H-1/H-0.

Penyimpanan: data/deadline.json -> {user_id: [{nama, tanggal, done: []}]}.
tanggal format ISO YYYY-MM-DD.
"""
import html
import json
import logging
import os
import re
import threading
from datetime import date, datetime

log = logging.getLogger(__name__)

# lock read-modify-write lintas thread (command vs background loop)
_LOCK = threading.Lock()

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
FILE = os.path.join(DATA_DIR, "deadline.json")

MILESTONES = [(7, "H-7"), (3, "H-3"), (1, "H-1"), (0, "HARI INI")]

_BULAN = {"jan": 1, "januari": 1, "feb": 2, "februari": 2, "mar": 3, "maret": 3,
          "apr": 4, "april": 4, "mei": 5, "jun": 6, "juni": 6, "jul": 7, "juli": 7,
          "agu": 8, "agustus": 8, "sep": 9, "september": 9, "okt": 10,
          "oktober": 10, "nov": 11, "november": 11, "des": 12, "desember": 12}


def _load():
    try:
        with open(FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(d):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, FILE)


def parse_tanggal(text):
    """'5/10/2026', '5-10-2026', '2026-10-05', '5 Okt 2026' -> date / None."""
    t = (text or "").strip().lower()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.fullmatch(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})", t)
        if m:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        else:
            m = re.fullmatch(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})", t)
            if not m:
                return None
            d, y = int(m.group(1)), int(m.group(3))
            mo = _BULAN.get(m.group(2))
            if not mo:
                return None
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def add(user_id, nama, tanggal):
    with _LOCK:
        d = _load()
        uid = str(user_id)
        items = d.get(uid, [])
        items.append({"nama": nama.strip(), "tanggal": tanggal.isoformat(), "done": []})
        items.sort(key=lambda x: x["tanggal"])
        d[uid] = items
        _save(d)
    return len(items)


def list_all(user_id):
    return _load().get(str(user_id), [])


def remove(user_id, idx):
    with _LOCK:
        d = _load()
        uid = str(user_id)
        items = d.get(uid, [])
        if 0 <= idx < len(items):
            w = items.pop(idx)
            d[uid] = items
            _save(d)
            return w
    return None


def fmt_id(iso):
    y, m, d = iso.split("-")
    nama_bulan = ["", "Januari", "Februari", "Maret", "April", "Mei", "Juni",
                  "Juli", "Agustus", "September", "Oktober", "November", "Desember"]
    return f"{int(d)} {nama_bulan[int(m)]} {y}"


def check_due():
    """Dipanggil loop berkala. Return [(user_id(int), [pesan])].

    Penandaan milestone yang terkirim digabung di dalam lock supaya
    deadline yang ditambah/dihapus di tengah pengecekan tidak hilang.
    """
    d = _load()
    today = date.today()
    out, touched = [], []
    for uid, items in d.items():
        msgs = []
        for it in items:
            try:
                tgl = datetime.strptime(it["tanggal"], "%Y-%m-%d").date()
            except ValueError:
                continue
            sisa = (tgl - today).days
            if sisa < 0 or sisa > 7:
                continue
            for hari, label in MILESTONES:
                if sisa == hari and label not in it.get("done", []):
                    touched.append((uid, it["nama"], it["tanggal"], label))
                    when = "hari ini!" if hari == 0 else f"{hari} hari lagi ({fmt_id(it['tanggal'])})"
                    msgs.append(f"⏰ <b>{html.escape(it['nama'])}</b> {when}")
        if msgs:
            try:
                out.append((int(uid), msgs))
            except ValueError:
                pass
    if touched:
        with _LOCK:
            cur = _load()
            for uid, nama, tanggal, label in touched:
                for it in cur.get(uid, []):
                    if it.get("nama") == nama and it.get("tanggal") == tanggal:
                        if label not in it.setdefault("done", []):
                            it["done"].append(label)
            _save(cur)
    return out
