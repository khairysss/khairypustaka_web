"""Fitur AI skripsi: /kerangka, /abstrak, /kuesioner, /latarbelakang.

Pakai rotasi provider AI (ai_providers) + kuota harian
gratis agar siap jadi fitur premium (user premium = tanpa batas).
"""
import hashlib
import json
import logging
import os
import threading
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import ai_providers
import dbstore  # Postgres (DATABASE_URL) atau file lokal, lihat dbstore.py

log = logging.getLogger("skripsi_ai")

FREE_AI_PER_DAY = 10        # login: jatah AI gratis harian
FREE_AI_PER_DAY_TAMU = 5    # tamu web (uid "web_*"): lebih ketat
GUEST_IP_AI_DAILY_CAP = 25  # hitung ganda: plafon gabungan per alamat IP
# untuk SEMUA tamu dari IP itu (lintas identitas browser). Longgar supaya
# jaringan kampus/kos tidak langsung kena hukuman; cukup menutup akal
# ganti-ganti browser di satu jaringan.
_QUOTA_PATH = "data/ai_quota.json"
_WITA = ZoneInfo("Asia/Makassar")
_LOCK = threading.Lock()  # jaga read-modify-write kuota lintas thread
# S-7 (audit 2026-10-17) PERINGATAN SCALE-OUT: lock ini hanya melindungi
# read-modify-write di DALAM satu proses. Aman hari ini (Render free =
# 1 instance = 1 proses). Bila suatu hari scale ke >1 instance/proses,
# PINDAHKAN operasi kritis ke primitif atomik Postgres (UPDATE ... WHERE
# kondisional / advisory lock) SEBELUM menambah instance — kalau tidak,
# double-redeem / kuota terlampaui / double-claim bisa terjadi.



def limit_ai_harian(uid):
    """Limit jatah AI gratis harian: tamu 5x, login 10x."""
    try:
        if str(uid or "").startswith("web_"):
            return FREE_AI_PER_DAY_TAMU
    except Exception:
        pass
    return FREE_AI_PER_DAY


def _today():
    """Tanggal hari ini dalam zona WITA (ISO 'YYYY-MM-DD')."""
    return datetime.now(_WITA).date().isoformat()

BASE = ("Kamu asisten skripsi/tugas akhir untuk mahasiswa Indonesia semua jurusan. "
        "Jawab dalam Bahasa Indonesia yang santai tapi jelas, netral jurusan. "
        "Jangan menyebut kamu AI generik; kamu asisten skripsi. ")


# ------------------------------------------------------------ kuota
def _load():
    d = dbstore.load_json(_QUOTA_PATH, {"users": {}, "premium": {}})
    if not isinstance(d, dict):
        return {"users": {}, "premium": {}}
    d.setdefault("users", {})
    d.setdefault("premium", {})
    # migrasi format lama (list) -> dict {chat_id: expires_iso}
    if isinstance(d["premium"], list):
        d["premium"] = {str(c): "2099-12-31" for c in d["premium"]}
    return d


def _save(d):
    dbstore.save_json(_QUOTA_PATH, d)


# ------------------------------------------------------------ saldo shared bot
# Saldo top-up (ai_balance) milik user yang login via Telegram disimpan di
# file data MILIK BOT (/root/jurnal-bot/data/ai_quota.json, key = Telegram
# numeric id) supaya bisa dipakai di KEDUA platform. Jatah harian 10x TIDAK
# digabung — tetap per platform (file web, key = uid web).
_BOT_DATA_DIR = os.environ.get("BOT_DATA_DIR", "/root/jurnal-bot/data")


def _bot_quota_path():
    return os.path.join(_BOT_DATA_DIR, "ai_quota.json")


def _tg_pid(chat_id):
    """Telegram numeric id (str) bila user web terhubung Telegram; None untuk
    tamu / akun tanpa tautan / data tak valid.

    Terhubung = login via Telegram (provider) ATAU hasil hubungkan manual
    (field tg_id pada akun username/password).
    """
    try:
        if not isinstance(chat_id, str) or not chat_id.startswith("user_"):
            return None
        import auth as authmod  # lazy: hindari circular import
        return authmod.linked_tg_id(chat_id)
    except Exception:
        pass
    return None


def _bot_load():
    """Baca file kuota bot. Return dict atau None bila gagal (fail-safe)."""
    d = dbstore.load_json(_bot_quota_path(), None)
    return d if isinstance(d, dict) else None


def _bot_save(d):
    dbstore.save_json(_bot_quota_path(), d)


def _bot_new():
    return {"users": {}, "premium": {}, "ai_balance": {}}


def _migrate_tg_balance(chat_id, pid):
    """Pindahkan saldo web lama (key = uid web) ke pool bot (key = pid).

    Dijalankan sekali saat user telegram-linked pertama diakses dan masih
    punya saldo >0 di file web. Idempoten: pemanggilan ulang tidak
    menggandakan. Bila tulis ke file bot gagal, saldo dikembalikan ke file
    web agar tidak hilang."""
    with _LOCK:
        d = _load()
        bal = d.get("ai_balance") or {}
        try:
            old = int(bal.pop(str(chat_id), 0) or 0)
        except (TypeError, ValueError):
            old = 0
        if not old:
            return
        _save(d)  # nol-kan sisi web dulu (atomic)
        try:
            bd = _bot_load() or _bot_new()
            bb = bd.setdefault("ai_balance", {})
            bb[pid] = int(bb.get(pid, 0) or 0) + old
            _bot_save(bd)
        except Exception as e:
            log.warning("migrasi saldo telegram gagal: %s", e)
            try:
                d2 = _load()
                b2 = d2.setdefault("ai_balance", {})
                b2[str(chat_id)] = int(b2.get(str(chat_id), 0) or 0) + old
                _save(d2)
            except Exception:
                pass


