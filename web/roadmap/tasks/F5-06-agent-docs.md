---
id: F5-06-agent-docs
fase: F5
dipende_da: [F1-11-integration, F5-01-api-read-deployments]
puo_parallelo_con: F5-04-mcp-server, F5-05-openapi-full
---

> **Wave**: F5-06 NON va in parallelo con F5-01..F5-03 (legge i file route che
> F5-01 deve ancora creare). Eseguilo in 5A-bis, DOPO F5-01..03 (può girare in
> parallelo a F5-04/F5-05: è solo documentazione, nessun conflitto di file).

# F5-06 — AGENT.md bilingue (locales/{it,en}) + doc

## Obiettivo

Documentazione per agenti che useranno `/api/v1` e MCP: `docs/AGENT.md` (EN,
default) + copie localizzate `locales/en/AGENT.md`, `locales/it/AGENT.md`,
con endpoint, autenticazione (Bearer JWT agent o agtok_ ottenuto via
`scripts/agent-login.mjs`), esempi curl, errori, limiti.

## File da creare/modificare

- `scrocco-web/docs/AGENT.md` (crea — lingua EN)
- `scrocco-web/locales/en/AGENT.md` (crea — sinonimo)
- `scrocco-web/locales/it/AGENT.md` (crea — traduzione IT)

NON toccare altro.

## Contratto

- Struttura AGENT.md:
  1. Cos'è scrocco-web e come si collega al gateway.
  2. Auth: `Authorization: Bearer <token>` — dove il token è (a) un JWT agent
     da `scripts/agent-login.mjs` (password) oppure (b) un API token
     `agtok_...` creato in UI/CLI; scadenza, revoca, mascheramento.
  3. Endpoint `/api/v1` (tabella: metodo, path, query/body, RBAC).
  4. Esempi curl (read, create, bulk, probe, policy PATCH admin).
  5. Formato errori `{error:{message}}`, status.
  6. MCP: URL, tools disponibili (rimanda a F5-04 quando esiste).
  7. Rate limit e buone pratiche (probe one-shot, niente loop).
- `locales/{en,it}/AGENT.md` identici (o IT tradotto).
- Se serve `src/services/i18n.js` non ne ha bisogno: file statici.

## Criterio di done

I tre file esistono, il contenuto riflette LE VERE route create in
F5-01/F5-02/F5-03 (se quelle endpoint non sono ancora merigate, annota
"stabile dopo F5-integr"). `markdown` link interni relativi.

## Rischi / note

- Non inventare endpoint: riusa l'elenco reale (leggilo dai file route
  esistenti se serve).
- Pubblicato anche via `/api/v1/guide`? NO: `/guide` è il gateway. Se vuoi
  servire AGENT.md di scrocco-web, aggiungilo a `/api/v1/docs` in F5-05.

---

## PROMPT PRONTO PER IL SUBAGENTE

Lavora in `~/Serverino/scrocco-web` (solo path relativi; se `read` fallisce usa
`cat`/`sed -n`). Scrivi la documentazione per agenti.

Leggi `src/routes/api-deployments.js`, `src/routes/api-core.js`,
`src/routes/api-write.js` (se esistono già) per elencare gli ENDPOINT REALI.
Crea 3 file:
1. `docs/AGENT.md` (inglese) — contenuto come da contratto (cos'è, auth con i
   due metodi, tabella endpoint, esempi curl, errori, MCP puntato a
   `docs/MCP.md` se esiste altrimenti "in arrivo con F5-04", buone pratiche).
2. `locales/en/AGENT.md` — copia identica.
3. `locales/it/AGENT.md` — traduzione italiana fedele.

Nota: usa solo verbi già disponibili (endpoint esistenti); per quelli in
F5-04 scrivi "disponibile al termine della fase Agenti". Verifica che i tre
file siano markdown validi (parimenti arricchisci i link con path relativi).
Non toccare altro. Riepiloga in 2 righe.