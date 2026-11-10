"""Klien API Exa untuk fitur Cari Jurnal Mendalam (KhairyPustaka).

Jalur yang dipakai adalah jalur yang TERBUKTI di Fase 0 (2026-11-10,
lihat your_files/analisis-exa-premium/laporan-fase0-exa.md):

- Cari Jurnal    : POST /search, type neural, category research-paper,
                   teks sampai 4.000 karakter per hasil. Biaya $0,004.
- Jawaban Cepat  : POST /answer dengan teks sumber. Biaya $0,005.
- Bedah Mendalam : POST /search, type deep + outputSchema terstruktur
                   (ringkasan, faktor_penyebab, dampak, rekomendasi)
                   plus grounding sitasi per bidang. Biaya $0,012.
- Laporan Riset  : POST /agent/runs effort high, lalu polling
                   GET /agent/runs/{id} sampai completed/failed.
                   Biaya $0,50 flat, sekitar 3 menit.

API lama /research/v1/tasks SUDAH PENSIUN (HTTP 410) — jangan dipakai.
Jalur /agent lama juga 404; yang benar /agent/runs.

Kredensial:
- Produksi: environment EXA_API_KEY (header x-api-key ke api.exa.ai).
- Lingkungan Muse lokal (uji): bila EXA_API_KEY kosong, klien memakai
  pola konektor custom.exa yang sama dengan helper resminya, yaitu
  surrogate dynamic_credentials yang ditempel ke request; nilai kunci
  asli tidak pernah muncul di kode, berkas, atau log modul ini.
Redirect pada request terautentikasi selalu ditolak.
"""
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request

log = logging.getLogger("kpweb.exa")

BASE = "https://api.exa.ai"
_HOST = "api.exa.ai"


class ExaError(Exception):
    """Galat panggilan Exa. `status` = kode HTTP bila ada."""

    def __init__(self, pesan, status=None):
        super().__init__(pesan)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a):
        raise ExaError("redirect ditolak pada request Exa terautentikasi")


def _siapkan_kredensial(req):
    """Tempel kredensial ke request: env EXA_API_KEY dulu, lalu
    surrogate konektor custom.exa (pola helper resmi) untuk uji lokal."""
    key = (os.environ.get("EXA_API_KEY") or "").strip()
    if key:
        req.add_header("x-api-key", key)
        return
    try:
        p = "/opt/muse/skills/custom-connector-setup/bin"
        if p not in sys.path:
            sys.path.insert(0, p)
        from dynamic_credentials import add_surrogate_to_request
        add_surrogate_to_request(req, "custom.exa", allowed_hosts=[_HOST])
        return
    except Exception as e:
        raise ExaError(
            "kredensial Exa belum tersedia: pasang EXA_API_KEY di "
            "environment server, atau jalankan di lingkungan Muse "
            f"dengan konektor custom.exa aktif ({e})")