def _ensure_tg_migrated(chat_id, pid):
    """Trigger migrasi sekali jalan bila masih ada saldo web lama."""
    try:
        if int(_load().get("ai_balance", {}).get(str(chat_id), 0) or 0) > 0:
            _migrate_tg_balance(chat_id, pid)
    except Exception:
        pass


def _admin_id():
    return os.environ.get("ADMIN_CHAT_ID", "").strip()


def is_premium(chat_id):
    """True bila langganan masih berlaku (berbasis tanggal kedaluwarsa).

    Admin/pemilik bot selalu premium tanpa batas.
    """
    if _admin_id() and str(chat_id) == _admin_id():
        return True
    exp = _load().get("premium", {}).get(str(chat_id))
    return bool(exp) and exp >= _today()


def premium_until(chat_id):
    """Tanggal kedaluwarsa 'YYYY-MM-DD', 'selamanya' untuk admin, atau None."""
    if _admin_id() and str(chat_id) == _admin_id():
        return "selamanya"
    exp = _load().get("premium", {}).get(str(chat_id))
    return exp if exp and exp >= _today() else None


def set_premium(chat_id, days=30):
    """Aktifkan/perpanjang premium N hari.

    Dihitung dari expiry lama bila masih aktif (extend), bukan dari hari
    ini — jadi beli dua kali / perpanjang dini tidak menghanguskan sisa hari.
    """
    d = _load()
    if isinstance(d.get("premium"), list):  # jaga-jaga format lama
        d["premium"] = {}
    today = datetime.now(_WITA).date()
    cur = d["premium"].get(str(chat_id))
    try:
        base = max(date.fromisoformat(cur), today) if cur else today
    except (ValueError, TypeError):
        base = today
    exp = (base + timedelta(days=days)).isoformat()
    d["premium"][str(chat_id)] = exp
    _save(d)
    return exp


def del_premium(chat_id):
    d = _load()
    if isinstance(d.get("premium"), dict):
        d["premium"].pop(str(chat_id), None)
        _save(d)


def premium_sweep():
    """Dipakai background loop: kembalikan (expired, remind_h3, remind_h1).

    - expired: chat_id yang langganannya habis hari ini (dihapus).
    - remind_h3 / remind_h1: perlu diingatkan H-3 / H-1 (sekali saja).
    """
    d = _load()
    today = _today()
    prem = d.get("premium", {})
    if isinstance(prem, list):
        prem = {str(c): "2099-12-31" for c in prem}
        d["premium"] = prem
    rem = d.setdefault("reminded", [])
    expired, r3, r1 = [], [], []
    for cid, exp in list(prem.items()):
        try:
            delta = (date.fromisoformat(exp) - date.fromisoformat(today)).days
        except ValueError:
            continue
        if delta < 0:
            expired.append(cid)
            del prem[cid]
        elif delta == 3 and f"{cid}:h3" not in rem:
            r3.append(cid)
            rem.append(f"{cid}:h3")
        elif delta == 1 and f"{cid}:h1" not in rem:
            r1.append(cid)
            rem.append(f"{cid}:h1")
    _save(d)
    return expired, r3, r1


def ai_quota_left(chat_id):
    """Sisa jatah AI gratis hari ini (premium -> 999 = tak terbatas)."""
    if is_premium(chat_id):
        return 999
    u = _load().get("users", {}).get(str(chat_id))
    today = _today()
    used = u["count"] if u and u.get("date") == today else 0
    return max(0, limit_ai_harian(chat_id) - used)


def ai_try_consume(chat_id):
    """Reservasi 1 jatah AI secara ATOMIK.

    Urutan pakai: jatah harian gratis (tamu 5x / login 10x) dulu, lalu saldo top-up.
    Kembalikan pool yang dipakai ("harian"/"saldo"), atau None bila keduanya habis.
    Premium selalu "harian" tanpa mencatat. Dipakai sebelum generate AI;
    bila AI gagal, kembalikan dengan ai_refund(chat_id, pool).
    """
    if is_premium(chat_id):
        return "harian"
    pid = _tg_pid(chat_id)
    if pid:
        _ensure_tg_migrated(chat_id, pid)
    limit = limit_ai_harian(chat_id)
    with _LOCK:
        d = _load()
        today = _today()
        users = d.setdefault("users", {})
        u = users.get(str(chat_id))
        used = u["count"] if u and u.get("date") == today else 0
        if used < limit:
            users[str(chat_id)] = {"date": today, "count": used + 1}
            _save(d)
            return "harian"
        if pid:
            # user telegram-linked: saldo diambil dari pool BOT (shared)
            try:
                bd = _bot_load()
                if bd is None:
                    return None
                bb = bd.setdefault("ai_balance", {})
                saldo = int(bb.get(pid, 0) or 0)
                if saldo > 0:
                    bb[pid] = saldo - 1
                    _bot_save(bd)
                    return "saldo"
            except Exception as e:
                log.warning("pakai saldo bot gagal: %s", e)
            return None
        bal = d.setdefault("ai_balance", {})
        saldo = int(bal.get(str(chat_id), 0) or 0)
        if saldo > 0:
            bal[str(chat_id)] = saldo - 1
            _save(d)
            return "saldo"
        return None


