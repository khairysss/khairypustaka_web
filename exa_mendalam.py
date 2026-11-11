"""Logika fitur Cari Jurnal Mendalam (Exa) untuk KhairyPustaka.

Keputusan Sul yang ditegakkan modul ini (2026-11-10):
- Tiga mode ringan (Cari Jurnal, Jawaban Cepat, Bedah Mendalam) GRATIS
  untuk member dengan SATU kuota gabungan 3x pemakaian per hari per
  member, reset pukul 00.00 WITA (kunci tanggal kalender WITA).
- Laporan Riset di LUAR kuota itu: berbayar dengan kredit laporan.
  Tarif Rp5.000 = 2 kredit; 1 kredit terpotong per laporan yang
  BERHASIL selesai (bukan per klik). Mekanisme cadangan (reserve) saat
  job dibuat mencegah dua laporan berjalan bersamaan membelanjakan
  kredit yang sama; cadangan lepas lagi bila laporan gagal, dan hanya
  berubah jadi potongan nyata saat laporan selesai.
- Rem biaya mode ringan: cache hasil 7 hari (hasil cache tidak memotong
  kuota dan tidak menambah belanja) + plafon belanja global harian dan
  bulanan yang dicatat dari costDollars ASLI tiap panggilan Exa dan
  ditegakkan sebelum panggilan baru dibuat. Plafon bisa diatur lewat
  environment EXA_CAP_HARIAN_USD (default 0.30) dan
  EXA_CAP_BULANAN_USD (default 9.0). Laporan Riset prabayar lewat
  kredit, jadi tidak masuk plafon ini.
- Label akses 4 tingkat dihitung dari teks yang BENAR-BENAR terambil
  dari Exa (bukan tebakan): lihat label_akses().

Semua penyimpanan lewat dbstore (kp_kv / file lokal) mengikuti pola
model_stats.py dan job_stats.py: dokumen JSON per keperluan, kunci
threading.Lock, fail-safe untuk pencatatan nonkritis.
"""
import copy
import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import dbstore

log = logging.getLogger("kpweb.exa_mendalam")

_WITA = timezone(timedelta(hours=8))

MODE_RINGAN = ("cari", "cepat", "bedah")
MODE_SEMUA = MODE_RINGAN + ("laporan",)
KUOTA_RINGAN_HARIAN = 3          # gabungan 3 mode ringan, per member
KUOTA_RINGAN_NAIK = 5            # tier 1 (lihat tangga tier di bawah)
KUOTA_RINGAN_TIER2 = 10          # tier 2
CACHE_TTL = 7 * 24 * 3600        # 7 hari
HARGA_KREDIT_RP = 5000           # Rp5.000 ...
KREDIT_PER_BELI = 2              # ... = 2 kredit laporan
# Benefit top up BERTINGKAT (Sul 2026-11-10 21:14 + perluasan 21:19):
# berdasar AKUMULASI total top up seumur akun (lifetime_topup),
# permanen selama akumulasinya terpenuhi:
#   Tier 0 (< Rp5.000)   : Exa ringan 3x/hari,  AI utama 15x/hari
#   Tier 1 (>= Rp5.000)  : Exa ringan 5x/hari,  AI utama 20x/hari
#   Tier 2 (>= Rp10.000) : Exa ringan 10x/hari, AI utama 25x/hari
# Tier 2 adalah PLAFON FINAL (penegasan Sul 2026-11-10 21:22):
# akumulasi di atas Rp10.000 TIDAK menaikkan limit apa pun lagi
# (Exa ringan mentok 10x/hari, AI utama mentok 25x/hari).
# Kredit Laporan tetap per transaksi (tiap Rp5.000 = 2 kredit) dan
# terus bertambah tanpa batas tier.
AMBANG_LIMIT_NAIK_RP = 5000      # ambang tier 1 (alias historis)
AMBANG_TIER2_RP = 10000          # ambang tier 2
_KUOTA_PER_TIER = {0: KUOTA_RINGAN_HARIAN, 1: KUOTA_RINGAN_NAIK,
                   2: KUOTA_RINGAN_TIER2}

_PATH_CACHE = "data/exa-cache.json"
_PATH_KUOTA = "data/exa-kuota.json"
_PATH_BELANJA = "data/exa-belanja.json"
_PATH_KREDIT = "data/exa-kredit.json"
_PATH_RIWAYAT = "data/exa-riwayat.json"
_PATH_LAPORAN = "data/exa-laporan.json"

_LOCK = threading.Lock()
_CACHE_MAKS = 60                 # entri cache tersimpan (terbaru)
_RIWAYAT_MAKS = 30               # per member
_LAPORAN_MAKS = 10               # laporan tersimpan per member
_LEDGER_MAKS = 50                # mutasi kredit tersimpan per member


def _hari_ini():
    return datetime.now(_WITA).strftime("%Y-%m-%d")


def _bulan_ini():
    return datetime.now(_WITA).strftime("%Y-%m")


def _muat(path):
    try:
        d = dbstore.load_json(path, None)
    except Exception as e:
        log.warning("exa_mendalam muat %s gagal: %s", path, e)
        d = None
    return d if isinstance(d, dict) else {}


