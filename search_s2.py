"""Sumber cadangan: Semantic Scholar Academic Graph API.

Dipakai sebagai fallback bila OpenAlex kena rate-limit (429):
rantai pencarian = OpenAlex -> Semantic Scholar -> Crossref.

Butuh (opsional tapi SANGAT disarankan) API key gratis dari
https://www.semanticscholar.org/product/api#api-key
disimpan di env SEMANTIC_SCHOLAR_API_KEY. Tanpa key, limitnya pool
bersama (100 req / 5 mnt, sering 429 dari IP shared). Dengan key,
kuota dedicated ~1 request/detik — modul ini menjaga jarak >=1,1 dtk
antar request agar tidak melanggar.
"""

import logging
import os
import threading
import time

import httpx

log = logging.getLogger("jurnalbot.s2")

S2 = "https://api.semanticscholar.org/graph/v1"
UA = {"User-Agent": "KhairyPustaka/1.0 (Telegram bot)"}

_S2_LOCK = threading.Lock()
_S2_LAST = 0.0
_S2_GAP = 1.1  # detik antar request (kuota key ~1 req/detik)


def _clean_doi(doi):
    if not doi:
        return None
    d = str(doi).strip().lower()
    for p in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(p):
            d = d[len(p):]
    return d or None


def _s2_get(path, params=None, timeout=10, tries=2):
    """GET ke Semantic Scholar: throttle 1,1 dtk + retry 429 + x-api-key."""
    headers = dict(UA)
    key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "").strip()
    if key:
        headers["x-api-key"] = key
    last_err = None
    for attempt in range(tries):
        with _S2_LOCK:
            global _S2_LAST
            gap = _S2_GAP - (time.time() - _S2_LAST)
            if gap > 0:
                time.sleep(gap)
            try:
                r = httpx.get(S2 + path, params=params, headers=headers,
                              timeout=timeout)
            finally:
                _S2_LAST = time.time()
        if r.status_code == 429:
            # 429 -> jangan berlama-lama; rantai fallback (Crossref) lebih cepat
            if attempt == tries - 1:
                r.raise_for_status()
            log.warning("S2 429 — tunggu 2s (percobaan %d/%d)", attempt + 1, tries)
            time.sleep(2)
            continue
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r
    raise last_err if last_err else RuntimeError("Semantic Scholar gagal")


_PAPER_FIELDS = ("title,abstract,year,authors,venue,url,openAccessPdf,"
                 "externalIds,citationCount")


def _norm_s2_paper(p):
    ext = p.get("externalIds") or {}
    doi = _clean_doi(ext.get("DOI"))
    oapdf = p.get("openAccessPdf") or {}
    pdf = oapdf.get("url")
    return {
        "title": p.get("title"),
        "authors": [a.get("name") for a in (p.get("authors") or [])
                    if a.get("name")],
        "year": p.get("year"),
        "journal": p.get("venue"),
        "doi": doi,
        "url": (f"https://doi.org/{doi}" if doi else None) or p.get("url"),
        "abstract": p.get("abstract"),
        "cited_by": p.get("citationCount"),
        "origin": "🌍 Internasional",
        "extra": "Open Access 🟢" if pdf else None,
        "volume": None, "issue": None, "pages": None,
        # --- field tambahan untuk fitur lanjutan ---
        "openalex_id": None,
        "pdf_url": pdf,
        "is_oa": bool(pdf),
        "doaj": False,
        "publisher": None,
        "s2_id": p.get("paperId"),
        "src": "s2",
    }


def s2_search_papers(query, per_page=6, timeout=10):
    """Cari karya via Semantic Scholar. Return list[dict] format unified
    ([] bila gagal/kosong — pemanggil lanjut ke sumber berikut)."""
    try:
        r = _s2_get("/paper/search",
                    params={"query": query, "limit": per_page,
                            "fields": _PAPER_FIELDS},
                    timeout=timeout)
        if r is None:
            return []
        items = r.json().get("data", []) or []
    except Exception as e:
        log.warning("S2 search gagal: %s", e)
        return []
    out = []
    for p in items:
        try:
            w = _norm_s2_paper(p)
            if w.get("title"):
                out.append(w)
        except Exception as e:
            log.warning("parse S2 gagal: %s", e)
    return out


def s2_fetch_doi(doi, timeout=10):
    """Ambil metadata satu karya via DOI. Return dict atau None."""
    doi = _clean_doi(doi)
    if not doi:
        return None
    try:
        r = _s2_get(f"/paper/DOI:{doi}",
                    params={"fields": _PAPER_FIELDS}, timeout=timeout)
        if r is None:
            return None
        return _norm_s2_paper(r.json())
    except Exception as e:
        log.warning("S2 fetch DOI gagal %s: %s", doi, e)
        return None


def s2_search_authors(name, per_page=5, timeout=10):
    """Cari penulis via Semantic Scholar.

    Return list dict format sama seperti search_authors (OpenAlex),
    atau None bila request gagal total.
    """
    try:
        r = _s2_get("/author/search",
                    params={"query": name, "limit": per_page,
                            "fields": "name,hIndex,citationCount,paperCount,"
                                      "affiliations,url"},
                    timeout=timeout)
        if r is None:
            return []
        items = r.json().get("data", []) or []
    except Exception as e:
        log.warning("S2 search authors gagal %s: %s", name, e)
        return None
    out = []
    for a in items:
        aff = []
        for x in (a.get("affiliations") or []):
            if isinstance(x, str) and x not in aff:
                aff.append(x)
            elif isinstance(x, dict) and x.get("name"):
                aff.append(x["name"])
        aid = a.get("authorId")
        out.append({
            "name": a.get("name"),
            "h_index": a.get("hIndex"),
            "i10_index": None,
            "works_count": a.get("paperCount"),
            "cited_by_count": a.get("citationCount"),
            "affiliations": aff[:3],
            "topics": [],
            "url": a.get("url") or (f"https://www.semanticscholar.org/author/{aid}"
                                    if aid else None),
        })
    return out
