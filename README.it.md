<div align="right"><a href="README.md">🇬🇧 English</a> · <b>🇮🇹 Italiano</b></div>

# scrocco-llm

[![License: Unlicense](https://img.shields.io/badge/license-Unlicense-brightgreen.svg)](https://unlicense.org/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-compose%20up-blue.svg)](#in-cinque-comandi)
[![Tests](https://img.shields.io/badge/tests-1779%20passing-brightgreen.svg)](#test)

**Un gateway LLM che mette in pool decine di chiavi free e a pagamento, sceglie
il modello più piccolo che regge il contesto, e non spreca chiamate inutili.**

Zero database, un container, porta `4001`. È compatibile con l'API OpenAI,
quindi i client puntano al gateway senza cambiare nulla.

---

## Il problema che prova a risolvere

Se ti registri su più provider free — Groq, Google AI Studio, Mistral, NVIDIA,
OpenRouter, Cloudflare Workers AI — ti ritrovi con account diversi, finestre di
contesto diverse e modi diversi di fallire. Un agente che gira da solo prima o
poi trova un 429 nel momento sbagliato, oppure il modello scelto non regge il
prompt e taglia la risposta senza dirlo chiaramente.

scrocco-llm nasce per gestire agenti che lavorano senza supervisione, dove
questi due problemi capitano abbastanza spesso da volerli automatizzare via.

## Cosa fa, in tre punti

**Sceglie il modello minimo che ci sta.** Stima i token del prompt e lo manda
al gruppo di contesto più piccolo che basta (`-24k`, `-128k`, `-1000k`...). Le
"dim" non sono fisse: vengono lette dal CSV, quindi aggiungere un modello a
256k crea da solo il gradino `-256k`. Una richiesta esplicita (`-200k`,
`-1000k`) è una **soglia minima**: la rotazione sale, mai scende. Il limite
`max_input` di ogni deployment viene applicato **sempre** (anche sulle richieste
esplicite), e `max_tokens` è ridotto alla finestra residua (`max_input - input`),
così un modello 32k non viene mai scelto per un prompt da 150k.

**Ruota prima di rompersi, non solo dopo.** Un fallimento mette la chiave in
cooldown con escalation **lineare** (30 minuti di base, +30 per ogni
fallimento nelle ultime 24h, tetto a 5 ore). Un **timeout costa 10 volte
tanto**: un upstream che "appende" fa perdere tempo vero, quindi viene punito
molto più di un errore con codice. In più il budget guard impara i limiti dai
429 osservati e declassa le chiavi vicine alla soglia.

**Non confonde un fallback con un degrado silenzioso.** Una richiesta di
vision non atterra su un modello text-only. Se un gruppo di contesto è morto,
la catena sale in modo resiliente — al massimo qualche tentativo per gradino,
poi rianimazione dei cooldown stantii, un paracadute per le chiavi "croniche"
e infine il `-fallback` a pagamento. Lo streaming ha un watchdog: parte verso
il client solo quando arriva contenuto reale, e se un upstream si pianta ruota
in modo trasparente.

## Come funziona il routing (in breve)

1. **Risolvi** il modello/alias in un gruppo (`-vision`, `-200k`, ...).
2. **Stima** i token e scegli il gradino giusto (il richiesto, o il più piccolo
   che ci sta).
3. **Pesca** un deployment nel gruppo (punteggio adattivo: latenza EMA,
   recency, chiamate in volo, affinità di sessione; salta chiavi in cooldown o
   ritirate).
4. **Se fallisce**, marca il cooldown e cammina la scala: altri candidati del
   gradino → dim successive (in salita) → `-go` → cooldown stantii → paracadute
   cronici → `-fallback` → ultima spiaggia.
5. **Se riesce**, registra un eventuale "escalation winner" (se ha servito in
   salita) e scrive una riga `[summary]`.

### Escalation winner + ricampionamento

Quando una richiesta esce da un bucket morto e un gruppo più alto la serve, il
gateway **ricorda quel winner per il bucket richiesto**. Alla richiesta dopo
NON salta subito al winner: riprova una volta il bucket richiesto, poi sonda
fino a **due gruppi intermedi scelti a caso** (solo candidati vivi) e solo
allora va al winner ricordato. Così non rifà ogni volta la scala morta, ma
nemmeno si incolla ciecamente alla scorciatoia: se un gradino intermedio è
"guarito" nel frattempo, viene usato.

## Decisioni di design

Ogni modulo del codice parte con una docstring bilingue IT/EN che spiega cosa
fa, come lo fa e perché. Alcune scelte che vale la pena raccontare:

- **Cooldown lineare, tetto a 5 ore.** I free-tier si rinnovano su finestre
  brevi; tenere una chiave ferma un giorno intero era più spreco che prudenza.
  I timeout, però, vengono moltiplicati per 10 perché bruciano tempo reale.
- **Le dimensioni di contesto sono dinamiche.** Non c'è una lista fissa di
  gradini: nascono dai valori `context` del CSV. Aggiungere un modello estende
  la scala senza toccare il codice.
- **I generatori (`*_gen`) sono separati dalla chat a livello strutturale.**
  Una richiesta testo non deve atterrare su un endpoint immagini per errore, e
  viceversa — è una regola nel routing, non un filtro aggiunto dopo.
- **Le chiavi morte vengono ritirate, mai cancellate.** Dopo 7 giorni di
  fallimenti escono dal routing ma restano nel CSV: se ricarichi i crediti, un
  probe riuscito le riporta in vita da solo.
- **Il probe costa una chiamata per chiave, una volta sola.** Sui free-tier che
  contano le chiamate invece dei token, un probe periodico brucerebbe quota
  inutilmente. Il risultato resta su disco e viene riverificato solo con un
  `force=true` esplicito.
- **Protocolli nativi per riga (`api_style`).** Il gateway parla e accetta
  sempre OpenAI Chat Completions, ma ogni riga del CSV può dichiarare il
  protocollo nativo dell'upstream (`responses`, `messages`, `google`): richiesta,
  risposta e stream SSE vengono tradotti al volo, quindi i modelli che esistono
  solo su quegli SDK funzionano dallo stesso endpoint OpenAI-compatible. Anche
  il **thinking** attraversa il confine: se il client chiede reasoning
  (`reasoning_effort`/`effort`/`x-effort`, filtrato da `effort_capable` di riga)
  il gateway lo abilita in nativo (`reasoning.summary`, `thinking.budget_tokens`,
  `thinkingConfig`) e lo restituisce come `message.reasoning_content` (anche
  nella risposta finale non-stream) / `delta.reasoning_content` (stream), gli
  stessi campi dei provider OpenAI-compat in pass-through.
- **Session-dep guard.** Evita che sessioni concorrenti si usurpino gli stessi
  deployment free. L'ultima sessione che ha servito con **successo** un
  deployment free-dims se lo tiene (ownership rinnovata finché è viva); per le
  altre resta eleggibile solo nel tier pre-ultima-spiaggia, fra l'ultimo `-dim` e
  il `-go`. Dopo 15 minuti di silenzio l'intero set torna libero.
- **Warm pool (tier "caldi").** Prima del `-dim` richiesto e di tutta la scala
  vengono esauriti i free-dims che **questa sessione** ha già servito con
  successo (ancora vivi, non in cooldown, compatibili con `need` e `max_input`).
  Ordine: cache-holder di sessione, poi MRU, poi `order`, poi `max_input`
  crescente. Vale per il routing automatico e per i dim espliciti (`-Nk`); mai
  per `-go`/`-fallback` (escalation deliberata a pagamento). Tag log `[warm]`.
  Un dep con EMA di latenza sopra i 90s — o andato **lento per questa
  sessione** — esce da caldi/sticky/cache-holder e dalle selezioni successive;
  per la stessa sessione torna pescabile solo all'ultimo scaglione
  (`-fallback`/ultima spiaggia). Il pool caldi non pesca mai una dim
  **inferiore** a quella richiesta: con `...-200k` esplicito ignora i caldi
  `-64k` della stessa sessione (il `-Nk` è il minimo voluto dal client).
- **Cache pagata su `-go`/`-fallback` espliciti.** Quando il client chiama
  *esplicitamente* un gruppo testo `-go`/`-fallback`, il detentore cache della
  sessione (l'ultima chiave che le ha dato successo) vince anche sul tier di
  rinnovo: la sessione resta sull'identico account a scaldare la KV-cache e,
  al 429, il holder si esclude da solo e la rotazione prosegue nell'ordine
  normale — i crediti si sommano un account alla volta. Routing automatico ed
  escalation interne NON lo usano: lì resta il random nel tier migliore.
- **L'autoprobe non insiste.** Un probe KO fa **almeno raddoppiare** il residuo
  del cooldown, moltiplicato per il numero di probe fatti su quel deployment
  nelle ultime 24h; i residui oltre 2 ore escono dai probe e li rivede la scala
  che risveglia i cooldown o l'ultima spiaggia.
- **Cold spread (carico distribuito).** Quando la scelta è "a freddo" (nessun
  deployment della sessione in gioco) i dims vivi con **più tentativi** nelle
  ultime 24h (ok+fail) vengono nascosti dal pool per una quota configurabile
  (`cold_spread_pct`, default 20%), così anche i provider poco usati ricevono
  traffico a prescindere dalla colonna `order`. I deployment della sessione
  corrente sono sempre esentati; `-go`/`-fallback` e i gruppi capacità non sono
  toccati.

<a name="test"></a>Oltre 1100 test coprono queste logiche: molti sono nati da bug
reali, non sono test scritti per riempire una percentuale.

## Cosa non è

Mi sembra corretto dirlo prima, non dopo:

- **Non è un modo per non pagare.** È un modo per usare fino in fondo quello
  che i free-tier offrono legalmente, e spendere il credito a pagamento solo
  dove serve davvero.
- **Non è plug-and-play a zero account.** Devi comunque registrarti sui
  provider e creare le chiavi — quello nessun software può farlo al posto tuo.
  Il playbook `GET /bootstrap` guida passo passo anche un agente AI in questa
  fase, e la validazione costa una sola chiamata per chiave, cached.
- **Non è pensato per essere esposto a internet senza pensarci.** Di default
  ascolta su `127.0.0.1`. Prima di aprirlo verso l'esterno, cambia la master
  key e mettici davanti un reverse proxy — il modello di sicurezza è
  documentato nel [README inglese](README.md#security-model).
- **Non è un router aziendale con SLA.** È software scritto per essere usato
  ogni giorno con agenti reali. Se ti serve, prendilo e adattalo; se cerchi
  garanzie contrattuali, non è lo strumento giusto.

## In cinque comandi

```bash
git clone https://github.com/BravoRicDev/scrocco-llm && cd scrocco-llm
cp .env.gateway.example .env.gateway                      # OBBLIGATORIO: poi cambia la master key
cp var/keys_rotation.csv.example var/keys_rotation.csv
docker compose up -d                                      # solo gateway (profilo minimale)
curl -s localhost:4001/bootstrap        # playbook guidato, in inglese
# ...registri le chiavi sui provider, le inserisci via API...
curl -s localhost:4001/v1/chat/completions \
  -H "Authorization: Bearer sk-myteam" \
  -H "Content-Type: application/json" \
  -d '{"model":"scrocco-llm-myteam","messages":[{"role":"user","content":"ciao"}]}'
```

Da lì in poi basta aggiungere una chiave nel CSV quando ne trovi una nuova: il
gateway la carica a caldo in circa 5 secondi, senza restart.

## Documentazione

| Documento | Lingua | A cosa serve |
|---|---|---|
| [docs/BOOTSTRAP.md](docs/BOOTSTRAP.md) | EN | Setup zero-to-running, utile prima del primo avvio |
| `GET /bootstrap` | EN | Lo stesso playbook, servito live dal gateway |
| [docs/AGENT.md](docs/AGENT.md) | EN | Protocollo operativo day-2: admin API, ricette, log |
| `GET /admin/guide` | EN | Lo stesso documento, live |
| [var/gateway.yaml.example](var/gateway.yaml.example) | IT | Template commentato di tutte le policy |
| Docstring dei moduli | IT+EN | Cosa / come / perché di ogni decisione |

## Licenza

[Unlicense](LICENSE) — pubblico dominio. Usalo, modificalo, distribuiscilo come
preferisci.
