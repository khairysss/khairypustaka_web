"""Konversi file lokal untuk halaman Convert File (tanpa API luar).

Semua fungsi menerima bytes dan mengembalikan bytes. File tidak pernah
disimpan ke disk oleh modul ini; yang masuk hanya dibaca di memori lalu
dibuang. Batas keras (ukuran file, jumlah halaman) ditegakkan di sini
supaya aman di spek Render Free.

Pesan error memakai kelas ConvertError dengan kalimat Bahasa Indonesia
yang ramah dan aman ditampilkan langsung ke pengguna.
"""

import io
import re
import zipfile

from pypdf import PdfReader, PdfWriter

try:
    import pymupdf  # PyMuPDF
except ImportError:  # pragma: no cover
    pymupdf = None

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None


class ConvertError(ValueError):
    """Error konversi yang pesannya aman ditampilkan ke pengguna."""


# Batas keras convert lokal (keputusan desain 2026-10-19).
MAKS_HALAMAN_PDF = 100
MAKS_HALAMAN_KE_GAMBAR = 30
DPI_KE_GAMBAR = 150
GAMBAR_FORMAT_BOLEH = {"JPEG", "PNG", "WEBP", "BMP", "GIF", "TIFF"}
GAMBAR_DIMENSI_MAKS = 2000  # sisi terpanjang, biar PDF tidak raksasa


def _buka_pdf(raw, maks_halaman=MAKS_HALAMAN_PDF):
    """Validasi + buka PDF. Kembalikan PdfReader, atau lempar ConvertError."""
    try:
        baca = PdfReader(io.BytesIO(raw))
    except Exception:
        raise ConvertError(
            "file PDF-nya tidak bisa dibaca, kemungkinan rusak. "
            "Coba file lain ya")
    if getattr(baca, "is_encrypted", False):
        raise ConvertError(
            "PDF-nya terkunci kata sandi, jadi tidak bisa diproses. "
            "Lepas kuncinya dulu ya")
    n = len(baca.pages)
    if n == 0:
        raise ConvertError("PDF-nya kosong, tidak ada halaman yang diproses")
    if n > maks_halaman:
        raise ConvertError(
            f"PDF-nya {n} halaman, maksimal {maks_halaman} halaman ya. "
            "Pisah dulu jadi beberapa bagian")
    return baca


def gabung_pdf(daftar_raw):
    """Satukan 2 sampai 5 file PDF jadi satu. Total maksimal 100 halaman."""
    if not 2 <= len(daftar_raw) <= 5:
        raise ConvertError("pilih 2 sampai 5 file PDF buat digabung ya")
    tulis = PdfWriter()
    total = 0
    for raw in daftar_raw:
        baca = _buka_pdf(raw)
        total += len(baca.pages)
        if total > MAKS_HALAMAN_PDF:
            raise ConvertError(
                "total halamannya lewat 100, maksimal 100 halaman ya")
        for hal in baca.pages:
            tulis.add_page(hal)
    keluar = io.BytesIO()
    tulis.write(keluar)
    return keluar.getvalue()


_RENTANG = re.compile(r"^\s*(\d+)(?:\s*-\s*(\d+))?\s*$")


def _urai_rentang(rentang, total):
    """Urai '1-3,5' jadi daftar indeks halaman 0-based. Validasi ketat."""
    bagian = []
    for potong in (rentang or "").split(","):
        cocok = _RENTANG.match(potong)
        if not cocok:
            raise ConvertError(
                "format rentangnya tidak dimengerti. Contoh yang benar: "
                "1-3,5 (halaman 1 sampai 3, lalu halaman 5)")
        awal = int(cocok.group(1))
        akhir = int(cocok.group(2) or cocok.group(1))
        if awal < 1 or akhir < 1 or awal > total or akhir > total or akhir < awal:
            raise ConvertError(
                f"rentang {awal} sampai {akhir} di luar jumlah halaman "
                f"({total})")
        bagian.extend(range(awal - 1, akhir))
    unik = list(dict.fromkeys(bagian))  # buang duplikat, jaga urutan
    if not unik:
        raise ConvertError("rentangnya kosong, tulis halaman yang mau "
                           "diambil ya")
    return unik


def pisah_pdf(raw, rentang):
    """Ambil halaman tertentu dari PDF sesuai rentang '1-3,5'."""
    baca = _buka_pdf(raw)
    indeks = _urai_rentang(rentang, len(baca.pages))
    tulis = PdfWriter()
    for i in indeks:
        tulis.add_page(baca.pages[i])
    keluar = io.BytesIO()
    tulis.write(keluar)
    return keluar.getvalue()


