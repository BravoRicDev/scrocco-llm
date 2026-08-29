"""Bootstrap playbook per agenti (self-setup zero-to-running).

Filosofia: l'agente non riceve una lista statica di modelli (invecchierebbe)
ma un PLAYBOOK -- fatti stabili (URL signup, formato chiavi, semantica CSV) +
istruzioni di RICERCA ONLINE per lo stato attuale dei free-tier + gli
endpoint admin gia' esistenti per inserire/validare le chiavi.

Tutte le route sono PUBBLICHE read-only: nessun segreto nelle risposte
(chiavi sempre mascherate), cosi' un agente su localhost interroga subito
dopo `docker compose up` senza cercare la master key.

Requisito costo-zero: /status NON fa mai chiamate upstream; anche il probe
(app/admin.py) e' one-shot con cache persistente perche' alcuni free-tier
contano le CHIAMATE, non i token.
[EN] WHAT: machine-readable first-run playbook for AI agents. The agent
gets stable facts only (signup URLs, key formats, CSV semantics) plus
instructions to RESEARCH current free tiers online -- hardcoded model
lists rot. /bootstrap/status computes live gaps with zero upstream calls;
the probe (admin.py) is one-shot with persistent cache because some free
tiers count CALLS, not tokens.
"""
from __future__ import annotations

import os
import time

from fastapi import APIRouter

from . import csv_store
from .admin import probe_results_view

bootstrap_api = APIRouter()

# --------------------------------------------------------------- registry --
# FATTI STABILI soltanto: niente modelli "consigliati" che tra un mese sono
# sbagliati. Il modello corrente lo sceglie l'agente con ricerca online (F2)
# e lo VERIFICA con GET {api_base}/models dopo aver registrato la chiave.
PROVIDERS = [
    {"id": "groq", "signup_url": "https://console.groq.com/keys",
     "api_base": "https://api.groq.com/openai/v1",
     "key_prefix": "gsk_", "data_hint": "free",
     "notes": "Generous free tier (rate-limited). OpenAI-compatible. "
              "Models often named openai/<model>."},
    {"id": "openrouter", "signup_url": "https://openrouter.ai/settings/keys",
     "api_base": "https://openrouter.ai/api/v1",
     "key_prefix": "sk-or-v1-", "data_hint": "paid",
     "notes": "Aggregator: hundreds of models behind one key. ':free' "
              "models cost nothing but are rate-limited. Vendor-prefixed "
              "ids (bytedance/seedance-* for video)."},
    {"id": "google", "signup_url": "https://aistudio.google.com/apikey",
     "api_base": "https://generativelanguage.googleapis.com/v1beta/openai",
     "key_prefix": "AIza", "data_hint": "free",
     "notes": "AI Studio keys are free-tier first. OpenAI-compatible. "
              "Strong vision and image-gen (gemini-*-image)."},
    {"id": "mistral", "signup_url": "https://console.mistral.ai/api-keys",
     "api_base": "https://api.mistral.ai/v1",
     "key_prefix": None, "data_hint": "free",
     "notes": "Free experimental tier on some models. Plain model names."},
    {"id": "nvidia", "signup_url": "https://build.nvidia.com/",
     "api_base": "https://integrate.api.nvidia.com/v1",
     "key_prefix": "nvapi-", "data_hint": "free",
     "notes": "NIM free credits at signup. Vendor-prefixed ids "
              "(meta/llama-*)."},
    {"id": "cloudflare",
     "signup_url": "https://dash.cloudflare.com/profile/api-tokens",
     "api_base": "https://api.cloudflare.com/client/v4/accounts/"
                 "<ACCOUNT_ID>/ai/v1",
     "key_prefix": None, "data_hint": "free",
     "notes": "Workers AI: token AND ACCOUNT_ID (api_base embeds it). "
              "@cf/* model names. Proprietary error body format."},
    {"id": "local-stt", "signup_url": None,
     "api_base": "http://speaches:8000/v1",
     "key_prefix": "-", "data_hint": "free",
     "notes": "docker-compose ships a speaches sidecar (whisper/piper): "
              "STT/TTS without any external account."},
]

