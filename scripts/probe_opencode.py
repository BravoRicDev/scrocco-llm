#!/usr/bin/env python3
"""Probe fedele con httpx (il gateway usa httpx, e Cloudflare blocca urllib).
Riusa _session_headers del forwarder per il flusso x-opencode-session."""
from __future__ import annotations
import asyncio, csv, json, os, sys, time
from pathlib import Path
import httpx

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from app.forwarder import _session_headers

CSV = REPO / "var" / "keys_rotation.csv"
PROFILE_COL = os.environ.get("SCROCCO_PROFILE_COL", "scrocco-llm-example")
ZEN_EP = "https://opencode.ai/zen/v1"
GO_EP  = "https://opencode.ai/zen/go/v1"
ZEN_MODEL = "deepseek-v4-flash-free"
GO_MODEL  = "deepseek-v4-flash"


def key_rows():
    rows = list(csv.DictReader(open(CSV, encoding="utf-8-sig")))
    z = sorted({r[PROFILE_COL].strip() for r in rows
                if r["provider"].strip().lower()=="opencode-zen"
                and (r.get(PROFILE_COL) or "").strip()})
    g = sorted({r[PROFILE_COL].strip() for r in rows
                if r["provider"].strip().lower()=="opencode-go"
                and (r.get(PROFILE_COL) or "").strip()})
    return z, g


async def probe_key(client, key, api_base, model, tries=2):
    last = None
    for attempt in range(tries):
        dep = {"api_key": key, "api_base": api_base}
        hdr = _session_headers(dep, profile=os.environ.get("SCROCCO_PROFILE", "example"), client_ip="127.0.0.1")
        hdr.update({"Authorization": f"Bearer {key}",
                    "Content-Type": "application/json"})
        body = {"model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1}
        try:
            r = await client.post(f"{api_base}/chat/completions",
                                  json=body, headers=hdr, timeout=30.0)
            if r.status_code in (400, 502, 503, 504) and attempt < tries-1:
                last = (r.status_code, r.text[:150])
                await asyncio.sleep(2 + attempt*2)   # backoff
                continue
            return (key, r.status_code, r.text[:180])
        except Exception as e:
            last = (-1, str(e)[:150])
            await asyncio.sleep(1.5)
    return (key, last[0], last[1])


async def main():
    z, g = key_rows()
    print(f"probe: {len(z)} zen keys, {len(g)} go keys")
    out = {"zen": [], "go": []}
    async with httpx.AsyncClient(http2=False,
                                 headers={"User-Agent": "python-httpx/0.27.2"}) as client:
        # sequenziale con piccolo delay per non triggerare rate limit Cloudflare
        for k in z:
            res = await probe_key(client, k, ZEN_EP, ZEN_MODEL)
            out["zen"].append(res)
            await asyncio.sleep(0.3)
        for k in g:
            res = await probe_key(client, k, GO_EP, GO_MODEL)
            out["go"].append(res)
            await asyncio.sleep(0.3)

    def summarize(name, items):
        valid, inv, rl, credits, unk = [], [], [], [], []
        for k, c, d in items:
            if c == 200: valid.append(k)
            elif c == 401: inv.append((k, d))          # auth KO -> morta
            elif c == 429: rl.append(k)                # rate-limit -> tieni
            elif c == 403 and "Insufficient balance" in d: credits.append(k)  # auth OK, no soldi -> tieni
            elif c == 403: inv.append((k, d))          # 403 Cloudflare/altro -> investiga
            else: unk.append((k, c, d))
        print(f"\n=== {name}: {len(items)} keys ===")
        print(f"  ✓ 200 valide:          {len(valid)}")
        print(f"  🟡 429 rate-limit:     {len(rl)} (tieniamo valide)")
        print(f"  💰 403 no-credits:     {len(credits)} (auth OK, tieni)")
        print(f"  ✗ 401 invalid key:     {len(inv)}")
        print(f"  ? altro:               {len(unk)}")
        for k, d in inv:
            print(f"     401   {k[:14]}… {d[:80]}")
        for k, c, d in unk:
            print(f"     {c}    {k[:14]}… {d[:80]}")
        return valid, rl, credits, [k for k, _ in inv]

    vz, rz, cz, iz = summarize("zen", out["zen"])
    vg, rg, cg, ig = summarize("go", out["go"])

    # merge: una chiave è invalida SOLO se 401 su QUALSIASI provider
    inv_keys = set(iz) | set(ig)      # tutte le chiavi 401
    inv_all = sorted(inv_keys)
    valid_all = sorted(set(vz + rz + cz + vg + rg + cg) - inv_keys)
    print(f"\n=== SINTESI ===")
    print(f"chiavi totali nel CSV: {len(set(z)|set(g))}")
    print(f"  valide (200/429/403-credits): {len(valid_all)}")
    print(f"  INVALIDE (401): {len(inv_all)}")
    if inv_all:
        print("  da rimuovere:")
        for k in inv_all:
            src = []
            if k in iz: src.append("zen")
            if k in ig: src.append("go")
            print(f"    {k[:14]}… ({'/'.join(src)})")

    (REPO / "var" / "probe_opencode_result.json").write_text(
        json.dumps({"valid": valid_all, "invalid": inv_all,
                    "zen_raw": out["zen"], "go_raw": out["go"]}, indent=2))
    print(f"\nrisultati in var/probe_opencode_result.json")


if __name__ == "__main__":
    asyncio.run(main())
