-- Skema Postgres untuk KhairyPustaka web (Render + Neon/Supabase).
-- Dijalankan sekali per database: psql "$DATABASE_URL" -f schema.sql
-- (juga dijalankan otomatis oleh migrate_to_pg.py).

CREATE TABLE IF NOT EXISTS kp_kv (
  store      TEXT PRIMARY KEY,          -- mis. 'data/users.json', 'botdata/ai_quota.json'
  data       JSONB NOT NULL,            -- dokumen JSON apa adanya (dict/list)
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kp_unanswered (
  id    BIGSERIAL PRIMARY KEY,
  ts    TIMESTAMPTZ NOT NULL DEFAULT now(),
  entry JSONB NOT NULL                 -- satu baris log pertanyaan tak terjawab
);

-- Penanda migrasi (dipakai migrate_to_pg.py agar idempoten)
CREATE TABLE IF NOT EXISTS kp_migrated (
  store   TEXT PRIMARY KEY,            -- nama store yang sudah diimpor
  done_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