CAPS = ["text", "vision", "image_gen", "video_gen", "tts", "stt"]

_RESEARCH_HINTS = {
    "text": ["<provider> free tier API models", "openrouter :free models"],
    "vision": ["<provider> vision model api free", "openrouter vision free"],
    "image_gen": ["google aistudio image generation api free",
                  "openrouter image generation free"],
    "video_gen": ["openrouter video generation models",
                  "seedance veo api free tier"],
    "tts": ["openai-compatible tts api free"],
    "stt": ["whisper api free tier", "groq whisper large v3 turbo"],
}


def _playbook() -> dict:
    """Playbook completo: fasi 0..7 con goal/how_to/verify."""
    mk = ("GATEWAY_MASTER_KEY from .env.gateway next to docker-compose.yml "
          "(fresh install default: 'sk-master' -- CHANGE IT before exposing "
          "anything)")
    return {
        "service": "scrocco-llm",
        "what": "OpenAI-compatible multi-provider LLM gateway: adaptive "
                "routing by estimated context, capability groups, key "
                "rotation with cooldown escalation.",
        "auth": {
            "master_key": mk + " -> full /admin/* access.",
            "client_keys": "deterministic sk-<profile> keys for consumers; "
                           'custom overrides via PATCH /admin/policy '
                           '{"client_keys":{"profile":"sk-..."}}.',
        },
        "cost_warning": "Many free tiers count CALLS, not tokens. Validate "
                        "each key ONCE via /admin/deployments/probe (cached "
                        "result, pass force=true to re-test). Never probe "
                        "in loops.",
        "steps": [
            {"id": 0, "title": "Health check",
             "goal": "Confirm the gateway is running.",
             "how_to": "GET /healthz (no auth)",
             "verify": "HTTP 200"},
            {"id": 1, "title": "Gather requirements from the user",
             "goal": "Which capabilities do you need? "
                     f"{CAPS}. Typical max context per request?",
             "how_to": "Ask the user. If unsure, start with text-only: it "
                       "is enough for most agent workloads.",
             "verify": "A list of needed caps."},
            {"id": 2, "title": "Research current free-tier offers ONLINE",
             "goal": "Pick providers/models that exist TODAY. Do NOT trust "
                     "hardcoded model lists (they rot).",
             "how_to": [
                 "Web-search the queries in research_hints for each needed "
                 "cap and provider from GET /bootstrap/providers.",
                 "Record per candidate: exact model id, context window, "
                 "rate limits, free/paid.",
                 "Signup at the provider and create an API key (human or "
                 "browser step).",
                 "VERIFY the model exists: GET {api_base}/models with the "
                 "new key before inserting anything."],
             "research_hints": _RESEARCH_HINTS,
             "verify": "You hold >=1 valid API key per chosen provider and "
                       "confirmed target models exist."},
            {"id": 3, "title": "Insert deployments",
             "goal": "Feed keys into the gateway (hot-reload, no restart).",
             "how_to": "POST /admin/deployments/bulk with master key auth. "
                       "One object per (model x key): "
                       '{"profile":"collego","model":"openai/gpt-oss-120b",'
                       '"endpoint":"https://api.groq.com/openai/v1",'
                       '"key":"gsk_...","data":"free","context":128,'
                       '"max_input":8000,"priority":0,"caps":"text"}',
             "field_semantics": {
                 "profile": "namespace/tenant (creates sk-<profile> client "
                            "key); groups are named <prefix><profile>-...",
                 "model": "EXACT upstream id as verified in step 2",
                 "endpoint": "provider api_base",
                 "data": "free|priority -> preferred buckets; paid|fallback"
                         " -> last-resort bucket; a day number = monthly "
                         "renewal ordering inside -go",
                 "context": "kilo-units: 128 means 128k context window",
                 "caps": "comma-separated subset of " + str(CAPS) +
                         "; text rows may omit it"},
             "verify": "GET /admin/state -> new uniques present"},
            {"id": 4, "title": "Validate keys (ONCE)",
             "goal": "Prove every new deployment really works, burning at "
                     "most ONE call per key.",
             "how_to": 'POST /admin/deployments/probe/bulk '
                       '{"filter":"all"} (or filter "cap:<x>" / profile '
                       'name). Results are CACHED persistently: healthy '
                       "keys are NOT re-called on later runs unless you "
                       "pass force=true.",
             "verify": "Every deployment reports ok=true; delete or fix the "
                       "others via DELETE /admin/deployments/{id}."},
            {"id": 5, "title": "Client keys",
             "goal": "Give consumers a key without leaking admin powers.",
             "how_to": "Deterministic sk-<profile> works out of the box. "
                       "Optionally PATCH /admin/policy "
                       '{"client_keys":{"<profile>":"sk-custom"}} to '
                       "override it.",
             "verify": "Authorization: Bearer sk-<profile> on GET /v1/models "
                       "returns the visible models."},
            {"id": 6, "title": "Smoke test end-to-end",
             "goal": "One real request through the front door.",
             "how_to": "POST /v1/chat/completions with Bearer sk-<profile>: "
                       '{"model":"<proxy-prefix><profile>",'
                       '"messages":[{"role":"user","content":"ping"}]}',
             "verify": "HTTP 200 with choices[].message.content"},
            {"id": 7, "title": "Day-2 operations",
             "goal": "Everything else (rotation, cooldowns, policy tuning, "
                     "capability changes).",
             "how_to": "GET /admin/guide -- served by the gateway itself, "
                       "always in sync with the code.",
             "verify": "-"},
        ],
        "links": {
            "status": "/bootstrap/status",
            "providers": "/bootstrap/providers",
            "operational_guide": "/admin/guide",
        },
    }


