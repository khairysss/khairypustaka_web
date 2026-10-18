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

    # STRATEGI1: Cerebras BERBAYAR (kredit $250) paling depan. Daftar model
    # diperbarui dari TES LIVE 2026-10-19: key Cerebras hanya bisa
    # gpt-oss-120b + qwen-3.8-27b (llama-3.3-70b & qwen-3-32b = 404);
    # katalog Groq sekarang openai/gpt-oss-120b + qwen/qwen3.8-27b
    # (llama-3.3-70b-versatile & llama-3.1-8b-instant sudah pensiun);
    # varian :free OpenRouter dihentikan, jadi OpenRouter keluar dari
    # rotasi reguler (hanya jalur premium + pilihan Llama eksplisit),
    # kecuali OPENROUTER_MODEL diisi manual via env.
    add("cerebras", "https://api.cerebras.ai/v1",
        os.environ.get("CEREBRAS_API_KEY"),
        [os.environ.get("CEREBRAS_MODEL") or "gpt-oss-120b",
         "qwen-3.8-27b"])
    if os.environ.get("GEMINI_API_KEY", "").strip():
        eps.append(("gemini", None, None))
    add("groq", "https://api.groq.com/openai/v1",
        os.environ.get("GROQ_API_KEY"),
        [os.environ.get("GROQ_MODEL") or "openai/gpt-oss-120b",
         "qwen/qwen3.8-27b"])
    if (os.environ.get("OPENROUTER_MODEL") or "").strip():
        add("openrouter", "https://openrouter.ai/api/v1",
            os.environ.get("OPENROUTER_API_KEY"),
            [os.environ.get("OPENROUTER_MODEL")])
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


