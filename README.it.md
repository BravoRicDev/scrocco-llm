<div align="right"><a href="README.md">🇬🇧 English</a> · <b>🇮🇹 Italiano</b></div>

# scrocco-llm

[![License: Unlicense](https://img.shields.io/badge/license-Unlicense-brightgreen.svg)](https://unlicense.org/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-compose%20up-blue.svg)](#in-cinque-comandi)
[![Tests](https://img.shields.io/badge/tests-176%20passing-brightgreen.svg)](#test)

**Un gateway LLM che ruota decine di chiavi free e a pagamento, sceglie il
modello più piccolo che regge il contesto, e prova a non sprecare chiamate
inutili.**

Zero database, un container, porta `4001`. È compatibile con l'API OpenAI,
quindi i client puntano al gateway senza dover cambiare nulla.

---

## Il problema che provo a risolvere

Se ti registri su più provider free — Groq, Google AI Studio, Mistral,
NVIDIA, OpenRouter — ti ritrovi con account diversi, finestre di contesto
diverse e modi diversi di fallire. In pratica: un agente che gira da solo
prima o poi trova un 429 nel momento sbagliato, oppure il modello scelto
non regge il prompt e taglia la risposta senza dirlo chiaramente.

Ho scritto scrocco-llm perché gestisco alcuni agenti che lavorano senza
supervisione e questi due problemi mi capitavano abbastanza spesso da
volerli automatizzare via.

## Cosa fa, in tre punti

**Sceglie il modello minimo che ci sta.** Stima i token del prompt e lo
manda al gruppo di contesto più piccolo che basta (`-32k`, `-128k`,
`-1000k`...), così non si spreca un modello grande su un prompt piccolo e
non si scopre a risposta finita che quello piccolo ha tagliato.

**Ruota prima di rompersi, non solo dopo.** Un 429 mette la chiave in
cooldown con backoff esponenziale, ma il gateway prova anche a imparare i
limiti di ogni chiave dai 429 osservati e a deprioritizzare quelle vicine
alla soglia, per ridurre quante volte si finisce contro il muro.

**Non confonde un fallback con un degrado silenzioso.** Una richiesta di
vision non atterra su un modello text-only. Uno scarto di QC sul JSON
consegna comunque l'ultimo tentativo, annotato, invece di un 500 secco. Lo
streaming ha un watchdog che nota se l'upstream muore a metà.

## Un principio che guida il design

Alcuni free-tier contano le chiamate, non i token — e questo ha influenzato
parecchie decisioni. Un probe che testa periodicamente la salute delle
chiavi è comune, ma su questi provider rischia di bruciare quota gratuita
senza mai usarla per davvero.

Per questo qui il probe fa **una chiamata reale per chiave, una volta
sola**, e il risultato resta su disco. Una chiave sana non viene richiamata
automaticamente; per riverificarla serve un `force=true` esplicito. Lo
stesso principio vale per il budget guard: i limiti si imparano dai 429
osservati, non si indovinano — finché non c'è evidenza, il gateway non
tocca niente.

## Alcune decisioni di design

Ogni modulo del codice parte con una docstring bilingue IT/EN che spiega
cosa fa, come lo fa e perché è stato fatto in un certo modo — è la parte
del progetto di cui vado più fiero, perché tende a invecchiare meglio dei
commenti sparsi. Tre esempi:

- **Cooldown massimo a 5 ore, non 24.** I free-tier tendono a rinnovarsi su
  finestre brevi; tenere una chiave ferma un giorno intero sembrava più
  spreco che prudenza.
- **I generatori (`*_gen`) sono separati dalla chat a livello strutturale.**
  Una richiesta testo non deve atterrare su un endpoint immagini per
  errore, e viceversa — è una regola nel routing, non un filtro a runtime
  aggiunto dopo.
- **Le chiavi morte vengono ritirate, mai cancellate.** Dopo 7 giorni di
  fallimenti escono dal routing ma restano nel CSV: se ricarichi i crediti,
  un probe riuscito le riporta in vita da solo.

## Bug reali che ho trovato mentre lo usavo

Preferisco raccontare anche i problemi trovati per strada, non solo il
percorso liscio — mi sembra più onesto e probabilmente più utile a chi
valuta se usarlo.

**Le chiavi Mistral e Cloudflare erano morte dal primo giorno.**
L'inferenza dei prefissi provider aggiungeva `mistral/` e `cloudflare/` ai
nomi dei modelli — formato corretto per litellm, sbagliato per le chiamate
dirette via httpx che fa questo gateway. Per settimane quei deployment
rispondevano 400 "No such model" senza che nessuno se ne accorgesse,
perché la rotazione li aggirava semplicemente. L'ho trovato testando con
curl lo stesso account e modello senza prefisso: funzionava.

**Un errore "No such model" arrivava al client come se fosse colpa sua.**
Cloudflare risponde 400 con un formato proprietario, senza le firme di
errore standard. Un deployment rotto a fine catena passava l'errore grezzo
all'agente invece di far ruotare la richiesta. Ora c'è una regex dedicata e
la rotazione è uniforme su tutti i percorsi (chat, stream, immagini,
tts/stt, video).

**Un `max_tokens` troppo piccolo causava 5 tentativi sprecati e cooldown su
chiavi sane.** Con i reasoning model, un budget piccolo finisce tutto nel
ragionamento e il contenuto arriva vuoto; il gateway lo trattava come un
deployment rotto, bruciando la catena di fallback e raffreddando per 10
minuti chiavi perfettamente funzionanti. Ora riconosce
`finish_reason=length` e consegna comunque la risposta.

<a name="test"></a>176 test coprono queste regressioni: ognuno è nato da un
bug reale, non sono test scritti per riempire una percentuale.

## Cosa non è

Mi sembra corretto dirlo prima, non dopo:

- **Non è un modo per non pagare.** È un modo per usare fino in fondo
  quello che i free-tier offrono legalmente, e spendere il credito a
  pagamento solo dove serve davvero.
- **Non è plug-and-play a zero account.** Devi comunque registrarti sui
  provider e creare le chiavi — quello nessun software può farlo al posto
  tuo. Il playbook `GET /bootstrap` guida passo passo anche un agente AI in
  questa fase, e la validazione costa una sola chiamata per chiave, cached.
- **Non è pensato per essere esposto a internet senza pensarci.** Di
  default ascolta su `127.0.0.1`. Prima di aprirlo verso l'esterno, cambia
  la master key e mettici davanti un reverse proxy — il modello di
  sicurezza è documentato nel [README inglese](README.md#security-model).
- **Non è un router aziendale con SLA.** È software che scrivo e uso io
  ogni giorno in produzione con i miei agenti. Se ti serve, prendilo e
  adattalo pure; se cerchi garanzie contrattuali, non è lo strumento
  giusto.

## In cinque comandi

```bash
git clone https://github.com/BravoRicDev/scrocco-llm && cd scrocco-llm
cp var/keys_rotation.csv.example var/keys_rotation.csv
docker compose up -d
curl -s localhost:4001/bootstrap        # playbook guidato, in inglese
# ...registri le chiavi sui provider, le inserisci via API...
curl -s localhost:4001/v1/chat/completions \
  -H "Authorization: Bearer sk-miotteam" \
  -H "Content-Type: application/json" \
  -d '{"model":"scrocco-llm-miotteam","messages":[{"role":"user","content":"ciao"}]}'
```

Da lì in poi basta aggiungere una chiave nel CSV quando ne trovi una nuova:
il gateway la carica a caldo in circa 5 secondi, senza restart.

## Documentazione

| Documento | Lingua | A cosa serve |
|---|---|---|
| [docs/BOOTSTRAP.md](docs/BOOTSTRAP.md) | EN | Setup zero-to-running, utile prima del primo avvio |
| `GET /bootstrap` | EN | Lo stesso playbook, servito live dal gateway |
| [docs/AGENT.md](docs/AGENT.md) | IT | Protocollo operativo day-2: admin API, ricette, log |
| `GET /admin/guide` | IT | Lo stesso documento, live |
| Docstring dei moduli | IT+EN | Cosa / come / perché di ogni decisione |

## Licenza

[Unlicense](LICENSE) — pubblico dominio. Usalo, modificalo, distribuiscilo
come preferisci.

---

<div align="center">

Costruito e mantenuto mentre lo uso ogni giorno per i miei agenti —
[riccardomurru.it](https://www.riccardomurru.it)

</div>
