"""Ambil metadata artikel dari link website atau file PDF — untuk /sitasi.

- Link artikel: baca meta tag citation_* (dipakai mayoritas situs jurnal),
  fallback OpenGraph. Kalau ada DOI -> kembalikan DOI-nya saja.
- File PDF: ekstrak teks 3 halaman pertama, cari DOI di dalamnya.
"""
import logging
import re
import urllib.request
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)


class _SSRFBlocked(Exception):
    """URL mengarah ke host internal — diblokir."""


def _host_is_public(url):
    """True bila URL http(s) dan SEMUA IP hasil resolusi adalah publik.

    Menolak: skema non-http(s), userinfo, host tak teresolusi,
    IP privat/loopback/link-local/reserved (127.0.0.1, 10/8,
    192.168/16, 169.254.169.254, ::1, ...). Fail-closed.
    """
    from urllib.parse import urlparse
    import ipaddress
    import socket
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return False
        if "@" in p.netloc:
            return False
        host = p.hostname
        if not host:
            return False
        port = p.port or (443 if p.scheme == "https" else 80)
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not infos:
            return False
        for _fam, _typ, _proto, _canon, sockaddr in infos:
            if not ipaddress.ip_address(sockaddr[0]).is_global:
                return False
        return True
    except Exception:
        return False


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Ikuti redirect HANYA bila target tetap host publik."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _host_is_public(newurl):
            raise _SSRFBlocked(f"redirect ke host internal diblokir: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)

_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\]\)\"'<>]+", re.I)
_UA = {"User-Agent": "Mozilla/5.0 (KhairyPustakaBot/1.0)"}
_MONTHS = ["", "January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"]
_MONTHS_S = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def doi_in_text(t):
    m = _DOI_RE.search(t or "")
    return m.group(0).rstrip(".,;)") if m else None


def _parse_pub_date(s):
    """'2021/05/03' / '2021-05-03T..' -> (2021, 5, 3)."""
    m = re.search(r"((?:19|20)\d{2})[-/](\d{1,2})[-/](\d{1,2})", s or "")
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    m = re.search(r"((?:19|20)\d{2})", s or "")
    if m:
        return int(m.group(1)), None, None
    return None, None, None


def _site_name(soup, url):
    site = _meta(soup, "og:site_name")
    if site:
        return site
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def _meta(soup, name):
    tag = (soup.find("meta", attrs={"name": name})
           or soup.find("meta", attrs={"property": name}))
    return (tag.get("content", "").strip() if tag and tag.get("content") else "")


def fetch_metadata_from_url(url):
    """Return (kind, data):
    - ("doi", "10.xxxx/...") -> tinggal fetch_by_doi
    - ("meta", work_dict) -> metadata dari meta tag
    - (None, None) -> gagal / tidak dikenali
    """
    url = (url or "").strip()
    if not url:
        return (None, None)
    if not url.startswith("http"):
        url = "https://" + url
    d = doi_in_text(url)
    if d:
        return ("doi", d)
    if not _host_is_public(url):
        log.warning("SSRF diblokir, host tidak publik: %s", url)
        return (None, None)
    try:
        opener = urllib.request.build_opener(_SafeRedirectHandler)
        req = Request(url, headers=_UA)
        with opener.open(req, timeout=20) as r:
            html = r.read(1_500_000).decode("utf-8", "ignore")
    except Exception as ex:
        log.warning("fetch url gagal %s: %s", url, ex)
        return (None, None)
    d = doi_in_text(html)
    if d:
        return ("doi", d)
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return (None, None)
    soup = BeautifulSoup(html, "lxml")
    if _meta(soup, "citation_doi"):
        return ("doi", _meta(soup, "citation_doi"))
    title = _meta(soup, "citation_title") or _meta(soup, "og:title")
    if not title:
        return (None, None)
    journal = _meta(soup, "citation_journal_title")
    if journal:
        # --- artikel jurnal ---
        authors = [t.get("content", "").strip()
                   for t in soup.find_all("meta", attrs={"name": "citation_author"})
                   if t.get("content")]
        date = (_meta(soup, "citation_publication_date")
                or _meta(soup, "citation_date")
                or _meta(soup, "article:published_time"))
        ym = re.search(r"(19|20)\d{2}", date or "")
        work = {
            "title": title,
            "authors": authors,
            "year": int(ym.group(0)) if ym else None,
            "journal": journal,
            "doi": _meta(soup, "citation_doi") or None,
            "url": url, "pdf_url": None, "abstract": None,
            "cited_by": 0, "oa": False,
        }
        return ("meta", work)
    # --- halaman website biasa (artikel web, berita, dsb.) ---
    authors = [t.get("content", "").strip()
               for t in soup.find_all("meta", attrs={"name": "citation_author"})
               if t.get("content")]
    if not authors:
        a = _meta(soup, "article:author") or _meta(soup, "author")
        if a:
            authors = [a]
    y, m, d = _parse_pub_date(
        _meta(soup, "citation_publication_date")
        or _meta(soup, "article:published_time")
        or _meta(soup, "datePublished"))
    from datetime import datetime
    now = datetime.now()
    work = {
        "is_website": True,
        "title": title,
        "authors": authors,
        "year": y, "month": m, "day": d,
        "site": _site_name(soup, url),
        "url": url,
        "accessed": f"{now.day} {_MONTHS[now.month]} {now.year}",
        "accessed_short": f"{now.year} {_MONTHS_S[now.month]} {now.day}",
    }
    return ("web", work)


def doi_from_pdf(path):
    """Ekstrak DOI dari 3 halaman pertama PDF. Return DOI atau None."""
    try:
        from pypdf import PdfReader
    except ImportError:
        log.warning("pypdf belum terinstal")
        return None
    try:
        reader = PdfReader(path)
    except Exception as ex:
        log.warning("baca PDF gagal: %s", ex)
        return None
    text = ""
    for page in reader.pages[:3]:
        try:
            text += "\n" + (page.extract_text() or "")
        except Exception:
            break
    return doi_in_text(text)
