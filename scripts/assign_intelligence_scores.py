#!/usr/bin/env python3
"""
Assegna le colonne `effort_capable` e `intelligence_score` ai modelli nel CSV.

- Legge var/keys_rotation.csv (10 colonne).
- Per ogni riga calcola (intelligence_score 1-10, effort_capable bool).
- Scrive var/keys_rotation.csv.new con le 2 colonne in coda.

La tabella e' keyed sul nome "normalizzato": minuscolo, basename dopo l'ultimo
'/' (rimuove prefissi vendor tipo openai/, nvidia/, @cf/...), senza suffissi
:free / -free / -0731 / -vN / -preview.
"""

import csv
import re
from pathlib import Path

CSV_PATH = Path("/home/serverino/Serverino/scrocco-llm/var/keys_rotation.csv")
OUTPUT_PATH = Path("/home/serverino/Serverino/scrocco-llm/var/keys_rotation.csv.new")
BASE_COLS = 10

# model name normalizzato -> (intelligence_score, effort_capable, nota)
MODEL_SCORES = {
    # --- Reasoning / effort-capable ---
    "o1": (10, True, "OpenAI o1"),
    "o1-pro": (10, True, "OpenAI o1 Pro"),
    "o1-mini": (9, True, "OpenAI o1-mini"),
    "o3-mini": (10, True, "OpenAI o3-mini"),
    "o4-mini": (10, True, "OpenAI o4-mini"),
    "deepseek-r1": (9, True, "DeepSeek R1"),
    "deepseek-r1-distill-qwen-32b": (8, True, "DeepSeek R1 Distill Qwen 32B"),
    "qwq-32b": (8, True, "Qwen QwQ 32B"),
    "qwen3.8-27b-a3b-reasoning": (7, True, "Qwen 3.8 27B Reasoning"),
    "nemotron-3-nano-omni-30b-a3b-reasoning": (7, True, "Nemotron 3 Nano Omni Reasoning"),
    "nemotron-3-ultra-550b-a55b": (7, True, "Nemotron 3 Ultra 550B (reasoning)"),
    "glm-5.3": (6, True, "GLM 5.3 (reasoning)"),

    # --- Chat / general high ---
    "gpt-4": (8, False, "GPT-4"),
    "gpt-4-turbo": (8, False, "GPT-4 Turbo"),
    "gpt-4o": (7, False, "GPT-4o"),
    "claude-3.5-sonnet": (8, False, "Claude 3.5 Sonnet"),
    "deepseek-v3.1": (9, False, "DeepSeek V3.1"),
    "kimi-k3": (6, False, "Moonshot Kimi K3"),
    "minimax-m3": (6, False, "MiniMax M3"),
    "nemotron-3-120b-a12b": (7, False, "Nemotron 3 120B"),
    "nemotron-3-super-120b-a12b": (7, False, "Nemotron 3 Super 120B"),
    "qwen3.6-27b": (6, False, "Qwen 3.6 27B"),
    "qwen3.8-27b": (6, False, "Qwen 3.8 27B"),
    "qwen2.5-72b": (6, False, "Qwen 2.5 72B"),
    "gemini-1.5-pro": (7, False, "Gemini 1.5 Pro"),
    "gemini-2.5-pro": (7, False, "Gemini 2.5 Pro"),
    "mistral-large-latest": (6, False, "Mistral Large"),
    "codestral-latest": (6, False, "Codestral"),
    "deepseek-v4.1-flash": (6, False, "DeepSeek V4.1 Flash"),
    "deepseek-v4-1-flash": (6, False, "DeepSeek V4.1 Flash"),
    "gpt-oss-120b": (6, False, "GPT OSS 120B"),
    "llama-3.3-70b": (6, False, "Llama 3.3 70B"),
    "llama-3.3-70b-instruct-fp8-fast": (6, False, "Llama 3.3 70B FP8"),
    "llama-3.1-405b": (7, False, "Llama 3.1 405B"),
    "compound": (6, False, "Groq Compound"),

    # --- Mid ---
    "gpt-4o-mini": (5, False, "GPT-4o Mini"),
    "gpt-oss-20b": (5, False, "GPT OSS 20B"),
    "gemini-1.5-flash": (5, False, "Gemini 1.5 Flash"),
    "gemini-1.5-flash-8b": (5, False, "Gemini 1.5 Flash 8B"),
    "gemini-2.0-flash": (5, False, "Gemini 2.0 Flash"),
    "gemini-2.5-flash": (5, False, "Gemini 2.5 Flash"),
    "gemini-3.5-flash": (5, False, "Gemini 3.5 Flash"),
    "gemini-3.7-flash": (5, False, "Gemini 3.7 Flash"),
    "gemma-4-26b-a4b-it": (5, False, "Gemma 4 26B"),
    "gemma-4-31b-it": (5, False, "Gemma 4 31B"),
    "gemma-sea-lion-v4-27b-it": (4, False, "Gemma Sea Lion"),
    "mistral-medium-latest": (5, False, "Mistral Medium"),
    "mistral-nemo": (5, False, "Mistral Nemo"),
    "mistral-nemo-instruct": (5, False, "Mistral Nemo Instruct"),
    "mistral-small-3.1-24b-instruct": (5, False, "Mistral Small 3.1 24B"),
    "mixtral-8x7b-instruct": (5, False, "Mixtral 8x7B"),
    "llama-3.1-70b": (6, False, "Llama 3.1 70B"),
    "llama-3.1-8b-instruct-fast": (4, False, "Llama 3.1 8B Fast"),
    "llama-3.1-8b-instruct-fp8": (4, False, "Llama 3.1 8B FP8"),
    "llama-3.2-11b-vision-instruct": (5, False, "Llama 3.2 11B Vision"),
    "llama-3.2-3b-instruct": (3, False, "Llama 3.2 3B"),
    "llama-3.2-1b-instruct": (3, False, "Llama 3.2 1B"),
    "llama-4-scout-17b-16e-instruct": (5, False, "Llama 4 Scout"),
    "mimo-v2.5": (5, False, "Mimo v2.5"),
    "minimax-m2.7": (5, False, "MiniMax M2.7"),
    "qwen2.5-32b": (5, False, "Qwen 2.5 32B"),
    "qwen2.5-coder-32b": (5, False, "Qwen 2.5 Coder 32B"),
    "qwen2.5-coder-32b-instruct": (5, False, "Qwen 2.5 Coder 32B Instruct"),
    "qwen3-30b-a3b-fp8": (5, False, "Qwen 3 30B A3B FP8"),
    "qwen3.8-flash": (5, False, "Qwen 3.8 Flash"),
    "qwen3.8-27b-a3b-fp8": (6, False, "Qwen 3.8 27B A3B FP8"),
    "nemotron-3-nano-30b-a3b": (6, False, "Nemotron 3 Nano 30B"),
    "nemotron-3.5-lightning": (5, False, "Nemotron 3.5 Lightning"),
    "nemotron-3.5-content-safety": (3, False, "Nemotron 3.5 Content Safety"),
    "glm-4.7-flash": (5, False, "GLM 4.7 Flash"),
    "granite-4.0-h-micro": (3, False, "Granite 4.0 Micro"),
    "allam-2-7b": (4, False, "Allam 2 7B"),
    "lfm-2.5-2.6b": (4, False, "Liquid LFM 2.5"),
    "nex-n2.5-mini": (4, False, "Nex N2.5 Mini"),
    "nex-n2.5-pro": (5, False, "Nex N2.5 Pro"),
    "inkling": (4, False, "Inkling"),
    "inkling-small": (3, False, "Inkling Small"),
    "dots-3-note": (3, False, "Dots 3 Note"),
    "dots-3-note-preview": (3, False, "Dots 3 Note Preview"),
    "north-mini-code": (4, False, "Cohere North Mini Code"),
    "muse-spark-1.2-contributor": (5, False, "Muse Spark 1.2"),
    "muse-spark-1.3-contributor": (5, False, "Muse Spark 1.3"),
    "laguna-s-2.1": (5, False, "Poolside Laguna S 2.1"),
    "laguna-xs-2.1": (4, False, "Poolside Laguna XS"),
    "ling-3.0-flash-fin": (5, False, "Ling 3.0 Flash Fin"),
    "ling-3.0-flash-sante": (5, False, "Ling 3.0 Flash Sante"),
    "compound-mini": (5, False, "Groq Compound Mini"),
    "deepseek-v4-flash": (5, False, "DeepSeek V4 Flash"),
    "deepseek-v4-flash-0731": (5, False, "DeepSeek V4 Flash 0731"),
    "deepseek-v3": (9, False, "DeepSeek V3"),
    "deepseek-v3.2": (7, False, "DeepSeek V3.2"),

    # --- Modelli speciali (vision/image/video/audio) ---
    "gemini-2.5-flash-image": (5, False, "Gemini 2.5 Flash Image"),
    "gemini-3-pro-image": (5, False, "Gemini 3 Pro Image"),
    "gemini-3.1-flash-image": (5, False, "Gemini 3.1 Flash Image"),
    "gemini-3.1-flash-lite-image": (5, False, "Gemini 3.1 Flash Lite Image"),
    "nano-banana-pro": (5, False, "Nano Banana Pro"),
    "nano-banana-pro-preview": (5, False, "Nano Banana Pro Preview"),
    "veo-3.1-lite": (5, False, "Veo 3.1 Lite"),
    "seedance-2.0-mini": (4, False, "Seedance 2.0 Mini"),
    "piper-it_it-paola-medium": (2, False, "Piper TTS Italiano"),
    "whisper-large-v3-turbo": (2, False, "Whisper Large V3 Turbo"),
    "faster-whisper-base": (2, False, "Faster Whisper Base"),
}

