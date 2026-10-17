"""Ide topik & research gap: agregasi topik dari publikasi terbaru via OpenAlex.

/topik <kata kunci> -> topik yang paling ramai dibahas 3 tahun terakhir
+ saran sudut pandang yang masih jarang diteliti.
"""
import json
import logging
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

_UA = {"User-Agent": "KhairyPustakaBot/1.0 (mailto:bot@localhost)"}
_API = "https://api.openalex.org/works"


def _fetch(params):
    # Lewat _oa_get terpusat: dapat mailto (polite pool), circuit breaker,
    # dan fail-fast saat 429 — bukan urllib mentah.
    from search_intl import _oa_get
    r = _oa_get(_API, params=params, timeout=10)
    return r.json()


def trending_topics(keyword, per_page=60):
    """Return (top_topics, gap_hints).

    top_topics: [(nama_topik, jumlah)] teratas.
    gap_hints: [(nama_topik, jumlah)] topik yang relatif jarang (kandidat gap).
    """
    params = {
        "search": keyword,
        "filter": "from_publication_date:2023-01-01",
        "per-page": per_page,
        "select": "id,title,topics,publication_year",
    }
    try:
        data = _fetch(params)
    except Exception as e:
        log.warning("topik gagal: %s", e)
        return [], []
    counts = {}
    for w in data.get("results", []):
        for t in w.get("topics") or []:
            name = t.get("display_name")
            if name:
                counts[name] = counts.get(name, 0) + 1
    ranked = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    top = ranked[:8]
    # gap: topik di luar 8 besar tapi muncul >= 2x
    gaps = [x for x in ranked[8:] if x[1] >= 2][:5]
    return top, gaps


def topic_suggestions(keyword, top, gaps):
    """Saran sudut pandang penelitian dari hasil agregasi."""
    s = []
    if top:
        names = [n for n, _ in top[:3]]
        s.append("Topik paling ramai: " + ", ".join(names) + ".")
    if gaps:
        s.append("Relatif jarang diteliti (peluang gap): " +
                 ", ".join(n for n, _ in gaps[:3]) + ".")
    if len(top) >= 2:
        s.append(f"Coba gabungkan sudut pandang: '{top[0][0]}' + '{top[1][0]}' — "
                 "kombinasi dua topik ramai sering menghasilkan kebaruan.")
    return s


async def translate_keyword_en(keyword):
    """Terjemahkan kata kunci (mis. Indonesia) -> Inggris via Gemini.

    Dipakai /topik saat pencarian kata kunci asli kosong di OpenAlex
    (datanya dominan Inggris). Fallback: kembalikan keyword asli.
    """
    try:
        import ai_providers
        out = await ai_providers.generate(
            "Terjemahkan kata kunci pencarian jurnal berikut ke bahasa Inggris. "
            "Jawab HANYA dengan hasil terjemahannya, tanpa penjelasan dan tanpa tanda kutip.\n\n"
            "Kata kunci: " + keyword[:200])
        out = (out or "").strip().strip('"').strip().split("\n")[0].strip()[:200]
        return out or keyword
    except Exception as e:
        log.warning("translate keyword gagal: %s", e)
        return keyword


async def ide_topik_ai(keyword, top, gaps, premium=False):
    """Rumuskan 5 ide judul penelitian via AI, Bahasa Indonesia.

    top/gaps: hasil trending_topics (boleh kosong — AI tetap bisa berkreasi
    dari pengetahuannya). Premium -> Sonar (riset tren) dulu. Kembalikan str | None.
    """
    try:
        import ai_providers
        tren = ""
        if top:
            tren = ("Data tren publikasi 2023-sekarang untuk topik ini:\n" +
                    "\n".join(f"- {n} ({c} publikasi)" for n, c in top[:8]))
            if gaps:
                tren += ("\nTopik yang masih jarang diteliti: " +
                         ", ".join(n for n, _ in gaps[:5]))
        prompt = (
            "Kamu asisten skripsi untuk mahasiswa Indonesia semua jurusan. "
            "Buatkan 5 ide topik/judul penelitian dari minat berikut.\n\n"
            f"Minat: {keyword[:300]}\n{tren}\n\n"
            "Jawab dalam Bahasa Indonesia, maksimal 350 kata, dengan format:\n"
            "Untuk tiap ide (1-5): tulis judul yang spesifik, lalu 1 baris "
            "menjelaskan variabel/fokusnya (variabel X dan Y untuk kuantitatif, "
            "atau fokus penelitian untuk kualitatif), dan 1 baris sudut kebaruan/gap-nya. "
            "Variasikan: campur ide kuantitatif dan kualitatif. "
            "Akhiri dengan 1 baris tips memilih topik yang paling feasible.")
        out = await ai_providers.generate(prompt, premium=premium,
                                            search=True)
        return (out or "").strip() or None
    except Exception as e:
        log.warning("ide topik AI gagal: %s", e)
        return None
