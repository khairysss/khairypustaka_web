"""Template proposal/skripsi DOCX siap isi — netral jurusan.

Dua varian: kuantitatif & kualitatif. Struktur BAB mengikuti format umum
perguruan tinggi Indonesia; sesuaikan dengan pedoman kampus masing-masing.
"""
import logging
from io import BytesIO

log = logging.getLogger(__name__)

TYPES = {
    "kuantitatif": "Kuantitatif",
    "kualitatif": "Kualitatif",
}

_SECTIONS_KUANT = [
    ("HALAMAN JUDUL", ["[Judul penelitian — maksimal 20 kata, jelas menggambarkan variabel yang diteliti]",
                       "[Nama Lengkap]",
                       "[NIM]",
                       "[Program Studi / Jurusan]",
                       "[Fakultas]",
                       "[Universitas]",
                       "[Tahun]"]),
    ("ABSTRAK", ["[Ringkasan 150–250 kata: latar belakang singkat, tujuan, metode, hasil utama, simpulan]",
                 "[Kata kunci: 3–5 kata]"]),
    ("KATA PENGANTAR", ["[Ucapan terima kasih — singkat dan formal]"]),
    ("DAFTAR ISI", ["[Daftar isi otomatis — di Word: References > Table of Contents]"]),
    ("BAB I PENDAHULUAN", [
        "1.1 Latar Belakang\n[Tulis fenomena/gap yang melatarbelakangi penelitian. "
        "Dukung dengan data dan referensi. Akhiri dengan alasan mengapa penelitian ini penting.]",
        "1.2 Rumusan Masalah\n[Rumusan dalam bentuk kalimat tanya, spesifik dan terukur. Contoh: "
        "\"Apakah ada hubungan antara X dan Y pada ...?\"]",
        "1.3 Tujuan Penelitian\n1.3.1 Tujuan Umum\n1.3.2 Tujuan Khusus\n[Tuliskan tujuan yang selaras dengan rumusan masalah.]",
        "1.4 Manfaat Penelitian\n1.4.1 Manfaat Teoretis\n1.4.2 Manfaat Praktis\n",
        "1.5 Keaslian Penelitian\n[Tabel perbandingan dengan 3–5 penelitian terdahulu: penulis/tahun, judul, "
        "metode, hasil, dan perbedaan dengan penelitianmu. Bisa dibuat otomatis via /matriks di bot.]",
    ]),
    ("BAB II TINJAUAN PUSTAKA", [
        "2.1 Kajian Teori\n[Uraikan teori/konsep tiap variabel: definisi, dimensi/indikator, "
        "faktor yang memengaruhi — masing-masing didukung referensi.]",
        "2.2 Penelitian Terdahulu\n[Ringkasan penelitian sejenis + posisikan penelitianmu.]",
        "2.3 Kerangka Teori / Kerangka Konsep\n[Bagan alur hubungan antarvariabel + penjelasan narasi.]",
        "2.4 Hipotesis\n[Jawaban sementara atas rumusan masalah. Contoh: \"Ada hubungan antara X dan Y ...\"]",
    ]),
    ("BAB III METODE PENELITIAN", [
        "3.1 Desain Penelitian\n[Jenis/desain: mis. cross-sectional, kohort, eksperimen, survei.]",
        "3.2 Lokasi dan Waktu Penelitian",
        "3.3 Populasi dan Sampel\n[Populasi, teknik sampling, kriteria inklusi/eksklusi, besar sampel + cara perhitungan.]",
        "3.4 Variabel Penelitian\n3.4.1 Variabel Bebas (Independen)\n3.4.2 Variabel Terikat (Dependen)\n"
        "3.4.3 Definisi Operasional\n[Tabel: variabel | definisi operasional | cara ukur | alat ukur | skala data]",
        "3.5 Instrumen Penelitian\n[Jenis instrumen, uji validitas & reliabilitas bila memakai kuesioner.]",
        "3.6 Teknik Pengumpulan Data\n[Alur/prosedur pengambilan data langkah demi langkah.]",
        "3.7 Teknik Analisis Data\n[Uji statistik yang dipakai + alasan pemilihannya. "
        "Bingung pilih uji? Pakai /statistik di bot.]",
        "3.8 Etika Penelitian\n[Ethical clearance, informed consent, kerahasiaan data.]",
    ]),
    ("BAB IV HASIL DAN PEMBAHASAN", [
        "4.1 Gambaran Umum Lokasi Penelitian",
        "4.2 Hasil Penelitian\n4.2.1 Karakteristik Responden (analisis univariat)\n"
        "4.2.2 Analisis Bivariat/Multivariat\n[Sajikan tabel + narasi: apa yang signifikan, apa yang tidak.]",
        "4.3 Pembahasan\n[Jelaskan makna hasil, bandingkan dengan teori & penelitian terdahulu, "
        "akui keterbatasan penelitian.]",
    ]),
    ("BAB V PENUTUP", [
        "5.1 Simpulan\n[Jawab tiap rumusan masalah secara ringkas.]",
        "5.2 Saran\n[Saran praktis dan saran untuk penelitian selanjutnya.]",
    ]),
    ("DAFTAR PUSTAKA", ["[Minimal 20–30 referensi primer (jurnal 5–10 tahun terakhir). "
                        "Gunakan /koleksi + /export di bot untuk menyusun otomatis.]"]),
    ("LAMPIRAN", ["[Instrumen/kuesioner, surat izin, ethical clearance, dokumentasi, output analisis]"]),
]