def guest_ip_key(ip):
    """Kunci namespace ember gabungan tamu per alamat IP.

    Bentuk "ip:" + 32 hex sha256 — sengaja beda namespace dari kunci
    identitas tamu-tanpa-cookie ("web_ip" + hex di app._session), jadi
    satu pemakaian dihitung tepat 1x di tiap ember, tidak dobel.
    """
    return "ip:" + hashlib.sha256(str(ip or "unknown").encode()).hexdigest()[:32]


def ip_ai_used(ip):
    """Total pemakaian AI oleh SEMUA tamu dari alamat IP ini hari ini (WITA).

    Fail-safe baca saja: bila store gagal dibaca, anggap 0 (tidak memblokir).
    """
    try:
        d = _load()
    except Exception:
        return 0
    ipd = d.get("ip_usage") or {}
    e = ipd.get(guest_ip_key(ip))
    if isinstance(e, dict) and e.get("date") == _today():
        try:
            return max(0, int(e.get("count", 0) or 0))
        except (TypeError, ValueError):
            return 0
    return 0


def ip_ai_left(ip):
    """Sisa plafon gabungan alamat IP hari ini."""
    return max(0, GUEST_IP_AI_DAILY_CAP - ip_ai_used(ip))


def ai_try_consume_guest(uid, ip):
    """Reservasi 1 jatah AI untuk tamu web dengan hitung ganda.

    Ember identitas (5x/hari, kunci uid "web_*") DAN ember alamat IP
    (GUEST_IP_AI_DAILY_CAP/hari, kunci "ip:*") dicek DULU keduanya, baru
    keduanya dikurangi — satu langkah atomik di bawah _LOCK. Hasil:
    "harian" bila lolos, None bila identitas ATAU IP sudah habis
    (yang habis identitas tidak mengurangi ember IP, dan sebaliknya).
    Premium tetap "harian" tanpa mencatat, seperti ai_try_consume.
    Login/member TIDAK lewat sini (mereka pakai ai_try_consume biasa).
    """
    if is_premium(uid):
        return "harian"
    with _LOCK:
        d = _load()  # DBUnavailable (mode strict) menular ke pemanggil -> 503
        today = _today()
        users = d.setdefault("users", {})
        u = users.get(str(uid))
        used = 0
        if isinstance(u, dict) and u.get("date") == today:
            try:
                used = max(0, int(u.get("count", 0) or 0))
            except (TypeError, ValueError):
                used = 0
        if used >= FREE_AI_PER_DAY_TAMU:
            return None
        ipd = d.get("ip_usage") or {}
        k = guest_ip_key(ip)
        e = ipd.get(k)
        iused = 0
        if isinstance(e, dict) and e.get("date") == today:
            try:
                iused = max(0, int(e.get("count", 0) or 0))
            except (TypeError, ValueError):
                iused = 0
        if iused >= GUEST_IP_AI_DAILY_CAP:
            return None
        users[str(uid)] = {"date": today, "count": used + 1}
        ipd[k] = {"date": today, "count": iused + 1}
        d["ip_usage"] = ipd
        _save(d)
        return "harian"


def ai_refund_guest(uid, ip):
    """Kembalikan 1 jatah tamu yang gagal dipakai: kurangi ember
    identitas DAN ember alamat IP (masing-masing dijaga tidak negatif)."""
    if is_premium(uid):
        return
    with _LOCK:
        d = _load()
        today = _today()
        u = d.get("users", {}).get(str(uid))
        if isinstance(u, dict) and u.get("date") == today:
            try:
                if int(u.get("count", 0) or 0) > 0:
                    u["count"] = int(u["count"]) - 1
            except (TypeError, ValueError):
                pass
        ipd = d.get("ip_usage") or {}
        e = ipd.get(guest_ip_key(ip))
        if isinstance(e, dict) and e.get("date") == today:
            try:
                if int(e.get("count", 0) or 0) > 0:
                    e["count"] = int(e["count"]) - 1
            except (TypeError, ValueError):
                pass
        _save(d)


def ai_quota_info_guest(uid, ip):
    """(sisa_harian_efektif, saldo) untuk tamu web.

    sisa_harian = min(ember identitas, ember alamat IP) supaya tampilan
    tidak pernah menjanjikan pemakaian yang akan diblokir plafon IP.
    """
    if is_premium(uid):
        return 999, ai_balance(uid)
    return min(ai_quota_left(uid), ip_ai_left(ip)), ai_balance(uid)


# ------------------------------------------- kuota gabungan tamu (AI + non-AI)
# Keputusan Sul 2026-10-18: tamu web dapat SATU ember gabungan
# untuk SEMUA fitur (AI dan non-AI dihitung bareng).
# Menggantikan skema lama: AI 5x/hari sendiri + non-AI 10x/hari per fitur.
# REVISI Sul 2026-10-19: ember tamu diturunkan 10 -> 5x/hari supaya
# pengunjung terdorong bikin akun (member: AI 10x/hari, non-AI sepuasnya).
# Member login TIDAK lewat sini (AI 10x/hari, non-AI sepuasnya).
GUEST_ALL_DAILY_LIMIT = 5   # per identitas tamu, gabungan semua fitur
GUEST_ALL_IP_DAILY_CAP = 50  # plafon gabungan per alamat IP (lintas browser)


def _guest_all_used(d, uid, ip, today):
    """(pakai_identitas, pakai_ip) ember gabungan tamu hari ini."""
    ga = d.get("guest_all") or {}
    e = ga.get(str(uid))
    used = 0
    if isinstance(e, dict) and e.get("date") == today:
        try:
            used = max(0, int(e.get("count", 0) or 0))
        except (TypeError, ValueError):
            used = 0
    gi = d.get("guest_all_ip") or {}
    ie = gi.get(guest_ip_key(ip))
    iused = 0
    if isinstance(ie, dict) and ie.get("date") == today:
        try:
            iused = max(0, int(ie.get("count", 0) or 0))
        except (TypeError, ValueError):
            iused = 0
    return used, iused


