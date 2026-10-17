"""Panduan pemilihan uji statistik untuk skripsi — netral jurusan.

Alur tanya-jawab (decision tree) via tombol inline:
tujuan -> kelompok/jenis var -> pasangan -> normalitas -> rekomendasi uji
+ syarat + cara baca hasil + contoh kalimat.
"""
import logging

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ opsi
OPT_TUJUAN = [
    ("beda", "⚖️ Membandingkan kelompok"),
    ("hubung", "🔗 Hubungan 2 variabel"),
    ("prediksi", "🔮 Prediksi / pengaruh (regresi)"),
    ("deskriptif", "📊 Deskriptif saja"),
]
OPT_KELOMPOK = [("1", "1 kelompok (vs nilai acuan)"),
                ("2", "2 kelompok"), ("banyak", "> 2 kelompok")]
OPT_DATA = [("kategorik", "Kategorik (ya/tidak, kategori)"),
            ("numerik", "Numerik (skor, kadar, jumlah, ...)")]
OPT_PASANGAN = [("ya", "Ya, berpasangan (pre-post / sama subjek)"),
                ("tidak", "Tidak (subjek beda)")]
OPT_NORMAL = [("ya", "Ya, normal"), ("tidak", "Tidak normal"),
              ("ragu", "Belum tahu")]
OPT_JENIS_HUB = [("katkat", "Kategorik ↔ Kategorik"),
                 ("numnum", "Numerik ↔ Numerik"),
                 ("katnum", "Kategorik ↔ Numerik")]
OPT_NBEBAS = [("1", "1 variabel"), ("2", "2 variabel"), ("3+", "3 atau lebih")]
OPT_OUTCOME = [("kategorik", "Kategorik (mis. ya/tidak)"),
               ("numerik", "Numerik (mis. skor, nilai)")]


def next_step(a):
    """Dari jawaban sejauh ini -> ('q', qid, teks, opsi) atau ('hasil', key)."""
    t = a.get("tujuan")
    if not t:
        return ("q", "tujuan", "Apa <b>tujuan</b> analisismu?", OPT_TUJUAN)
    if t == "deskriptif":
        return ("hasil", "deskriptif")
    if t == "beda":
        k = a.get("kelompok")
        if not k:
            return ("q", "kelompok", "Berapa <b>kelompok</b> yang dibandingkan?", OPT_KELOMPOK)
        d = a.get("data")
        if not d:
            return ("q", "data", "Jenis <b>data hasil ukurmu</b>?", OPT_DATA)
        if d == "kategorik":
            if k == "1":
                return ("hasil", "binomial")
            if k == "banyak":
                return ("hasil", "chisquare")
            p = a.get("pasangan")
            if not p:
                return ("q", "pasangan", "Datanya <b>berpasangan</b>?", OPT_PASANGAN)
            return ("hasil", "mcnemar" if p == "ya" else "chisquare")
        # numerik
        p = a.get("pasangan")
        if not p:
            return ("q", "pasangan", "Datanya <b>berpasangan</b>?", OPT_PASANGAN)
        n = a.get("normal")
        if not n:
            return ("q", "normal",
                    "Apakah data <b>berdistribusi normal</b>?\n"
                    "<i>(uji Shapiro-Wilk/Kolmogorov-Smirnov; p &gt; 0,05 = normal)</i>",
                    OPT_NORMAL)
        if k == "1":
            return ("hasil", "onesample_t" if n == "ya" else "wilcoxon_1")
        if k == "2":
            if p == "ya":
                return ("hasil", "paired_t" if n == "ya" else "wilcoxon")
            return ("hasil", "independent_t" if n == "ya" else "mannwhitney")
        # >2 kelompok
        if p == "ya":
            return ("hasil", "repeated_anova" if n == "ya" else "friedman")
        return ("hasil", "oneway_anova" if n == "ya" else "kruskal")
    if t == "hubung":
        j = a.get("jenis")
        if not j:
            return ("q", "jenis", "Jenis <b>variabelnya</b>?", OPT_JENIS_HUB)
        nb = a.get("nbebas")
        if not nb:
            return ("q", "nbebas",
                    "Berapa <b>variabel bebas</b> (independen) yang diuji?",
                    OPT_NBEBAS)
        if nb != "1":
            # multivariat: 2+ variabel bebas -> regresi berganda
            if j == "katkat":
                return ("hasil", "logreg_multi")
            return ("hasil", "linreg_multi")
        if j == "katkat":
            return ("hasil", "chisquare_hub")
        if j == "katnum":
            return ("hasil", "katnum")
        n = a.get("normal")
        if not n:
            return ("q", "normal",
                    "Apakah data <b>berdistribusi normal</b>?\n"
                    "<i>(uji Shapiro-Wilk/Kolmogorov-Smirnov; p &gt; 0,05 = normal)</i>",
                    OPT_NORMAL)
        return ("hasil", "pearson" if n == "ya" else "spearman")
    if t == "prediksi":
        o = a.get("outcome")
        if not o:
            return ("q", "outcome",
                    "Variabel <b>hasil (outcome)</b> yang diprediksi jenis apa?",
                    OPT_OUTCOME)
        return ("hasil", "logreg" if o == "kategorik" else "linreg")
    return ("hasil", "deskriptif")