def kompres_pdf(raw):
    """Kompres PDF dengan PyMuPDF. Bila hasil tidak lebih kecil,
    kembalikan file aslinya supaya tidak buang waktu pengguna."""
    if pymupdf is None:
        raise ConvertError("mesin kompresnya belum tersedia, coba lagi nanti")
    try:
        dok = pymupdf.open(stream=raw, filetype="pdf")
    except Exception:
        raise ConvertError(
            "file PDF-nya tidak bisa dibaca, kemungkinan rusak. "
            "Coba file lain ya")
    try:
        if dok.is_encrypted:
            raise ConvertError(
                "PDF-nya terkunci kata sandi, jadi tidak bisa diproses. "
                "Lepas kuncinya dulu ya")
        n = dok.page_count
        if n == 0:
            raise ConvertError("PDF-nya kosong, tidak ada yang dikompres")
        if n > MAKS_HALAMAN_PDF:
            raise ConvertError(
                f"PDF-nya {n} halaman, maksimal {MAKS_HALAMAN_PDF} halaman ya")
        keluar = dok.tobytes(garbage=4, deflate=True,
                             deflate_images=True, deflate_fonts=True)
    finally:
        dok.close()
    if len(keluar) >= len(raw):
        return raw  # sudah padat, tidak ada yang bisa dihemat
    return keluar


def gambar_ke_pdf(daftar_raw):
    """Satukan 1 sampai 10 gambar (JPG/PNG/dsb) jadi satu file PDF."""
    if Image is None:
        raise ConvertError("mesin gambarnya belum tersedia, coba lagi nanti")
    if not 1 <= len(daftar_raw) <= 10:
        raise ConvertError("pilih 1 sampai 10 gambar ya")
    daftar = []
    for raw in daftar_raw:
        try:
            gbr = Image.open(io.BytesIO(raw))
            gbr.load()
        except Exception:
            raise ConvertError(
                "ada gambar yang tidak bisa dibaca, pastikan formatnya "
                "JPG atau PNG ya")
        if (gbr.format or "").upper() not in GAMBAR_FORMAT_BOLEH:
            raise ConvertError(
                "format gambarnya tidak didukung, pakai JPG atau PNG ya")
        if gbr.mode in ("RGBA", "LA", "P"):
            gbr = gbr.convert("RGB")
        # Kecilkan gambar raksasa (foto HP) biar PDF tidak membengkak.
        if max(gbr.size) > GAMBAR_DIMENSI_MAKS:
            gbr.thumbnail((GAMBAR_DIMENSI_MAKS, GAMBAR_DIMENSI_MAKS),
                          Image.LANCZOS)
        daftar.append(gbr)
    keluar = io.BytesIO()
    daftar[0].save(keluar, format="PDF", save_all=True,
                   append_images=daftar[1:])
    return keluar.getvalue()


def pdf_ke_gambar(raw, dpi=DPI_KE_GAMBAR, maks_halaman=MAKS_HALAMAN_KE_GAMBAR):
    """Render tiap halaman PDF jadi JPG. Kembalikan (bytes, 'jpg'/'zip'):
    satu halaman -> JPG langsung, banyak halaman -> ZIP berisi JPG."""
    if pymupdf is None:
        raise ConvertError("mesin gambarnya belum tersedia, coba lagi nanti")
    try:
        dok = pymupdf.open(stream=raw, filetype="pdf")
    except Exception:
        raise ConvertError(
            "file PDF-nya tidak bisa dibaca, kemungkinan rusak. "
            "Coba file lain ya")
    try:
        if dok.is_encrypted:
            raise ConvertError(
                "PDF-nya terkunci kata sandi, jadi tidak bisa diproses. "
                "Lepas kuncinya dulu ya")
        n = dok.page_count
        if n == 0:
            raise ConvertError("PDF-nya kosong, tidak ada yang dirender")
        if n > maks_halaman:
            raise ConvertError(
                f"PDF-nya {n} halaman, maksimal {maks_halaman} halaman ya "
                "untuk PDF ke Gambar")
        zum = dpi / 72.0
        hasil = []
        for i in range(n):
            piksel = dok[i].get_pixmap(matrix=pymupdf.Matrix(zum, zum))
            hasil.append(piksel.tobytes("jpg", jpg_quality=85))
    finally:
        dok.close()
    if n == 1:
        return hasil[0], "jpg"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, data in enumerate(hasil, 1):
            zf.writestr(f"halaman-{i:03d}.jpg", data)
    return buf.getvalue(), "zip"


