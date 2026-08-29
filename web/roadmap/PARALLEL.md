# PARALLEL.md — Wave plan per subagenti paralleli (~10 per wave)

> Regole d'oro: ogni **wave** tocca file disgiunti (nessun conflitto su file
> condivisi); `src/index.js` e `views/partials/sidebar.ejs` sono modificati
> SOLO dai task `-integration`. I task GP (gateway) sono SEQUENZIALI tra loro.
> Durata stimata per task: 30–90 min.
>
> **Regola test**: SOLO l'agente del task `*-integration` lancia `npm test`;
> tutti gli altri subagenti fanno al massimo `node --check` sui propri file
> (mai due subagenti in parallelo che girano `npm test`: race su migrazioni e
> DB di test condiviso).

## F0 — Scaffold (6 wave)

| Wave | Task-id | Contenuto | Barriera |
|---|---|---|---|
| **0A** (5) | F0-01, F0-02, F0-03, F0-04, F0-08 | repo; docker/compose; migrazioni; config/db/logger; layout/theme | — |
| **0B** (2) | F0-05, F0-06 | wrapper gateway + fixture mock COMPLETA; api-tokens service | dipende 0A |
| **0C** (2) | F0-07, F0-09 | auth+RBAC (matrice permessi COMPLETA fin da subito); health+openapi base | dipende 0A+0B |
| **0C-bis** (1) | F0-10 | route api-tokens + agent-login | dipende F0-07 |
| **0D** (1) | F0-11 | **INTEGRAZIONE F0** (wire index.js, test harness, smoke) | dipende 0A..0C-bis |
| *(smoke)* | → F0 verde | `npm test` (SOLO qui) | PRIMA BARRIERA |

Nota wave: F0-09 spostata in 0C (dopo F0-05, perché `health.js` importa
`services/gateway.js`); F0-10 in 0C-bis da sola dopo F0-07 (appende a
`routes/auth.js`); F0-11 da sola perché tocca index.js.

## F1 — Parità READ (2 wave)

| Wave | Task-id | Contenuto | Barriera |
|---|---|---|---|
| **1A** (10) | F1-01 … F1-10 | deployments list, profili, policy, capacità, dashboard, scadenze, history, insights, bootstrap, guide | richiede F0-11 |
| **1B** (1) | F1-11 | **INTEGRAZIONE F1** (mount 10 router, sidebar, smoke read) | dipende 1A |
| *(smoke)* | → F1 verde | tutte le pagine read 200 con GATEWAY_MOCK | 2ª BARRIERA |

## F2 — Parità WRITE (2 wave)

| Wave | Task-id | Contenuto | Barriera |
|---|---|---|---|
| **2A** (9) | F2-01 … F2-08, F2-10 | CRUD, bulk, profili write, policy editor, policy keys, system actions, probe, capabilities write, **users admin** | richiede F1-11; file disgiunti |
| **2B** (1) | F2-09 | **INTEGRAZIONE F2** (flash parsing, mount, smoke write) | dipende 2A |
| *(smoke)* | → F2 verde | mutazioni e2e su mock + audit_log | 3ª BARRIERA |

Nota wave: `src/constants/permissions.js` NON è toccato da F2-02/F2-07 (né da
nessun task nella stessa wave): la matrice completa nasce in F0-07 e le uniche
eventuali append stanno in F2-09.

## F3 — Osservabilità (2 wave)

| Wave | Task-id | Contenuto | Barriera |
|---|---|---|---|
| **3A** (4) | F3-01, F3-02, F3-03, F3-04 | live, errori, leaderboard, chart uPlot | richiede F1-11 |
| **3B** (1) | F3-05 | **INTEGRAZIONE F3** | dipende 3A |
| *(smoke)* | → F3 verde | 4ª BARRIERA |

## F4 — "E anche di più" (3 wave)

Le wave 4A e 4B sono su CORSI indipendenti: 4A non richiede i prerequisiti
gateway, 4B sì (wave 4B SOLO dopo GP fatti). GP sono paralleli a 4A.

| Wave | Task-id | Contenuto | Barriera |
|---|---|---|---|
| **GP** (1 alla volta) | GP-01, GP-02, GP-03, GP-04 | prerequisiti gateway (sequenziali) | varia; esauriti → 4B |
| **4A** (4) | F4-05, F4-06, F4-07, F4-08 | key-health, alerts, sticky, ui-polish | richiede F2-09; nessun GP |
| **4B** (4) | F4-01, F4-02, F4-03, F4-04 | csv-editor, playground, policy-raw, config-history | richiede F2-09 + GP-01..04 |
| **4C** (1) | F4-09 | **INTEGRAZIONE F4** (snapshot-hook, poller start, mount, smoke) | dipende 4A+4B |
| *(smoke)* | → F4 verde | 5ª BARRIERA |

## F5 — Agenti + API + MCP (4 wave)

| Wave | Task-id | Contenuto | Barriera |
|---|---|---|---|
| **5A** (3) | F5-01, F5-02, F5-03 | api read deployments, api read core, api write | richiede F1/F2 (F5-01 richiede F1-01+F2-01; F5-02 richiede F2-09) |
| **5A-bis** (1) | F5-06 | AGENT.md bilingue | dipende 5A (legge i file route che 5A crea) |
| **5B** (2) | F5-04, F5-05 | MCP server; openapi completo | dipende 5A |
| **5C** (1) | F5-07 | **INTEGRAZIONE F5** (mount api+mcp+docs, e2e, README) | dipende 5B + F4-09 |
| *(smoke)* | → F5 verde | e2e completo (UI+API+MCP) | BARRIERA FINALE |

Nota wave: F5-06 tolta dalla wave parallela a F5-01 (legge route che F5-01 non
ha ancora creato): eseguita in 5A-bis, dopo F5-01..03. 5B richiede solo 5A;
F5-06 può girare anche in parallelo a 5B (documentazione, nessun conflitto di
file), ma MAI insieme a F5-01 nella stessa wave.

## Conteggio riepilogo

| Fase | Task | Wave parallele (max simultanei) |
|---|---|---|
| F0 | 11 | 0A(5) → 0B(2) → 0C(2) → 0C-bis(1) → 0D(1) |
| F1 | 11 | 1A(10) → 1B(1) |
| F2 | 10 | 2A(9) → 2B(1) |
| F3 | 5 | 3A(4) → 3B(1) |
| F4 | 9 | 4A(4) ∥ GP(1×4 seq) → 4B(4) → 4C(1) |
| F5 | 7 | 5A(3) → 5A-bis(1) → 5B(2) → 5C(1) |
| GP | 4 | sequenziali (1 alla volta) |
| **Totale** | **57** | — |

Picchi di parallelismo: wave 1A = 10 agenti identici; 2A = 9; 0A = 5.
Ogni barriera (integrazione + smoke `npm test`) va superata prima di aprire
le wave successive.