"""Fitur /tanya — tanya jawab bebas seputar skripsi/tugas akhir.

Cara kerja:
1. Pertanyaan dicocokkan ke basis pengetahuan (KB) lewat skor kata kunci.
2. Bila skor cocok rendah dan env GEMINI_API_KEY tersedia, pertanyaan
   diteruskan ke Gemini (opsional, gratis ada kuota harian).
3. Pertanyaan yang tidak terjawab dicatat ke data/tanya_unanswered.jsonl
   agar pemilik bot bisa menambah/memperbaiki KB.
"""
import json
import logging
import os
import re
import time

import dbstore  # Postgres (DATABASE_URL) atau file lokal, lihat dbstore.py

log = logging.getLogger(__name__)

KB = [
    dict(
        id="jenis_penelitian",
        title="Jenis-jenis penelitian",
        keywords=["jenis penelitian", "macam penelitian", "macam-macam penelitian",
                  "pendekatan penelitian", "metode penelitian secara umum",
                  "penelitian apa saja"],
        answer=(
            "❓ <b>Jenis-jenis penelitian</b>\n\n"
            "Secara garis besar ada 3 pendekatan:\n\n"
            "<b>1. Kuantitatif</b> — datanya angka, diuji secara statistik. "
            "Contoh: survei, eksperimen, korelasional.\n"
            "<b>2. Kualitatif</b> — datanya kata/kalimat, digali maknanya secara mendalam. "
            "Contoh: wawancara mendalam, studi kasus, fenomenologi.\n"
            "<b>3. Campuran (mixed methods)</b> — gabungan keduanya.\n\n"
            "Selain itu ada <b>R&amp;D</b> (mengembangkan produk, mis. media/alat), "
            "dan <b>penelitian tindakan</b> (action research, mis. PTK di kelas).\n\n"
            "Pilih pendekatan yang <i>cocok dengan rumusan masalahmu</i>, bukan yang "
            "paling keren. Mau panduan pilih uji statistiknya? Coba /statistik."
        ),
    ),
    dict(
        id="kuantitatif",
        title="Penelitian kuantitatif",
        keywords=["penelitian kuantitatif", "kuantitatif itu apa", "kuantitatif adalah",
                  "ciri kuantitatif", "metode kuantitatif"],
        answer=(
            "❓ <b>Penelitian kuantitatif</b>\n\n"
            "Penelitian yang datanya berupa <b>angka</b> dan dianalisis dengan statistik.\n\n"
            "<b>Ciri-cirinya:</b>\n"
            "• Ada variabel yang diukur (bebas &amp; terikat)\n"
            "• Sampel relatif besar &amp; dipilih acak bila memungkinkan\n"
            "• Instrumen terstandar (kuesioner/skala/tes)\n"
            "• Hasilnya digeneralisasi ke populasi\n\n"
            "<b>Desain yang umum:</b> deskriptif, korelasional, komparatif, eksperimen.\n\n"
            "Contoh judul: <i>Pengaruh X terhadap Y pada ...</i> atau "
            "<i>Hubungan antara A dan B ...</i>\n\n"
            "Butuh bantu pilih uji statistiknya? Pakai /statistik — dipandu sampai dapat jawabannya."
        ),
    ),
    dict(
        id="kualitatif",
        title="Penelitian kualitatif",
        keywords=["penelitian kualitatif", "kualitatif itu apa", "kualitatif adalah",
                  "ciri kualitatif", "metode kualitatif", "fenomenologi",
                  "studi kasus", "etnografi"],
        answer=(
            "❓ <b>Penelitian kualitatif</b>\n\n"
            "Penelitian yang datanya berupa <b>kata, kalimat, dan perilaku</b>, "
            "digali maknanya secara mendalam — bukan dihitung.\n\n"
            "<b>Desain yang umum:</b>\n"
            "• <b>Fenomenologi</b> — menggali pengalaman hidup partisipan\n"
            "• <b>Studi kasus</b> — mengupas tuntas satu kasus/unit\n"
            "• <b>Etnografi</b> — memahami budaya/kelompok masyarakat\n"
            "• <b>Grounded theory</b> — membangun teori dari data lapangan\n\n"
            "<b>Pengumpulan data:</b> wawancara mendalam, observasi, dokumentasi.\n"
            "<b>Sampel:</b> kecil &amp; purposif, berhenti saat data jenuh (saturasi).\n\n"
            "Analisisnya pakai model Miles &amp; Huberman: reduksi → penyajian → penarikan kesimpulan. "
            "Butuh template proposal kualitatif? Coba /template."
        ),
    ),
    dict(
        id="eksperimen",
        title="Penelitian eksperimen",
        keywords=["eksperimen", "experimental", "quasi experiment", "eksperimen semu",
                  "pretest posttest", "kelompok kontrol", "true experimental"],
        answer=(
            "❓ <b>Penelitian eksperimen</b>\n\n"
            "Menguji <b>pengaruh perlakuan (treatment)</b> terhadap suatu hasil, "
            "dengan membandingkan kelompok.\n\n"
            "<b>3 bentuk utama:</b>\n"
            "• <b>Pra-eksperimen</b> — satu kelompok, pretest → treatment → posttest "
            "(tanpa kelompok pembanding)\n"
            "• <b>Quasi experiment</b> — ada kelompok kontrol, tapi pembagian "
            "kelompoknya tidak acak (mis. pakai kelas yang sudah ada)\n"
            "• <b>True experiment</b> — ada kelompok kontrol <i>dan</i> pembagian "
            "acak (random assignment)\n\n"
            "<b>Syarat sahih:</b> kontrol variabel pengganggu, instrumen yang sama "
            "untuk pre &amp; post, dan uji beda yang tepat (uji-t/ANOVA). "
            "Uji mana yang cocok untuk datamu? Cek /statistik."
        ),
    ),
    dict(
        id="variabel",
        title="Variabel penelitian",
        keywords=["variabel", "variabel bebas", "variabel terikat", "variabel independen",
                  "variabel dependen", "variabel kontrol", "variabel moderator",
                  "operasionalisasi", "definisi operasional"],
        answer=(
            "❓ <b>Variabel penelitian</b>\n\n"
            "• <b>Variabel bebas (X)</b> — yang diduga <i>mempengaruhi</i>. "
            "Contoh: intensitas belajar.\n"
            "• <b>Variabel terikat (Y)</b> — yang <i>dipengaruhi</i>. Contoh: nilai ujian.\n"
            "• <b>Variabel kontrol</b> — dijaga konstan agar tidak mengacaukan.\n"
            "• <b>Variabel moderator/intervening</b> — memperkuat/memperlemah atau "
            "menjembatani hubungan X→Y.\n\n"
            "<b>Definisi operasional</b> wajib dibuat: jelaskan <i>cara mengukur</i> "
            "tiap variabel (indikator, skala, alat ukur). Misal: "
            "<i>\"Motivasi diukur dengan skala Likert 20 butir...\"</i>\n\n"
            "Jumlah variabel bebasmu menentukan uji statistik: 1 variabel → uji bivariat, "
            "2+ variabel → regresi berganda. Panduan lengkapnya di /statistik."
        ),
    ),
    dict(
        id="hipotesis",
        title="Hipotesis penelitian",
        keywords=["hipotesis", "hipotesa", "h0", "h1", "ha", "ho", "hipotesis nol",
                  "hipotesis alternatif"],
        answer=(
            "❓ <b>Hipotesis penelitian</b>\n\n"
            "Dugaan sementara atas jawaban rumusan masalah, yang akan diuji dengan data.\n\n"
            "• <b>H0 (hipotesis nol)</b> — menyatakan <i>tidak ada</i> pengaruh/perbedaan/hubungan. "
            "Contoh: <i>\"Tidak ada pengaruh X terhadap Y.\"</i>\n"
            "• <b>Ha (hipotesis alternatif)</b> — menyatakan <i>ada</i> pengaruh/perbedaan/hubungan. "
            "Contoh: <i>\"Ada pengaruh X terhadap Y.\"</i>\n\n"
            "<b>Aturan baca hasil:</b> bila p-value &lt; 0,05 → <b>H0 ditolak, Ha diterima</b>. "
            "Bila p ≥ 0,05 → <b>H0 diterima</b> (bukan berarti \"terbukti tidak ada\", "
            "tapi buktinya belum cukup).\n\n"
            "Hipotesis hanya dipakai di penelitian kuantitatif inferensial — "
            "penelitian deskriptif &amp; kualitatif umumnya tidak pakai hipotesis."
        ),
    ),
    dict(
        id="populasi_sampel",
        title="Populasi dan sampel",
        keywords=["populasi", "sampel", "sample", "responden", "subjek penelitian",
                  "populasi dan sampel", "populasi adalah", "sampel adalah"],
        answer=(
            "❓ <b>Populasi dan sampel</b>\n\n"
            "• <b>Populasi</b> — seluruh subjek yang ingin digeneralisasi hasilnya. "
            "Contoh: seluruh mahasiswa angkatan 2023 di kampusmu.\n"
            "• <b>Sampel</b> — sebagian populasi yang benar-benar diteliti. "
            "Hasil dari sampel digeneralisasi ke populasi.\n"
            "• <b>Responden/informan</b> — orang yang mengisi kuesioner (kuantitatif) "
            "atau diwawancarai (kualitatif).\n\n"
            "<b>Syarat sampel yang baik:</b> representatif (mewakili populasi) dan "
            "jumlahnya memadai. Cara menentukan jumlahnya: rumus Slovin, tabel "
            "Krejcie-Morgan, atau aturan praktis tiap uji statistik.\n\n"
            "Tanya aku: <i>\"rumus slovin\"</i>, <i>\"teknik sampling\"</i>, atau "
            "<i>\"berapa jumlah sampel\"</i>."
        ),
    ),
    dict(
        id="sampling_probability",
        title="Teknik sampling probability",
        keywords=["probability sampling", "random sampling", "acak", "sampel acak",
                  "stratified", "strata", "cluster", "sistematis", "systematic",
                  "simple random", "teknik sampling"],
        answer=(
            "❓ <b>Probability sampling</b> (setiap anggota populasi punya peluang terpilih)\n\n"
            "• <b>Simple random</b> — diundi langsung dari daftar populasi. "
            "Cocok bila populasi homogen.\n"
            "• <b>Stratified</b> — populasi dibagi strata (mis. per jurusan/angkatan), "
            "lalu diacak di tiap strata. Hasil lebih representatif.\n"
            "• <b>Cluster</b> — diacak per <i>kelompok</i> (mis. per kelas/sekolah), "
            "bukan per individu. Hemat biaya untuk populasi tersebar.\n"
            "• <b>Sistematis</b> — pilih tiap kelipatan ke-n dari daftar "
            "(mis. tiap nomor kelipatan 5).\n\n"
            "Teknik ini <b>wajib</b> bila kamu ingin menggeneralisasi hasil ke populasi. "
            "Untuk teknik non-acak (purposive, snowball, dll.), tanya: "
            "<i>\"sampling non probability\"</i>."
        ),
    ),
    dict(
        id="sampling_nonprobability",
        title="Teknik sampling non-probability",
        keywords=["non probability", "nonprobability", "non-probability", "purposive",
                  "snowball", "accidental", "quota", "sampling jenuh", "sampel jenuh",
                  "sensus", "total sampling"],
        answer=(
            "❓ <b>Non-probability sampling</b> (pemilihan tidak acak)\n\n"
            "• <b>Purposive</b> — dipilih berdasarkan kriteria tertentu. "
            "Standar di penelitian kualitatif.\n"
            "• <b>Snowball</b> — dari satu informan merujuk ke informan lain. "
            "Cocok untuk populasi sulit dijangkau.\n"
            "• <b>Accidental/convenience</b> — siapa yang kebetulan ditemui. "
            "Paling mudah, tapi paling lemah generalisasinya.\n"
            "• <b>Quota</b> — ditentukan jatah per kategori.\n"
            "• <b>Jenuh/sensus</b> — seluruh populasi dijadikan sampel "
            "(bila populasi &lt; 100).\n\n"
            "Catatan: hasilnya <b>tidak bisa digeneralisasi</b> sekuat probability "
            "sampling — tulis keterbatasan ini di skripsimu dengan jujur."
        ),
    ),
    dict(
        id="slovin",
        title="Rumus Slovin",
        keywords=["slovin", "rumus sampel", "jumlah sampel", "berapa sampel",
                  "besar sampel", "hitung sampel", "ukuran sampel"],
        answer=(
            "❓ <b>Rumus Slovin</b>\n\n"
            "Menentukan jumlah sampel minimal dari populasi yang diketahui jumlahnya (N):\n\n"
            "<b>n = N / (1 + N × e²)</b>\n\n"
            "• <b>n</b> = jumlah sampel\n"
            "• <b>N</b> = jumlah populasi\n"
            "• <b>e</b> = margin of error (umumnya 0,05 atau 0,10)\n\n"
            "<b>Contoh:</b> populasi 500 mahasiswa, e = 0,05\n"
            "n = 500 / (1 + 500 × 0,0025) = 500 / 2,25 = <b>222 responden</b>\n\n"
            "Tips: bila populasi tidak diketahui jumlahnya, pakai rumus lain "
            "(mis. Lemeshow) atau aturan praktis: minimal 30 untuk uji korelasi, "
            "dan untuk regresi ≈ 15–20 responden per variabel bebas. "
            "Alternatif cepat: tabel Krejcie-Morgan — tanya <i>\"krejcie morgan\"</i>."
        ),
    ),
    dict(
        id="krejcie",
        title="Tabel Krejcie-Morgan",
        keywords=["krejcie", "morgan", "tabel sampel", "tabel krejcie"],
        answer=(
            "❓ <b>Tabel Krejcie-Morgan</b>\n\n"
            "Tabel siap pakai untuk menentukan jumlah sampel berdasarkan jumlah "
            "populasi (taraf kepercayaan 95%, margin of error 5%). "
            "Tinggal cari N-mu, baca n-nya — tanpa hitung.\n\n"
            "<b>Contoh nilai populer:</b>\n"
            "• N = 100 → n = 80\n"
            "• N = 200 → n = 132\n"
            "• N = 300 → n = 169\n"
            "• N = 500 → n = 217\n"
            "• N = 1000 → n = 278\n"
            "• N = 10000 → n = 370\n\n"
            "Tabel lengkapnya mudah dicari di internet dengan kata kunci "
            "<i>\"tabel Krejcie Morgan\"</i> — lampirkan sebagai dasar pengambilan "
            "sampel di Bab 3. Untuk populasi tak diketahui jumlahnya, "
            "tanya <i>\"rumus slovin\"</i>."
        ),
    ),
    dict(
        id="sampel_kualitatif",
        title="Sampel penelitian kualitatif",
        keywords=["sampel kualitatif", "informan", "jumlah informan", "berapa informan",
                  "saturasi data", "data jenuh"],
        answer=(
            "❓ <b>Sampel penelitian kualitatif</b>\n\n"
            "Kualitatif <b>tidak</b> pakai rumus Slovin — jumlah informan ditentukan "
            "oleh <b>kejenuhan data (saturasi)</b>: berhenti menambah informan saat "
            "jawaban sudah berulang dan tidak ada informasi baru.\n\n"
            "<b>Patokan umum:</b>\n"
            "• Fenomenologi: 5–25 informan\n"
            "• Studi kasus: 1 kasus dengan beberapa sumber data\n"
            "• Wawancara mendalam: sering cukup 5–15 informan yang benar-benar paham topik\n\n"
            "Teknik pemilihannya <b>purposive</b> (berdasarkan kriteria) atau "
            "<b>snowball</b>. Yang penting: jelaskan <i>kriterianya</i> dengan jelas "
            "di Bab 3, bukan sekadar jumlahnya."
        ),
    ),
    dict(
        id="instrumen",
        title="Instrumen / alat ukur",
        keywords=["instrumen", "alat ukur", "kuesioner", "angket", "wawancara",
                  "pedoman wawancara", "observasi", "dokumentasi", "pengumpulan data",
                  "teknik pengumpulan data", "tes"],
        answer=(
            "❓ <b>Instrumen / alat ukur</b>\n\n"
            "<b>Kuantitatif:</b>\n"
            "• <b>Kuesioner/angket</b> — paling umum; pakai skala Likert\n"
            "• <b>Tes</b> — untuk mengukur kemampuan/hasil belajar\n"
            "• <b>Lembar observasi terstruktur</b> — ceklis perilaku terukur\n"
            "• <b>Dokumentasi</b> — data sekunder (nilai, arsip, laporan)\n\n"
            "<b>Kualitatif:</b>\n"
            "• <b>Pedoman wawancara</b> — daftar pertanyaan terbuka\n"
            "• <b>Pedoman observasi &amp; dokumentasi</b>\n"
            "• Peneliti sendiri adalah instrumen utama\n\n"
            "<b>Wajib:</b> susun <b>kisi-kisi instrumen</b> dulu (variabel → indikator → "
            "butir pertanyaan), lalu uji <b>validitas &amp; reliabilitas</b> sebelum "
            "dipakai. Tanya: <i>\"uji validitas\"</i> atau <i>\"uji reliabilitas\"</i>."
        ),
    ),
    dict(
        id="likert",
        title="Skala Likert",
        keywords=["likert", "skala likert", "skala", "skor", "sangat setuju",
                  "semantic differential", "skala guttman"],
        answer=(
            "❓ <b>Skala Likert</b>\n\n"
            "Skala sikap paling populer di kuesioner skripsi. Responden memilih "
            "tingkat persetujuan per pernyataan:\n\n"
            "<b>5 pilihan (paling umum):</b>\n"
            "5 = Sangat Setuju, 4 = Setuju, 3 = Netral/Ragu-ragu, "
            "2 = Tidak Setuju, 1 = Sangat Tidak Setuju\n\n"
            "<b>Aturan penting:</b>\n"
            "• Pernyataan <b>negatif (unfavourable)</b> skornya <i>dibalik</i> "
            "(SS=1 ... STS=5) — jangan lupa reverse scoring!\n"
            "• Hindari pernyataan ganda dalam satu butir (<i>double-barreled</i>)\n"
            "• Minimal 3–5 butir per indikator agar reliabel\n\n"
            "Varian lain: skala Guttman (Ya/Tidak) dan diferensial semantik "
            "(mis. Baik 5-4-3-2-1 Buruk)."
        ),
    ),
    dict(
        id="validitas",
        title="Uji validitas",
        keywords=["validitas", "valid", "uji valid", "r hitung", "r tabel",
                  "corrected item", "butir valid", "kuesioner valid"],
        answer=(
            "❓ <b>Uji validitas</b>\n\n"
            "Menguji apakah tiap <b>butir pertanyaan benar-benar mengukur</b> "
            "apa yang seharusnya diukur.\n\n"
            "<b>Cara umum (korelasi Product Moment):</b>\n"
            "1. Uji coba kuesioner ke ±30 responden\n"
            "2. Hitung korelasi tiap butir dengan skor total → <b>r hitung</b>\n"
            "3. Bandingkan dengan <b>r tabel</b> (df = n−2, α = 0,05)\n"
            "4. Butir <b>valid</b> bila r hitung &gt; r tabel\n\n"
            "Butir yang tidak valid → <b>dibuang atau direvisi</b>, lalu uji ulang. "
            "Di SPSS: <i>Analyze → Correlate → Bivariate</i> (atau pakai "
            "<i>Corrected Item-Total Correlation</i> ≥ 0,30).\n\n"
            "Setelah valid, lanjut uji <b>reliabilitas</b> — tanya "
            "<i>\"uji reliabilitas\"</i>."
        ),
    ),
    dict(
        id="reliabilitas",
        title="Uji reliabilitas",
        keywords=["reliabilitas", "reliabel", "reliability", "cronbach", "alpha",
                  "koefisien reliabilitas", "uji reliabel"],
        answer=(
            "❓ <b>Uji reliabilitas</b>\n\n"
            "Menguji apakah instrumen <b>konsisten</b> — bila dipakai berulang, "
            "hasilnya stabil.\n\n"
            "<b>Ukuran paling umum: Cronbach's Alpha</b>\n"
            "• &gt; 0,90 → sangat baik\n"
            "• 0,70–0,90 → baik (batas umum kelayakan: <b>≥ 0,70</b>)\n"
            "• 0,60–0,70 → cukup (masih diterima untuk penelitian eksploratif)\n"
            "• &lt; 0,60 → tidak reliabel, instrumen perlu diperbaiki\n\n"
            "Di SPSS: <i>Analyze → Scale → Reliability Analysis</i>, "
            "masukkan butir-butir yang sudah <b>lolos uji validitas</b>.\n\n"
            "Ingat urutannya: <b>validitas dulu, baru reliabilitas</b> — "
            "instrumen yang tidak valid tidak ada gunanya walau reliabel."
        ),
    ),
    dict(
        id="pilih_uji",
        title="Memilih uji statistik",
        keywords=["regresi", "regresi berganda", "regresi linear", "regresi logistik",
                  "uji regresi", "anova", "anava", "uji t", "uji-t", "chi square",
                  "korelasi pearson", "uji beda", "uji hubungan", "mann whitney",
                  "wilcoxon", "kruskal", "uji nonparametrik", "uji parametrik",
                  "spearman", "uji chi", "membandingkan", "bandingin",
                  "perbandingan", "beda"],
        answer=(
            "❓ <b>Memilih uji statistik</b>\n\n"
            "Pertanyaan ini pas banget dijawab lewat fitur <b>/statistik</b> — "
            "kamu dipandu tanya-jawab sampai dapat nama uji yang tepat "
            "(termasuk cara baca hasil &amp; contoh penulisannya).\n\n"
            "Bocoran cepatnya:\n"
            "• 1 variabel bebas + data numerik → korelasi Pearson / uji-t / ANOVA\n"
            "• 1 variabel bebas + data kategorik → chi-square / Spearman\n"
            "• <b>2+ variabel bebas</b> → <b>regresi linear berganda</b> (Y numerik) "
            "atau <b>regresi logistik berganda</b> (Y kategorik)\n"
            "• Asumsi gagal → versi non-parametrik (Mann-Whitney, Wilcoxon, Kruskal-Wallis)\n\n"
            "Langsung coba: /statistik — atau dari menu 🎓 Tools Skripsi → 📊 Uji Statistik."
        ),
    ),
    dict(
        id="analisis_data",
        title="Analisis data kuantitatif",
        keywords=["analisis data", "analisa data", "teknik analisis", "analisis deskriptif",
                  "statistik deskriptif", "inferensial", "olah data", "analisis kuantitatif"],
        answer=(
            "❓ <b>Analisis data kuantitatif</b>\n\n"
            "<b>1. Statistik deskriptif</b> — menggambarkan data: rata-rata (mean), "
            "median, modus, standar deviasi, frekuensi, persentase.\n\n"
            "<b>2. Statistik inferensial</b> — menguji hipotesis &amp; generalisasi:\n"
            "• Uji beda: uji-t, ANOVA\n"
            "• Uji hubungan: korelasi Pearson/Spearman\n"
            "• Uji pengaruh: regresi sederhana &amp; berganda\n"
            "• Uji asosiasi kategorik: chi-square\n\n"
            "<b>Alur kerja yang benar:</b>\n"
            "1. Uji validitas &amp; reliabilitas instrumen\n"
            "2. Uji prasyarat/asumsi (normalitas, homogenitas, linearitas)\n"
            "3. Uji hipotesis sesuai jenis data\n"
            "4. Interpretasi: p-value, koefisien, dan makna praktisnya\n\n"
            "Bingung pilih uji yang mana? /statistik memandumu lewat tanya-jawab."
        ),
    ),
    dict(
        id="asumsi_klasik",
        title="Uji asumsi klasik / prasyarat",
        keywords=["asumsi klasik", "uji asumsi", "normalitas", "uji normalitas",
                  "homogenitas", "linearitas", "multikolinearitas", "heteroskedastisitas",
                  "kolmogorov", "shapiro", "levene", "uji prasyarat"],
        answer=(
            "❓ <b>Uji asumsi klasik / uji prasyarat</b>\n\n"
            "Syarat yang harus dipenuhi <i>sebelum</i> uji parametrik (uji-t, ANOVA, regresi):\n\n"
            "• <b>Normalitas</b> — data berdistribusi normal. Uji: Kolmogorov-Smirnov "
            "(n &gt; 50) atau Shapiro-Wilk (n &lt; 50). Syarat: p &gt; 0,05.\n"
            "• <b>Homogenitas</b> — varians antar kelompok sama. Uji: Levene. "
            "Syarat: p &gt; 0,05.\n"
            "• <b>Linearitas</b> — hubungan X–Y linear (untuk regresi/korelasi Pearson). "
            "Lihat <i>deviation from linearity</i> p &gt; 0,05.\n"
            "• <b>Multikolinearitas</b> — antar variabel bebas tidak saling berkorelasi "
            "kuat. Syarat: VIF &lt; 10.\n"
            "• <b>Heteroskedastisitas</b> — residual menyebar acak (scatterplot/Glejser), "
            "tidak membentuk pola.\n\n"
            "Bila asumsi <b>gagal</b>, solusinya: transformasi data atau pakai uji "
            "non-parametrik (Mann-Whitney, Wilcoxon, Kruskal-Wallis, Spearman)."
        ),
    ),
    dict(
        id="analisis_kualitatif",
        title="Analisis data kualitatif",
        keywords=["analisis kualitatif", "analisa kualitatif", "miles huberman",
                  "reduksi data", "triangulasi", "koding", "coding", "analisis kualitatif"],
        answer=(
            "❓ <b>Analisis data kualitatif</b> (model Miles &amp; Huberman)\n\n"
            "<b>1. Reduksi data</b> — merangkum, memilih hal pokok, membuang yang "
            "tidak relevan dari transkrip/catatan lapangan.\n"
            "<b>2. Penyajian data</b> — menyusun dalam narasi, matriks, bagan, atau "
            "jaringan agar polanya terlihat.\n"
            "<b>3. Penarikan kesimpulan &amp; verifikasi</b> — merumuskan temuan, "
            "lalu memverifikasi dengan kembali ke data.\n\n"
            "<b>Teknik pendukung:</b>\n"
            "• <b>Koding</b> — memberi label pada potongan data (open → axial → selective)\n"
            "• <b>Triangulasi</b> — cek silang sumber/teknik/waktu agar temuan kredibel\n"
            "• <b>Member check</b> — konfirmasi hasil ke informan\n\n"
            "Uji keabsahan data kualitatif: kredibilitas, transferabilitas, "
            "dependabilitas, konfirmabilitas."
        ),
    ),
    dict(
        id="p_value",
        title="p-value dan signifikansi",
        keywords=["p value", "p-value", "signifikansi", "signifikan", "0,05", "0.05",
                  "alpha", "taraf nyata", "tidak signifikan"],
        answer=(
            "❓ <b>p-value dan signifikansi</b>\n\n"
            "p-value = peluang hasilmu terjadi <i>secara kebetulan</i> bila sebenarnya "
            "tidak ada efek (H0 benar).\n\n"
            "<b>Aturan umum (α = 0,05):</b>\n"
            "• <b>p &lt; 0,05</b> → signifikan → <b>H0 ditolak, Ha diterima</b> ✅\n"
            "• <b>p ≥ 0,05</b> → tidak signifikan → <b>H0 diterima</b>\n\n"
            "<b>Kesalahan umum:</b>\n"
            "• p = 0,000 di SPSS artinya p &lt; 0,001, <b>bukan</b> nol mutlak\n"
            "• \"Tidak signifikan\" ≠ \"tidak ada hubungan\" — mungkin sampel kurang besar\n"
            "• Signifikan statistik ≠ penting secara praktis — laporkan juga "
            "besar efeknya (koefisien korelasi, R², atau selisih rata-rata)\n\n"
            "Cara menulis: <i>\"...berpengaruh signifikan (p = 0,021 &lt; 0,05)...\"</i>"
        ),
    ),
    dict(
        id="latar_belakang",
        title="Menulis latar belakang",
        keywords=["latar belakang", "bab 1", "bab i", "pendahuluan", "cara menulis latar",
                  "latar belakang masalah"],
        answer=(
            "❓ <b>Menulis latar belakang</b>\n\n"
            "Pola <b>piramida terbalik</b>: umum → khusus → masalahmu.\n\n"
            "<b>1. Kondisi ideal</b> — seharusnya seperti apa (teori/kebijakan).\n"
            "<b>2. Kondisi nyata/fakta</b> — ada kesenjangan (data, observasi, studi "
            "pendahuluan). Inilah <i>masalah</i>-nya.\n"
            "<b>3. Dampak</b> — mengapa masalah ini penting &amp; mendesak diteliti.\n"
            "<b>4. Solusi yang ditawarkan</b> — penelitianmu menjawab lewat apa.\n"
            "<b>5. Jembatan</b> — kalimat pengantar ke rumusan masalah.\n\n"
            "<b>Tips:</b> setiap klaim fakta <b>wajib ada sitasi</b> (jurnal/buku/data "
            "resmi). Hindari opini tanpa rujukan. Panjang umum: 4–7 halaman.\n\n"
            "Butuh referensi pendukung? Cari di sini dengan /cari, lalu buat sitasinya "
            "dengan /sitasi."
        ),
    ),
    dict(
        id="rumusan_masalah",
        title="Rumusan masalah, tujuan & manfaat",
        keywords=["rumusan masalah", "tujuan penelitian", "manfaat penelitian",
                  "pertanyaan penelitian", "research question"],
        answer=(
            "❓ <b>Rumusan masalah, tujuan &amp; manfaat</b>\n\n"
            "<b>Rumusan masalah</b> — pertanyaan yang akan dijawab penelitian. "
            "Syarat: jelas, spesifik, bisa diteliti, dan terjawab oleh hasil. "
            "Contoh: <i>\"Apakah ada pengaruh X terhadap Y pada ...?\"</i>\n\n"
            "<b>Tujuan penelitian</b> — rumusan masalah yang diubah jadi pernyataan. "
            "Contoh: <i>\"Untuk mengetahui pengaruh X terhadap Y pada ...\"</i> "
            "(cerminan 1:1 dengan rumusan masalah).\n\n"
            "<b>Manfaat penelitian:</b>\n"
            "• <b>Teoretis</b> — sumbangan bagi pengembangan ilmu\n"
            "• <b>Praktis</b> — manfaat bagi pihak terkait (sekolah, perusahaan, "
            "pemerintah, masyarakat)\n\n"
            "Segitiga emasnya: <b>latar belakang → rumusan → tujuan → hasil → "
            "kesimpulan</b> harus saling menjawab. Kalau kesimpulanmu tidak menjawab "
            "rumusan masalah, ada yang salah."
        ),
    ),
    dict(
        id="bab4",
        title="Menulis Bab 4 (hasil & pembahasan)",
        keywords=["bab 4", "bab iv", "hasil penelitian", "pembahasan", "diskusi",
                  "cara menulis pembahasan", "interpretasi hasil"],
        answer=(
            "❓ <b>Bab 4: Hasil &amp; Pembahasan</b>\n\n"
            "<b>Hasil:</b> sajikan temuan secara objektif — tabel/grafik + narasi "
            "singkat. Jangan campur opini di bagian hasil.\n\n"
            "<b>Pembahasan:</b> bagian tersulit sekaligus terpenting. Pola per temuan:\n"
            "1. <b>Nyatakan temuanmu</b> (apa yang didapat)\n"
            "2. <b>Bandingkan</b> dengan teori &amp; penelitian terdahulu — "
            "mendukung atau bertentangan? (di sinilah /matriks membantumu)\n"
            "3. <b>Jelaskan mengapa</b> bisa begitu (analisis &amp; argumentasimu)\n"
            "4. <b>Implikasinya</b> apa\n\n"
            "<b>Kesalahan umum:</b> hanya mengulang angka tanpa analisis, tidak "
            "membandingkan dengan penelitian lain, dan pembahasan yang tidak "
            "menjawab rumusan masalah."
        ),
    ),
    dict(
        id="plagiarisme",
        title="Plagiarisme & Turnitin",
        keywords=["plagiarisme", "plagiat", "turnitin", "similarity", "kemiripan",
                  "cek plagiasi", "bebas plagiat"],
        answer=(
            "❓ <b>Plagiarisme &amp; Turnitin</b>\n\n"
            "Plagiarisme = memakai karya orang lain tanpa menyebut sumber — "
            "termasuk <b>copy-paste lalu ganti sedikit kata</b>.\n\n"
            "<b>Cara menghindarinya:</b>\n"
            "• <b>Sitasi</b> setiap ide/data/kutipan dari sumber lain (/sitasi bisa bantu)\n"
            "• <b>Parafrase</b> dengan bahasamu sendiri, bukan sekadar tukar sinonim "
            "(coba /parafrase, tapi tetap baca ulang maknanya!)\n"
            "• Kutipan langsung (&lt; 40 kata pakai tanda kutip; panjang pakai blok kutipan)\n"
            "• Tulis daftar pustaka lengkap &amp; konsisten\n\n"
            "<b>Tentang Turnitin:</b> skor similarity umum ditoleransi &lt; 20–30% "
            "(tergantung kampus). Yang dihitung bukan sekadar angka — "
            "<i>self-plagiarism</i> (pakai tulisanmu sendiri tanpa sitasi) dan "
            "daftar pustaka yang tidak dikutip di teks juga masalah."
        ),
    ),
    dict(
        id="sidang",
        title="Tips sidang skripsi",
        keywords=["sidang", "ujian skripsi", "presentasi sidang", "pertanyaan penguji",
                  "tips sidang", "seminar hasil", "sempro"],
        answer=(
            "❓ <b>Tips sidang skripsi</b>\n\n"
            "<b>Presentasi (10–15 menit):</b> latar belakang singkat → rumusan &amp; "
            "tujuan → metode → <b>hasil utama</b> → kesimpulan. Jangan bacakan "
            "seluruh skripsi — tonjolkan temuanmu.\n\n"
            "<b>Pertanyaan penguji yang paling sering keluar:</b>\n"
            "• Kenapa memilih topik/metode ini?\n"
            "• Apa kebaruan (novelty) penelitianmu dibanding penelitian terdahulu?\n"
            "• Kenapa sampelnya segitu / teknik samplingnya itu?\n"
            "• Jelaskan angka di tabel X — maksudnya apa?\n"
            "• Apa keterbatasan penelitianmu?\n\n"
            "<b>Tips:</b> kuasai Bab 3 &amp; Bab 4 luar kepala, jawab jujur "
            "(lebih baik bilang \"belum terpikirkan, akan saya perdalam\" daripada "
            "ngarang), dan siapkan daftar penelitian pembanding — /matriks bisa "
            "membantumu menyiapkannya."
        ),
    ),
    dict(
        id="judul",
        title="Memilih judul/topik",
        keywords=["judul", "memilih topik", "bingung topik", "ide judul",
                  "topik skripsi", "mencari topik"],
        answer=(
            "❓ <b>Memilih judul/topik skripsi</b>\n\n"
            "<b>Kriteria topik yang bagus:</b>\n"
            "• Kamu <b>tertarik</b> (akan ditemani berbulan-bulan!)\n"
            "• <b>Bisa diteliti</b> — data terjangkau, waktu &amp; biaya cukup\n"
            "• Ada <b>kebaruan</b> — celah yang belum dijawab penelitian lain\n"
            "• <b>Disetujui</b> dosen &amp; sesuai bidangmu\n\n"
            "<b>Cara menemukan celah (research gap):</b>\n"
            "1. Baca 10–15 jurnal terbaru di bidangmu (/cari bisa bantu)\n"
            "2. Catat bagian \"saran penelitian selanjutnya\" di tiap artikel\n"
            "3. Lihat ketidakkonsistenan hasil antar penelitian\n"
            "4. Terapkan konteks baru: lokasi, populasi, atau waktu berbeda\n\n"
            "Mau dibantu brainstorming? Coba /topik — kasih kata kunci minatmu, "
            "bot carikan tren &amp; celah riset terkini."
        ),
    ),
    dict(
        id="bimbingan",
        title="Tips bimbingan & revisi",
        keywords=["bimbingan", "dosen pembimbing", "revisi", "konsultasi",
                  "tips bimbingan", "pembimbing"],
        answer=(
            "❓ <b>Tips bimbingan &amp; revisi</b>\n\n"
            "• Datang dengan <b>draf yang sudah dibaca ulang</b> — jangan setor "
            "naskah mentah\n"
            "• Bawa <b>pertanyaan spesifik</b>, bukan \"tolong dicek semuanya\"\n"
            "• Catat semua masukan saat itu juga; kerjakan revisi <b>maksimal "
            "1–2 minggu</b> setelah bimbingan\n"
            "• Saat setor revisi, sertakan <b>daftar perubahan</b> (poin masukan → "
            "apa yang diubah, halaman berapa) — dosen suka ini\n"
            "• Beda pendapat? Sampaikan dengan data/rujukan, bukan debat kusir\n"
            "• Hormati gaya &amp; jadwal pembimbing; konfirmasi sebelum datang\n\n"
            "Ingat: revisi itu <b>bagian normal</b> penulisan skripsi, bukan vonis "
            "gagal. Rata-rata skripsi direvisi 3–5 putaran sebelum ACC."
        ),
    ),
]

