"""Login web KhairyPustaka: username+password + mode tamu.

Login Telegram DINONAKTIFKAN TOTAL (keputusan Sul 2026-10-17): tidak ada lagi
widget/callback Telegram dan tidak ada lagi penghubungan akun ke Telegram.
Data lama (provider "telegram" / field tg_id) tetap dibaca tanpa crash,
hanya cara loginnya yang hilang; field tg_id tetap dipakai untuk merujuk
pool shared botdata (bukan login).

Identitas dipakai sebagai ``g.uid`` di app.py:
- tamu        -> "web_<sid>"   (seperti sekarang, cookie 2 tahun)
- sudah login -> "user_<12hex>" (semua modul kuota/saldo/referral otomatis
  memakai uid ini karena mereka key by str(chat_id))

Data: data/users.json -> {uid: {provider, pid, nama, email, foto, created}}
Sesi login: cookie kp_login berisi uid yang ditandatangani itsdangerous
dengan WEB_SECRET (dibuat otomatis sekali bila belum ada).
"""
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time

import dbstore  # Postgres (DATABASE_URL) atau file lokal, lihat dbstore.py
import urllib.parse
import urllib.request

log = logging.getLogger("kpweb")

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_USERS_FILE = os.path.join(_DIR, "users.json")
_TOKENS_FILE = os.path.join(_DIR, "tokens.json")
_SECRET_FILE = os.path.join(_DIR, ".web_secret")
_LOCK = threading.Lock()
# S-7 (audit 2026-10-17) PERINGATAN SCALE-OUT: lock ini hanya melindungi
# read-modify-write di DALAM satu proses. Aman hari ini (Render free =
# 1 instance = 1 proses). Bila suatu hari scale ke >1 instance/proses,
# PINDAHKAN operasi kritis ke primitif atomik Postgres (UPDATE ... WHERE
# kondisional / advisory lock) SEBELUM menambah instance — kalau tidak,
# double-redeem / kuota terlampaui / double-claim bisa terjadi.


# masa berlaku token (detik)
VERIFY_TTL = 24 * 3600   # link verifikasi email: 24 jam
RESET_TTL = 3600         # link reset password: 1 jam

LOGIN_COOKIE = "kp_login"
LOGIN_TTL = 90 * 24 * 3600  # 90 hari


# ---------------------------------------------------------------- util file
def _load_users():
    d = dbstore.load_json(_USERS_FILE, {})
    return d if isinstance(d, dict) else {}


def _save_users(d):
    dbstore.save_json(_USERS_FILE, d)


def _load_tokens():
    d = dbstore.load_json(_TOKENS_FILE, {})
    return d if isinstance(d, dict) else {}


def _save_tokens(d):
    dbstore.save_json(_TOKENS_FILE, d)


def _secret():
    """Rahasia penandatangan cookie: WEB_SECRET, atau dibuat sekali."""
    s = (os.environ.get("WEB_SECRET") or "").strip()
    if s:
        return s
    try:
        with open(_SECRET_FILE, encoding="utf-8") as f:
            s = f.read().strip()
            if s:
                # R4: amankan juga file lama yang terlanjur 644 (best effort)
                try:
                    os.chmod(_SECRET_FILE, 0o600)
                except Exception:
                    pass
                return s
    except Exception:
        pass
    s = secrets.token_hex(32)
    try:
        os.makedirs(_DIR, exist_ok=True)
        with open(_SECRET_FILE, "w", encoding="utf-8") as f:
            f.write(s)
        # R4 (audit 2026-10-13): secret penandatangan cookie = data paling
        # sensitif -> hanya owner yang boleh baca
        os.chmod(_SECRET_FILE, 0o600)
    except Exception as e:
        log.warning("simpan web secret gagal: %s", e)
    return s


def _signer():
    from itsdangerous import URLSafeSerializer
    return URLSafeSerializer(_secret(), salt="kp-login-v1")


def make_login_cookie(uid):
    u = get_user(uid) or {}
    return _signer().dumps({"uid": uid, "t": int(time.time()),
                            "v": int(u.get("sess_ver") or 0)})