def guest_all_try_consume(uid, ip):
    """Reservasi 1 dari ember gabungan tamu (AI + non-AI), hitung ganda.

    Ember identitas (5x/hari) DAN ember alamat IP (50x/hari) dicek dulu
    keduanya, baru keduanya ditambah — satu langkah atomik di bawah _LOCK.
    True bila lolos, False bila salah satu ember sudah habis.
    """
    if is_premium(uid):
        return True
    with _LOCK:
        d = _load()  # DBUnavailable (mode strict) menular ke pemanggil -> 503
        today = _today()
        used, iused = _guest_all_used(d, uid, ip, today)
        if used >= GUEST_ALL_DAILY_LIMIT or iused >= GUEST_ALL_IP_DAILY_CAP:
            return False
        ga = d.setdefault("guest_all", {})
        ga[str(uid)] = {"date": today, "count": used + 1}
        gi = d.setdefault("guest_all_ip", {})
        gi[guest_ip_key(ip)] = {"date": today, "count": iused + 1}
        _save(d)
        return True


def guest_all_refund(uid, ip):
    """Kembalikan 1 jatah gabungan tamu yang gagal dipakai: kurangi ember
    identitas DAN ember alamat IP (masing-masing dijaga tidak negatif)."""
    if is_premium(uid):
        return
    with _LOCK:
        d = _load()
        today = _today()
        ga = d.get("guest_all") or {}
        e = ga.get(str(uid))
        if isinstance(e, dict) and e.get("date") == today:
            try:
                if int(e.get("count", 0) or 0) > 0:
                    e["count"] = int(e["count"]) - 1
            except (TypeError, ValueError):
                pass
        gi = d.get("guest_all_ip") or {}
        ie = gi.get(guest_ip_key(ip))
        if isinstance(ie, dict) and ie.get("date") == today:
            try:
                if int(ie.get("count", 0) or 0) > 0:
                    ie["count"] = int(ie["count"]) - 1
            except (TypeError, ValueError):
                pass
        _save(d)


def guest_all_left(uid, ip):
    """Sisa ember gabungan tamu hari ini = min(identitas, IP), supaya
    tampilan tidak menjanjikan pemakaian yang akan diblokir plafon IP."""
    if is_premium(uid):
        return 999
    try:
        d = _load()
    except Exception:
        return GUEST_ALL_DAILY_LIMIT
    used, iused = _guest_all_used(d, uid, ip, _today())
    return max(0, min(GUEST_ALL_DAILY_LIMIT - used,
                      GUEST_ALL_IP_DAILY_CAP - iused))


def ai_refund(chat_id, pool=None):
    """Kembalikan 1 jatah yang sudah direservasi ke pool yang benar.

    pool WAJIB diisi dari hasil ai_try_consume (anti race bila 2 request
    konkuren user yang sama gagal bersamaan). Tanpa pool yang valid,
    default ke "harian".
    """
    if is_premium(chat_id):
        return
    if pool not in ("harian", "saldo"):
        pool = "harian"
    pid = _tg_pid(chat_id)
    with _LOCK:
        if pool == "saldo" and pid:
            # user telegram-linked: kembalikan ke pool BOT (shared)
            try:
                bd = _bot_load() or _bot_new()
                bb = bd.setdefault("ai_balance", {})
                bb[pid] = int(bb.get(pid, 0) or 0) + 1
                _bot_save(bd)
            except Exception as e:
                log.warning("refund saldo bot gagal: %s", e)
            return
        d = _load()
        if pool == "saldo":
            bal = d.setdefault("ai_balance", {})
            bal[str(chat_id)] = int(bal.get(str(chat_id), 0) or 0) + 1
        else:
            today = _today()
            u = d.get("users", {}).get(str(chat_id))
            if u and u.get("date") == today and u.get("count", 0) > 0:
                u["count"] -= 1
        _save(d)


def ai_try_consume_saldo(chat_id):
    """Reservasi 1x KHUSUS dari saldo top-up (jalur premium pemilih model).

    Atomik di bawah _LOCK yang sama dengan ai_try_consume. Return "saldo"
    bila saldo > 0 dan berhasil direservasi, None bila saldo kosong.
    Jatah harian TIDAK pernah disentuh fungsi ini — gerbang premium di
    app.py memakai ini agar model premium mustahil terlayani dari jatah
    gratis. Kembalikan reservasi gagal pakai ai_refund(chat_id, "saldo").
    """
    pid = _tg_pid(chat_id)
    if pid:
        _ensure_tg_migrated(chat_id, pid)
    with _LOCK:
        if pid:
            try:
                bd = _bot_load()
                if bd is None:
                    return None
                bb = bd.setdefault("ai_balance", {})
                saldo = int(bb.get(pid, 0) or 0)
                if saldo > 0:
                    bb[pid] = saldo - 1
                    _bot_save(bd)
                    return "saldo"
            except Exception as e:
                log.warning("pakai saldo premium bot gagal: %s", e)
            return None
        d = _load()
        bal = d.setdefault("ai_balance", {})
        saldo = int(bal.get(str(chat_id), 0) or 0)
        if saldo > 0:
            bal[str(chat_id)] = saldo - 1
            _save(d)
            return "saldo"
        return None


