"""Impor referensi dari file BibTeX (.bib) dan RIS (.ris).

Pasangan export yang sudah ada di collection.py: pengguna mengekspor
daftar referensinya dari Mendeley / Zotero / Google Scholar, lalu
mengimpornya ke Koleksi KhairyPustaka secara massal.

Hasil parse = list work format internal (sama seperti hasil pencarian):
{title, authors[], year, journal, volume, issue, pages, doi, url, abstract}
"""
import re

_TIPE_SKIP = {"string", "preamble", "comment"}


def _bersih(s):
    """Bersihkan nilai teks: kurung kurawal BibTeX, perintah LaTeX
    sederhana, dan spasi berlebih dibuang."""
    s = str(s or "")
    s = s.replace("\\{", "{").replace("\\}", "}")
    s = s.replace("{", "").replace("}", "")
    s = re.sub(r"\\[a-zA-Z]+\s*", "", s)  # perintah LaTeX sisa: \alpha dll
    s = s.replace("\\&", "&").replace("\\%", "%").replace("\\_", "_")
    return " ".join(s.split()).strip()


def _konversi_penulis(nama):
    """Samakan format nama penulis ke gaya internal 'Keluarga, I. I.'.

    Masukan bisa 'Keluarga, Nama Depan', 'Nama Depan Keluarga', atau
    sudah 'Keluarga, I.'. Nama tunggal dikembalikan apa adanya.
    """
    nama = _bersih(nama).strip(" ,")
    if not nama:
        return ""
    if "," in nama:
        fam, giv = [x.strip() for x in nama.split(",", 1)]
        if not giv:
            return fam
        if re.fullmatch(r"(?:[A-Z]\.?\s*)+", giv):
            inisial = " ".join(t if t.endswith(".") else t + "."
                               for t in giv.replace(".", " ").split())
            return f"{fam}, {inisial}"
        inisial = " ".join(t[0].upper() + "."
                           for t in re.split(r"[\s\-]+", giv) if t)
        return f"{fam}, {inisial}" if inisial else fam
    toks = nama.split()
    if len(toks) == 1:
        return toks[0]
    fam = toks[-1]
    inisial = " ".join(t[0].upper() + "." for t in toks[:-1] if t)
    return f"{fam}, {inisial}" if inisial else fam


def _nilai_bibtex(s, i):
    """Baca satu nilai BibTeX mulai di posisi i. Return (nilai, posisi_baru)."""
    n = len(s)
    while i < n and s[i] in " \t\r\n":
        i += 1
    if i >= n:
        return "", i
    if s[i] == "{":
        depth = 0
        j = i
        while j < n:
            if s[j] == "{":
                depth += 1
            elif s[j] == "}":
                depth -= 1
                if depth == 0:
                    return s[i + 1:j], j + 1
            j += 1
        return s[i + 1:], n
    if s[i] == '"':
        j = i + 1
        while j < n and s[j] != '"':
            j += 1
        return s[i + 1:j], min(j + 1, n)
    j = i
    while j < n and s[j] not in ",}":
        j += 1
    return s[i:j].strip(), j


