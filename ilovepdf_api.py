"""Klien minimal iLoveAPI (iLovePDF) via urllib.

Dipakai KHUSUS untuk 2 tool berat di halaman Convert File:
- Word ke PDF  -> tool "officepdf"
- PDF ke Word  -> tool "pdfword"

Alur resmi (developer.ilovepdf.com): auth -> start task -> upload file ->
process -> download hasil. Konversi berat dikerjakan server iLovePDF,
server kita hanya meneruskan file.

CATATAN VERIFIKASI 2026-10-19: dokumentasi resmi developer.ilovepdf.com
serta pustaka resmi ilovepdf-python TIDAK mencantumkan tool "pdfword"
dalam daftar tool publik API (yang ada: officepdf untuk Office ke PDF).
Nama "pdfword" dipakai sesuai desain yang disepakati; bila saat kunci API
tiba server menjawab tool tidak dikenal, cukup ubah SATU konstanta
_PDFWORD_TOOL di bawah, tanpa menyentuh logika lain.

Kunci dibaca dari env ILOVEPDF_PUBLIC_KEY + ILOVEPDF_SECRET_KEY dan TIDAK
PERNAH ditulis ke log / respons / pesan error.
"""

import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("kpweb")

_WORDPDF_TOOL = "officepdf"
_PDFWORD_TOOL = "pdfword"  # verifikasi ulang saat kunci API tiba (lihat catatan modul)

# kode alat web -> nama tool iLoveAPI
ALAT_KE_TOOL = {
    "word_pdf": _WORDPDF_TOOL,
    "pdf_word": _PDFWORD_TOOL,
}

_HASIL_EXT_MIME = {
    "word_pdf": ("pdf", "application/pdf"),
    "pdf_word": ("docx", "application/vnd.openxmlformats-officedocument"
                        ".wordprocessingml.document"),
}

_API = "https://api.ilovepdf.com/v1"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0.0.0 Safari/537.36")

_token_lock = threading.Lock()
_token_cache = {"token": None, "kedaluwarsa": 0}


class ILoveError(ValueError):
    """Error iLoveAPI yang pesannya aman ditampilkan ke pengguna."""


class ToolTidakDidukung(ILoveError):
    """Tool yang diminta tidak dikenal / tidak disediakan iLoveAPI.

    Dipakai app.py untuk mengalihkan PDF ke Word ke mode lokal, karena
    daftar tool resmi iLoveAPI memang tidak memuat PDF ke Word."""


def aktif():
    """True bila kedua kunci API terisi di env."""
    return bool(os.environ.get("ILOVEPDF_PUBLIC_KEY", "").strip()
                and os.environ.get("ILOVEPDF_SECRET_KEY", "").strip())