def _simpan(path, d):
    dbstore.save_json(path, d)


# ------------------------------------------------------------ label akses
# Aturan (dari panjang teks yang benar-benar terambil untuk sumber itu):
#   >= 2500 karakter : Teks Penuh Terbuka (isi terbaca substantial)
#   900 - 2499       : Abstrak Saja (yang terambil seukuran abstrak)
#   200 - 899        : Pratinjau Terbatas (sebagian isi terambil)
#   < 200 / kosong   : Metadata Saja (hanya info terbit)
# Catatan kejujuran: label menyatakan apa yang berhasil diambil SISTEM
# KAMI dari sumber itu, bukan janji tentang situs penerbitnya.
LABEL_AKSES = {
    "full": "Teks Penuh Terbuka",
    "abstract": "Abstrak Saja",
    "partial": "Pratinjau Terbatas",
    "metadata": "Metadata Saja",
}


def label_akses(text_len, highlights_len=0):
    """Tentukan (kode, catatan) dari panjang teks terambil."""
    n = int(text_len or 0)
    if n >= 2500:
        return "full", (f"Sistem kami berhasil membaca isi sumber ini "
                        f"secara substantial ({n:,} karakter terambil)."
                        .replace(",", "."))
    if n >= 900:
        return "abstract", (f"Yang berhasil terambil hanya seukuran "
                            f"abstrak ({n:,} karakter). Teks penuhnya "
                            f"kemungkinan di balik langganan penerbit."
                            .replace(",", "."))
    if n >= 200:
        return "partial", (f"Hanya sebagian isi yang terambil "
                           f"({n:,} karakter), sisanya tidak terbuka."
                           .replace(",", "."))
    if highlights_len:
        return "abstract", ("Hanya cuplikan dan ringkasan yang terambil "
                            "dari sumber ini, bukan isi penuhnya.")
    return "metadata", ("Hanya judul, penulis, dan info terbit yang "
                        "terambil. Isi sumber tidak tersedia terbuka.")


def label_relevansi(idx):
    """Label relevansi berbasis PERINGKAT hasil (keputusan Sul
    2026-11-11): tanpa angka/persen, karena respons pencarian memang
    tidak membawa skor. Urutan hasil dari mesin adalah urutan
    relevansi: peringkat 1 paling relevan dengan cerita/kebutuhan
    member, makin ke bawah relevansinya menurun bertahap tapi tetap
    relevan. Berlaku juga untuk hasil DOI/Paper Serupa; skor
    kemiripan aslinya sengaja tidak ditampilkan sebagai angka."""
    if idx <= 1:
        return "Paling Relevan", "rel1"
    if idx <= 3:
        return "Sangat Relevan", "rel2"
    return "Relevan", "rel3"


# Kata fungsi Bahasa Indonesia: bukan pembawa topik, dibuang saat
# mengekstrak frasa kunci dari cerita member maupun judul paper.
_STOPKATA = {
    "yang", "dan", "atau", "di", "ke", "dari", "untuk", "dengan", "pada",
    "dalam", "ini", "itu", "saya", "aku", "kami", "kita", "juga", "sebagai",
    "oleh", "karena", "agar", "supaya", "bisa", "dapat", "akan", "telah",
    "sudah", "tidak", "ada", "menjadi", "saat", "ketika", "lebih", "para",
    "bahwa", "tersebut", "antara", "setelah", "sebelum", "melalui",
    "secara", "seperti", "hingga", "sampai", "bagi", "yaitu", "yakni",
    "maupun", "serta", "jika", "bila", "apabila", "namun", "tetapi",
    "tapi", "lagi", "pun", "saja", "hanya", "masih", "belum", "harus",
    "ingin", "mau", "butuh", "mencari", "meneliti", "tolong", "bantu",
    "jurnal", "paper", "referensi", "pendukung", "skripsi", "skripsiku",
    "penelitianku", "bab", "satu", "dua", "tiga", "nya", "apa", "siapa",
    "kapan", "dimana", "bagaimana", "apakah", "adalah", "merupakan",
    "terhadap",
    # Kata fungsi Bahasa Inggris (judul paper sering berbahasa Inggris)
    "and", "the", "of", "with", "for", "among", "between", "within",
    "in", "on", "at", "to", "a", "an", "by", "from", "as", "is", "are",
    "was", "were", "toward", "towards", "through", "during", "using",
    # Kata struktur judul penelitian yang terlalu umum untuk jadi
    # bukti kecocokan sendirian (hampir semua judul memuatnya)
    "pengaruh", "hubungan", "faktor", "dampak", "efek", "peran",
    "gambaran", "tingkat", "perbedaan", "korelasi", "determinan",
    # Rangkaian pembuka cerita member (boilerplate, bukan topik):
    # tanpa ini frasa seperti "sedang menyusun" bisa cocok tidak
    # sengaja di isi paper dan menghasilkan alasan yang tidak valid
    # (temuan verifikasi live v74, 2026-11-11).
    "sedang", "menyusun", "menulis", "membuat", "mengerjakan",
    "menyelesaikan", "landasan", "teori", "berbagai", "beberapa",
    "setiap", "semua", "banyak",
    # Kata fungsi Bahasa Inggris tambahan (temuan v74: "how students")
    "how", "what", "which", "who", "whom", "whose", "when", "where",
    "why", "this", "that", "these", "those", "its", "it", "their",
    "they", "them", "our", "your", "his", "her", "also", "more",
    "most", "other", "into", "over", "under", "about", "per", "via",
}


