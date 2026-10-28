"""Rotasi provider AI: Cerebras (berbayar) -> Gemini -> Groq -> OpenRouter.

Tujuan: menumpuk free tier beberapa provider agar bot tahan banyak request
tanpa keluar biaya. Semua provider cadangan memakai API OpenAI-compatible
(/chat/completions); hanya Gemini yang memakai API-nya sendiri.

PORT 2026-10-28 (merge patch AI khairypustaka-web ke kode produksi,
TANPA deploy): jalur premium berbayar OpenRouter (Claude Haiku /
Perplexity Sonar) DINONAKTIFKAN TOTAL, dan semua provider dikunci.
Satu-satunya model yang boleh dipanggil adalah 9 pilihan picker di
MODEL_REGISTRY di bawah (2 Cerebras + 1 Gemini Flash + 6 OpenRouter
gratis) plus mode "auto" yang memakai rotasi yang sama. Env
OPENROUTER_MODEL, OPENROUTER_MODEL_PREMIUM*, CEREBRAS_MODEL, dan
GROQ_MODEL sengaja TIDAK dibaca lagi untuk memilih model — nilai lama
di Render tidak boleh melesetkan request ke model berbayar/mati.
Pemanggil lama yang masih mengirim premium=True jatuh ke rotasi gratis
(tanpa memotong saldo model premium; saldo top-up tetap dipakai sebagai
jatah AI ekstra lewat ai_try_consume di skripsi_ai, bukan untuk model).

Key yang kosong di .env = provider dilewati otomatis.
"""
import asyncio
import logging
import os
import time

log = logging.getLogger("ai_providers")


# Registry 9 model picker (port patch 2026-10-28). "key" = kunci picker
# produksi (yang dikirim frontend di field "model"); "id" = id model
# sebenarnya yang dikirim ke provider. Hanya berisi id + label + provider;
# TIDAK ada rahasia di sini.
MODEL_REGISTRY = [
    {"key": "gptoss", "id": "gpt-oss-120b", "label": "GPT OSS 120B",
     "provider": "cerebras", "group": "cerebras"},
    {"key": "qwen", "id": "qwen-3.8-27b", "label": "Qwen 3.8 27B",
     "provider": "cerebras", "group": "cerebras"},
    {"key": "gemini", "id": "gemini-flash", "label": "Gemini Flash",
     "provider": "gemini", "group": "google"},
    {"key": "gemma31", "id": "google/gemma-4-31b-it:free",
     "label": "Gemma 4 31B",
     "provider": "openrouter", "group": "openrouter"},
    {"key": "gemma26", "id": "google/gemma-4-26b-a4b-it:free",
     "label": "Gemma 4 26B",
     "provider": "openrouter", "group": "openrouter"},
    {"key": "nemotron_ultra", "id": "nvidia/nemotron-3-ultra-550b-a55b:free",
     "label": "Nemotron 3 Ultra",
     "provider": "openrouter", "group": "openrouter"},
    {"key": "inkling", "id": "thinkingmachines/inkling:free",
     "label": "Inkling",
     "provider": "openrouter", "group": "openrouter"},
    {"key": "nemotron_lightning",
     "id": "nvidia/nemotron-3.5-lightning:free",
     "label": "Nemotron 3.5 Lightning",
     "provider": "openrouter", "group": "openrouter"},
    {"key": "nemotron_nano",
     "id": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
     "label": "Nemotron 3 Nano Reasoning",
     "provider": "openrouter", "group": "openrouter"},
]

# KUNCI: satu-satunya model OpenRouter / Cerebras yang BOLEH dipanggil.
# Registry, rotasi _endpoints(), dan guard _openai_chat() semuanya merujuk
# ke sini. Menambah model = menambah id di sini DAN di MODEL_REGISTRY.
OPENROUTER_FREE_MODELS = tuple(
    e["id"] for e in MODEL_REGISTRY if e["provider"] == "openrouter")
CEREBRAS_MODELS = tuple(
    e["id"] for e in MODEL_REGISTRY if e["provider"] == "cerebras")


def _is_openrouter_free(model_id):
    """True hanya bila id persis ada di daftar kunci (dan berakhir :free)."""
    return (isinstance(model_id, str)
            and model_id.endswith(":free")
            and model_id in OPENROUTER_FREE_MODELS)


