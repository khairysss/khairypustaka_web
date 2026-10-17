#!/usr/bin/env python3
"""Migrasi data JSON rescue -> Postgres (idempoten).

Sumber data:
  --data-dir      : direktori berisi data/*.json + data/tanya_unanswered.jsonl
                    (dari opt-all.tgz -> opt/khairypustaka-web/data)
  --bot-data-dir  : direktori berisi ai_quota.json + referral.json pool shared
                    (dari root-jurnal-bot.tgz -> jurnal-bot/data)
  --rescue-dir    : ATAU langsung ke folder arsip rescue; script mengekstrak
                    sendiri yang diperlukan ke direktori sementara.

Contoh:
  DATABASE_URL="postgresql://..." python3 migrate_to_pg.py \\
      --rescue-dir ~/workspace/backups/advin-rescue-2026-10-17

Yang diimpor (idempoten, boleh dijalankan ulang):
  data/users.json, data/ai_quota.json, data/referral.json, data/usage.json,
  data/guest_usage.json, data/riwayat.json, data/deadline.json,
  data/favorit.json, data/koleksi.json, data/pantau.json,
  data/premium_pending.json, data/redeem.json           -> tabel kp_kv
  data/tanya_unanswered.jsonl                           -> tabel kp_unanswered
  botdata/ai_quota.json, botdata/referral.json          -> tabel kp_kv
  (diambil dari data bot karena bot dipensiunkan; pool shared ikut web)

Keamanan: script HANYA mencetak nama store + jumlah kunci/baris.
Tidak pernah mencetak isi data member atau nilai secret apa pun.
Jangan pernah memberi file .env sebagai argumen ke script ini.
"""

import argparse
import json
import os
import sys
import tarfile
import tempfile

try:
    import psycopg2
    from psycopg2.extras import Json
except ImportError:
    print("butuh psycopg2 (pip install psycopg2-binary)", file=sys.stderr)
    sys.exit(2)

HERE = os.path.dirname(os.path.abspath(__file__))

WEB_JSON_FILES = [
    "users.json", "ai_quota.json", "referral.json", "usage.json",
    "guest_usage.json", "riwayat.json", "deadline.json", "favorit.json",
    "koleksi.json", "pantau.json", "premium_pending.json", "redeem.json",
]
BOT_JSON_FILES = ["ai_quota.json", "referral.json"]


def _load_schema_sql():
    with open(os.path.join(HERE, "schema.sql"), encoding="utf-8") as f:
        return f.read()


def _extract_rescue(rescue_dir, tmp):
    """Ekstrak data web + data bot dari arsip rescue. Return (data_dir, bot_data_dir)."""
    opt_tgz = os.path.join(rescue_dir, "opt-all.tgz")
    bot_tgz = os.path.join(rescue_dir, "root-jurnal-bot.tgz")
    data_dir = bot_data_dir = None
    if os.path.isfile(opt_tgz):
        with tarfile.open(opt_tgz, "r:gz") as tf:
            members = [m for m in tf.getmembers()
                       if m.name.startswith("opt/khairypustaka-web/data/")
                       and not m.name.endswith(".env")
                       and ".env" not in m.name]
            tf.extractall(tmp, members=members)
        data_dir = os.path.join(tmp, "opt/khairypustaka-web/data")
    if os.path.isfile(bot_tgz):
        with tarfile.open(bot_tgz, "r:gz") as tf:
            members = [m for m in tf.getmembers()
                       if m.name.startswith("jurnal-bot/data/")]
            tf.extractall(tmp, members=members)
        bot_data_dir = os.path.join(tmp, "jurnal-bot/data")
    return data_dir, bot_data_dir


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    return d


def _count_keys(d):
    if isinstance(d, dict):
        return len(d)
    if isinstance(d, list):
        return len(d)
    return 1