def parse_login_cookie(val):
    """Kembalikan uid bila cookie valid & belum kedaluwarsa, else None.

    sess_ver: versi sesi di cookie harus cocok dengan di database. Ganti
    password me-naikkan versi -> semua sesi lain (termasuk yang dicuri)
    otomatis mati.
    """
    if not val:
        return None
    try:
        data = _signer().loads(val, max_age=LOGIN_TTL)
    except Exception:
        return None
    uid = (data or {}).get("uid")
    if not uid:
        return None
    u = _load_users().get(uid)
    if not u:
        return None
    if int(data.get("v", 0)) != int(u.get("sess_ver") or 0):
        return None
    return uid


# ------------------------------------------------------------------ users
def get_user(uid):
    return _load_users().get(uid)


def list_users():
    """Semua user: {uid: {...}}. Dipakai panel admin (baca saja)."""
    return _load_users()


def find_user(provider, pid):
    pid = str(pid)
    for uid, u in _load_users().items():
        if u.get("provider") == provider and str(u.get("pid")) == pid:
            return uid
    return None


def create_user(provider, pid, nama="", email="", foto=""):
    with _LOCK:
        d = _load_users()
        uid = "user_" + secrets.token_hex(6)
        while uid in d:
            uid = "user_" + secrets.token_hex(6)
        d[uid] = {
            "provider": provider,
            "pid": str(pid),
            "nama": (nama or "")[:80],
            "email": (email or "")[:120],
            "foto": (foto or "")[:300],
            "avatar": "teal",
            "tg_id": None,  # diisi saat akun password dihubungkan manual ke Telegram
            "created": int(time.time()),
        }
        _save_users(d)
    return uid


def touch_login(uid):
    """Catat login terakhir (best-effort)."""
    with _LOCK:
        d = _load_users()
        if uid in d:
            d[uid]["last_login"] = int(time.time())
            _save_users(d)


# ------------------------------------------------------------- Google OAuth
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def google_auth_url(client_id, redirect_uri, state=""):
    q = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "online",
        "prompt": "select_account",
        "state": state or secrets.token_hex(8),
    })
    return GOOGLE_AUTH_URL + "?" + q


def _http_post_json(url, fields, timeout=20):
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _http_get_json(url, token, timeout=20):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def google_exchange(code, client_id, client_secret, redirect_uri):
    return _http_post_json(GOOGLE_TOKEN_URL, {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    })


def google_userinfo(access_token):
    info = _http_get_json(GOOGLE_USERINFO_URL, access_token)
    if not info.get("sub"):
        raise ValueError("userinfo Google tidak valid")
    return info


# ------------------------------------------------------- username+password
import re as _re

_PW_ITER = 600_000  # iterasi PBKDF2 (rekomendasi OWASP)

# Password yang terlalu umum — ditolak saat daftar/ganti password.
_PW_BLOCKLIST = {
    "password", "password123", "password1", "12345678", "123456789",
    "qwerty123", "qwertyuiop", "admin123", "admin1234", "letmein123",
    "welcome123", "indonesia123", "manado123", "skripsi123", "mahasiswa123",
    "abcdefg1", "1234567890", "iloveyou1", "p@ssword1", "rahasia123",
}


def valid_password(pw):
    """Return pesan error bila password tidak memenuhi syarat, else None."""
    if not pw or len(pw) < 8:
        return "password minimal 8 karakter"
    if len(pw) > 128:
        return "password kepanjangan"
    if pw.lower() in _PW_BLOCKLIST:
        return "password terlalu umum, pakai yang lebih unik ya"
    return None
_USERNAME_RE = _re.compile(r"^[a-z0-9][a-z0-9._]{1,18}[a-z0-9]$")

# rate limit login: {ip: [timestamp, ...]} — maks 10 percobaan / 10 menit
_LOGIN_ATTEMPTS = {}
_LOGIN_MAX = 10
_LOGIN_WINDOW = 600


def login_allowed(ip):
    now = time.time()
    lst = [t for t in _LOGIN_ATTEMPTS.get(ip, []) if now - t < _LOGIN_WINDOW]
    _LOGIN_ATTEMPTS[ip] = lst
    return len(lst) < _LOGIN_MAX


def login_hit(ip):
    _LOGIN_ATTEMPTS.setdefault(ip, []).append(time.time())


# rate limit login PER IDENTITAS (username/email yang diketik, ternormalisasi):
# {identitas: [timestamp, ...]} — pertahanan utama dari menebak password,
# karena kunci IP terbukti tidak stabil di belakang router Render (alamat
# internal berganti antar request sehingga penghitung per-IP tidak pernah
# terkumpul; temuan tes serang 2026-10-19: 32x login gagal tanpa kunci).
_LOGIN_USER_ATTEMPTS = {}