def get_model(model_id):
    """Cari entri registry berdasarkan id model sebenarnya; None bila tidak."""
    if not model_id:
        return None
    model_id = str(model_id).strip()
    for entry in MODEL_REGISTRY:
        if entry["id"] == model_id:
            return entry
    return None


def get_model_by_key(key):
    """Cari entri registry berdasarkan kunci picker; None bila tidak."""
    if not key:
        return None
    key = str(key).strip().lower()
    for entry in MODEL_REGISTRY:
        if entry["key"] == key:
            return entry
    return None


def model_label(model_id):
    """Label ramah untuk id registry; id mentah bila di luar registry."""
    entry = get_model(model_id)
    return entry["label"] if entry else str(model_id)


def registry_public():
    """Salinan registry untuk endpoint GET /api/models (tanpa key)."""
    return [dict(entry) for entry in MODEL_REGISTRY]


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

    # PORT 2026-10-28, kunci semua: Cerebras hanya 2 model Shared
    # Inference (tanpa override env CEREBRAS_MODEL, yang bisa menunjuk
    # model Dedicated yang mahal); Groq DINONAKTIFKAN total (model lama
    # sudah pensiun, tidak ada di picker, dan env GROQ_MODEL bisa
    # meleset ke model berbayar); OpenRouter SELALU 6 model gratis di
    # atas bila key ada (dulu hanya bila OPENROUTER_MODEL diisi manual,
    # dan nilai env itu bisa berbayar — sekarang env itu diabaikan).
    add("cerebras", "https://api.cerebras.ai/v1",
        os.environ.get("CEREBRAS_API_KEY"),
        list(CEREBRAS_MODELS))
    if os.environ.get("GEMINI_API_KEY", "").strip():
        eps.append(("gemini", None, None))
    add("openrouter", "https://openrouter.ai/api/v1",
        os.environ.get("OPENROUTER_API_KEY"),
        list(OPENROUTER_FREE_MODELS))
    return eps



