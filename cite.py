"""Formatter sitasi multi-gaya: Harvard, APA 7, MLA 9, Chicago, Vancouver, IEEE."""
import re


def _initials(given):
    parts = re.split(r"[\s.\-]+", given.strip())
    return " ".join(p[0].upper() + "." for p in parts if p)


def _split_name(full):
    """'Charles D. Reese' -> ('Reese', 'Charles D.'). Nama satu kata dikembalikan apa adanya."""
    full = re.sub(r"\s+", " ", full.strip())
    if not full:
        return None
    # format sudah "Family, Given"?
    if "," in full:
        fam, giv = [x.strip() for x in full.split(",", 1)]
        return (fam, _initials(giv)) if giv else (fam, "")
    bits = full.split(" ")
    if len(bits) == 1:
        return (bits[0], "")
    return (bits[-1], _initials(" ".join(bits[:-1])))


def _format_authors_harvard(authors):
    parsed = []
    for a in authors or []:
        p = _split_name(a)
        if p:
            fam, ini = p
            parsed.append(f"{fam}, {ini}".strip().rstrip(","))
    if not parsed:
        return ""
    if len(parsed) == 1:
        return parsed[0]
    if len(parsed) == 2:
        return f"{parsed[0]} and {parsed[1]}"
    if len(parsed) == 3:
        return f"{parsed[0]}, {parsed[1]} and {parsed[2]}"
    return f"{parsed[0]} et al."


def _sentence_case(title):
    if not title:
        return ""
    t = title.strip()
    # judul full kapital (umum di jurnal Indonesia) -> ubah ke sentence case
    if t.isupper():
        return t[0].upper() + t[1:].lower()
    # jangan ubah singkatan/kata berhuruf kapital di tengah (e.g. COVID-19, Indonesia)
    if re.search(r"[A-Z]{2,}", t):
        return t[0].upper() + t[1:] if t else t
    return t[0].upper() + t[1:].lower() if t else t


def _split_name_full(full):
    """'Charles D. Reese' -> ('Reese', 'Charles D.'). Versi nama depan utuh."""
    full = re.sub(r"\s+", " ", full.strip())
    if not full:
        return None
    if "," in full:
        fam, giv = [x.strip() for x in full.split(",", 1)]
        return (fam, giv)
    bits = full.split(" ")
    if len(bits) == 1:
        return (bits[0], "")
    return (bits[-1], " ".join(bits[:-1]))


def _initials_plain(given):
    """Inisial tanpa titik: 'Charles D.' -> 'CD' (Vancouver)."""
    parts = re.split(r"[\s.\-]+", given.strip())
    return "".join(p[0].upper() for p in parts if p)


def _title_case(title):
    if not title:
        return ""
    t = title.strip()
    if t.isupper():
        t = t[0].upper() + t[1:].lower()
    small = {"a", "an", "the", "and", "but", "or", "for", "nor", "on", "at",
             "to", "from", "by", "of", "in", "vs", "vs.", "v", "v."}
    out = []
    for i, w_ in enumerate(t.split()):
        core = re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$", "", w_)
        if not core:
            out.append(w_)
        elif i > 0 and core.lower() in small and not re.search(r"[A-Z]{2,}", core):
            out.append(w_.lower())
        elif re.search(r"[A-Z]{2,}", core) or re.search(r"\d", core):
            out.append(w_)  # akronim / angka dipertahankan
        else:
            out.append(w_[0].upper() + w_[1:].lower())
    res = " ".join(out)
    return res[0].upper() + res[1:] if res else res


def _pages_pp(pages):
    if not pages:
        return ""
    return f"pp. {pages}" if "-" in str(pages) else f"p. {pages}"


def _doi_or_url(w):
    if w.get("doi"):
        return f"https://doi.org/{w['doi']}"
    return w.get("url") or ""


# ---------------------------------------------------------------- Harvard
def harvard(w):
    """w: dict format unified dari modul search_*. Return string sitasi Harvard.

    Contoh: Sigar, S. K. (2020) 'Article title', *Journal*, 10(2),
             pp. 100-110. Available at: https://doi.org/10.xxxx
    Tanda *...* = italic (dikonversi ke HTML oleh pemanggil).
    """
    authors = _format_authors_harvard(w.get("authors"))
    year = w.get("year") or "n.d."
    title = _sentence_case(w.get("title"))
    journal = (w.get("journal") or "").strip()
    vol = w.get("volume")
    issue = w.get("issue")
    pages = w.get("pages")
    doi = w.get("doi")

    parts = [f"{authors} ({year})" if authors else f"Anon. ({year})"]
    parts.append(f"'{title}',")
    if journal:
        jpart = f"*{journal}*"
        if vol:
            jpart += f", {vol}"
            if issue:
                jpart += f"({issue})"
        if pages:
            jpart += f", pp. {pages}" if "-" in str(pages) else f", p. {pages}"
        parts.append(jpart + ".")
    if doi:
        parts.append(f"Available at: https://doi.org/{doi}")
    elif w.get("url"):
        parts.append(f"Available at: {w['url']}")
    return " ".join(parts)


