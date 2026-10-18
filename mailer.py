"""Email transaksional KhairyPustaka — SMTP (stdlib) atau API Resend (stdlib).

Konfigurasi dari environment (.env):
  SMTP_HOST  (mis. smtp.gmail.com)
  SMTP_PORT  (default 587)
  SMTP_USER  (alamat email pengirim / username SMTP)
  SMTP_PASS  (password / App Password)
  MAIL_FROM  (mis. "KhairyPustaka <nama@gmail.com>")
  BASE_URL   (mis. https://pustaka.khairys.web.id) — untuk link di email

  Jalur Resend (untuk Render free yang memblokir port SMTP keluar):
  RESEND_API_KEY  (kalau terisi, email dikirim lewat https://api.resend.com/emails)
  RESEND_FROM     (pengirim untuk Resend; kalau kosong, pakai MAIL_FROM)
"""
import json
import logging
import os
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage

log = logging.getLogger("kpweb.mailer")


def mail_config():
    return {
        "host": (os.environ.get("SMTP_HOST") or "").strip(),
        "port": int(os.environ.get("SMTP_PORT") or 587),
        "user": (os.environ.get("SMTP_USER") or "").strip(),
        "password": (os.environ.get("SMTP_PASS") or ""),
        "sender": (os.environ.get("MAIL_FROM") or "").strip(),
        "base_url": (os.environ.get("BASE_URL")
                     or "https://pustaka.khairys.web.id").strip().rstrip("/"),
    }


def _resend_config():
    """Konfig jalur Resend. Dipakai kalau RESEND_API_KEY terisi."""
    return {
        "api_key": (os.environ.get("RESEND_API_KEY") or "").strip(),
        "sender": ((os.environ.get("RESEND_FROM")
                    or os.environ.get("MAIL_FROM")) or "").strip(),
    }


def mail_configured():
    c = mail_config()
    r = _resend_config()
    return bool((r["api_key"] and r["sender"])
                or (c["host"] and c["user"] and c["password"]))