def _kata_konten(teks):
    """Daftar kata pembawa topik dari teks (tanpa kata fungsi).

    Hanya token huruf: token berisi angka (kode hash, nomor berkas,
    potongan DOI) bukan topik dan tidak boleh dikutip sebagai topik
    (temuan verifikasi live v75: string hash 64 karakter dikutip
    sebagai "topik utama" sebuah hasil serupa)."""
    mentah = re.findall(r"[a-zà-ÿ]+", (teks or "").lower())
    return [w for w in mentah if w not in _STOPKATA and len(w) >= 3]


def _frasa_kunci(teks, maks_bigram=8):
    """(bigram, unigram) pembawa topik dari teks, urut kemunculan.

    Bigram hanya dari dua kata konten yang bersebelahan persis di
    teks asli, jadi frasanya nyata ("kualitas tidur"), bukan
    gabungan acak.
    """
    mentah = re.findall(r"[a-zà-ÿ]+", (teks or "").lower())
    bigram, unigram, terlihat = [], [], set()
    for i, w in enumerate(mentah):
        if w in _STOPKATA or len(w) < 3:
            continue
        if w not in terlihat:
            terlihat.add(w)
            unigram.append(w)
        if i + 1 < len(mentah):
            w2 = mentah[i + 1]
            if w2 not in _STOPKATA and len(w2) >= 3 and w2 != w:
                bg = w + " " + w2
                if bg not in bigram:
                    bigram.append(bg)
    return bigram[:maks_bigram], unigram


def _frasa_cerita(teks):
    """Frasa bukti dari cerita member: rangkaian kata konten yang
    bersebelahan PERSIS di teks asli, diurutkan dari yang terpanjang
    (4 kata) ke bigram. Frasa utuh didahulukan supaya yang dikutip
    sebagai bukti adalah frasa lengkapnya ("pola asuh orang tua"),
    bukan potongan di tengahnya ("asuh orang") yang tidak bermakna
    sendirian (temuan audit live v76, 2026-11-11)."""
    mentah = re.findall(r"[a-zà-ÿ]+", (teks or "").lower())

    def konten(w):
        return w not in _STOPKATA and len(w) >= 3

    frasa = []
    for n in (4, 3, 2):
        for i in range(len(mentah) - n + 1):
            jendela = mentah[i:i + n]
            if all(konten(w) for w in jendela) \
                    and len(set(jendela)) > 1:
                f = " ".join(jendela)
                if f not in frasa:
                    frasa.append(f)
    return frasa


def _gabung_dan(items):
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " dan " + items[-1]


# Judul yang BUKAN judul paper sungguhan: halaman penghalang bot,
# placeholder mesin pencari, string hash, atau judul kosong. Hasil
# seperti ini dibuang dari daftar hasil (lihat _saring_hasil), bukan
# ditampilkan sebagai paper; alasan_relevansi juga menjaganya supaya
# judul sampah tidak pernah diklaim sebagai topik (temuan verifikasi
# live v74 dan v75, 2026-11-11: "Human Verification", "One moment,
# please...", "[title in Japanese]", string hash 64 karakter).
_JUDUL_SAMPAH = ("human verification", "one moment, please",
                 "just a moment", "ip address", "are you a robot",
                 "captcha", "attention required", "access denied",
                 "please wait", "checking your browser", "tanpa judul",
                 "403 forbidden", "404 not found", "error 404",
                 "error 403", "client challenge", "just checking",
                 "security check", "verify you are human",
                 "enable javascript", "ddos protection")
_RE_HEX = re.compile(r"[0-9a-f]{16,}")


def judul_sampah(judul):
    """True bila judul hasil bukan judul paper yang bisa dirujuk."""
    j = (judul or "").strip()
    if not j:
        return True
    low = j.lower()
    if any(s in low for s in _JUDUL_SAMPAH):
        return True
    if j.startswith("[") and j.endswith("]"):
        return True
    if j.startswith("<"):
        return True
    for tok in _RE_HEX.findall(low):
        if any(c.isdigit() for c in tok) \
                and any(c.isalpha() for c in tok):
            return True
    return False


def _saring_hasil(hasil):
    """Buang hasil berjudul sampah SEBELUM peringkat dan label
    diberikan, supaya label relevansi hanya menempel pada paper yang
    benar benar bisa dirujuk member."""
    return [r for r in (hasil or [])
            if not judul_sampah(r.get("title"))]


