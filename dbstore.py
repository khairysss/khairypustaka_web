"""Lapisan penyimpanan JSON: file lokal ATAU Postgres, digerakkan env.

Perilaku:
- Bila env DATABASE_URL KOSONG (atau psycopg2 tak tersedia): 100% perilaku
  lama, baca/tulis file JSON seperti kode aslinya (atomic write via .tmp).
- Bila DATABASE_URL terisi: baca/tulis diarahkan ke Postgres (tabel kp_kv /
  kp_unanswered). Kegagalan DB apa pun bersifat fail-safe: dicatat ke log
  lalu fallback ke file, supaya request tidak pernah 500 gara-gara storage.

Satu tabel cukup karena semua state app berbentuk dokumen JSON arbitrer:

    CREATE TABLE IF NOT EXISTS kp_kv (
      store TEXT PRIMARY KEY,
      data JSONB NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS kp_unanswered (
      id BIGSERIAL PRIMARY KEY,
      ts TIMESTAMPTZ NOT NULL DEFAULT now(),
      entry JSONB NOT NULL
    );

Pemetaan path file -> nama store:
    .../data/<nama>.json    -> "data/<nama>.json"
    .../botdata/<nama>.json -> "botdata/<nama>.json"   (BOT_DATA_DIR)
Ini penting karena data/ai_quota.json (jatah web) dan botdata/ai_quota.json
(pool shared) adalah dokumen yang berbeda.

Catatan konkurensi: modul pemanggil sudah memegang lock read-modify-write
mereka masing-masing; pola akses dipertahankan persis seperti versi file.
"""

import json
import logging
import os
import threading

log = logging.getLogger("kpweb.dbstore")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS kp_kv (
  store TEXT PRIMARY KEY,
  data JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS kp_unanswered (
  id BIGSERIAL PRIMARY KEY,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  entry JSONB NOT NULL
);
"""

_KV_LOAD_SQL = "SELECT data FROM kp_kv WHERE store = %s"
_KV_SAVE_SQL = ("INSERT INTO kp_kv (store, data, updated_at) VALUES (%s, %s, now()) "
                "ON CONFLICT (store) DO UPDATE SET data = EXCLUDED.data, "
                "updated_at = now()")
_LOG_SQL = "INSERT INTO kp_unanswered (entry) VALUES (%s)"


def _dsn():
    """DSN aktif, atau '' bila mode file."""
    return (os.environ.get("DATABASE_URL") or "").strip()


def enabled():
    """True bila lapisan Postgres aktif."""
    return bool(_dsn())


# ------------------------------------------------------- koneksi (lazy pool)

_pg_pool_obj = None
_pool_lock = threading.Lock()
_pool_failed = False  # True sementara bila koneksi gagal (hindari spam log)


def _pool():
    global _pg_pool_obj, _pool_failed
    dsn = _dsn()
    if not dsn:
        return None
    try:
        import psycopg2
        from psycopg2 import pool as pgpool
    except Exception as e:
        log.warning("psycopg2 tidak tersedia (%s); pakai mode file", e)
        return None
    with _pool_lock:
        if _pg_pool_obj is not None:
            return _pg_pool_obj
        try:
            _pg_pool_obj = pgpool.ThreadedConnectionPool(
                1, 4, dsn, connect_timeout=5)
            # smoke test
            conn = _pg_pool_obj.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
            finally:
                _pg_pool_obj.putconn(conn)
            _pool_failed = False
            log.info("dbstore: terhubung ke Postgres")
            return _pg_pool_obj
        except Exception as e:
            if not _pool_failed:
                log.warning("dbstore: koneksi Postgres gagal (%s); fallback file",
                            e)
                _pool_failed = True
            _pg_pool_obj = None
            return None


def _pg_conn():
    p = _pool()
    if p is None:
        return None
    try:
        return p.getconn()
    except Exception as e:
        log.warning("dbstore: getconn gagal (%s); fallback file", e)
        return None


def _pg_release(conn, ok=True):
    try:
        p = _pg_pool_obj
        if p is None:
            try:
                conn.close()
            except Exception:
                pass
            return
        if ok:
            p.putconn(conn)
        else:
            try:
                conn.rollback()
            except Exception:
                pass
            p.putconn(conn, close=True)
    except Exception:
        pass


# ------------------------------------------------------- pemetaan store

def store_name(path):
    """'.../data/x.json' -> 'data/x.json'; '.../botdata/x.json' -> 'botdata/x.json'."""
    ap = os.path.abspath(path)
    base = os.path.basename(ap)
    parent = os.path.basename(os.path.dirname(ap))
    if parent == "botdata":
        return "botdata/" + base
    return "data/" + base


# ------------------------------------------------------- primitif file (perilaku lama)

_NO_DEFAULT = object()  # bedakan "tak diberi default" dari default=None


def _resolve_default(default):
    return {} if default is _NO_DEFAULT else default


def file_load(path, default=_NO_DEFAULT):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
            if d is None:
                return _resolve_default(default)
            return d
    except Exception:
        return _resolve_default(default)


def file_save(path, data):
    try:
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception as e:
        log.warning("dbstore.file_save gagal (%s): %s", path, e)


def file_append_jsonl(path, entry):
    try:
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("dbstore.file_append_jsonl gagal (%s): %s", path, e)


# ------------------------------------------------------- API utama

def load_json(path, default=_NO_DEFAULT):
    """Muat dokumen JSON (dict/list). default dipakai bila tak ada / gagal."""
    if not enabled():
        return file_load(path, default)
    conn = _pg_conn()
    if conn is None:
        return file_load(path, default)
    name = store_name(path)
    try:
        with conn.cursor() as cur:
            cur.execute(_KV_LOAD_SQL, (name,))
            row = cur.fetchone()
        if row is None:
            # belum dimigrasi -> file sebagai sumber sementara
            return file_load(path, default)
        return row[0] if row[0] is not None else default
    except Exception as e:
        log.warning("dbstore.load_json gagal (%s): %s; fallback file", name, e)
        return file_load(path, default)
    finally:
        _pg_release(conn, ok=True)


def save_json(path, data):
    """Simpan dokumen JSON. Fail-safe: gagal DB -> tulis file."""
    if not enabled():
        file_save(path, data)
        return
    conn = _pg_conn()
    if conn is None:
        file_save(path, data)
        return
    name = store_name(path)
    try:
        from psycopg2.extras import Json
        with conn.cursor() as cur:
            cur.execute(_KV_SAVE_SQL, (name, Json(data)))
        conn.commit()
    except Exception as e:
        log.warning("dbstore.save_json gagal (%s): %s; fallback file", name, e)
        try:
            conn.rollback()
        except Exception:
            pass
        file_save(path, data)
    finally:
        _pg_release(conn, ok=True)


def append_jsonl(path, entry):
    """Tambah satu baris log JSON (tanya_unanswered)."""
    if not enabled():
        file_append_jsonl(path, entry)
        return
    conn = _pg_conn()
    if conn is None:
        file_append_jsonl(path, entry)
        return
    try:
        from psycopg2.extras import Json
        with conn.cursor() as cur:
            cur.execute(_LOG_SQL, (Json(entry),))
        conn.commit()
    except Exception as e:
        log.warning("dbstore.append_jsonl gagal: %s; fallback file", e)
        try:
            conn.rollback()
        except Exception:
            pass
        file_append_jsonl(path, entry)
    finally:
        _pg_release(conn, ok=True)