# ------------------------------------------------------------------ hasil
RESULTS = {
    "deskriptif": {
        "nama": "Statistik Deskriptif",
        "kapan": "Mendeskripsikan karakteristik data tanpa uji hipotesis.",
        "syarat": ["Tidak ada syarat khusus."],
        "baca": ("Data numerik: tampilkan mean ± SD bila normal, atau median "
                 "(min–maks) bila tidak normal. Data kategorik: tampilkan frekuensi (n) dan persen (%)."),
        "contoh": ("Rerata usia responden 34,2 ± 5,1 tahun; 62% responden berjenis "
                   "kelamin perempuan."),
    },
    "binomial": {
        "nama": "Uji Binomial / Chi-Square Goodness of Fit",
        "kapan": "Membandingkan proporsi 1 kelompok kategorik dengan nilai acuan.",
        "syarat": ["Data kategorik.", "Sampel acak."],
        "baca": ("p &lt; 0,05 → proporsi berbeda signifikan dari nilai acuan; "
                 "p ≥ 0,05 → tidak ada perbedaan signifikan."),
        "contoh": ("Proporsi kelulusan (78%) berbeda signifikan dari target 60% (p = 0,012)."),
    },
    "onesample_t": {
        "nama": "One-Sample T-Test",
        "kapan": "Membandingkan rerata 1 kelompok numerik dengan nilai acuan, data normal.",
        "syarat": ["Data numerik berdistribusi normal.", "Sampel acak."],
        "baca": ("p &lt; 0,05 → rerata berbeda signifikan dari nilai acuan."),
        "contoh": ("Rerata skor (72,4) berbeda signifikan dari KKM 70 (p = 0,031)."),
    },
    "wilcoxon_1": {
        "nama": "Wilcoxon Signed-Rank (1 sampel)",
        "kapan": "Versi non-parametrik one-sample t-test; data numerik tidak normal.",
        "syarat": ["Data numerik/ordinal.", "Tidak perlu normal."],
        "baca": ("p &lt; 0,05 → median berbeda signifikan dari nilai acuan."),
        "contoh": ("Median skor berbeda signifikan dari KKM (p = 0,018)."),
    },
    "chisquare": {
        "nama": "Uji Chi-Square",
        "kapan": "Membandingkan proporsi antar 2 kelompok atau lebih (data kategorik, tidak berpasangan).",
        "syarat": ["Data kategorik.", "Nilai expected tiap sel ≥ 5 (kalau tidak, pakai Fisher's Exact).",
                   "Sampel tidak berpasangan."],
        "baca": ("p &lt; 0,05 → ada perbedaan/hubungan yang signifikan antar kelompok."),
        "contoh": ("Ada perbedaan signifikan tingkat kepuasan antar tiga kelompok (p = 0,004)."),
    },
    "mcnemar": {
        "nama": "Uji McNemar",
        "kapan": "Membandingkan 2 pengukuran kategorik berpasangan (mis. pre vs post pada subjek sama).",
        "syarat": ["Data kategorik dikotomi (ya/tidak).", "Sampel berpasangan."],
        "baca": ("p &lt; 0,05 → ada perubahan signifikan antara sebelum dan sesudah."),
        "contoh": ("Ada perubahan signifikan status kelulusan sebelum dan sesudah intervensi (p = 0,021)."),
    },
    "paired_t": {
        "nama": "Paired T-Test (uji T berpasangan)",
        "kapan": "Membandingkan 2 pengukuran numerik berpasangan (pre vs post), data normal.",
        "syarat": ["Data numerik.", "Selisih kedua pengukuran berdistribusi normal.", "Sampel berpasangan."],
        "baca": ("p &lt; 0,05 → ada perbedaan signifikan sebelum vs sesudah."),
        "contoh": ("Ada perbedaan signifikan skor sebelum (65,1) dan sesudah (78,3) pelatihan (p &lt; 0,001)."),
    },
    "wilcoxon": {
        "nama": "Uji Wilcoxon (signed-rank)",
        "kapan": "Versi non-parametrik paired t-test; data tidak normal.",
        "syarat": ["Data numerik/ordinal.", "Sampel berpasangan.", "Tidak perlu normal."],
        "baca": ("p &lt; 0,05 → ada perbedaan signifikan sebelum vs sesudah."),
        "contoh": ("Ada perbedaan signifikan skor sebelum dan sesudah intervensi (p = 0,009)."),
    },
    "independent_t": {
        "nama": "Independent T-Test (uji T tidak berpasangan)",
        "kapan": "Membandingkan rerata 2 kelompok bebas, data normal.",
        "syarat": ["Data numerik normal di tiap kelompok.", "Varians homogen (cek Levene; bila tidak, pakai Welch).",
                   "Sampel tidak berpasangan."],
        "baca": ("p &lt; 0,05 → rerata kedua kelompok berbeda signifikan."),
        "contoh": ("Rerata kelompok A (80,2) berbeda signifikan dari kelompok B (74,6) (p = 0,027)."),
    },
    "mannwhitney": {
        "nama": "Uji Mann-Whitney",
        "kapan": "Versi non-parametrik independent t-test; data tidak normal.",
        "syarat": ["Data numerik/ordinal.", "Sampel tidak berpasangan.", "Tidak perlu normal."],
        "baca": ("p &lt; 0,05 → ada perbedaan signifikan antar kedua kelompok."),
        "contoh": ("Ada perbedaan signifikan skor antara kelompok A dan B (p = 0,014)."),
    },
    "oneway_anova": {
        "nama": "One-Way ANOVA",
        "kapan": "Membandingkan rerata &gt; 2 kelompok bebas, data normal.",
        "syarat": ["Data numerik normal tiap kelompok.", "Varians homogen.",
                   "Lanjut uji post-hoc (Tukey/LSD) bila signifikan."],
        "baca": ("p &lt; 0,05 → minimal ada satu kelompok yang berbeda; lanjut post-hoc untuk tahu pasangan mana."),
        "contoh": ("Ada perbedaan signifikan rerata antar tiga kelompok (p = 0,006); uji Tukey menunjukkan ..."),
    },
    "kruskal": {
        "nama": "Uji Kruskal-Wallis",
        "kapan": "Versi non-parametrik one-way ANOVA; data tidak normal.",
        "syarat": ["Data numerik/ordinal.", "Tidak perlu normal.",
                   "Lanjut uji post-hoc (Mann-Whitney + koreksi Bonferroni) bila signifikan."],
        "baca": ("p &lt; 0,05 → minimal ada satu kelompok yang berbeda."),
        "contoh": ("Ada perbedaan signifikan antar tiga kelompok (p = 0,011)."),
    },
    "repeated_anova": {
        "nama": "Repeated Measures ANOVA",
        "kapan": "Membandingkan ≥ 3 pengukuran berulang pada subjek sama, data normal.",
        "syarat": ["Data numerik normal.", "Sphericity terpenuhi (uji Mauchly)."],
        "baca": ("p &lt; 0,05 → ada perbedaan signifikan antar waktu pengukuran."),
        "contoh": ("Ada perbedaan signifikan skor pada tiga waktu pengukuran (p &lt; 0,001)."),
    },
    "friedman": {
        "nama": "Uji Friedman",
        "kapan": "Versi non-parametrik repeated measures ANOVA; data tidak normal.",
        "syarat": ["Data numerik/ordinal berulang.", "Tidak perlu normal."],
        "baca": ("p &lt; 0,05 → ada perbedaan signifikan antar waktu pengukuran."),
        "contoh": ("Ada perbedaan signifikan skor pada tiga waktu pengukuran (p = 0,003)."),
    },
    "chisquare_hub": {
        "nama": "Uji Chi-Square (hubungan)",
        "kapan": "Melihat hubungan 2 variabel kategorik.",
        "syarat": ["Kedua variabel kategorik.", "Expected tiap sel ≥ 5 (atau Fisher's Exact)."],
        "baca": ("p &lt; 0,05 → ada hubungan signifikan; lihat koefisien kontingensi/Cramer's V untuk kekuatan hubungan."),
        "contoh": ("Ada hubungan signifikan antara jenis kelamin dan pilihan jurusan (p = 0,032)."),
    },
    "katnum": {
        "nama": "Korelasi Point-Biserial / Eta",
        "kapan": "Hubungan 1 variabel kategorik dengan 1 variabel numerik.",
        "syarat": ["Satu kategorik, satu numerik."],
        "baca": ("Bila kategoriknya 2 kategori → sama dengan uji beda 2 kelompok "
                 "(independent t-test / Mann-Whitney). Bila &gt; 2 kategori → ANOVA / Kruskal-Wallis."),
        "contoh": ("Hubungan diuji dengan membandingkan rerata antar kategori (lihat panduan 'Membandingkan kelompok')."),
    },
    "pearson": {
        "nama": "Korelasi Pearson",
        "kapan": "Hubungan 2 variabel numerik, data normal, hubungan linear.",
        "syarat": ["Kedua variabel numerik & normal.", "Hubungan linear (cek scatterplot)."],
        "baca": ("p &lt; 0,05 → hubungan signifikan. Nilai r: 0,1–0,3 lemah; 0,3–0,5 sedang; "
                 "&gt; 0,5 kuat. Tanda +/− = arah hubungan."),
        "contoh": ("Ada hubungan positif sedang antara jam belajar dan nilai (r = 0,42; p = 0,001)."),
    },
    "spearman": {
        "nama": "Korelasi Spearman",
        "kapan": "Versi non-parametrik Pearson; data tidak normal / ordinal.",
        "syarat": ["Variabel numerik/ordinal.", "Tidak perlu normal."],
        "baca": ("Sama seperti Pearson: p &lt; 0,05 = signifikan; rho menunjukkan kekuatan & arah."),
        "contoh": ("Ada hubungan positif antara tingkat stres dan tekanan darah (rho = 0,38; p = 0,008)."),
    },
    "linreg": {
        "nama": "Regresi Linear",
        "kapan": "Memprediksi outcome numerik dari satu/beberapa prediktor.",
        "syarat": ["Outcome numerik.", "Uji asumsi klasik: normalitas residual, multikolinearitas, heteroskedastisitas."],
        "baca": ("p &lt; 0,05 pada koefisien → prediktor berpengaruh signifikan. "
                 "R² = proporsi variasi outcome yang dijelaskan model."),
        "contoh": ("Motivasi berpengaruh signifikan terhadap prestasi (β = 0,45; p &lt; 0,001; R² = 0,38)."),
    },
    "logreg": {
        "nama": "Regresi Logistik",
        "kapan": "Memprediksi outcome kategorik (mis. ya/tidak, lulus/gagal).",
        "syarat": ["Outcome kategorik.", "Sampel cukup besar (≥ 10 kejadian per prediktor)."],
        "baca": ("p &lt; 0,05 → prediktor berpengaruh. OR (odds ratio) &gt; 1 = faktor risiko, &lt; 1 = protektif."),
        "contoh": ("Merokok meningkatkan odds kejadian penyakit (OR = 2,3; p = 0,015)."),
    },
    "linreg_multi": {
        "nama": "Regresi Linear Berganda",
        "kapan": ("Menguji pengaruh 2 atau lebih variabel bebas terhadap 1 variabel terikat "
                  "numerik — sekaligus (simultan) maupun masing-masing (parsial)."),
        "syarat": ["Variabel terikat numerik.", "Variabel bebas numerik/kategorik (kategorik dijadikan dummy).",
                   "Uji asumsi klasik: normalitas residual, tidak ada multikolinearitas (VIF &lt; 10), "
                   "tidak ada heteroskedastisitas, hubungan linear."],
        "baca": ("Uji F (p &lt; 0,05 → minimal satu variabel bebas berpengaruh signifikan secara bersama-sama). "
                 "Uji t tiap variabel (p &lt; 0,05 → variabel itu berpengaruh signifikan bila variabel lain dikontrol). "
                 "Adjusted R² = proporsi variasi yang dijelaskan seluruh model."),
        "contoh": ("Secara simultan, motivasi, fasilitas, dan dukungan keluarga berpengaruh signifikan "
                   "terhadap prestasi (F, p &lt; 0,001; adjusted R² = 0,52). Secara parsial, motivasi "
                   "berpengaruh paling dominan (β = 0,41; p &lt; 0,001)."),
    },
    "logreg_multi": {
        "nama": "Regresi Logistik Berganda",
        "kapan": ("Menguji pengaruh 2 atau lebih variabel bebas terhadap 1 variabel terikat "
                  "kategorik (mis. ya/tidak, sakit/sehat)."),
        "syarat": ["Variabel terikat kategorik (biner).", "Variabel bebas numerik/kategorik.",
                   "Sampel cukup besar (≥ 10 kejadian per variabel bebas).",
                   "Tidak ada multikolinearitas antar variabel bebas."],
        "baca": ("Uji Omnibus/Wald (p &lt; 0,05 → model/variabel signifikan). "
                 "OR (odds ratio) tiap variabel: OR &gt; 1 = faktor risiko, OR &lt; 1 = protektif, "
                 "setelah mengontrol variabel lain. Nagelkerke R² = kekuatan model."),
        "contoh": ("Setelah dikontrol variabel lain, merokok (OR = 2,1; p = 0,022) dan riwayat keluarga "
                   "(OR = 3,4; p = 0,004) berhubungan signifikan dengan kejadian penyakit."),
    },
}


def get_result(key):
    return RESULTS.get(key)
