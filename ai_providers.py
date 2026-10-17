"""Rotasi provider AI: Cerebras (berbayar) -> Gemini -> Groq -> OpenRouter.

Tujuan: menumpuk free tier beberapa provider agar bot tahan banyak request
tanpa keluar biaya. Semua provider cadangan memakai API OpenAI-compatible
(/chat/completions); hanya Gemini yang memakai API-nya sendiri.

PREMIUM: user premium memakai model berbayar via OpenRouter
(Claude Haiku untuk menulis, Perplexity Sonar untuk riset), dengan fallback
otomatis ke rotasi gratis bila gagal. Model bisa dioverride via env:
OPENROUTER_MODEL_PREMIUM (default anthropic/claude-haiku-4.5) dan
OPENROUTER_MODEL_PREMIUM_SEARCH (default perplexity/sonar).

Key yang kosong di .env = provider dilewati otomatis. Model default bisa
dioverride via env (GROQ_MODEL, CEREBRAS_MODEL, OPENROUTER_MODEL) karena
daftar model gratis berubah tiap beberapa bulan; model 404 otomatis dilewati.
"""
import asyncio
import logging
import os
import time

log = logging.getLogger("ai_providers")


class _KeyRateLimited(Exception):
    """Internal: key provider kena 429 di semua attempt.

    Rate limit itu key-level, bukan model-level: tidak ada gunanya mencoba
    model lain dari provider yang sama.
    """


def _endpoints():
    """Daftar (nama, base_url, cfg) berurutan. cfg None = Gemini (custom)."""
    eps = []

    def add(name, base, key, models):
        key = (key or "").strip()
        if not key:
            return
        for m in models:
            if m:
                eps.append((name, base, {"key": key, "model": m.strip()}))

    # STRATEGI1: Cerebras BERBAYAR (kredit $250) paling depan; model pertama
    # gpt-oss-120b yang terbukti tersedia untuk key ini (llama-3.3-70b &
    # qwen-3-32b balas 404 di log 2026-10-14). Cadangan gratis berlapis.
    add("cerebras", "https://api.cerebras.ai/v1",
        os.environ.get("CEREBRAS_API_KEY"),
        [os.environ.get("CEREBRAS_MODEL") or "gpt-oss-120b",
         "llama-3.3-70b", "qwen-3-32b"])
    if os.environ.get("GEMINI_API_KEY", "").strip():
        eps.append(("gemini", None, None))
    add("groq", "https://api.groq.com/openai/v1",
        os.environ.get("GROQ_API_KEY"),
        [os.environ.get("GROQ_MODEL") or "llama-3.3-70b-versatile",
         "llama-3.1-8b-instant"])
    add("openrouter", "https://openrouter.ai/api/v1",
        os.environ.get("OPENROUTER_API_KEY"),
        [os.environ.get("OPENROUTER_MODEL") or
         "meta-llama/llama-3.3-70b-instruct:free"])
    return eps


def _premium_endpoints(search):
    """Endpoint premium via OpenRouter: Haiku (nulis) / Sonar (riset).

    search=True -> Sonar dulu (jawaban berbasis web), lalu Haiku.
    search=False -> Haiku dulu (tulisan akademik), lalu Sonar.
    Key kosong = tidak ada endpoint premium (fallback ke gratis).
    """
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not key:
        return []
    haiku = ((os.environ.get("OPENROUTER_MODEL_PREMIUM") or
              "anthropic/claude-haiku-4.5").strip())
    sonar = ((os.environ.get("OPENROUTER_MODEL_PREMIUM_SEARCH") or
              "perplexity/sonar").strip())
    models = [sonar, haiku] if search else [haiku, sonar]
    return [("openrouter-premium", "https://openrouter.ai/api/v1",
             {"key": key, "model": m}) for m in models if m]