def _gw():
    """Globali del servizio a runtime (evita import circolari)."""
    from . import main as mod
    return mod


@bootstrap_api.get("/bootstrap/providers")
async def bootstrap_providers():
    """Registro provider SOLO con fatti stabili + istruzioni di ricerca.

    Niente modelli consigliati: cambiano troppo spesso. L'agente fa ricerca
    online (step 2 del playbook) e verifica via GET {api_base}/models.
    """
    return {
        "disclaimer": "Signup URLs and api_base shapes are stable; model "
                      "availability and free tiers are NOT. Always research "
                      "current offers online, then GET {api_base}/models "
                      "with the fresh key to confirm exact model ids.",
        "data_categories": {
            "free": "preferred bucket, rotated first",
            "priority": "same as free, sorted first",
            "paid": "last-resort (-fallback bucket)",
            "fallback": "alias of paid",
            "1..31": "monthly renewal day: orders the -go bucket",
        },
        "providers": PROVIDERS,
        "research_hints": _RESEARCH_HINTS,
    }


@bootstrap_api.get("/bootstrap/status")
async def bootstrap_status():
    """Gap analysis LIVE, zero chiamate upstream.

    Pubblico: le chiavi sono sempre mascherate. Suggerisce azioni con gli
    endpoint esatti. Il probe NON viene mai auto-invocato: al massimo
    segnala quanti deployment non risultano ancora validati.
    """
    gw = _gw()
    cfg, pol = gw.config, gw.policy
    now = time.time()
    issues: list[dict] = []
    actions: list[dict] = []

    profiles = list(cfg.profiles)
    dep_total = sum(len(v) for v in cfg.groups.values())

    if dep_total == 0:
        issues.append({"code": "no_deployments", "severity": "critical",
                       "detail": "No deployments configured yet."})
        actions.append({
            "do": "Follow playbook steps 2-3",
            "endpoint": "/bootstrap"})
    else:
        # copertura capacita' per profilo
        want_caps = ["vision", "image_gen", "video_gen", "tts", "stt"]
        for p in profiles:
            have = set((cfg.chains_cap.get(p) or {}).keys())
            missing = [c for c in want_caps if c not in have]
            if missing:
                issues.append({
                    "code": "caps_missing", "severity": "info",
                    "profile": p, "missing": missing,
                    "detail": "No deployments declare these caps for this "
                              "profile (fine if you don't need them)."})
                actions.append({
                    "do": f"Add rows with caps column: {','.join(missing)}",
                    "endpoint": "POST /admin/deployments/bulk"})
        # chiavi sospette: cooldown attivo o streak alto (+ lifecycle state)
        suspicious = []
        retired_list = []
        kh = getattr(gw, "KEYHEALTH", None)
        for u, exp in getattr(gw.router, "_cooldown", {}).items():
            dep = cfg.deployment_by_unique(u)
            streak = gw.router.stats_for(u).fail_streak
            if exp > now or streak >= 3:
                suspicious.append({
                    "deployment": u,
                    "key_masked": csv_store.mask_key(dep["api_key"]) if dep else "?",
                    "cooldown_remaining_s": max(0, int(exp - now)),
                    "fail_streak": streak,
                    "health": (kh.data.get(u) or {}).get("state") or "suspect"})
        if kh:
            for u, rec in kh.data.items():
                if rec.get("state") == "retired":
                    retired_list.append({
                        "deployment": u,
                        "dead_since_days": max(0, int(
                            (now - (rec.get("first_dead_ts") or now)) / 86400)),
                        "last_reason": rec.get("last_reason")})
        if retired_list:
            # MAI azioni di DELETE: solo rotazione (PUT) + unretire manuale
            issues.append({
                "code": "retired_keys", "severity": "info",
                "count": len(retired_list), "items": retired_list[:20],
                "detail": "Keys excluded from routing (dead too long). CSV "
                          "untouched: rotate via PUT then POST "
                          "/admin/deployments/unretire."})
        if suspicious:
            issues.append({
                "code": "suspicious_keys", "severity": "warning",
                "count": len(suspicious), "items": suspicious[:20],
                "detail": "In cooldown or repeatedly failing upstream."})
            actions.append({
                "do": "Inspect via GET /admin/state; rotate key "
                      "(PUT) the dead ones",
                "endpoint": "/admin/deployments/{id}"})
        # mai validati (info: nessuna chiamata automatica!)
        from .admin import probe_results_view
        probed = probe_results_view()
        never = [u for g in cfg.groups.values() for u in
                 (d["unique"] for d in g) if u not in probed]
        if never:
            issues.append({
                "code": "never_probed", "severity": "info",
                "count": len(never),
                "detail": "Deployments without a recorded probe result. "
                          "Validate ONCE (cached afterwards): do not loop "
                          "probes on call-counted free tiers."})
            actions.append({
                "do": 'POST /admin/deployments/probe/bulk {"filter":"all"}',
                "endpoint": "/admin/deployments/probe/bulk"})

    if not pol.client_keys:
        issues.append({"code": "no_client_keys", "severity": "info",
                       "detail": "No custom client keys; deterministic "
                                 "sk-<profile> still works."})
        actions.append({
            "do": 'PATCH /admin/policy {"client_keys":{"<profile>":"sk-..."}}',
            "endpoint": "/admin/policy"})

    mk_env = os.environ.get("GATEWAY_MASTER_KEY")
    if not mk_env or mk_env == "sk-master":
        issues.append({"code": "master_key_is_default", "severity": "warning",
                       "detail": "GATEWAY_MASTER_KEY is unset or the "
                                 "default 'sk-master'. Fine on localhost; "
                                 "CHANGE IT before exposing the service."})
        actions.append({"do": "Set GATEWAY_MASTER_KEY in .env.gateway and "
                              "restart once", "endpoint": "-"})

    return {
        "profiles": profiles,
        "deployments": dep_total,
        "cap_coverage": {p: sorted((cfg.chains_cap.get(p) or {}).keys())
                         for p in profiles},
        "issues": issues,
        "actions": actions,
        "next": "/bootstrap (full playbook)",
    }


@bootstrap_api.get("/bootstrap")
async def bootstrap_playbook():
    """Playbook completo zero-to-running per gli agenti (inglese, pubblico).

    Il gateway appena installato (CSV vuoto) e' interrogabile SUBITO dopo
    `docker compose up`: nessuna auth, nessun segreto nelle risposte.
    """
    return _playbook()