def _ident_key(ident):
    if not isinstance(ident, str):
        return ""
    return ident.strip().lower()


def login_user_allowed(ident):
    key = _ident_key(ident)
    if not key:
        return True
    now = time.time()
    lst = [t for t in _LOGIN_USER_ATTEMPTS.get(key, [])
           if now - t < _LOGIN_WINDOW]
    _LOGIN_USER_ATTEMPTS[key] = lst
    return len(lst) < _LOGIN_MAX


def login_user_hit(ident):
    key = _ident_key(ident)
    if key:
        _LOGIN_USER_ATTEMPTS.setdefault(key, []).append(time.time())


def login_user_clear(ident):
    _LOGIN_USER_ATTEMPTS.pop(_ident_key(ident), None)


def valid_username(name):
    """Kembalikan username ternormalisasi (lowercase) atau None."""
    n = (name or "").strip().lower()
    if _USERNAME_RE.match(n):
        return n
    return None


_EMAIL_RE = _re.compile(r"^[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}$")


def valid_email(email):
    """Kembalikan email ternormalisasi (lowercase, tanpa spasi) atau None."""
    e = (email or "").strip().lower()
    if len(e) > 120:
        return None
    if _EMAIL_RE.match(e):
        return e
    return None


def hash_password(pw):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, _PW_ITER)
    return f"pbkdf2${_PW_ITER}${salt.hex()}${dk.hex()}"


def verify_password(pw, stored):
    try:
        algo, it, salt_h, dk_h = stored.split("$")
        assert algo == "pbkdf2"
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(),
                                 bytes.fromhex(salt_h), int(it))
        return hmac.compare_digest(dk.hex(), dk_h)
    except Exception:
        return False


def register_password(username, password, email, ref_code=None):
    """Daftar akun baru. Return (uid, None) sukses / (None, pesan_error).

    Email wajib & harus diverifikasi sebelum bisa login.
    ref_code (opsional): kode referral yang dibawa pendaftar; disimpan
    sebagai pending_ref dan HANYA diproses saat email terverifikasi
    (K-1 audit 2026-10-17: tamu/belum-verifikasi tidak pernah dihitung).
    """
    if not isinstance(username, str):
        username = ""
    if not isinstance(password, str):
        password = ""
    if not isinstance(email, str):
        email = ""
    uname = valid_username(username)
    if not uname:
        return None, "username 3–20 karakter: huruf, angka, titik, underscore"
    mail = valid_email(email)
    if not mail:
        return None, "email nggak valid, cek lagi ya 📧"
    perr = valid_password(password)
    if perr:
        return None, perr
    pref = (ref_code or "").strip()[:24] or None
    with _LOCK:
        d = _load_users()
        for u in d.values():
            if u.get("provider") != "password":
                continue
            if u.get("pid") == uname:
                return None, "username sudah dipakai 😅"
            if (u.get("email") or "").lower() == mail:
                return None, "email ini sudah terdaftar 📧"
        uid = "user_" + secrets.token_hex(6)
        while uid in d:
            uid = "user_" + secrets.token_hex(6)
        d[uid] = {
            "provider": "password",
            "pid": uname,
            "nama": uname,
            "email": mail,
            "email_verified": False,
            "pw_hash": hash_password(password),
            "created": int(time.time()),
        }
        if pref:
            d[uid]["pending_ref"] = pref
        _save_users(d)
    return uid, None


def delete_user(uid):
    """Hapus akun dari users.json (dipakai panel admin, mis. akun uji).
    Return True bila ada yang terhapus."""
    with _LOCK:
        d = _load_users()
        if uid not in d:
            return False
        del d[uid]
        _save_users(d)
    return True


def consume_pending_ref(uid):
    """Ambil & hapus pending_ref user (dipakai sekali saat verifikasi email)."""
    with _LOCK:
        d = _load_users()
        u = d.get(uid)
        if not u:
            return None
        code = u.pop("pending_ref", None)
        _save_users(d)
        return code


def find_password_user_by_email(email):
    """Kembalikan (uid, user) akun password dengan email tsb, else (None, None)."""
    mail = (email or "").strip().lower()
    if not mail:
        return None, None
    for uid, u in _load_users().items():
        if u.get("provider") == "password" and (u.get("email") or "").lower() == mail:
            return uid, u
    return None, None