def ai_balance(chat_id):
    """Saldo jatah AI hasil top-up/bonus (tidak hangus).

    User telegram-linked -> baca dari pool BOT (shared antar platform).
    """
    pid = _tg_pid(chat_id)
    if pid:
        _ensure_tg_migrated(chat_id, pid)
        try:
            bd = _bot_load()
            if bd is None:
                return 0
            return int(bd.get("ai_balance", {}).get(pid, 0) or 0)
        except (TypeError, ValueError):
            return 0
    try:
        return int(_load().get("ai_balance", {}).get(str(chat_id), 0) or 0)
    except (TypeError, ValueError):
        return 0


def ai_topup(chat_id, uses):
    """Tambah saldo jatah AI. Return saldo baru.

    User telegram-linked -> tulis ke pool BOT (shared antar platform).
    ID Telegram mentah (all-digit, mis. dari reward referral) -> langsung
    ke pool BOT juga.
    """
    uses = max(0, int(uses or 0))
    pid = _tg_pid(chat_id)
    if pid is None and isinstance(chat_id, str) and chat_id.isdigit():
        pid = chat_id  # ID Telegram mentah -> pool bot langsung
    if pid:
        _ensure_tg_migrated(chat_id, pid)
        if not uses:
            return ai_balance(chat_id)
        with _LOCK:
            try:
                bd = _bot_load() or _bot_new()
                bb = bd.setdefault("ai_balance", {})
                bb[pid] = int(bb.get(pid, 0) or 0) + uses
                _bot_save(bd)
                return bb[pid]
            except Exception as e:
                log.warning("topup saldo bot gagal: %s", e)
                return ai_balance(chat_id)
    if not uses:
        return ai_balance(chat_id)
    with _LOCK:
        d = _load()
        bal = d.setdefault("ai_balance", {})
        bal[str(chat_id)] = int(bal.get(str(chat_id), 0) or 0) + uses
        _save(d)
        return bal[str(chat_id)]


def ai_quota_info(chat_id):
    """(sisa_harian, saldo). Premium -> (999, saldo)."""
    if is_premium(chat_id):
        return 999, ai_balance(chat_id)
    return ai_quota_left(chat_id), ai_balance(chat_id)


def ai_record(chat_id):
    if is_premium(chat_id):
        return
    d = _load()
    users = d["users"]
    today = _today()
    u = users.get(str(chat_id))
    if not u or u.get("date") != today:
        # total diakumulasi lintas hari (untuk /stats); count reset harian
        u = {"date": today, "count": 0,
             "total": int((u or {}).get("total", 0) or 0)}
    u["count"] += 1
    u["total"] = int(u.get("total", 0) or 0) + 1
    users[str(chat_id)] = u
    _save(d)


QUOTA_MSG = ("⏳ <b>Jatah AI harianmu habis</b> (10x/hari).\n"
             "Besok reset lagi — atau tambah jatah sekarang:\n"
             "⚡ <b>Rp5.000 = 25x pakai AI</b> (tidak hangus) — ketik /topup.")


def track_user(chat_id):
    """Catat pengguna baru (id -> tanggal pertama terlihat) + last_seen.

    STRATEGI1: last_seen diperbarui tiap interaksi agar retensi terukur
    (member aktif hari ini & member yang kembali di hari lain)."""
    try:
        d = _load()
        seen = d.setdefault("seen", {})
        last = d.setdefault("last_seen", {})
        key = str(chat_id)
        today = _today()
        changed = False
        if key not in seen:
            seen[key] = today
            changed = True
        if last.get(key) != today:
            last[key] = today
            changed = True
        if changed:
            _save(d)
    except Exception as e:
        log.warning("track user gagal: %s", e)


def first_seen(chat_id):
    """Tanggal ISO pertama user terlihat, atau None bila belum tercatat."""
    try:
        return _load().get("seen", {}).get(str(chat_id))
    except Exception:
        return None


# Command yang memakai kuota AI (untuk /stats: "Fitur AI" vs "Fitur non-AI").
AI_TOOLS = {"topik", "tanya", "kerangka", "abstrak", "kuesioner",
            "latarbelakang", "simpulan", "sidang", "kisi",
            "metode", "pembahasan", "bab2",
            "ringkas", "interpretasi", "jadwalai"}  # STRATEGI1: AI tempel


def track_tool(name, chat_id=None):
    """Catat pemakaian tool/command: hitungan total + per-hari (WITA).

    Struktur: d["tools"][name] = {"total": int, "days": {YYYY-MM-DD: int}}.
    Aktivitas admin TIDAK dicatat (stats khusus member/buyer).
    Entri hari >30 hari dipangkas agar file tidak membengkak.
    Ringan & fail-safe: kegagalan dicatat di log, tidak mengganggu bot.
    """
    try:
        name = (name or "").strip().lower()
        if not name:
            return
        admin = _admin_id()
        if admin and chat_id is not None and str(chat_id) == admin:
            return  # admin dikecualikan dari stats
        d = _load()
        tools = d.setdefault("tools", {})
        t = tools.setdefault(name, {"total": 0, "days": {}})
        t["total"] = t.get("total", 0) + 1
        today = _today()
        days = t.setdefault("days", {})
        days[today] = days.get(today, 0) + 1
        # pangkas histori lama (simpan 30 hari terakhir)
        cutoff = (datetime.now(_WITA).date() - timedelta(days=30)).isoformat()
        for k in [k for k in days if k < cutoff]:
            del days[k]
        _save(d)
    except Exception as e:
        log.warning("track tool gagal: %s", e)


