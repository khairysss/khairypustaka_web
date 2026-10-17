"""Bonus jatah AI + sistem referral KhairyPustaka (SYNC Web <-> Bot).

Identitas referral kanonik — satu orang = satu kode & satu hitungan:
- user web terhubung Telegram (login Telegram / hasil hubungkan manual)
  -> identitas = ID Telegram numerik, tercatat di file BOT
    (/root/jurnal-bot/data/referral.json), kode KH-<tg_id> SAMA seperti di bot.
- selain itu (tamu web_<sid> / akun password belum link)
  -> identitas = uid web, tercatat di file web (data/referral.json).

Format kode (keputusan Sul 2026-10-17): KP-<6 char acak A-Z0-9>,
mis. KP-AB12CD. Dibuat SEKALI per user, disimpan di store
("codes": {"KP-XXXXXX": "<uid_pemilik>"}) di file web; stabil lintas
restart. Kode legacy KH-<tg_id> / KH-<uid> / <tg_id> telanjang dari era bot
TETAP dikenali saat dipakai mendaftar (lihat parse_referrer) karena
member mungkin sudah membagikannya.

Reward milestone (+30x tiap 3 teman) selalu dikirim ke pool saldo yang benar
(pool bot/shared untuk ID Telegram, pool web untuk uid web-only).

Aturan:
- Trial: 1x per user seumur hidup, bonus +10x jatah AI (bot-only).
- Referral: user baru yang gabung pakai kode teman tercatat 1x.
  Setiap kelipatan 3 teman -> pereferensia dapat +30x jatah AI.
- Anti-curang: tidak bisa mereferensikan diri sendiri; satu user baru
  hanya dihitung untuk satu pereferensia (yang pertama, dicek lintas file).
"""
import json
import logging
import os
import re
import secrets
import threading

import dbstore  # Postgres (DATABASE_URL) atau file lokal, lihat dbstore.py

log = logging.getLogger("skripsi_ai")

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_FILE = os.path.join(_DIR, "referral.json")
_BOT_FILE = os.path.join(os.environ.get("BOT_DATA_DIR", "/root/jurnal-bot/data"),
                         "referral.json")
_LOCK = threading.Lock()
# S-7 (audit 2026-10-17) PERINGATAN SCALE-OUT: lock ini hanya melindungi
# read-modify-write di DALAM satu proses. Aman hari ini (Render free =
# 1 instance = 1 proses). Bila suatu hari scale ke >1 instance/proses,
# PINDAHKAN operasi kritis ke primitif atomik Postgres (UPDATE ... WHERE
# kondisional / advisory lock) SEBELUM menambah instance — kalau tidak,
# double-redeem / kuota terlampaui / double-claim bisa terjadi.


REFERRALS_FOR_REWARD = 3   # tiap 3 teman...
REWARD_USES = 30          # ...dapat +30x jatah AI
TRIAL_USES = 10           # bonus klaim trial (1x per user, bot-only)

# ---------------------------------------------------------------- format kode
_KP_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"  # 36^6 ~ 2,2 miliar
_KP_LEN = 6


def _codes_of(path):
    """Peta codes di satu file store: {kode: uid_pemilik}. Fail-safe."""
    try:
        codes = _load_file(path).get("codes")
        return codes if isinstance(codes, dict) else {}
    except Exception:
        return {}


def _generate_unique_code(existing):
    """Hasilkan kode KP- unik. Tabrakan -> ulangi (di bawah _LOCK)."""
    taken = set(existing or ())
    try:
        taken |= set(_codes_of(_BOT_FILE))
    except Exception:
        pass
    for _ in range(1000):
        code = "KP-" + "".join(secrets.choice(_KP_ALPHABET)
                               for _ in range(_KP_LEN))
        if code not in taken:
            return code
    raise RuntimeError("gagal membuat kode referral unik")


def _load_file(path):
    d = dbstore.load_json(path, {})
    return d if isinstance(d, dict) else {}


def _save_file(path, d):
    dbstore.save_json(path, d)


def _load():
    return _load_file(_FILE)


def _save(d):
    _save_file(_FILE, d)


# ---------------------------------------------------------- identitas kanonik
def _tg_of_uid(uid):
    """ID Telegram terhubung untuk uid web, atau None. Fail-safe."""
    try:
        u = str(uid or "").strip()
        if not (u.startswith("web_") or u.startswith("user_")):
            return None
        import auth as authmod  # lazy: hindari circular import
        return authmod.linked_tg_id(u)
    except Exception:
        return None