CONTOH = [
    "Apa itu variabel bebas dan terikat?",
    "Bagaimana cara menentukan jumlah sampel?",
    "Apa beda purposive dan random sampling?",
    "Bagaimana uji validitas kuesioner?",
    "Kapan pakai regresi berganda?",
    "Bagaimana cara menulis latar belakang?",
    "Apa itu p-value?",
    "Tips menghadapi sidang skripsi?",
]

_STOP = {
    "apa", "itu", "apa", "yang", "dan", "dengan", "untuk", "pada", "adalah",
    "ini", "dari", "dalam", "sebagai", "oleh", "atau", "tidak", "saya",
    "bagaimana", "kenapa", "kapan", "dimana", "apakah", "tolong", "mohon",
    "cara", "tentang", "the", "of", "to", "in", "is", "a",
}


def _norm(q):
    return re.sub(r"\s+", " ", (q or "").lower().strip())


def match(q):
    """Kembalikan (entry, skor). Skor 0 bila tidak ada yang cocok."""
    qn = _norm(q)
    if not qn:
        return None, 0
    words = [w for w in re.findall(r"[a-z0-9]+", qn) if w not in _STOP]
    best, best_score = None, 0
    for e in KB:
        score = 0
        for kw in e["keywords"]:
            kwn = kw.lower()
            if " " in kwn:
                if kwn in qn:
                    score += 4  # frasa
            else:
                # kata tunggal: kata sangat pendek wajib cocok persis
                # (biar "ha" tidak cocok dengan "halo"), kata lain boleh
                # sebagai awalan (mis. "sampel" cocok "sampelnya")
                if len(kwn) <= 2:
                    pat = r"\b" + re.escape(kwn) + r"\b"
                else:
                    pat = r"\b" + re.escape(kwn) + r"\w*"
                if re.search(pat, qn):
                    score += 2
        if score > best_score:
            best, best_score = e, score
    if best_score < 2:
        return None, 0
    return best, best_score


