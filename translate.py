"""Terjemahan abstrak Inggris -> Indonesia.

Prioritas: Argos Translate offline (tanpa kuota, privasi terjaga, model
diunduh otomatis saat pertama dipakai). Fallback: MyMemory API gratis.
"""
import logging
import re

import httpx

log = logging.getLogger(__name__)

_ID_WORDS = {"yang", "dan", "dengan", "untuk", "pada", "adalah", "ini", "itu",
             "dari", "dalam", "sebagai", "oleh", "penelitian", "hasil",
             "menunjukkan", "tersebut", "secara", "lebih", "tidak", "dapat"}
_EN_WORDS = {"the", "and", "with", "for", "this", "that", "from", "were",
             "was", "are", "study", "results", "showed", "research", "between",
             "using", "used", "than", "which", "have"}


def looks_indonesian(text):
    """Heuristik: True bila teks kemungkinan sudah berbahasa Indonesia."""
    words = set(re.findall(r"[a-z]+", (text or "").lower()))
    if not words:
        return False
    id_score = len(words & _ID_WORDS)
    en_score = len(words & _EN_WORDS)
    return id_score > en_score


def _argos_translate(text, from_code="en", to_code="id"):
    """Terjemahkan via Argos offline. Unduh model bila belum ada."""
    import argostranslate.translate as at

    def _pair():
        langs = at.get_installed_languages()
        src = next((l for l in langs if l.code == from_code), None)
        dst = next((l for l in langs if l.code == to_code), None)
        return src, dst

    src, dst = _pair()
    if not src or not dst:
        log.info("model Argos %s->%s belum ada, mengunduh…", from_code, to_code)
        import argostranslate.package as pkg
        pkg.update_package_index()
        avail = pkg.get_available_packages()
        p = next((x for x in avail
                  if x.from_code == from_code and x.to_code == to_code), None)
        if not p:
            return None
        p.install()  # ~100MB, sekali saja per pasangan bahasa
        src, dst = _pair()
    if not src or not dst:
        return None
    return src.get_translation(dst).translate(text)


def _split_chunks(text, limit=450):
    """Pecah teks jadi potongan <= limit char, utamakan batas kalimat."""
    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    chunks, cur = [], ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        while len(p) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            cut = p.rfind(" ", 0, limit)
            if cut < 100:
                cut = limit
            chunks.append(p[:cut].strip())
            p = p[cut:].strip()
        if cur and len(cur) + 1 + len(p) > limit:
            chunks.append(cur)
            cur = p
        else:
            cur = (cur + " " + p) if cur else p
    if cur:
        chunks.append(cur)
    return chunks


_UA_BROWSER = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/124.0 Safari/537.36")}
# Email pemilik situs (sudah publik di record DMARC domain) — MyMemory
# memberi kuota harian jauh lebih besar bila param "de" diisi.
_MYMEMORY_DE = "admin.khairys@gmail.com"


def _mymemory_translate(text, langpair="en|id"):
    """Fallback via MyMemory (gratis, ada batas harian).

    MyMemory menolak query > 500 char, jadi teks panjang dipecah dulu
    per <=450 char lalu hasil tiap potongan digabung lagi.
    """
    text = (text or "").strip()
    if not text:
        return None
    out_parts = []
    for chunk in _split_chunks(text):
        try:
            r = httpx.get("https://api.mymemory.translated.net/get",
                          params={"q": chunk, "langpair": langpair,
                                  "de": _MYMEMORY_DE},
                          headers=_UA_BROWSER, timeout=25)
            r.raise_for_status()
            t = (r.json().get("responseData") or {}).get("translatedText")
        except Exception as e:
            log.warning("MyMemory gagal: %s", e)
            return None
        if not t:
            return None
        t = t.strip()
        bad = ("query length limit", "quota exceeded", "invalid",
               "no translation")
        if any(b in t.lower() for b in bad):
            return None
        out_parts.append(t)
    out = " ".join(out_parts).strip()
    if not out or out.lower() == text.lower():
        return None
    return out


