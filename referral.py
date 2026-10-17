"""Bonus jatah AI + sistem referral KhairyPustaka (SYNC Web <-> Bot).

Identitas referral kanonik — satu orang = satu kode & satu hitungan:
- user web terhubung Telegram (login Telegram / hasil hubungkan manual)
  -> identitas = ID Telegram numerik, tercatat di file BOT
    (/root/jurnal-bot/data/referral.json), kode KH-<tg_id> SAMA seperti di bot.
- selain itu (tamu web_<sid> / akun password belum link)
  -> identitas = uid web, tercatat di file web (data/referral.json),
  kode KH-<uid>.

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
import threading

log = logging.getLogger("skripsi_ai")

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_FILE = os.path.join(_DIR, "referral.json")
_BOT_FILE = os.path.join(os.environ.get("BOT_DATA_DIR", "/root/jurnal-bot/data"),
                         "referral.json")
_LOCK = threading.Lock()

REFERRALS_FOR_REWARD = 3   # tiap 3 teman...
REWARD_USES = 30          # ...dapat +30x jatah AI
TRIAL_USES = 10           # bonus klaim trial (1x per user, bot-only)


def _load_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_file(path, d):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.replace(tmp, path)
    except Exception as e:
        log.warning("simpan referral gagal (%s): %s", path, e)


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
    """Kode referral milik user: KH-<tg_id> bila terhubung Telegram
    (SAMA dengan kode di bot), else KH-<uid>."""
    _path, key = kanal(uid)
    return f"KH-{key}"


def parse_referrer(payload):
    """'KH-12345'/'12345' -> '12345'; 'KH-web_<hex>'/'KH-user_<hex>'
    -> 'web_<hex>'/'user_<hex>'; else None."""
    p = (payload or "").strip()
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