def _call(method, path, payload=None, timeout=120):
    url = BASE + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    _siapkan_kredensial(req)
    try:
        with urllib.request.build_opener(_NoRedirect()).open(
                req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")[:2000]
        try:
            body = json.loads(raw)
        except Exception:
            body = {"raw": raw}
        tag = ""
        if isinstance(body, dict):
            tag = str((body.get("error") or {}).get("tag")
                      if isinstance(body.get("error"), dict)
                      else body.get("error") or "")
        raise ExaError(f"Exa HTTP {e.code} {tag}".strip(), status=e.code)
    except ExaError:
        raise
    except Exception as e:
        raise ExaError(f"Exa tidak terjangkau: {type(e).__name__}")
    try:
        return json.loads(raw)
    except Exception:
        raise ExaError("respons Exa bukan JSON")


def biaya(body):
    """Normalisasi costDollars: bisa angka atau dict {total: ...}."""
    c = (body or {}).get("costDollars")
    if isinstance(c, dict):
        try:
            return float(c.get("total") or 0)
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(c or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- mode 1
def cari_jurnal(query, num=10, kategori="research-paper",
                start_year=None, end_year=None, domains=None,
                isi_penuh=True):
    """Pencarian neural + teks 4.000 karakter per hasil.

    kategori None = semua sumber (tanpa filter kategori Exa). Bila
    isi_penuh False, yang diminta hanya highlights (label akses hasil
    akan turun sendiri karena teks tidak terambil).
    """
    payload = {"query": query, "numResults": int(num), "type": "neural"}
    if kategori:
        payload["category"] = kategori
    if isi_penuh:
        payload["contents"] = {"text": {"maxCharacters": 4000}}
    else:
        payload["contents"] = {"highlights": True}
    if start_year:
        payload["startPublishedDate"] = f"{int(start_year)}-01-01T00:00:00.000Z"
    if end_year:
        payload["endPublishedDate"] = f"{int(end_year)}-12-31T23:59:59.999Z"
    if domains:
        payload["includeDomains"] = list(domains)
    return _call("POST", "/search", payload, timeout=120)


def cari_serupa(url, num=10):
    """Find Similar: paper serupa dari satu link/DOI (titik mulai DOI)."""
    payload = {"url": url, "numResults": int(num),
               "contents": {"text": {"maxCharacters": 4000}}}
    return _call("POST", "/findSimilar", payload, timeout=120)


# ---------------------------------------------------------------- mode 2
def jawaban_cepat(query):
    """Jawaban langsung bersitasi dari POST /answer."""
    return _call("POST", "/answer", {"query": query, "text": True},
                 timeout=120)


# ---------------------------------------------------------------- mode 3
SKEMA_BEDAH = {
    "type": "object",
    "required": ["ringkasan", "faktor_penyebab", "dampak", "rekomendasi"],
    "properties": {
        "ringkasan": {"type": "string"},
        "faktor_penyebab": {"type": "array", "items": {"type": "string"}},
        "dampak": {"type": "array", "items": {"type": "string"}},
        "rekomendasi": {"type": "array", "items": {"type": "string"}},
    },
}


def bedah_mendalam(query, num=10):
    """Deep search + outputSchema terstruktur + grounding per bidang."""
    payload = {"query": query, "type": "deep", "numResults": int(num),
               "outputSchema": SKEMA_BEDAH,
               "contents": {"text": {"maxCharacters": 3000}}}
    return _call("POST", "/search", payload, timeout=180)


# ---------------------------------------------------------------- mode 4
SISTEM_LAPORAN = (
    "Kamu asisten riset akademik. Susun laporan riset terstruktur "
    "berbahasa Indonesia berdasarkan sumber penelitian asli, dengan "
    "sitasi inline berupa tautan markdown ke sumber (DOI atau jurnal) "
    "untuk setiap klaim penting. Bedakan hubungan statistik dari sebab "
    "akibat, dan jangan menggeneralisasi angka lokal menjadi angka "
    "nasional.")


def laporan_buat(query):
    """Buat agent run effort high. Return body (berisi id + status)."""
    payload = {"query": query, "effort": "high",
               "systemPrompt": SISTEM_LAPORAN}
    return _call("POST", "/agent/runs", payload, timeout=120)


def laporan_status(run_id):
    return _call("GET", f"/agent/runs/{run_id}", None, timeout=60)


def laporan_tunggu(run_id, batas_detik=420, jeda=8.0, on_step=None):
    """Polling sampai completed/failed/cancelled atau batas waktu.

    Return body final. Melempar ExaError bila timeout lokal atau
    status gagal. Dipakai worker job di app.py, bukan request web.
    """
    t0 = time.time()
    while True:
        body = laporan_status(run_id)
        st = str(body.get("status") or "")
        if on_step:
            try:
                on_step(st, int(time.time() - t0))
            except Exception:
                pass
        if st == "completed":
            return body
        if st in ("failed", "cancelled", "canceled"):
            err = body.get("error")
            raise ExaError(f"agent run {st}: {str(err)[:200]}")
        if time.time() - t0 > batas_detik:
            raise ExaError("agent run melewati batas waktu tunggu lokal")
        time.sleep(jeda)
