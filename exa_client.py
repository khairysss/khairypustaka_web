"""Klien API Exa untuk fitur Cari Jurnal Mendalam (KhairyPustaka).

Jalur yang dipakai adalah jalur yang TERBUKTI di Fase 0 (2026-11-10,
lihat your_files/analisis-exa-premium/laporan-fase0-exa.md):

- Cari Jurnal    : POST /search, type neural, category research-paper,
                   teks sampai 4.000 karakter per hasil. Biaya $0,004.
- Jawaban Cepat  : POST /answer dengan teks sumber. Biaya $0,005.
- Bedah Mendalam : POST /search, type deep + outputSchema terstruktur
                   (ringkasan, faktor_penyebab, dampak, rekomendasi)
                   plus grounding sitasi per bidang. Biaya $0,012.
- Laporan Riset  : POST /agent/runs effort high, lalu polling
                   GET /agent/runs/{id} sampai completed/failed.
                   Biaya $0,50 flat, sekitar 3 menit.

API lama /research/v1/tasks SUDAH PENSIUN (HTTP 410) — jangan dipakai.
Jalur /agent lama juga 404; yang benar /agent/runs.

Kredensial:
- Produksi: environment EXA_API_KEY (header x-api-key ke api.exa.ai).
  Sejak v81 bisa ada kunci cadangan berurutan EXA_API_KEY_2,
  EXA_API_KEY_3, ... Modul exa_mendalam menentukan kunci aktifnya
  (rotasi lunak berdasar perkiraan sisa); modul ini memakai kunci itu
  untuk tiap panggilan dan melakukan FAILOVER KERAS: bila API
  menjawab saldo habis, kunci itu ditandai habis di exa_mendalam,
  panggilan yang sama diulang SATU kali dengan kunci berikutnya.
- Lingkungan Muse lokal (uji): bila tidak ada kunci env sama sekali,
  klien memakai pola konektor custom.exa yang sama dengan helper
  resminya, yaitu surrogate dynamic_credentials yang ditempel ke
  request; nilai kunci asli tidak pernah muncul di kode, berkas,
  atau log modul ini.
Redirect pada request terautentikasi selalu ditolak.
"""
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import quote

log = logging.getLogger("kpweb.exa")

BASE = "https://api.exa.ai"
_HOST = "api.exa.ai"


class ExaError(Exception):
    """Galat panggilan Exa. `status` = kode HTTP bila ada."""

    def __init__(self, pesan, status=None):
        super().__init__(pesan)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a):
        raise ExaError("redirect ditolak pada request Exa terautentikasi")


# Nomor kunci (1-based) yang melayani panggilan sukses terakhir.
# Dibaca exa_mendalam.belanja_catat untuk mencatat belanja per kunci.
KUNCI_TERAKHIR = None


def kunci_terakhir():
    return KUNCI_TERAKHIR


def _kunci_env(nomor):
    """Nilai kunci nomor tertentu dari env ('' bila tidak terpasang)."""
    if not nomor or int(nomor) <= 1:
        return (os.environ.get("EXA_API_KEY") or "").strip()
    return (os.environ.get(f"EXA_API_KEY_{int(nomor)}") or "").strip()


def _modul_mendalam():
    """Modul exa_mendalam (penjaga rotasi kunci), diimpor malas;
    None bila tidak tersedia supaya klien tetap jalan sendiri."""
    try:
        import exa_mendalam
        return exa_mendalam
    except Exception:
        return None


_POLA_SALDO_HABIS = ("insufficient", "credit", "balance", "quota",
                     "exceeded")


def _galat_saldo_habis(status, teks):
    """Deteksi galat saldo habis dari respons API: HTTP 402 selalu
    dianggap habis; galat 4xx lain hanya bila teks galatnya memuat
    pola saldo/kredit/kuota."""
    if status == 402:
        return True
    if status and 400 <= int(status) < 500:
        t = (teks or "").lower()
        return any(p in t for p in _POLA_SALDO_HABIS)
    return False