def parse_bibtex(teks):
    """Parse isi file .bib -> list work. Entri rusak dilewati diam-diam."""
    works = []
    s = teks or ""
    pos = 0
    n = len(s)
    while True:
        m = re.search(r"@\s*(\w+)\s*\{", s[pos:])
        if not m:
            break
        tipe = m.group(1).lower()
        i = pos + m.end()  # posisi tepat setelah '{'
        # lewati citekey sampai koma pertama (atau '}' bila entri kosong)
        j = i
        while j < n and s[j] not in ",}":
            j += 1
        if j >= n:
            break
        if s[j] == "}":
            pos = j + 1
            continue
        i = j + 1
        fields = {}
        while i < n:
            fm = re.compile(r"\s*(\w+)\s*=").match(s, i)
            if not fm:
                # cari '}' penutup entri atau koma pemisah
                k = i
                while k < n and s[k] not in ",}":
                    k += 1
                if k >= n:
                    i = n
                    break
                if s[k] == "}":
                    i = k + 1
                    break
                i = k + 1
                continue
            nama_field = fm.group(1).lower()
            val, i = _nilai_bibtex(s, fm.end())
            fields[nama_field] = val
            # lewati koma sesudah nilai
            while i < n and s[i] in " \t\r\n":
                i += 1
            if i < n and s[i] == ",":
                i += 1
                continue
            if i < n and s[i] == "}":
                i += 1
            break
        pos = max(i, pos + m.end())
        if tipe in _TIPE_SKIP:
            continue
        judul = _bersih(fields.get("title"))
        if not judul:
            continue
        penulis = []
        for a in re.split(r"\s+and\s+", fields.get("author", ""),
                          flags=re.I):
            p = _konversi_penulis(a)
            if p:
                penulis.append(p)
        tahun = None
        ym = re.search(r"(19|20)\d{2}", fields.get("year", ""))
        if ym:
            tahun = int(ym.group(0))
        pages = _bersih(fields.get("pages")).replace("--", "-")
        works.append({
            "title": judul,
            "authors": penulis,
            "year": tahun,
            "journal": _bersih(fields.get("journal")
                               or fields.get("booktitle")) or None,
            "volume": _bersih(fields.get("volume")) or None,
            "issue": _bersih(fields.get("number")) or None,
            "pages": pages or None,
            "doi": _bersih(fields.get("doi")) or None,
            "url": _bersih(fields.get("url")) or None,
            "abstract": _bersih(fields.get("abstract")) or None,
        })
    return works


def parse_ris(teks):
    """Parse isi file .ris -> list work. Record diawali TY, diakhiri ER."""
    works = []
    cur = None
    for ln in (teks or "").splitlines():
        m = re.match(r"^([A-Z][A-Z0-9])\s{2}-\s?(.*)$", ln.rstrip())
        if not m:
            continue
        tag, val = m.group(1), m.group(2).strip()
        if tag == "TY":
            cur = {"authors": [], "_sp": None, "_ep": None}
            continue
        if cur is None:
            continue
        if tag == "ER":
            judul = (cur.get("title") or "").strip()
            if judul:
                sp, ep = cur.pop("_sp"), cur.pop("_ep")
                if sp and ep:
                    cur["pages"] = f"{sp}-{ep}"
                elif sp:
                    cur["pages"] = sp
                else:
                    cur.pop("_sp", None)
                    cur.pop("_ep", None)
                works.append(cur)
            cur = None
            continue
        if tag in ("AU", "A1"):
            p = _konversi_penulis(val)
            if p:
                cur["authors"].append(p)
        elif tag in ("TI", "T1"):
            cur["title"] = val
        elif tag in ("JO", "JF", "JA", "T2"):
            cur.setdefault("journal", val)
        elif tag in ("Y1", "PY"):
            ym = re.search(r"(19|20)\d{2}", val)
            if ym:
                cur["year"] = int(ym.group(0))
        elif tag == "VL":
            cur["volume"] = val
        elif tag == "IS":
            cur["issue"] = val
        elif tag == "SP":
            cur["_sp"] = val
        elif tag == "EP":
            cur["_ep"] = val
        elif tag == "DO":
            cur["doi"] = val
        elif tag == "UR":
            cur["url"] = val
        elif tag in ("AB", "N2"):
            cur.setdefault("abstract", val)
    return works


def parse_referensi(teks, nama_file=""):
    """Deteksi format dari nama file / isi, lalu parse.
    Return (works, format) dengan format 'bibtex' / 'ris' / None."""
    nama = (nama_file or "").lower()
    isi = teks or ""
    if nama.endswith(".ris"):
        return parse_ris(isi), "ris"
    if nama.endswith(".bib"):
        return parse_bibtex(isi), "bibtex"
    if re.search(r"^\s*TY\s{2}-", isi, flags=re.M):
        return parse_ris(isi), "ris"
    if re.search(r"@\s*\w+\s*\{", isi):
        return parse_bibtex(isi), "bibtex"
    return [], None
