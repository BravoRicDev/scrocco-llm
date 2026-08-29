"""Menu numerico minimale (fallback senza textual): ./scrocco.sh --cli

Stesse operazioni della TUI, via admin API. Nessuna dipendenza extra.
"""
from __future__ import annotations

import asyncio
import os
import sys

from .gateway_client import GatewayClient, GatewayError


def _p(msg: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        v = input(f"{msg}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return v or default


async def show_state(cli: GatewayClient) -> None:
    st = await cli.state()
    print(f"servizio={st['service_name']}  prefisso={st['prefix']}  "
          f"deployment={st['deployments']} ({len(st['profiles'])} profili)")
    print(f"cooldown attivi={len(st['cooldowns_active'])}  "
          f"sticky={len(st['sticky_sessions'])}")
    pol = st["policy"]
    print(f"salita: globale {pol['step_up_pct']}%  "
          f"per-profilo: {pol['step_up_per_profile'] or '-'}")
    print(f"alias: {pol['aliases']}")


async def show_profiles(cli: GatewayClient) -> None:
    for p in await cli.profiles():
        print(f"{p['name']:<12} dep={p['deployments']:<4} "
              f"dims={p['dims_k']}  salita={p['step_up_pct']}%")


async def list_deps(cli: GatewayClient) -> None:
    profs = [p["name"] for p in await cli.profiles()]
    pname = _p("profilo da elencare", ",".join(profs))
    deps = await cli.deployments(None if "," in pname else pname)
    for d in deps:
        caps = "".join({"vision": "V", "video": "D", "audio": "A",
                        "image_gen": "I", "tools": "T", "stt": "S",
                        "tts": "P", "video_gen": "G"}.get(c, "")
                       for c in (d.get("capabilities") or [])
                       if c != "text") or "-"
        print(f"{d['id'][:16]:<18}{d['profile']:<10}{d['modello'][:24]:<26}"
              f"{d['provider']:<12}{str(d['data'])[:8]:<10}"
              f"ctx={d['context_k']} caps={caps}\t{d['key_masked']}")


async def create_dep(cli: GatewayClient) -> None:
    payload = {
        "profile": _p("profilo"),
        "modello": _p("modello"),
        "provider": _p("provider", "groq"),
        "endpoint": _p("endpoint"),
        "data": _p("data (free/priority/fallback/paid/giorno 1-31)", "free"),
        "context": int(_p("context migliaia", "32")),
        "priority": int(_p("priority", "0")),
        "key": _p("chiave API"),
    }
    res = await cli.create_deployment(payload)
    print(f"creato: id={res['id']} totale={res['deployments_total']}")


async def delete_dep(cli: GatewayClient) -> None:
    did = _p("id deployment (drow_…)")
    res = await cli.delete_deployment(did)
    print(f"eliminato: {res['deleted']} (profilo {res['profile']})")


async def expiring_report(cli: GatewayClient) -> None:
    days = int(_p("entro quanti giorni", "7"))
    rows = await cli.expiring(days)
    if not rows:
        print("nessun rinnovo imminente ✓")
    for r in rows:
        print(f"in {r['in_days']:>3} gg  {r['modello']:<28} "
              f"(data raw: {r['data_raw']})")


async def policy_menu(cli: GatewayClient) -> None:
    data = await cli.policy_get()
    eff = data["effective"]
    print(f"prefisso='{eff['proxy_prefix']}'  go='{eff['go_suffix']}'  "
          f"fb='{eff['fallback_suffix']}'")
    print(f"salita globale={eff['step_up_pct']}%  per-profilo="
          f"{eff['profile_step_up_pct']}")
    print(f"timing: sticky={eff['sticky_ttl_sec']}s cooldown="
          f"{eff['cooldown_sec']}s divisor={eff['estimate_divisor']} "
          f"hotwin={eff['hotwords_window']}")
    print(f"legacy_prefixes={eff['legacy_prefixes']}  hotwords="
          f"{eff['hotwords']}")
    print(f"veloce: min_dim={eff.get('speed_min_dim_k')}k  "
          f"fit={eff.get('speed_qualify_pct')}%  "
          f"speed_hotwords={eff.get('speed_hotwords')}")
    for k, v in eff["aliases"].items():
        print(f"alias: {k} → {v}")

    what = _p("modificare? [s=salita-profilo | g=globali | a=alias | invio=no]")
    if what == "s":
        prof = _p("profilo")
        val = int(_p("nuova soglia % (1-200)"))
        await cli.policy_patch({"profiles": {prof: {"step_up_pct": val}}})
        print("applicata ✓")
    elif what == "g":
        key = _p("parametro (step_up_pct/sticky_ttl_sec/cooldown_sec/…)")
        val = int(_p("valore"))
        await cli.policy_patch({key: val})
        print("applicata ✓")
    elif what == "a":
        full = dict(eff.get("aliases") or {})
        name = _p("nome alias")
        tgt = _p("destinazione (col prefisso)")
        full[name] = tgt
        await cli.policy_patch({"aliases": full})
        print("applicata ✓")


async def ops_menu(cli: GatewayClient) -> None:
    what = _p("[c=sblocca cooldown | s=rilascia sessioni | r=reload]")
    if what == "c":
        u = _p("unique (vuoto=tutti)")
        res = await cli.clear_cooldowns(u or None)
        print(f"sbloccati: {res['cleared']}")
    elif what == "s":
        sid = _p("session_id (vuoto=tutte)")
        res = await cli.release_sessions(sid or None)
        print(f"rilasciate: {res['released']}")
    elif what == "r":
        res = await cli.reload()
        print(f"reload ok: {res['deployments']} deployment")


MENU = """
── scrocco-llm · menu lite ──────────────────────
 1) stato            2) profili         3) deployment
 4) nuovo deployment 5) elimina deploy. 6) rotazioni in arrivo
 7) policy           8) ops runtime     0) esci
"""


async def run() -> None:
    cli = GatewayClient()
    if not cli.master_key:
        print("master key mancante: setta GATEWAY_MASTER_KEY o compila "
              ".env.gateway")
        sys.exit(2)
    print(f"connesso a {cli.base_url}")
    actions = {1: show_state, 2: show_profiles, 3: list_deps,
               4: create_dep, 5: delete_dep, 6: expiring_report,
               7: policy_menu, 8: ops_menu}
    while True:
        print(MENU)
        try:
            choice = int(_p("scelta", "0"))
        except ValueError:
            continue
        if choice == 0:
            break
        action = actions.get(choice)
        if not action:
            continue
        try:
            await action(cli)
        except GatewayError as exc:
            print(f"ERRORE [{exc.status}]: {exc.message}")


if __name__ == "__main__":
    asyncio.run(run())