def _send_via_resend(to_addr, subject, html, r):
    """Kirim via Resend HTTPS API (tanpa dependency tambahan)."""
    if not r["sender"]:
        return False, "email belum dikonfigurasi admin"
    payload = {
        "from": r["sender"],
        "to": [to_addr],
        "subject": subject,
        "html": html,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=data,
        headers={
            "Authorization": "Bearer " + r["api_key"],
            "Content-Type": "application/json",
            # Wajib: tanpa User-Agent wajar, Cloudflare di depan api.resend.com
            # menolak urllib (HTTP 403 error code 1010, kejadian 2026-10-18).
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        log.warning("Resend gagal kirim ke %s: HTTP %s %s", to_addr, e.code, body)
        return False, "gagal mengirim email, coba lagi nanti"
    except Exception as e:
        log.warning("Resend gagal kirim ke %s: %s", to_addr, e)
        return False, "gagal mengirim email, coba lagi nanti"
    if not (200 <= status < 300):
        log.warning("Resend gagal kirim ke %s: status %s", to_addr, status)
        return False, "gagal mengirim email, coba lagi nanti"
    return True, None


def send_email(to_addr, subject, html):
    """Kirim satu email HTML. Return (True, None) / (False, pesan_error)."""
    to_addr = (to_addr or "").strip()
    if not to_addr or "@" not in to_addr:
        return False, "alamat email tidak valid"
    r = _resend_config()
    if r["api_key"]:
        return _send_via_resend(to_addr, subject, html, r)
    c = mail_config()
    if not (c["host"] and c["user"] and c["password"]):
        return False, "email belum dikonfigurasi admin"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = c["sender"] or c["user"]
    msg["To"] = to_addr
    msg.set_content("Email ini butuh tampilan HTML. Buka lewat browser/HP ya. 🙂")
    msg.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP(c["host"], c["port"], timeout=20) as s:
            s.starttls()
            s.login(c["user"], c["password"])
            s.send_message(msg)
        return True, None
    except Exception as e:
        log.warning("kirim email ke %s gagal: %s", to_addr, e)
        return False, "gagal mengirim email, coba lagi nanti"


def _wrap(judul, isi_html, tombol_teks, tombol_url, catatan):
    return f"""\
<!doctype html><html><body style="margin:0;font-family:sans-serif;background:#f7f3ea">
<div style="max-width:480px;margin:0 auto;padding:32px 20px">
<div style="background:#fffdf7;border-radius:16px;padding:28px 24px;box-shadow:0 2px 12px rgba(0,0,0,.06)">
<h2 style="color:#0f766e;margin:0 0 8px">{judul}</h2>
<div style="color:#334155;font-size:15px;line-height:1.6">{isi_html}</div>
<div style="text-align:center;margin:24px 0">
<a href="{tombol_url}" style="display:inline-block;background:#0f766e;color:#fff;
text-decoration:none;font-weight:bold;padding:13px 30px;border-radius:12px">{tombol_teks}</a>
</div>
<p style="color:#94a3b8;font-size:12px;line-height:1.5">{catatan}</p>
</div>
<p style="text-align:center;color:#94a3b8;font-size:12px;margin-top:16px">
KhairyPustaka — teman seperjuangan skripsianmu 📚</p>
</div></body></html>"""


def send_verify_email(to_addr, nama, token):
    c = mail_config()
    url = f"{c['base_url']}/#/verifikasi?token={token}"
    html = _wrap(
        "Verifikasi email kamu 📧",
        f"Halo <b>{nama}</b>! Satu langkah lagi biar akun KhairyPustaka-mu aktif. "
        "Klik tombol di bawah ini ya:",
        "Verifikasi Email",
        url,
        "Link berlaku 24 jam. Kalau tombolnya nggak bisa diklik, copy-paste link ini ke browser:<br>" + url,
    )
    return send_email(to_addr, "Verifikasi email KhairyPustaka 📧", html)


def send_reset_email(to_addr, nama, token):
    url = f"{c['base_url']}/#/reset?token={token}"
    html = _wrap(
        "Reset password 🔑",
        f"Halo <b>{nama}</b>! Kami terima permintaan reset password untuk akunmu. "
        "Klik tombol di bawah untuk bikin password baru:",
        "Buat Password Baru",
        url,
        "Link berlaku 1 jam. Kalau kamu nggak merasa minta reset, abaikan email ini — "
        "password-mu tetap aman.<br><br>Link:<br>" + url,
    )
    return send_email(to_addr, "Reset password KhairyPustaka 🔑", html)


def send_welcome_email(to_addr, nama):
    """Email sambutan untuk member baru (dikirim sekali, tepat setelah
    verifikasi email berhasil). Kegagalan kirim tidak boleh menggagalkan
    verifikasi; pemanggil membungkusnya dengan try/except."""
    c = mail_config()
    url = c["base_url"] + "/"
    html = _wrap(
        "Selamat datang di KhairyPustaka 🎓",
        f"Halo <b>{nama}</b>! Akunmu sudah aktif. Sekarang kamu bisa memakai "
        "semua fitur KhairyPustaka buat nemenin perjalanan skripsimu:<br><br>"
        "🔎 <b>Cari Jurnal</b> dari 4 sumber sekaligus<br>"
        "📝 <b>Buat Sitasi</b> 6 gaya dalam hitungan detik<br>"
        "📊 <b>Panduan Statistik</b> interaktif buat nentuin uji yang tepat<br>"
        "🤖 <b>AI Studio</b> dengan 10 generator naskah, termasuk "
        "<b>Simulasi Sidang</b> buat latihan sebelum hari H<br><br>"
        "Fitur non-AI bisa kamu pakai sepuasnya, dan jatah AI gratis "
        "10x per hari selalu terisi ulang tiap tengah malam. Satu tips: "
        "pasang KhairyPustaka ke layar utama HP lewat menu Akun, biar "
        "bukanya secepat aplikasi biasa.",
        "Mulai Pakai KhairyPustaka",
        url,
        "Email ini dikirim karena kamu baru saja mendaftar dan "
        "memverifikasi akun di KhairyPustaka. Kalau kamu tidak merasa "
        "mendaftar, abaikan saja email ini.",
    )
    return send_email(to_addr, "Selamat datang di KhairyPustaka 🎓", html)


def send_change_email(to_addr, nama, token):
    """Email konfirmasi ke alamat BARU saat user minta ganti email."""
    c = mail_config()
    url = f"{c['base_url']}/#/konfirmasi-email?token={token}"
    html = _wrap(
        "Konfirmasi email barumu 📧",
        f"Halo <b>{nama}</b>! Kamu minta ganti email akun KhairyPustaka ke alamat ini. "
        "Klik tombol di bawah buat konfirmasi ya:",
        "Konfirmasi Email Baru",
        url,
        "Link berlaku 24 jam. Email lamamu tetap aktif sampai kamu klik link ini. "
        "Kalau kamu nggak merasa minta ganti email, abaikan saja email ini.<br><br>Link:<br>" + url,
    )
    return send_email(to_addr, "Konfirmasi email baru KhairyPustaka 📧", html)