def kanal(uid):
    """(path_file, key) kanonik untuk sebuah identitas referral.

    ID Telegram numerik ATAU uid web yang terhubung Telegram
    -> (file bot, tg_id). Selain itu -> (file web, uid).
    """
    u = str(uid or "").strip()
    if u.isdigit():
        return _BOT_FILE, u
    tg = _tg_of_uid(u)
    if tg:
        return _BOT_FILE, tg
    return _FILE, u


def kunci_lihat(uid):
    """Daftar (path, key) untuk TAMPILAN hitungan.

    uid web non-link dijumlah dari kedua file (teman bisa gabung via
    web maupun via bot dengan kode web-nya).
    """
    u = str(uid or "").strip()
    if u.isdigit():
        return [(_BOT_FILE, u)]
    tg = _tg_of_uid(u)
    if tg:
        return [(_BOT_FILE, tg)]
    if u.startswith("web_") or u.startswith("user_"):
        return [(_FILE, u), (_BOT_FILE, u)]
    return [(_FILE, u)]


# ------------------------------------------------------------------ trial
def trial_available(chat_id):
    """True bila user belum pernah klaim trial (cek file web)."""
    return str(chat_id) not in _load().get("trial_claimed", [])


def claim_trial(chat_id):
    """Catat klaim trial. Return True bila ini klaim pertama."""
    with _LOCK:
        d = _load()
        claimed = d.setdefault("trial_claimed", [])
        if str(chat_id) in claimed:
            return False
        claimed.append(str(chat_id))
        _save(d)
        return True


# ---------------------------------------------------------------- referral
def my_code(uid):
    """Kode referral milik user, format KP-XXXXXX (acak, 6 char A-Z0-9).

    Dibuat SEKALI per user lalu disimpan di store ("codes" di file web)
    sehingga stabil lintas pemanggilan & restart. Kode lama era bot
    (KH-<tg_id> / KH-<uid>) tetap bisa DIPAKAI mendaftar, tapi my_code
    tidak lagi mengeluarkannya untuk user baru.
    """
    u = str(uid or "").strip()
    if u.isdigit():
        return f"KH-{u}"  # identitas bot-era (tidak dijangkau alur web)
    with _LOCK:
        d = _load()
        codes = d.get("codes")
        if not isinstance(codes, dict):
            codes = {}
        for code, owner in codes.items():
            if owner == u:
                return code
        try:
            code = _generate_unique_code(codes)
        except RuntimeError:
            # Praktis mustahil (36^6 ruang); fallback deterministik lama
            # supaya endpoint tidak pernah 500.
            log.warning("gagal buat kode KP unik, fallback KH")
            return f"KH-{u}"
        codes[code] = u
        d["codes"] = codes
        _save(d)
        return code


def _move_code_locked(d, old_uid, new_uid):
    """Pindahkan kepemilikan kode KP- dari uid lama ke uid baru.

    Dipanggil dengan _LOCK sudah dipegang (dipakai migrate_guest di
    auth.py). Bila uid baru sudah punya kode sendiri, kode lama dibuang
    (hitungan & referred_by sudah dipindahkan pemanggil).
    """
    codes = d.get("codes")
    if not isinstance(codes, dict):
        return
    old_u = str(old_uid or "").strip()
    new_u = str(new_uid or "").strip()
    if not old_u or not new_u or old_u == new_u:
        return
    if any(o == new_u for o in codes.values()):
        for code in [c for c, o in codes.items() if o == old_u]:
            del codes[code]
        return
    for code, owner in codes.items():
        if owner == old_u:
            codes[code] = new_u


