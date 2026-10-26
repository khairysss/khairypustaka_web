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
import time
from datetime import datetime, timezone

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
    d = dbstore.load_json(path, {}, strict=True)
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


# ---------------------------------------------------------------- event share
# Event share KhairyPustaka (keputusan Sul 2026-10-25, periode selamanya).
# Tangga hadiah pemakaian AI premium, DIKUNCI (total, bukan tambahan):
#   share saja            -> 5x
#   share + 1 teman gabung -> total 10x (bukan 15x)
#   3 teman gabung        -> total 15x
#   5 teman gabung        -> total 20x
#   7 teman gabung        -> total 25x (mentok; teman ke 8 tidak nambah)
# Hadiah dicatat di kantong event TERPISAH ("saldo_event") di file web ini,
# bukan ditambah ke saldo top-up biasa. Jalur premium yang memotong kantong
# ini dibangun terpisah; sampai saat itu saldo_event hanya tercatat.
# Join HANYA dihitung mulai peluncuran event (tidak mengimpor hitungan
# referral lama). Klaim share dan join dicatat dalam satu transaksi
# read-modify-write di bawah _LOCK, sama seperti referral biasa.
EVENT_MAX = 25
EVENT_TIERS = ((0, 5), (1, 10), (3, 15), (5, 20), (7, 25))
# Batas event (keputusan Sul 2026-10-26): kartu hilang dari dashboard
# mulai 10 Nov 2026 00.00 WITA. Tanggal batas TIDAK ditampilkan di
# dashboard, hanya label event waktu terbatas. Hadiah yang sudah
# didapat sebelum batas tetap tersimpan di kantong event.
EVENT_END_TS = int(datetime(2026, 11, 9, 16, 0, tzinfo=timezone.utc).timestamp())


def event_berakhir():
    """True bila waktu sekarang sudah melewati batas event."""
    return time.time() >= EVENT_END_TS


def event_tier(teman, sudah_share):
    """Total hadiah event untuk jumlah teman gabung dan status share."""
    if not sudah_share:
        return 0
    hadiah = 5
    for ambang, total in EVENT_TIERS:
        if teman >= ambang:
            hadiah = total
    return hadiah


def event_key(uid):
    """Kunci identitas event untuk uid: ID Telegram bila terhubung, else uid."""
    u = str(uid or "").strip()
    if u.isdigit():
        return u
    return _tg_of_uid(u) or u


def _event_doc(d):
    """Bagian event di dokumen referral; dibuat bila belum ada."""
    ev = d.get("event")
    if not isinstance(ev, dict):
        ev = {"shared": [], "joined": {}, "diberikan": {}, "saldo_event": {}}
        d["event"] = ev
    for k, v in (("shared", []), ("joined", {}),
                 ("diberikan", {}), ("saldo_event", {})):
        if not isinstance(ev.get(k), type(v)):
            ev[k] = v
    return ev


def _event_status_dari(ev, key):
    shared = key in (ev.get("shared") or [])
    teman = len((ev.get("joined") or {}).get(key) or [])
    hadiah = event_tier(teman, shared)
    berikutnya = None
    if shared and hadiah < EVENT_MAX:
        for ambang, total in EVENT_TIERS:
            if total > hadiah:
                berikutnya = ambang - teman
                break
    return {"shared": shared, "teman": teman, "hadiah": hadiah,
            "diberikan": int((ev.get("diberikan") or {}).get(key, 0) or 0),
            "saldo_event": int((ev.get("saldo_event") or {}).get(key, 0) or 0),
            "berikutnya": berikutnya, "maks": EVENT_MAX}


def _event_bayar_locked(ev, key):
    """Bayarkan selisih tier terbaru ke kantong event (idempoten).

    Hanya menambah bila tier naik; tidak pernah mengurangi dan tidak
    pernah membayar dua kali untuk tier yang sama.
    """
    st = _event_status_dari(ev, key)
    tier = st["hadiah"]
    lama = int((ev.get("diberikan") or {}).get(key, 0) or 0)
    if tier > lama:
        ev.setdefault("diberikan", {})[key] = tier
        saldo = ev.setdefault("saldo_event", {})
        saldo[key] = int(saldo.get(key, 0) or 0) + (tier - lama)
    return _event_status_dari(ev, key)


def event_status(uid):
    """Status event untuk user (tanpa mengubah data)."""
    key = event_key(uid)
    ev = _load().get("event") or {}
    return _event_status_dari(ev, key)


def event_claim_share(uid):
    """Catat share pertama user. Return (status, baru).

    Share ke 2, ke 10, ke 100 dari akun yang sama: baru=False dan
    hadiah TIDAK bertambah (hadiah share hanya sekali per akun).
    """
    key = event_key(uid)
    with _LOCK:
        d = _load()
        ev = _event_doc(d)
        if event_berakhir():
            return _event_status_dari(ev, key), False
        shared = ev["shared"]
        if key in shared:
            return _event_status_dari(ev, key), False
        shared.append(key)
        st = _event_bayar_locked(ev, key)
        _save(d)
        return st, True


def event_record_join(ref_key, new_key):
    """Catat satu teman gabung untuk event. Return status referrer/None.

    None bila: referrer sama dengan teman baru (akun sendiri), atau
    teman baru sudah tercatat di referrer mana pun (tidak dobel).
    Join dari referrer yang belum share tetap dicatat, tapi hadiahnya
    0 sampai referrer itu share (klaim share lalu membayar tier penuh).
    """
    ref = str(ref_key or "").strip()
    new = str(new_key or "").strip()
    if event_berakhir():
        return None
    if not ref or not new or ref == new:
        return None
    with _LOCK:
        d = _load()
        ev = _event_doc(d)
        joined = ev["joined"]
        for lst in joined.values():
            if new in lst:
                return None
        joined.setdefault(ref, []).append(new)
        st = _event_bayar_locked(ev, ref)
        _save(d)
        return st


def _move_event_locked(d, old_uid, new_uid):
    """Pindahkan data event tamu ke akun baru (dipanggil dengan _LOCK).

    Dipakai migrate_guest di auth.py, di samping pemindahan kode KP-.
    Bila akun baru sudah punya data event sendiri, hitungan digabung
    (union teman, jumlah saldo) bukan ditimpa.
    """
    ev = d.get("event")
    if not isinstance(ev, dict):
        return
    old = str(old_uid or "").strip()
    new = str(new_uid or "").strip()
    if not old or not new or old == new:
        return
    ev = _event_doc(d)
    shared = ev["shared"]
    if old in shared:
        shared = [x for x in shared if x != old]
        if new not in shared:
            shared.append(new)
        ev["shared"] = shared
    joined = ev["joined"]
    if old in joined:
        lst = joined.pop(old)
        gab = joined.setdefault(new, [])
        for x in lst:
            if x not in gab:
                gab.append(x)
    for lst in joined.values():
        for i, x in enumerate(lst):
            if x == old:
                lst[i] = new
    for field in ("diberikan", "saldo_event"):
        m = ev[field]
        if old in m:
            v = int(m.pop(old, 0) or 0)
            m[new] = int(m.get(new, 0) or 0) + v
    d["event"] = ev


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