def related(entry, n=2):
    """Entri KB lain yang berbagi token kata kunci (untuk tombol 'terkait')."""
    toks = set()
    for kw in entry["keywords"]:
        toks.update(w for w in re.findall(r"[a-z0-9]+", kw.lower()) if w not in _STOP)
    scored = []
    for e in KB:
        if e["id"] == entry["id"]:
            continue
        etoks = set()
        for kw in e["keywords"]:
            etoks.update(w for w in re.findall(r"[a-z0-9]+", kw.lower()) if w not in _STOP)
        overlap = len(toks & etoks)
        if overlap:
            scored.append((overlap, e))
    scored.sort(key=lambda x: -x[0])
    return [e for _, e in scored[:n]]


_GEMINI_MODELS = None  # daftar model terurut (cache per proses)
_GEMINI_OK = 0        # indeks model yang terakhir berhasil


async def _gemini_ranked_models(key):
    """Daftar kandidat model terurut: alias latest dulu, lalu versi terbaru."""
    global _GEMINI_MODELS
    if _GEMINI_MODELS is not None:
        return _GEMINI_MODELS
    import httpx
    import re as _re

    try:
        async with httpx.AsyncClient(timeout=15) as cli:
            r = await cli.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                params={"key": key, "pageSize": 100})
            r.raise_for_status()
            names = [m.get("name") for m in r.json().get("models", [])
                     if "generateContent" in (m.get("supportedGenerationMethods") or [])
                     and m.get("name")]
    except Exception as e:
        log.warning("gemini list models gagal: %s", e)
        names = []
    for extra in ("models/gemini-flash-latest",
                  "models/gemini-3.8-flash", "models/gemini-3.6-flash",
                  "models/gemini-3.5-flash", "models/gemini-3.5-flash-lite",
                  "models/gemini-3.1-flash-lite",
                  "models/gemini-2.5-flash", "models/gemini-2.0-flash"):
        if extra not in names:
            names.append(extra)

    def _rank(n):
        nl = n.lower()
        if "latest" in nl:
            return 0
        m = _re.search(r"(\d+)\.(\d+)", nl)
        if m:
            return 100 - (int(m.group(1)) * 10 + int(m.group(2)))
        if "flash" in nl:
            return 50
        return 90

    _GEMINI_MODELS = sorted(names, key=_rank)
    return _GEMINI_MODELS