def parse_referrer(payload):
    """Kode referral -> kunci identitas pemiliknya.

    Menerima (case-insensitive; dinormalisasi ke huruf besar):
      - 'KP-AB12CD' -> kunci pemilik dari peta codes
        (None bila kode tidak dikenal: ditolak tanpa kredit)
    Kompatibilitas legacy era bot (perilaku byte-identical):
      - 'KH-12345'/'12345' -> '12345'
      - 'KH-web_<hex>'/'KH-user_<hex>' -> 'web_<hex>'/'user_<hex>'
    Selain itu -> None.
    """
    p = (payload or "").strip()
    m = re.fullmatch(r"KP-([A-Z0-9]{6})", p, re.IGNORECASE)
    if m:
        code = "KP-" + m.group(1).upper()
        for path in (_FILE, _BOT_FILE):
            owner = _codes_of(path).get(code)
            if owner:
                return owner
        return None
    m = re.fullmatch(r"KH-(\d+)", p, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.fullmatch(r"(\d{5,})", p)
    if m:
        return m.group(1)
    m = re.fullmatch(r"KH-((?:web|user)_[0-9a-f]+)", p, re.IGNORECASE)
    if m:
        return m.group(1).lower()
    return None


def count(uid):
    """Jumlah teman yang gabung lewat kode user (gabungan lintas platform)."""
    n = 0
    for path, key in kunci_lihat(uid):
        try:
            n += int(_load_file(path).get("counts", {}).get(key, 0) or 0)
        except Exception:
            pass
    return n


def need_for_next(uid):
    """Sisa teman yang dibutuhkan menuju bonus berikutnya."""
    n = count(uid)
    r = n % REFERRALS_FOR_REWARD
    return REFERRALS_FOR_REWARD if r == 0 else REFERRALS_FOR_REWARD - r


def _sudah_dirujuk(keys):
    """True bila salah satu kunci identitas sudah tercatat di referred_by
    (di file mana pun)."""
    for path in (_FILE, _BOT_FILE):
        try:
            by = _load_file(path).get("referred_by", {})
            if any(k in by for k in keys):
                return True
        except Exception:
            pass
    return False


def register(new_uid, payload):
    """Catat referral user baru (lintas platform).

    Return (referrer_key_kanonik | None, milestone: bool).
    milestone True tepat saat jumlah tembus kelipatan 3 (3, 6, 9, ...).
    referrer_key bisa '12345' (ID Telegram) atau 'web_<hex>' (uid web-only).
    """
    ref_id = parse_referrer(payload)
    if not ref_id:
        return None, False
    ref_path, ref_key = kanal(ref_id)
    new_u = str(new_uid or "").strip()
    new_keys = {new_u}
    tg_new = _tg_of_uid(new_u)
    if tg_new:
        new_keys.add(tg_new)
    if ref_key in new_keys:
        return None, False  # mereferensikan diri sendiri
    with _LOCK:
        if _sudah_dirujuk(new_keys):
            return None, False  # user baru sudah pernah dihitung
        d = _load_file(ref_path)
        by = d.setdefault("referred_by", {})
        new_key = tg_new or new_u
        by[new_key] = ref_key
        counts = d.setdefault("counts", {})
        n = int(counts.get(ref_key, 0) or 0) + 1
        counts[ref_key] = n
        _save_file(ref_path, d)
    milestone = (n % REFERRALS_FOR_REWARD == 0)
    return ref_key, milestone


def merge_referral_to_tg(uids, tg_id):
    """Satukan hitungan referral uid web lama ke identitas Telegram.

    Dipanggil saat akun web dihubungkan ke Telegram (login / hubungkan
    manual). Idempoten: aman dipanggil berulang. Fail-safe.
    """
    if isinstance(uids, str):
        uids = [uids]
    olds = [str(u or "").strip() for u in uids if str(u or "").strip()]
    tg = str(tg_id or "").strip()
    if not olds or not tg.isdigit():
        return
    olds = [o for o in olds if o != tg]
    if not olds:
        return
    with _LOCK:
        try:
            # --- sapu file web: pindahkan counts & referred_by milik uid lama
            dw = _load_file(_FILE)
            wcounts = dw.get("counts") or {}
            wby = dw.get("referred_by") or {}
            carry = 0
            moved = False
            for o in olds:
                carry += int(wcounts.pop(o, 0) or 0)
                if o in wby:
                    rb = wby.pop(o)
                    if tg not in wby:
                        wby[tg] = rb
                    moved = True
            if carry or moved:
                if carry:
                    wcounts[tg] = int(wcounts.get(tg, 0) or 0) + carry
                dw["counts"] = wcounts
                dw["referred_by"] = wby
                _save_file(_FILE, dw)
            # --- sapu file bot: entri berkunci uid web lama -> kunci tg_id
            # (hanya tulis bila ada perubahan: file ini juga ditulis proses bot,
            #  rewrite tiap request berisiko menimpa update bot yang bersamaan)
            db = _load_file(_BOT_FILE)
            bcounts = db.get("counts") or {}
            bby = db.get("referred_by") or {}
            bcarry = 0
            bmoved = False
            for o in olds:
                bcarry += int(bcounts.pop(o, 0) or 0)
                if o in bby:
                    rb = bby.pop(o)
                    if tg not in bby:
                        bby[tg] = rb
                    bmoved = True
            if bcarry or bmoved:
                if bcarry:
                    bcounts[tg] = int(bcounts.get(tg, 0) or 0) + bcarry
                db["counts"] = bcounts
                db["referred_by"] = bby
                _save_file(_BOT_FILE, db)
        except Exception as e:
            log.warning("merge referral ke tg gagal: %s", e)