async def _openai_chat(base, key, model, prompt):
    """Satu panggilan chat/completions. Return str | None.

    None = gagal sementara (coba lagi / pindah endpoint).
    Raise RuntimeError("skip") = endpoint mati (404) -> langsung endpoint berikut.
    """
    import httpx
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=30) as cli:
                r = await cli.post(
                    base + "/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"model": model,
                          "messages": [{"role": "user", "content": prompt}]})
            if r.status_code == 404:
                raise RuntimeError("skip")
            if r.status_code == 429:
                log.info("AI %s 429, ulangi (%d/2)", model, attempt)
                if attempt < 2:
                    await asyncio.sleep(2 * attempt)
                    continue
                raise _KeyRateLimited(model)
            if r.status_code in (500, 502, 503):
                log.info("AI %s %s, ulangi (%d/2)", model, r.status_code, attempt)
                if attempt < 2:
                    await asyncio.sleep(2 * attempt)
                    continue
                return None
            r.raise_for_status()
            data = r.json()
            text = (((data.get("choices") or [{}])[0].get("message") or {})
                    .get("content") or "").strip()
            return text or None
        except RuntimeError:
            raise
        except Exception as e:
            log.info("AI %s gagal: %s", model, e)
            return None
    return None


async def generate(prompt, premium=False, search=False, budget=75.0):
    """Generate teks via provider pertama yang berhasil. Return str | None.

    premium=True -> coba model berbayar (Haiku/Sonar) dulu, fallback ke
    rotasi gratis bila gagal. premium=False -> langsung rotasi gratis.

    budget: batas total detik untuk seluruh rotasi (default 75). Tanpa ini,
    request bisa menggantung bermenit-menit saat semua provider bermasalah
    dan menahan worker Flask. Provider yang key-nya kena 429 dilewati
    model-model berikutnya (rate limit = key-level).
    """
    t0 = time.monotonic()

    def sisa():
        return max(0.5, budget - (time.monotonic() - t0))

    if premium:
        for name, base, cfg in _premium_endpoints(search):
            try:
                out = await asyncio.wait_for(
                    _openai_chat(base, cfg["key"], cfg["model"], prompt),
                    timeout=sisa())
            except (RuntimeError, _KeyRateLimited):
                log.info("AI premium %s tidak ada/terbatas, lewati", cfg["model"])
                continue
            except Exception as e:
                log.info("AI premium %s gagal: %s", cfg["model"], e)
                continue
            if out:
                log.info("AI premium pakai %s", cfg["model"])
                return out
        log.info("AI premium gagal semua, fallback ke rotasi gratis")
    eps = _endpoints()
    if not eps:
        log.warning("AI: tidak ada provider terkonfigurasi (isi API key di .env)")
        return None
    async def _coba_rotasi():
        skip_provider = set()
        for name, base, cfg in eps:
            if name in skip_provider:
                continue
            if name == "gemini":
                try:
                    import tanya as tanya_mod
                    out = await asyncio.wait_for(
                        tanya_mod.gemini_generate(prompt), timeout=sisa())
                except Exception as e:
                    log.info("AI gemini gagal: %s", e)
                    continue
                if out:
                    return out
                continue
            try:
                out = await asyncio.wait_for(
                    _openai_chat(base, cfg["key"], cfg["model"], prompt),
                    timeout=sisa())
            except _KeyRateLimited:
                log.info("AI %s key kena 429, lewati model lain provider ini", name)
                skip_provider.add(name)
                continue
            except RuntimeError:
                log.info("AI %s model %s tidak ada (404), lewati", name, cfg["model"])
                continue
            except asyncio.TimeoutError:
                log.info("AI %s/%s kehabisan budget, hentikan rotasi",
                         name, cfg["model"])
                break
            except Exception as e:
                log.info("AI %s gagal: %s", name, e)
                continue
            if out:
                log.info("AI pakai %s/%s", name, cfg["model"])
                return out
        return None

    out = await _coba_rotasi()
    if out:
        return out
    # Semua provider gagal. Bila kegagalannya CEPAT (pola khas 429
    # beruntun di free tier), jeda 3 detik lalu coba rotasi sekali lagi —
    # limit provider gratis sering pulih dalam hitungan detik (kejadian
    # 2026-10-14: Tanya AI & AI Studio 502 massal ~5 menit lalu pulih).
    # Bila gagalnya karena timeout (elapsed mendekati budget), jangan
    # ulangi: percuma dan request bisa menggantung.
    if time.monotonic() - t0 < 35:
        log.info("AI: rotasi pertama gagal cepat, jeda 3 dtk lalu coba lagi")
        await asyncio.sleep(3)
        out = await _coba_rotasi()
        if out:
            return out
    log.warning("AI: semua provider gagal")
    return None