async def gemini_generate(prompt, max_output_tokens=1500):
    """Generate teks bebas via Gemini dengan fallback model.

    max_output_tokens membatasi output di sisi provider (ekonomi AI,
    T-2 audit 2026-10-17).
    Tahan banting: tiap model dicoba; 404 -> lewati (model pensiun),
    429/500/503 -> coba ulang 1x lalu pindah model berikut.
    Model yang berhasil diingat untuk panggilan berikutnya.
    Kembalikan str | None.
    """
    global _GEMINI_OK
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return None
    import asyncio
    import httpx

    try:
        models = await _gemini_ranked_models(key)
    except Exception as e:
        log.warning("gemini_generate gagal (models): %s", e)
        return None
    if not models:
        log.warning("gemini: daftar model kosong")
        return None
    n = len(models)
    order = [models[(_GEMINI_OK + i) % n] for i in range(n)]
    async with httpx.AsyncClient(timeout=30) as cli:
        for model in order:
            for attempt in (1, 2):
                try:
                    r = await cli.post(
                        f"https://generativelanguage.googleapis.com/v1beta/{model}:generateContent",
                        params={"key": key},
                        json={"contents": [{"parts": [{"text": prompt}]}],
                              "generationConfig": {
                                  "maxOutputTokens": int(max_output_tokens)}},
                    )
                    if r.status_code == 404:
                        log.info("gemini %s 404 (pensiun), lanjut model berikut", model)
                        break
                    if r.status_code in (429, 500, 503):
                        log.info("gemini %s -> %s, ulangi (%d/2)", model, r.status_code, attempt)
                        if r.status_code == 429 and attempt == 2:
                            # 429 di Gemini level API key, bukan model — semua
                            # model lain pasti gagal juga, jangan buang budget
                            # 75 dtk (caller lanjut ke provider berikutnya).
                            log.warning("gemini 429 level key, hentikan percobaan model lain")
                            return None
                        await asyncio.sleep(2 * attempt)
                        continue
                    r.raise_for_status()
                    data = r.json()
                    cands = data.get("candidates") or []
                    if not cands:
                        log.warning("gemini %s: tanpa candidates (mungkin diblokir filter)", model)
                        return None
                    _GEMINI_OK = models.index(model)
                    log.info("gemini pakai model %s", model)
                    return cands[0]["content"]["parts"][0]["text"].strip()
                except httpx.HTTPStatusError as e:
                    sc = e.response.status_code
                    if sc == 404:
                        break
                    if sc in (429, 500, 503) and attempt < 2:
                        await asyncio.sleep(2 * attempt)
                        continue
                    log.warning("gemini_generate gagal (http=%s): %s", sc, e)
                    return None
                except Exception as e:
                    log.warning("gemini_generate gagal: %s", e)
                    return None
    log.warning("gemini: semua %d model gagal", n)
    return None