def usage_stats():
    """Statistik untuk admin: member (total/premium/non-premium, TANPA admin)
    + pemakaian fitur AI vs non-AI (hari ini & keseluruhan, hitungan kali pakai)."""
    d = _load()
    today = _today()
    admin = _admin_id()
    seen = d.get("seen", {})
    total = sum(1 for k in seen if not (admin and str(k) == admin))
    prem = d.get("premium", {})
    if isinstance(prem, list):
        prem = {}
    premium_n = sum(1 for cid, exp in prem.items()
                    if isinstance(exp, str) and exp >= today
                    and not (admin and str(cid) == admin))
    ai_hari = ai_total = nonai_hari = nonai_total = 0
    for name, t in (d.get("tools") or {}).items():
        if not isinstance(t, dict):
            continue
        days = t.get("days") or {}
        h, tot = days.get(today, 0), t.get("total", 0)
        if name in AI_TOOLS:
            ai_hari += h
            ai_total += tot
        else:
            nonai_hari += h
            nonai_total += tot
    # STRATEGI1: retensi (dari last_seen) + rincian fitur terlaris
    last = d.get("last_seen", {})
    if not isinstance(last, dict):
        last = {}
    aktif_hari = sum(1 for k, v in last.items()
                     if v == today and not (admin and str(k) == admin))
    kembali = sum(1 for k, v in last.items()
                  if k in seen and v > str(seen.get(k, ""))
                  and not (admin and str(k) == admin))
    tools_top = sorted(
        ((nm, int(tt.get("total", 0))) for nm, tt in (d.get("tools") or {}).items()
         if isinstance(tt, dict)),
        key=lambda x: -x[1])[:8]
    return {"total": total, "premium": premium_n,
            "free": max(0, total - premium_n),
            "ai_hari": ai_hari, "ai_total": ai_total,
            "nonai_hari": nonai_hari, "nonai_total": nonai_total,
            "aktif_hari": aktif_hari, "kembali": kembali,
            "tools_top": tools_top}


def quota_footer(chat_id):
    """Footer transparan sisa kuota di tiap balasan AI.

    Dipanggil SETELAH ai_record() agar angkanya mutakhir.
    Premium -> string kosong (tidak diganggu footer).
    """
    if is_premium(chat_id):
        return ""
    harian, saldo = ai_quota_info(chat_id)
    s = f"\n\n<i>⚡ Sisa AI hari ini: {harian}x"
    if saldo > 0:
        s += f" (+{saldo}x saldo)"
    s += " — /topup buat tambah jatah</i>"
    return s


