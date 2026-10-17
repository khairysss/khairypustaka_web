"""Top-up Jatah AI KhairyPustaka: pembayaran QRIS via WijayaPay.

Alur:
1. User tekan "Tambah Jatah AI" -> create_invoice() -> bot kirim QR QRIS.
2. _topup_loop() di bot.py mengecek status via check_invoice() tiap 10 detik.
3. Status paid -> skripsi_ai.ai_topup(chat_id, TOPUP_USES) -> user dinotifikasi.

Paket: Rp5.000 = 25x pakai AI (saldo tidak hangus).

Kredensial: WIJAYAPAY_MERCHANT (code_merchant) + WIJAYAPAY_API_KEY (api_key)
di environment. Docs: https://docs.wijayapay.com/
"""
import base64
import hashlib
import io
import json
import logging
import os
import re
import secrets
import threading
import time

import httpx

import dbstore  # Postgres (DATABASE_URL) atau file lokal, lihat dbstore.py

log = logging.getLogger("jurnalbot")

TOPUP_PRICE = 5000   # Rp per paket dasar (= minimal)
TOPUP_USES = 25      # x pakai AI per paket dasar (tidak hangus)
TOPUP_MIN_USES = 25       # minimal top-up bebas: 25x pakai AI
TOPUP_PRICE_PER_USE = 200 # Rp200 per 1x (25x = Rp5.000)
PENDING_TTL = 3600     # invoice pending dibuang setelah 1 jam
PENDING_PATH = "data/premium_pending.json"

WP_BASE = "https://gateway.wijayapay.com/api"
WP_MERCHANT = os.environ.get("WIJAYAPAY_MERCHANT", "").strip()
WP_API_KEY = os.environ.get("WIJAYAPAY_API_KEY", "").strip()


def pay_enabled():
    return bool(WP_MERCHANT and WP_API_KEY)


# ---------------------------------------------------------- pending store
_PENDING_LOCK = threading.Lock()
# S-7 (audit 2026-10-17) PERINGATAN SCALE-OUT: lock ini (_PENDING_LOCK) hanya melindungi
# read-modify-write di DALAM satu proses. Aman hari ini (Render free =
# 1 instance = 1 proses). Bila suatu hari scale ke >1 instance/proses,
# PINDAHKAN operasi kritis ke primitif atomik Postgres (UPDATE ... WHERE
# kondisional / advisory lock) SEBELUM menambah instance — kalau tidak,
# double-redeem / kuota terlampaui / double-claim bisa terjadi.



def _load_pending():
    d = dbstore.load_json(PENDING_PATH, {})
    return d if isinstance(d, dict) else {}


def _save_pending(d):
    dbstore.save_json(PENDING_PATH, d)


def pending_add(ref_id, chat_id, username=None, uses=None, amount=None):
    d = _load_pending()
    d[ref_id] = {"chat_id": chat_id, "ts": time.time(),
                 "username": (username or "").lstrip("@")}
    if uses is not None:
        d[ref_id]["uses"] = int(uses)
    if amount is not None:
        d[ref_id]["amount"] = int(amount)
    # buang yang kedaluwarsa sekalian
    for k in [k for k, v in d.items()
              if time.time() - v.get("ts", 0) > PENDING_TTL]:
        d.pop(k, None)
    _save_pending(d)


def pending_set_msg(ref_id, message_id):
    """Simpan message_id pesan QR agar bisa dihapus saat lunas."""
    d = _load_pending()
    if ref_id in d:
        d[ref_id]["msg_id"] = message_id
        _save_pending(d)


def pending_all():
    return _load_pending()


def pending_for_user(chat_id):
    """ref_id invoice pending yang masih berlaku milik chat_id, atau None.

    Dipakai untuk dedup: user yang sudah punya kode bayar aktif tidak
    dibuatkan invoice baru (mencegah bayar ganda & polusi dashboard).
    """
    now = time.time()
    for ref_id, p in _load_pending().items():
        if str(p.get("chat_id")) == str(chat_id) \
                and now - p.get("ts", 0) <= PENDING_TTL:
            return ref_id
    return None


def pending_drop(ref_id):
    d = _load_pending()
    if d.pop(ref_id, None) is not None:
        _save_pending(d)


# S-6 (audit 2026-10-17): plafon pembuatan invoice per akun per hari.
# Disimpan di dokumen pending yang sama di bawah kunci "_creates":
# {"<uid>": {"2026-10-17": 3}}. Disapu ke 2 hari terakhir tiap tulis.
_CREATES_KEY = "_creates"
_INVOICE_HARIAN_MAX = 5


def _today():
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime
        return datetime.now(ZoneInfo("Asia/Makassar")).date().isoformat()
    except Exception:
        from datetime import date
        return date.today().isoformat()