# Provider default quando il modello non e' in tabella
PROVIDER_DEFAULTS = {
    "openai": 7, "anthropic": 8, "google": 6, "deepseek": 7,
    "mistral": 6, "groq": 5, "openrouter": 6, "nvidia": 6,
    "cloudflare": 5, "airforce": 5, "api.airforce": 5, "bynara": 5,
    "cline": 5, "tokenharbor": 5, "tokenrouter": 5, "opencode-go": 5,
    "opencode-zen": 5, "unorouter": 5, "llm7": 5, "yolo-auto": 3,
    "speaches": 2,
}

# Fallback: modelli che accettano reasoning_effort in base al nome
EFFORT_CAPABLE_PATTERNS = [
    r"^o1(-|$)", r"^o3(-|$)", r"^o4(-|$)", r"deepseek-r1",
    r"qwq", r"reasoning", r"nemotron-3-ultra", r"glm-5\.3",
]


def normalize_model_name(model: str) -> str:
    """Minuscolo, basename dopo l'ultimo '/', senza suffissi free/versione."""
    m = model.strip().lower()
    if "/" in m:
        m = m.rsplit("/", 1)[1]
    m = re.sub(r":free.*$", "", m)
    m = re.sub(r"-free$", "", m)
    m = re.sub(r"-\d{4}$", "", m)      # -0731
    m = re.sub(r"-v\d+$", "", m)
    m = re.sub(r"-preview$", "", m)
    return m.strip()


