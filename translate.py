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
                          params={"q": chunk, "langpair": langpair},
                          timeout=25)
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

def translate_en_id(text):
    """Terjemahkan abstrak EN->ID.

    Return (hasil, sumber) dengan sumber: 'argos' | 'mymemory' |
    'sudah_indonesia' | None (gagal).
    """
    text = (text or "").strip()
    if not text:
        return None, None
    if looks_indonesian(text):
        return None, "sudah_indonesia"
    text = text[:2500]
    try:
        out = _argos_translate(text)
        if out:
            return out, "argos"
    except Exception as e:
        log.warning("Argos gagal: %s", e)
    out = _mymemory_translate(text)
    if out:
        return out, "mymemory"
    return None, None


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
    en = None
    try:
        en = _argos_translate(text, "id", "en")
    except Exception as e:
        log.warning("Argos id->en gagal: %s", e)
    if not en:
        en = _mymemory_translate(text, "id|en")
    if not en:
        return None, "Gagal menerjemahkan (ID→EN). Coba lagi ya."
    # EN -> ID
    back = None
    try:
        back = _argos_translate(en, "en", "id")
    except Exception as e:
        log.warning("Argos en->id gagal: %s", e)
    if not back:
        back = _mymemory_translate(en, "en|id")
    if not back:
        return None, "Gagal menerjemahkan (EN→ID). Coba lagi ya."
    if back.strip().lower() == text.lower():
        return None, "Hasilnya sama persis dengan aslinya, coba ubah sedikit kalimatnya."
    return back.strip(), None