def creates_hari_ini(chat_id):
    """Jumlah invoice yang dibuat uid ini hari ini (0 bila tak ada)."""
    try:
        return int((_load_pending().get(_CREATES_KEY, {})
                    .get(str(chat_id), {}).get(_today(), 0) or 0)
                   )
    except Exception:
        return 0


def catat_create(chat_id):
    """Catat satu pembuatan invoice. Return jumlah hari ini sesudah dicatat."""
    today = _today()
    uid = str(chat_id)
    with _PENDING_LOCK:
        d = _load_pending()
        c = d.get(_CREATES_KEY)
        if not isinstance(c, dict):
            c = {}
        u = c.get(uid)
        if not isinstance(u, dict):
            u = {}
        n = int(u.get(today, 0) or 0) + 1
        u[today] = n
        # sapu hari lama (simpan 2 hari terakhir saja)
        for k in [k for k in u.keys() if k != today]:
            try:
                from datetime import date as _date
                if (_date.fromisoformat(today) -
                        _date.fromisoformat(str(k))).days > 1:
                    del u[k]
            except ValueError:
                del u[k]
        if not u:
            c.pop(uid, None)
        else:
            c[uid] = u
        d[_CREATES_KEY] = c
        _save_pending(d)
    return n


def pending_claim(ref_id):
    """Klaim atomik: True hanya untuk pemanggil pertama.

    Mencegah double-credit saat 2 tab mem-poll /api/topup/status bersamaan:
    pemanggil pertama menandai entry 'claimed', pemanggil berikut gagal.
    """
    with _PENDING_LOCK:
        d = _load_pending()
        p = d.get(ref_id)
        if not p or p.get("claimed"):
            return False
        p["claimed"] = True
        d[ref_id] = p
        _save_pending(d)
        return True


def pending_set(ref_id, **kw):
    """Tambah field ke entry pending (mis. qr_b64 untuk resume)."""
    with _PENDING_LOCK:
        d = _load_pending()
        if ref_id in d:
            d[ref_id].update(kw)
            _save_pending(d)


# ------------------------------------------------------------------ API
def _auth():
    return {"code_merchant": WP_MERCHANT, "api_key": WP_API_KEY}


def _signature(ref_id):
    """X-Signature: md5(code_merchant + api_key + ref_id), sesuai docs."""
    raw = f"{WP_MERCHANT}{WP_API_KEY}{ref_id}"
    return hashlib.md5(raw.encode()).hexdigest()


async def create_invoice(amount, description, chat_id, username=None):
    """Buat transaksi QRIS. Kembalikan (ref_id, qr_image_b64, qris_string,
    final_amount) atau None. ref_id dipakai untuk polling status."""
    if not pay_enabled():
        return None
    uname = (username or "").strip().lstrip("@") or f"user{chat_id}"
    local = re.sub(r"[^a-z0-9._-]", "", uname.lower()) or f"user{chat_id}"
    ref_id = f"KHP-{chat_id}-{int(time.time() * 1000)}-{secrets.token_hex(2)}"
    # Gross-up: pelanggan menanggung fee, merchant terima bersih = amount.
    pct, fix = await _qris_fee_params()
    nominal = _gross_up(int(amount), pct, fix)
    body = {
        **_auth(),
        "code_payment": "QRIS",   # kode channel pembayaran (wajib di API)
        "ref_id": ref_id,
        "nominal": nominal,
        "customer_name": uname[:60],
        # wajib di API; placeholder karena bot hanya tahu Telegram user
        "customer_email": f"{local}@khairypustaka.id",
        "customer_phone": "-",
        "expired": str(PENDING_TTL),
    }
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(WP_BASE + "/transaction/create", json=body,
                             headers={"Content-Type": "application/json",
                                      "Accept": "application/json",
                                      "X-Signature": _signature(ref_id)})
    except Exception as e:  # noqa: BLE001
        log.error("wijayapay create gagal: %s", e)
        return None
    if r.status_code != 200:
        log.error("wijayapay create -> HTTP %s: %s",
                  r.status_code, r.text[:250])
        return None
    try:
        payload = r.json()
    except Exception:
        log.error("wijayapay create: respons bukan JSON")
        return None
    if not (payload.get("success") or payload.get("status")):
        log.error("wijayapay create ditolak: %s", str(payload)[:300])
        return None
    data = payload.get("data") or {}
    qr_string = data.get("qr_string") or ""
    # qr_image WijayaPay berupa URL gambar (bukan base64) -> diunduh di bot
    qr_image = data.get("qr_image") or ""
    try:
        # fallback ke nominal (gross) supaya caption konsisten dengan QR
        final = int(data.get("total_bayar") or nominal)
    except (TypeError, ValueError):
        final = int(nominal)
    log.info("wijayapay invoice dibuat (ref %s, nominal %s, bayar %s)",
             ref_id, nominal, final)
    return (ref_id, qr_image, qr_string, final)