def lookup_score(model: str, provider: str) -> tuple[int, bool]:
    """Ritorna (intelligence_score, effort_capable)."""
    norm = normalize_model_name(model)
    if norm in MODEL_SCORES:
        score, capable, _ = MODEL_SCORES[norm]
        return score, capable
    capable = any(re.search(p, norm) for p in EFFORT_CAPABLE_PATTERNS)
    score = PROVIDER_DEFAULTS.get(provider.strip().lower(), 5)
    return score, capable


def main() -> None:
    with open(CSV_PATH, "r", encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        print("CSV vuoto!")
        return

    header = rows[0][:BASE_COLS]
    new_header = header + ["effort_capable", "intelligence_score"]

    stats = {"total": 0, "capable": 0, "scores": {}}
    new_rows = [new_header]
    for i, row in enumerate(rows[1:], 1):
        if not any(c.strip() for c in row):
            continue
        while len(row) < BASE_COLS:
            row.append("")
        model = row[1].strip()
        provider = row[2].strip()
        score, capable = lookup_score(model, provider)
        stats["total"] += 1
        stats["capable"] += int(capable)
        stats["scores"][score] = stats["scores"].get(score, 0) + 1
        new_rows.append(row[:BASE_COLS] + [str(capable).lower(), str(score)])
        if i <= 3:
            print(f"  {model[:48]:48} {provider:15} score={score} capable={capable}")

    with open(OUTPUT_PATH, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(new_rows)

    print(f"\nRighe: {stats['total']} | effort_capable: {stats['capable']}")
    print(f"Distribuzione score: {dict(sorted(stats['scores'].items()))}")
    print(f"Output: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