# ------------------------------------------------------------ prompt
def _prompt(kind, text, extra=""):
    # sidang v2 boleh menempel abstrak/ringkasan bab -> batas lebih longgar
    text = (text or "")[:2000 if kind == "sidang" else 1000]
    extra = (extra or "")[:1000]
    if kind == "kerangka":
        return (BASE +
                "Buatkan kerangka skripsi dari judul berikut.\n\nJudul: " + text +
                "\n\nJawab maksimal 400 kata dengan format:\n"
                "1. Analisis variabel: sebutkan variabel bebas (X) dan variabel terikat (Y); "
                "kalau judulnya kualitatif, sebutkan fokus penelitiannya.\n"
                "2. Penilaian singkat: apakah judul layak diteliti (terukur & tidak terlalu luas)? "
                "Kalau terlalu luas, beri saran penyempitan.\n"
                "3. Contoh rumusan masalah (3 butir).\n"
                "4. Tujuan penelitian.\n"
                "5. Outline BAB I: poin-poin latar belakang, batasan masalah, manfaat penelitian.\n"
                "6. Outline BAB II: teori/konsep apa saja yang perlu dibahas.\n"
                "7. Outline BAB III: desain penelitian yang cocok, populasi & sampel (atau partisipan), "
                "instrumen, dan teknik analisis data yang disarankan.")
    if kind == "abstrak":
        return (BASE +
                "Buatkan draf abstrak skripsi.\n\nJudul: " + extra +
                "\nTemuan utama: " + text +
                "\n\nJawab maksimal 300 kata: tulis abstrak Bahasa Indonesia "
                "(±200 kata: latar belakang, tujuan, metode, hasil, kesimpulan), "
                "lalu abstrak Bahasa Inggris, diakhiri 3-5 kata kunci. "
                "Tambahkan satu baris catatan: sesuaikan angka & metode dengan hasil penelitian sebenarnya.")
    if kind == "kuesioner":
        return (BASE +
                "Buatkan draf kuesioner penelitian.\n\nVariabel: " + text +
                "\n\nJawab maksimal 400 kata: definisi operasional singkat tiap variabel, "
                "lalu 8-10 butir pernyataan skala Likert 1-5 per variabel "
                "(campur pernyataan positif & negatif; tandai yang negatif dengan *). "
                "Tutup dengan satu baris catatan: uji validitas & reliabilitas sebelum disebar.")
    if kind == "latarbelakang":
        return (BASE +
                "Buatkan DRAF AWAL latar belakang skripsi dari judul berikut.\n\nJudul: " + text +
                "\n\nJawab maksimal 350 kata: 3-4 paragraf dengan alur fenomena umum -> masalah spesifik "
                "-> urgensi/kesenjangan penelitian -> tujuan. "
                "Sisipkan penanda [butuh sitasi] pada klaim yang memerlukan referensi.")
    if kind == "simpulan":
        return (BASE +
                "Buatkan draf kesimpulan dan saran skripsi.\n\nTemuan utama: " + text +
                "\n\nJawab maksimal 300 kata:\n"
                "1. Kesimpulan: 3-5 butir bernomor yang menjawab rumusan masalah, berdasarkan temuan.\n"
                "2. Saran: saran teoritis (untuk penelitian lanjutan) dan saran praktis (untuk pihak terkait).\n"
                "Tutup dengan satu baris catatan: sesuaikan dengan hasil penelitian sebenarnya.")
    if kind == "sidang":
        return (BASE +
                "Buatkan SIMULASI SIDANG skripsi yang lengkap dan terstruktur.\n\n"
                "Bahan (judul, dan bila ada abstrak/ringkasan tiap bab): " + text +
                "\n\nJawab maksimal 750 kata dengan format persis seperti ini:\n"
                "Pembuka: 1 kalimat penyemangat + catatan singkat bila bahan yang diberikan hanya judul "
                "(sarankan menempel abstrak supaya prediksi lebih akurat).\n"
                "A. PERTANYAAN PEMBUKA & UMUM (2 pertanyaan): pertanyaan klasik pembuka sidang, "
                "masing-masing diikuti 2-3 poin kunci jawaban.\n"
                "B. PERTANYAAN PER BAB: BAB I (2 pertanyaan), BAB II (2), BAB III (2), BAB IV (2), "
                "BAB V (1). Tiap pertanyaan diikuti 2-3 poin kunci jawaban yang spesifik terhadap "
                "judul/bahan ini, BUKAN jawaban umum. Untuk BAB III tanyakan alasan pemilihan metode, "
                "populasi/sampel, dan instrumen. Untuk BAB IV tanyakan makna hasil dan kenapa hasilnya "
                "bisa seperti itu.\n"
                "C. PERTANYAAN JEBAKAN KLASIK (2 pertanyaan): pertanyaan yang kelihatannya sederhana "
                "tapi sering menjebak, plus strategi menjawabnya dengan aman.\n"
                "D. TIPS PENUTUP: 2 tips praktis, salah satunya cara menghadapi pertanyaan yang "
                "tidak bisa dijawab tanpa mengarang.")
    if kind == "kisi":
        return (BASE +
                "Buatkan kisi-kisi instrumen penelitian.\n\nVariabel: " + text +
                "\n\nJawab maksimal 350 kata, untuk tiap variabel tulis:\n"
                "- Definisi operasional (1 kalimat)\n"
                "- Indikator-indikatornya\n"
                "- Nomor butir yang mengukur tiap indikator (contoh: butir 1-3)\n"
                "Tutup dengan satu baris catatan: kisi-kisi ini acuan membuat kuesioner/pedoman wawancara.")
    if kind == "metode":
        return (BASE +
                "Buatkan DRAF AWAL BAB III (metode penelitian) skripsi.\n\nJudul: " + text +
                "\n\nJawab maksimal 400 kata dengan struktur: desain penelitian yang cocok beserta alasannya, "
                "populasi & sampel (atau partisipan bila kualitatif), instrumen, teknik pengumpulan data, "
                "teknik analisis data. Sisipkan [sesuaikan] pada bagian yang harus diisi data sebenarnya. "
                "Netral jurusan.")
    if kind == "pembahasan":
        return (BASE +
                "Buatkan DRAF AWAL pembahasan (BAB IV) skripsi.\n\nTemuan utama: " + text +
                "\n\nJawab maksimal 350 kata: 3-4 paragraf yang mengaitkan temuan dengan teori/penelitian "
                "terdahulu (alur: temuan -> makna -> perbandingan dengan teori -> implikasi). "
                "Sisipkan [butuh sitasi] pada klaim yang perlu referensi. "
                "Akhiri dengan catatan: wajib sunting dengan bahasamu dan lengkapi sitasi.")
    if kind == "bab2":
        return (BASE +
                "Buatkan DRAF AWAL narasi BAB II (tinjauan pustaka) skripsi.\n\nBahan literatur: " + text +
                "\n\nJawab maksimal 350 kata: 3-4 paragraf sintesis yang merangkai bahan di atas menjadi "
                "alur logis (bukan ringkasan satu per satu), dengan penanda [butuh sitasi] di tiap klaim. "
                "Akhiri dengan catatan: wajib parafrase ulang dengan bahasamu dan lengkapi sitasi.")
    if kind == "ringkas":
        return (BASE +
                "Ringkas jurnal berikut untuk mahasiswa yang sedang menyusun skripsi.\n\n" + text +
                "\n\nJawab maksimal 200 kata dengan format:\n"
                "1. Inti penelitian (2-3 kalimat).\n"
                "2. Metode yang dipakai (1-2 kalimat).\n"
                "3. Temuan utama (1-2 kalimat).\n"
                "4. Relevansi untuk skripsi pembaca (1 kalimat).")
    if kind == "interpretasi":
        return (BASE +
                "Mahasiswa ini baru menerima rekomendasi uji statistik berikut:\n\n" + text +
                "\n\nBantu tuliskan contoh interpretasi hasilnya untuk BAB IV skripsi. "
                "Jawab maksimal 200 kata: 1 paragraf interpretasi siap pakai dengan penanda "
                "[isi hasil ujimu, mis. nilai p dan statistik uji] di tempat yang harus diisi angka sebenarnya, "
                "lalu 1 kalimat cara melaporkan hasilnya. Jangan mengarang angka.")
    if kind == "jadwalai":
        return (BASE +
                "Buatkan rencana pengerjaan skripsi/tugas menuju deadline berikut:\n\n" + text +
                "\n\nJawab maksimal 300 kata: bagi sisa waktu jadi tahap-tahap realistis "
                "(per minggu bila sisa lebih dari 14 hari, per hari bila 14 hari atau kurang), "
                "tiap tahap ada target konkret, sisakan 20% waktu terakhir untuk revisi & cadangan. "
                "Tutup dengan 1 kalimat penyemangat.")
    raise ValueError("kind AI tidak dikenal: " + kind)


