"""Pencarian jurnal Indonesia via Sinta & Garuda (kemdiktisaintek.go.id).

CATATAN: kedua portal tidak punya API publik -> dipakai HTTP scraping
terhadap HTML server-rendered (tanpa JS, tanpa CAPTCHA per riset 2026-10-05).
- Garuda  : pencarian ARTIKEL  -> /documents?q={keyword}
- Sinta   : direktori JURNAL   -> /journals?q={keyword}  (tidak ada pencarian artikel)

Seluruh fungsi aman dipanggil: gagal -> ([], catatan), tidak pernah raise.
"""
import logging
import re
import time
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 JurnalBot/1.0"}
SINTA = "https://sinta.kemdiktisaintek.go.id"
GARUDA = "https://garuda.kemdiktisaintek.go.id"


def _get(url, timeout=10):
    """GET dengan satu kali retry bila kena 403 transient."""
    try:
        r = httpx.get(url, headers=UA, timeout=timeout, follow_redirects=True)
        if r.status_code == 403:
            time.sleep(2)
            r = httpx.get(url, headers=UA, timeout=timeout, follow_redirects=True)
        r.raise_for_status()
        return r.text
    except Exception as e:
        log.warning("GET gagal %s: %s", url, e)
        raise


def _clean_doi(href):
    if not href:
        return None
    m = re.search(r"10\.\d{4,}/[^\s\"'<>]+", href)
    return m.group(0).rstrip(").,;") if m else None


def _mk(title, authors, year, journal, url, abstract=None, doi=None, publisher=None):
    return {
        "title": title, "authors": authors or [], "year": year,
        "journal": journal, "doi": doi, "url": url,
        "abstract": abstract, "cited_by": None,
        "origin": "🇮🇩 Indonesia",
        "extra": f"🏢 {publisher}" if publisher else None,
        "publisher": publisher,
        "volume": None, "issue": None, "pages": None,
    }


def parse_garuda(html, limit=6):
    """Parse HTML hasil pencarian Garuda -> list dict unified."""
    soup = BeautifulSoup(html, "lxml")
    out = []
    for card in soup.select("div.article-item")[:limit]:
        try:
            t = card.select_one("a.title-article")
            if not t:
                continue
            title = t.get_text(" ", strip=True)
            href = t.get("href") or ""
            url = GARUDA + href if href.startswith("/") else href
            authors = [x.get_text(" ", strip=True)
                       for x in card.select("a.author-article")]
            # info jurnal: xmp berisi pola "... Vol X No Y (YYYY)"
            journal, year = None, None
            for x in card.find_all("xmp"):
                info = x.get_text(" ", strip=True)
                if re.search(r"\bVol\.?\s+\d+", info, re.I):
                    mj = re.search(r"^(.*?)\s+Vol\.?\s+\d+", info, re.I)
                    if mj:
                        journal = mj.group(1).strip()
                    my = re.search(r"\((19|20)\d{2}\)", info)
                    if my:
                        year = int(my.group(0)[1:-1])
                    break
            # publisher: <i>Publisher :</i> diikuti <xmp>
            publisher = None
            pi = card.find("i", string=re.compile(r"Publisher", re.I))
            if pi:
                nx = pi.find_next_sibling("xmp")
                if nx:
                    publisher = nx.get_text(" ", strip=True)
            abs_el = card.select_one("div.abstract-article")
            abstract = abs_el.get_text(" ", strip=True) if abs_el else None
            doi_a = card.select_one('a.title-citation[href*="doi.org"]')
            doi = _clean_doi(doi_a.get("href")) if doi_a else None
            out.append(_mk(title, authors, year, journal, url or None,
                           abstract, doi, publisher))
        except Exception as e:
            log.warning("parse kartu Garuda gagal: %s", e)
    return out


def search_garuda(query, limit=6, timeout=10):
    """Cari artikel di Garuda. Return (list_hasil, catatan_atau_None)."""
    try:
        html = _get(f"{GARUDA}/documents?q={quote_plus(query)}", timeout)
    except Exception:
        return [], "Garuda tidak bisa dijangkau saat ini"
    out = parse_garuda(html, limit)
    if not out:
        return [], "Garuda tidak menampilkan hasil (coba kata kunci lain)"
    return out, None