def _panggil(url, data=None, token=None, timeout=20, jenis="application/json"):
    """Satu panggilan HTTP. Kembalikan body bytes. Lempar ILoveError."""
    kepala = {"User-Agent": _UA, "Accept": "application/json"}
    if token:
        kepala["Authorization"] = "Bearer " + token
    if data is not None:
        kepala["Content-Type"] = jenis
    req = urllib.request.Request(url, data=data, headers=kepala,
                                 method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        # Diagnostik audit 2026-10-19: catat metode + host/path yang gagal
        # (tanpa kredensial) supaya langkah alur yang ditolak provider
        # terlihat persis di log server.
        from urllib.parse import urlparse
        _u = urlparse(url)
        log.warning("iLoveAPI langkah gagal: %s %s%s",
                    req.method, _u.netloc, _u.path)
        pesan = _pesan_http(e)
        if pesan.startswith("tool convert ini belum didukung iLoveAPI"):
            raise ToolTidakDidukung(pesan)
        raise ILoveError(pesan)
    except TimeoutError:
        raise ILoveError("iLovePDF lama merespons, coba lagi nanti ya")
    except Exception as e:
        log.warning("iLoveAPI gagal: %s", type(e).__name__)
        raise ILoveError("tidak bisa menghubungi iLovePDF, coba lagi nanti ya")


def _pesan_http(e):
    """Petakan HTTPError jadi kalimat ramah, tanpa membocorkan apa pun."""
    isi = ""
    try:
        isi = e.read().decode("utf-8", "replace")[:500]
    except Exception:
        pass
    pesan_api = ""
    try:
        _d = json.loads(isi)
        # Body error asli berbentuk {"error": {"message": ...}} (tersarang),
        # sebagian respons lain datar {"message": ...} — baca dua-duanya.
        pesan_api = ((_d.get("message") or "")
                     or ((_d.get("error") or {}).get("message") or "")
                     ).strip()
    except Exception:
        pass
    log.warning("iLoveAPI HTTP %s: %s", e.code, pesan_api or isi[:120])
    if e.code == 401:
        return ("kunci API iLovePDF ditolak, periksa kembali kunci publik "
                "dan kunci rahasianya")
    if e.code == 402:
        return ("kuota file iLoveAPI bulan ini habis, coba lagi bulan depan "
                "ya")
    if e.code == 404 and "tool" in (pesan_api + isi).lower():
        return ("tool convert ini belum didukung iLoveAPI, kami sedang "
                "menyesuaikannya")
    if e.code == 429:
        return ("iLovePDF lagi ramai, tunggu sebentar lalu coba lagi ya")
    if 500 <= e.code < 600:
        return "server iLovePDF lagi bermasalah, coba lagi nanti ya"
    if pesan_api:
        return "iLovePDF: " + pesan_api[:160]
    return "iLovePDF menolak permintaan ini, coba lagi nanti ya"


def _token():
    """Ambil token JWT, pakai cache sampai mendekati kedaluwarsa."""
    kini = time.time()
    with _token_lock:
        if (_token_cache["token"]
                and kini < _token_cache["kedaluwarsa"] - 600):
            return _token_cache["token"]
    publik = os.environ.get("ILOVEPDF_PUBLIC_KEY", "").strip()
    rahasia = os.environ.get("ILOVEPDF_SECRET_KEY", "").strip()
    if not publik or not rahasia:
        raise ILoveError("kunci API iLovePDF belum dipasang")
    badan = json.dumps({"public_key": publik,
                        "secret_key": rahasia}).encode("utf-8")
    try:
        mentah = _panggil(_API + "/auth", data=badan, timeout=15)
        token = json.loads(mentah.decode("utf-8"))["token"]
    except (KeyError, ValueError):
        raise ILoveError("respons autentikasi iLovePDF tidak dimengerti")
    with _token_lock:
        _token_cache["token"] = token
        _token_cache["kedaluwarsa"] = kini + 100 * 60  # aman di bawah umur JWT
    return token


def _mulai(token, tool):
    # Path yang benar: GET /v1/start/<tool> (BUKAN /v1/process/start/... —
    # path lama menjawab 404 "This resource can't be found", terbukti dari
    # log produksi 2026-10-19 05:56 + pola klien iLoveAPI yang berjalan).
    try:
        mentah = _panggil(f"{_API}/start/{tool}", token=token, timeout=15)
    except ILoveError:
        # Tool "pdfword" memang TIDAK ADA di daftar tool resmi iLoveAPI;
        # kegagalan start untuk tool ini = tidak didukung -> app.py
        # mengalihkan job ke mode lokal teks rapi.
        if tool == _PDFWORD_TOOL:
            raise ToolTidakDidukung(
                "tool convert ini belum didukung iLoveAPI")
        raise
    try:
        d = json.loads(mentah.decode("utf-8"))
        return d["server"], d["task"]
    except (KeyError, ValueError):
        raise ILoveError("gagal memulai task di iLovePDF")


def _multipart(task, nama_file, raw):
    """Bangun body multipart manual (tanpa dependency tambahan)."""
    batas = "----kp" + secrets.token_hex(8)
    aman = "".join(c for c in (nama_file or "file")
                   if c not in '"\r\n') or "file"
    bagian = [
        (f"--{batas}\r\n"
         f'Content-Disposition: form-data; name="task"\r\n\r\n'
         f"{task}\r\n").encode("utf-8"),
        (f"--{batas}\r\n"
         f'Content-Disposition: form-data; name="file"; filename="{aman}"\r\n'
         "Content-Type: application/octet-stream\r\n\r\n").encode("utf-8"),
        raw,
        f"\r\n--{batas}--\r\n".encode("utf-8"),
    ]
    return b"".join(bagian), batas


def _unggah(server, token, task, nama_file, raw):
    badan, batas = _multipart(task, nama_file, raw)
    mentah = _panggil(f"https://{server}/v1/upload", data=badan, token=token,
                      timeout=120,
                      jenis=f"multipart/form-data; boundary={batas}")
    try:
        return json.loads(mentah.decode("utf-8"))["server_filename"]
    except (KeyError, ValueError):
        raise ILoveError("gagal mengunggah file ke iLovePDF")


def _proses(server, token, task, tool, server_filename, nama_file):
    badan = json.dumps({
        "task": task,
        "tool": tool,
        "files": [{"server_filename": server_filename,
                   "filename": nama_file}],
    }).encode("utf-8")
    # Tanpa webhook: koneksi ditahan sampai proses selesai di server mereka.
    _panggil(f"https://{server}/v1/process", data=badan, token=token,
            timeout=300)


def _unduh(server, token, task):
    return _panggil(f"https://{server}/v1/download/{task}", token=token,
                    timeout=120)


def convert(raw, nama_file, kode_alat, tahap=None):
    """Convert satu file via iLoveAPI.

    tahap: callable opsional dipanggil dengan teks tahap nyata untuk
    transparansi ("Menghubungi iLovePDF", ...).
    Kembalikan (hasil_bytes, nama_hasil, mime). Lempar ILoveError.
    """
    tool = ALAT_KE_TOOL.get(kode_alat)
    if not tool:
        raise ILoveError("alat convert tidak dikenal")
    if not aktif():
        raise ILoveError("kunci API iLovePDF belum dipasang")

    def kabar(teks):
        try:
            if tahap:
                tahap(teks)
        except Exception:
            pass

    kabar("Menghubungi iLovePDF")
    token = _token()
    server, task = _mulai(token, tool)
    kabar("Mengirim file ke iLovePDF")
    server_filename = _unggah(server, token, task, nama_file, raw)
    kabar("Sedang dikonversi di server iLovePDF")
    _proses(server, token, task, tool, server_filename, nama_file)
    kabar("Mengunduh hasil")
    hasil = _unduh(server, token, task)
    if not hasil:
        raise ILoveError("hasil dari iLovePDF kosong, coba lagi ya")
    ext, mime = _HASIL_EXT_MIME[kode_alat]
    dasar = (nama_file or "dokumen").rsplit(".", 1)[0].strip() or "dokumen"
    return hasil, f"{dasar}.{ext}", mime