def migrate(dsn, data_dir, bot_data_dir):
    try:
        conn = psycopg2.connect(dsn, connect_timeout=15)
    except Exception as e:
        print("gagal koneksi ke Postgres: %s" % e, file=sys.stderr)
        sys.exit(1)
    conn.autocommit = True
    report = []
    try:
        with conn.cursor() as cur:
            cur.execute(_load_schema_sql())

            def upsert(store, data):
                cur.execute(
                    "INSERT INTO kp_kv (store, data, updated_at) "
                    "VALUES (%s, %s, now()) "
                    "ON CONFLICT (store) DO UPDATE SET data = EXCLUDED.data, "
                    "updated_at = now()",
                    (store, Json(data)))

            def migrated(store):
                cur.execute("SELECT 1 FROM kp_migrated WHERE store = %s",
                            (store,))
                return cur.fetchone() is not None

            def mark(store):
                cur.execute(
                    "INSERT INTO kp_migrated (store) VALUES (%s) "
                    "ON CONFLICT (store) DO NOTHING", (store,))

            n = 0
            if data_dir and os.path.isdir(data_dir):
                for fn in WEB_JSON_FILES:
                    p = os.path.join(data_dir, fn)
                    if not os.path.isfile(p):
                        continue
                    try:
                        data = _read_json(p)
                    except Exception as e:
                        report.append(("data/" + fn, "GAGAL BACA: %s" % e))
                        continue
                    store = "data/" + fn
                    upsert(store, data)
                    mark(store)
                    report.append((store, "%d kunci" % _count_keys(data)))
                    n += 1

            if bot_data_dir and os.path.isdir(bot_data_dir):
                for fn in BOT_JSON_FILES:
                    p = os.path.join(bot_data_dir, fn)
                    if not os.path.isfile(p):
                        continue
                    try:
                        data = _read_json(p)
                    except Exception as e:
                        report.append(("botdata/" + fn, "GAGAL BACA: %s" % e))
                        continue
                    store = "botdata/" + fn
                    upsert(store, data)
                    mark(store)
                    report.append((store, "%d kunci" % _count_keys(data)))
                    n += 1

            # log pertanyaan tak terjawab (idempoten via penanda)
            if data_dir:
                lp = os.path.join(data_dir, "tanya_unanswered.jsonl")
                if os.path.isfile(lp) and not migrated("data/tanya_unanswered.jsonl"):
                    rows = 0
                    with open(lp, encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                entry = json.loads(line)
                            except Exception:
                                continue
                            cur.execute(
                                "INSERT INTO kp_unanswered (entry) VALUES (%s)",
                                (Json(entry),))
                            rows += 1
                    mark("data/tanya_unanswered.jsonl")
                    report.append(("data/tanya_unanswered.jsonl",
                                   "%d baris" % rows))
                elif os.path.isfile(lp):
                    report.append(("data/tanya_unanswered.jsonl",
                                   "sudah diimpor sebelumnya, dilewati"))
    finally:
        conn.close()

    print("=== hasil migrasi (nama store + hitungan saja) ===")
    for store, info in report:
        print("%-32s %s" % (store, info))
    print("selesai.")


def main():
    ap = argparse.ArgumentParser(description="Migrasi JSON rescue -> Postgres")
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL", ""),
                    help="DSN Postgres (default: env DATABASE_URL)")
    ap.add_argument("--data-dir", default="",
                    help="direktori data/*.json web")
    ap.add_argument("--bot-data-dir", default="",
                    help="direktori ai_quota.json + referral.json bot")
    ap.add_argument("--rescue-dir", default="",
                    help="folder arsip rescue (otomatis ekstrak yang perlu)")
    a = ap.parse_args()
    if not (a.dsn or "").strip():
        print("DATABASE_URL kosong. Isi env DATABASE_URL atau --dsn.",
              file=sys.stderr)
        sys.exit(2)
    data_dir, bot_data_dir = a.data_dir or None, a.bot_data_dir or None
    tmp = None
    if a.rescue_dir:
        tmp = tempfile.mkdtemp(prefix="kp-migrate-")
        data_dir, bot_data_dir = _extract_rescue(a.rescue_dir, tmp)
    if not data_dir and not bot_data_dir:
        print("tidak ada sumber data (--data-dir/--bot-data-dir/--rescue-dir).",
              file=sys.stderr)
        sys.exit(2)
    migrate(a.dsn, data_dir, bot_data_dir)


if __name__ == "__main__":
    main()