def _siapkan_kredensial(req, key=None):
    """Tempel kredensial ke request: kunci aktif yang diberikan (atau
    env EXA_API_KEY) dulu, lalu surrogate konektor custom.exa (pola
    helper resmi) untuk uji lokal."""
    key = (key or "").strip() \
        or (os.environ.get("EXA_API_KEY") or "").strip()
    if key:
        req.add_header("x-api-key", key)
        return
    try:
        p = "/opt/muse/skills/custom-connector-setup/bin"
        if p not in sys.path:
            sys.path.insert(0, p)
        from dynamic_credentials import add_surrogate_to_request
        add_surrogate_to_request(req, "custom.exa", allowed_hosts=[_HOST])
        return
    except Exception as e:
        raise ExaError(
            "kredensial Exa belum tersedia: pasang EXA_API_KEY di "
            "environment server, atau jalankan di lingkungan Muse "
            f"dengan konektor custom.exa aktif ({e})")


def _call(method, path, payload=None, timeout=120):
    return _call_sekali(method, path, payload, timeout,
                        boleh_failover=True)


def _call_sekali(method, path, payload, timeout, boleh_failover):
    """Satu percobaan panggilan API dengan kunci aktif saat ini.

    Failover keras: bila galatnya galat saldo habis DAN masih boleh,
    kunci yang gagal ditandai habis lewat exa_mendalam, lalu panggilan
    yang sama diulang SATU kali dengan kunci berikutnya. Pengecualian
    hasil percobaan kedua dibiarkan naik apa adanya."""
    global KUNCI_TERAKHIR
    em = _modul_mendalam()
    nomor = None
    if em is not None:
        try:
            nomor = em.kunci_aktif()
        except Exception:
            nomor = None
    key = _kunci_env(nomor) if nomor else ""
    url = BASE + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    _siapkan_kredensial(req, key)
    try:
        with urllib.request.build_opener(_NoRedirect()).open(
                req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")[:2000]
        try:
            body = json.loads(raw)
        except Exception:
            body = {"raw": raw}
        tag = ""
        if isinstance(body, dict):
            tag = str((body.get("error") or {}).get("tag")
                      if isinstance(body.get("error"), dict)
                      else body.get("error") or "")
        err = ExaError(f"Exa HTTP {e.code} {tag}".strip(), status=e.code)
        err.saldo_habis = _galat_saldo_habis(e.code, raw)
        if boleh_failover and nomor and err.saldo_habis \
                and em is not None:
            pengganti = None
            try:
                pengganti = em.kunci_tandai_habis(int(nomor))
            except Exception:
                pengganti = None
            if pengganti and int(pengganti) != int(nomor):
                log.warning(
                    "failover kunci: kunci %s dinyatakan habis "
                    "(HTTP %s), panggilan %s %s diulang dengan "
                    "kunci %s", nomor, e.code, method, path, pengganti)
                return _call_sekali(method, path, payload, timeout,
                                    boleh_failover=False)
        raise err
    except ExaError:
        raise
    except Exception as e:
        raise ExaError(f"Exa tidak terjangkau: {type(e).__name__}")
    if nomor:
        KUNCI_TERAKHIR = int(nomor)
    try:
        return json.loads(raw)
    except Exception:
        raise ExaError("respons Exa bukan JSON")


def biaya(body):
    """Normalisasi costDollars: bisa angka atau dict {total: ...}."""
    c = (body or {}).get("costDollars")
    if isinstance(c, dict):
        try:
            return float(c.get("total") or 0)
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(c or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- mode 1
def cari_jurnal(query, num=10, kategori="research-paper",
                start_year=None, end_year=None, domains=None,
                isi_penuh=True):
    """Pencarian neural + teks 4.000 karakter per hasil.

    kategori None = semua sumber (tanpa filter kategori Exa). Bila
    isi_penuh False, yang diminta hanya highlights (label akses hasil
    akan turun sendiri karena teks tidak terambil).
    """
    payload = {"query": query, "numResults": int(num), "type": "neural"}
    if kategori:
        payload["category"] = kategori
    if isi_penuh:
        payload["contents"] = {"text": {"maxCharacters": 4000}}
    else:
        payload["contents"] = {"highlights": True}
    if start_year:
        payload["startPublishedDate"] = f"{int(start_year)}-01-01T00:00:00.000Z"
    if end_year:
        payload["endPublishedDate"] = f"{int(end_year)}-12-31T23:59:59.999Z"
    if domains:
        payload["includeDomains"] = list(domains)
    return _call("POST", "/search", payload, timeout=120)


class DoiTidakTerdaftar(ExaError):
    """DOI titik mulai tidak ada di register DOI global, jadi paper
    sumbernya tidak bisa dikenali. Pesannya aman ditampilkan ke
    member apa adanya."""


_DOI_CARI = re.compile(r"10\.\d{4,9}/[^\s\"<>\)\]]+")


def doi_dari_teks(teks):
    """Ekstrak DOI pertama dari teks/URL, '' bila tidak ada."""
    m = _DOI_CARI.search(teks or "")
    return m.group(0).rstrip(".,;") if m else ""


def doi_terdaftar(doi):
    """Cek register DOI global (API handle doi.org).

    True = terdaftar, False = pasti tidak terdaftar, None = tidak
    bisa dipastikan (jaringan/API/handle asing) dan pemanggil wajib
    fail-open. Tanpa cek ini, findSimilar atas DOI yang tidak ada
    mengembalikan halaman acak berskor tinggi yang tampak meyakinkan
    padahal sumbernya tidak pernah dikenali (temuan 2026-11-11: DOI
    10.14710/jpki.9.1.2024.41-52 tidak terdaftar, doi.org, Crossref,
    dan OpenAlex sama sama 404, tetapi findSimilar mengembalikan 9
    "paper serupa" berskor 0,95)."""
    if not doi:
        return None
    url = "https://doi.org/api/handles/" + quote(doi, safe="/")
    req = urllib.request.Request(
        url, headers={"Accept": "application/json",
                      "User-Agent": "KhairyPustaka/1.0"})
    raw = None
    try:
        with urllib.request.build_opener(_NoRedirect()).open(
                req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            return None
    except Exception:
        return None
    try:
        rc = json.loads(raw).get("responseCode")
    except Exception:
        return None
    if rc == 1:
        return True
    if rc == 100:
        return False
    return None


_JUDUL_DOI_CACHE = {}


def judul_sumber_doi(doi):
    """Judul resmi paper pemilik DOI ini dari register DOI
    (API Crossref, agen registrasi DOI jurnal), '' bila gagal.

    Dipakai menyaring paper sumber dari daftar paper serupanya
    sendiri lewat kecocokan judul (audit live v77 T3: saringan DOI
    di URL/teks lolos karena DOI tidak tercetak di awal teks
    halaman artikel). Fail-safe total: kegagalan apa pun
    mengembalikan '' dan pemanggil melanjutkan tanpa saringan
    judul, tidak pernah mengarang judul."""
    doi = (doi or "").strip()
    if not doi:
        return ""
    if doi in _JUDUL_DOI_CACHE:
        return _JUDUL_DOI_CACHE[doi]
    judul = ""
    # Jalur utama Crossref: API resminya mengembalikan judul di
    # message.title. Negosiasi konten doi.org tidak dipakai sebagai
    # jalur utama karena doi.org MENYALURKAN (redirect) permintaan
    # CSL ke penyedia metadata, dan pengalihan memang ditolak di
    # modul ini (uji lokal v78: doi.org memberi string kosong).
    url = "https://api.crossref.org/works/" + quote(doi, safe="")
    req = urllib.request.Request(
        url, headers={"Accept": "application/json",
                      "User-Agent": "KhairyPustaka/1.0 "
                                    "(mailto:admin@khairys.web.id)"})
    try:
        with urllib.request.build_opener(_NoRedirect()).open(
                req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        msg = data.get("message") if isinstance(data, dict) else None
        t = (msg or {}).get("title") if isinstance(msg, dict) else None
        if isinstance(t, list):
            t = t[0] if t else ""
        judul = str(t or "").strip()
    except Exception:
        judul = ""
    _JUDUL_DOI_CACHE[doi] = judul
    return judul


def pastikan_doi_terdaftar(teks):
    """Lempar DoiTidakTerdaftar bila teks memuat DOI yang pasti
    tidak terdaftar. Teks tanpa DOI atau cek yang tidak pasti
    dibiarkan lewat.

    Pesan pengecualiannya adalah PENJELASAN untuk member (perintah
    Sul 2026-11-11: sistem harus menjelaskan, bukan sekadar mengirim
    pesan error): apa yang terjadi, kemungkinan penyebabnya, dan apa
    yang bisa dilakukan member. Jaminan kuota tidak terpotong
    ditambahkan oleh pemanggil sesuai jalurnya."""
    doi = doi_dari_teks(teks)
    if doi and doi_terdaftar(doi) is False:
        raise DoiTidakTerdaftar(
            f"DOI {doi} yang kamu tempel tidak ditemukan di register "
            "DOI resmi, jadi paper sumbernya tidak bisa dikenali dan "
            "pencariannya tidak dijalankan. Kemungkinan penyebabnya: "
            "ada karakter yang salah ketik, DOI nya memang belum "
            "terdaftar oleh penerbitnya, atau yang ditempel bukan "
            "DOI (DOI selalu berawalan 10. lalu diikuti garis "
            "miring). Coba cek lagi DOI nya dari halaman jurnal "
            "resminya, atau mulai dari cerita kebutuhanmu saja, itu "
            "juga bisa. Kalau kamu punya file PDF papernya, pakai "
            "titik mulai Upload PDF: sistem bisa membaca DOI dan "
            "isinya langsung dari file itu.")


def cari_serupa(url, num=10):
    """Find Similar: paper serupa dari satu link/DOI (titik mulai DOI).

    DOI polos WAJIB diubah jadi URL doi.org dulu. Mesin tidak bisa
    membaca string DOI mentah: terbukti 2026-11-11, DOI polos
    10.32832/pro.v5i4.6973 mengembalikan panduan instalasi, halaman
    login router, dan halaman tantangan bot, sedangkan URL doi.org
    untuk DOI yang sama mengembalikan paper serupa yang benar benar
    sejalur (paper sumber skor 0,9965, sisanya paper stres kerja
    perawat yang memang satu topik)."""
    pastikan_doi_terdaftar(url)
    doi = doi_dari_teks(url)
    if doi:
        url = "https://doi.org/" + doi
    payload = {"url": url, "numResults": int(num),
               "contents": {"text": {"maxCharacters": 4000}}}
    return _call("POST", "/findSimilar", payload, timeout=120)


# ---------------------------------------------------------------- mode 2
def jawaban_cepat(query):
    """Jawaban langsung bersitasi dari POST /answer."""
    return _call("POST", "/answer", {"query": query, "text": True},
                 timeout=120)


# ---------------------------------------------------------------- mode 3
SKEMA_BEDAH = {
    "type": "object",
    "required": ["ringkasan", "faktor_penyebab", "dampak", "rekomendasi"],
    "properties": {
        "ringkasan": {"type": "string"},
        "faktor_penyebab": {"type": "array", "items": {"type": "string"}},
        "dampak": {"type": "array", "items": {"type": "string"}},
        "rekomendasi": {"type": "array", "items": {"type": "string"}},
    },
}


def bedah_mendalam(query, num=10):
    """Deep search + outputSchema terstruktur + grounding per bidang."""
    payload = {"query": query, "type": "deep", "numResults": int(num),
               "outputSchema": SKEMA_BEDAH,
               "contents": {"text": {"maxCharacters": 3000}}}
    return _call("POST", "/search", payload, timeout=180)


# ---------------------------------------------------------------- mode 4
SISTEM_LAPORAN = (
    "Kamu asisten riset akademik. Susun laporan riset terstruktur "
    "berbahasa Indonesia berdasarkan sumber penelitian asli, dengan "
    "sitasi inline berupa tautan markdown ke sumber (DOI atau jurnal) "
    "untuk setiap klaim penting. Bedakan hubungan statistik dari sebab "
    "akibat, dan jangan menggeneralisasi angka lokal menjadi angka "
    "nasional.")


def laporan_buat(query):
    """Buat agent run effort high. Return body (berisi id + status)."""
    payload = {"query": query, "effort": "high",
               "systemPrompt": SISTEM_LAPORAN}
    return _call("POST", "/agent/runs", payload, timeout=120)


def laporan_status(run_id):
    return _call("GET", f"/agent/runs/{run_id}", None, timeout=60)


def laporan_tunggu(run_id, batas_detik=420, jeda=8.0, on_step=None):
    """Polling sampai completed/failed/cancelled atau batas waktu.

    Return body final. Melempar ExaError bila timeout lokal atau
    status gagal. Dipakai worker job di app.py, bukan request web.
    """
    t0 = time.time()
    while True:
        body = laporan_status(run_id)
        st = str(body.get("status") or "")
        if on_step:
            try:
                on_step(st, int(time.time() - t0))
            except Exception:
                pass
        if st == "completed":
            return body
        if st in ("failed", "cancelled", "canceled"):
            err = body.get("error")
            raise ExaError(f"agent run {st}: {str(err)[:200]}")
        if time.time() - t0 > batas_detik:
            raise ExaError("agent run melewati batas waktu tunggu lokal")
        time.sleep(jeda)