async def check_invoice(ref_id):
    """Cek status transaksi. Kembalikan dict dengan key 'status' ('paid'
    bila lunas) atau None. API memakai field 'status_pembayaran'."""
    if not pay_enabled():
        return None
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.get(WP_BASE + "/get-status",
                            params={**_auth(), "ref_id": ref_id})
        if r.status_code != 200:
            return None
        payload = r.json()
    except Exception as e:  # noqa: BLE001
        log.error("wijayapay check gagal: %s", e)
        return None
    data = payload.get("data") or {}
    status = str(
        payload.get("status_pembayaran")
        or data.get("status_pembayaran")
        or data.get("status")
        or payload.get("status")
        or ""
    ).lower()
    out = dict(data)
    # nominal kadang dikirim di top-level, bukan di dalam "data"
    for k in ("total_bayar", "total_diterima", "nominal"):
        if k not in out and k in payload:
            out[k] = payload[k]
    out["status"] = status
    return out


async def list_payment_methods():
    """Kembalikan info channel pembayaran yang aktif (diagnostik admin)."""
    if not pay_enabled():
        return None
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.get(WP_BASE + "/get-payment", params=_auth())
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:  # noqa: BLE001
        log.error("wijayapay methods gagal: %s", e)
        return None


async def _qris_fee_params():
    """Ambil (fee_percent, fee_amount) channel QRIS dari /api/get-payment.
    Fallback ke 0,7% + Rp100 bila gagal (sesuai data transaksi nyata)."""
    try:
        res = await list_payment_methods()
        for ch in ((res or {}).get("data") or []):
            if str(ch.get("code", "")).upper() == "QRIS":
                pct = float(ch.get("fee_percent") or 0) / 100.0
                fix = float(ch.get("fee_amount") or 0)
                return pct, fix
    except Exception as e:  # noqa: BLE001
        log.warning("baca fee QRIS gagal: %s", e)
    return 0.007, 100.0


def _gross_up(net, pct, fix):
    """Nominal terkecil supaya setelah dipotong fee, bersih diterima >= net.

    fee(nominal) = round_half_up(pct*nominal + fix), terverifikasi dari
    transaksi nyata: nominal 7500 -> fee 153.

    Guard: pct dijepit di bawah 100% dan iterasi dibatasi supaya respons
    API yang rusak tidak bisa membuat bot hang (infinite loop).
    """
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        pct = 0.007
    try:
        fix = float(fix)
    except (TypeError, ValueError):
        fix = 100.0
    if not 0 <= pct < 1:
        log.warning("fee_percent aneh (%s) -> pakai fallback 0,7%%", pct)
        pct, fix = 0.007, 100.0
    if fix < 0:
        fix = 0.0

    def fee(n):
        return int(pct * n + fix + 0.5)
    n = int(net + fix + pct * net) + 2
    for _ in range(10000):
        if n - fee(n) >= net:
            break
        n += 1
    else:
        log.warning("gross-up mentok 10000 iterasi; pakai %s", n)
    while n - 1 - fee(n - 1) >= net and n > net:
        n -= 1
    return n


# ------------------------------------------------------------------ QR
def qr_png_bytes(qris_string):
    """Render string QRIS menjadi PNG (fallback bila qr_image kosong)."""
    if not qris_string:
        return None
    try:
        import qrcode
        qr = qrcode.QRCode(box_size=8, border=2)
        qr.add_data(qris_string)
        qr.make(fit=True)
        buf = io.BytesIO()
        qr.make_image().save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        log.warning("render QR gagal: %s", e)
        return None


def qr_image_to_png(data_uri):
    """Decode 'data:image/png;base64,...' dari WijayaPay menjadi bytes PNG."""
    if not data_uri or "," not in data_uri:
        return None
    try:
        return base64.b64decode(data_uri.split(",", 1)[1])
    except Exception as e:
        log.warning("decode qr_image gagal: %s", e)
        return None


async def qr_png_from_api(qr_image_field, qr_string):
    """Ambil PNG QR dari field qr_image WijayaPay.

    qr_image bisa berupa URL gambar (diunduh) atau data URI base64
    (di-decode). Fallback: render qr_string secara lokal.
    """
    if qr_image_field:
        if qr_image_field.startswith("http"):
            try:
                async with httpx.AsyncClient(timeout=25) as c:
                    r = await c.get(qr_image_field)
                ct = r.headers.get("content-type", "")
                if r.status_code == 200 and ct.startswith("image"):
                    return r.content
                log.warning("qr_image URL bukan gambar: %s %s",
                            r.status_code, ct[:40])
            except Exception as e:  # noqa: BLE001
                log.warning("unduh qr_image gagal: %s", e)
        else:
            png = qr_image_to_png(qr_image_field)
            if png:
                return png
    return qr_png_bytes(qr_string) if qr_string else None