# ------------------------------------------------------------------ APA 7
def _format_authors_apa(authors):
    parsed = []
    for a in authors or []:
        p = _split_name(a)
        if p:
            fam, ini = p
            parsed.append(f"{fam}, {ini}".strip().rstrip(","))
    if not parsed:
        return ""
    if len(parsed) == 1:
        return parsed[0]
    if len(parsed) <= 20:
        return ", ".join(parsed[:-1]) + ", & " + parsed[-1]
    return ", ".join(parsed[:19]) + ", … " + parsed[-1]


def apa7(w):
    """APA edisi 7.
    Smith, J. D., & Doe, A. (2020). Title in sentence case. *Journal*, *10*(2), 100-110.
    """
    authors = _format_authors_apa(w.get("authors"))
    year = w.get("year") or "n.d."
    title = _sentence_case(w.get("title"))
    journal = (w.get("journal") or "").strip()
    vol, issue, pages = w.get("volume"), w.get("issue"), w.get("pages")
    link = _doi_or_url(w)

    parts = [f"{authors} ({year})." if authors else f"({year}).",
             f"{title}."]
    if journal:
        j = f"*{journal}*"
        if vol:
            j += f", *{vol}*"
            if issue:
                j += f"({issue})"
        if pages:
            j += f", {pages}"
        parts.append(j + ".")
    if link:
        parts.append(link)
    return " ".join(parts)


# ------------------------------------------------------------------ MLA 9
def _format_authors_mla(authors):
    parsed = []
    for a in authors or []:
        p = _split_name_full(a)
        if p:
            fam, giv = p
            parsed.append((fam, giv))
    if not parsed:
        return ""
    first = f"{parsed[0][0]}, {parsed[0][1]}".strip().rstrip(",")
    if len(parsed) == 1:
        return first + "."
    if len(parsed) == 2:
        return f"{first}, and {parsed[1][1]} {parsed[1][0]}.".strip()
    return f"{first}, et al.".strip()


def mla9(w):
    """MLA edisi 9.
    Reese, Charles D. "Title in Title Case." *Journal*, vol. 10, no. 2, 2020, pp. 100-110.
    """
    authors = _format_authors_mla(w.get("authors"))
    title = _title_case(w.get("title"))
    journal = (w.get("journal") or "").strip()
    vol, issue, pages = w.get("volume"), w.get("issue"), w.get("pages")
    year = w.get("year") or "n.d."
    link = _doi_or_url(w)

    parts = []
    parts.append(f"{authors}" if authors else "")
    parts.append(f'"{title}."')
    if journal:
        j = f"*{journal}*"
        if vol:
            j += f", vol. {vol}"
        if issue:
            j += f", no. {issue}"
        j += f", {year}"
        if pages:
            j += f", {_pages_pp(pages)}"
        parts.append(j + ".")
    else:
        parts.append(f"{year}.")
    if link:
        parts.append(link + ".")
    return " ".join(x for x in parts if x)


# ---------------------------------------------------------------- Chicago
def _format_authors_chicago(authors):
    parsed = []
    for a in authors or []:
        p = _split_name_full(a)
        if p:
            fam, giv = p
            parsed.append((fam, giv))
    if not parsed:
        return ""
    first = f"{parsed[0][0]}, {parsed[0][1]}".strip().rstrip(",")
    rest = [f"{g} {f}".strip() for f, g in parsed[1:]]
    if len(parsed) == 1:
        return first + "."
    if len(parsed) <= 3:
        return f"{first}, " + ", ".join(rest[:-1] + ["and " + rest[-1]]) + "."
    return f"{first}, et al."


