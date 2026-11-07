"""Feedback member KhairyPustaka (Fase 3 tema glass, 2026-11-08).

Dua bagian penyimpanan:
1. Entri feedback: tabel Postgres `kp_feedback` bila DATABASE_URL aktif
   (pola yang sama seperti kp_unanswered di dbstore), atau file
   data/feedback.jsonl + peta status data/feedback_status.json di mode
   file lokal.
2. Meta per member (penghitung pemakaian untuk memicu popup, penanda
   sudah mengisi / menunda / terakhir ditanya, batas frekuensi kirim):
   dokumen kp_kv data/feedback_meta.json lewat dbstore.

Semua fungsi fail-safe: kegagalan pencatatan TIDAK PERNAH mengganggu
alur fitur utama.
"""
import json
import logging
import threading
import time
from datetime import date, datetime

import dbstore

log = logging.getLogger("kpweb.feedback")

KATEGORI = ("kritik", "saran", "fitur", "penilaian")
STATUS = ("baru", "dibaca", "ditindaklanjuti")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS kp_feedback (
  id BIGSERIAL PRIMARY KEY,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  username TEXT NOT NULL,
  kategori TEXT NOT NULL,
  bintang INT NOT NULL DEFAULT 0,
  saran TEXT NOT NULL DEFAULT '',
  fitur TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'baru'
);
"""

_table_ready = False
_table_lock = threading.Lock()

_FILE = "data/feedback.jsonl"
_FILE_STATUS = "data/feedback_status.json"
_file_lock = threading.Lock()

_META_PATH = "data/feedback_meta.json"
_meta_lock = threading.Lock()

BATAS_KIRIM_JEDA = 60        # detik antar kiriman per member
BATAS_KIRIM_HARIAN = 5       # kiriman per member per hari
POPUP_MIN_PAKAI = 5          # popup muncul mulai pemakaian ke-5
TUNDA_POPUP_DETIK = 30 * 86400


def _today_wita():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Makassar")).date().isoformat()
    except Exception:
        return date.today().isoformat()


# ------------------------------------------------------------ tabel PG

def _ensure_table(conn):
    global _table_ready
    if _table_ready:
        return
    with _table_lock:
        if _table_ready:
            return
        with conn.cursor() as cur:
            cur.execute(_SCHEMA_SQL)
        conn.commit()
        _table_ready = True


# ------------------------------------------------------------ entri

def tambah(username, kategori, bintang, saran, fitur):
    """Simpan satu entri feedback. Return id (int) atau None bila gagal."""
    if dbstore.enabled():
        conn = None
        try:
            conn = dbstore._pg_conn_strict()
            _ensure_table(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO kp_feedback "
                    "(username, kategori, bintang, saran, fitur) "
                    "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                    (username, kategori, int(bintang), saran, fitur))
                fid = cur.fetchone()[0]
            conn.commit()
            dbstore._pg_release(conn, ok=True)
            return int(fid)
        except Exception as e:
            log.error("feedback.tambah PG gagal: %s", e)
            if conn is not None:
                dbstore._pg_release(conn, ok=False)
            return None
    # mode file
    try:
        with _file_lock:
            rows = _file_rows()
            fid = max([int(r.get("id") or 0) for r in rows] + [0]) + 1
            entri = {"id": fid, "ts": int(time.time()),
                     "username": username, "kategori": kategori,
                     "bintang": int(bintang), "saran": saran,
                     "fitur": fitur}
            with open(_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entri, ensure_ascii=False) + "\n")
            return fid
    except Exception as e:
        log.error("feedback.tambah file gagal: %s", e)
        return None


def _file_rows():
    rows = []
    try:
        with open(_FILE, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        rows.append(json.loads(ln))
                    except Exception:
                        pass
    except Exception:
        pass
    return rows


def _file_status_map():
    try:
        d = dbstore.file_load(_FILE_STATUS, {})
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def daftar(kategori=None, limit=100):
    """Daftar feedback terbaru dulu. kategori None = semua."""
    try:
        limit = max(1, min(200, int(limit)))
    except (TypeError, ValueError):
        limit = 100
    if dbstore.enabled():
        conn = dbstore._pg_conn()
        if conn is None:
            return []
        try:
            _ensure_table(conn)
            sql = ("SELECT id, extract(epoch from ts)::float, username, "
                   "kategori, bintang, saran, fitur, status FROM kp_feedback ")
            args = []
            if kategori in KATEGORI:
                sql += "WHERE kategori = %s "
                args.append(kategori)
            sql += "ORDER BY id DESC LIMIT %s"
            args.append(limit)
            with conn.cursor() as cur:
                cur.execute(sql, args)
                rows = cur.fetchall()
            return [{"id": r[0], "ts": r[1], "username": r[2],
                     "kategori": r[3], "bintang": r[4], "saran": r[5],
                     "fitur": r[6], "status": r[7]} for r in rows]
        except Exception as e:
            log.warning("feedback.daftar PG gagal: %s", e)
            return []
        finally:
            dbstore._pg_release(conn, ok=True)
    smap = _file_status_map()
    out = []
    for r in reversed(_file_rows()):
        if kategori in KATEGORI and r.get("kategori") != kategori:
            continue
        r = dict(r)
        r["status"] = smap.get(str(r.get("id"))) or "baru"
        out.append(r)
        if len(out) >= limit:
            break
    return out


def ubah_status(fid, status):
    """Ubah status satu entri. Return True bila ada baris yang berubah."""
    if status not in STATUS:
        return False
    try:
        fid = int(fid)
    except (TypeError, ValueError):
        return False
    if dbstore.enabled():
        conn = None
        try:
            conn = dbstore._pg_conn_strict()
            _ensure_table(conn)
            with conn.cursor() as cur:
                cur.execute("UPDATE kp_feedback SET status = %s "
                            "WHERE id = %s", (status, fid))
                n = cur.rowcount or 0
            conn.commit()
            dbstore._pg_release(conn, ok=True)
            return n > 0
        except Exception as e:
            log.error("feedback.ubah_status PG gagal: %s", e)
            if conn is not None:
                dbstore._pg_release(conn, ok=False)
            return False
    with _file_lock:
        if not any(int(r.get("id") or 0) == fid for r in _file_rows()):
            return False
        smap = _file_status_map()
        smap[str(fid)] = status
        dbstore.file_save(_FILE_STATUS, smap)
        return True


# ------------------------------------------------------------ meta member

def _muat_meta():
    try:
        d = dbstore.load_json(_META_PATH, {})
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _simpan_meta(d):
    try:
        dbstore.save_json_best_effort(_META_PATH, d)
    except Exception as e:
        log.warning("feedback meta simpan gagal: %s", e)


def _meta_user(d, uid):
    m = d.get(uid)
    if not isinstance(m, dict):
        m = {"pakai": 0, "isi": False, "tunda_ts": 0, "ditanya_ts": 0,
             "kirim_terakhir": 0, "kirim_tanggal": "", "kirim_jumlah": 0}
        d[uid] = m
    return m


def catat_pakai(uid):
    """Tambah 1 ke penghitung pemakaian fitur member (untuk popup)."""
    if not uid:
        return
    with _meta_lock:
        d = _muat_meta()
        m = _meta_user(d, uid)
        m["pakai"] = int(m.get("pakai") or 0) + 1
        _simpan_meta(d)


def status_popup(uid):
    """{pakai, perlu}: perlu=True bila popup layak ditampilkan sekarang."""
    with _meta_lock:
        d = _muat_meta()
        m = _meta_user(d, uid)
        pakai = int(m.get("pakai") or 0)
        perlu = (
            pakai >= POPUP_MIN_PAKAI
            and not m.get("isi")
            and (not m.get("tunda_ts")
                 or time.time() - float(m.get("tunda_ts") or 0)
                 >= TUNDA_POPUP_DETIK)
        )
        return {"pakai": pakai, "perlu": bool(perlu)}


def tandai_isi(uid):
    with _meta_lock:
        d = _muat_meta()
        m = _meta_user(d, uid)
        m["isi"] = True
        m["kirim_terakhir"] = int(time.time())
        today = _today_wita()
        if m.get("kirim_tanggal") != today:
            m["kirim_tanggal"] = today
            m["kirim_jumlah"] = 0
        m["kirim_jumlah"] = int(m.get("kirim_jumlah") or 0) + 1
        _simpan_meta(d)


def tandai_tunda(uid):
    with _meta_lock:
        d = _muat_meta()
        m = _meta_user(d, uid)
        m["tunda_ts"] = int(time.time())
        _simpan_meta(d)


def tandai_ditanya(uid):
    with _meta_lock:
        d = _muat_meta()
        m = _meta_user(d, uid)
        m["ditanya_ts"] = int(time.time())
        _simpan_meta(d)


def cek_batas_kirim(uid):
    """(boleh, pesan): jeda antar kiriman + plafon harian per member."""
    with _meta_lock:
        d = _muat_meta()
        m = _meta_user(d, uid)
        now = time.time()
        if now - float(m.get("kirim_terakhir") or 0) < BATAS_KIRIM_JEDA:
            return False, "Barusan saja kamu mengirim. Tunggu sebentar ya."
        if m.get("kirim_tanggal") == _today_wita() and \
                int(m.get("kirim_jumlah") or 0) >= BATAS_KIRIM_HARIAN:
            return False, "Kamu sudah mengirim beberapa kali hari ini. Besok bisa lagi ya."
        return True, ""