def _google_translate(text, from_code="en", to_code="id"):
    """Fallback terakhir: endpoint publik Google Translate (gtx).

    Dipakai karena IP server (Render) kerap kena kuota MyMemory.
    Balikan: gabungan segmen data[0][i][0]. Gagal total -> None.
    """
    text = (text or "").strip()
    if not text:
        return None
    out_parts = []
    for chunk in _split_chunks(text):
        try:
            r = httpx.get(
                "https://translate.googleapis.com/translate_a/single",
                params={"client": "gtx", "sl": from_code, "tl": to_code,
                        "dt": "t", "q": chunk},
                headers=_UA_BROWSER, timeout=25)
            r.raise_for_status()
            data = r.json()
            segs = data[0] or []
            t = "".join((s[0] or "") for s in segs if s).strip()
        except Exception as e:
            log.warning("Google gtx gagal: %s", e)
            return None
        if not t:
            return None
        out_parts.append(t)
    out = " ".join(out_parts).strip()
    if not out or out.lower() == text.lower():
        return None
    return out


_NAMA_BAHASA = {"en": "Inggris", "id": "Indonesia"}


def _ai_translate(text, from_code="en", to_code="id"):
    """Fallback terakhir: terjemahkan via rotasi AI (Cerebras/Gemini).

    Ditambahkan 2026-10-19: MyMemory kehabisan kuota harian dari IP
    Render dan Google gtx memblokir IP Render, sehingga seluruh rantai
    gratis mati total (terjemah + parafrase 502). Rotasi AI memakai key
    produksi sendiri, jadi tidak tergantung kuota pihak ketiga.
    Balikan dibersihkan dari pembuka basa-basi dan tanda kutip pembungkus.
    """
    text = (text or "").strip()
    if not text:
        return None
    asal = _NAMA_BAHASA.get(from_code, from_code)
    tujuan = _NAMA_BAHASA.get(to_code, to_code)
    prompt = (
        f"Terjemahkan teks berikut dari bahasa {asal} ke bahasa {tujuan} "
        "secara akurat dan alami, seperti gaya penulisan akademik. "
        "Pertahankan istilah teknis, nama, angka, dan singkatan apa adanya. "
        "Jawab HANYA dengan hasil terjemahannya, tanpa penjelasan, tanpa "
        "pembuka, dan tanpa tanda kutip pembungkus.\n\n"
        f"{text}"
    )
    try:
        import asyncio

        import ai_providers
        out = asyncio.run(ai_providers.generate(prompt, budget=35.0,
                                                max_tokens=1500))
    except Exception as e:
        log.warning("AI translate gagal: %s", e)
        return None
    if not out:
        return None
    out = out.strip()
    if len(out) >= 2 and out[0] == '"' and out[-1] == '"':
        out = out[1:-1].strip()
    if not out or out.lower() == text.lower():
        return None
    return out


def _terjemahkan(text, from_code, to_code):
    """Rantai penerjemah: Argos offline -> MyMemory -> Google gtx -> AI."""
    try:
        out = _argos_translate(text, from_code, to_code)
        if out:
            return out, "argos"
    except Exception as e:
        log.warning("Argos gagal: %s", e)
    out = _mymemory_translate(text, f"{from_code}|{to_code}")
    if out:
        return out, "mymemory"
    out = _google_translate(text, from_code, to_code)
    if out:
        return out, "google"
    out = _ai_translate(text, from_code, to_code)
    if out:
        return out, "ai"
    return None, None


def translate_en_id(text):
    """Terjemahkan abstrak EN->ID.

    Return (hasil, sumber) dengan sumber: 'argos' | 'mymemory' |
    'google' | 'ai' | 'sudah_indonesia' | None (gagal).
    """
    text = (text or "").strip()
    if not text:
        return None, None
    if looks_indonesian(text):
        return None, "sudah_indonesia"
    text = text[:2500]
    return _terjemahkan(text, "en", "id")


def paraphrase_id(text):
    """Parafrase teks Indonesia via round-trip ID -> EN -> ID.

    Return (hasil, catatan). Bukan pengganti pemahaman — selalu baca ulang
    dan sesuaikan dengan maksud aslimu sebelum dipakai di skripsi.
    """
    text = (text or "").strip()
    if not text:
        return None, "Teks kosong."
    if len(text) > 800:
        return None, "Maksimal 800 karakter ya, potong jadi beberapa bagian."
    if not looks_indonesian(text):
        return None, "Kirim teks berbahasa Indonesia ya."
    # ID -> EN
    en, _s1 = _terjemahkan(text, "id", "en")
    if not en:
        return None, "Gagal menerjemahkan (ID→EN). Coba lagi ya."
    # EN -> ID
    back, _s2 = _terjemahkan(en, "en", "id")
    if not back:
        return None, "Gagal menerjemahkan (EN→ID). Coba lagi ya."
    if back.strip().lower() == text.lower():
        return None, "Hasilnya sama persis dengan aslinya, coba ubah sedikit kalimatnya."
    return back.strip(), None
