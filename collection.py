"""Koleksi pribadi: simpan referensi, lihat, hapus, export daftar pustaka.

Penyimpanan: JSON per user di data/koleksi.json (persist antar restart).
"""
import json
import logging
import os
import re
from io import BytesIO

import dbstore  # Postgres (DATABASE_URL) atau file lokal, lihat dbstore.py
from cite import cite_in, style_label

log = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
KOLEKSI_FILE = os.path.join(DATA_DIR, "koleksi.json")


def _load():
    d = dbstore.load_json(KOLEKSI_FILE, {})
    return d if isinstance(d, dict) else {}


def _save(d):
    dbstore.save_json(KOLEKSI_FILE, d)


def _key(work):
    doi = (work.get("doi") or "").lower().strip()
    if doi:
        return "doi:" + doi
    t = re.sub(r"\s+", " ", (work.get("title") or "").lower()).strip()
    return f"judul:{t}|{work.get('year')}"


def add(user_id, work):
    """Simpan work ke koleksi user. Return (ok, total, duplikat)."""
    d = _load()
    uid = str(user_id)
    items = d.get(uid, [])
    k = _key(work)
    if any(_key(w) == k for w in items):
        return False, len(items), True
    items.append(work)
    d[uid] = items
    _save(d)
    return True, len(items), False


def list_all(user_id):
    return _load().get(str(user_id), [])


def count(user_id):
    return len(list_all(user_id))


def remove(user_id, idx):
    """Hapus item nomor idx (0-based). Return work yang dihapus / None."""
    d = _load()
    uid = str(user_id)
    items = d.get(uid, [])
    if 0 <= idx < len(items):
        w = items.pop(idx)
        d[uid] = items
        _save(d)
        return w
    return None


def clear(user_id):
    d = _load()
    d[str(user_id)] = []
    _save(d)


def _plain_citation(work, style):
    return re.sub(r"\*([^*]+)\*", r"\1", cite_in(work, style)).strip()


def export_txt(user_id, style="harvard"):
    """Return isi daftar pustaka format teks polos."""
    items = list_all(user_id)
    lines = [f"DAFTAR PUSTAKA ({style_label(style)})", ""]
    for i, w in enumerate(items, 1):
        lines.append(f"[{i}] {_plain_citation(w, style)}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def export_docx(user_id, style="harvard"):
    """Return bytes file .docx daftar pustaka."""
    from docx import Document
    from docx.shared import Pt

    items = list_all(user_id)
    doc = Document()
    doc.add_heading(f"Daftar Pustaka ({style_label(style)})", level=1)
    for i, w in enumerate(items, 1):
        p = doc.add_paragraph()
        run = p.add_run(f"[{i}] {_plain_citation(w, style)}")
        run.font.size = Pt(11)
    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _bibtex_key(w, n):
    authors = w.get("authors") or []
    fam = "anon"
    if authors:
        a0 = authors[0]
        fam = (a0.split(",")[0] if "," in a0 else a0.strip().split()[-1])
        fam = re.sub(r"[^a-zA-Z]", "", fam).lower() or "anon"
    year = w.get("year") or "nd"
    tw = re.sub(r"[^a-zA-Z]", "", (w.get("title") or "").split()[0] if (w.get("title") or "").split() else "")
    return f"{fam}{year}{tw.lower()[:12]}{n}"


def _bibtex_authors(authors):
    parts = []
    for a in authors or []:
        a = a.strip()
        if "," in a:
            fam, giv = [x.strip() for x in a.split(",", 1)]
            parts.append(f"{giv} {fam}".strip())
        else:
            parts.append(a)
    return " and ".join(parts)


def _bib_escape(s):
    """Escape nilai field BibTeX: kurung kurawal, persen, backslash, dan
    newline dihapus/dinetralkan agar file .bib tidak rusak saat di-parse."""
    s = str(s or "")
    s = s.replace("\\", "\\\\")
    s = s.replace("{", "\\{").replace("}", "\\}")
    s = s.replace("%", "\\%")
    return " ".join(s.split())


def export_bibtex(user_id):
    """Return isi file .bib (BibTeX) dari koleksi."""
    items = list_all(user_id)
    out = []
    for n, w in enumerate(items, 1):
        key = _bibtex_key(w, n)
        fields = [
            f'  author = {{{_bib_escape(_bibtex_authors(w.get("authors")))}}}',
            f'  title = {{{_bib_escape(w.get("title"))}}}',
        ]
        if w.get("journal"):
            fields.append(f'  journal = {{{_bib_escape(w.get("journal"))}}}')
        if w.get("year"):
            fields.append(f'  year = {{{_bib_escape(w.get("year"))}}}')
        if w.get("volume"):
            fields.append(f'  volume = {{{_bib_escape(w.get("volume"))}}}')
        if w.get("issue"):
            fields.append(f'  number = {{{_bib_escape(w.get("issue"))}}}')
        if w.get("pages"):
            fields.append(f'  pages = {{{_bib_escape(w.get("pages"))}}}')
        if w.get("doi"):
            fields.append(f'  doi = {{{_bib_escape(w.get("doi"))}}}')
        if w.get("url"):
            fields.append(f'  url = {{{_bib_escape(w.get("url"))}}}')
        out.append("@article{%s,\n%s\n}" % (key, ",\n".join(fields)))
    return "\n\n".join(out) + "\n"


def export_ris(user_id):
    """Return isi file .ris dari koleksi (untuk Mendeley/Zotero/EndNote)."""
    items = list_all(user_id)
    out = []
    for w in items:
        lines = ["TY  - JOUR"]
        for a in w.get("authors") or []:
            lines.append(f"AU  - {a}")
        if w.get("title"):
            lines.append(f"TI  - {w.get('title')}")
        if w.get("journal"):
            lines.append(f"JO  - {w.get('journal')}")
        if w.get("year"):
            lines.append(f"Y1  - {w.get('year')}")
        if w.get("volume"):
            lines.append(f"VL  - {w.get('volume')}")
        if w.get("issue"):
            lines.append(f"IS  - {w.get('issue')}")
        if w.get("pages"):
            # pecah rentang "123-130" -> SP/EP agar terbaca Mendeley/Zotero
            m = re.match(r"\s*(\d+)\s*[-–—]\s*(\d+)\s*$", str(w.get("pages")))
            if m:
                lines.append(f"SP  - {m.group(1)}")
                lines.append(f"EP  - {m.group(2)}")
            else:
                lines.append(f"SP  - {w.get('pages')}")
        if w.get("doi"):
            lines.append(f"DO  - {w.get('doi')}")
        if w.get("url"):
            lines.append(f"UR  - {w.get('url')}")
        lines.append("ER  - ")
        out.append("\n".join(lines))
    return "\n\n".join(out) + "\n"