def find_password_user_by_identifier(identifier):
    """Cari akun password dari username ATAU email.

    Dipakai alur kirim ulang verifikasi: field di halaman Masuk menerima
    username maupun email, jadi backend tidak boleh menganggap semua
    masukan sebagai email.
    """
    key = (identifier or "").strip().lower()
    if not key:
        return None, None
    for uid, u in _load_users().items():
        if u.get("provider") != "password":
            continue
        if u.get("pid") == key or (u.get("email") or "").lower() == key:
            return uid, u
    return None, None


def replace_unverified_email(uid, new_email):
    """Ganti email akun password yang BELUM terverifikasi.

    Return (email_normal, None) sukses / (None, pesan_error). Semua token
    verifikasi lama milik akun dibatalkan di transaksi yang sama, sehingga
    link yang terkirim ke alamat lama tidak bisa lagi dipakai setelah
    alamat diganti.
    """
    mail = valid_email(new_email)
    if not mail:
        return None, "email baru nggak valid, cek lagi ya 📧"
    with _LOCK:
        d = _load_users()
        u = d.get(uid)
        if not u:
            return None, "akun tidak ditemukan"
        if u.get("provider") != "password":
            return None, "ganti email ini hanya untuk akun username+password 😅"
        if u.get("email_verified"):
            return None, "akun ini sudah terverifikasi — ganti email dari halaman Akun ya"
        if (u.get("email") or "").lower() == mail:
            return None, "itu email kamu yang sekarang 😄"
        for oid, o in d.items():
            if oid == uid or o.get("provider") != "password":
                continue
            if (o.get("email") or "").lower() == mail:
                return None, "email baru ini sudah dipakai akun lain 📧"
        u["email"] = mail
        u["email_verified"] = False
        _save_users(d)
        tokens = _load_tokens()
        kept = {t: v for t, v in tokens.items()
                if not (v.get("uid") == uid and v.get("kind") == "verify")}
        if len(kept) != len(tokens):
            _save_tokens(kept)
    return mail, None


def set_email_verified(uid):
    with _LOCK:
        d = _load_users()
        u = d.get(uid)
        if not u:
            return False
        u["email_verified"] = True
        _save_users(d)
    return True


def update_password(uid, password):
    """Ganti password. Return None sukses / pesan_error."""
    perr = valid_password(password)
    if perr:
        return perr
    with _LOCK:
        d = _load_users()
        u = d.get(uid)
        if not u:
            return "akun tidak ditemukan"
        u["pw_hash"] = hash_password(password)
        # bunuh semua sesi lain (termasuk yang mungkin dibajak)
        u["sess_ver"] = int(u.get("sess_ver") or 0) + 1
        _save_users(d)
    return None


def login_password(ident, password):
    """Return uid bila cocok, else None. Selalu hash (anti timing-oracle).

    ident bisa username ATAU email.
    """
    if not isinstance(ident, str):
        ident = ""
    if not isinstance(password, str):
        password = ""
    key = (ident or "").strip().lower()
    found = None
    for uid, u in _load_users().items():
        if u.get("provider") != "password":
            continue
        if u.get("pid") == key or (u.get("email") or "").lower() == key:
            found = (uid, u)
            break
    # dummy verify biar waktu respons mirip walau user tidak ada
    dummy = "pbkdf2$600000$" + "00" * 16 + "$" + "00" * 32
    if not found:
        verify_password(password or "", dummy)
        return None
    uid, u = found
    if verify_password(password or "", u.get("pw_hash") or ""):
        return uid
    return None


# ------------------------------------------------------- token email
def issue_token(uid, kind, ttl, extra=None):
    """Buat token sekali pakai. kind: 'verify' | 'reset' | 'ubah_email'.
    extra: dict opsional (mis. {"email_baru": ...}). Return token string."""
    tok = secrets.token_urlsafe(32)
    with _LOCK:
        d = _load_tokens()
        now = int(time.time())
        # bersih-bersih token kedaluwarsa sekalian
        d = {t: v for t, v in d.items() if v.get("exp", 0) > now}
        rec = {"uid": uid, "kind": kind, "exp": now + int(ttl)}
        if extra:
            rec.update(extra)
        d[tok] = rec
        _save_tokens(d)
    return tok