HEADERS = {
    "kerangka": "🗂️ <b>Kerangka Skripsi</b>\n\n",
    "abstrak": "📄 <b>Draf Abstrak</b>\n\n",
    "kuesioner": "📋 <b>Draf Kuesioner</b>\n\n",
    "latarbelakang": "✍️ <b>Draf Latar Belakang</b>\n\n",
    "simpulan": "🎯 <b>Kesimpulan & Saran</b>\n\n",
    "sidang": "🛡️ <b>Simulasi Sidang</b>\n\n",
    "kisi": "📐 <b>Kisi-kisi Instrumen</b>\n\n",
    "metode": "🔬 <b>Draf BAB III (Metode)</b>\n\n",
    "pembahasan": "💬 <b>Draf Pembahasan (BAB IV)</b>\n\n",
    "bab2": "📖 <b>Draf BAB II (Tinjauan Pustaka)</b>\n\n",
    "ringkas": "✨ <b>Ringkasan AI</b>\n\n",
    "interpretasi": "✨ <b>Contoh Interpretasi (AI)</b>\n\n",
    "jadwalai": "✨ <b>Rencana Pengerjaan (AI)</b>\n\n",
}

FOOTERS = {
    "kerangka": "\n\n💡 <i>Ini kerangka awal — kembangkan dengan bahasamu sendiri dan konsultasikan ke dosen pembimbing ya.</i>",
    "abstrak": "\n\n💡 <i>Sesuaikan angka, metode, dan hasil dengan penelitianmu yang sebenarnya.</i>",
    "kuesioner": "\n\n💡 <i>Uji validitas & reliabilitas instrumen sebelum disebar ke responden.</i>",
    "latarbelakang": "\n\n⚠️ <i>Ini draf awal — wajib sunting dengan bahasamu sendiri, lengkapi sitasi pada [butuh sitasi], dan konsultasikan ke dosen.</i>",
    "simpulan": "\n\n💡 <i>Pastikan tiap butir kesimpulan menjawab rumusan masalahmu dan sesuai data hasil penelitian.</i>",
    "sidang": "\n\n💡 <i>Latih jawabanmu dengan suara lantang biar pede saat sidang nanti.</i>",
    "kisi": "\n\n💡 <i>Pakai kisi-kisi ini sebagai acuan menyusun kuesioner (/kuesioner) atau pedoman wawancara.</i>",
    "metode": "\n\n⚠️ <i>Ini draf awal — sesuaikan dengan kondisi penelitianmu yang sebenarnya dan konsultasikan ke dosen.</i>",
    "pembahasan": "\n\n⚠️ <i>Ini draf awal — wajib sunting dengan bahasamu sendiri, lengkapi sitasi pada [butuh sitasi].</i>",
    "bab2": "\n\n⚠️ <i>Ini draf awal — wajib parafrase ulang dengan bahasamu sendiri dan lengkapi sitasi pada [butuh sitasi].</i>",
    "ringkas": "\n\n💡 <i>Ringkasan AI — tetap baca jurnal aslinya sebelum dikutip ya.</i>",
    "interpretasi": "\n\n💡 <i>Ganti penanda [...] dengan angka hasil ujimu yang sebenarnya.</i>",
    "jadwalai": "\n\n💡 <i>Sesuaikan ritmenya dengan jadwal kuliahmu — yang penting konsisten.</i>",
}


# fitur yang lebih cocok dijawab model riset (Sonar) dibanding model tulis
_SEARCH_KINDS = {"tanya", "topik"}


# Ekonomi AI (audit 2026-10-17): batas output per kind, proporsional dengan
# instruksi "maksimal N kata" di tiap prompt (1 kata Indonesia ~= 1,5 token;
# angka di sini longgar 2-3x supaya jawaban normal tidak kepotong).
AI_KIND_MAXTOK = {
    "kerangka": 1500, "kuesioner": 1500, "sidang": 1500, "metode": 1500,
    "abstrak": 1200, "latarbelakang": 1200, "simpulan": 1200,
    "kisi": 1200, "pembahasan": 1200, "bab2": 1200,
    "ringkas": 800, "interpretasi": 800,
}


async def generate(kind, text, extra="", chat_id=None):
    """Generate via rotasi AI. User premium -> model berbayar (Haiku/Sonar),
    fallback otomatis ke rotasi gratis bila gagal."""
    try:
        premium = bool(chat_id) and is_premium(chat_id)
        return await ai_providers.generate(
            _prompt(kind, text, extra),
            premium=premium, search=kind in _SEARCH_KINDS,
            max_tokens=AI_KIND_MAXTOK.get(kind, 1200))
    except Exception as e:
        log.warning("generate %s gagal: %s", kind, e)
        return None



async def generate_pilihan(kind, text, extra="", chat_id=None,
                           pilihan="auto"):
    """Generate AI Studio dengan pemilih model (Sul 2026-10-19).

    Return (hasil, info) — info dict dari ai_providers.generate_pilihan
    (label model sebenarnya, dialihkan, status). Akuntansi kuota/saldo
    diatur pemanggil (app.py): fungsi ini murni generate.
    """
    try:
        info = await ai_providers.generate_pilihan(
            _prompt(kind, text, extra), pilihan=pilihan,
            max_tokens=AI_KIND_MAXTOK.get(kind, 1200))
        return (info.get("teks") or None), info
    except Exception as e:
        log.warning("generate_pilihan %s gagal: %s", kind, e)
        return None, {"teks": None, "label": None, "dialihkan": False,
                      "status": "gagal"}