async def _openai_chat(base, key, model, prompt, max_tokens=2000):
    """Satu panggilan chat/completions. Return str | None.

    max_tokens membatasi panjang output di sisi provider (ekonomi AI,
    T-2 audit 2026-10-17): tanpa ini model bisa mengembalikan puluhan ribu
    token dan membakar kredit berbayar per request.
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
                    json={"model": model, "max_tokens": int(max_tokens),
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


async def generate(prompt, premium=False, search=False, budget=40.0,
                   max_tokens=2000):
    """Generate teks via provider pertama yang berhasil. Return str | None.

    premium=True -> coba model berbayar (Haiku/Sonar) dulu, fallback ke
    rotasi gratis bila gagal. premium=False -> langsung rotasi gratis.

    budget: batas total detik untuk seluruh rotasi (default 40; dulunya 75 —
    diturunkan T-2 audit 2026-10-17 supaya request gagal tidak menahan
    thread waitress bermenit-menit). Provider yang key-nya kena 429 dilewati
    model-model berikutnya (rate limit = key-level).
    max_tokens: diteruskan ke semua provider (lihat _openai_chat dan
    gemini_generate) untuk membatasi biaya output per panggilan.
    """
    t0 = time.monotonic()

    def sisa():
        return max(0.5, budget - (time.monotonic() - t0))

    if premium:
        for name, base, cfg in _premium_endpoints(search):
            try:
                out = await asyncio.wait_for(
                    _openai_chat(base, cfg["key"], cfg["model"], prompt,
                                 max_tokens=max_tokens),
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
                        tanya_mod.gemini_generate(
                            prompt, max_output_tokens=max_tokens),
                        timeout=sisa())
                except Exception as e:
                    log.info("AI gemini gagal: %s", e)
                    continue
                if out:
                    return out
                continue
            try:
                out = await asyncio.wait_for(
                    _openai_chat(base, cfg["key"], cfg["model"], prompt,
                                 max_tokens=max_tokens),
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
    # Semua provider gagal. Ulangi rotasi SEKALI saja dan hanya bila
    # kegagalannya SANGAT cepat (pola khas 429 beruntun di free tier yang
    # pulih dalam hitungan detik). Batas diperketat T-2 audit 2026-10-17:
    # rotasi kedua menggandakan biaya & waktu request, jadi hanya untuk
    # kegagalan di bawah 12 detik pertama.
    if time.monotonic() - t0 < 12:
        log.info("AI: rotasi pertama gagal cepat, jeda 2 dtk lalu coba lagi")
        await asyncio.sleep(2)
        out = await _coba_rotasi()
        if out:
            return out
    log.warning("AI: semua provider gagal")
    return None


# ================================================================ PEMILIH MODEL
# Fitur pemilih model (keputusan Sul 2026-10-19): pengguna memilih model di
# AI Studio / Chat Tanya / Ide Topik. Kunci pilihan dari frontend SELALU
# divalidasi app.py terhadap daftar putih; fungsi di sini hanya menerima
# kunci yang sudah sah: auto, gptoss, qwen, gemini, llama, claude, sonar.

MODEL_LABELS = {
    "gpt-oss-120b": "GPT-OSS 120B",
    "openai/gpt-oss-120b": "GPT-OSS 120B",
    "qwen-3.8-27b": "Qwen 3.8 27B",
    "qwen/qwen3.8-27b": "Qwen 3.8 27B",
    "meta-llama/llama-3.3-70b-instruct": "Llama 3.3 70B",
    "anthropic/claude-haiku-4.5": "Claude Haiku 4.5",
    "perplexity/sonar": "Perplexity Sonar",
}

PILIHAN_LABEL = {
    "auto": "Otomatis",
    "gptoss": "GPT-OSS 120B",
    "qwen": "Qwen 3.8 27B",
    "gemini": "Gemini",
    "llama": "Llama 3.3 70B",
    "claude": "Claude Haiku 4.5",
    "sonar": "Perplexity Sonar",
}

PILIHAN_PREMIUM = {"claude", "sonar"}

# Cap output jalur premium (ekonomi: biaya terburuk per pakai terkunci;
# harga jual Rp200/pakai, lihat rancangan Sul 2026-10-19).
PREMIUM_MAKS_TOKEN = 1500

_MODEL_PREMIUM_ID = {
    "claude": (os.environ.get("OPENROUTER_MODEL_PREMIUM") or
               "anthropic/claude-haiku-4.5"),
    "sonar": (os.environ.get("OPENROUTER_MODEL_PREMIUM_SEARCH") or
              "perplexity/sonar"),
}


def _label_model(model, nama=""):
    if nama == "gemini":
        return "Gemini"
    return MODEL_LABELS.get(model or "", model or "AI")


def _sasaran_pilihan(pilihan):
    """Endpoint sasaran untuk pilihan model reguler spesifik.

    Return list bentuk _endpoints(): (nama, base, cfg); base None = Gemini.
    Model yang sama dicoba lewat dua pintu berbeda dulu (mis. GPT-OSS via
    Cerebras lalu via Groq) sebelum sistem mengalihkan ke model lain.
    """
    cb = (os.environ.get("CEREBRAS_API_KEY") or "").strip()
    gq = (os.environ.get("GROQ_API_KEY") or "").strip()
    orr = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    eps = []
    if pilihan == "gptoss":
        if cb:
            eps.append(("cerebras", "https://api.cerebras.ai/v1",
                        {"key": cb, "model": "gpt-oss-120b"}))
        if gq:
            eps.append(("groq", "https://api.groq.com/openai/v1",
                        {"key": gq, "model": "openai/gpt-oss-120b"}))
    elif pilihan == "qwen":
        if cb:
            eps.append(("cerebras", "https://api.cerebras.ai/v1",
                        {"key": cb, "model": "qwen-3.8-27b"}))
        if gq:
            eps.append(("groq", "https://api.groq.com/openai/v1",
                        {"key": gq, "model": "qwen/qwen3.8-27b"}))
    elif pilihan == "gemini":
        if (os.environ.get("GEMINI_API_KEY") or "").strip():
            eps.append(("gemini", None, None))
    elif pilihan == "llama":
        if orr:
            eps.append(("openrouter", "https://openrouter.ai/api/v1",
                        {"key": orr,
                         "model": "meta-llama/llama-3.3-70b-instruct"}))
    return eps


async def _coba_eps(eps, prompt, max_tokens, sisa, batas=None):
    """Coba daftar endpoint berurutan. Return (teks, label) atau (None, None).

    Semantik sama dengan rotasi lama: 404 = lewati model, 429 = lewati
    seluruh provider itu (key-level), timeout budget = berhenti.
    batas: sisa detik maksimum khusus daftar ini (fase sasaran pemilih
    model dibatasi agar fase rotasi selalu kebagian budget — pelajaran
    tes live 2026-10-19: satu panggilan lambat bisa menghabiskan seluruh
    budget 40 detik dan menggagalkan request yang sebenarnya bisa
    diselamatkan rotasi).
    """
    import time as _time
    t_mulai = _time.monotonic()

    def jatah():
        s = sisa()
        if batas is not None:
            s = min(s, max(0.5, batas - (_time.monotonic() - t_mulai)))
        return s

    skip_provider = set()
    for name, base, cfg in eps:
        if name in skip_provider:
            continue
        if jatah() <= 0.5:
            break
        if name == "gemini":
            try:
                import tanya as tanya_mod
                out = await asyncio.wait_for(
                    tanya_mod.gemini_generate(
                        prompt, max_output_tokens=max_tokens),
                    timeout=jatah())
            except Exception as e:
                log.info("AI pilihan gemini gagal: %s", e)
                continue
            if out:
                return out, "Gemini"
            continue
        try:
            out = await asyncio.wait_for(
                _openai_chat(base, cfg["key"], cfg["model"], prompt,
                             max_tokens=max_tokens),
                timeout=jatah())
        except _KeyRateLimited:
            log.info("AI pilihan %s key kena 429, lewati provider", name)
            skip_provider.add(name)
            continue
        except RuntimeError:
            log.info("AI pilihan %s model %s tidak ada (404), lewati",
                     name, cfg["model"])
            continue
        except asyncio.TimeoutError:
            log.info("AI pilihan %s/%s kehabisan budget", name, cfg["model"])
            break
        except Exception as e:
            log.info("AI pilihan %s gagal: %s", name, e)
            continue
        if out:
            return out, _label_model(cfg["model"], name)
    return None, None


async def generate_pilihan(prompt, pilihan="auto", budget=40.0,
                           max_tokens=2000):
    """Generate dengan pilihan model eksplisit (fitur pemilih model).

    Return dict:
      teks       : str | None
      label      : label model yang BENAR-BENAR menjawab (utk ditampilkan)
      dialihkan  : True bila yang menjawab bukan model pilihan (reguler)
      status     : "ok" | "premium_gagal" | "gagal"

    Aturan (keputusan Sul 2026-10-19):
    - Premium (claude/sonar): HANYA model itu yang dicoba. Gagal =
      "premium_gagal" TANPA fallback; pemanggil (app.py) menampilkan
      kartu pilihan ke member dan tidak memotong saldo.
    - Reguler spesifik: model pilihan dicoba dulu (semua pintunya), lalu
      otomatis dialihkan ke rotasi penuh bila gagal; label = pemenang
      sebenarnya dan dialihkan=True bila beda dari pilihan.
    - auto: rotasi reguler biasa, dialihkan selalu False.
    """
    t0 = time.monotonic()

    def sisa():
        return max(0.5, budget - (time.monotonic() - t0))

    pilihan = (pilihan or "auto").strip().lower()
    if pilihan in PILIHAN_PREMIUM:
        key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
        model = _MODEL_PREMIUM_ID[pilihan].strip()
        if not key or not model:
            return {"teks": None, "label": PILIHAN_LABEL[pilihan],
                    "dialihkan": False, "status": "premium_gagal"}
        cap = min(int(max_tokens or 2000), PREMIUM_MAKS_TOKEN)
        for percobaan in (1, 2):
            try:
                out = await asyncio.wait_for(
                    _openai_chat("https://openrouter.ai/api/v1", key, model,
                                 prompt, max_tokens=cap),
                    timeout=sisa())
            except Exception as e:
                log.info("AI premium pilihan %s gagal (%d): %s",
                         model, percobaan, e)
                out = None
            if out:
                return {"teks": out, "label": _label_model(model),
                        "dialihkan": False, "status": "ok"}
            if percobaan == 1 and sisa() > 6:
                await asyncio.sleep(3)
        return {"teks": None, "label": PILIHAN_LABEL[pilihan],
                "dialihkan": False, "status": "premium_gagal"}

    if pilihan != "auto":
        sasaran = _sasaran_pilihan(pilihan)
        if sasaran:
            out, label = await _coba_eps(sasaran, prompt, max_tokens, sisa,
                                         batas=min(22.0, budget * 0.55))
            if out:
                return {"teks": out, "label": label, "dialihkan": False,
                        "status": "ok"}
        # Model pilihan gagal total -> alihkan ke rotasi penuh.
        eps = _endpoints()
        sudah = {(c or {}).get("model") for _, _, c in sasaran}
        eps = [e for e in eps
               if ((e[2] or {}).get("model") if e[2] else "gemini")
               not in sudah | {None}] if eps else []
        if pilihan == "gemini":
            eps = [e for e in eps if e[0] != "gemini"]
        out, label = await _coba_eps(eps, prompt, max_tokens, sisa)
        if out:
            return {"teks": out, "label": label,
                    "dialihkan": label != PILIHAN_LABEL.get(pilihan),
                    "status": "ok"}
        return {"teks": None, "label": None, "dialihkan": False,
                "status": "gagal"}

    out, label = await _coba_eps(_endpoints(), prompt, max_tokens, sisa)
    if not out and time.monotonic() - t0 < 12:
        # Pola lama rotasi: gagal sangat cepat -> ulangi sekali setelah jeda.
        await asyncio.sleep(2)
        out, label = await _coba_eps(_endpoints(), prompt, max_tokens, sisa)
    if out:
        return {"teks": out, "label": label, "dialihkan": False,
                "status": "ok"}
    return {"teks": None, "label": None, "dialihkan": False,
            "status": "gagal"}
