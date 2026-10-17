"""Agregasi publisher per bidang + peringkat Sinta jurnal-jurnalnya.

Dipakai fitur /publisher: bidang -> daftar publisher -> jurnal per publisher
-> filter indeks (Sinta 1-6 / DOAJ / OA).
"""
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from search_id import sinta_profile_for, sinta_rank_for

log = logging.getLogger(__name__)


def _norm(name):
    n = re.sub(r"\s+", " ", (name or "").strip())
    return n, n.lower()


def aggregate_publishers(works, top_n=8):
    """Kelompokkan works berdasar publisher.

    Return [{"name", "count", "journals": [{"name","count","oa","doaj"}]}]
    journals terurut jumlah artikel desc.
    """
    pubs = {}
    for w in (works or []):
        if not isinstance(w, dict):
            continue
        pname, pkey = _norm(w.get("publisher"))
        if not pkey or len(pkey) < 3:
            continue
        p = pubs.setdefault(pkey, {"name": pname, "count": 0, "journals": {}})
        p["count"] += 1
        jn, jk = _norm(w.get("journal"))
        if jk:
            j = p["journals"].setdefault(
                jk, {"name": jn, "count": 0, "oa": 0, "doaj": False})
            j["count"] += 1
            if w.get("is_oa"):
                j["oa"] += 1
            if w.get("doaj"):
                j["doaj"] = True
    out = []
    for p in pubs.values():
        journals = sorted(p["journals"].values(), key=lambda j: -j["count"])
        out.append({"name": p["name"], "count": p["count"], "journals": journals})
    return sorted(out, key=lambda p: -p["count"])[:top_n]


def enrich_journal_ranks(journals, max_workers=4, timeout=15):
    """Isi 'sinta_rank' tiap jurnal (konkuren, best-effort, hasil di-cache)."""
    targets = [j for j in (journals or [])
               if j.get("name") and not j.get("sinta_rank")]
    if not targets:
        return journals
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            ranks = list(ex.map(lambda j: sinta_rank_for(j["name"], timeout),
                                targets))
        for j, r in zip(targets, ranks):
            if r:
                j["sinta_rank"] = r
    except Exception as e:
        log.warning("enrich rank jurnal gagal: %s", e)
    return journals


def enrich_journal_profiles(journals, max_workers=4, timeout=15):
    """Isi 'sinta_rank' + 'sinta_url' tiap jurnal (profil Sinta lengkap)."""
    targets = [j for j in (journals or [])
               if j.get("name") and not j.get("sinta_rank")]
    if not targets:
        return journals
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            profs = list(ex.map(lambda j: sinta_profile_for(j["name"], timeout),
                                targets))
        for j, p in zip(targets, profs):
            if p:
                j["sinta_rank"] = p["rank"]
                if p.get("url"):
                    j["sinta_url"] = p["url"]
    except Exception as e:
        log.warning("enrich profil jurnal gagal: %s", e)
    return journals