def consume_token_data(token, kind):
    """Pakai token sekali pakai. Return dict token bila valid, else None."""
    tok = (token or "").strip()
    if not tok:
        return None
    with _LOCK:
        d = _load_tokens()
        v = d.get(tok)
        if not v or v.get("kind") != kind or v.get("exp", 0) < int(time.time()):
            return None
        d.pop(tok, None)
        _save_tokens(d)
    return v


def consume_token(token, kind):
    """Pakai token sekali pakai. Return uid bila valid, else None."""
    v = consume_token_data(token, kind)
    return v.get("uid") if v else None


# ------------------------------------------------------- edit profil
def change_password(uid, old_pw, new_pw):
    """Ganti password setelah verifikasi password lama.
    Return None sukses / pesan_error."""
    perr = valid_password(new_pw)
    if perr:
        return perr.replace("password minimal", "password baru minimal")
    with _LOCK:
        d = _load_users()
        u = d.get(uid)
        if not u:
            return "akun tidak ditemukan"
        if u.get("provider") != "password" or not u.get("pw_hash"):
            return "akun ini login via Telegram/Google — belum punya password 😅"
        if not verify_password(old_pw or "", u.get("pw_hash") or ""):
            return "password lama salah 😅"
        u["pw_hash"] = hash_password(new_pw)
        # bunuh semua sesi lain (termasuk yang mungkin dibajak)
        u["sess_ver"] = int(u.get("sess_ver") or 0) + 1
        _save_users(d)
    return None


def change_username(uid, new_uname):
    """Ganti username (pid). Return None sukses / pesan_error."""
    uname = valid_username(new_uname)
    if not uname:
        return "username 3–20 karakter: huruf kecil, angka, titik, underscore"
    with _LOCK:
        d = _load_users()
        u = d.get(uid)
        if not u:
            return "akun tidak ditemukan"
        if u.get("provider") != "password":
            return "ubah username hanya untuk akun username+password 😅"
        if u.get("pid") == uname:
            return "itu username kamu yang sekarang 😄"
        for oid, o in d.items():
            if oid == uid or o.get("provider") != "password":
                continue
            if (o.get("pid") or "").lower() == uname:
                return "username sudah dipakai 😅"
        u["pid"] = uname
        u["nama"] = uname
        _save_users(d)
    return None


def request_email_change(uid, new_email):
    """Validasi email baru & terbitkan token 'ubah_email' (24 jam).
    Return (token, email_normal, None) sukses / (None, None, pesan_error)."""
    mail = valid_email(new_email)
    if not mail:
        return None, None, "email nggak valid, cek lagi ya 📧"
    with _LOCK:
        d = _load_users()
        u = d.get(uid)
        if not u:
            return None, None, "akun tidak ditemukan"
        if u.get("provider") != "password":
            return None, None, "ubah email hanya untuk akun username+password 😅"
        if (u.get("email") or "").lower() == mail:
            return None, None, "itu email kamu yang sekarang 😄"
        for oid, o in d.items():
            if oid == uid or o.get("provider") != "password":
                continue
            if (o.get("email") or "").lower() == mail:
                return None, None, "email ini sudah dipakai akun lain 📧"
    tok = issue_token(uid, "ubah_email", VERIFY_TTL, {"email_baru": mail})
    return tok, mail, None


def apply_email_change(uid, new_email):
    """Terapkan email baru (dipanggil setelah token ubah_email valid).
    Return None sukses / pesan_error."""
    mail = valid_email(new_email)
    if not mail:
        return "email nggak valid"
    with _LOCK:
        d = _load_users()
        u = d.get(uid)
        if not u:
            return "akun tidak ditemukan"
        # pastikan masih unik (bisa berubah sejak token diterbitkan)
        for oid, o in d.items():
            if oid == uid or o.get("provider") != "password":
                continue
            if (o.get("email") or "").lower() == mail:
                return "email ini sudah dipakai akun lain 📧"
        u["email"] = mail
        u["email_verified"] = True
        _save_users(d)
    return None


AVATAR_PILIHAN = ["teal", "orange", "navy", "red", "purple"]


def user_avatar(u):
    """Avatar user (default 'teal' untuk record lama)."""
    a = (u or {}).get("avatar") or "teal"
    return a if a in AVATAR_PILIHAN else "teal"


def set_avatar(uid, pilihan):
    """Pilih avatar maskot profil. Return None sukses / pesan_error."""
    p = (pilihan or "").strip().lower()
    if p not in AVATAR_PILIHAN:
        return "pilihan avatar nggak valid 😅"
    with _LOCK:
        d = _load_users()
        u = d.get(uid)
        if not u:
            return "akun tidak ditemukan"
        u["avatar"] = p
        _save_users(d)
    return None