def chicago(w):
    """Chicago author-date.
    Reese, Charles D. 2020. "Title in Title Case." *Journal* 10 (2): 100-110.
    """
    authors = _format_authors_chicago(w.get("authors"))
    title = _title_case(w.get("title"))
    journal = (w.get("journal") or "").strip()
    vol, issue, pages = w.get("volume"), w.get("issue"), w.get("pages")
    year = w.get("year") or "n.d."
    link = _doi_or_url(w)

    parts = []
    if authors:
        parts.append(f"{authors} {year}.")
    else:
        parts.append(f"{year}.")
    parts.append(f'"{title}."')
    if journal:
        j = f"*{journal}*"
        if vol:
            j += f" {vol}"
            if issue:
                j += f" ({issue})"
        if pages:
            j += f": {pages}"
        parts.append(j + ".")
    if link:
        parts.append(link + ".")
    return " ".join(parts)


# --------------------------------------------------------------- Vancouver
def _format_authors_vancouver(authors):
    parsed = []
    for a in authors or []:
        p = _split_name(a)
        if p:
            fam, ini = p
            parsed.append(f"{fam} {_initials_plain(ini)}".strip())
    if not parsed:
        return ""
    if len(parsed) <= 6:
        return ", ".join(parsed)
    return ", ".join(parsed[:6]) + ", et al"


def vancouver(w):
    """Vancouver (tanpa nomor urut — nomor ditambah pemanggil bila perlu).
    Hayati A, Nabila CH. Title in sentence case. Journal. 2022;4(2):10-15.
    """
    authors = _format_authors_vancouver(w.get("authors"))
    title = _sentence_case(w.get("title"))
    journal = (w.get("journal") or "").strip()
    vol, issue, pages = w.get("volume"), w.get("issue"), w.get("pages")
    year = w.get("year") or "t.t."
    doi = w.get("doi")

    parts = []
    if authors:
        parts.append(f"{authors}.")
    parts.append(f"{title}.")
    if journal:
        j = journal + f". {year}"
        if vol:
            j += f";{vol}"
            if issue:
                j += f"({issue})"
        if pages:
            j += f":{pages}"
        parts.append(j + ".")
    else:
        parts.append(f"{year}.")
    if doi:
        parts.append(f"doi: {doi}")
    elif w.get("url"):
        parts.append(f"Tersedia di: {w['url']}")
    return " ".join(parts)


# ------------------------------------------------------------------- IEEE
def _format_authors_ieee(authors):
    parsed = []
    for a in authors or []:
        p = _split_name(a)
        if p:
            fam, ini = p
            parsed.append(f"{ini} {fam}".strip())
    if not parsed:
        return ""
    if len(parsed) <= 6:
        return ", ".join(parsed)
    return ", ".join(parsed[:6]) + ", et al."


def ieee(w):
    """IEEE (tanpa nomor urut — nomor ditambah pemanggil bila perlu).
    A. Hayati, "Title in sentence case," *Journal*, vol. 4, no. 2, pp. 10-15, 2022.
    """
    authors = _format_authors_ieee(w.get("authors"))
    title = _sentence_case(w.get("title"))
    journal = (w.get("journal") or "").strip()
    vol, issue, pages = w.get("volume"), w.get("issue"), w.get("pages")
    year = w.get("year") or "t.t."
    doi = w.get("doi")

    parts = []
    if authors:
        parts.append(f"{authors},")
    parts.append(f'"{title},"')
    if journal:
        j = f"*{journal}*"
        if vol:
            j += f", vol. {vol}"
        if issue:
            j += f", no. {issue}"
        if pages:
            j += f", {_pages_pp(pages)}"
        j += f", {year}"
        parts.append(j + ".")
    if doi:
        parts.append(f"doi: {doi}.")
    elif w.get("url"):
        parts.append(w["url"])
    return " ".join(parts)


# ------------------------------------------------------- sitasi website
_MONTHS = ["", "January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"]
_MONTHS_S = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _web_date_apa(w):
    y, m, d = w.get("year"), w.get("month"), w.get("day")
    if y and m and d:
        return f"{y}, {_MONTHS[m]} {d}"
    if y and m:
        return f"{y}, {_MONTHS[m]}"
    return str(y) if y else "n.d."


def _web_date_long(w):
    y, m, d = w.get("year"), w.get("month"), w.get("day")
    if y and m and d:
        return f"{d} {_MONTHS[m]} {y}"
    return str(y) if y else "n.d."


def _web_date_us(w):
    y, m, d = w.get("year"), w.get("month"), w.get("day")
    if y and m and d:
        return f"{_MONTHS[m]} {d}, {y}"
    return str(y) if y else "n.d."


