"""Lacak artikel dari sitasi / daftar pustaka yang dipaste user.

Alur: paste sitasi -> ekstrak DOI (paling akurat) atau judul+tahun ->
cari via OpenAlex/Crossref -> tampilkan lokasi file (PDF/DOI/Scholar).
parse_citation juga dipakai /sitasi untuk membuat ulang sitasi
dari teks daftar pustaka.
"""
import logging
import re
from difflib import SequenceMatcher

log = logging.getLogger(__name__)

_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\]\)\"'<>]+", re.I)
_YEAR_RE = re.compile(r"\((19|20)\d{2}[a-z]?\)")


def _parse_authors(auth_part):
    """'Santoso, A., & Wijaya, B.' -> ['Santoso, A.', 'Wijaya, B.'].
    Juga tangani format Vancouver 'Santoso A, Wijaya B'."""
    s = re.sub(r"\s*&\s*|\s+and\s+", ",", auth_part or "", flags=re.I)
    toks = [x.strip(" .") for x in s.split(",") if x.strip(" .")]
    authors, i = [], 0
    while i < len(toks):
        fam = toks[i]
        giv = []
        j = i + 1
        while j < len(toks) and re.fullmatch(r"(?:[A-Z]\.? ?)+", toks[j]):
            giv.append(toks[j])
            j += 1
        name = fam + (", " + " ".join(giv) if giv else "")
        # format Vancouver "Santoso A" -> "Santoso, A."
        mv = re.fullmatch(r"([A-Z][\w\-']+(?: [A-Z][\w\-']+)*) ([A-Z])\.?", name)
        if mv and "," not in name:
            name = f"{mv.group(1)}, {mv.group(2)}."
        authors.append(name)
        i = j if j > i + 1 else i + 1
    return [a for a in authors if a]


def parse_citation(text):
    """Ekstrak DOI / penulis / judul / tahun dari satu sitasi. Return dict."""
    t = " ".join((text or "").split())
    doi = None
    m = _DOI_RE.search(t)
    if m:
        doi = m.group(0).rstrip(".,;)")
    year = None
    authors = []
    ym = _YEAR_RE.search(t)
    if ym:
        year = ym.group(0)[1:5]
        authors = _parse_authors(t[:ym.start()].rstrip(" .;"))
    title = None
    if ym:
        rest = t[ym.end():].lstrip(" .")
        acc = ""
        for p in re.split(r"\.\s+", rest):
            acc = (acc + " " + p).strip()
            if len(acc) >= 20:
                break
        title = acc.rstrip(".") or None
    elif not doi:
        # format tanpa tahun berkurung (mis. Vancouver): penulis = segmen pertama
        segs = [s.strip() for s in re.split(r"\.\s+", t) if s.strip()]
        if segs:
            authors = _parse_authors(segs[0])
        for p in segs[1:]:
            if len(p) >= 25:
                title = p.rstrip(".")
                break
        ym2 = re.search(r"(19|20)\d{2}", t)
        if ym2:
            year = ym2.group(0)
    return {"doi": doi, "authors": authors, "title": title, "year": year,
            "raw": text.strip()}


def split_citations(text):
    """Pecah paste daftar pustaka jadi entri-entri (maks 5)."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    entries, cur = [], ""
    for ln in lines:
        is_new = bool(_YEAR_RE.search(ln)) and bool(re.match(r"^[A-Z\[]", ln))
        if is_new and cur:
            entries.append(cur)
            cur = ln
        elif cur:
            cur += " " + ln
        else:
            cur = ln
        if len(entries) >= 5:
            break
    if cur and len(entries) < 5:
        entries.append(cur)
    return entries[:5]


def title_match(a, b):
    """Skor kemiripan judul 0-1 (abaikan kapital & tanda baca)."""
    na = re.sub(r"[^a-z0-9 ]", "", (a or "").lower())
    nb = re.sub(r"[^a-z0-9 ]", "", (b or "").lower())
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()
