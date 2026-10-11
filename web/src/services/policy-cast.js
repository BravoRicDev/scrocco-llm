// Fase 5 (FRONTEND-ROADMAP-OPERATIONAL.md): logica PURA di casting dei campi
// policy, estratta da `routes/policy.js` cosi' che i test unitari possano
// importarla senza transitare da `audit.js`/`db.js` (che richiedono DATABASE_URL).
//
// FIX del bug di cast numerico: `Number("")`, `Number("   ")` e `Number(null)`
// valgono tutti `0`, quindi un campo numerico svuotato in UI veniva salvato
// silenziosamente come `0`. Ora un valore vuoto/nullo su int e num e' un errore
// di validazione, non uno zero.

// Mappa metadati: -> tipo di cast per il campo (scalari, liste, mappe).
export const META = {
  scalar: {
    // GARA LENTA (slow-race): se il primo tentativo sta ancora generando
    // dopo N ms si apre un canario e si tiene buono il primo che consegna.
    // 0 = disattivata. Sono le stesse chiavi della TUI (sezione Warm).
    "stream_slow_race_after_ms": "int",
    "nonstream_slow_race_after_ms": "int",
    "stream_slow_race_canaries": "int",
    "server.host": "str",
    "server.port": "int",
    "server.ssl_enabled": "bool",
    "server.max_body": "num",
    "timeouts.connect": "int",
    "timeouts.read": "int",
    "timeouts.write": "int",
    "rate_limit.rpm": "int",
    "rate_limit.tpm": "int",
    "log.level": "str",
    "log.enabled": "bool",
    "debug": "bool",
    "adaptive.enabled": "bool",
    "adaptive.learn": "bool",
    "adaptive.min_samples": "int",
    "adaptive.confidence": "num",
    "qc.enabled": "bool",
    "qc.dedup_window": "int",
    "routing.stickiness": "bool",
    "routing.prefer": "str",
    "routing.fallback": "str",
  },
  list: {
    "routing.allowed_providers": "list",
    "routing.models": "list",
    "capability_routing.models": "list",
    "capability_routing.dedicated_models": "list",
    "server.allowed_origins": "list",
    "health.bind_hosts": "list",
  },
  map: {
    "capability_routing.model_capabilities": "map",
  },
};

export const LISTS = Object.fromEntries(Object.keys(META.list).map((k) => [k, "list"]));
export const MAPS = Object.fromEntries(Object.keys(META.map).map((k) => [k, "map"]));

export function castValue(field, raw) {
  const type = MAPS[field] ?? LISTS[field] ?? META.scalar[field] ?? null;

  if (type === "bool") {
    return { ok: true, value: raw === true || raw === "true" || raw === "1" || raw === 1 };
  }

  if (type === "int") {
    if (raw === null || raw === undefined || (typeof raw === "string" && raw.trim() === "")) {
      return { ok: false, msg: "valore numerico atteso" };
    }
    const n = Number(raw);
    if (!Number.isFinite(n)) return { ok: false, msg: "valore numerico atteso" };
    return { ok: true, value: Math.trunc(n) };
  }

  if (type === "num") {
    if (raw === null || raw === undefined || (typeof raw === "string" && raw.trim() === "")) {
      return { ok: false, msg: "valore numerico atteso" };
    }
    const n = Number(raw);
    if (!Number.isFinite(n)) return { ok: false, msg: "valore numerico atteso" };
    return { ok: true, value: n };
  }

  if (type === "list") {
    if (Array.isArray(raw)) return { ok: true, value: raw.map(String) };
    if (typeof raw === "string") {
      const parts = raw.split(/[\n,]+/).map((s) => s.trim()).filter(Boolean);
      return { ok: true, value: parts };
    }
    return { ok: false, msg: "lista attesa (valori separati da virgola o newline)" };
  }

  if (type === "map") {
    if (typeof raw === "object" && raw !== null && !Array.isArray(raw)) return { ok: true, value: raw };
    if (typeof raw === "string") {
      try {
        const parsed = JSON.parse(raw);
        if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) {
          return { ok: true, value: parsed };
        }
      } catch {}
    }
    return { ok: false, msg: "mappa attesa (oggetto JSON valido)" };
  }

  // Campo non censito o scalare generico: restituisce stringa grezza senza alterazione
  return { ok: true, value: String(raw ?? "") };
}