def parse_sinta_journals(html, limit=5):
    """Parse HTML direktori jurnal Sinta -> list dict info jurnal."""
    soup = BeautifulSoup(html, "lxml")
    out = []
    for a in soup.select('a[href*="/journals/profile/"]')[:limit]:
        try:
            title = a.get_text(" ", strip=True)
            href = a.get("href") or ""
            url = href if href.startswith("http") else SINTA + href
            card = None
            for p in a.parents:
                if p.name and p.select_one('a[href*="/affiliations/profile/"]'):
                    card = p
                    break
            publisher, rank, issn = None, None, None
            if card:
                pub = card.select_one('a[href*="/affiliations/profile/"]')
                if pub:
                    publisher = pub.get_text(" ", strip=True)
                txt = card.get_text(" ", strip=True)
                mr = re.search(r"\bS[1-6]\b", txt)
                if mr:
                    rank = f"Sinta {mr.group(0)[1:]}"
                mi = re.search(r"P-ISSN\s*:\s*(\S+)[\s\S]*?E-ISSN\s*:\s*(\S+)", txt)
                if mi:
                    issn = f"{mi.group(1)} / {mi.group(2)}"
            out.append({"name": title, "rank": rank, "issn": issn,
                        "publisher": publisher, "url": url})
        except Exception as e:
            log.warning("parse kartu Sinta gagal: %s", e)
    return out


def journal_info_sinta(name, limit=5, timeout=10):
    """Info jurnal terakreditasi dari direktori Sinta. Return (list, catatan)."""
    try:
        html = _get(f"{SINTA}/journals?q={quote_plus(name)}", timeout)
    except Exception:
        return [], "Sinta tidak bisa dijangkau saat ini"
    out = parse_sinta_journals(html, limit)
    if not out:
        return [], "tidak ada jurnal cocok di Sinta"
    return out, None


def search_indonesia(query, limit=6):
    """Artikel Indonesia via Garuda. Return (list_hasil, list_catatan)."""
    hasil, catatan = [], []
    res, note = search_garuda(query, limit)
    hasil.extend(res)
    if note:
        catatan.append(note)
    return hasil[:limit], catatan


# ------------------------------------------------- peringkat Sinta
_RANK_CACHE = {}


def _norm_title(t):
    t = re.sub(r"[^a-z0-9 ]", " ", (t or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def _match_journal(key, jn):
    """Cocokkan nama jurnal: persis selalu OK; substring hanya utk nama panjang."""
    if not key or not jn:
        return False
    if key == jn:
        return True
    if len(key) < 6:
        return False  # nama pendek (mis. "BMJ") rawan salah cocok
    return key in jn or jn in key


def sinta_profile_for(journal_name, timeout=6):
    """Cari profil Sinta sebuah nama jurnal.

    Return {'rank','url','name'} atau None. Hasil di-cache di memori.
    """
    key = _norm_title(journal_name)
    if not key:
        return None
    if key in _RANK_CACHE:
        return _RANK_CACHE[key]
    prof = None
    try:
        journals, _ = journal_info_sinta(journal_name, limit=3, timeout=timeout)
        for j in journals:
            jn = _norm_title(j.get("name"))
            if _match_journal(key, jn) and j.get("rank"):
                prof = {"rank": j["rank"], "url": j.get("url"),
                        "name": j.get("name")}
                break
    except Exception as e:
        log.warning("sinta profile gagal utk %s: %s", journal_name, e)
    _RANK_CACHE[key] = prof
    return prof


def sinta_rank_for(journal_name, timeout=6):
    """Cari peringkat Sinta sebuah nama jurnal. Return 'Sinta 2' dsb / None."""
    p = sinta_profile_for(journal_name, timeout)
    return p["rank"] if p else None


def enrich_sinta_ranks(works, timeout=6, max_workers=4):
    """Isi w['sinta_rank'] untuk tiap hasil Indonesia (konkuren, best-effort).

    Dibatasi deadline keseluruhan (timeout+2 dtk) agar tidak menahan
    respons bot bila situs Sinta lambat.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    targets = [w for w in (works or [])
               if w.get("journal") and not w.get("sinta_rank")]
    if not targets:
        return works
    ex = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futs = {ex.submit(sinta_rank_for, w["journal"], timeout): w
                for w in targets}
        deadline = time.time() + timeout + 2
        for fut in as_completed(futs, timeout=max(deadline - time.time(), 1)):
            try:
                r = fut.result(timeout=0)
            except Exception:
                continue
            if r:
                futs[fut]["sinta_rank"] = r
    except Exception as e:
        log.warning("enrich sinta gagal: %s", e)
    finally:
        # jangan tunggu yang belum selesai — hasilnya best-effort
        ex.shutdown(wait=False, cancel_futures=True)
    return works
