"""Sitasi via CSL resmi (standar yang dipakai Mendeley/Zotero).

Memakai citeproc-py + file gaya .csl resmi dari repositori CSL
(disimpan di csl_styles/). Mendukung semua item (DOI maupun bukan),
tidak tergantung API luar.

Fallback: bila gagal (library/style hilang, data kurang), pemanggil
memakai formatter lokal di cite.py seperti sebelumnya.
"""
import html as _html
import logging
import os
import re

log = logging.getLogger("jurnalbot")

_STYLE_DIR = os.path.join(os.path.dirname(__file__), "csl_styles")
STYLE_FILES = {
    "harvard": "harvard-cite-them-right.csl",
    "apa": "apa.csl",
    "mla": "modern-language-association.csl",
    "chicago": "chicago-author-date.csl",
    "vancouver": "vancouver.csl",
    "ieee": "ieee.csl",
}
# gaya bernomor: awalan "1." dari CSL dibuang (nomor ditambah pemanggil)
_NUMBERED = {"vancouver", "ieee"}

_styles_cache = {}


def _split_name(full):
    """'Charles D. Reese' -> ('Reese', 'Charles D.'); 'Reese, Charles' likewise."""
    full = re.sub(r"\s+", " ", (full or "").strip())
    if not full:
        return None
    if "," in full:
        fam, giv = [x.strip() for x in full.split(",", 1)]
        return (fam, giv) if fam else None
    bits = full.split(" ")
    if len(bits) == 1:
        return (bits[0], "")
    return (bits[-1], " ".join(bits[:-1]))


def _to_csl_item(w):
    authors = []
    for a in w.get("authors") or []:
        p = _split_name(a)
        if not p:
            continue
        fam, giv = p
        entry = {"family": fam}
        if giv:
            entry["given"] = giv
        authors.append(entry)
    item = {"id": "item", "type": "webpage" if w.get("is_website")
            else "article-journal"}
    if authors:
        item["author"] = authors
    year = w.get("year")
    try:
        y = int(str(year)[:4])
        if y > 1000:
            item["issued"] = {"date-parts": [[y]]}
    except (TypeError, ValueError):
        pass
    if w.get("title"):
        title = w["title"].strip()
        # judul full kapital (umum di jurnal Indonesia) -> sentence case,
        # menyamai perilaku formatter lokal sebelumnya
        if title.isupper():
            title = title[0].upper() + title[1:].lower()
        item["title"] = title
    if w.get("journal"):
        item["container-title"] = w["journal"].strip()
    if w.get("volume"):
        item["volume"] = str(w["volume"]).strip()
    if w.get("issue"):
        item["issue"] = str(w["issue"]).strip()
    if w.get("pages"):
        item["page"] = str(w["pages"]).strip().replace("--", "-")
    if w.get("doi"):
        item["DOI"] = re.sub(r"^https?://(dx\.)?doi\.org/",
                             "", str(w["doi"]).strip())
    if w.get("url"):
        item["URL"] = str(w["url"]).strip()
    return item


def _get_style(key):
    if key in _styles_cache:
        return _styles_cache[key]
    from citeproc import CitationStylesStyle
    path = os.path.join(_STYLE_DIR, STYLE_FILES[key])
    st = CitationStylesStyle(path, validate=False)
    _styles_cache[key] = st
    return st


def _clean(text, key):
    t = _html.unescape(text or "")
    t = re.sub(r"</?i>", "", t)          # buang tag italic
    t = t.replace("“", '"').replace("”", '"')
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"\s+([.,;:!?])", r"\1", t)   # "kata ." -> "kata."
    t = re.sub(r"\(\s+", "(", t)
    t = re.sub(r"\s+\)", ")", t)
    # rapikan spasi di dalam tanda kutip saja: " judul " -> "judul"
    t = re.sub(r'"\s+([^"]*?)\s+"', r'"\1"', t)
    t = t.strip()
    if key in _NUMBERED:
        t = re.sub(r"^(\[\d+\]|\d+\.)\s*", "", t)  # buang nomor urut CSL
    return t.strip()


def cite_csl(w, key="harvard"):
    """Format sitasi gaya `key` via CSL resmi. Kembalikan string polos
    atau None bila gagal (pemanggil pakai fallback lokal)."""
    if key not in STYLE_FILES:
        return None
    try:
        from citeproc import (Citation, CitationItem,
                              CitationStylesBibliography)
        from citeproc.source.json import CiteProcJSON
        item = _to_csl_item(w)
        if not item.get("title"):
            return None
        bib = CitationStylesBibliography(_get_style(key),
                                         CiteProcJSON([item]))
        bib.register(Citation([CitationItem("item")]))
        rendered = bib.bibliography()
        if not rendered:
            return None
        out = _clean(str(rendered[0]), key)
        return out or None
    except Exception as e:  # noqa: BLE001
        log.warning("csl gagal (%s): %s", key, e)
        return None