def pdf_ke_word(raw):
    """PDF -> DOCX mode teks rapi (lokal, PyMuPDF + python-docx).

    Dipakai sebagai jalur cadangan PDF ke Word karena iLoveAPI tidak
    menyediakan tool PDF ke Word. Hasilnya dokumen Word yang teksnya
    bisa diedit (paragraf mengikuti urutan baca, judul dari ukuran huruf
    besar, tabel sederhana dipertahankan); tata letak visual tidak
    dijanjikan sama persis dengan PDF aslinya.
    """
    _buka_pdf(raw)  # validasi: rusak / terkunci / lewat batas halaman
    if pymupdf is None:
        raise ConvertError("alat convert ini lagi tidak tersedia, "
                           "coba lagi nanti ya")
    try:
        from docx import Document
    except Exception:
        raise ConvertError("alat convert ini lagi tidak tersedia, "
                           "coba lagi nanti ya")
    try:
        dok = pymupdf.open(stream=raw, filetype="pdf")
    except Exception:
        raise ConvertError(
            "file PDF-nya tidak bisa dibaca, kemungkinan rusak. "
            "Coba file lain ya")
    doc = Document()
    total_kata = 0
    try:
        for hal in dok:
            # Kumpulkan item per halaman: (y, jenis, muatan), urut baca.
            item = []
            tabel_bbox = []
            try:
                for tab in hal.find_tables().tables:
                    tabel_bbox.append(tab.bbox)
                    item.append((tab.bbox[1], "tabel", tab))
            except Exception:
                pass

            def _di_tabel(x0, y0, x1, y1):
                for (tx0, ty0, tx1, ty1) in tabel_bbox:
                    if x0 >= tx0 - 2 and y0 >= ty0 - 2 \
                            and x1 <= tx1 + 2 and y1 <= ty1 + 2:
                        return True
                return False

            try:
                d = hal.get_text("dict")
            except Exception:
                d = {"blocks": []}
            for blok in d.get("blocks", []):
                if blok.get("type") != 0:
                    continue
                bx0, by0, bx1, by1 = blok["bbox"]
                if _di_tabel(bx0, by0, bx1, by1):
                    continue
                teks_baris = []
                maks_huruf = 0.0
                for baris in blok.get("lines", []):
                    potong = []
                    for span in baris.get("spans", []):
                        potong.append(span.get("text", ""))
                        maks_huruf = max(maks_huruf,
                                         float(span.get("size", 0)))
                    teks_baris.append("".join(potong).strip())
                teks = " ".join(t for t in teks_baris if t).strip()
                if teks:
                    item.append((by0, "judul" if maks_huruf >= 15 else "teks",
                                 teks))
            item.sort(key=lambda t: t[0])
            for _y, jenis, muatan in item:
                if jenis == "tabel":
                    try:
                        baris2 = muatan.extract() or []
                    except Exception:
                        baris2 = []
                    baris2 = [r for r in baris2 if r]
                    if not baris2:
                        continue
                    kolom = max(len(r) for r in baris2)
                    tab = doc.add_table(rows=0, cols=kolom)
                    tab.style = "Table Grid"
                    for r in baris2:
                        sel2 = tab.add_row().cells
                        for i in range(kolom):
                            sel2[i].text = (r[i] or "") if i < len(r) else ""
                        total_kata += len(" ".join(
                            (c or "") for c in r).split())
                elif jenis == "judul":
                    doc.add_heading(muatan, level=2)
                    total_kata += len(muatan.split())
                else:
                    doc.add_paragraph(muatan)
                    total_kata += len(muatan.split())
    finally:
        dok.close()
    if total_kata < 10:
        raise ConvertError(
            "PDF ini tampaknya hasil scan gambar, jadi teksnya tidak "
            "bisa diambil. Coba PDF yang teksnya bisa dipilih ya")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def nama_hasil(nama_asli, ekstensi_baru):
    """Bangun nama file hasil yang aman dari nama file asli."""
    dasar = (nama_asli or "dokumen")
    dasar = dasar.replace("\\", "/").rsplit("/", 1)[-1]
    dasar = dasar.rsplit(".", 1)[0].strip() or "dokumen"
    dasar = re.sub(r"[^A-Za-z0-9 _().-]+", "", dasar).strip() or "dokumen"
    return f"{dasar}.{ekstensi_baru}"