def alasan_relevansi(idx, total, query, r, serupa=False):
    """Kalimat alasan peringkat satu paper, dibangun HANYA dari bukti
    yang benar benar ada (perintah Sul 2026-11-11: label tanpa alasan
    adalah klaim kosong): (1) frasa dari cerita member yang benar
    benar muncul di judul atau cuplikan isi paper ini, (2) topik
    utama paper dari judulnya sendiri, (3) posisi peringkatnya dari
    mesin pencari makna di antara semua hasil. Tanpa bukti frasa,
    alasannya jujur menyebut dasar topik + peringkat, tidak
    mengarang kecocokan."""
    judul = (r.get("title") or "")
    judul_low = judul.lower()
    isi_low = (" ".join(r.get("highlights") or []) + " "
               + (r.get("text") or "")[:1500]).lower()
    temu = {"judul": [], "isi": [], "kata": []}
    if query and not serupa:
        bigram, unigram = _frasa_kunci(query)
        terkutip = []
        for f in _frasa_cerita(query):
            if any(f in t or t in f for t in terkutip):
                continue
            if f in judul_low:
                temu["judul"].append(f)
                terkutip.append(f)
            elif f in isi_low:
                temu["isi"].append(f)
                terkutip.append(f)
            if len(temu["judul"]) + len(temu["isi"]) >= 2:
                break
        if len(temu["judul"]) + len(temu["isi"]) < 2:
            for w in unigram:
                if len(w) >= 4 and (w in judul_low or w in isi_low) \
                        and not any(w in bg.split()
                                    for bg in temu["judul"]
                                    + temu["isi"]):
                    temu["kata"].append(w)
                    if len(temu["judul"]) + len(temu["isi"]) \
                            + len(temu["kata"]) >= 2:
                        break
    bukti = []
    if temu["judul"]:
        bukti.append("judulnya memuat frasa persis "
                     + _gabung_dan([f"'{b}'" for b in temu["judul"]]))
    if temu["isi"]:
        bukti.append("cuplikan isinya membahas "
                     + _gabung_dan([f"'{b}'" for b in temu["isi"]]))
    if temu["kata"]:
        bukti.append("membahas "
                     + _gabung_dan([f"'{b}'" for b in temu["kata"]]))
    klausa_peringkat = ""
    if serupa:
        if idx <= 1 and total:
            klausa_peringkat = (f" Ini paper yang paling mirip dengan "
                                f"paper sumbermu di antara {total} "
                                f"hasil serupa.")
        elif total:
            klausa_peringkat = (f" Peringkat {idx} dari {total} paper "
                                f"serupa dengan paper sumbermu.")
    elif total:
        if idx <= 1:
            klausa_peringkat = (f" Mesin pencarian makna "
                                f"menempatkannya di peringkat 1 dari "
                                f"{total} hasil.")
        else:
            klausa_peringkat = (f" Ia berada di peringkat {idx} dari "
                                f"{total} hasil.")
    if bukti:
        return ("Dasarnya: " + _gabung_dan(bukti)
                + ", cocok langsung dengan yang kamu ceritakan."
                + klausa_peringkat)
    # Judul sampah bukan topik paper, jangan diklaim sebagai topik.
    # Alasannya mundur ke dasar peringkat saja, yang tetap benar apa
    # adanya. (Hasil berjudul sampah sebenarnya sudah dibuang oleh
    # _saring_hasil; ini penjaga kedua untuk jalur sitasi.)
    generik = ("Paper ini terpilih oleh mesin pencarian makna dari "
               "kemiripannya dengan paper sumbermu."
               if serupa else
               "Paper ini terpilih oleh mesin pencarian makna dari "
               "kecocokan isinya dengan kebutuhanmu.")
    if judul_sampah(judul):
        return generik + klausa_peringkat
    topik = _frasa_kunci(judul, maks_bigram=3)[0] \
        or _kata_konten(judul)[:3]
    if topik:
        # Hanya menyebut topik paper itu sendiri. Klaim "sejalur
        # dengan kebutuhanmu" dibuang (temuan v75): tanpa bukti frasa
        # yang cocok, keselarasan topik tidak bisa dinyatakan; dasar
        # relevansinya adalah peringkat dari mesin, yang disebut di
        # klausa peringkat.
        return ("Topik utamanya seputar "
                + _gabung_dan([f"'{t}'" for t in topik[:3]])
                + "." + klausa_peringkat)
    return generik + klausa_peringkat


_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>\)\]]+")


def _doi_dari(url, text=""):
    m = _DOI_RE.search(url or "")
    if m:
        return m.group(0).rstrip(".,;")
    m = _DOI_RE.search((text or "")[:2000])
    return m.group(0).rstrip(".,;") if m else ""


def _domain(url):
    try:
        return (urlparse(url or "").hostname or "").replace("www.", "")
    except Exception:
        return ""


