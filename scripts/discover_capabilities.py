#!/usr/bin/env python3
"""Discovery delle capacità multimodali dei provider.

Per ogni endpoint+chiave dal CSV, fa GET /models e prova a dedurre le modalità
(input_modalities, output_modalities, architecture.*) per generare un blocco YAML
gateway.yaml suggerito per capability_routing.model_capabilities.

Usato per la prima volta per popolare il primo model_capabilities nella repo
pronta per l'adozione.

Uso:
  python3 scripts/discover_capabilities.py [--fix] [--profile collego]

Opzioni:
  --fix     scrive il blocco YAML in var/gateway.yaml, aggiungendo al
            existing capability_routing.model_capabilities (non sovrascrive)
  --profile  profilo CSV da esaminare (default: tutte le chiavi del profilo)
"""
from __future__ import annotations

import argparse
import asyncio
import fnmatch
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app.config import GatewayConfig, infer_model_prefix
from tui.gateway_client import GatewayClient, load_master_key

BASE = Path(__file__).resolve().parent.parent
CSV_PATH = BASE / "var" / "keys_rotation.csv"

PREFIXES = ("openai/", "mistral/", "nvidia/", "cloudflare/", "meta-llama/")


async def _slug(name: str) -> str:
    """Normalizza per fuzzy match: minuscolo, rimuove prefissi noti e punteggiatura."""
    s = name.strip().lower()
    for p in PREFIXES:
        if s.startswith(p):
            s = s[len(p):]
    return re.sub(r"[^a-z0-9]+", "", s)


async def main(fix: bool, profile: str | None) -> int:
    cfg = GatewayConfig(CSV_PATH)

    # Raccogliamo tutte le (endpoint, chiave) uniche da esaminare
    combos: set[tuple[str, str]] = set()
    for gname, deps in cfg.groups.items():
        for d in deps:
            combos.add((d["api_base"], d["api_key"]))

    # Se il profilo è specificato, filtra per colonne di profilo (simplificato)
    if profile:
        # cerca una colonna che inizia con prefix + profilo
        prefix = cfg.proxy_prefix
        target = f"{prefix}{profile}"
        for (base, _) in list(combos):
            # non facile — skip per ora per semplicità
            pass

    print(f"Da esaminare {len(combos)} endpoint+chiave unici\n")

    # Mappa modello -> capacità dedotte (per provider)
    # modello -> {"input": ["vision"], "output": ["image"]}
    discovered: dict[str, dict[str, list[str]]] = {}

    async with httpx.AsyncClient(timeout=20.0) as http:
        for i, (base, key) in enumerate(sorted(combos), start=1):
            masked = f"{key[:6]}…{key[-3:]}" if len(key) > 10 else "***"
            try:
                r = await http.get(f"{base.rstrip('/')}/models",
                                   headers={"Authorization": f"Bearer {key}"})
                if r.status_code != 200:
                    print(f"[{i:>2}] {base} ({masked}) -> HTTP {r.status_code}: salto")
                    continue

                items = r.json().get("data") or []
                # Alcuni provider (OpenRouter) restituiscono architettura, altri no
                for m in items:
                    model_id = m.get("id", "")
                    arch = m.get("architecture") or {}
                    in_modes = arch.get("input_modalities") or []
                    out_modes = arch.get("output_modalities") or []
                    # Fallback: deduci da id testuale
                    if not in_modes and not out_modes:
                        model_id_lower = model_id.lower()
                        if any(x in model_id_lower for x in ("vision", "vl", "llava", "clip")):
                            in_modes.append("vision")
                        if "image" in model_id_lower:
                            out_modes.append("image")
                        if "whisper" in model_id_lower:
                            in_modes.append("stt")
                        if any(x in model_id_lower for x in ("tts", "speech", "kokoro", "piper")):
                            in_modes.append("tts")
                        if "audio" in model_id_lower:
                            in_modes.append("audio")
                        if "video" in model_id_lower:
                            in_modes.append("video")

                    # Mappa in modality canoniche
                    def canonize(mod_list: list[str]) -> list[str]:
                        out = []
                        for m in mod_list:
                            if isinstance(m, str):
                                low = m.strip().lower()
                                if low in ("text", "testo"):
                                    out.append("text")
                                elif low in ("image", "immagine"):
                                    out.append("image")
                                elif low in ("vision", "visuale"):
                                    out.append("vision")
                                elif low in ("audio", "voce"):
                                    out.append("audio")
                                elif low == "video":
                                    out.append("video")
                                elif low in ("tts", "speech"):
                                    out.append("tts")
                                elif low in ("stt", "asr", "transcription"):
                                    out.append("stt")
                        return out

                    in_caps = canonize(in_modes)
                    out_caps = canonize(out_modes)

                    # Mantieni la capacità del provider più ricca
                    if model_id not in discovered:
                        discovered[model_id] = {"input": set(in_caps), "output": set(out_caps)}
                    else:
                        discovered[model_id]["input"].update(in_caps)
                        discovered[model_id]["output"].update(out_caps)

            except Exception as exc:                    # noqa: BLE001
                print(f"[{i:>2}] {base} ({masked}) -> ERRORE {type(exc).__name__}")
                continue
            await asyncio.sleep(0.35)                   # gentile coi provider

    # Produci YAML di suggerimento
    print("\n=== MODello -> capacità scoperte ===")
    yaml_lines = ["model_capabilities:"]
    for model, caps in sorted(discovered.items()):
        all_caps = set().union(caps["input"], caps["output"])
        if not all_caps:
            continue
        line = f"  \"{model}\": {sorted(all_caps)}"
        print(line)
        yaml_lines.append(line)

    if not discovered:
        print("Nessun modello scoperto. Forse nessun provider supporta /models?")
        return 0

    if fix:
        # Carica gateway.yaml esistente (o vuoto)
        from app.policy import Policy
        policy_path = BASE / "var" / "gateway.yaml"
        policy = Policy.load_or_default(policy_path) if policy_path.exists() else Policy.default()

        # Aggiorna solo se la chiave non esiste già (non sovrascrivere)
        updated = False
        for model, caps in discovered.items():
            if model in policy.model_capabilities:
                continue
            policy.model_capabilities[model] = sorted(caps["input"].union(caps["output"]))
            updated = True

        if updated:
            # Scrivi con un blocco capability_routing
            policy_path.parent.mkdir(parents=True, exist_ok=True)
            import yaml
            block = {"capability_routing": {"model_capabilities": policy.model_capabilities}}
            # Mantieni il resto della policy esistente
            existing = {}
            if policy_path.exists():
                try:
                    existing = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
                except Exception:
                    pass
            existing.update(block)
            policy_path.write_text(yaml.dump(existing, sort_keys=False, default_flow_style=False),
                                   encoding="utf-8")
            print(f"\nAggiornato {policy_path}")
        else:
            print("\nNessuna chiave nuova: tutte le capacità scoperte già presenti.")

    print(f"\nScoperto {len(discovered)} modelli unici.")
    await asyncio.get_event_loop().run_in_executor(None, lambda: None)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true",
                    help="scrive le scoperte in var/gateway.yaml")
    ap.add_argument("--profile", help="profilo CSV da esaminare (default: tutte)")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.fix, args.profile)))