_SECTIONS_KUAL = [
    ("HALAMAN JUDUL", _SECTIONS_KUANT[0][1]),
    ("ABSTRAK", _SECTIONS_KUANT[1][1]),
    ("KATA PENGANTAR", _SECTIONS_KUANT[2][1]),
    ("DAFTAR ISI", _SECTIONS_KUANT[3][1]),
    ("BAB I PENDAHULUAN", [
        "1.1 Latar Belakang\n[Fenomena yang diamati + kesenjangan pengetahuan.]",
        "1.2 Rumusan Masalah / Fokus Penelitian\n[Bentuk pertanyaan terbuka. Contoh: "
        "\"Bagaimana pengalaman ... dalam ...?\"]",
        "1.3 Tujuan Penelitian\n[Memahami/mendeskripsikan fenomena secara mendalam.]",
        "1.4 Manfaat Penelitian",
    ]),
    ("BAB II TINJAUAN PUSTAKA", [
        "2.1 Kajian Teori / Konsep\n[Teori yang melandasi, tanpa harus kaku seperti kuantitatif.]",
        "2.2 Penelitian Terdahulu",
        "2.3 Kerangka Pikir\n[Alur pikir penelitian dalam bentuk bagan + narasi.]",
    ]),
    ("BAB III METODE PENELITIAN", [
        "3.1 Desain Penelitian\n[Jenis: fenomenologi, studi kasus, etnografi, grounded theory, dsb.]",
        "3.2 Lokasi dan Waktu Penelitian",
        "3.3 Subjek / Informan Penelitian\n[Kriteria pemilihan informan + teknik purposive/snowball.]",
        "3.4 Teknik Pengumpulan Data\n[Wawancara mendalam, observasi, dokumentasi, FGD.]",
        "3.5 Instrumen Penelitian\n[Pedoman wawancara (interview guide), peneliti sebagai instrumen utama.]",
        "3.6 Teknik Analisis Data\n[Reduksi data, penyajian data, penarikan simpulan (model Miles & Huberman) "
        "atau analisis tematik.]",
        "3.7 Keabsahan Data\n[Triangulasi, member check, audit trail, perpanjangan pengamatan.]",
        "3.8 Etika Penelitian",
    ]),
    ("BAB IV HASIL DAN PEMBAHASAN", [
        "4.1 Gambaran Umum Lokasi & Informan",
        "4.2 Hasil Penelitian\n[Sajikan per tema/kategori temuan, lengkapi kutipan verbatim informan.]",
        "4.3 Pembahasan\n[Hubungkan temuan dengan teori & penelitian terdahulu.]",
    ]),
    ("BAB V PENUTUP", _SECTIONS_KUANT[7][1]),
    ("DAFTAR PUSTAKA", _SECTIONS_KUANT[8][1]),
    ("LAMPIRAN", ["[Pedoman wawancara, transkrip, dokumentasi, surat izin]"]),
]


def build_template_docx(jenis="kuantitatif"):
    """Return bytes .docx template. jenis: 'kuantitatif' | 'kualitatif'."""
    from docx import Document
    from docx.shared import Pt, RGBColor

    jenis = jenis if jenis in TYPES else "kuantitatif"
    sections = _SECTIONS_KUANT if jenis == "kuantitatif" else _SECTIONS_KUAL

    doc = Document()
    doc.add_heading(f"Template Proposal/Skripsi ({TYPES[jenis]})", level=1)
    p = doc.add_paragraph()
    r = p.add_run("Ganti semua teks dalam [kurung siku] dengan tulisanmu. "
                  "Sesuaikan dengan pedoman penulisan kampus masing-masing.")
    r.font.size = Pt(10)
    r.font.italic = True
    r.font.color.rgb = RGBColor(0x66, 0x66, 0x66)

    for title, bodies in sections:
        doc.add_heading(title, level=2)
        for b in bodies:
            par = doc.add_paragraph()
            run = par.add_run(b)
            run.font.size = Pt(11)
            if b.startswith("["):
                run.font.italic = True
                run.font.color.rgb = RGBColor(0x55, 0x55, 0x55)

    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()
