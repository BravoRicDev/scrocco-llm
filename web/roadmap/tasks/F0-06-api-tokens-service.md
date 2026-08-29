---
id: F0-06-api-tokens-service
fase: F0
dipende_da: [F0-04-core-services, F0-03-db-migrations]
puo_parallelo_con: F0-05-gateway-client
---

# F0-06 — `services/api-tokens.js`: token di lunga durata

## Obiettivo

Creare il servizio di API token di lunga durata (prefisso `agtok_`, hash
SHA-256 in DB, mai salvare il valore in chiaro, revoca singola, last_used_at):
stesso modello di `gestione-siti-riccardom/src/services/api-tokens.js`. È il
meccanismo con cui gli agenti/automazioni si autenticheranno a `/api/*` e MCP.

## File da creare/modificare

- `scrocco-web/src/services/api-tokens.js` (crea)

NON toccare altri file.

## Contratto

- `createApiToken(userId, name, expiresInDays)` → ritorna `{id, token
  (grezzo, mostrato una sola volta), prefix, expiresAt, createdAt}`.
- `verifyApiToken(rawToken)` → utente `{sub, email, name, role,
  token_version, agent: true, api_token: true}` oppure `null` (token
  serializzato+scaduto+revocato, o utente non-active). Aggiorna `last_used_at`
  fire-and-forget.
- `isApiTokenFormat(rawToken)` → true se inizia con `agtok_`.
- `listApiTokens(userId)` → token con `token_prefix`, `expires_at`,
  `last_used_at`, `revoked_at`, `created_at`.
- `revokeApiToken(userId, tokenId)` → revoca (update `revoked_at` only se del
  proprietario).
- Tabella `api_tokens` (già creata da `db/001_schema.sql`).

## Criterio di done

```bash
cd ~/Serverino/scrocco-web
node --check src/services/api-tokens.js
```
E, se DB disponibile: mini script che crea un token, lo verifica, verifica una
stringa sbagliata → null, revoca → verify → null.

## Rischi / note

- Il valore grezzo esiste solo in memoria al momento della create: nessun log,
  nessun DB. Usa esattamente il pattern del CMS.
- `verifyApiToken` NON deve mai fallire con eccezione: ritorna null (requireAuth
  catch-and-null nel middleware).

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi `src/...`; se `read`
fallisce usa `cat`/`sed -n`).

Copia il servizio API token dal CMS gemello: leggi
`gestione-siti-riccardom/src/services/api-tokens.js` e replica la logica
identica (stesso prefisso `agtok_`, hash SHA-256, `token_prefix` visibile,
`last_used_at` aggiornato fire-and-forget, revoca per proprietario), usando
`src/db.js` di questo progetto e la tabella `api_tokens` già creata da
`db/001_schema.sql` (campi: id, user_id, name, token_hash, token_prefix,
expires_at, last_used_at, revoked_at, created_at).

Crea ESATTAMENTE `src/services/api-tokens.js` con:
- `createApiToken(userId, name, expiresInDays)` → {id, token, prefix,
  expiresAt, createdAt}
- `verifyApiToken(rawToken)` → utente {sub, email, name, role, token_version,
  agent:true, api_token:true} o null (mai eccezioni: usa .catch(()=>null))
- `isApiTokenFormat(rawToken)` → startsWith("agtok_")
- `listApiTokens(userId)`
- `revokeApiToken(userId, tokenId)`

Verifica: `node --check src/services/api-tokens.js`. Non creare altri file, non
toccare altro. Alla fine riepiloga in 2 righe le funzioni esportate.