async def gemini_answer(q, premium=False):
    """Jawaban AI via rotasi provider. Premium -> Sonar (riset) dulu."""
    import ai_providers
    prompt = (
        "Kamu asisten skripsi/tugas akhir untuk mahasiswa Indonesia semua jurusan. "
        "Jawab dalam Bahasa Indonesia yang santai tapi jelas, maksimal 250 kata, "
        "netral jurusan, beri contoh konkret bila membantu. "
        "Jika pertanyaan di luar topik skripsi/akademik, tolak dengan sopan dan "
        "arahkan kembali ke topik skripsi.\n\nPertanyaan: " + q
    )
    # Prompt meminta maksimal 250 kata -> 1000 token longgar.
    return await ai_providers.generate(prompt, premium=premium, search=True,
                                       max_tokens=1000)


async def gemini_answer_pilihan(q, pilihan="auto"):
    """Seperti gemini_answer tapi dengan pemilih model (Sul 2026-10-19).

    Return (jawaban, info) — info dari ai_providers.generate_pilihan.
    """
    import ai_providers
    prompt = (
        "Kamu asisten skripsi/tugas akhir untuk mahasiswa Indonesia semua jurusan. "
        "Jawab dalam Bahasa Indonesia yang santai tapi jelas, maksimal 250 kata, "
        "netral jurusan, beri contoh konkret bila membantu. "
        "Jika pertanyaan di luar topik skripsi/akademik, tolak dengan sopan dan "
        "arahkan kembali ke topik skripsi.\n\nPertanyaan: " + q
    )
    info = await ai_providers.generate_pilihan(prompt, pilihan=pilihan,
                                               max_tokens=1000)
    return (info.get("teks") or None), info


_UNANSWERED_PRUNE_AT = 0.0


def log_unanswered(q):
    """Catat pertanyaan tak terjawab agar KB bisa diperbaiki.

    S-4 (audit 2026-10-17): retensi dibatasi 30 hari; prune dijalankan
    maksimal sekali per 24 jam (throttled) supaya tidak membebani tiap
    append.
    """
    global _UNANSWERED_PRUNE_AT
    dbstore.append_jsonl("data/tanya_unanswered.jsonl",
                         {"ts": int(time.time()), "q": q})
    now = time.time()
    if now - _UNANSWERED_PRUNE_AT > 86400:
        _UNANSWERED_PRUNE_AT = now
        try:
            n = dbstore.prune_unanswered(30)
            if n:
                log.info("tanya_unanswered: %d entri lama dibersihkan", n)
        except Exception as e:
            log.warning("prune tanya_unanswered gagal: %s", e)