async def _openai_chat(base, key, model, prompt, max_tokens=2000):
    """Satu panggilan chat/completions. Return str | None.

    max_tokens membatasi panjang output di sisi provider (ekonomi AI,
    T-2 audit 2026-10-17): tanpa ini model bisa mengembalikan puluhan ribu
    token dan membakar kredit berbayar per request.
    None = gagal sementara (coba lagi / pindah endpoint).
    Raise RuntimeError("skip") = endpoint mati (404) -> langsung endpoint berikut.
    KUNCI (port 2026-10-28): OpenRouter WAJIB id di OPENROUTER_FREE_MODELS,
    Cerebras WAJIB id di CEREBRAS_MODELS, Groq SELALU ditolak — dicek
    SEBELUM request dikirim. Request OpenRouter selalu dipasangi provider
    max_price 0/0 sebagai jaring kedua (OpenRouter menolak daripada
    menagih bila penyedia di belakangnya ternyata berbayar).
    """
    is_openrouter = "openrouter" in (base or "")
    if is_openrouter and not _is_openrouter_free(model):
        raise ValueError(
            "OpenRouter dikunci: model di luar 6 gratis ditolak: %s" % (model,))
    if "cerebras" in (base or "") and model not in CEREBRAS_MODELS:
        raise ValueError(
            "Cerebras dikunci: model di luar 2 Shared Inference ditolak: %s"
            % (model,))
    if "groq" in (base or ""):
        raise ValueError("Groq dinonaktifkan total sejak port 2026-10-28")
    import httpx
    for attempt in (1, 2):
        try:
            payload = {"model": model, "max_tokens": int(max_tokens),
                       "messages": [{"role": "user", "content": prompt}]}
            if is_openrouter:
                payload["provider"] = {
                    "max_price": {"prompt": 0, "completion": 0}}
            async with httpx.AsyncClient(timeout=30) as cli:
                r = await cli.post(
                    base + "/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json=payload)
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
            if is_openrouter:
                # Jaring ketiga (deteksi): biaya > 0 yang dilaporkan
                # OpenRouter untuk request yang seharusnya gratis dicatat
                # keras di log, bukan diam-diam menggerus saldo.
                try:
                    cost = float((data.get("usage") or {}).get("cost") or 0)
                except (TypeError, ValueError):
                    cost = 0
                if cost > 0:
                    log.warning("OpenRouter biaya > 0 terdeteksi: %s cost=%s",
                                model, cost)
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
                   max_tokens=2000, model=None, return_model=False):
    """Generate teks via provider pertama yang berhasil. Return str | None.

    premium=True -> DIABAIKAN sejak port 2026-10-28 (jalur berbayar
    dinonaktifkan; pemanggil lama langsung jatuh ke rotasi gratis).
    model: id registry dari MODEL_REGISTRY (opsional, port dari patch
    khairypustaka-web). Bila diisi, model itu dicoba dulu; bila gagal,
    fallback ke rotasi normal. Id tidak dikenal -> ValueError (bukan
    diam-diam memakai model lain). return_model=True -> (teks, id).
    Tanpa model/return_model, perilaku persis seperti produksi lama.

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

    def finish(text, used):
        return (text, used) if return_model else text

    chosen = None
    if model is not None:
        chosen = get_model(model)
        if chosen is None:
            raise ValueError("model tidak dikenal: %s" % (model,))

    # Model pilihan eksplisit dulu (bila ada), sebelum rotasi.
    if chosen is not None:
        try:
            if chosen["provider"] == "gemini":
                import tanya as tanya_mod
                out = await asyncio.wait_for(
                    tanya_mod.gemini_generate(
                        prompt, max_output_tokens=max_tokens),
                    timeout=sisa())
                if out:
                    log.info("AI pilihan pakai %s", chosen["id"])
                    return finish(out, chosen["id"])
            else:
                base = ("https://api.cerebras.ai/v1"
                        if chosen["provider"] == "cerebras"
                        else "https://openrouter.ai/api/v1")
                key_env = ("CEREBRAS_API_KEY"
                           if chosen["provider"] == "cerebras"
                           else "OPENROUTER_API_KEY")
                key = (os.environ.get(key_env) or "").strip()
                if key:
                    out = await asyncio.wait_for(
                        _openai_chat(base, key, chosen["id"], prompt,
                                     max_tokens=max_tokens),
                        timeout=sisa())
                    if out:
                        log.info("AI pilihan pakai %s", chosen["id"])
                        return finish(out, chosen["id"])
        except (_KeyRateLimited, RuntimeError, asyncio.TimeoutError) as e:
            log.info("AI pilihan %s gagal (%s), fallback ke rotasi",
                     chosen["id"], type(e).__name__)
        except Exception as e:
            log.info("AI pilihan %s gagal: %s", chosen["id"], e)
        log.info("AI pilihan %s gagal, fallback ke rotasi", chosen["id"])

    # REVOKASI 2026-10-28: argumen premium diabaikan total;
    # tidak ada lagi cabang berbayar, semua pemanggil jatuh ke rotasi
    # gratis di bawah.
    eps = _endpoints()
    if not eps:
        log.warning("AI: tidak ada provider terkonfigurasi (isi API key di .env)")
        return finish(None, None)
    async def _coba_rotasi():
        skip_provider = set()
        for name, base, cfg in eps:
            if name in skip_provider:
                continue
            # Jangan coba ulang model pilihan yang baru saja gagal.
            if chosen is not None:
                if name == "gemini" and chosen["provider"] == "gemini":
                    continue
                if cfg is not None and cfg.get("model") == chosen["id"]:
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
                    return out, "gemini-flash"
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
                used = cfg["model"] if get_model(cfg["model"]) else (
                    "%s/%s" % (name, cfg["model"]))
                return out, used
        return None, None

    out, used = await _coba_rotasi()
    if out:
        return finish(out, used)
    # Semua provider gagal. Ulangi rotasi SEKALI saja dan hanya bila
    # kegagalannya SANGAT cepat (pola khas 429 beruntun di free tier yang
    # pulih dalam hitungan detik). Batas diperketat T-2 audit 2026-10-17:
    # rotasi kedua menggandakan biaya & waktu request, jadi hanya untuk
    # kegagalan di bawah 12 detik pertama.
    if time.monotonic() - t0 < 12:
        log.info("AI: rotasi pertama gagal cepat, jeda 2 dtk lalu coba lagi")
        await asyncio.sleep(2)
        out, used = await _coba_rotasi()
        if out:
            return finish(out, used)
    log.warning("AI: semua provider gagal")
    return finish(None, None)


# ================================================================ PEMILIH MODEL
# Fitur pemilih model (keputusan Sul 2026-10-19; daftar diperbarui port
# 2026-10-28): pengguna memilih model di AI Studio / Chat Tanya / Ide
# Topik. Kunci pilihan dari frontend SELALU divalidasi app.py terhadap
# daftar putih; fungsi di sini hanya menerima kunci yang sudah sah.
# Kunci lama yang sudah tidak ada (llama, claude, sonar — mis. tersimpan
# di localStorage user dari versi lama) TIDAK error: generate_pilihan
# memetakannya ke "auto" (lihat di bawah), tanpa memotong saldo premium.

MODEL_LABELS = {e["id"]: e["label"] for e in MODEL_REGISTRY}

PILIHAN_LABEL = {"auto": "Otomatis"}
PILIHAN_LABEL.update({e["key"]: e["label"] for e in MODEL_REGISTRY})

# REVOKASI 2026-10-28: tidak ada lagi pilihan premium; definisi
# premium lama dihapus total (bukan lagi set kosong dormant).

# Kunci picker lama -> pemetaan aman (tanpa saldo, tanpa error).
_LEGACY_KE_AUTO = {"llama", "claude", "sonar"}


def _label_model(model, nama=""):
    if nama == "gemini":
        return "Gemini Flash"
    return MODEL_LABELS.get(model or "", model or "AI")


def _sasaran_pilihan(pilihan):
    """Endpoint sasaran untuk satu kunci picker (port 2026-10-28).

    Return list bentuk _endpoints(): (nama, base, cfg); base None = Gemini.
    Setiap kunci hanya punya SATU pintu yang terkunci (dulu GPT-OSS/Qwen
    dicoba lewat Cerebras lalu Groq; Groq sekarang dinonaktifkan, jadi
    pintu kedua itu hilang — dan tidak mengurangi pilihan, karena modelnya
    sama). Kunci di luar MODEL_REGISTRY -> [] (pemanggil jatuh ke rotasi).
    """
    entry = get_model_by_key(pilihan)
    if entry is None:
        return []
    if entry["provider"] == "cerebras":
        key = (os.environ.get("CEREBRAS_API_KEY") or "").strip()
        if key:
            return [("cerebras", "https://api.cerebras.ai/v1",
                     {"key": key, "model": entry["id"]})]
    elif entry["provider"] == "gemini":
        if (os.environ.get("GEMINI_API_KEY") or "").strip():
            return [("gemini", None, None)]
    elif entry["provider"] == "openrouter":
        key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
        if key:
            return [("openrouter", "https://openrouter.ai/api/v1",
                     {"key": key, "model": entry["id"]})]
    return []


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
                return out, "Gemini Flash"
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
      status     : "ok" | "gagal"

    Aturan (keputusan Sul 2026-10-19; diperbarui port 2026-10-28):
    - TIDAK ADA lagi pilihan premium (dihapus total 2026-10-28). Kunci lama
      (llama/claude/sonar) dipetakan ke "auto" — dijawab rotasi gratis,
      tanpa memotong saldo, tanpa error — karena frontend lama / nilai
      localStorage lama masih bisa mengirimnya.
    - Reguler spesifik: model pilihan dicoba dulu, lalu otomatis dialihkan
      ke rotasi penuh bila gagal; label = pemenang sebenarnya dan
      dialihkan=True bila beda dari pilihan.
    - auto: rotasi reguler biasa, dialihkan selalu False.
    """
    t0 = time.monotonic()

    def sisa():
        return max(0.5, budget - (time.monotonic() - t0))

    pilihan = (pilihan or "auto").strip().lower()
    if pilihan in _LEGACY_KE_AUTO or pilihan not in PILIHAN_LABEL:
        # Kunci lama / tidak dikenal -> auto (produksi lama juga memetakan
        # nilai tak dikenal ke auto di app.py; ini jaring kedua di sini).
        if pilihan != "auto":
            log.info("AI pilihan lama/tak dikenal %s -> auto (tanpa saldo)",
                     pilihan)
        pilihan = "auto"

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
