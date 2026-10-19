"""Lapisan penyimpanan JSON: file lokal ATAU Postgres, digerakkan env.

Perilaku:
- Bila env DATABASE_URL KOSONG (atau psycopg2 tak tersedia): mode file,
  100 persen sama seperti perilaku lama, baca/tulis file JSON seperti kode
  aslinya (atomic write via .tmp). Tidak pernah melempar DBUnavailable.
- Bila DATABASE_URL terisi: baca/tulis diarahkan ke Postgres (tabel kp_kv /
  kp_unanswered).
- TULISAN bersifat strict di mode Postgres: gagal DB (satu percobaan ulang
  untuk menangani bangun-tidur Neon) -> lempar DBUnavailable, dicatat
  ERROR, dan API menjawab 503. TIDAK ADA lagi penulisan diam-diam ke file
  ephemeral yang akan hilang saat restart (S-2 audit 2026-10-17).
- Pengecualian best-effort: save_json_best_effort() dan append_jsonl()
  (log diagnostik) tetap fallback ke file saat DB mati, tetapi SELALU
  mencatat ERROR divergensi agar terlihat di monitoring.
- BACAAN tetap boleh fallback ke file saat DB mati (data mungkin basi,
  tapi lebih baik daripada 500 untuk tampilan baca-saja).

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
_LOG_PRUNE_SQL = ("DELETE FROM kp_unanswered "
                         "WHERE ts < now() - (%s || ' days')::interval")


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


class DBUnavailable(Exception):
    """Postgres tidak bisa dihubungi (satu percobaan ulang sudah dilakukan).

    Dilempar oleh save_json() di mode Postgres. Penangkap di lapisan Flask
    mengubahnya menjadi 503, supaya tulisan TIDAK PERNAH jatuh diam-diam
    ke file ephemeral lalu hilang saat restart (S-2 audit 2026-10-17).
    """


def _pool_reset():
    """Buang pool yang rusak supaya percobaan berikut membangun ulang."""
    global _pg_pool_obj
    with _pool_lock:
        p, _pg_pool_obj = _pg_pool_obj, None
    if p is not None:
        try:
            p.closeall()
        except Exception:
            pass


def _pg_conn_strict():
    """Koneksi Postgres untuk TULISAN, atau lempar DBUnavailable.

    Satu percobaan ulang dilakukan dulu (menangani bangun-tidur Neon /
    koneksi basi). Mode file (tanpa DATABASE_URL) tidak pernah sampai sini.
    """
    global _pool_failed
    if not _dsn():
        return None
    for attempt in (1, 2):
        try:
            p = _pool()
            if p is None:
                raise RuntimeError("pool tidak tersedia")
            return p.getconn()
        except Exception as e:
            log.error("dbstore: koneksi Postgres gagal (percobaan %d): %s",
                      attempt, e)
            _pool_reset()
    _pool_failed = True
    raise DBUnavailable("Postgres tidak dapat dihubungi")


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

def load_json(path, default=_NO_DEFAULT, strict=False):
    """Muat dokumen JSON (dict/list). default dipakai bila tak ada / gagal.

    strict=True (WAJIB untuk dokumen read-modify-write kritis: users,
    tokens, kuota/saldo, referral, redeem, koleksi, riwayat): di mode
    Postgres TIDAK ADA fallback ke file dan TIDAK ADA default senyap
    saat DB tak terjangkau — lempar DBUnavailable (-> 503) supaya
    dokumen basi/kosong tidak pernah menjadi dasar tulisan berikutnya.
    Insiden 2026-10-19: load users jatuh ke file kosong saat koneksi
    Neon gagal sesaat, lalu hasil mutasinya menimpa dokumen asli dan
    menghapus 9 akun. Baris yang memang belum ada tetap mengembalikan
    default (kasus sah: store baru).
    """
    if not enabled():
        return file_load(path, default)
    if strict:
        name = store_name(path)
        last_err = None
        for attempt in (1, 2):
            conn = _pg_conn_strict()
            try:
                with conn.cursor() as cur:
                    cur.execute(_KV_LOAD_SQL, (name,))
                    row = cur.fetchone()
                _pg_release(conn, ok=True)
                if row is None:
                    return _resolve_default(default)
                return row[0] if row[0] is not None else _resolve_default(default)
            except DBUnavailable:
                raise
            except Exception as e:
                last_err = e
                log.error("dbstore.load_json strict gagal (%s, percobaan "
                          "%d): %s", name, attempt, e)
                _pg_release(conn, ok=False)
                _pool_reset()
        raise DBUnavailable("baca Postgres gagal: %s" % name) from last_err
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


def dump_all():
    """Seluruh isi kp_kv sebagai {store: data}. STRICT (untuk backup).

    Dipakai endpoint backup berkunci: snapshot eksternal harian supaya
    pemulihan tidak bergantung riwayat 6 jam Neon Free (insiden
    2026-10-19). Gagal DB -> DBUnavailable.
    """
    if not enabled():
        raise DBUnavailable("dump_all butuh mode Postgres")
    conn = _pg_conn_strict()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT store, data FROM kp_kv ORDER BY store")
            rows = cur.fetchall()
        _pg_release(conn, ok=True)
        return {r[0]: r[1] for r in rows}
    except DBUnavailable:
        raise
    except Exception as e:
        _pg_release(conn, ok=False)
        raise DBUnavailable("dump_all gagal") from e


def save_json(path, data):
    """Simpan dokumen JSON. STRICT di mode Postgres.

    Mode file: tulis file seperti biasa (tidak pernah melempar).
    Mode Postgres: gagal DB (sudah termasuk satu percobaan ulang) ->
    lempar DBUnavailable supaya API menjawab 503, bukan menulis diam-diam
    ke file ephemeral (S-2 audit 2026-10-17).
    """
    if not enabled():
        file_save(path, data)
        return
    name = store_name(path)
    conn = _pg_conn_strict()
    try:
        from psycopg2.extras import Json
        with conn.cursor() as cur:
            cur.execute(_KV_SAVE_SQL, (name, Json(data)))
        conn.commit()
    except DBUnavailable:
        raise
    except Exception as e:
        log.error("dbstore.save_json gagal (%s): %s", name, e)
        _pg_release(conn, ok=False)
        raise DBUnavailable("tulis ke Postgres gagal: %s" % name) from e
    _pg_release(conn, ok=True)


def save_json_best_effort(path, data):
    """Simpan dokumen JSON best-effort: gagal DB -> tulis file + ERROR log.

    Hanya untuk tulisan diagnostik/statistik yang kehilangan satu sampelnya
    tidak merusak konsistensi (contoh: statistik pemakaian). JANGAN dipakai
    untuk kuota, saldo, user, referral, redeem, atau pembayaran.
    """
    if not enabled():
        file_save(path, data)
        return
    try:
        save_json(path, data)
    except DBUnavailable as e:
        log.error("dbstore DIVERGENSI (best-effort): %s tidak tersimpan ke "
                  "Postgres, jatuh ke file lokal: %s", path, e)
        file_save(path, data)


def append_jsonl(path, entry):
    """Tambah satu baris log JSON (tanya_unanswered). Best-effort.

    Log diagnostik: gagal DB -> tulis file + ERROR log divergensi, request
    tetap jalan (kehilangan satu baris log tidak merusak konsistensi).
    """
    if not enabled():
        file_append_jsonl(path, entry)
        return
    try:
        conn = _pg_conn_strict()
    except DBUnavailable as e:
        log.error("dbstore DIVERGENSI (append_jsonl): %s tidak tersimpan ke "
                  "Postgres, jatuh ke file lokal: %s", path, e)
        file_append_jsonl(path, entry)
        return
    try:
        from psycopg2.extras import Json
        with conn.cursor() as cur:
            cur.execute(_LOG_SQL, (Json(entry),))
        conn.commit()
    except Exception as e:
        log.error("dbstore.append_jsonl gagal: %s; fallback file", e)
        _pg_release(conn, ok=False)
        file_append_jsonl(path, entry)
        return
    _pg_release(conn, ok=True)


def prune_unanswered(max_age_days=30):
    """Hapus entri tanya_unanswered lebih tua dari max_age_days (S-4).

    Mode Postgres: DELETE ... WHERE ts < now() - interval.
    Mode file: tulis ulang jsonl hanya dengan baris yang masih muda
    (berdasarkan field ts epoch; baris tanpa ts dianggap muda = aman).
    Kembalikan jumlah baris yang dihapus (0 bila tidak ada).
    """
    import time as _time
    cutoff = _time.time() - max_age_days * 86400
    if not enabled():
        path = "data/tanya_unanswered.jsonl"
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            return 0
        keep, dropped = [], 0
        for ln in lines:
            try:
                ts = json.loads(ln).get("ts")
                if ts is not None and float(ts) < cutoff:
                    dropped += 1
                    continue
            except Exception:
                pass
            keep.append(ln)
        if dropped:
            try:
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.writelines(keep)
                os.replace(tmp, path)
            except Exception as e:
                log.warning("prune_unanswered file gagal: %s", e)
                return 0
        return dropped
    try:
        conn = _pg_conn_strict()
    except DBUnavailable:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute(_LOG_PRUNE_SQL, (str(int(max_age_days)),))
            n = cur.rowcount or 0
        conn.commit()
    except Exception as e:
        log.warning("prune_unanswered PG gagal: %s", e)
        _pg_release(conn, ok=False)
        return 0
    _pg_release(conn, ok=True)
    return n
