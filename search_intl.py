"""Pencarian jurnal internasional via OpenAlex (primer) dan Crossref (fallback).

OpenAlex: gratis, tanpa API key.
https://docs.openalex.org/

Rantai fallback pencarian: OpenAlex -> Semantic Scholar -> Crossref
(lihat search_s2.py). Circuit breaker: bila OpenAlex gagal, ia dilewati
sementara agar bot tetap responsif.
"""
import json
import logging
import os
import re
import threading
import time
import urllib.parse

import httpx

from search_s2 import s2_fetch_doi, s2_search_authors, s2_search_papers

# --- Circuit breaker OpenAlex ---
# Bila OpenAlex gagal (429/timeout/koneksi), jangan hantam terus:
# lewati OpenAlex selama beberapa menit agar fallback (S2/Crossref)
# langsung dipakai dan bot tetap responsif. Pulih otomatis.
_OA_CB_UNTIL = 0.0
_OA_CB_LOCK = threading.Lock()
_OA_CB_SECONDS = 180


def _oa_circuit_open():
    return time.time() < _OA_CB_UNTIL


def _oa_note_failure(exc):
    """Catat kegagalan OpenAlex; 404 bukan outage jadi tidak dihitung."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 404:
        return
    global _OA_CB_UNTIL
    with _OA_CB_LOCK:
        _OA_CB_UNTIL = time.time() + _OA_CB_SECONDS
    log.warning("OpenAlex circuit breaker: skip %.0f dtk", _OA_CB_SECONDS)

log = logging.getLogger(__name__)

UA = {"User-Agent": "KhairyPustaka/1.0 (Telegram bot)"}
OPENALEX = "https://api.openalex.org"
CROSSREF = "https://api.crossref.org"


class _CircuitOpen(RuntimeError):
    """OpenAlex dilewati sementara oleh circuit breaker."""


def _oa_get(url, params=None, timeout=10, tries=2):
    """GET ke OpenAlex dengan perlakuan sopan:

    - sertakan ``mailto`` dari env OPENALEX_MAILTO (masuk polite pool),
    - circuit breaker: bila OpenAlex baru saja gagal, raise _CircuitOpen
      langsung tanpa request (berlaku untuk SEMUA pemanggil),
    - 429 -> langsung raise TANPA menunggu: rantai fallback
      (Semantic Scholar -> Crossref) yang menangani,
    - error koneksi/timeout -> coba 1x lagi selang 1 dtk.
    """
    if _oa_circuit_open():
        raise _CircuitOpen("OpenAlex dilewati (circuit breaker)")
    params = dict(params or {})
    mailto = os.environ.get("OPENALEX_MAILTO", "").strip()
    if mailto and "mailto" not in params:
        params["mailto"] = mailto
    last_err = None
    for attempt in range(tries):
        try:
            r = httpx.get(url, params=params, headers=UA, timeout=timeout)
            r.raise_for_status()  # 429 langsung raise -> fallback cepat
            return r
        except httpx.HTTPStatusError as e:
            _oa_note_failure(e)  # 404 tidak dihitung sebagai outage
            raise
        except Exception as e:
            last_err = e
            log.warning("OpenAlex error (percobaan %d/%d): %s", attempt + 1, tries, e)
            if attempt < tries - 1:
                time.sleep(1)
    _oa_note_failure(last_err)
    raise last_err if last_err else RuntimeError("OpenAlex gagal")


def _reconstruct_abstract(inv):
    """OpenAlex menyimpan abstrak sebagai inverted index -> kembalikan teks."""
    if not inv:
        return None
    try:
        pos = {}
        for word, positions in inv.items():
            for p in positions:
                pos[p] = word
        return " ".join(pos[i] for i in range(len(pos)))
    except Exception:
        return None


def _clean_doi(doi):
    if not doi:
        return None
    doi = doi.strip()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I)
    return doi or None


def _norm_openalex(w):
    authors = []
    for a in w.get("authorships", []) or []:
        name = (a.get("author") or {}).get("display_name") or a.get("raw_author_name")
        if name:
            authors.append(name)
    loc = w.get("primary_location") or {}
    src = loc.get("source") or {}
    biblio = w.get("biblio") or {}
    pages = None
    if biblio.get("first_page"):
        pages = biblio["first_page"]
        if biblio.get("last_page"):
            pages += "-" + biblio["last_page"]
    oa = w.get("open_access") or {}
    best = w.get("best_oa_location") or {}
    ox_id = (w.get("id") or "").rsplit("/", 1)[-1] or None
    return {
        "title": w.get("title"),
        "authors": authors,
        "year": w.get("publication_year"),
        "journal": src.get("display_name"),
        "doi": _clean_doi(w.get("doi")),
        "url": (w.get("doi") or loc.get("landing_page_url")
                or (w.get("id") or "").replace("https://openalex.org/", "https://openalex.org/")),
        "abstract": _reconstruct_abstract(w.get("abstract_inverted_index")),
        "cited_by": w.get("cited_by_count"),
        "origin": "🌍 Internasional",
        "extra": "Open Access 🟢" if oa.get("is_oa") else None,
        "volume": biblio.get("volume"),
        "issue": biblio.get("issue"),
        "pages": pages,
        # --- field tambahan untuk fitur lanjutan ---
        "openalex_id": ox_id,                       # mis. "W2741809807"
        "pdf_url": best.get("pdf_url"),             # link PDF bila OA
        "is_oa": bool(oa.get("is_oa")),
        "doaj": bool(src.get("is_in_doaj")),
        "publisher": src.get("host_organization_name"),  # untuk agregasi /publisher
    }


def search_openalex(query, per_page=6, timeout=10, sort=None):
    """Cari karya ilmiah di OpenAlex. Return list[dict] format unified."""
    params = {
        "search": query,
        "per-page": per_page,
        "select": "id,doi,title,authorships,publication_year,primary_location,"
                  "abstract_inverted_index,cited_by_count,biblio,open_access,"
                  "best_oa_location",
    }
    if sort:
        params["sort"] = sort
    try:
        r = _oa_get(f"{OPENALEX}/works", params=params, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning("OpenAlex gagal: %s", e)
        return []
    out = []
    for w in data.get("results", []):
        try:
            out.append(_norm_openalex(w))
        except Exception as e:
            log.warning("parse OpenAlex gagal: %s", e)
    return [w for w in out if w.get("title")]


def _strip_tags(t):
    return re.sub(r"<[^>]+>", "", t or "").strip()


def search_crossref(query, rows=6, timeout=10):
    """Fallback via Crossref bila OpenAlex bermasalah."""
    try:
        params = {"query": query, "rows": rows,
                  "select": "DOI,title,author,published,container-title,URL,abstract"}
        mailto = os.environ.get("OPENALEX_MAILTO", "").strip()
        if mailto:
            params["mailto"] = mailto  # Crossref polite pool
        r = httpx.get(f"{CROSSREF}/works", params=params,
                      headers=UA, timeout=timeout)
        r.raise_for_status()
        items = r.json().get("message", {}).get("items", [])
    except Exception as e:
        log.warning("Crossref gagal: %s", e)
        return []
    out = []
    for it in items:
        authors = []
        for a in it.get("author", []) or []:
            nm = " ".join(x for x in [a.get("given"), a.get("family")] if x)
            if nm:
                authors.append(nm)
        pub = it.get("published") or {}
        year = (pub.get("date-parts") or [[None]])[0][0]
        cont = it.get("container-title") or []
        out.append({
            "title": (it.get("title") or [None])[0],
            "authors": authors,
            "year": year,
            "journal": cont[0] if cont else None,
            "doi": _clean_doi(it.get("DOI")),
            "url": it.get("URL") or (f"https://doi.org/{it.get('DOI')}" if it.get("DOI") else None),
            "abstract": _strip_tags(it.get("abstract")) or None,
            "cited_by": None,
            "origin": "🌍 Internasional",
            "extra": None,
            "volume": None, "issue": None, "pages": None,
            "openalex_id": None, "pdf_url": None, "is_oa": False, "doaj": False,
            "publisher": None,
        })
    return [w for w in out if w.get("title")]


def search_international(query, per_page=6):
    """Rantai sumber internasional: OpenAlex -> Semantic Scholar -> Crossref.

    OpenAlex utama (data terlengkap). Bila kosong/gagal (mis. 429),
    otomatis lanjut ke Semantic Scholar, lalu Crossref sebagai pelengkap.
    """
    res = search_openalex(query, per_page)
    if not res:
        log.warning("OpenAlex kosong/gagal utk %r — fallback Semantic Scholar", query)
        res = s2_search_papers(query, per_page)
    if len(res) < 3:
        seen = {w.get("doi") for w in res if w.get("doi")}
        for w in search_crossref(query, per_page):
            if w.get("doi") and w["doi"] not in seen:
                res.append(w)
                seen.add(w["doi"])
    return res[:per_page]


def fetch_by_doi(doi, timeout=10):
    """Ambil metadata satu karya via DOI (OpenAlex dulu, lalu Crossref)."""
    doi = _clean_doi(doi)
    if not doi:
        return None
    try:
        r = _oa_get(f"{OPENALEX}/works/https://doi.org/{doi}", timeout=timeout)
        if r.status_code == 200:
            return _norm_openalex(r.json())
    except Exception as e:
        log.warning("fetch DOI OpenAlex gagal: %s", e)
    try:
        w = s2_fetch_doi(doi, timeout=timeout)
        if w:
            return w
    except Exception as e:
        log.warning("fetch DOI S2 gagal: %s", e)
    try:
        cr_params = {}
        _cr_mailto = os.environ.get("OPENALEX_MAILTO", "").strip()
        if _cr_mailto:
            cr_params["mailto"] = _cr_mailto
        r = httpx.get(f"{CROSSREF}/works/{doi}", params=cr_params or None,
                      headers=UA, timeout=timeout)
        if r.status_code == 200:
            it = r.json().get("message", {})
            # bangun manual dari item crossref
            authors = []
            for a in it.get("author", []) or []:
                nm = " ".join(x for x in [a.get("given"), a.get("family")] if x)
                if nm:
                    authors.append(nm)
            pub = it.get("published") or {}
            year = (pub.get("date-parts") or [[None]])[0][0]
            cont = it.get("container-title") or []
            return {
                "title": (it.get("title") or [None])[0], "authors": authors, "year": year,
                "journal": cont[0] if cont else None, "doi": doi,
                "url": it.get("URL") or f"https://doi.org/{doi}",
                "abstract": _strip_tags(it.get("abstract")) or None, "cited_by": None,
                "origin": "🌍 Internasional", "extra": None,
                "volume": None, "issue": None, "pages": None,
                "openalex_id": None, "pdf_url": None, "is_oa": False, "doaj": False,
                "publisher": None,
            }
    except Exception as e:
        log.warning("fetch DOI Crossref gagal: %s", e)
    return None


def journal_info_openalex(name, per_page=5, timeout=25):
    """Info jurnal dari OpenAlex Sources."""
    try:
        r = _oa_get(f"{OPENALEX}/sources", params={
            "search": name, "per-page": per_page,
            "select": "id,display_name,type,issn_l,issn,host_organization_name,works_count,"
                      "cited_by_count,homepage_url,is_oa,is_in_doaj,apc_usd",
        }, timeout=timeout)
        r.raise_for_status()
        return r.json().get("results", [])
    except Exception as e:
        log.warning("journal info gagal: %s", e)
        return []


# ------------------------------------------------- fitur lanjutan
def _get_work_raw(openalex_id, timeout=25):
    """Ambil objek work mentah OpenAlex by ID (mis. 'W2741809807')."""
    ox = (openalex_id or "").strip().upper()
    if ox.startswith("HTTP"):
        ox = ox.rsplit("/", 1)[-1]
    if not ox or not ox.startswith("W"):
        return None
    try:
        r = _oa_get(f"{OPENALEX}/works/{ox}", timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("get work %s gagal: %s", ox, e)
        return None


def related_works(openalex_id, limit=6, timeout=25):
    """Artikel terkait dari OpenAlex (field related_works). Return list[dict]."""
    raw = _get_work_raw(openalex_id, timeout)
    if not raw:
        return []
    ids = []
    for u in raw.get("related_works") or []:
        wid = u.rsplit("/", 1)[-1]
        if wid and wid != (openalex_id or "").upper():
            ids.append(wid)
        if len(ids) >= limit:
            break
    if not ids:
        return []
    try:
        r = _oa_get(f"{OPENALEX}/works", params={
            "filter": "openalex:" + "|".join(ids),
            "per-page": len(ids),
            "select": "id,doi,title,authorships,publication_year,primary_location,"
                      "abstract_inverted_index,cited_by_count,biblio,open_access,"
                      "best_oa_location",
        }, timeout=timeout)
        r.raise_for_status()
        out = []
        for w in r.json().get("results", []):
            try:
                out.append(_norm_openalex(w))
            except Exception:
                pass
        return [w for w in out if w.get("title")]
    except Exception as e:
        log.warning("related works gagal: %s", e)
        return []


def cited_by_works(openalex_id, limit=6, timeout=25):
    """Artikel yang mengutip karya ini (filter cites:). Urut sitasi terbanyak."""
    ox = (openalex_id or "").strip().upper()
    if ox.startswith("HTTP"):
        ox = ox.rsplit("/", 1)[-1]
    if not ox or not ox.startswith("W"):
        return []
    try:
        r = _oa_get(f"{OPENALEX}/works", params={
            "filter": f"cites:{ox}",
            "sort": "cited_by_count:desc",
            "per-page": limit,
            "select": "id,doi,title,authorships,publication_year,primary_location,"
                      "abstract_inverted_index,cited_by_count,biblio,open_access,"
                      "best_oa_location",
        }, timeout=timeout)
        r.raise_for_status()
        out = []
        for w in r.json().get("results", []):
            try:
                out.append(_norm_openalex(w))
            except Exception:
                pass
        return [w for w in out if w.get("title")]
    except Exception as e:
        log.warning("cited-by gagal: %s", e)
        return []


def resolve_openalex_id(work, timeout=25):
    """Dapatkan OpenAlex ID dari sebuah work dict (doi -> fetch, judul -> search)."""
    if work.get("openalex_id"):
        return work["openalex_id"]
    if work.get("doi"):
        w = fetch_by_doi(work["doi"], timeout)
        if w and w.get("openalex_id"):
            return w["openalex_id"]
    title = (work.get("title") or "").strip()
    if title:
        res = search_openalex(title[:120], 3, timeout)
        for w in res:
            if w.get("openalex_id"):
                return w["openalex_id"]
    return None


def recommend_journals(query, limit=8, sample=150, timeout=30):
    """Rekomendasi jurnal untuk publikasi: agregasi jurnal dari artikel topik terkait.

    Rantai sumber: OpenAlex -> Semantic Scholar (fallback bila OpenAlex
    gagal/rate-limit/circuit breaker). Keduanya mengembalikan format
    unified yang sama ({journal, year, cited_by, is_oa}).

    Return list dict: {name, publisher, count, oa_pct, homepage, doaj}.
    """
    from datetime import date
    works = []
    try:
        works = search_openalex(query, sample, timeout, sort="cited_by_count:desc")
    except Exception as e:
        log.warning("rekomendasi: OpenAlex gagal (%s) -> fallback S2", e)
    if not works:
        try:
            from search_s2 import s2_search_papers
            works = s2_search_papers(query, per_page=min(sample, 100),
                                     timeout=timeout)
        except Exception as e:
            log.warning("rekomendasi: Semantic Scholar juga gagal: %s", e)
    # batasi 5 tahun terakhir agar relevan
    cutoff = date.today().year - 5
    agg = {}
    for w in works:
        if not w.get("journal"):
            continue
        if w.get("year") and w["year"] < cutoff:
            continue
        key = w["journal"].strip().lower()
        e = agg.setdefault(key, {"name": w["journal"], "publisher": None,
                                 "count": 0, "oa": 0, "homepage": None,
                                 "doaj": False, "cited": 0})
        e["count"] += 1
        e["cited"] += w.get("cited_by") or 0
        if w.get("is_oa"):
            e["oa"] += 1
    # lengkapi info jurnal via sources (konkuren, best-effort)
    from concurrent.futures import ThreadPoolExecutor
    ranked = sorted(agg.values(),
                    key=lambda e: (-e["count"], -e["cited"]))[:30]

    def _fill(e):
        try:
            srcs = journal_info_openalex(e["name"], 1, timeout=10)
            if srcs:
                s = srcs[0]
                if s.get("type") and s["type"] != "journal":
                    e["skip"] = True
                    return
                e["publisher"] = s.get("host_organization_name")
                e["homepage"] = s.get("homepage_url")
                e["doaj"] = bool(s.get("is_in_doaj"))
                e["apc_usd"] = s.get("apc_usd")
                e["source_id"] = s.get("id")
                e["issn_l"] = s.get("issn_l")
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(_fill, ranked))
    out = []
    for e in sorted(ranked, key=lambda e: (-e["count"], -e["cited"])):
        if e.get("skip"):
            continue
        if len(out) >= limit:
            break
        e["oa_pct"] = round(100 * e["oa"] / e["count"]) if e["count"] else 0
        out.append(e)
    return out


# domain yang dilewati saat mencari website publisher (direktori/agregator/sosmed)
_DDG_SKIP = ("companyhouse", "companieshouse", "idalamat", "linkedin.com",
             "facebook.com", "instagram.com", "wikipedia.org",
             "garuda.kemdikbud", "sinta.", "youtube.com", "twitter.com")
_DDG_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
_HOMEPAGE_CACHE_FILE = None  # di-set saat pertama dipakai


def _homepage_cache_file():
    global _HOMEPAGE_CACHE_FILE
    if _HOMEPAGE_CACHE_FILE is None:
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(d, exist_ok=True)
        _HOMEPAGE_CACHE_FILE = os.path.join(d, "homepages.json")
    return _HOMEPAGE_CACHE_FILE


def _load_homepage_cache():
    try:
        with open(_homepage_cache_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_homepage_cache(cache):
    try:
        with open(_homepage_cache_file(), "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
    except Exception as e:
        log.warning("gagal simpan cache homepage: %s", e)


def _ddg_homepage(query, timeout=12):
    """Ambil URL hasil pertama DuckDuckGo (best-effort, tanpa API key)."""
    try:
        r = httpx.get("https://html.duckduckgo.com/html/",
                      params={"q": query}, headers=_DDG_UA, timeout=timeout)
        r.raise_for_status()
        for href in re.findall(r'class="result__a"[^>]*href="([^"]+)"', r.text):
            if href.startswith("//"):
                href = "https:" + href
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            if "uddg" not in qs:
                continue
            url = qs["uddg"][0]
            dom = urllib.parse.urlparse(url).netloc.lower()
            if any(s in dom for s in _DDG_SKIP):
                continue
            return url
    except Exception as e:
        log.warning("ddg homepage gagal %s: %s", query, e)
    return None


def publisher_homepages(names, timeout=15, max_workers=2):
    """Cari website publisher.

    Urutan: cache persisten -> OpenAlex /publishers (resmi) -> DuckDuckGo
    (best-effort, 1x retry). Return {nama: homepage_url} untuk yang ketemu.
    """
    from concurrent.futures import ThreadPoolExecutor

    cache = _load_homepage_cache()
    names = [n for n in (names or []) if n]
    out = {n: cache[n] for n in names if cache.get(n)}
    todo = [n for n in names if n not in out]
    if not todo:
        return out

    def _one(name):
        # 1) database resmi OpenAlex (paling akurat)
        try:
            r = _oa_get(f"{OPENALEX}/publishers",
                          params={"search": name, "per-page": 3}, timeout=timeout)
            r.raise_for_status()
            key = re.sub(r"\s+", " ", (name or "").lower()).strip()
            for p in r.json().get("results", []) or []:
                dn = re.sub(r"\s+", " ", (p.get("display_name") or "").lower()).strip()
                if not key or not dn:
                    continue
                if key == dn or (len(key) >= 6 and (key in dn or dn in key)):
                    if p.get("homepage_url"):
                        return p["homepage_url"]
        except Exception as e:
            log.warning("publisher homepage gagal %s: %s", name, e)
        # 2) fallback pencarian web (untuk publisher Indonesia), 1x retry
        url = _ddg_homepage(name)
        if not url:
            time.sleep(2)
            url = _ddg_homepage(name)
        return url

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for n, url in zip(todo, ex.map(_one, todo)):
            if url:
                out[n] = url
                cache[n] = url
    if len(cache) > 2000:  # batasi ukuran cache
        cache = dict(list(cache.items())[-2000:])
    _save_homepage_cache(cache)
    return out


def _search_authors_openalex(name, per_page=5, timeout=10):
    """Cari penulis via OpenAlex Authors.

    Return list dict: {name, h_index, i10_index, works_count, cited_by_count,
    affiliations, topics, url} — atau None bila request gagal total
    (timeout/limit), agar pemanggil bisa bedakan "gagal" vs "tidak ketemu".
    (_oa_get sudah menangani retry 429 + Retry-After.)
    """
    try:
        r = _oa_get(f"{OPENALEX}/authors", params={
            "search": name, "per-page": per_page,
            "select": "id,display_name,works_count,cited_by_count,summary_stats,"
                      "last_known_institutions,topics",
        }, timeout=timeout)
        out = []
        for a in r.json().get("results", []) or []:
            ss = a.get("summary_stats") or {}
            out.append({
                "name": a.get("display_name"),
                "h_index": ss.get("h_index"),
                "i10_index": ss.get("i10_index"),
                "works_count": a.get("works_count"),
                "cited_by_count": a.get("cited_by_count"),
                "affiliations": [x.get("display_name")
                                 for x in (a.get("last_known_institutions") or [])][:3],
                "topics": [t.get("display_name")
                           for t in (a.get("topics") or [])][:3],
                "url": a.get("id"),
            })
        return out
    except Exception as e:
        log.warning("search authors gagal %s: %s", name, e)
        return None



def search_authors(name, per_page=5, timeout=10):
    """Cari penulis: OpenAlex dulu, fallback ke Semantic Scholar.

    Return list (bisa []) atau None bila SEMUA sumber gagal total.
    """
    res = _search_authors_openalex(name, per_page, timeout)
    if res is None:
        log.warning("OpenAlex authors gagal utk %r — fallback Semantic Scholar", name)
        res = s2_search_authors(name, per_page, timeout)
    return res

def source_new_works(source_id, since_date, per_page=5, timeout=25):
    """Artikel baru sebuah jurnal (OpenAlex source) sejak tanggal YYYY-MM-DD.

    Return list dict: {title, url, date} — atau None bila request gagal
    (agar pemanggil tidak memajukan watermark last_check sia-sia).
    """
    try:
        sid = (source_id or "").rsplit("/", 1)[-1]
        r = _oa_get(f"{OPENALEX}/works", params={
            "filter": f"primary_location.source.id:{sid},from_publication_date:{since_date}",
            "sort": "publication_date:desc",
            "per-page": per_page,
            "select": "id,display_name,publication_date,doi",
        }, timeout=timeout)
        r.raise_for_status()
        out = []
        for w in r.json().get("results", []) or []:
            url = w.get("doi") and f"https://doi.org/{w['doi']}" or w.get("id")
            out.append({"title": w.get("display_name"),
                        "url": url,
                        "date": w.get("publication_date")})
        return out
    except Exception as e:
        log.warning("source new works gagal %s: %s", source_id, e)
        return None