def harvard_web(w):
    authors = _format_authors_harvard(w.get("authors")) or (w.get("site") or "Anon.")
    year = w.get("year") or "n.d."
    title = _sentence_case(w.get("title"))
    site = (w.get("site") or "").strip()
    acc = w.get("accessed") or ""
    parts = [f"{authors} ({year})", f"'{title}',"]
    if site:
        parts.append(f"*{site}*.")
    parts.append(f"Available at: {w.get('url') or ''}")
    if acc:
        parts.append(f"(Accessed: {acc}).")
    return " ".join(x for x in parts if x)


def apa7_web(w):
    authors = _format_authors_apa(w.get("authors")) or (w.get("site") or "")
    title = _sentence_case(w.get("title"))
    site = (w.get("site") or "").strip()
    parts = [f"{authors} ({_web_date_apa(w)})." if authors else f"({_web_date_apa(w)}).",
             f"{title}."]
    if site and site not in authors:
        parts.append(f"*{site}*.")
    if w.get("url"):
        parts.append(w["url"])
    return " ".join(x for x in parts if x)


def mla9_web(w):
    authors = _format_authors_mla(w.get("authors"))
    site = (w.get("site") or "").strip()
    title = _title_case(w.get("title"))
    parts = []
    parts.append(authors if authors else (site + "." if site else ""))
    parts.append(f'"{title}."')
    if site:
        parts.append(f"*{site}*,")
    parts.append(_web_date_long(w) + ",")
    parts.append((w.get("url") or "") + ".")
    return " ".join(x for x in parts if x)


def chicago_web(w):
    authors = _format_authors_chicago(w.get("authors"))
    site = (w.get("site") or "").strip()
    title = _title_case(w.get("title"))
    year = w.get("year") or "n.d."
    m, d = w.get("month"), w.get("day")
    if m and d and w.get("year"):
        date = f"{_MONTHS[m]} {d}"
    else:
        date = str(year)
    parts = []
    parts.append(f"{authors} {year}." if authors else (f"{site}. {year}." if site else f"{year}."))
    parts.append(f'"{title}."')
    if site:
        parts.append(f"*{site}*, {date}.")
    if w.get("url"):
        parts.append(w["url"] + ".")
    return " ".join(x for x in parts if x)


def vancouver_web(w):
    authors = _format_authors_vancouver(w.get("authors")) or (w.get("site") or "")
    title = _sentence_case(w.get("title"))
    site = (w.get("site") or "").strip()
    year = w.get("year") or "t.t."
    cited = w.get("accessed_short") or ""
    parts = []
    if authors:
        parts.append(f"{authors}.")
    parts.append(f"{title} [Internet].")
    if site:
        parts.append(f"{site}; {year}")
    else:
        parts.append(f"{year}")
    if cited:
        parts.append(f"[cited {cited}].")
    if w.get("url"):
        parts.append(f"Available from: {w['url']}")
    return " ".join(x for x in parts if x)


def ieee_web(w):
    authors = _format_authors_ieee(w.get("authors")) or (w.get("site") or "")
    title = _title_case(w.get("title"))
    site = (w.get("site") or "").strip()
    parts = []
    if authors:
        parts.append(f"{authors},")
    parts.append(f'"{title},"')
    if site:
        parts.append(f"*{site}*, {_web_date_us(w)}.")
    else:
        parts.append(f"{_web_date_us(w)}.")
    parts.append("[Online].")
    if w.get("url"):
        parts.append(f"Available: {w['url']}")
    return " ".join(x for x in parts if x)


WEB_STYLES = {
    "harvard": ("Harvard", harvard_web),
    "apa": ("APA 7", apa7_web),
    "mla": ("MLA 9", mla9_web),
    "chicago": ("Chicago", chicago_web),
    "vancouver": ("Vancouver", vancouver_web),
    "ieee": ("IEEE", ieee_web),
}


def cite_website_in(w, key):
    """Format sitasi website gaya `key` (fallback Harvard)."""
    return WEB_STYLES.get(key, WEB_STYLES["harvard"])[1](w)


# --------------------------------------------------------------- registry
STYLES = {
    "harvard": ("Harvard", harvard),
    "apa": ("APA 7", apa7),
    "mla": ("MLA 9", mla9),
    "chicago": ("Chicago", chicago),
    "vancouver": ("Vancouver", vancouver),
    "ieee": ("IEEE", ieee),
}
STYLE_ORDER = ["harvard", "apa", "mla", "chicago", "vancouver", "ieee"]


def style_label(key):
    return STYLES.get(key, STYLES["harvard"])[0]


def cite_in(w, key):
    """Format sitasi gaya `key` (fallback Harvard). Tanda *...* = italic."""
    return STYLES.get(key, STYLES["harvard"])[1](w)