def normalisasi_paper(r, idx, query="", total=0, serupa=False):
    """Satu hasil Exa -> objek paper seragam untuk frontend."""
    url = r.get("url") or ""
    text = r.get("text") or ""
    highlights = r.get("highlights") or []
    hl_text = " ".join(highlights)
    kode, catatan = label_akses(len(text), len(hl_text))
    tgl = r.get("publishedDate") or ""
    tahun = tgl[:4] if len(tgl) >= 4 and tgl[:4].isdigit() else ""
    cuplikan = (highlights[0] if highlights else text[:300]).strip()
    rel_label, rel_kelas = label_relevansi(idx)
    penulis = (r.get("author") or "").strip()
    penulis = re.sub(r"\s+", " ", penulis).strip(" ,&")
    _w = penulis.split(" ")
    if len(_w) >= 4 and len(_w) % 2 == 0 \
            and _w[:len(_w) // 2] == _w[len(_w) // 2:]:
        # Nama yang sama tertulis dua kali berturut turut oleh
        # sumbernya (temuan audit live v76: "Ernawati Harahap
        # Ernawati Harahap") ditampilkan sekali saja.
        penulis = " ".join(_w[:len(_w) // 2])
    if penulis.lower() in ("windows user", "admin", "administrator",
                           "unknown", "anonymous", "user"):
        # Nama akun bawaan perangkat lunak penerbit, bukan penulis
        # (temuan verifikasi live v75: penulis tampil "Windows User").
        penulis = ""
    return {
        "id": idx,
        "title": (r.get("title") or "(tanpa judul)").strip(),
        "url": url,
        "domain": _domain(url),
        "author": penulis,
        "published": tgl,
        "year": tahun,
        "doi": _doi_dari(url, text),
        "text": text,
        "text_len": len(text),
        "highlights": highlights,
        "snippet": cuplikan,
        "access": kode,
        "access_label": LABEL_AKSES[kode],
        "access_note": catatan,
        "relevansi": rel_label,
        "relevansi_kelas": rel_kelas,
        "alasan_relevansi": alasan_relevansi(idx, total, query, r,
                                             serupa=serupa),
        "type": "Paper" if (r.get("id") or "") else "Sumber",
    }


def normalisasi_hasil_cari(body, query="", serupa=False):
    hasil = _saring_hasil(body.get("results"))
    if serupa:
        # Paper sumber tidak boleh tampil sebagai "paper serupa"
        # bagi dirinya sendiri: lewat URL doi.org, mesin menaruh
        # paper sumber di peringkat 1 (skor 0,9965). Saring lewat
        # DOI di URL hasil atau di awal teksnya (halaman artikel
        # biasanya mencetak DOI nya sendiri di bagian atas).
        doi_sumber = _doi_dari(query or "")
        if doi_sumber:
            hasil = [r for r in hasil
                     if _doi_dari(r.get("url") or "",
                                  (r.get("text") or "")[:1200])
                     != doi_sumber]
    papers = [normalisasi_paper(r, i + 1, query=query, total=len(hasil),
                                 serupa=serupa)
              for i, r in enumerate(hasil)]
    return {"papers": papers}


def normalisasi_hasil_cepat(body, query=""):
    """POST /answer: jawaban + sitasi dinormalisasi jadi paper juga."""
    papers = []
    sitasi = _saring_hasil(body.get("citations"))
    for i, c in enumerate(sitasi):
        rr = {"title": c.get("title"), "url": c.get("url"),
              "author": c.get("author"), "publishedDate":
              c.get("publishedDate"), "text": c.get("text") or "",
              "highlights": c.get("highlights") or [],
              "id": c.get("id")}
        papers.append(normalisasi_paper(rr, i + 1, query=query,
                                         total=len(sitasi)))
    return {"answer": body.get("answer") or "", "papers": papers}


def normalisasi_hasil_bedah(body, query=""):
    """Deep + schema: konten terstruktur + grounding + paper hasil."""
    out = body.get("output") or {}
    konten = out.get("content") if isinstance(out, dict) else None
    if not isinstance(konten, dict):
        konten = {}
    grounding = {}
    for g in (out.get("grounding") or []) if isinstance(out, dict) else []:
        f = g.get("field")
        if f:
            grounding[f] = [{"title": c.get("title") or "",
                             "url": c.get("url") or ""}
                            for c in (g.get("citations") or [])]
    hasil_cari = _saring_hasil(body.get("results"))
    papers = [normalisasi_paper(r, i + 1, query=query,
                                 total=len(hasil_cari))
              for i, r in enumerate(hasil_cari)]
    return {"sections": {
                "ringkasan": konten.get("ringkasan") or "",
                "faktor_penyebab": konten.get("faktor_penyebab") or [],
                "dampak": konten.get("dampak") or [],
                "rekomendasi": konten.get("rekomendasi") or []},
            "grounding": grounding, "papers": papers}


# ------------------------------------------------------------------ cache
def kunci_cache(mode, query, saringan):
    # "v" = versi skema hasil. Naik dari 1 ke 2 pada 2026-11-11
    # (v77): entri lama memuat hasil Paper Serupa dari jalur DOI
    # polos yang isinya halaman acak, plus kartu tanpa label dan
    # alasan relevansi; semua entri lama sengaja ditinggalkan.
    bahan = json.dumps({"m": mode, "q": (query or "").strip().lower(),
                        "f": saringan or {}, "v": 2},
                       sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(bahan.encode("utf-8")).hexdigest()[:32]


def cache_ambil(kunci):
    """Hasil cache segar (< 7 hari) atau None."""
    d = _muat(_PATH_CACHE)
    e = d.get(kunci)
    if not isinstance(e, dict):
        return None
    if time.time() - float(e.get("ts") or 0) > CACHE_TTL:
        return None
    return copy.deepcopy(e)


def cache_simpan(kunci, mode, query, hasil, biaya_usd):
    try:
        with _LOCK:
            d = _muat(_PATH_CACHE)
            d[kunci] = {"ts": time.time(), "mode": mode, "query": query,
                        "hasil": hasil, "biaya": float(biaya_usd or 0)}
            kini = time.time()
            for k in [k for k, v in d.items()
                      if kini - float(v.get("ts") or 0) > CACHE_TTL]:
                d.pop(k, None)
            if len(d) > _CACHE_MAKS:
                for k, _v in sorted(d.items(),
                                    key=lambda kv: kv[1].get("ts") or 0
                                    )[:len(d) - _CACHE_MAKS]:
                    d.pop(k, None)
            _simpan(_PATH_CACHE, d)
    except Exception as e:
        log.warning("exa cache simpan gagal: %s", e)


# ------------------------------------------------------------------ kuota
def lifetime_topup(uid):
    """Total rupiah top up yang pernah lunas di akun ini (penanda tier)."""
    return int((_kredit_doc().get("lifetime_topup") or {}).get(uid) or 0)


def tier(uid):
    """Tier benefit member berdasar akumulasi lifetime_topup:
    0 (< Rp5.000), 1 (>= Rp5.000), 2 (>= Rp10.000)."""
    lt = lifetime_topup(uid)
    if lt >= AMBANG_TIER2_RP:
        return 2
    if lt >= AMBANG_LIMIT_NAIK_RP:
        return 1
    return 0


def kuota_limit(uid):
    """Limit harian mode ringan member ini menurut tiernya:
    tier 0 = 3, tier 1 = 5, tier 2 = 10 (permanen)."""
    return _KUOTA_PER_TIER[tier(uid)]


def kuota_terpakai(uid):
    d = _muat(_PATH_KUOTA)
    return int((d.get(uid) or {}).get(_hari_ini()) or 0)


def kuota_sisa(uid):
    return max(0, kuota_limit(uid) - kuota_terpakai(uid))


def kuota_konsumsi(uid):
    """Potong 1 kuota gabungan. True bila berhasil, False bila habis."""
    limit = kuota_limit(uid)
    with _LOCK:
        d = _muat(_PATH_KUOTA)
        entri = d.setdefault(uid, {})
        hari = _hari_ini()
        for k in [k for k in entri if k != hari]:
            entri.pop(k, None)
        n = int(entri.get(hari) or 0)
        if n >= limit:
            return False
        entri[hari] = n + 1
        _simpan(_PATH_KUOTA, d)
        return True


def kuota_refund(uid):
    with _LOCK:
        d = _muat(_PATH_KUOTA)
        entri = d.get(uid) or {}
        hari = _hari_ini()
        n = int(entri.get(hari) or 0)
        if n > 0:
            entri[hari] = n - 1
            d[uid] = entri
            _simpan(_PATH_KUOTA, d)


def kuota_reset_harian(uid):
    """Nolkan pemakaian mode ringan HARI INI milik uid ini.

    Aturan Sul 2026-11-10 23:53: begitu klaim top up kredit laporan
    lunas terdeteksi, sisa kuota langsung jadi plafon tier penuh,
    BUKAN plafon dikurangi pemakaian yang sudah lewat. Pemanggil
    (jalur klaim atomik di app.py) menjamin tepat sekali per invoice;
    fungsi ini sendiri idempoten (menolkan hitungan yang sudah 0
    tidak mengubah apa pun)."""
    with _LOCK:
        d = _muat(_PATH_KUOTA)
        entri = d.setdefault(uid, {})
        hari = _hari_ini()
        for k in [k for k in entri if k != hari]:
            entri.pop(k, None)
        entri[hari] = 0
        d[uid] = entri
        _simpan(_PATH_KUOTA, d)


# ---------------------------------------------------------------- belanja
def _cap_harian():
    try:
        return float(os.environ.get("EXA_CAP_HARIAN_USD") or 0.30)
    except ValueError:
        return 0.30


def _cap_bulanan():
    try:
        return float(os.environ.get("EXA_CAP_BULANAN_USD") or 9.0)
    except ValueError:
        return 9.0


def belanja_ringkas():
    d = _muat(_PATH_BELANJA)
    return {"hari_ini": float((d.get("hari") or {}).get(_hari_ini()) or 0),
            "bulan_ini": float((d.get("bulan") or {}).get(_bulan_ini()) or 0),
            "cap_harian": _cap_harian(), "cap_bulanan": _cap_bulanan(),
            "total": float(d.get("total") or 0)}


def belanja_boleh(estimasi_usd):
    """True bila estimasi panggilan baru masih di dalam plafon."""
    r = belanja_ringkas()
    return (r["hari_ini"] + estimasi_usd <= r["cap_harian"]
            and r["bulan_ini"] + estimasi_usd <= r["cap_bulanan"])


def belanja_catat(usd, mode, hitung_cap=True):
    """Catat biaya ASLI dari respons Exa (fail-safe). hitung_cap=False
    untuk mode yang digerbang kredit (Laporan Riset): biayanya tetap
    masuk total + per_mode supaya monitor jujur, tapi tidak memakan
    ember plafon harian/bulanan yang khusus menjaga mode ringan."""
    try:
        u = float(usd or 0)
        if u <= 0:
            return
        with _LOCK:
            d = _muat(_PATH_BELANJA)
            hari = d.setdefault("hari", {})
            bulan = d.setdefault("bulan", {})
            if hitung_cap:
                hari[_hari_ini()] = float(hari.get(_hari_ini()) or 0) + u
                bulan[_bulan_ini()] = float(bulan.get(_bulan_ini()) or 0) + u
            d["total"] = float(d.get("total") or 0) + u
            for k in sorted(hari.keys())[:-92]:
                hari.pop(k, None)
            for k in sorted(bulan.keys())[:-13]:
                bulan.pop(k, None)
            per_mode = d.setdefault("per_mode", {})
            per_mode[mode] = float(per_mode.get(mode) or 0) + u
            _simpan(_PATH_BELANJA, d)
    except Exception as e:
        log.warning("exa belanja catat gagal: %s", e)


def job_catat(mode, ok):
    """Catat penyelesaian satu job mendalam (selesai/gagal) per mode
    per hari kalender WITA ke store belanja (kunci "job"), supaya tab
    Sistem admin bisa menampilkan hitungan job hari ini. Fail-safe:
    galat hanya dicatat di log, tidak pernah mengganggu job."""
    try:
        m = str(mode or "").strip().lower()
        if m not in MODE_SEMUA:
            return
        with _LOCK:
            d = _muat(_PATH_BELANJA)
            job = d.setdefault("job", {})
            hari = job.setdefault("hari", {})
            entri = hari.setdefault(_hari_ini(), {})
            s = entri.setdefault(m, {"selesai": 0, "gagal": 0})
            kunci = "selesai" if ok else "gagal"
            s[kunci] = int(s.get(kunci) or 0) + 1
            for k in sorted(hari.keys())[:-92]:
                hari.pop(k, None)
            _simpan(_PATH_BELANJA, d)
    except Exception as e:
        log.warning("exa job catat gagal: %s", e)


_SALDO_BASIS_DEFAULT = 50.0  # USD, basis perkiraan sisa kredit akun


def _saldo_basis():
    """Basis kredit akun (USD) untuk perkiraan sisa: angka yang
    tersimpan di store belanja (kunci saldo_basis_usd); bawaan 50."""
    try:
        v = float((_muat(_PATH_BELANJA).get("saldo_basis_usd") or 0))
    except (TypeError, ValueError):
        v = 0
    return v if v > 0 else _SALDO_BASIS_DEFAULT


def monitor_ringkas():
    """Ringkasan pemantauan mesin mendalam untuk tab Sistem admin:
    status kunci (boolean saja, nilainya tidak pernah dibaca di
    sini), belanja vs plafon, hitungan job hari ini per mode, total
    belanja sejak fitur berjalan, dan perkiraan sisa kredit akun
    (basis tersimpan dikurangi total belanja tercatat)."""
    r = belanja_ringkas()
    d = _muat(_PATH_BELANJA)
    job_hari = ((d.get("job") or {}).get("hari") or {}).get(
        _hari_ini()) or {}

    def _jm(mode):
        s = job_hari.get(mode) or {}
        return {"selesai": int(s.get("selesai") or 0),
                "gagal": int(s.get("gagal") or 0)}

    basis = _saldo_basis()
    return {
        "kunci_terpasang": bool(
            (os.environ.get("EXA_API_KEY") or "").strip()),
        "belanja_hari_ini_usd": round(r["hari_ini"], 4),
        "belanja_bulan_ini_usd": round(r["bulan_ini"], 4),
        "total_belanja_usd": round(r["total"], 4),
        "cap_harian_usd": r["cap_harian"],
        "cap_bulanan_usd": r["cap_bulanan"],
        "sisa_cap_harian_usd": round(
            max(0.0, r["cap_harian"] - r["hari_ini"]), 4),
        "sisa_cap_bulanan_usd": round(
            max(0.0, r["cap_bulanan"] - r["bulan_ini"]), 4),
        "job_hari_ini": {m: _jm(m) for m in MODE_SEMUA},
        "belanja_per_mode_usd": {
            k: round(float(v or 0), 4)
            for k, v in (d.get("per_mode") or {}).items()},
        "saldo_basis_usd": basis,
        "perkiraan_sisa_kredit_usd": round(basis - r["total"], 4)}


def estimasi_biaya(mode, num):
    """Estimasi kasar biaya panggilan (untuk gerbang plafon)."""
    n = max(1, int(num or 10))
    if mode == "cari":
        return 0.004 * (n / 10.0)
    if mode == "cepat":
        return 0.005
    if mode == "bedah":
        return 0.012 * (n / 10.0)
    return 0.0


# ----------------------------------------------------------------- kredit
def _kredit_doc():
    d = _muat(_PATH_KREDIT)
    for k in ("saldo", "cadangan", "riwayat", "lifetime_topup"):
        if not isinstance(d.get(k), dict):
            d[k] = {}
    return d


def kredit_catat_topup(uid, amount_rp):
    """Catat satu top up LUNAS (rupiah) ke penanda lifetime member.

    Dipanggil tepat satu kali per invoice dari jalur klaim atomik
    premium_pay (pending_claim), jadi invoice yang sama tidak bisa
    tercatat dua kali. Penanda ini yang menaikkan limit mode ringan
    secara permanen (lihat kuota_limit). Return total lifetime."""
    try:
        rp = int(amount_rp or 0)
    except (TypeError, ValueError):
        rp = 0
    if rp <= 0:
        return lifetime_topup(uid)
    with _LOCK:
        d = _kredit_doc()
        d["lifetime_topup"][uid] = int(
            d["lifetime_topup"].get(uid) or 0) + rp
        _simpan(_PATH_KREDIT, d)
        return int(d["lifetime_topup"][uid])


def kredit_saldo(uid):
    return int((_kredit_doc().get("saldo") or {}).get(uid) or 0)


def kredit_cadangan(uid):
    return int((_kredit_doc().get("cadangan") or {}).get(uid) or 0)


def kredit_tersedia(uid):
    return max(0, kredit_saldo(uid) - kredit_cadangan(uid))


def _ledger(d, uid, delta, keterangan):
    lst = d["riwayat"].setdefault(uid, [])
    lst.append({"ts": time.time(), "delta": int(delta),
                "saldo": int(d["saldo"].get(uid) or 0),
                "keterangan": keterangan})
    del lst[:-_LEDGER_MAKS]


def kredit_tambah(uid, jumlah, keterangan):
    """Tambah kredit (pembelian lunas / pemberian admin). Return saldo."""
    jumlah = int(jumlah)
    if jumlah <= 0:
        return kredit_saldo(uid)
    with _LOCK:
        d = _kredit_doc()
        d["saldo"][uid] = int(d["saldo"].get(uid) or 0) + jumlah
        _ledger(d, uid, jumlah, keterangan)
        _simpan(_PATH_KREDIT, d)
        return int(d["saldo"][uid])


def kredit_cadangkan(uid):
    """Cadangkan 1 kredit untuk laporan yang mulai jalan. True bila ada."""
    with _LOCK:
        d = _kredit_doc()
        saldo = int(d["saldo"].get(uid) or 0)
        cad = int(d["cadangan"].get(uid) or 0)
        if saldo - cad < 1:
            return False
        d["cadangan"][uid] = cad + 1
        _simpan(_PATH_KREDIT, d)
        return True


def kredit_lepas_cadangan(uid):
    """Lepas cadangan (laporan gagal/dibatalkan; saldo tidak berubah)."""
    with _LOCK:
        d = _kredit_doc()
        cad = int(d["cadangan"].get(uid) or 0)
        if cad > 0:
            d["cadangan"][uid] = cad - 1
            _simpan(_PATH_KREDIT, d)


def kredit_potong_cadangan(uid, keterangan="Laporan Riset selesai"):
    """Ubah 1 cadangan jadi potongan nyata (laporan berhasil selesai)."""
    with _LOCK:
        d = _kredit_doc()
        cad = int(d["cadangan"].get(uid) or 0)
        saldo = int(d["saldo"].get(uid) or 0)
        if cad < 1 or saldo < 1:
            return False
        d["cadangan"][uid] = cad - 1
        d["saldo"][uid] = saldo - 1
        _ledger(d, uid, -1, keterangan)
        _simpan(_PATH_KREDIT, d)
        return True


def kredit_riwayat(uid):
    return list((_kredit_doc().get("riwayat") or {}).get(uid) or [])


# ------------------------------------------------------- riwayat & laporan
def riwayat_tambah(uid, mode, query, jumlah_sumber, kunci):
    try:
        with _LOCK:
            d = _muat(_PATH_RIWAYAT)
            lst = d.setdefault(uid, [])
            lst.insert(0, {"ts": time.time(), "mode": mode,
                           "query": (query or "")[:300],
                           "jumlah_sumber": int(jumlah_sumber or 0),
                           "kunci": kunci})
            del lst[_RIWAYAT_MAKS:]
            _simpan(_PATH_RIWAYAT, d)
    except Exception as e:
        log.warning("exa riwayat tambah gagal: %s", e)


def riwayat_daftar(uid):
    return list((_muat(_PATH_RIWAYAT).get(uid) or []))


def laporan_simpan(uid, query, teks, biaya_usd):
    """Simpan laporan selesai agar bisa dibuka lagi dari riwayat."""
    lid = hashlib.sha256(
        f"{uid}|{time.time()}|{query}".encode()).hexdigest()[:16]
    try:
        with _LOCK:
            d = _muat(_PATH_LAPORAN)
            lst = d.setdefault(uid, [])
            lst.insert(0, {"id": lid, "ts": time.time(), "query": query,
                           "teks": (teks or "")[:30000],
                           "biaya": float(biaya_usd or 0)})
            del lst[_LAPORAN_MAKS:]
            _simpan(_PATH_LAPORAN, d)
    except Exception as e:
        log.warning("exa laporan simpan gagal: %s", e)
    return lid


def laporan_ambil(uid, lid):
    for e in (_muat(_PATH_LAPORAN).get(uid) or []):
        if e.get("id") == lid:
            return e
    return None