def migrate_guest(old_uid, new_uid):
    """Pindahkan jatah harian, saldo, dan data referral tamu ke akun.

    Dipanggil sekali saat login pertama. Best-effort: kegagalan satu
    bagian tidak menggagalkan login.
    """
    if not old_uid or old_uid == new_uid or not old_uid.startswith("web_"):
        return
    # --- ai_quota.json: jatah harian + saldo
    try:
        import skripsi_ai as sai
        with sai._LOCK:
            d = sai._load()
            users = d.get("users") or {}
            old_u = users.pop(old_uid, None)
            if old_u:
                new_u = users.get(new_uid)
                if new_u and new_u.get("date") == old_u.get("date"):
                    new_u["count"] = max(int(new_u.get("count", 0)),
                                         int(old_u.get("count", 0)))
                elif not new_u:
                    users[new_uid] = old_u
            bal = d.get("ai_balance") or {}
            old_b = int(bal.pop(old_uid, 0) or 0)
            if old_b:
                # akun telegram lama (provider "telegram" / tg_id hasil
                # hubungkan sebelum penghubungan dimatikan) -> saldo tamu
                # pindah ke pool BOT (shared antar platform), bukan ke uid web
                pid = linked_tg_id(new_uid)
                if pid:
                    try:
                        bd = sai._bot_load() or sai._bot_new()
                        bb = bd.setdefault("ai_balance", {})
                        bb[pid] = int(bb.get(pid, 0) or 0) + old_b
                        sai._bot_save(bd)
                    except Exception as e:
                        log.warning("migrasi saldo tamu ke pool bot gagal: %s", e)
                        bal[new_uid] = int(bal.get(new_uid, 0) or 0) + old_b
                else:
                    bal[new_uid] = int(bal.get(new_uid, 0) or 0) + old_b
            d["users"] = users
            d["ai_balance"] = bal
            sai._save(d)
    except Exception as e:
        log.warning("migrasi kuota gagal: %s", e)
    # --- referral.json: hitungan + referred_by + trial
    try:
        import referral as refmod
        with refmod._LOCK:
            d = refmod._load()
            counts = d.get("counts") or {}
            old_c = int(counts.pop(old_uid, 0) or 0)
            if old_c:
                counts[new_uid] = int(counts.get(new_uid, 0) or 0) + old_c
            by = d.get("referred_by") or {}
            if old_uid in by:
                by[new_uid] = by.pop(old_uid)
            claimed = d.get("trial_claimed") or []
            if old_uid in claimed:
                claimed = [x for x in claimed if x != old_uid]
                if new_uid not in claimed:
                    claimed.append(new_uid)
                d["trial_claimed"] = claimed
            d["counts"] = counts
            d["referred_by"] = by
            # --- codes: kepemilikan kode KP- tamu pindah ke akun baru
            refmod._move_code_locked(d, old_uid, new_uid)
            refmod._save(d)
    except Exception as e:
        log.warning("migrasi referral gagal: %s", e)
    # --- referral sync: bila akun baru terhubung Telegram, satukan hitungan
    # referral (web + bot) ke identitas Telegram. Idempoten & fail-safe.
    try:
        import referral as refmod2
        pid = linked_tg_id(new_uid)
        if pid:
            refmod2.merge_referral_to_tg([old_uid, new_uid], pid)
    except Exception as e:
        log.warning("merge referral ke tg gagal: %s", e)


# -------------------------------------------- identitas Telegram lama
# (baca-saja: dipakai referral sync + migrasi saldo tamu untuk akun yang
# dulunya login/hubung via Telegram. Penghubungan BARU sudah dimatikan.)


def linked_tg_id(uid_or_user):
    """Telegram numeric id (str) yang terhubung ke akun web, atau None.

    Mencakup login via Telegram (provider) maupun hasil hubungkan manual
    (field tg_id pada akun username/password).
    """
    try:
        u = uid_or_user if isinstance(uid_or_user, dict) else get_user(uid_or_user)
        if not u:
            return None
        if u.get("provider") == "telegram":
            pid = str(u.get("pid") or "").strip()
            return pid if pid.isdigit() else None
        tid = str(u.get("tg_id") or "").strip()
        return tid if tid.isdigit() else None
    except Exception:
        return None
