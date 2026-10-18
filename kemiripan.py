"""Cek kemiripan teks naskah vs teks sumber (Jalur A, keputusan Sul
2026-10-18): perbandingan frasa kata (n-gram/shingle) murni, TANPA AI,
TANPA database eksternal, dan teks pengguna TIDAK disimpan di mana pun
(diproses di memori lalu dibuang).

Prinsip kerja: teks sumber dipecah jadi potongan frasa 5 kata berturut
(sidik jari). Tiap kalimat naskah dihitung berapa persen kata-katanya
yang tertutup potongan yang sama persis dengan sumber. Skor total =
persentase kata naskah yang mirip. Kalimat diberi level: tinggi (>=70%),
sedang (40-69%), rendah (<40%).
"""
import re

_KATA = re.compile(r"[a-z0-9]+")
_KALIMAT = re.compile(r"[^.!?\n]+(?:[.!?]+|$)")
N = 5  # panjang frasa pembanding (kata)


def _kata(teks):
    return _KATA.findall((teks or "").lower())


def _shingles(words):
    if len(words) < N:
        return set()
    return {" ".join(words[i:i + N]) for i in range(len(words) - N + 1)}


def jumlah_kata(teks):
    return len(_kata(teks))


def cek(naskah, sumber):
    """Bandingkan naskah terhadap sumber. Kembalikan dict skor +
    rincian per kalimat (teks asli kalimat untuk ditampilkan)."""
    src_shingles = _shingles(_kata(sumber))
    kalimat_out = []
    total_kata = 0
    kata_mirip = 0
    for m in _KALIMAT.finditer(naskah or ""):
        teks_kal = m.group(0).strip()
        if not teks_kal:
            continue
        w = _kata(teks_kal)
        if not w:
            continue
        total_kata += len(w)
        skor_kal = 0.0
        if len(w) >= N and src_shingles:
            covered = [False] * len(w)
            for i in range(len(w) - N + 1):
                if " ".join(w[i:i + N]) in src_shingles:
                    for j in range(i, i + N):
                        covered[j] = True
            c = sum(covered)
            kata_mirip += c
            skor_kal = c / len(w) * 100
        level = "tinggi" if skor_kal >= 70 else (
            "sedang" if skor_kal >= 40 else "rendah")
        kalimat_out.append({"teks": teks_kal, "skor": round(skor_kal),
                            "level": level})
    skor = (kata_mirip / total_kata * 100) if total_kata else 0.0
    return {"skor": round(skor, 1), "total_kata": total_kata,
            "kata_mirip": kata_mirip, "kalimat": kalimat_out}
